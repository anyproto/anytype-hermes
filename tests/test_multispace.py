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
            data = {}
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
        for event in [*initial, h.event(space, 'snapshot_complete')]:
            yield self.encode(event)
        while True:
            yield self.encode(await h.queues[(space, kind)].get())

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
    assert [r.url.params['offset'] for r in listings] == ['0', '1']
    assert all(r.url.params['ids'] == 'full' for r in listings)
    await adapter.disconnect()
    assert heart.closed == {(s, k) for s in heart.spaces for k in ('chats', 'search')}


async def test_restart_discovers_new_spaces_and_suspends_removed_destinations(fleet, monkeypatch):
    heart, create = fleet
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
    assert len([r for r in heart.calls if r.url.path == '/v2/spaces']) == 2


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
    await eventually(lambda: len(heart.closed) == 4)
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
    adapter = create()
    assert not await adapter.connect()
    assert not any(r.url.path.endswith('/stream') for r in heart.calls)
