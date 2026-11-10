import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import merge_pending_message_event
from anytype_plugin.adapter import AnytypeAdapter, standalone_send
from anytype_plugin.client import APIError, Client, Frame, ProtocolError
from anytype_plugin.config import Settings, Target
from anytype_plugin.discovery import Discovery, mention_ids
from anytype_plugin.state import Store


SPACE = 'space.full'
MENTION = '<mention object_id="bot">Agent</mention>'


def msg(n=1, text=MENTION, author='alice', state=None, **extra):
    return {'id': f'm{n}', 'state_id': state or f'{n:024x}', 'order': f'{n:08d}',
            'author_id': author, 'text': text, **extra}


class Heart:
    def __init__(self):
        self.histories = {}
        self.chats = {}
        self.calls = []
        self.parent = {'id': 'parent', 'type': 'page', 'properties': {'name': 'Research'}, 'markdown': 'Findings'}
        self.parent_wait = None
        self.fail_before = None

    async def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path.endswith('/members/me'):
            data = {'id': 'bot', 'identity': 'bot-key'}
        elif path.endswith('/members'):
            data = {'data': [{'id': 'bot', 'identity': 'bot-key'}, {'id': 'alice', 'identity': 'alice-key'},
                             {'id': 'bob', 'identity': 'bob-key'}], 'has_more': False}
        elif path.endswith('/chats'):
            data = {'data': list(self.chats.values()), 'has_more': False}
        elif '/objects/' in path:
            if self.parent_wait:
                await self.parent_wait.wait()
            data = self.parent
        elif path.endswith('/messages') and request.method == 'GET':
            chat = path.split('/')[-2]
            rows = self.histories.get(chat, [])
            before = request.url.params.get('before')
            if before == self.fail_before and before is not None:
                return httpx.Response(500)
            if before:
                rows = [m for m in rows if m['order'] < before]
            limit = int(request.url.params.get('limit', '1000'))
            batch = rows[-limit:]
            data = {'messages': batch, 'has_more': len(rows) > limit,
                    'state': {'last_state_id': max((m['state_id'] for m in rows), default='')}}
            if batch:
                data['next_before'] = batch[0]['order']
        elif request.method == 'POST':
            data = {'id': 'sent'}
        else:
            data = {}
        return httpx.Response(200, json=data)


@pytest.fixture
async def multi():
    config = PlatformConfig(enabled=True, extra={
        'discovery': {'space_id': SPACE}, 'group_allow_from': ['alice-key'],
        'group_allow_admin_from': ['alice-key'], 'activity_status': False,
    })
    adapter = AnytypeAdapter(config)
    heart = Heart()
    adapter.client = Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart))
    adapter.store = Store(adapter.home / 'anytype/state.sqlite3', adapter.settings.base_url + '|bot-key')
    adapter._me[SPACE] = {'id': 'bot', 'identity': 'bot-key'}
    await adapter._refresh_members(SPACE)
    adapter.discovery = Discovery(adapter)
    adapter.client.target_allowed = adapter.discovery.available
    adapter.discovery._chat_snapshot = {}
    adapter.discovery._object_snapshot = {}
    yield adapter, heart
    await adapter.disconnect()


async def chat(adapter, heart, ident='chat', parent=None):
    row = {'id': ident, 'name': 'Conversation', 'kind': 'discussion' if parent else 'chat',
           'last_state_id': max((m['state_id'] for m in heart.histories.get(ident, [])), default='')}
    if parent:
        row['parent_id'] = parent
    heart.chats[ident] = row
    await adapter.discovery.chat_frame({'type': 'chat_added', 'space_id': SPACE, 'chat': row})
    return Target(SPACE, ident)


async def snapshot(adapter, objects=()):
    for obj in objects:
        await adapter.discovery.object_frame({'type': 'object_added', 'space_id': SPACE, 'object': obj})
    await adapter.discovery.object_frame({'type': 'snapshot_complete', 'space_id': SPACE})
    await adapter.discovery.chat_frame({'type': 'snapshot_complete', 'space_id': SPACE})


async def added(adapter, heart, target, message, *, stored=True, event='message_added'):
    if stored:
        rows = heart.histories.setdefault(target.chat_id, [])
        rows[:] = sorted([m for m in rows if m['id'] != message['id']] + [message], key=lambda m: m['order'])
    row = adapter.store.discovered(target.key)
    await adapter.discovery.chat_frame({'type': event, 'space_id': SPACE, 'chat_id': target.chat_id,
                                        'kind': row['kind'], 'parent_id': row['parent_id'], 'message': message})


@pytest.mark.parametrize('text,expected', [
    (MENTION, {'bot'}), ('@Agent', set()), ('\\' + MENTION, set()),
    ('`' + MENTION + '`', set()), ('```html\n' + MENTION + '\n```', set()),
    ('`` ` ' + MENTION + ' ``', set()), ('`code` ' + MENTION, {'bot'}),
    ('&lt;mention object_id="bot"&gt;Agent&lt;/mention&gt;', set()),
    ('<mention object_id="other">Agent</mention>', {'other'}),
    ('```html\n' + MENTION, set()),
])
def test_markup_mentions(text, expected):
    assert mention_ids({'text': text}) == expected
    assert mention_ids({'text': '', 'blocks_text': text}) == expected


def test_discovery_configuration_is_single_space_and_optional():
    settings = Settings.load({'discovery': {'space_id': SPACE}})
    assert settings.spaces == (SPACE,) and settings.targets == ()
    with pytest.raises(ValueError, match='one space'):
        Settings.load({'discovery': {'space_id': SPACE}, 'chats': [{'space_id': 'other', 'chat_id': 'chat'}]})
    with pytest.raises(ValueError):
        Settings.load({'discovery': {'response': 'all'}})


async def test_authorized_mention_enrolls_once_and_self_or_denied_cannot(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await added(adapter, heart, target, msg(1, author='bob'))
    await added(adapter, heart, target, msg(2, author='bot'))
    assert not adapter.discovery.available(target.key)
    await added(adapter, heart, target, msg(3))
    await added(adapter, heart, target, msg(3))
    assert adapter.store.discovered(target.key)['reasons'] == ['mention']
    assert [r['message_id'] for r in adapter.store.inputs(['pending'])] == ['m3']
    await added(adapter, heart, target, msg(4, text='follow-up'))
    assert len(adapter.store.inputs(['pending'])) == 2


async def test_baseline_then_buffered_live_add_promotes_only_live_message(multi):
    adapter, heart = multi
    live = msg(2, state=adapter.discovery.bootstrap_boundary[:-1] + '1')
    heart.histories['chat'] = [msg(1), live]
    target = await chat(adapter, heart)
    await snapshot(adapter)
    # The persisted cutoff identifies the overlap even before its live event.
    assert [r['message_id'] for r in adapter.store.inputs(['pending'])] == ['m2']
    adapter.discovery = Discovery(adapter)  # crash/restart before buffered delivery
    await added(adapter, heart, target, live)
    assert [r['message_id'] for r in adapter.store.inputs(['pending'])] == ['m2']
    await adapter.discovery.recover(target)
    assert adapter.store.receipt(target.key, 'm1')['status'] == 'baseline'


async def test_historical_backfill_after_reappearance_does_not_revive_baseline(multi):
    adapter, heart = multi
    heart.histories['chat'] = [msg()]
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await adapter.discovery.chat_frame({'type': 'chat_removed', 'space_id': SPACE, 'chat_id': 'chat'})
    await chat(adapter, heart)
    await added(adapter, heart, target, msg())
    assert adapter.store.receipt(target.key, 'm1')['status'] == 'baseline'
    assert not adapter.discovery.available(target.key)


async def test_offline_first_mention_edit_promotes_baseline_but_unchanged_mentions_do_not(multi):
    adapter, heart = multi
    heart.histories['chat'] = [msg(1, text='ordinary'), msg(2)]
    target = await chat(adapter, heart)
    await snapshot(adapter)
    heart.histories['chat'][0] = msg(1, text=MENTION)
    await adapter.discovery.recover(target, full=False)
    assert adapter.store.receipt(target.key, 'm1')['status'] == 'pending'
    assert adapter.store.receipt(target.key, 'm2')['status'] == 'baseline'


async def test_rolled_back_first_mention_edit_keeps_baseline(multi):
    adapter, heart = multi
    heart.histories['chat'] = [msg(1, text='ordinary')]
    target = await chat(adapter, heart)
    await snapshot(adapter, [{'id': 'unused'}])
    await added(adapter, heart, target, msg(1), stored=False, event='message_updated')
    assert adapter.store.receipt(target.key, 'm1')['status'] == 'baseline'
    assert not adapter.discovery.available(target.key)


async def test_rolled_back_mention_removal_cannot_poison_baseline_comparison(multi):
    adapter, heart = multi
    heart.histories['chat'] = [msg()]
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await added(adapter, heart, target, msg(text='ordinary'), stored=False, event='message_updated')
    await adapter.discovery.recover(target)
    assert adapter.store.receipt(target.key, 'm1')['status'] == 'baseline'
    assert not adapter.discovery.available(target.key)
    # A confirmed removal updates the baseline, so a later real mention still works.
    heart.histories['chat'] = [msg(text='ordinary')]
    await adapter.discovery.recover(target)
    await added(adapter, heart, target, msg(), event='message_updated')
    assert adapter.store.receipt(target.key, 'm1')['status'] == 'pending'


async def test_late_creator_discovery_promotes_first_comment_and_survives_restart(multi):
    adapter, heart = multi
    target = await chat(adapter, heart, 'discussion', 'parent')
    await snapshot(adapter)
    await added(adapter, heart, target, msg(text='What about these findings?'))
    assert not adapter.store.inputs(['pending'])
    adapter.discovery = Discovery(adapter)
    await adapter.discovery.object_frame({'type': 'object_added', 'space_id': SPACE,
                                           'object': {'id': 'parent', 'discussion': 'discussion'}})
    assert adapter.discovery.available(target.key)
    assert adapter.store.inputs(['pending'])[0]['message_id'] == 'm1'


async def test_creator_before_chat_and_first_comment(multi):
    adapter, heart = multi
    await snapshot(adapter, [{'id': 'parent', 'discussion': 'discussion'}])
    heart.histories['discussion'] = [msg(text='A first comment')]
    target = await chat(adapter, heart, 'discussion', 'parent')
    assert adapter.discovery.available(target.key)
    assert len(adapter.store.inputs(['pending'])) == 1


async def test_search_snapshot_removal_reconciles_only_at_boundary(multi):
    adapter, heart = multi
    target = await chat(adapter, heart, 'discussion', 'parent')
    await snapshot(adapter, [{'id': 'parent', 'discussion': 'discussion'}])
    adapter.discovery._object_snapshot = {}
    assert adapter.discovery.available(target.key)
    await adapter.discovery.object_frame({'type': 'snapshot_complete', 'space_id': SPACE})
    assert not adapter.discovery.available(target.key)
    await added(adapter, heart, target, msg())
    await adapter.discovery.object_frame({'type': 'object_removed', 'space_id': SPACE, 'object_id': 'parent'})
    assert adapter.store.discovered(target.key)['reasons'] == ['mention']


async def test_first_mention_edit_enrolls_but_completed_edits_do_not_rerun(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await added(adapter, heart, target, msg(text='ordinary'))
    await added(adapter, heart, target, msg(text=MENTION), event='message_updated')
    assert len(adapter.store.inputs(['pending'])) == 1
    adapter.store.mark(target.key, 'm1', 'completed')
    await added(adapter, heart, target, msg(text=MENTION + ' edited'), event='message_updated')
    assert not adapter.store.inputs(['pending'])


async def test_counter_ahead_is_not_checkpoint_and_out_of_order_recovery(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await adapter.discovery.chat_frame({'type': 'state_updated', 'space_id': SPACE, 'chat_id': 'chat',
                                        'state': {'last_state_id': 'f' * 24}})
    assert adapter.store.chat(target.key)['cursor'] is None
    heart.histories['chat'] = [msg(1, state='3' * 24), msg(2, state='2' * 24)]
    await adapter.discovery.recover(target)
    assert adapter.store.chat(target.key)['cursor'] == '3' * 24
    assert len(adapter.store.inputs(['pending'])) == 2


async def test_full_recovery_finds_late_sync_outside_newest_thousand(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    heart.histories['chat'] = [msg(i, text='ordinary') for i in range(1, 1003)]
    await adapter.discovery.recover(target)
    heart.histories['chat'][0] = msg(1, state='f' * 24)
    await adapter.discovery.recover(target)
    assert adapter.store.receipt(target.key, 'm1')['status'] == 'pending'
    assert adapter.store.chat(target.key)['cursor'] == 'f' * 24


async def test_failed_page_prevents_live_checkpoint_skipping_gap(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    heart.histories['chat'] = [msg(i, text='ordinary') for i in range(1, 1003)]
    heart.fail_before = '00000003'
    await adapter.discovery.recover(target)
    assert adapter.store.scan_incomplete(target.key)
    assert adapter.store.chat(target.key)['cursor'] is None
    await added(adapter, heart, target, msg(1003))
    assert adapter.store.chat(target.key)['cursor'] is None
    heart.fail_before = None
    await adapter.discovery.recover(target)
    assert not adapter.store.scan_incomplete(target.key)
    assert adapter.store.chat(target.key)['cursor'] == f'{1003:024x}'


async def test_precommit_hint_retries_and_uses_canonical_payload(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await added(adapter, heart, target, msg(), stored=False)
    assert not adapter.discovery.available(target.key)
    assert adapter.store.chat(target.key)['health'] == 'message_unconfirmed'
    heart.histories['chat'] = [msg(text='rolled-back mention is absent')]
    await adapter.discovery.reconsider()
    assert not adapter.discovery.available(target.key)
    heart.histories['chat'] = [msg()]
    await added(adapter, heart, target, msg(), event='message_updated')
    assert adapter.discovery.available(target.key)


async def test_chat_removal_blocks_send_and_existing_activity_client(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await added(adapter, heart, target, msg())
    await adapter.discovery.chat_frame({'type': 'chat_removed', 'space_id': SPACE, 'chat_id': 'chat'})
    assert not (await adapter.send(target.key, 'answer')).success
    with pytest.raises(APIError):
        await adapter.client.publish_status(target, {'text': 'Searching'})
    with pytest.raises(APIError):
        await adapter.client.send(target, {'text': 'answer'}, 'key')


async def test_shared_discussion_session_and_merge_retains_each_attachment(multi):
    adapter, heart = multi
    target = await chat(adapter, heart, 'discussion', 'parent')
    await snapshot(adapter)
    a = adapter._event(target, msg(author='alice', attachments=[{'id': 'first'}]))
    b = adapter._event(target, msg(2, author='bob', attachments=[{'id': 'second'}]))
    assert adapter._event_session_key(a) == adapter._event_session_key(b)
    assert a.source.thread_id == 'discussion'
    queue = {}
    for event in (a, b):
        merge_pending_message_event(queue, 's', event, merge_text=True)
    assert 'first' in queue['s'].text and 'second' in queue['s'].text
    assert queue['s'].metadata['thread_id'] == 'discussion'


async def test_discussion_uses_real_hermes_group_authorization_and_admin_policy(multi):
    from gateway.run import GatewayRunner
    from gateway.config import Platform
    from gateway.slash_access import policy_from_extra
    adapter, heart = multi
    target = await chat(adapter, heart, 'discussion', 'parent')
    await snapshot(adapter)
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = None
    runner.adapters = {Platform('anytype'): adapter}
    runner._profile_adapters = {}
    runner._primary_profile_name = 'default'
    alice = adapter._event(target, msg(author='alice'))
    bob = adapter._event(target, msg(author='bob'))
    assert alice.source.chat_type == 'group' and alice.source.thread_id == 'discussion'
    assert runner._is_user_authorized(alice.source)
    assert not runner._is_user_authorized(bob.source)
    policy = policy_from_extra(adapter.config.extra, alice.source.chat_type)
    assert policy.can_run('alice', 'model') and not policy.can_run('bob', 'model')


@pytest.mark.parametrize('hook', ['tools.clarify_gateway.get_pending_for_session',
                                  'tools.approval.get_pending_gateway_approval',
                                  'tools.slash_confirm.get_pending'])
async def test_mentions_policy_accepts_pending_control_answers(multi, monkeypatch, hook):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    adapter.settings = replace(adapter.settings, discovered_trigger='mentions')
    event = adapter._event(target, msg(text='yes'))
    assert not adapter._trigger(event)
    monkeypatch.setattr(hook, lambda *args, **kwargs: object())
    assert adapter._trigger(event)


async def test_ignored_followup_can_gain_its_first_mention(multi):
    adapter, heart = multi
    target = await chat(adapter, heart)
    await snapshot(adapter)
    await added(adapter, heart, target, msg())
    adapter.store.mark(target.key, 'm1', 'completed')
    await added(adapter, heart, target, msg(2, text='ordinary'))
    adapter.settings = replace(adapter.settings, discovered_trigger='mentions')
    adapter.handle_message = AsyncMock()
    adapter._running = True
    task = asyncio.create_task(adapter._dispatch_loop())
    for _ in range(100):
        if adapter.store.receipt(target.key, 'm2')['status'] == 'ignored':
            break
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert adapter.store.receipt(target.key, 'm2')['status'] == 'ignored'
    await added(adapter, heart, target, msg(2), event='message_updated')
    assert adapter.store.receipt(target.key, 'm2')['status'] == 'pending'


async def test_parent_context_is_bounded_and_removed_input_not_dispatched(multi):
    adapter, heart = multi
    target = await chat(adapter, heart, 'discussion', 'parent')
    await snapshot(adapter, [{'id': 'parent', 'discussion': 'discussion'}])
    heart.parent['markdown'] = 'x' * 20000
    context = await adapter.discovery.parent_context(target)
    assert 'parent' in context and 'truncated' in context and len(context) < 12500
    await added(adapter, heart, target, msg(text='review this'))
    adapter.handle_message = AsyncMock()
    heart.parent_wait = asyncio.Event()
    adapter._running = True
    task = asyncio.create_task(adapter._dispatch_loop())
    for _ in range(100):
        if len([r for r in heart.calls if '/objects/' in r.url.path]) >= 2:
            break
        await asyncio.sleep(0)
    await adapter.discovery.chat_frame({'type': 'message_deleted', 'space_id': SPACE,
                                        'chat_id': target.chat_id, 'message_id': 'm1'})
    heart.parent_wait.set()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    adapter.handle_message.assert_not_called()


async def test_scope_mismatch_is_rejected(multi):
    adapter, _ = multi
    with pytest.raises(ProtocolError, match='different space'):
        await adapter.discovery.chat_frame({'type': 'snapshot_complete', 'space_id': 'other'})
    with pytest.raises(ProtocolError, match='different space'):
        await adapter.discovery.object_frame({'type': 'snapshot_complete', 'space_id': 'other'})


async def test_one_deleted_chat_does_not_shut_down_space_subscription(multi, monkeypatch):
    adapter, heart = multi
    bad = await chat(adapter, heart, 'bad')
    good = await chat(adapter, heart, 'good')
    await snapshot(adapter)
    done = asyncio.Event()
    original = adapter.discovery.find_message

    async def find(target, message_id):
        if target == bad:
            raise APIError(404)
        return await original(target, message_id)

    async def stream(space, search=None):
        for row in heart.chats.values():
            payload = {'type': 'chat_added', 'space_id': SPACE, 'chat': row}
            yield Frame('chat_added', json.dumps(payload))
        yield Frame('snapshot_complete', json.dumps({'type': 'snapshot_complete', 'space_id': SPACE}))
        for target in (bad, good):
            heart.histories[target.chat_id] = [msg()]
            yield Frame('message_added', json.dumps({'type': 'message_added', 'space_id': SPACE,
                         'chat_id': target.chat_id, 'kind': 'chat', 'message': msg()}))
        done.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter.discovery, 'find_message', find)
    monkeypatch.setattr(adapter.client, 'subscription', stream)
    adapter._running = True
    task = asyncio.create_task(adapter.discovery.listen('chats'))
    try:
        await asyncio.wait_for(done.wait(), 2)
        assert adapter.discovery.authorized
        assert not adapter.store.discovered(bad.key)['available']
        assert adapter.discovery.available(good.key)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_standalone_watched_discussion_and_wrong_scope(multi, monkeypatch):
    adapter, heart = multi
    target = await chat(adapter, heart, 'discussion', 'parent')
    await snapshot(adapter)
    await added(adapter, heart, target, msg())
    def client(*args, **kwargs):
        return Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart))
    monkeypatch.setattr('anytype_plugin.adapter.Client', client)
    assert (await standalone_send(adapter.config, target.key, 'answer', thread_id='discussion'))['success']
    assert not (await standalone_send(adapter.config, target.key, 'answer', thread_id='wrong'))['success']
    assert not (await standalone_send(adapter.config, 'other/chat', 'answer'))['success']
    adapter.store.unavailable(target.key)
    assert not (await standalone_send(adapter.config, target.key, 'answer'))['success']


async def test_standalone_respects_disabled_creator_policy_without_gateway_restart(multi, monkeypatch):
    adapter, heart = multi
    target = await chat(adapter, heart, 'discussion', 'parent')
    await snapshot(adapter, [{'id': 'parent', 'discussion': 'discussion'}])
    monkeypatch.setattr('anytype_plugin.adapter.Client', lambda *args, **kwargs:
                        Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(heart)))
    adapter.config.extra['discovery']['watch_agent_objects'] = False
    assert not (await standalone_send(adapter.config, target.key, 'answer'))['success']
    await added(adapter, heart, target, msg())
    assert (await standalone_send(adapter.config, target.key, 'answer'))['success']


async def test_post_search_subscription_wire_contract_and_no_write_lock():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                              content='event: snapshot_complete\ndata: {"type":"snapshot_complete","space_id":"space.full"}\n\n')
    client = Client('http://localhost:31009', 'test', transport=httpx.MockTransport(handler))
    body = {'filter': 'creator = "bot"', 'fields': ['discussion']}
    async with client._write_lock:
        frames = await asyncio.wait_for(_frames(client.subscription(SPACE, search=body)), 1)
    assert frames[0].event == 'snapshot_complete'
    assert requests[0].method == 'POST' and json.loads(requests[0].content) == body
    assert requests[0].url.params['ids'] == 'full'
    assert 'Last-Event-ID' not in requests[0].headers
    await client.close()


async def test_multichat_gateway_lifecycle_with_both_http_streams():
    heart = Heart()
    row = {'id': 'discussion', 'kind': 'discussion', 'parent_id': 'parent',
           'name': 'Research discussion', 'last_state_id': ''}
    heart.chats['discussion'] = row
    queues = {kind: asyncio.Queue() for kind in ('chats', 'search')}
    closed = set()
    complete = asyncio.Event()
    handled = []

    def frame(event_type, **payload):
        return {'type': event_type, 'space_id': SPACE, **payload}

    for event in (frame('chat_added', chat=row), frame('snapshot_complete')):
        queues['chats'].put_nowait(event)
    for event in (frame('object_added', object={'id': 'parent', 'discussion': 'discussion'}),
                  frame('snapshot_complete')):
        queues['search'].put_nowait(event)

    class Stream(httpx.AsyncByteStream):
        def __init__(self, kind):
            self.kind = kind

        async def __aiter__(self):
            while True:
                event = await queues[self.kind].get()
                yield ('event: ' + event['type'] + '\ndata: ' + json.dumps(event) + '\n\n').encode()

        async def aclose(self):
            closed.add(self.kind)

    async def transport(request):
        if request.url.path.endswith('/stream'):
            kind = request.url.path.split('/')[-2]
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream(kind))
        return await heart(request)

    class ObservedAdapter(AnytypeAdapter):
        async def on_processing_complete(self, event, outcome):
            await super().on_processing_complete(event, outcome)
            complete.set()

    adapter = ObservedAdapter(PlatformConfig(enabled=True, extra={
        'discovery': {'space_id': SPACE}, 'group_allow_from': ['alice-key'], 'activity_status': False,
    }))
    adapter.client = Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(transport))

    async def answer(event):
        handled.append(event)
        return 'Here are the findings.'

    adapter.set_message_handler(answer)
    try:
        assert await adapter.connect()
        await asyncio.wait_for(adapter.discovery.ready.wait(), 3)
        incoming = msg(text='Explain the findings', state=adapter.discovery.bootstrap_boundary[:-1] + '1')
        heart.histories['discussion'] = [incoming]
        queues['chats'].put_nowait(frame('message_added', chat_id='discussion', kind='discussion',
                                        parent_id='parent', message=incoming))
        await asyncio.wait_for(complete.wait(), 5)
        assert len(handled) == 1 and 'Research' in handled[0].channel_context
        assert handled[0].source.thread_id == 'discussion'
        assert adapter.store.inputs(['handed_off'])[0]['message_id'] == 'm1'
        sends = [r for r in heart.calls if r.method == 'POST']
        assert len(sends) == 1 and sends[0].url.path.endswith('/chats/discussion/messages')
        assert json.loads(sends[0].content)['reply_to'] == 'm1'
    finally:
        await adapter.disconnect()
    assert closed == {'chats', 'search'}


async def _frames(stream):
    return [frame async for frame in stream]
