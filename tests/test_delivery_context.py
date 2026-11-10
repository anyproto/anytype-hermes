"""Exercise the stock Hermes delivery ledger and retry loop with the real Anytype sender."""
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from anytype_plugin.adapter import AnytypeAdapter
from anytype_plugin.client import Client
from anytype_plugin.sender import Sender
from anytype_plugin.state import Store
from gateway import delivery_ledger as ledger
from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.run_startup import GatewayStartupMixin


def adapter_ready(path, transport):
    adapter = AnytypeAdapter(PlatformConfig(enabled=True, extra={
        'chats': [{'space_id': 'space', 'chat_id': 'discussion'}], 'activity_status': False}))
    adapter._chat_meta['space/discussion'] = {'kind': 'discussion'}
    adapter.client = Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(transport))
    adapter.store = Store(path, 'node|bot')
    adapter.store.recover_deliveries()
    adapter.sender = Sender(adapter.client, adapter.store, adapter.settings)
    return adapter


async def redeliver(adapter, rows):
    runner = object.__new__(GatewayStartupMixin)
    runner._obligation_adapter = AsyncMock(return_value=adapter)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    return await runner._redeliver_claimed_obligations(rows)


async def test_stock_boot_recovery_is_marked_and_keeps_chat_without_exact_context(tmp_path, monkeypatch):
    assert not hasattr(BasePlatformAdapter, 'get_delivery_context')
    calls = []

    def transport(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'id': 'report'})
        calls.append((request.url.path, json.loads(request.content)))
        if len(calls) == 1:
            raise httpx.ReadTimeout('response lost after acceptance')
        return httpx.Response(201, json={'id': 'recovered'})

    path = tmp_path / 'state.sqlite3'
    adapter = adapter_ready(path, transport)
    event = adapter._event(adapter.settings.targets[0], {'id': 'incoming', 'author_id': 'alice', 'text': 'research'})
    result, _ = await adapter.send_final_ledgered(
        event, adapter._event_session_key(event), 'Final answer',
        {'attachments': ['report'], 'thread_id': 'discussion'}, reply_to='incoming')
    assert not result.success and len(calls) == 1  # no immediate plain-text fallback
    assert calls[0][1] == {'text': 'Final answer', 'reply_to': 'incoming', 'attachments': ['report']}
    await adapter.disconnect()

    monkeypatch.setattr(ledger, '_owner_alive', lambda *args: False)
    rows = ledger.sweep_recoverable(now=time.time() + 300)
    assert len(rows) == 1 and rows[0]['needs_marker']
    adapter = adapter_ready(path, transport)
    try:
        assert await redeliver(adapter, rows) == 1
        assert calls[1] == ('/v2/spaces/space/chats/discussion/messages',
                            {'text': ledger.RECOVERED_MARKER + 'Final answer'})
        # Unknown original stays visible; the explicit stock recovery is a new operation.
        assert adapter.store.summary()['deliveries'] == {'delivered': 1, 'uncertain': 1}
        assert not ledger.sweep_recoverable(now=time.time() + 600)
    finally:
        await adapter.disconnect()


async def test_stock_runtime_recovery_obeys_flood_delay_and_uses_marker(tmp_path):
    calls = []

    def transport(request):
        calls.append(json.loads(request.content))
        return httpx.Response(201, json={'id': 'remote'})

    ledger.record_obligation(obligation_id='op', session_key='session', platform='anytype',
                             chat_id='space/discussion', thread_id='discussion', content='hello')
    ledger.mark_failed('op', 'flood_control:60')
    assert not ledger.sweep_failed_for_runtime('anytype')
    rows = ledger.sweep_failed_for_runtime('anytype', now=time.time() + 120)
    adapter = adapter_ready(tmp_path / 'state.sqlite3', transport)
    try:
        assert await redeliver(adapter, rows) == 1
        assert calls == [{'text': ledger.FLOOD_MARKER + 'hello'}]
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize('reply_to,expected_calls,success', [('incoming', 3, True), (None, 2, False)])
async def test_stock_inline_retry_does_not_repeat_delivered_chunks(tmp_path, monkeypatch,
                                                                 reply_to, expected_calls, success):
    calls = []

    def transport(request):
        calls.append((request.headers['Idempotency-Key'], json.loads(request.content)))
        if len(calls) == 2:
            raise httpx.ConnectError('refused before delivery')
        return httpx.Response(201, json={'id': str(len(calls))})

    monkeypatch.setattr('gateway.platforms.base.random.uniform', lambda *args: 0)
    adapter = adapter_ready(tmp_path / 'state.sqlite3', transport)
    try:
        result = await adapter._send_with_retry('space/discussion', 'a' * 8100,
                                                reply_to=reply_to, base_delay=0)
        assert result.success is success and len(calls) == expected_calls
        if reply_to:
            assert calls[1] == calls[2] and calls[0][0] != calls[1][0]
        else:
            assert result.raw_response['partial_overflow'] is True
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize('reply_to', ['incoming', None])
async def test_stock_inline_send_never_falls_back_after_ambiguous_post(tmp_path, reply_to):
    calls = []

    def transport(request):
        calls.append(request)
        raise httpx.ReadTimeout('response lost')

    adapter = adapter_ready(tmp_path / 'state.sqlite3', transport)
    try:
        result = await adapter._send_with_retry('space/discussion', '**reply**', reply_to=reply_to)
        assert not result.success and len(calls) == 1
        assert adapter.store.summary()['deliveries'] == {'uncertain': 1}
    finally:
        await adapter.disconnect()
