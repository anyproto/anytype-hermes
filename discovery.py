"""Independent space watchers against Heart's snapshot-based chat/search streams."""

import asyncio
from collections import defaultdict
import html
import json
import logging
import random
import re
from urllib.parse import quote

import httpx

from .client import APIError, ProtocolError, event_payload
from .config import Target
from .formatting import object_link


log = logging.getLogger(__name__)
HISTORY_PAGES = 100
CONTEXT_CHARS = 12000
_MENTION = re.compile(r'<mention\s+object_id="([^"]+)"\s*>.*?</mention>', re.DOTALL)


def mention_ids(message):
    """Read canonical API markup, skipping backslash escapes and code spans/fences."""
    found = set()
    for text in (message.get('text', ''), message.get('blocks_text', '')):
        i = 0
        while i < len(text):
            if text[i] == '\\':
                i += 2
                continue
            if text[i] == '`':
                end = i
                while end < len(text) and text[end] == '`':
                    end += 1
                close = re.search(r'(?<!`)`{' + str(end - i) + r'}(?!`)', text[end:])
                i = end + close.end() if close else (len(text) if end - i >= 3 else end)
                continue
            match = _MENTION.match(text, i)
            if match:
                found.add(html.unescape(match[1]))
                i = match.end()
            else:
                i += 1
    return found


class Discovery:
    def __init__(self, adapter, space=None):
        self.adapter = adapter
        self.store, self.client = adapter.store, adapter.client
        self.space = space or adapter.settings.discovery_space
        self.ready = asyncio.Event()
        self.stopped = asyncio.Event()
        self.chat_ready = False
        self.objects_ready = not adapter.settings.watch_agent_objects
        self.authorized = True
        self._locks = defaultdict(asyncio.Lock)
        self._chat_snapshot = None
        self._object_snapshot = None
        self._pins = {t.key for t in adapter.settings.targets}
        self.bootstrap_boundary = self.store.bootstrap_boundary(
            self.space, since=getattr(adapter, '_discovery_started', None))
        for row in self.store.catalog(self.space):
            self.store.reason(row['target'], 'pinned', row['target'] in self._pins)
        if not adapter.settings.watch_agent_objects:
            self.store.set_objects(self.space, {})
        self._sync_targets()

    def tasks(self):
        tasks = [self.listen('chats'), self.retry_observed()]
        if self.adapter.settings.watch_agent_objects:
            tasks.append(self.listen('objects'))
        return tasks

    def for_space(self, space):
        return self if space == self.space else None

    def available(self, key):
        row = self.store.discovered(key)
        reasons = set(row['reasons']) if row else set()
        if key not in self._pins:
            reasons.discard('pinned')
        if not self.adapter.settings.watch_agent_objects:
            reasons.discard('agent_created_object')
        return bool(self.authorized and self.space in self.adapter._me and row and row['space_id'] == self.space and
                    row['available'] and reasons)

    def suspend(self):
        self.authorized = False
        self.ready.clear()
        self.stopped.set()
        self.store.space_health(self.space, 'auth_failed')
        self._sync_targets()
        for row in self.store.catalog(self.space):
            self.store.health(row['target'], 'auth_failed')

    def _sync_targets(self):
        self.adapter._targets = {key: target for key, target in self.adapter._targets.items()
                                 if target.space_id != self.space}
        for row in self.store.catalog(self.space):
            key = row['target']
            self.adapter._names[key] = row['name']
            self.adapter._chat_meta[key] = row
            if self.available(key):
                self.adapter._targets[key] = Target(self.space, row['chat_id'])

    def _set_ready(self):
        if self.chat_ready and self.objects_ready and self.authorized:
            self.ready.set()
            self.store.space_health(self.space, 'connected')
            self.adapter._wake.set()

    def _validate_scope(self, payload):
        if payload.get('space_id') != self.space:
            raise ProtocolError('Subscription returned a different space')

    async def listen(self, kind):
        backoff = 1
        while self.adapter._running and self.authorized:
            if kind == 'chats':
                self._chat_snapshot = {}
                self.chat_ready = False
                self.ready.clear()
            else:
                self._object_snapshot = {}
            search = None
            if kind == 'objects':
                member = self.adapter._me[self.space]['id']
                search = {'filter': 'creator = ' + json.dumps(member), 'fields': ['name', 'discussion']}
            retry = 0
            try:
                async for frame in self.client.subscription(self.space, search=search):
                    if not frame.data:
                        continue
                    payload = event_payload(frame)
                    self._validate_scope(payload)
                    # HTTP errors from processing a chat must not be mistaken for
                    # subscription-open failures (a deleted chat is not a dead space).
                    try:
                        await (self.chat_frame(payload) if kind == 'chats' else self.object_frame(payload))
                    except APIError as error:
                        if error.status == 401:
                            raise
                        chat_id = payload.get('chat_id')
                        if kind == 'chats' and chat_id:
                            target = Target(self.space, chat_id)
                            if error.status in {403, 404}:
                                self.store.unavailable(target.key)
                                self._sync_targets()
                            else:
                                self.store.health(target.key, 'message_unconfirmed')
                            log.warning('Anytype input verification deferred: HTTP %s', error.status)
                        else:
                            raise ProtocolError('Discovery event processing needs reconciliation') from error
                    backoff = 1
            except APIError as error:
                if error.status in {401, 403, 404}:
                    self.adapter._suspend_discovery(self.space, error.status)
                    self.store.space_health(self.space, 'auth_failed' if error.status != 404 else 'stream_unavailable')
                    for row in self.store.catalog(self.space):
                        self.store.health(row['target'], 'auth_failed' if error.status != 404 else 'stream_unavailable')
                    if self.adapter.settings.discovery_all_spaces and error.status != 401:
                        return
                    raise RuntimeError(f'Anytype {kind} subscription unavailable: HTTP {error.status}') from error
                retry = error.retry_after or 0
                self.store.space_health(self.space, 'stream_capacity' if error.status == 429 else 'reconnecting')
                log.warning('Anytype %s subscription refused: HTTP %s', kind, error.status)
            except (httpx.HTTPError, ProtocolError, RuntimeError) as error:
                self.store.space_health(self.space, 'reconnecting')
                log.warning('Anytype %s subscription reconnecting: %s', kind, type(error).__name__)
            await asyncio.sleep(max(retry, backoff) + random.random())
            backoff = min(60, backoff * 2)

    async def chat_frame(self, payload):
        self._validate_scope(payload)
        event = payload['type']
        if event in {'chat_added', 'chat_updated'}:
            row = payload.get('chat')
            if not isinstance(row, dict) or row.get('kind') not in {'chat', 'discussion'}:
                raise ProtocolError('Invalid chat metadata')
            target = Target(self.space, row.get('id'))
            if row['kind'] == 'discussion' and not isinstance(row.get('parent_id'), str):
                raise ProtocolError('Discussion lacks parent identity')
            previous = self.store.chat(target.key)
            baseline = self._chat_snapshot is not None and not self.store.initialized(self.space) and previous is None
            self.store.upsert_chat(target, row, baseline=baseline)
            self.store.reason(target.key, 'pinned', target.key in self._pins)
            self._ownership_reason(target)
            self._sync_targets()
            if self._chat_snapshot is not None:
                self._chat_snapshot[target.key] = row
            elif event == 'chat_added':
                await self.recover(target)
            return
        if event == 'snapshot_complete':
            if self._chat_snapshot is None:
                raise ProtocolError('Duplicate chat snapshot boundary')
            snapshot = self._chat_snapshot
            for row in self.store.catalog(self.space):
                if row['target'] not in snapshot:
                    self.store.unavailable(row['target'])
            self._sync_targets()
            for key, row in snapshot.items():
                checkpoint = self.store.chat(key)['cursor']
                full = (not checkpoint or self.store.discovered(key)['baseline_needed'] or self.store.scan_incomplete(key) or
                        (row.get('last_state_id') or '') > checkpoint)
                await self.recover(Target.parse(key), full=full)
            self.store.finish_snapshot(self.space)
            self._chat_snapshot = None
            self.chat_ready = True
            self._set_ready()
            return
        target = Target(self.space, payload.get('chat_id'))
        if event == 'chat_removed':
            self.store.unavailable(target.key)
            self._sync_targets()
        elif event in {'message_added', 'message_updated'}:
            row = self.store.discovered(target.key)
            if not row or not row['available']:
                raise ProtocolError('Message arrived without eligible chat metadata')
            message = payload.get('message')
            self.adapter._validate_messages([message])
            if not isinstance(message.get('state_id'), str) or not re.fullmatch('[0-9a-f]{24}', message['state_id']):
                raise ProtocolError('Multi-chat messages require BSON creation state_id')
            async with self._locks[target.key]:
                origin = 'live' if event == 'message_added' else 'edit'
                old = self.store.message(target.key, message['id']) or {}
                if event == 'message_added' and message['state_id'] < self.bootstrap_boundary:
                    origin = 'recovery'
                if event == 'message_updated' and (await self.addressed(target, message)) and not (await self.addressed(target, old)):
                    origin = 'mention_edit'
                self.store.observe(target.key, message, origin=origin)
                # The hint is durable even when REST cannot see a precommit event yet.
                if event == 'message_added':
                    self.store.checkpoint_forward(target.key, message['state_id'])
                await self.consider(target, message['id'])
        elif event == 'message_deleted':
            message_id = payload.get('message_id')
            if isinstance(message_id, str) and message_id:
                self.store.refresh_message(target.key, message_id)
                receipt = self.store.receipt(target.key, message_id)
                if receipt and receipt['status'] in {'observed', 'baseline'}:
                    self.store.mark(target.key, message_id, 'deleted')
        elif event not in {'state_updated', 'reactions_updated', 'pinned_updated'}:
            raise ProtocolError('Unknown space chat event')
        # state_updated.last_state_id is never an acknowledgement.
        self.adapter._wake.set()

    def _ownership_reason(self, target):
        row = self.store.discovered(target.key)
        owned = self.store.objects(self.space)
        match = (self.adapter.settings.watch_agent_objects and row['kind'] == 'discussion' and
                 owned.get(row['parent_id']) == target.chat_id)
        self.store.reason(target.key, 'agent_created_object', match)

    async def object_frame(self, payload):
        self._validate_scope(payload)
        event = payload['type']
        objects = self._object_snapshot if self._object_snapshot is not None else self.store.objects(self.space)
        if event in {'object_added', 'object_updated'}:
            row = payload.get('object')
            if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
                raise ProtocolError('Search row lacks object identity')
            discussion = row.get('discussion')
            if discussion is not None:
                Target(self.space, discussion)
            objects[row['id']] = discussion
        elif event == 'object_removed':
            objects.pop(payload.get('object_id'), None)
        elif event == 'snapshot_complete':
            if self._object_snapshot is None:
                raise ProtocolError('Duplicate search snapshot boundary')
            self._object_snapshot = None
            self.objects_ready = True
        else:
            raise ProtocolError('Unknown search event')
        if self._object_snapshot is not None:
            return
        self.store.set_objects(self.space, objects)
        for row in self.store.catalog(self.space):
            target = Target.parse(row['target'])
            self._ownership_reason(target)
        self._sync_targets()
        await self.reconsider()
        self._set_ready()

    async def history(self, target, *, full=True):
        messages, before = [], None
        for _ in range(HISTORY_PAGES if full else 1):
            page = await self.client.history(target, **({'before': before} if before else {}))
            batch = page.get('messages')
            self.adapter._validate_messages(batch)
            if any(not isinstance(m.get('state_id'), str) or not re.fullmatch('[0-9a-f]{24}', m['state_id']) for m in batch):
                raise ProtocolError('Multi-chat history requires BSON creation state_id')
            messages = batch + messages
            if not page.get('has_more') or not full:
                return messages
            next_before = page.get('next_before')
            if not batch or not next_before or next_before == before:
                raise ProtocolError('History pagination made no progress')
            before = next_before
        raise ProtocolError('History exceeds 100000 messages; recovery coverage unresolved')

    async def recover(self, target, *, full=True):
        async with self._locks[target.key]:
            try:
                row = self.store.discovered(target.key)
                if not row or not row['available']:
                    return
                if full:
                    self.store.start_scan(target.key)
                messages = await self.history(target, full=full)
                for message in messages:
                    origin = ('baseline' if row['baseline_needed'] and message['state_id'] < self.bootstrap_boundary
                              else 'recovery')
                    old = self.store.receipt(target.key, message['id'])
                    if (old and old['status'] in {'baseline', 'ignored'} and old['payload'] and
                            await self.addressed(target, message) and
                            not await self.addressed(target, json.loads(old['payload']))):
                        origin = 'mention_edit'
                    self.store.observe(target.key, message, origin=origin,
                                       confirmed=True)
                    await self.consider(target, message['id'])
                if full:
                    present = {m['id'] for m in messages}
                    for receipt in self.store.inputs(['pending', 'observed']):
                        if receipt['target'] == target.key and receipt['message_id'] not in present:
                            # An absent precommit hint stays unresolved and retryable.
                            if self.store.confirmed(target.key, receipt['message_id']):
                                self.store.refresh_message(target.key, receipt['message_id'])
                                self.store.mark(target.key, receipt['message_id'], 'deleted')
                    self.store.complete_scan(target.key, max((m['state_id'] for m in messages), default=None),
                                             messages[-1]['id'] if messages else None)
            except APIError as error:
                if error.status == 401:
                    self.adapter._suspend_discovery(self.space, error.status)
                if error.status in {401, 403, 404}:
                    self.store.unavailable(target.key)
                    self._sync_targets()
                else:
                    self.store.health(target.key, 'gap_unresolved')
                log.warning('Anytype chat reconciliation failed: HTTP %s', error.status)
            except (httpx.HTTPError, ProtocolError, RuntimeError) as error:
                self.store.health(target.key, 'gap_unresolved')
                log.warning('Anytype chat reconciliation incomplete: %s', type(error).__name__)
        self.adapter._wake.set()

    async def find_message(self, target, message_id):
        before = None
        for _ in range(HISTORY_PAGES):
            try:
                page = await self.client.history(target, **({'before': before} if before else {}))
            except APIError as error:
                if error.status == 401:
                    self.adapter._suspend_discovery(self.space, error.status)
                raise
            batch = page.get('messages', [])
            self.adapter._validate_messages(batch)
            for message in batch:
                if message['id'] == message_id:
                    return message
            if not page.get('has_more'):
                return None
            next_before = page.get('next_before')
            if not batch or not next_before or next_before == before:
                raise ProtocolError('Message verification pagination made no progress')
            before = next_before
        raise ProtocolError('Message visibility unresolved within history bound')

    async def addressed(self, target, message):
        me = self.adapter._me[self.space]['id']
        if me in mention_ids(message):
            return True
        reply_id = message.get('reply_to')
        if reply_id:
            reply = self.store.message(target.key, reply_id) or await self.find_message(target, reply_id)
            return bool(reply and reply.get('author_id') == me)
        return False

    async def consider(self, target, message_id):
        receipt = self.store.receipt(target.key, message_id)
        row = self.store.discovered(target.key)
        if not receipt or receipt['status'] not in {'observed', 'pending'} or not receipt['payload']:
            return
        if not self.authorized or not row or not row['available']:
            return
        message = json.loads(receipt['payload'])
        if not await self.adapter._author_allowed(target, message['author_id']):
            self.store.mark(target.key, message_id, 'filtered')
            return
        potential = row['reasons'] or self.adapter._me[self.space]['id'] in mention_ids(message) or message.get('reply_to')
        if not potential:
            return
        if not self.store.confirmed(target.key, message_id):
            canonical = await self.find_message(target, message_id)
            if canonical is None:
                self.store.health(target.key, 'message_unconfirmed')
                return
            current = self.store.receipt(target.key, message_id)
            if not current or current['status'] not in {'observed', 'pending'} or current['payload'] != receipt['payload']:
                return
            self.store.observe(target.key, canonical, origin='verified', confirmed=True)
            message = canonical
            if not await self.adapter._author_allowed(target, message['author_id']):
                self.store.mark(target.key, message_id, 'filtered')
                return
        mention = await self.addressed(target, message)
        baseline = self.store.baseline_candidate(target.key, message_id)
        if baseline is not None:
            promote = mention and not await self.addressed(target, baseline)
            self.store.resolve_baseline_candidate(target.key, message_id, promote=promote)
            if not promote:
                return
        row = self.store.discovered(target.key)
        follows_watch = row and ('pinned' in row['reasons'] or 'agent_created_object' in row['reasons'] or
                                ('mention' in row['reasons'] and
                                 message.get('state_id', '') > self.store.watch_boundary(target.key)))
        if row and row['available'] and (follows_watch or mention):
            self.store.admit(target.key, message_id, mention=mention)
            self._sync_targets()
            self.adapter._wake.set()

    async def reconsider(self):
        for receipt in self.store.inputs(['observed']):
            target = Target.parse(receipt['target'])
            if target.space_id != self.space:
                continue
            try:
                async with self._locks[target.key]:
                    await self.consider(target, receipt['message_id'])
            except (APIError, httpx.HTTPError, ProtocolError, RuntimeError) as error:
                self.store.health(target.key, 'message_unconfirmed')
                log.warning('Anytype input waiting for verification: %s', type(error).__name__)

    async def retry_observed(self):
        while self.adapter._running and self.authorized:
            await asyncio.sleep(10)
            await self.reconsider()
            self.adapter._wake.set()

    async def parent_context(self, target):
        row = self.store.discovered(target.key)
        if not row or row['kind'] != 'discussion':
            return None
        path = f'/v2/spaces/{quote(self.space, safe="")}/objects/{quote(row["parent_id"], safe="")}'
        parent = await self.client.request('GET', path, params={'format': 'md', 'ids': 'full'})
        content = json.dumps(parent, ensure_ascii=False)
        if len(content) > CONTEXT_CHARS:
            content = content[:CONTEXT_CHARS] + '\n[Excerpt truncated; read the parent through MCP for more.]'
        return ('Discussion parent (source data, read when this input was admitted):\n' +
                object_link(self.space, row['parent_id']) + '\n' + content)


class DiscoveryManager:
    """Own the fixed startup space set; each watcher has independent readiness/I/O."""

    def __init__(self, adapter, spaces, failures):
        self.adapter = adapter
        self.failures = failures
        # A newly granted space may already contain a mention from downtime.
        # Baseline it against first multi-space activation, not this restart.
        adapter._discovery_started = adapter.store.discovery_epoch(adapter._discovery_started)
        adapter._targets = {}
        for row in adapter.store.catalog():
            if row['space_id'] not in spaces:
                adapter.store.unavailable(row['target'])
        self.workers = {space: Discovery(adapter, space) for space in spaces}
        for space, status in failures.items():
            if status in {403, 404}:
                self.workers[space].suspend()
                adapter.store.space_health(space, 'auth_failed' if status == 403 else 'space_unavailable')

    def for_space(self, space):
        return self.workers.get(space)

    def available(self, key):
        worker = self.for_space(Target.parse(key).space_id)
        return bool(worker and worker.available(key))

    def suspend(self):
        for worker in self.workers.values():
            worker.suspend()

    def tasks(self):
        return [self.run_space(worker) for worker in self.workers.values()]

    async def recover(self, target, *, full=True):
        worker = self.for_space(target.space_id)
        if worker and worker.authorized and target.space_id in self.adapter._me:
            await worker.recover(target, full=full)

    async def run_space(self, worker):
        backoff = 1
        while self.adapter._running and worker.authorized and worker.space not in self.adapter._me:
            try:
                await self.adapter._load_space(worker.space)
                worker._sync_targets()
            except APIError as error:
                if error.status in {401, 403, 404}:
                    self.adapter._suspend_discovery(worker.space, error.status)
                    return
                self.adapter.store.space_health(worker.space, 'reconnecting')
            except httpx.HTTPError:
                self.adapter.store.space_health(worker.space, 'reconnecting')
            except ProtocolError:
                worker.suspend()
                self.adapter.store.space_health(worker.space, 'initialization_failed')
                return
            if worker.space not in self.adapter._me:
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
        if not self.adapter._running or not worker.authorized:
            return
        tasks = [asyncio.create_task(coro) for coro in worker.tasks()]
        stopped = asyncio.create_task(worker.stopped.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stopped], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stopped and not task.cancelled() and task.exception() is not None:
                    worker.suspend()
                    log.error('Anytype space watcher stopped: %s', type(task.exception()).__name__)
        finally:
            for task in [*tasks, stopped]:
                task.cancel()
            await asyncio.gather(*tasks, stopped, return_exceptions=True)
