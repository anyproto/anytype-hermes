import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from anytype_plugin.activity import ActivityPublisher, bounded_payload, clean, encode_payload
from anytype_plugin.client import Client
from anytype_plugin.config import Target


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), 5)


def publisher(handler, **kwargs):
    client = Client('http://localhost:31009', 'test-key', transport=httpx.MockTransport(handler))
    return ActivityPublisher(client, interval=.03, terminal_retry_seconds=.1, **kwargs)


async def test_parallel_calls_refresh_and_settle_in_their_own_chats():
    sent = []
    def http(request):
        assert request.url.path.endswith('/status')
        assert 'idempotency-key' not in request.headers
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})
    p = publisher(http)
    a, b = p.run(Target('space', 'one')), p.run(Target('space', 'two'))
    await asyncio.to_thread(a.start, 'same-provider-id', 'web_search', {'query': 'one'})
    await asyncio.to_thread(b.start, 'same-provider-id', 'web_search', {'query': 'two'})
    await eventually(lambda: len(sent) >= 4)
    ids = {body['data']['tool_call_id'] for _, body in sent}
    assert len(ids) == 2
    for path, body in sent:
        assert body['text'] and body['data']['status'] == 'in_progress'
        assert body['data']['query'] in path
    a.complete('same-provider-id', 'web_search', {'query': 'one'}, {'matches': 3})
    b.complete('same-provider-id', 'web_search', {'query': 'two'}, {'isError': True, 'error': 'failed'})
    await eventually(lambda: sum(body['data']['status'] != 'in_progress' for _, body in sent) == 2)
    a.close(); b.close()
    await p.close(); await p.client.close()
    for execution in ids:
        rows = [body for _, body in sent if body['data']['tool_call_id'] == execution]
        assert rows[-1]['data']['status'] in {'complete', 'error'}
        assert sum(r['data']['status'] != 'in_progress' for r in rows) == 1


@pytest.mark.parametrize('result', [None, False, 0, '', [], {'matches': 0}])
async def test_completion_during_inflight_refresh_keeps_terminal_order_and_falsy_result(result):
    sent, entered, release = [], asyncio.Event(), asyncio.Event()
    async def http(request):
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            entered.set()
            await release.wait()
        return httpx.Response(200, json={})
    p = publisher(http)
    r = p.run(Target('space', 'chat'))
    r.start('id', 'lookup', {})
    await asyncio.wait_for(entered.wait(), 5)
    r.complete('id', 'lookup', {}, result)
    release.set()
    await eventually(lambda: len(sent) == 2)
    r.complete('id', 'lookup', {}, {'error': 'late conflicting result'})
    r.close(); await p.close(); await p.client.close()
    assert [x['data']['status'] for x in sent] == ['in_progress', 'complete']
    assert 'result' in sent[-1]['data'] and sent[-1]['data']['result'] == result
    assert sent[0]['data']['tool_call_id'] == sent[1]['data']['tool_call_id']


async def test_retry_gets_new_id_and_interruption_gets_explicit_error():
    sent = []
    def http(request):
        sent.append(json.loads(request.content)); return httpx.Response(200, json={})
    p = publisher(http)
    r = p.run(Target('space', 'chat'))
    r.start('id', 'lookup', {})
    r.complete('id', 'lookup', {}, {'error': 'first failed'})
    await eventually(lambda: any(x['data']['status'] == 'error' for x in sent))
    r.start('id', 'lookup', {})
    await eventually(lambda: any(x['data']['status'] == 'in_progress' for x in sent))
    r.close()
    await eventually(lambda: sum(x['data']['status'] == 'error' for x in sent) == 2)
    r.complete('id', 'lookup', {}, 'too late')
    await p.close(); await p.client.close()
    assert len({x['data']['tool_call_id'] for x in sent}) == 2
    assert 'interrupted' in sent[-1]['text']


async def test_transient_activity_and_unavailable_endpoint_do_not_touch_chat_messages():
    sent = []
    def http(request):
        sent.append(json.loads(request.content)); return httpx.Response(404)
    p = publisher(http)
    target = Target('space', 'chat')
    p.set_text(target, 'Planning the next step')
    await p.typing(target)
    await p.typing(target)
    p.stop_typing(target)
    await p.close(); await p.client.close()
    assert sent == [{'text': 'Planning the next step'}]


def test_payload_bounds_and_secret_redaction_preserve_identity_and_terminal_state():
    data = clean({'query': 'look up', 'Authorization': 'secret', 'api_key': 'secret',
                  'nested': {'password': 'secret'}, 'result': '<🙂>' * 100_000})
    data.update(tool_call_id='unique', tool_name='lookup', status='complete')
    body = bounded_payload('Completed lookup', data)
    assert len(encode_payload(body)) <= 65_508
    assert body['data']['tool_call_id'] == 'unique' and body['data']['status'] == 'complete'
    assert body['data']['result']['truncated'] is True
    assert data['api_key'] == data['Authorization'] == data['nested']['password'] == '[redacted]'


async def test_real_gateway_callback_wiring_composes_and_routes_without_text_progress():
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from anytype_plugin.adapter import AnytypeAdapter
    from gateway.config import PlatformConfig, Platform
    sent, native = [], Mock()
    p = publisher(lambda request: (sent.append(json.loads(request.content)) or httpx.Response(200, json={})))
    adapter = AnytypeAdapter(PlatformConfig(enabled=True, extra={'chats': [{'space_id': 'space', 'chat_id': 'chat'}]}))
    adapter.activity = p
    source = SimpleNamespace(chat_id='space/chat', platform=Platform('anytype'))
    previous_status = Mock()
    ctx = TurnContext(source=source, session_key='session', user_config={}, _status_adapter=adapter,
                      _run_still_current=lambda: True, _hooks_ref=SimpleNamespace(loaded_hooks=[]),
                      _status_callback_sync=previous_status, _voice_ack_guild=['voice'], voice_ack_callback=native)
    runner = TurnRunner(SimpleNamespace(_service_tier=None, _consume_pending_turn_sidecar_notes=lambda key: []), ctx)
    runner._make_bg_review_callbacks = lambda: (None, None)
    runner._attach_session_title_callback = lambda agent, ctx: None
    agent = SimpleNamespace()
    runner._wire_turn_agent_callbacks(agent, {}, None, None, None, False)
    await asyncio.to_thread(agent.tool_start_callback, 'id', 'lookup', {'query': 'test'})
    native.assert_called_once_with('id', 'lookup', {'query': 'test'})
    await eventually(lambda: sent)
    await asyncio.to_thread(agent.tool_complete_callback, 'id', 'lookup', {}, {'matches': 1})
    agent.status_callback('lifecycle', 'Preparing the answer')
    agent.status_callback('warn', 'A warning')
    previous_status.assert_called_once_with('warn', 'A warning')
    runner._close_activity()
    await eventually(lambda: any(x.get('data', {}).get('status') == 'complete' for x in sent))
    await p.close(); await p.client.close()


def test_turn_runner_closes_activity_on_exception():
    from gateway.run_turn_runner import TurnRunner
    runner = TurnRunner(None, SimpleNamespace())
    runner._close_activity = Mock()
    runner._run_sync_with_activity = Mock(side_effect=RuntimeError('interrupted'))
    with pytest.raises(RuntimeError, match='interrupted'):
        runner.run_sync()
    runner._close_activity.assert_called_once()
