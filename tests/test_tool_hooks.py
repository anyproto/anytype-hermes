import asyncio
import contextvars
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import httpx
import pytest

from anytype_plugin import tool_hooks
from anytype_plugin.activity import ActivityPublisher
from anytype_plugin.adapter import AnytypeAdapter
from anytype_plugin.client import Client
from gateway.config import PlatformConfig
from gateway.platforms.event import ProcessingOutcome
from gateway.session_context import set_session_vars, clear_session_vars


@pytest.fixture
def hooks(monkeypatch):
    from hermes_cli import plugins
    manager = plugins.PluginManager()
    ctx = plugins.PluginContext(plugins.PluginManifest(name='anytype-platform'), manager)
    tool_hooks.register(ctx)
    monkeypatch.setattr(plugins, '_delivery_manager', lambda: manager)
    return manager, ctx


def adapter(sent, space='space', chat='chat'):
    def http(request):
        assert request.url.path.endswith('/status')
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})
    a = AnytypeAdapter(PlatformConfig(enabled=True, extra={
        'chats': [{'space_id': space, 'chat_id': chat}]}))
    a.store = Mock()
    a.store.message.return_value = None
    a.client = Client('http://localhost:31009', 'test-key', transport=httpx.MockTransport(http))
    a.activity = ActivityPublisher(a.client, interval=.03, terminal_retry_seconds=.1)
    target = a.settings.targets[0]
    if chat == 'discussion':
        a._chat_meta[target.key] = {'kind': 'discussion'}
    event = a._event(target, {'id': 'input', 'author_id': 'alice', 'text': 'hello',
                              'at': '2026-10-09T12:00:00Z'})
    return a, event


@contextmanager
def session(event, **kwargs):
    tokens = set_session_vars(platform='anytype', chat_id=event.source.chat_id, **kwargs)
    try:
        yield
    finally:
        clear_session_vars(tokens)


def fields(**kwargs):
    return dict(session_id='session', turn_id='turn', api_request_id='request',
                tool_call_id='call', tool_name='lookup', args={'query': 'test'}, **kwargs)


def start(data):
    from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
    return _dispatch_pre_tool_call_hooks(**data)


def finish(data, result=None, status='success'):
    from model_tools import _emit_post_tool_call_hook
    data = dict(data)
    name, args = data.pop('tool_name'), data.pop('args')
    _emit_post_tool_call_hook(function_name=name, function_args=args, result=result, status=status, **data)


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.01)
    await asyncio.wait_for(wait(), 5)


def terminal(sent):
    return [body['data'] for _, body in sent if body.get('data', {}).get('status') in {'complete', 'error'}]


async def test_real_processing_and_executor_route_hooks_without_activity_patch(hooks):
    from gateway.run import GatewayRunner
    from gateway.platforms.base import BasePlatformAdapter
    assert not hasattr(BasePlatformAdapter, 'get_activity_callbacks')
    sent = []
    a, event = adapter(sent)
    runner = object.__new__(GatewayRunner)
    runner._get_executor = lambda: None
    a.send = AsyncMock(return_value=SimpleNamespace(success=True, message_id='reply'))
    async def answer(incoming):
        with session(incoming):
            assert await runner._run_in_executor_with_context(start, fields()) == (None, None)
            await eventually(lambda: any(b.get('data') for _, b in sent))
            await runner._run_in_executor_with_context(finish, fields(), {'matches': 3})
        return 'Ordinary reply'
    a.set_message_handler(answer)
    try:
        await a._process_message_background(event, a._event_session_key(event))
        await eventually(lambda: terminal(sent))
        assert terminal(sent)[-1]['result'] == {'matches': 3}
        assert any(b['text'] == 'Preparing lookup' for _, b in sent)
        assert len({b['data']['tool_call_id'] for _, b in sent if 'data' in b}) == 1
        assert a.send.call_count == 1
        assert a.send.call_args.kwargs['chat_id'] == 'space/chat'
        assert a.send.call_args.kwargs['content'] == 'Ordinary reply'
        assert not a._activity_bindings
    finally:
        await a.disconnect()


async def test_concurrent_spaces_discussions_and_profiles_keep_call_ids_separate(hooks, monkeypatch):
    from hermes_cli import plugins
    from gateway.session_context import get_session_env
    # Hermes owns a separate plugin manager per profile. Within one profile,
    # concurrent conversations have different durable session IDs.
    other = plugins.PluginManager()
    tool_hooks.register(plugins.PluginContext(plugins.PluginManifest(name='anytype-platform'), other))
    monkeypatch.setattr(plugins, '_delivery_manager', lambda: (
        other if get_session_env('HERMES_SESSION_PROFILE') == 'profile-1' else hooks[0]))
    outputs = [[], [], []]
    pairs = [adapter(outputs[0], 'one', 'discussion'), adapter(outputs[1], 'two', 'discussion'),
             adapter(outputs[2], 'one', 'discussion')]
    gate, arrived = asyncio.Event(), []
    async def run(index, a, event):
        await a.on_processing_start(event)
        try:
            with session(event, profile=f'profile-{index // 2}'):
                data = {**fields(), 'session_id': f'session-{index % 2}'}
                await asyncio.to_thread(start, data)
                arrived.append(index)
                if len(arrived) == 3:
                    gate.set()
                await gate.wait()
                await asyncio.to_thread(finish, data, index)
        finally:
            await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
    try:
        await asyncio.gather(*(run(i, *pair) for i, pair in enumerate(pairs)))
        await eventually(lambda: all(terminal(out) for out in outputs))
        assert [terminal(out)[-1]['result'] for out in outputs] == [0, 1, 2]
        assert len({terminal(out)[-1]['tool_call_id'] for out in outputs}) == 3
        for out, (a, _) in zip(outputs, pairs):
            assert all(f'/spaces/{a.settings.targets[0].space_id}/chats/discussion/status' == path[3:]
                       for path, body in out)
    finally:
        for a, _ in pairs:
            await a.disconnect()


@pytest.mark.parametrize('status,result', [('success', None), ('success', False), ('success', 0),
                                         ('success', ''), ('blocked', 'Denied'), ('cancelled', ''),
                                         ('timeout', None), ('error', 'Failed')])
async def test_terminal_hook_without_start_preserves_status_and_falsy_results(hooks, status, result):
    sent = []
    a, event = adapter(sent)
    await a.on_processing_start(event)
    try:
        with session(event):
            await asyncio.to_thread(finish, fields(), result, status)
            await asyncio.to_thread(finish, fields(), 'late conflicting result', 'error')
        await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await eventually(lambda: terminal(sent))
        assert len(terminal(sent)) == 1
        assert terminal(sent)[0]['result'] == result
        assert terminal(sent)[0]['status'] == ('complete' if status == 'success' else 'error')
    finally:
        await a.disconnect()


async def test_public_policy_hook_composes_and_activity_failure_cannot_block(hooks, monkeypatch):
    manager, ctx = hooks
    sent = []
    a, event = adapter(sent)
    await a.on_processing_start(event)
    try:
        with session(event):
            binding = tool_hooks._binding.get()
            monkeypatch.setattr(binding, 'enqueue', Mock(side_effect=RuntimeError('unavailable')))
            assert await asyncio.to_thread(start, fields()) == (None, None)
            ctx.register_hook('pre_tool_call', lambda **kw: {'action': 'block', 'message': 'Denied'})
            block, modified = await asyncio.to_thread(start, fields())
            assert 'Denied' in block and modified is None
        assert not sent
    finally:
        await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await a.disconnect()


async def test_reused_provider_id_new_request_and_late_callbacks_from_closed_turn(hooks):
    manager, _ = hooks
    sent = []
    a, event = adapter(sent)
    await a.on_processing_start(event)
    try:
        with session(event):
            first, second = fields(), {**fields(), 'api_request_id': 'request-2'}
            await asyncio.to_thread(start, first)
            await asyncio.to_thread(finish, first, 1)
            await asyncio.to_thread(start, second)
            # Closing a turn settles its missing completion explicitly.
            await asyncio.to_thread(manager.invoke_hook, 'post_llm_call', **fields())
            await eventually(lambda: len(terminal(sent)) == 2)
            old = contextvars.copy_context()
        await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await a.on_processing_start(event)
        with session(event):
            await asyncio.to_thread(start, fields())
            await asyncio.to_thread(old.run, finish, second, 'late')
            await asyncio.to_thread(old.run, start, {**fields(), 'turn_id': 'stale-turn'})
            await asyncio.to_thread(finish, fields(), 'new run')
        await a.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        await eventually(lambda: len(terminal(sent)) == 3)
        assert len({row['tool_call_id'] for row in terminal(sent)}) == 3
        assert [row['status'] for row in terminal(sent)] == ['complete', 'error', 'complete']
        assert terminal(sent)[-1]['result'] == 'new run'
    finally:
        await a.disconnect()


async def test_unbound_delegated_muted_cron_and_other_platform_calls_are_ignored(hooks):
    from agent.delegation_context import delegated_child_context
    from agent.notification_presentation import notification_turn
    sent = []
    a, event = adapter(sent)
    try:
        with session(event):
            await asyncio.to_thread(start, fields())  # no adapter binding
        await a.on_processing_start(event)
        with session(event):
            with delegated_child_context('child'):
                await asyncio.to_thread(start, fields())
            with notification_turn(SimpleNamespace(), muted=True, session_id='session'):
                await asyncio.to_thread(start, fields())
        with session(event, cron_session='1'):
            await asyncio.to_thread(start, fields())
        tokens = set_session_vars(platform='telegram', chat_id=event.source.chat_id)
        try:
            await asyncio.to_thread(start, fields())
        finally:
            clear_session_vars(tokens)
        await a.on_processing_complete(event, ProcessingOutcome.CANCELLED)
        await asyncio.sleep(.05)
        assert sent == []
    finally:
        await a.disconnect()


async def test_processing_cancellation_settles_running_calls(hooks):
    sent, started = [], asyncio.Event()
    a, event = adapter(sent)
    async def answer(event):
        with session(event):
            await asyncio.to_thread(start, fields())
            started.set()
            await asyncio.Event().wait()
    a.set_message_handler(answer)
    task = asyncio.create_task(a._process_message_background(event, a._event_session_key(event)))
    try:
        await started.wait()
        await eventually(lambda: sent)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await eventually(lambda: terminal(sent))
        assert terminal(sent)[-1]['status'] == 'error'
        assert 'interrupted' in terminal(sent)[-1]['result']['error']
    finally:
        await a.disconnect()


async def test_upstream_status_sink_keeps_routine_text_ephemeral_and_warnings_visible():
    from gateway.run import _send_or_update_status_coro
    sent = []
    a, event = adapter(sent)
    a.activity.secrets = ('private-value',)
    a.send = AsyncMock(return_value=SimpleNamespace(success=True))
    try:
        result = await _send_or_update_status_coro(a, event.source.chat_id, 'lifecycle',
                                                  'Preparing private-value', None)
        assert result.success
        await eventually(lambda: sent)
        assert sent[-1][1] == {'text': 'Preparing [redacted]'}
        a.send.assert_not_called()
        await _send_or_update_status_coro(a, event.source.chat_id, 'warn', 'A warning', None)
        a.send.assert_called_once_with(event.source.chat_id, 'A warning', metadata=None)
    finally:
        await a.disconnect()
