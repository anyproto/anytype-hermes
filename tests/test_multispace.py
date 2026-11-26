import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import json
import time

import httpx
import pytest

from gateway.config import HomeChannel, Platform, PlatformConfig
from anytype_plugin.adapter import AnytypeAdapter, standalone_send
from anytype_plugin.client import Client
from anytype_plugin.config import Settings, Target


class MultiHeart:
    def __init__(self):
        self.spaces = ['space-a', 'space-b']
        self.queues = defaultdict(asyncio.Queue)
        self.histories = defaultdict(list)
        self.objects = defaultdict(list)
        self.holds = {}
        self.errors = {}
        self.calls = []
        self.closed = set()

    def row(self, space):
        return {'id': 'chat', 'kind': 'discussion', 'parent_id': 'parent', 'name': space,
                'last_state_id': max((m['state_id'] for m in self.histories[space]), default='')}

    def event(self, space, event_type, **data):
        return {'type': event_type, 'space_id': space, **data}

    def incoming(self, space, n=1, *, mention=True, author='alice'):
        text = f'<mention object_id="{space}:bot">Agent</mention>' if mention else 'Follow-up'
        return {'id': f'm{n}', 'author_id': f'{space}:{author}', 'text': text,
                'state_id': f'{int(time.time()):08x}{n:016x}', 'order': f'{n:08d}'}

    def add(self, space, **kwargs):
        message = self.incoming(space, **kwargs)
        self.histories[space].append(message)
        self.queues[(space, 'chats')].put_nowait(self.event(
            space, 'message_added', chat_id='chat', kind='discussion', parent_id='parent', message=message))

    async def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        error = self.errors.get(path)
        if isinstance(error, list):
            error = error.pop(0) if error else None
        if error:
            return httpx.Response(error)
        if path == '/v2/auth/whoami':
            data = {'key': {'id': 'key-fingerprint'}}
        elif path == '/v2/spaces/stream':
            assert request.method == 'GET' and request.url.params['ids'] == 'full'
            assert 'last-event-id' not in request.headers
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                  stream=LiveStream(self, None, 'spaces'))
        elif path == '/v2/spaces':
            # Force pagination: one listing means all pages exactly once.
            offset = int(request.url.params['offset'])
            data = {'data': [{'id': s} for s in self.spaces[offset:offset + 1]],
                    'has_more': offset + 1 < len(self.spaces)}
        else:
            space = path.split('/')[3]
            if path.endswith('/stream'):
                kind = path.split('/')[-2]
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                      stream=LiveStream(self, space, kind))
            if path.endswith('/members/me'):
                data = {'id': f'{space}:bot', 'identity': 'bot-key'}
            elif path.endswith('/members'):
                data = {'data': [{'id': f'{space}:{name}', 'identity': name + '-key'}
                                 for name in ('bot', 'alice', 'bob')], 'has_more': False}
            elif path.endswith('/chats'):
                data = {'data': [self.row(space)], 'has_more': False}
            elif '/objects/' in path:
                data = {'id': 'parent', 'properties': {'name': space + ' report'}, 'markdown': 'Findings'}
            elif path.endswith('/messages') and request.method == 'GET':
                data = {'messages': self.histories[space], 'has_more': False}
            else:
                data = {'id': 'sent'}
        return httpx.Response(200, json=data)


class LiveStream(httpx.AsyncByteStream):
    def __init__(self, heart, space, kind):
        self.heart, self.space, self.kind = heart, space, kind

    async def __aiter__(self):
        h, space, kind = self.heart, self.space, self.kind
        hold = h.holds.get((space, kind))
        if hold:
            await hold.wait()
        initial = ([h.event(space, 'chat_added', chat=h.row(space))] if kind == 'chats' else
                   [h.event(space, 'object_added', object=o) for o in h.objects[space]])
        if kind == 'spaces':
            initial = [{'type': 'space_added', 'space': {'id': s}} for s in h.spaces]
            boundary = {'type': 'snapshot_complete', 'has_not_granted_spaces': False}
        else:
            boundary = h.event(space, 'snapshot_complete')
        for event in initial:
            yield self.encode(event)
        boundary_gate = h.holds.get((space, kind, 'boundary'))
        if boundary_gate:
            await boundary_gate.wait()
        yield self.encode(boundary)
        while True:
            event = await h.queues[(space, kind)].get()
            if event is None:
                return
            yield self.encode(event)

    def encode(self, event):
        return ('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()

    async def aclose(self):
        self.heart.closed.add((self.space, self.kind))


@pytest.fixture
async def fleet():
    heart = MultiHeart()
    adapters = []

    def create():
        config = PlatformConfig(enabled=True, extra={
            'discovery': {'spaces': 'all'}, 'group_allow_from': ['alice-key'],
            'group_allow_admin_from': ['alice-key'], 'activity_status': False,
        })
        adapter = AnytypeAdapter(config)
        adapter.client = Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart))
        adapters.append(adapter)
        return adapter

    yield heart, create
    for adapter in adapters:
        await adapter.disconnect()


async def eventually(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def test_all_spaces_config_keeps_single_space_compatible():
    settings = Settings.load({'discovery': {'spaces': 'all'}})
    assert settings.discovery_enabled and settings.discovers('new-space')
    assert Settings.load({'discovery': {'space_id': 'a'}}).discovers('a')
    for discovery in ({'spaces': []}, {'spaces': 'all', 'space_id': 'a'}):
        with pytest.raises(ValueError):
            Settings.load({'discovery': discovery})


async def test_independent_readiness_identity_context_and_same_ids(fleet):
    heart, create = fleet
    heart.holds[('space-b', 'search')] = asyncio.Event()
    adapter = create()
    adapter.config.home_channel = HomeChannel(platform=Platform('anytype'), chat_id='space-a/home', name='Home')
    handled = []

    async def answer(event):
        handled.append(event)
        return 'Answer in ' + event.source.scope_id

    adapter.set_message_handler(answer)
    assert await adapter.connect()
    await asyncio.wait_for(adapter.discovery.for_space('space-a').ready.wait(), 5)
    heart.add('space-a')
    await eventually(lambda: len(adapter.store.inputs(['handed_off'])) == 1)
    assert not adapter.discovery.for_space('space-b').ready.is_set()
    heart.add('space-b')
    heart.holds[('space-b', 'search')].set()
    await eventually(lambda: len(adapter.store.inputs(['handed_off'])) == 2)
    await eventually(lambda: len([r for r in heart.calls if r.method == 'POST' and r.url.path.endswith('/messages')]) == 2)
    assert set(adapter._targets) == {'space-a/chat', 'space-b/chat'}
    assert {e.source.scope_id for e in handled} == {'space-a', 'space-b'}
    assert len({adapter._event_session_key(e) for e in handled}) == 2
    for event in handled:
        assert event.source.scope_id + ' report' in event.channel_context
        assert event.source.thread_id == 'chat'
        destination = 'anytype' if event.source.scope_id == 'space-a' else 'origin'
        assert f'deliver="{destination}"' in event.channel_prompt
    assert adapter._allowed == {'space-a': {'space-a:alice'}, 'space-b': {'space-b:alice'}}
    sends = [r for r in heart.calls if r.method == 'POST' and r.url.path.endswith('/messages')]
    assert {r.url.path.split('/')[3] for r in sends} == {'space-a', 'space-b'}
    assert all(json.loads(r.content)['reply_to'] == 'm1' for r in sends)
    searches = [r for r in heart.calls if r.url.path.endswith('/search/stream')]
    assert {json.loads(r.content)['filter'] for r in searches} == {
        'creator = "space-a:bot"', 'creator = "space-b:bot"'}
    listings = [r for r in heart.calls if r.url.path == '/v2/spaces']
    assert not listings
    await adapter.disconnect()
    assert heart.closed == {(None, 'spaces')} | {(s, k) for s in heart.spaces for k in ('chats', 'search')}


async def test_restart_discovers_new_spaces_and_suspends_removed_destinations(fleet, monkeypatch):
    heart, create = fleet
    heart.errors['/v2/spaces/stream'] = 404
    adapter = create()
    adapter.set_message_handler(lambda event: None)
    assert await adapter.connect()
    for worker in adapter.discovery.workers.values():
        await asyncio.wait_for(worker.ready.wait(), 5)
    # Persist a watch without running a turn.
    adapter.store.reason('space-b/chat', 'mention')
    adapter.discovery.for_space('space-b')._sync_targets()
    heart.spaces[:] = ['space-a', 'space-c']
    assert set(adapter.discovery.workers) == {'space-a', 'space-b'}
    assert len([r for r in heart.calls if r.url.path == '/v2/spaces']) == 2
    first_boundary = adapter.discovery.for_space('space-a').bootstrap_boundary
    message = heart.incoming('space-c')
    message['state_id'] = first_boundary[:-1] + '1'
    heart.histories['space-c'] = [message]
    await adapter.disconnect()

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return super().now(tz) + timedelta(minutes=1)

    monkeypatch.setattr('anytype_plugin.adapter.datetime', Later)
    adapter = create()
    async def answer(event):
        return 'Recovered downtime request'
    adapter.set_message_handler(answer)
    assert await adapter.connect()
    for worker in adapter.discovery.workers.values():
        await asyncio.wait_for(worker.ready.wait(), 5)
    assert set(adapter.discovery.workers) == {'space-a', 'space-c'}
    await eventually(lambda: bool(adapter.store.inputs(['handed_off'])))
    assert adapter.store.inputs(['handed_off'])[0]['target'] == 'space-c/chat'
    assert not adapter.store.discovered('space-b/chat')['available']
    monkeypatch.setattr('anytype_plugin.adapter.Client', lambda *a, **kw:
                        Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart)))
    assert not (await standalone_send(adapter.config, 'space-b/chat', 'no'))['success']
    assert not (await standalone_send(adapter.config, 'unknown/chat', 'no'))['success']
    await eventually(lambda: bool([r for r in heart.calls if r.method == 'POST' and r.url.path.endswith('/messages')]))
    sends = [r for r in heart.calls if r.method == 'POST' and r.url.path.endswith('/messages')]
    assert len(sends) == 1 and '/space-c/' in sends[0].url.path


async def test_one_space_forbidden_does_not_suspend_other_space(fleet):
    heart, create = fleet
    heart.errors['/v2/spaces/space-b/chats/stream'] = 403
    heart.objects['space-a'] = [{'id': 'parent', 'discussion': 'chat'}]
    adapter = create()
    assert await adapter.connect()
    await asyncio.wait_for(adapter.discovery.for_space('space-a').ready.wait(), 5)
    await eventually(lambda: not adapter.discovery.for_space('space-b').authorized)
    assert adapter.discovery.available('space-a/chat')
    assert not adapter.discovery.available('space-b/chat')
    assert adapter.store.discovery_spaces()['space-b'] == 'auth_failed'
    result = await adapter.send('space-a/chat', 'still connected')
    assert result.success


async def test_transient_member_initialization_retries_without_relisting(fleet):
    heart, create = fleet
    heart.errors['/v2/spaces/space-b/members/me'] = [503]
    adapter = create()
    assert await adapter.connect()
    await asyncio.wait_for(adapter.discovery.for_space('space-a').ready.wait(), 5)
    await asyncio.wait_for(adapter.discovery.for_space('space-b').ready.wait(), 5)
    assert adapter._allowed['space-b'] == {'space-b:alice'}
    assert not [r for r in heart.calls if r.url.path == '/v2/spaces']


async def test_revoked_account_stops_all_spaces_and_closes_subscriptions(fleet):
    heart, create = fleet
    for space in heart.spaces:
        heart.objects[space] = [{'id': 'parent', 'discussion': 'chat'}]
    adapter = create()
    assert await adapter.connect()
    for worker in adapter.discovery.workers.values():
        await asyncio.wait_for(worker.ready.wait(), 5)
    heart.errors['/v2/spaces/space-b/chats/chat/messages'] = 401
    heart.add('space-b')
    await eventually(lambda: len(heart.closed) == 5)
    assert not adapter._targets
    assert not (await adapter.send('space-a/chat', 'must not send')).success


async def test_standalone_send_uses_current_startup_scope_and_matching_thread(fleet, monkeypatch):
    heart, create = fleet
    heart.objects['space-b'] = [{'id': 'parent', 'discussion': 'chat'}]
    adapter = create()
    assert await adapter.connect()
    await asyncio.wait_for(adapter.discovery.for_space('space-b').ready.wait(), 5)
    monkeypatch.setattr('anytype_plugin.adapter.Client', lambda *a, **kw:
                        Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart)))
    assert (await standalone_send(adapter.config, 'space-b/chat', 'answer', thread_id='chat'))['success']
    assert not (await standalone_send(adapter.config, 'space-b/chat', 'answer', thread_id='wrong'))['success']


async def test_stream_capacity_fails_explicitly_before_partial_watch(fleet):
    heart, create = fleet
    heart.spaces = [f'space-{i}' for i in range(17)]
    heart.errors['/v2/spaces/stream'] = 404
    adapter = create()
    assert not await adapter.connect()
    assert [r.url.path for r in heart.calls if r.url.path.endswith('/stream')] == ['/v2/spaces/stream']


async def test_live_add_remove_regrant_and_account_revocation(fleet, monkeypatch):
    heart, create = fleet
    adapter = create()
    received = []
    async def answer(event):
        received.append(event.source.scope_id)
        return 'Answer'
    adapter.set_message_handler(answer)
    assert await adapter.connect()
    queue = heart.queues[(None, 'spaces')]
    add = {'type': 'space_added', 'space': {'id': 'new'}}
    queue.put_nowait(add)
    queue.put_nowait(add)
    queue.put_nowait({'type': 'space_updated', 'space': {'id': 'new', 'name': 'Renamed'}})
    await eventually(lambda: 'new' in adapter._me and adapter.discovery.for_space('new').ready.is_set())
    heart.add('new')
    await eventually(lambda: received == ['new'])
    remove = {'type': 'space_removed', 'space_id': 'new', 'reason': 'unavailable'}
    queue.put_nowait(remove)
    queue.put_nowait(dict(remove, reason='gone'))
    await eventually(lambda: ('new', 'search') in heart.closed and ('new', 'chats') in heart.closed)
    assert 'new' not in adapter.store.discovery_spaces()
    assert not adapter.store.discovered('new/chat')['available']
    assert 'new:alice' not in adapter.config.extra['group_allow_from']
    assert not (await adapter.send('new/chat', 'stale')).success
    monkeypatch.setattr('anytype_plugin.adapter.Client', lambda *a, **kw:
                        Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart)))
    assert not (await standalone_send(adapter.config, 'new/chat', 'stale')).get('success')
    assert adapter.discovery.for_space('space-a').ready.is_set()
    queue.put_nowait(add)
    await eventually(lambda: adapter.discovery.available('new/chat') and adapter.discovery.for_space('new').ready.is_set())
    heart.add('new', n=2)
    await eventually(lambda: received == ['new', 'new'])
    assert len([r for r in heart.calls if r.url.path == '/v2/spaces/new/chats/stream']) == 2
    queue.put_nowait({'type': 'stream_closed', 'reason': 'key_expired'})
    await eventually(lambda: not adapter._running and (None, 'spaces') in heart.closed)
    assert not adapter._targets
    assert set(adapter.store.discovery_spaces().values()) == {'auth_failed'}


async def test_empty_start_and_reconnect_snapshot(fleet):
    heart, create = fleet
    heart.spaces = []
    adapter = create()
    assert await adapter.connect()
    queue = heart.queues[(None, 'spaces')]
    queue.put_nowait({'type': 'space_added', 'space': {'id': 'first'}})
    await eventually(lambda: adapter.discovery and adapter.discovery.for_space('first').ready.is_set())
    # Membership changes while disconnected. A partial snapshot cannot enroll.
    heart.spaces = ['second']
    gate = heart.holds[(None, 'spaces', 'boundary')] = asyncio.Event()
    queue.put_nowait(None)
    await eventually(lambda: not adapter.discovery.workers)
    await eventually(lambda: len([r for r in heart.calls if r.url.path == '/v2/spaces/stream']) == 2)
    assert not adapter._members and 'second' not in adapter.discovery.workers
    gate.set()
    await eventually(lambda: adapter.discovery.for_space('second') and adapter.discovery.for_space('second').ready.is_set())
    assert set(adapter.store.discovery_spaces()) == {'second'}
    assert not adapter.store.discovered('first/chat')['available']
    assert not any(r.url.path == '/v2/spaces' for r in heart.calls)


async def test_slot_opens_for_waiting_space(fleet):
    heart, create = fleet
    heart.spaces = [f'space-{i:02}' for i in range(17)]
    adapter = create()
    assert await adapter.connect()
    assert adapter.store.discovery_spaces()['space-16'] == 'waiting_for_slot'
    assert len(adapter.discovery.workers) == 16
    heart.queues[(None, 'spaces')].put_nowait({'type': 'space_removed', 'space_id': 'space-00', 'reason': 'gone'})
    await eventually(lambda: adapter.discovery.for_space('space-16') and adapter.discovery.for_space('space-16').ready.is_set())
    assert len(adapter.discovery.workers) == 16 and 'space-00' not in adapter._me


async def test_revocation_during_parent_lookup_prevents_handoff(fleet, monkeypatch):
    heart, create = fleet
    adapter = create()
    calls = []
    async def answer(event):
        calls.append(event.source.scope_id)
        return 'Answer'
    adapter.set_message_handler(answer)
    assert await adapter.connect()
    worker = adapter.discovery.for_space('space-a')
    await asyncio.wait_for(worker.ready.wait(), 5)
    started, release = asyncio.Event(), asyncio.Event()
    async def slow_parent(target):
        started.set()
        await release.wait()
        return 'old context'
    monkeypatch.setattr(worker, 'parent_context', slow_parent)
    heart.add('space-a')
    await asyncio.wait_for(started.wait(), 5)
    heart.queues[(None, 'spaces')].put_nowait({'type': 'space_removed', 'space_id': 'space-a', 'reason': 'access_removed'})
    await eventually(lambda: adapter.discovery.for_space('space-a') is None)
    release.set()
    heart.add('space-b')
    await eventually(lambda: calls == ['space-b'])


async def test_empty_restart_retires_saved_chats_and_preserves_first_cutoff(fleet):
    heart, create = fleet
    heart.spaces = []
    first = create()
    assert await first.connect()
    from anytype_plugin.cli import local_status
    assert local_status(first.home / 'anytype' / 'state.sqlite3') == {'status': 'waiting_for_first_space'}
    since = first._discovery_started
    await first.disconnect()
    heart.spaces = ['new']
    heart.objects['new'] = [{'id': 'parent', 'discussion': 'chat'}]
    second = create()
    assert await second.connect()
    await eventually(lambda: second.discovery.for_space('new').ready.is_set())
    assert second._discovery_started == since
    await second.disconnect()
    heart.spaces = []
    third = create()
    assert await third.connect()
    assert not third.store.discovery_spaces() and not third.store.discovered('new/chat')['available']
    assert not third.discovery.workers and not third._targets
    assert not third.store.objects('new')
    assert 'agent_created_object' not in third.store.discovered('new/chat')['reasons']


@pytest.mark.parametrize('empty', [False, True])
async def test_account_lock_error_remains_retryable(fleet, empty, monkeypatch):
    heart, create = fleet
    adapter = create()
    notified = asyncio.Event()
    adapter.set_fatal_error_handler(lambda _: notified.set())
    def locked(*args):
        adapter._set_fatal_error('anytype_lock', 'held by another gateway', retryable=True)
        return False
    monkeypatch.setattr(adapter, '_acquire_platform_lock', locked)
    if empty:
        heart.spaces = []
        assert await adapter.connect()
        heart.queues[(None, 'spaces')].put_nowait({'type': 'space_added', 'space': {'id': 'new'}})
        await asyncio.wait_for(notified.wait(), 5)
    else:
        assert not await adapter.connect()
    assert adapter.fatal_error_code == 'anytype_lock' and adapter.fatal_error_retryable
    assert adapter.fatal_error_message == 'held by another gateway'


async def test_new_home_preempts_watcher_and_offline_reconcile_preserves_assignment(fleet):
    heart, create = fleet
    heart.spaces = [f'space-{i:02}' for i in range(16)]
    adapter = create()
    adapter.config.home_channel = HomeChannel(platform=Platform('anytype'), chat_id='zz-home/chat', name='Home')
    assert await adapter.connect()
    heart.spaces.append('zz-home')
    heart.queues[(None, 'spaces')].put_nowait({'type': 'space_added', 'space': {'id': 'zz-home'}})
    await eventually(lambda: adapter.discovery.for_space('zz-home') and adapter.discovery.for_space('zz-home').ready.is_set())
    await eventually(lambda: all(w.ready.is_set() for w in adapter.discovery.workers.values()))
    health = adapter.store.discovery_spaces()
    assert health['space-15'] == 'waiting_for_slot' and health['zz-home'] == 'connected'
    await adapter.disconnect()
    before = len(heart.calls)
    offline = create()
    assert await offline.connect(receive=False)
    await offline.reconcile(Target('zz-home', 'chat'), deep=True)
    assert not any(r.url.path.endswith('/stream') for r in heart.calls[before:])
    assert offline.store.discovery_spaces() == health


async def test_regrant_does_not_restore_old_creator_watch(fleet, monkeypatch):
    heart, create = fleet
    heart.objects['space-a'] = [{'id': 'parent', 'discussion': 'chat'}]
    adapter = create()
    assert await adapter.connect()
    await eventually(lambda: adapter.discovery.for_space('space-a').ready.is_set())
    assert adapter.discovery.available('space-a/chat')
    q = heart.queues[(None, 'spaces')]
    q.put_nowait({'type': 'space_removed', 'space_id': 'space-a', 'reason': 'access_removed'})
    await eventually(lambda: 'space-a' not in adapter.discovery.workers)
    heart.objects['space-a'] = []
    gate = heart.holds[('space-a', 'search')] = asyncio.Event()
    q.put_nowait({'type': 'space_added', 'space': {'id': 'space-a'}})
    await eventually(lambda: adapter.discovery.for_space('space-a') and adapter.discovery.for_space('space-a').chat_ready)
    assert not adapter.discovery.available('space-a/chat')
    monkeypatch.setattr('anytype_plugin.adapter.Client', lambda *a, **kw:
                        Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart)))
    assert not (await standalone_send(adapter.config, 'space-a/chat', 'stale'))['success']
    gate.set()
    await eventually(lambda: adapter.discovery.for_space('space-a').ready.is_set())
    assert not adapter.discovery.available('space-a/chat')


async def test_stale_member_error_during_dispatch_leaves_other_spaces_running(fleet, monkeypatch):
    from anytype_plugin.client import APIError
    heart, create = fleet
    adapter = create()
    received = []
    async def answer(event):
        received.append(event.source.scope_id)
        return 'Answer'
    adapter.set_message_handler(answer)
    assert await adapter.connect()
    await eventually(lambda: all(w.ready.is_set() for w in adapter.discovery.workers.values()))
    # The receipt was admitted earlier. Delay the final membership check so
    # its directory response arrives after removal.
    entered, release = asyncio.Event(), asyncio.Event()
    original = adapter._author_allowed
    async def delayed(target, author):
        if target.space_id == 'space-a' and asyncio.current_task().get_coro().__name__ == '_dispatch_loop':
            entered.set()
            await release.wait()
            raise APIError(403)
        return await original(target, author)
    monkeypatch.setattr(adapter, '_author_allowed', delayed)
    heart.add('space-a')
    await asyncio.wait_for(entered.wait(), 5)
    heart.queues[(None, 'spaces')].put_nowait({'type': 'space_removed', 'space_id': 'space-a', 'reason': 'gone'})
    await eventually(lambda: 'space-a' not in adapter.discovery.workers)
    release.set()
    heart.add('space-b')
    await eventually(lambda: received == ['space-b'])
    assert adapter._running
