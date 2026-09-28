import time
from unittest.mock import AsyncMock

from gateway import delivery_ledger as ledger
from gateway.platforms.base import SendResult
from gateway.run_startup import GatewayStartupMixin


async def test_boot_redelivery_preserves_exact_content_reply_and_metadata(monkeypatch):
    context = {'reply_to': 'incoming', 'metadata': {'attachments': ['report']}}
    ledger.record_obligation(obligation_id='op', session_key='session', platform='anytype',
                             chat_id='space/chat', thread_id=None, content='Final answer',
                             delivery_context=context)
    ledger.mark_attempting('op')
    monkeypatch.setattr(ledger, '_owner_alive', lambda *args: False)
    rows = ledger.sweep_recoverable()
    assert rows[0]['delivery_context'] == context
    sender = AsyncMock()
    sender.send.return_value = SendResult(True, message_id='remote')
    runner = object.__new__(GatewayStartupMixin)
    runner._obligation_adapter = AsyncMock(return_value=sender)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    assert await runner._redeliver_claimed_obligations(rows) == 1
    sender.send.assert_awaited_once_with(
        chat_id='space/chat', content='Final answer', reply_to='incoming',
        metadata={'attachments': ['report'], 'delivery_id': 'op', 'delivery_recovery': True})


def test_legacy_obligation_still_uses_recovery_marker(monkeypatch):
    ledger.record_obligation(obligation_id='old', session_key='session', platform='other',
                             chat_id='chat', thread_id=None, content='hello')
    ledger.mark_attempting('old')
    monkeypatch.setattr(ledger, '_owner_alive', lambda *args: False)
    row = ledger.sweep_recoverable()[0]
    assert row['needs_marker'] and 'delivery_context' not in row


def test_runtime_retry_retains_context():
    context = {'reply_to': 'incoming', 'metadata': {'attachments': ['report']}}
    ledger.record_obligation(obligation_id='op', session_key='session', platform='anytype',
                             chat_id='space/chat', thread_id=None, content='hello', delivery_context=context)
    ledger.mark_failed('op', 'flood_control:1')
    rows = ledger.sweep_failed_for_runtime('anytype', now=time.time() + 120)
    assert rows[0]['delivery_context'] == context
