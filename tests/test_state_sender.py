import json

import httpx
import pytest

from anytype_plugin.client import Client
from anytype_plugin.formatting import object_link, plain
from anytype_plugin.sender import Sender
from anytype_plugin.state import Store


def message(id='m1'):
    return {'id': id, 'author_id': 'alice', 'text': 'hello'}


def test_cursor_and_pending_receipt_survive_restart(tmp_path):
    path = tmp_path / 'state.sqlite3'
    state = Store(path, 'node|bot')
    state.baseline('space/chat', [], None)
    state.record('space/chat', message(), 'pending', 'cursor1')
    state.close()
    state = Store(path, 'node|bot')
    assert state.chat('space/chat')['cursor'] == 'cursor1'
    assert state.inputs(['pending'])[0]['message_id'] == 'm1'
    assert not state.record('space/chat', message(), 'pending', 'cursor2')
    assert state.chat('space/chat')['cursor'] == 'cursor2'
    state.mark('space/chat', 'm1', 'processing')
    state.recover_inputs()
    assert not state.inputs(['pending']) and state.inputs(['uncertain'])
    state.close()
    other = Store(path, 'node|other-bot')
    assert other.chat('space/chat') is None
    other.close()


def test_active_backlog_limit_does_not_advance_cursor(tmp_path):
    state = Store(tmp_path / 'state.sqlite3', 'node|bot')
    state.baseline('space/chat', [], None)
    for i in range(1000):
        state.record('space/chat', message(str(i)), 'pending', str(i))
        state.mark('space/chat', str(i), 'handing_off')
    with pytest.raises(RuntimeError):
        state.record('space/chat', message('overflow'), 'pending', 'must-not-commit')
    assert state.chat('space/chat')['cursor'] == '999'
    assert not state.known('space/chat', 'overflow')
    state.close()


async def test_sender_preserves_links_attachments_and_deduplicates_operation(tmp_path, settings):
    requests = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'id': 'report'})
        requests.append(request)
        return httpx.Response(201, json={'id': 'remote1'})
    client = Client(settings.base_url, 'test', transport=httpx.MockTransport(handler))
    store = Store(tmp_path / 'state.sqlite3', 'node|bot')
    sender = Sender(client, store, settings)
    content = object_link('space.full', 'report', 'Report')
    first = await sender.send(settings.targets[0], content, 'incoming')
    again = await sender.send(settings.targets[0], content, 'incoming')
    assert first.success and again.message_id == first.message_id
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {'text': content, 'reply_to': 'incoming', 'attachments': ['report']}
    await client.close()
    store.close()


async def test_ambiguous_send_is_not_blindly_replayed_after_restart(tmp_path, settings):
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('response lost')
    client = Client(settings.base_url, 'test', transport=httpx.MockTransport(handler))
    path = tmp_path / 'state.sqlite3'
    store = Store(path, 'node|bot')
    result = await Sender(client, store, settings).send(settings.targets[0], 'hello', metadata={'delivery_id': 'op1'})
    assert not result.success and 'uncertain' in result.error
    key = store.delivery('op1')[0]['request_key']
    store.close()
    store = Store(path, 'node|bot')
    store.recover_deliveries()
    result = await Sender(client, store, settings).send(settings.targets[0], 'hello', metadata={'delivery_id': 'op1'})
    assert not result.success and len(calls) == 1
    assert store.delivery('op1')[0]['request_key'] == key
    await client.close()
    store.close()


async def test_partial_chunks_retry_only_unsent_with_same_key(tmp_path, settings):
    calls = []
    def handler(request):
        calls.append((request.headers['Idempotency-Key'], request.content))
        if len(calls) == 2:
            return httpx.Response(429, headers={'Retry-After': '1'})
        return httpx.Response(201, json={'id': f'remote{len(calls)}'})
    client = Client(settings.base_url, 'test', transport=httpx.MockTransport(handler))
    store = Store(tmp_path / 'state.sqlite3', 'node|bot')
    sender = Sender(client, store, settings)
    content = '🙂' * 4100
    first = await sender.send(settings.targets[0], content, 'in', {'delivery_id': 'op'})
    assert not first.success and first.error.startswith('flood_control:')
    again = await sender.send(settings.targets[0], content, 'in', {'delivery_id': 'op'})
    assert again.success and calls[1] == calls[2] and calls[0][0] != calls[1][0]
    assert len(calls) == 3
    await client.close()
    store.close()


async def test_rejected_markup_is_resent_as_plain_text_with_same_key(tmp_path, settings):
    calls = []
    def handler(request):
        calls.append((request.headers['Idempotency-Key'], json.loads(request.content)['text']))
        if len(calls) == 1:
            return httpx.Response(400, json={'code': 'validation_failed', 'issues': [{'path': '/text'}]})
        return httpx.Response(201, json={'id': 'remote1'})
    client = Client(settings.base_url, 'test', transport=httpx.MockTransport(handler))
    store = Store(tmp_path / 'state.sqlite3', 'node|bot')
    content = 'see <mention x> **now**'
    result = await Sender(client, store, settings).send(settings.targets[0], content, metadata={'delivery_id': 'op'})
    assert result.success and result.message_id == 'remote1'
    key = calls[0][0]
    assert calls == [(key, content), (key, plain(content))]
    assert json.loads(store.delivery('op')[0]['payload'])['text'] == plain(content)
    await client.close()
    store.close()


async def test_rejected_plain_text_fails_without_resend(tmp_path, settings):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={'code': 'validation_failed'})
    client = Client(settings.base_url, 'test', transport=httpx.MockTransport(handler))
    store = Store(tmp_path / 'state.sqlite3', 'node|bot')
    result = await Sender(client, store, settings).send(settings.targets[0], 'hello', metadata={'delivery_id': 'op'})
    assert not result.success and 'failed' in result.error
    assert len(calls) == 1
    await client.close()
    store.close()


@pytest.mark.parametrize('before,after', [
    ('pending', 'pending'), ('handing_off', 'uncertain'), ('handed_off', 'handed_off'),
    ('dispatched', 'uncertain'), ('processing', 'uncertain'),
    ('completed', 'completed'), ('cancelled', 'cancelled'), ('uncertain', 'uncertain'),
])
def test_receipt_migration_preserves_history_without_replaying_handoffs(tmp_path, before, after):
    path = tmp_path / 'state.sqlite3'
    store = Store(path, 'node|bot')
    store.baseline('space/chat', [], 'cursor')
    store.record('space/chat', message(), before)
    store.close()
    store = Store(path, 'node|bot')
    store.recover_inputs()
    assert store.receipt('space/chat', 'm1')['status'] == after
    assert not store.record('space/chat', message(), 'pending')
    assert store.chat('space/chat')['cursor'] == 'cursor'
    assert store.pending_count() == (1 if after == 'pending' else 0)
    store.close()
