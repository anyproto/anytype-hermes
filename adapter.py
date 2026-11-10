"""Anytype SSE ingress as a normal Hermes platform adapter."""

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, ProcessingOutcome
from hermes_constants import get_hermes_home

from .client import APIError, Client, ProtocolError, ResyncRequired, event_payload
from .activity import ActivityPublisher
from . import tool_hooks
from .config import Settings, Target, resolve_api_key
from .formatting import object_link, utf16_len
from .sender import Sender
from .state import Store


log = logging.getLogger(__name__)
PLATFORM_HINT = (
    'You are replying in Anytype chat. Use concise text with links; chat blocks are unsupported. '
    'For research, task management or Anytype object work, load '
    'skill_view(name="anytype-platform:anytype"). Use the '
    'current conversation space unless the user explicitly requests another; '
    'tool permissions are configured separately. Let the gateway post '
    'your final reply; do not duplicate it using an MCP chat-send tool. '
    'For scheduled jobs requested here, use deliver="anytype" to send results to the configured '
    'Anytype home channel only when it belongs to this conversation space; otherwise use deliver="origin". '
    'Keep jobs in their originating space unless independently authorized.'
)


class AnytypeAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 8000
    SUPPORTS_MESSAGE_EDITING = False
    supports_status_text = True

    def __init__(self, config):
        super().__init__(config=config, platform=Platform('anytype'))
        extra = dict(config.extra or {})
        # Hermes may rebuild an adapter using the same PlatformConfig. Retain the
        # operator's identities when the shared gates contain their resolved IDs.
        previous = getattr(config, '_anytype_identity_settings', {})
        for key, values in previous.items():
            if extra.get(key) == values['resolved']:
                extra[key] = values['configured']
        self.settings = Settings.load(extra)
        self.home = get_hermes_home()
        self.client = None
        self.store = None
        self.sender = None
        self.activity = None
        self._activity_bindings = {}
        self._tasks = []
        self._wake = asyncio.Event()
        self._sync = asyncio.Lock()
        self._members, self._me, self._allowed, self._admins = {}, {}, {}, {}
        self._targets = {t.key: t for t in self.settings.targets}
        self._names = {}
        self._chat_meta = {}
        self.discovery = None
        self._spaces = self.settings.spaces
        self._account_identity = None

    @property
    def message_len_fn(self):
        return utf16_len

    def get_delivery_context(self, *, reply_to, metadata):
        # Opt into exact-payload recovery in the accompanying generic Hermes patch.
        return {'reply_to': reply_to, 'metadata': {
            key: value for key, value in (metadata or {}).items()
            if key in {'attachments', 'hermes_profile', 'thread_id'}
        }}

    async def _refresh_members(self, space):
        try:
            rows = [row async for row in self.client.rows(f'/v2/spaces/{quote(space, safe="")}/members')]
        except APIError as error:
            if self.discovery and error.status in {401, 403}:
                self._suspend_discovery(space, error.status)
            raise
        members = {row['id']: row for row in rows if row.get('id')}
        self._members[space] = members
        def resolve(configured):
            return {member_id for member_id, row in members.items()
                    if member_id in configured or row.get('identity') in configured}
        self._allowed[space] = resolve(self.settings.allow)
        self._admins[space] = resolve(self.settings.admins)
        self.config.extra['group_allow_from'] = (['*'] if self.settings.allow == {'*'}
                                                 else sorted(set().union(*self._allowed.values())))
        # A nonempty unresolved list must not disable Hermes's admin gate.
        self.config.extra['group_allow_admin_from'] = (
            sorted(set().union(*self._admins.values())) or ['__anytype_no_resolved_admin__']
        ) if self.settings.admins else []
        self.config._anytype_identity_settings = {
            key: {'configured': sorted(configured), 'resolved': list(self.config.extra[key])}
            for key, configured in [('group_allow_from', self.settings.allow),
                                    ('group_allow_admin_from', self.settings.admins)]
        }

    def _suspend_discovery(self, space, status):
        if not self.discovery:
            return
        watcher = self.discovery if status == 401 else self.discovery.for_space(space)
        if watcher:
            watcher.suspend()
        if status == 401:
            self._set_fatal_error('anytype_auth', 'Anytype credential is no longer accepted', retryable=False)

    async def _load_space(self, space):
        me = await self.client.request('GET', f'/v2/spaces/{quote(space, safe="")}/members/me')
        if not me.get('id') or not me.get('identity'):
            raise ProtocolError('Bot member lacks identity')
        if self._account_identity and me['identity'] != self._account_identity:
            raise ProtocolError('Spaces resolve to different API accounts')
        await self._refresh_members(space)
        if not self.settings.discovery_all_spaces:
            rows = [row async for row in self.client.rows(f'/v2/spaces/{quote(space, safe="")}/chats',
                                                          include='discussions')]
            chats = {row['id']: row for row in rows}
            for target in self.settings.targets:
                if target.space_id == space:
                    if target.chat_id not in chats:
                        raise ValueError('A configured chat does not exist in its space')
                    self._names[target.key] = chats[target.chat_id].get('name', 'Anytype chat')
                    self._chat_meta[target.key] = chats[target.chat_id]
        self._me[space] = me
        return me['identity']

    async def connect(self, *, is_reconnect=False, receive=True):
        try:
            self._discovery_started = datetime.now(timezone.utc).timestamp()
            if not hasattr(BasePlatformAdapter, 'get_delivery_context'):
                raise RuntimeError('Apply patches/hermes-delivery-context.patch before starting Anytype')
            if self.client is None:
                self.client = Client(self.settings.base_url, resolve_api_key({'api_key_env': self.settings.api_key_env}))
            await self.client.request('GET', '/v2/auth/whoami')
            if self.settings.discovery_all_spaces:
                # Exactly one complete listing per connect; no periodic space polling.
                rows = [row async for row in self.client.rows('/v2/spaces', ids='full')]
                self._spaces = tuple(dict.fromkeys(Target(row.get('id'), 'validate').space_id for row in rows))
                if not self._spaces:
                    raise ValueError('No accessible Anytype spaces; grant access and restart the gateway')
                if len(self._spaces) > 16:
                    raise ValueError('Heart currently supports at most 16 discovery spaces; narrow the API grant')
            identities = set()
            failures = {}
            for space in self._spaces:
                try:
                    identities.add(await self._load_space(space))
                except (APIError, httpx.HTTPError) as error:
                    if not self.settings.discovery_all_spaces or getattr(error, 'status', None) == 401:
                        raise
                    failures[space] = getattr(error, 'status', None)
                    log.warning('Anytype space initialization deferred: %s', type(error).__name__)
            if not identities and failures:
                raise APIError(403 if all(status == 403 for status in failures.values()) else 503)
            if len(identities) != 1:
                raise ProtocolError('Accessible spaces must resolve to one API account; none may be ready yet')
            self._account_identity = next(iter(identities))
            namespace = self.settings.base_url + '|' + self._account_identity
            if not self._acquire_platform_lock('anytype', namespace, 'Anytype API account'):
                await self.disconnect()
                return False
            self.store = Store(self.home / 'anytype' / 'state.sqlite3', namespace)
            if receive:
                self.store.recover_inputs()
                self.store.recover_deliveries()
            self.sender = Sender(self.client, self.store, self.settings)
            if self.settings.discovery_enabled:
                from .discovery import Discovery, DiscoveryManager
                self.store.set_discovery_spaces(self._spaces)
                self.discovery = (DiscoveryManager(self, self._spaces, failures) if self.settings.discovery_all_spaces
                                  else Discovery(self))
                self.client.target_allowed = self.discovery.available
            if receive and self.settings.activity_status:
                self.activity = ActivityPublisher(self.client, secrets=(resolve_api_key(
                    {'api_key_env': self.settings.api_key_env}),))
            for target in (() if self.discovery else self.settings.targets):
                if self.store.chat(target.key) is None:
                    messages, cursor = await self._history_scan(target, baseline=True)
                    self.store.baseline(target.key, messages, cursor)
            if not receive:
                return True
            self._mark_connected()
            self._tasks = [asyncio.create_task(self._dispatch_loop()), asyncio.create_task(self._periodic())]
            if self.discovery:
                self._tasks.extend(asyncio.create_task(task) for task in self.discovery.tasks())
            else:
                self._tasks.extend(asyncio.create_task(self._listen(target)) for target in self.settings.targets)
            for task in self._tasks:
                task.add_done_callback(self._task_done)
            self._wake.set()
            log.info('Anytype connected: %d chats; receipt counts %s', len(self._targets), self.store.summary())
            return True
        except (ValueError, RuntimeError, ProtocolError, APIError, httpx.HTTPError) as error:
            log.error('Anytype connect failed: %s', str(error))
            temporary = isinstance(error, httpx.HTTPError) or (
                isinstance(error, APIError) and (error.status == 429 or error.status >= 500))
            code = ('anytype_auth' if isinstance(error, APIError) and error.status in {401, 403}
                    else 'anytype_unavailable' if temporary
                    else 'anytype_incompatible' if isinstance(error, ProtocolError)
                    else 'anytype_connect')
            self._set_fatal_error(code, str(error), retryable=temporary)
            await self.disconnect()
            return False

    def _task_done(self, task):
        if task.cancelled() or task.exception() is None:
            return
        log.error('Anytype ingress paused after %s', type(task.exception()).__name__)
        self._set_fatal_error('anytype_background', 'Ingress paused; inspect local logs and receipt status', retryable=False)
        self._running = False
        for other in self._tasks:
            if other is not task:
                other.cancel()

    async def disconnect(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.cancel_background_tasks()
        for binding in self._activity_bindings.values():
            tool_hooks.unbind(binding)
        self._activity_bindings.clear()
        if self.activity:
            await self.activity.close()
            self.activity = None
        if self.client:
            await self.client.close()
            self.client = None
        if self.store:
            self.store.close()
            self.store = None
        self._release_platform_lock()
        self.discovery = None

    async def _history_scan(self, target, *, baseline=False, deep=False):
        checkpoint = self.store.chat(target.key) if self.store and not baseline else None
        boundary = checkpoint.get('latest_id') if checkpoint else None
        messages, before, cursor, covered = [], None, None, boundary is None
        for page_index in range(100 if deep or baseline else 10):
            page = await self.client.history(target, **({'before': before} if before else {}))
            batch = page.get('messages', [])
            if page_index == 0:
                cursor = page.get('state', {}).get('last_state_id')
            self._validate_messages(batch)
            messages = batch + messages
            covered = covered or any(m['id'] == boundary for m in batch)
            if not page.get('has_more') or (not baseline and not deep and covered and len(messages) >= 1000):
                return messages, cursor
            next_before = page.get('next_before')
            if not next_before or next_before == before:
                raise ProtocolError('History pagination made no progress')
            before = next_before
        raise ProtocolError('History coverage unresolved; run deeper reconciliation')

    @staticmethod
    def _validate_messages(messages):
        if not isinstance(messages, list) or any(
            not isinstance(m, dict) or any(not isinstance(m.get(k), str) or not m[k]
                                          for k in ('id', 'author_id'))
            or not isinstance(m.get('text', ''), str)
            or not isinstance(m.get('attachments', []), list)
            or any(not isinstance(a, dict) or not isinstance(a.get('id'), str) or not a['id']
                   for a in m.get('attachments', []))
            for m in messages
        ):
            raise ProtocolError('Message lacks stable message/author identity')

    async def reconcile(self, target, *, deep=False):
        if self.discovery:
            await self.discovery.recover(target, full=True)
            return
        async with self._sync:
            messages, cursor = await self._history_scan(target, deep=deep)
            for message in messages:
                await self._record(target, message)
            if cursor is not None:
                self.store.checkpoint(target.key, cursor)
            self.store.health(target.key, 'connected')
        self._wake.set()

    async def _record(self, target, message, cursor=None):
        self._validate_messages([message])
        if self.store.known(target.key, message['id']):
            if cursor is not None:
                self.store.checkpoint(target.key, cursor)
            return
        allowed = await self._author_allowed(target, message['author_id'])
        self.store.record(target.key, message, 'pending' if allowed else 'filtered', cursor)

    async def _author_allowed(self, target, author):
        if author not in self._members[target.space_id]:
            await self._refresh_members(target.space_id)
        return author != self._me[target.space_id]['id'] and (
            self.settings.allow == {'*'} or author in self._allowed[target.space_id])

    async def _frame(self, target, frame):
        async with self._sync:
            if not frame.data:
                if frame.id is not None:
                    self.store.checkpoint(target.key, frame.id)
                return
            payload = event_payload(frame)
            if frame.event == 'resync_required':
                raise ResyncRequired('Stream requested history reconciliation')
            if frame.event == 'message_added':
                await self._record(target, payload.get('message'), frame.id)
            elif frame.event in {'message_updated', 'message_deleted'}:
                message = payload.get('message') or {}
                message_id = message.get('id') or payload.get('message_id')
                if message_id:
                    self.store.refresh_message(target.key, message_id,
                                               message if frame.event == 'message_updated' else None)
            elif frame.event not in {'state_updated', 'reactions_updated', 'pinned_updated'}:
                log.warning('Anytype ignored unknown SSE event %s', frame.event)
        self._wake.set()

    async def _listen(self, target):
        backoff = 1
        server_retry = 0
        deep = False
        while self._running:
            try:
                await self.reconcile(target, deep=deep)
                deep = False
                cursor = self.store.chat(target.key)['cursor']
                async for frame in self.client.stream(target, cursor):
                    if frame.retry_ms is not None:
                        server_retry = frame.retry_ms / 1000
                    await self._frame(target, frame)
                    backoff = 1
                self.store.health(target.key, 'reconnecting')
            except APIError as error:
                if error.status in {401, 403, 404}:
                    self.store.health(target.key, 'auth_failed' if error.status != 404 else 'target_missing')
                    log.error('Anytype target suspended: HTTP %d', error.status)
                    return
                server_retry = error.retry_after or 0
                self.store.health(target.key, 'reconnecting')
                log.warning('Anytype stream refused with HTTP %d; waiting before retry', error.status)
            except ResyncRequired:
                deep = True
                self.store.health(target.key, 'gap_unresolved')
            except (ProtocolError, RuntimeError, httpx.HTTPError) as error:
                self.store.health(target.key, 'gap_unresolved' if isinstance(error, ProtocolError) else 'reconnecting')
                log.warning('Anytype stream will reconcile: %s', type(error).__name__)
            await asyncio.sleep(max(server_retry, min(60, backoff)) + random.random())
            backoff = min(60, backoff * 2)

    async def _periodic(self):
        while self._running:
            await asyncio.sleep(300)
            for space in list(self._members):
                try:
                    await self._refresh_members(space)
                except (APIError, ProtocolError, httpx.HTTPError):
                    log.warning('Anytype member refresh failed')
            targets = ([Target.parse(row['target']) for row in self.store.catalog()
                        if row['available'] and row['space_id'] in self._spaces] if self.discovery else self.settings.targets)
            for target in targets:
                if self.store.chat(target.key)['health'] in {'auth_failed', 'target_missing'}:
                    continue
                try:
                    await self.reconcile(target)
                except (APIError, ProtocolError, httpx.HTTPError):
                    self.store.health(target.key, 'gap_unresolved')
                    log.warning('Anytype periodic history reconciliation failed')

    def _event(self, target, message):
        text = message.get('text', '')
        discussion = self._chat_meta.get(target.key, {}).get('kind') == 'discussion'
        source = self.build_source(chat_id=target.key, scope_id=target.space_id,
                                   chat_type='group',
                                   thread_id=target.chat_id if discussion else None,
                                   chat_name=self._names.get(target.key), user_id=message['author_id'],
                                   user_name=message.get('author'), message_id=message['id'])
        context = []
        if message.get('blocks_text') and message['blocks_text'] != text:
            context.append('Additional message content:\n' + message['blocks_text'])
        attachments = message.get('attachments', [])
        if attachments:
            context.append('Attached objects:\n' + '\n'.join(
                object_link(target.space_id, a['id']) for a in attachments if a.get('id')))
        # Hermes merges queued texts, but keeps only the first channel_context.
        # Carry each input's attachments/block supplement through those merges.
        if self.discovery and context:
            text += '\n\n' + '\n\n'.join(context)
            context = []
        home = self.config.home_channel
        job_destination = ('anytype' if home and str(home.chat_id).startswith(target.space_id + '/')
                           else 'origin')
        event = MessageEvent(text=text, source=source, user_id=message['author_id'],
                             user_name=message.get('author'), message_id=message['id'], raw_message=message,
                             channel_context='\n\n'.join(context) or None,
                             channel_prompt=(f'Current Anytype space_id: {target.space_id}\nchat_id: {target.chat_id}\n'
                                             f'For scheduled jobs here, default to deliver="{job_destination}".'),
                             reply_to_message_id=message.get('reply_to'),
                             metadata={'anytype_input_ids': [message['id']],
                                       **({'thread_id': target.chat_id} if discussion else {})},
                             ingress_message_ids=[message['id']])
        if message.get('at'):
            try:
                event.timestamp = datetime.fromisoformat(message['at'].replace('Z', '+00:00'))
                if event.timestamp.tzinfo is None:
                    event.timestamp = event.timestamp.replace(tzinfo=timezone.utc)
            except (ValueError, AttributeError):
                log.warning('Anytype message has an invalid timestamp; using receipt time')
        reply = self.store.message(target.key, message.get('reply_to'))
        if reply:
            event.reply_to_text = reply.get('text')
            event.reply_to_author_id = reply.get('author_id')
            event.reply_to_author_name = reply.get('author')
            event.reply_to_is_own_message = reply.get('author_id') == self._me[target.space_id]['id']
        if not text and attachments:
            event.text = 'Please inspect the attached Anytype objects.'
        return event

    def _trigger(self, event):
        dynamic = self.discovery and event.source.chat_id not in {t.key for t in self.settings.targets}
        mode = self.settings.discovered_trigger if dynamic else self.settings.trigger
        if mode == 'all' or event.is_command():
            return True
        from tools.clarify_gateway import get_pending_for_session
        from tools.approval import get_pending_gateway_approval
        from tools.slash_confirm import get_pending as get_pending_slash_confirm
        session_key = self._event_session_key(event)
        if (get_pending_for_session(session_key, include_choice_prompts=True)
                or get_pending_gateway_approval(session_key) or get_pending_slash_confirm(session_key)):
            return True
        if dynamic:
            from .discovery import mention_ids
            return (self._me[event.source.scope_id]['id'] in mention_ids(event.raw_message) or
                    event.reply_to_is_own_message)
        value, prefix = event.text.lstrip(), self.settings.prefix
        if value.startswith(prefix) and (len(value) == len(prefix) or value[len(prefix)].isspace()):
            event.text = value[len(prefix):].lstrip()
            return bool(event.text or event.raw_message.get('attachments'))
        return False

    async def _resolve_reply(self, target, event):
        if not event.reply_to_message_id or event.reply_to_author_id:
            return
        before = None
        for _ in range(2):
            page = await self.client.history(target, limit=100, **({'before': before} if before else {}))
            for message in page.get('messages', []):
                if message.get('id') == event.reply_to_message_id:
                    event.reply_to_text = message.get('text')
                    event.reply_to_author_id = message.get('author_id')
                    event.reply_to_author_name = message.get('author')
                    event.reply_to_is_own_message = message.get('author_id') == self._me[target.space_id]['id']
                    return
            if not page.get('has_more') or not page.get('next_before'):
                break
            before = page['next_before']
        event.channel_context = ((event.channel_context or '') +
                                 f'\nReplied-to message {event.reply_to_message_id}: context unavailable.')

    async def _dispatch_loop(self):
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            for row in self.store.inputs(['pending']):
                target = self._targets.get(row['target'])
                if not target:
                    continue
                watcher = self.discovery.for_space(target.space_id) if self.discovery else None
                if self.discovery and (not watcher or not watcher.ready.is_set()):
                    continue
                event = self._event(target, json.loads(row['payload']))
                # Recheck current policy on recovery, not just the receipt's old admission.
                if self.settings.allow != {'*'} and event.user_id not in self._allowed[target.space_id]:
                    self.store.mark(target.key, event.message_id, 'filtered')
                    continue
                try:
                    await self._resolve_reply(target, event)
                except (APIError, httpx.HTTPError, ProtocolError):
                    event.channel_context = ((event.channel_context or '') + '\nReply context unavailable.')
                if self.discovery:
                    try:
                        parent = await watcher.parent_context(target)
                    except APIError as error:
                        if error.status in {401, 403, 404}:
                            if error.status == 401:
                                self._suspend_discovery(target.space_id, error.status)
                            self.store.unavailable(target.key)
                            watcher._sync_targets()
                        else:
                            self.store.health(target.key, 'context_unavailable')
                        continue
                    except (httpx.HTTPError, ProtocolError):
                        self.store.health(target.key, 'context_unavailable')
                        continue
                    if parent:
                        event.channel_context = ((event.channel_context or '') + '\n\n' + parent).strip()
                    current = self.store.receipt(target.key, event.message_id)
                    if (not self.discovery.available(target.key) or not watcher.ready.is_set() or
                            not current or current['status'] != 'pending' or current['payload'] != row['payload']):
                        self._wake.set()
                        continue
                if not self._trigger(event):
                    self.store.mark(target.key, event.message_id, 'ignored' if self.discovery else 'filtered')
                    continue
                if not await self._author_allowed(target, event.user_id):
                    self.store.mark(target.key, event.message_id, 'filtered')
                    continue
                if self.discovery:
                    current = self.store.receipt(target.key, event.message_id)
                    if (not self.discovery.available(target.key) or not current or
                            current['status'] != 'pending' or current['payload'] != row['payload']):
                        continue
                self.store.mark(target.key, event.message_id, 'dispatched')
                await self.handle_message(event)
                # Inline controls don't invoke processing hooks. The generic ingress hook
                # patch closes those outcomes; uncertain inputs are never auto-replayed.

    async def on_processing_start(self, event):
        if self.activity and event.source.chat_id in self._targets:
            previous = self._activity_bindings.pop(id(event), None)
            if previous:
                tool_hooks.unbind(previous)
            self._activity_bindings[id(event)] = tool_hooks.bind(
                self.activity, self._targets[event.source.chat_id])
        for message_id in self._receipt_ids(event):
            self.store.mark(event.source.chat_id, message_id, 'processing')

    async def on_processing_complete(self, event, outcome):
        binding = self._activity_bindings.pop(id(event), None)
        if binding:
            tool_hooks.unbind(binding)
        status = {ProcessingOutcome.SUCCESS: 'completed', ProcessingOutcome.CANCELLED: 'cancelled',
                  ProcessingOutcome.FAILURE: 'uncertain'}[outcome]
        for message_id in self._receipt_ids(event):
            self.store.mark(event.source.chat_id, message_id, status)

    @staticmethod
    def _receipt_ids(event):
        return set(getattr(event, 'ingress_message_ids', []) or
                   event.metadata.get('anytype_input_ids', [event.message_id]))

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if (not self.sender or chat_id not in self._targets or
                (self.discovery and not self.discovery.available(chat_id))):
            return SendResult(False, error='Anytype sender unavailable or target unconfigured')
        thread = (metadata or {}).get('thread_id')
        if thread and (self._chat_meta.get(chat_id, {}).get('kind') != 'discussion' or
                       str(thread) != self._targets[chat_id].chat_id):
            return SendResult(False, error='Anytype discussion thread does not match destination')
        return await self.sender.send(self._targets[chat_id], content, reply_to, metadata)

    async def send_typing(self, chat_id, metadata=None):
        if self.activity and chat_id in self._targets:
            await self.activity.typing(self._targets[chat_id], self._status_text.get(str(chat_id)))

    async def send_or_update_status(self, chat_id, status_key, content, metadata=None):
        # Hermes's optional adapter status sink keeps routine labels ephemeral.
        from gateway.warning_notifications import is_warning_status
        if self.activity and not is_warning_status(status_key, content):
            if (chat_id not in self._targets or
                    (self.discovery and not self.discovery.available(chat_id))):
                return SendResult(False, error='Anytype status target unavailable')
            self.activity.set_text(self._targets[chat_id], content)
            return SendResult(True)
        return await self.send(chat_id, content, metadata=metadata)

    async def stop_typing(self, chat_id):
        self._status_text.pop(str(chat_id), None)
        if self.activity and chat_id in self._targets:
            self.activity.stop_typing(self._targets[chat_id])

    async def get_chat_info(self, chat_id):
        if chat_id not in self._targets or (self.discovery and not self.discovery.available(chat_id)):
            raise ValueError('Anytype target is not configured')
        return {'name': self._names.get(chat_id, 'Anytype chat'),
                'type': 'group',
                'chat_id': chat_id}


def validate_config(config):
    try:
        Settings.load(config.extra or {})
        return True
    except (TypeError, ValueError):
        return False


async def standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    if media_files:
        return {'success': False, 'error': 'Anytype standalone sends support text and object links'}
    adapter = AnytypeAdapter(pconfig)
    # No listeners or input baseline during one-shot delivery.
    try:
        target = Target.parse(chat_id)
        if target not in adapter.settings.targets and not adapter.settings.discovers(target.space_id):
            raise ValueError('Anytype target is not configured')
        client = Client(adapter.settings.base_url, resolve_api_key({'api_key_env': adapter.settings.api_key_env}))
        try:
            me = await client.request('GET', f'/v2/spaces/{quote(target.space_id, safe="")}/members/me')
            store = Store(adapter.home / 'anytype' / 'state.sqlite3', adapter.settings.base_url + '|' + me['identity'])
            try:
                def permitted(key):
                    if key != target.key:
                        return False
                    row = store.discovered(key)
                    if adapter.settings.discovery_all_spaces:
                        health = store.discovery_spaces().get(target.space_id)
                        if health not in {'connected', 'discovering', 'reconnecting', 'stream_capacity'} or row is None:
                            return False
                    if target in adapter.settings.targets:
                        return row is None or bool(row['available'])
                    reasons = set(row['reasons']) - {'pinned'} if row else set()
                    if not adapter.settings.watch_agent_objects:
                        reasons.discard('agent_created_object')
                    return bool(adapter.settings.discovers(target.space_id) and row and
                                row['available'] and reasons)
                if not permitted(target.key):
                    raise ValueError('Anytype target is not watched or available')
                # Check the current live directory; stale offline state is not authorization.
                chats = [r async for r in client.rows(f'/v2/spaces/{quote(target.space_id, safe="")}/chats',
                                                       include='discussions')]
                row = next((r for r in chats if r.get('id') == target.chat_id), None)
                if row is None:
                    raise ValueError('Anytype target is no longer available')
                if thread_id and (row.get('kind') != 'discussion' or str(thread_id) != target.chat_id):
                    raise ValueError('Anytype discussion thread does not match destination')
                client.target_allowed = permitted
                result = await Sender(client, store, adapter.settings).send(
                    target, message, metadata={'thread_id': str(thread_id)} if thread_id else None)
                return {'success': result.success, 'message_id': result.message_id, 'error': result.error}
            finally:
                store.close()
        finally:
            await client.close()
    except (ValueError, ProtocolError, APIError, httpx.HTTPError) as error:
        return {'success': False, 'error': str(error)}


def register(ctx):
    from .cli import dispatch, setup_parser
    tool_hooks.register(ctx)
    ctx.register_platform(
        name='anytype', label='Anytype', adapter_factory=AnytypeAdapter,
        check_fn=lambda: True, validate_config=validate_config,
        is_connected=lambda cfg: validate_config(cfg) and bool(resolve_api_key(cfg.extra or {}, required=False)),
        required_env=[], allowed_users_env='ANYTYPE_ALLOWED_USERS',
        allow_all_env='ANYTYPE_ALLOW_ALL_USERS', standalone_sender_fn=standalone_send,
        cron_deliver_env_var='ANYTYPE_HOME_CHANNEL',
        max_message_length=8000, emoji='◈', platform_hint=PLATFORM_HINT, display_tier='low',
    )
    ctx.register_cli_command(name='anytype', help='Set up Anytype, diagnose the connection, or inspect local receipts',
                             setup_fn=setup_parser, handler_fn=dispatch)
