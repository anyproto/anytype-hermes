import asyncio
import json

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
