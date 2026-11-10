import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from anytype_plugin import activity
from anytype_plugin.adapter import AnytypeAdapter
from anytype_plugin.activity import ActivityPublisher
from anytype_plugin.client import Client
from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


def make_adapter(sent):
    def http(request):
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    adapter = AnytypeAdapter(PlatformConfig(enabled=True, extra={'chats': [
        {'space_id': 'one', 'chat_id': 'chat'},
        {'space_id': 'two', 'chat_id': 'chat'},
        {'space_id': 'one', 'chat_id': 'discussion'},
    ]}))
    client = Client('http://localhost:31009', 'test-key', transport=httpx.MockTransport(http))
    adapter.activity = ActivityPublisher(client, secrets=('native-secret',), interval=100)
    return adapter


def turn_runner(adapter, chat_id):
    ctx = TurnContext(source=SimpleNamespace(chat_id=chat_id),
                      _live_status_adapter=adapter if adapter.supports_status_text else None,
                      _live_status_mode='full', _run_still_current=lambda: True,
                      progress_queue=None, log_queue=None)
    return TurnRunner(None, ctx)


async def close(adapter):
    await adapter.activity.close()
    await adapter.activity.client.close()


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), 5)


async def test_native_progress_without_message_queue_and_elapsed_are_scoped(monkeypatch):
    sent, now = [], [0.0]
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    adapter = make_adapter(sent)
    runner = turn_runner(adapter, 'one/chat')
    try:
        await adapter.send_typing('one/chat')
        now[0] = 20
        await adapter.send_typing('two/chat')
        await asyncio.to_thread(runner.progress_callback, 'tool.started', 'process_manage',
                                args={'action': 'poll', 'session_id': 'test'})
        await adapter.send_typing('one/chat')
        assert sent[-1][0] == '/v2/spaces/one/chats/chat/status'
        assert sent[-1][1]['text'] != 'Thinking about the next step'
        await asyncio.to_thread(runner.progress_callback, 'tool.completed', 'process_manage')
        now[0] = 65
        await adapter.send_typing('one/chat')
        assert sent[-1][1] == {'text': 'Working — 1m05s'}
        await adapter.send_typing('two/chat')
        assert sent[-1] == ('/v2/spaces/two/chats/chat/status', {'text': 'Working — 0m45s'})
        await asyncio.to_thread(adapter.set_status_text, 'one/discussion', 'Searching native-secret')
        await adapter.send_typing('one/discussion', metadata={'thread_id': 'discussion'})
        assert sent[-1] == ('/v2/spaces/one/chats/discussion/status', {'text': 'Searching [redacted]'})
        await adapter.stop_typing('one/chat')
        now[0] = 200
        await adapter.send_typing('one/chat')
        assert sent[-1][1] == {'text': 'Thinking about the next step'}
        assert all(path.endswith('/status') and set(body) == {'text'} for path, body in sent)
    finally:
        await close(adapter)


async def test_structured_tool_activity_keeps_priority_and_explicit_completion(monkeypatch):
    sent, now = [], [0.0]
    monkeypatch.setattr(activity, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    adapter = make_adapter(sent)
    runner = turn_runner(adapter, 'one/chat')
    run = adapter.activity.run(adapter._targets['one/chat'])
    try:
        await adapter.send_typing('one/chat')
        runner.progress_callback('tool.started', 'process_manage', args={'action': 'wait'})
        run.start('call', 'process_manage', {'action': 'wait'})
        await eventually(lambda: any(body.get('data') for _, body in sent))
        count = len(sent)
        now[0] = 70
        await adapter.send_typing('one/chat')
        assert len(sent) == count
        runner.progress_callback('tool.completed', 'process_manage')
        run.complete('call', 'process_manage', {}, {'exit_code': 0})
        await eventually(lambda: any(body.get('data', {}).get('status') == 'complete' for _, body in sent))
        await adapter.send_typing('one/chat')
        assert sent[-1][1] == {'text': 'Working — 1m10s'}
        calls = [body['data'] for _, body in sent if 'data' in body]
        assert [row['status'] for row in calls] == ['in_progress', 'complete']
        assert calls[0]['tool_call_id'] == calls[1]['tool_call_id']
        assert calls[1]['result'] == {'exit_code': 0}
        assert all(path.endswith('/status') for path, _ in sent)
    finally:
        run.close()
        await close(adapter)


async def test_native_typing_loop_cleans_up_and_does_not_intercept_real_messages():
    sent = []
    adapter = make_adapter(sent)
    stop = asyncio.Event()
    try:
        adapter.set_status_text('one/chat', 'Preparing an answer')
        refresh = asyncio.create_task(adapter._keep_typing('one/chat', interval=.02, stop_event=stop))
        try:
            await eventually(lambda: len(sent) >= 2)
        finally:
            stop.set()
            await asyncio.wait_for(refresh, 5)
        assert all(body == {'text': 'Preparing an answer'} for _, body in sent)
        assert 'one/chat' not in adapter._status_text
        assert 'one/chat' not in adapter.activity.typing_started
        adapter.sender = SimpleNamespace(send=AsyncMock(return_value=SendResult(True, message_id='reply')))
        result = await adapter.send('one/chat', 'Here is the answer.')
        assert result.message_id == 'reply'
        adapter.sender.send.assert_awaited_once_with(adapter._targets['one/chat'], 'Here is the answer.', None, None)
    finally:
        await close(adapter)
