import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from anytype_plugin.adapter import AnytypeAdapter
from anytype_plugin.client import Client, Frame
from anytype_plugin.state import Store
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import ProcessingOutcome


@pytest.mark.parametrize('origin', [None, {'platform': 'anytype', 'chat_id': 'space.full/general'}])
def test_cron_home_delivery_uses_configured_chat(isolated_home, origin):
    from hermes_cli.plugins import discover_plugins
    from cron.scheduler_delivery import _resolve_delivery_targets, cron_delivery_targets

    root = Path(__file__).resolve().parents[1]
    plugins = isolated_home / 'plugins'
    plugins.mkdir()
    (plugins / 'anytype-platform').symlink_to(root, target_is_directory=True)
    platform = config().to_dict()
    platform['home_channel'] = {
        'platform': 'anytype', 'chat_id': 'space.full/home', 'name': 'Hermes Home',
    }
    platform['extra']['chats'].append({'space_id': 'space.full', 'chat_id': 'home'})
    (isolated_home / 'config.yaml').write_text(yaml.safe_dump({
        'plugins': {'enabled': ['anytype-platform']},
        'gateway': {'platforms': {'anytype': platform}},
    }))
    discover_plugins()
    targets = _resolve_delivery_targets({'deliver': 'anytype', 'origin': origin})
    assert [(t['platform'], t['chat_id'], t['thread_id']) for t in targets] == [
        ('anytype', 'space.full/home', None),
    ]
    assert any(t['id'] == 'anytype' and t['home_target_set'] for t in cron_delivery_targets())


def config(allow=None):
    return PlatformConfig(enabled=True, extra={
        'chats': [{'space_id': 'space.full', 'chat_id': 'chat'}],
        'group_allow_from': allow if allow is not None else ['alice-key'],
        'group_allow_admin_from': ['alice-key'], 'group_user_allowed_commands': ['new', 'stop'],
    })


def message(id='in1', author='alice', text='hello'):
    return {'id': id, 'author_id': author, 'text': text, 'at': '2026-09-24T12:00:00Z', 'order': 'order-' + id}


async def adapter_ready(tmp_path, allow=None):
    adapter = AnytypeAdapter(config(allow))
    def handler(request):
        return httpx.Response(200, json={'data': [
            {'id': 'alice', 'identity': 'alice-key'}, {'id': 'bob', 'identity': 'bob-key'},
            {'id': 'bot', 'identity': 'bot-key'}], 'has_more': False})
    adapter.client = Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(handler))
    adapter._me['space.full'] = {'id': 'bot', 'identity': 'bot-key'}
    await adapter._refresh_members('space.full')
    adapter.store = Store(tmp_path / 'state.sqlite3', 'node|bot')
    adapter.store.baseline('space.full/chat', [], None)
    return adapter


async def test_admission_canonicalizes_identity_and_suppresses_self(tmp_path):
    adapter = await adapter_ready(tmp_path)
    target = adapter.settings.targets[0]
    for id, author in [('a', 'alice'), ('b', 'bob'), ('c', 'bot')]:
        await adapter._record(target, message(id, author), 'state-' + id)
    assert [r['message_id'] for r in adapter.store.inputs(['pending'])] == ['a']
    assert adapter.config.extra['group_allow_from'] == ['alice']
    assert adapter.config.extra['group_allow_admin_from'] == ['alice']
    assert adapter.store.chat(target.key)['cursor'] == 'state-c'
    await adapter.disconnect()


async def test_recreated_adapter_keeps_unresolved_account_configuration(tmp_path):
    adapter = await adapter_ready(tmp_path, ['not-joined-yet'])
    assert adapter.config.extra['group_allow_from'] == []
    recreated = AnytypeAdapter(adapter.config)
    assert recreated.settings.allow == {'not-joined-yet'}
    # An explicit configuration edit takes precedence over the cached resolution.
    adapter.config.extra['group_allow_from'] = ['*']
    edited = AnytypeAdapter(adapter.config)
    assert edited.settings.allow == {'*'}
    await adapter.disconnect()


@pytest.mark.parametrize('allow,expected', [(['*'], True), (['alice-key'], False), ([], False)])
async def test_real_gateway_gate_and_local_policy(tmp_path, monkeypatch, allow, expected):
    from gateway.run import GatewayRunner
    adapter = await adapter_ready(tmp_path, allow)
    event = adapter._event(adapter.settings.targets[0], message(author='bob'))
    runner = object.__new__(GatewayRunner)
    runner.pairing_store = None
    runner.adapters = {Platform('anytype'): adapter}
    runner._profile_adapters = {}
    runner._primary_profile_name = 'default'
    assert runner._is_user_authorized(event.source) == expected
    monkeypatch.setenv('GATEWAY_ALLOW_ALL_USERS', 'true')
    await adapter._record(adapter.settings.targets[0], message(author='bob'))
    assert bool(adapter.store.inputs(['pending'])) == expected
    await adapter.disconnect()


async def test_native_session_keys_isolate_users_and_chats(tmp_path):
    adapter = await adapter_ready(tmp_path)
    target = adapter.settings.targets[0]
    a = adapter._event(target, message(author='alice'))
    b = adapter._event(target, message(author='bob'))
    assert adapter._event_session_key(a) != adapter._event_session_key(b)
    from anytype_plugin.config import Target
    c = adapter._event(Target('other.full', 'chat'), message(author='alice'))
    assert adapter._event_session_key(a) != adapter._event_session_key(c)
    await adapter.disconnect()


async def test_sse_only_additions_trigger_and_cursor_is_durable(tmp_path):
    adapter = await adapter_ready(tmp_path)
    target = adapter.settings.targets[0]
    data = {'id': 'state1', 'type': 'message_added', 'message': message()}
    await adapter._frame(target, Frame('message_added', json.dumps(data), 'state1'))
    await adapter._frame(target, Frame('message_added', json.dumps(data), 'state1'))
    update = {'type': 'message_updated', 'message': message(text='edited')}
    await adapter._frame(target, Frame('message_updated', json.dumps(update)))
    await adapter._frame(target, Frame('message', '', 'state2'))
    assert len(adapter.store.inputs(['pending'])) == 1
    assert adapter.store.chat(target.key)['cursor'] == 'state2'
    assert adapter.store.message(target.key, 'in1')['text'] == 'edited'
    await adapter.disconnect()


async def test_merged_inputs_receive_terminal_outcomes(tmp_path):
    adapter = await adapter_ready(tmp_path)
    target = adapter.settings.targets[0]
    events = []
    for id in ('a', 'b'):
        m = message(id)
        await adapter._record(target, m)
        events.append(adapter._event(target, m))
    queue = {}
    for event in events:
        merge_pending_message_event(queue, 'session', event, merge_text=True)
    merged = queue['session']
    assert merged.ingress_message_ids == ['a', 'b']
    await adapter.on_processing_start(merged)
    await adapter.on_processing_complete(merged, ProcessingOutcome.SUCCESS)
    assert len(adapter.store.inputs(['completed'])) == 2
    await adapter.disconnect()


def test_real_plugin_discovery_and_yaml_loading(isolated_home):
    from hermes_cli.plugins import PluginManager
    from gateway.config import load_gateway_config
    from gateway.platform_registry import platform_registry
    root = Path(__file__).resolve().parents[1]
    plugins = isolated_home / 'plugins'
    plugins.mkdir()
    (plugins / 'anytype-platform').symlink_to(root, target_is_directory=True)
    (isolated_home / 'config.yaml').write_text(yaml.safe_dump({
        'plugins': {'enabled': ['anytype-platform']},
        'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': config().extra}}},
    }))
    manager = PluginManager()
    manager.discover_and_load()
    assert manager._plugins['anytype-platform'].enabled, manager._plugins['anytype-platform'].error
    entry = platform_registry.get('anytype')
    assert entry and entry.standalone_sender_fn
    loaded = load_gateway_config()
    assert loaded.platforms[Platform('anytype')].extra['chats'] == config().extra['chats']


def test_profile_secrets_and_home_A_B_A(tmp_path):
    from agent.secret_scope import reset_secret_scope, set_multiplex_active, set_secret_scope
    from gateway.platforms._shared import get_scoped_secret
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    set_multiplex_active(True)
    try:
        for name, secret in [('a', 'key-a'), ('b', 'key-b'), ('a', 'key-a')]:
            home = tmp_path / name
            home_token = set_hermes_home_override(home)
            secret_token = set_secret_scope({'ANYTYPE_API_KEY': secret}, profile_home=str(home))
            try:
                adapter = AnytypeAdapter(config())
                assert adapter.home == home
                assert get_scoped_secret('ANYTYPE_API_KEY', '') == secret
            finally:
                reset_secret_scope(secret_token)
                reset_hermes_home_override(home_token)
        token = set_secret_scope({}, profile_home=str(tmp_path / 'empty'))
        try:
            assert get_scoped_secret('ANYTYPE_API_KEY', '') == ''
        finally:
            reset_secret_scope(token)
    finally:
        set_multiplex_active(False)


async def test_full_gateway_loop_and_new_adapter_recovers_downtime(isolated_home):
    from anytype_plugin.formatting import object_link
    requests, handled = [], []
    incoming = message()
    history = []
    completed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            value = {'id': 's1', 'type': 'message_added', 'message': incoming}
            yield ('id: s1\nevent: message_added\ndata: ' + json.dumps(value) + '\n\nid: s1\n\n').encode()
            await asyncio.Event().wait()

    def transport(request):
        path = request.url.path
        if path.endswith('/members/me'):
            return httpx.Response(200, json={'id': 'bot', 'identity': 'bot-key'})
        if path.endswith('/members'):
            return httpx.Response(200, json={'data': [{'id': 'bot', 'identity': 'bot-key'},
                                                    {'id': 'alice', 'identity': 'alice-key'}]})
        if path.endswith('/chats'):
            return httpx.Response(200, json={'data': [{'id': 'chat', 'name': 'Test chat'}]})
        if path.endswith('/stream'):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, stream=Stream())
        if request.method == 'POST':
            requests.append(json.loads(request.content))
            return httpx.Response(201, json={'id': f'remote{len(requests)}'})
        if path.endswith('/messages'):
            return httpx.Response(200, json={'messages': history, 'state': {}, 'has_more': False})
        return httpx.Response(200, json={'id': 'report'})

    class ObservedAdapter(AnytypeAdapter):
        async def on_processing_complete(self, event, outcome):
            await super().on_processing_complete(event, outcome)
            completed.set()

    async def answer(event):
        handled.append(event.message_id)
        return object_link('space.full', 'report', 'Result')

    adapter = ObservedAdapter(config())
    adapter.client = Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(transport))
    adapter.set_message_handler(answer)
    assert await adapter.connect()
    await asyncio.wait_for(completed.wait(), 10)
    assert adapter.store.inputs(['completed'])[0]['message_id'] == 'in1'
    assert requests[0]['reply_to'] == 'in1' and requests[0]['attachments'] == ['report']
    assert set(requests[0]) == {'text', 'reply_to', 'attachments'}
    await adapter.disconnect()

    # A fresh instance must restore its cursor, and the history catch-up must admit outage input.
    history[:] = [incoming, message('outage')]
    completed.clear()
    adapter = ObservedAdapter(config())
    adapter.client = Client(adapter.settings.base_url, 'test', transport=httpx.MockTransport(transport))
    adapter.set_message_handler(answer)
    assert await adapter.connect(is_reconnect=True)
    await asyncio.wait_for(completed.wait(), 10)
    assert handled == ['in1', 'outage']
    assert requests[-1]['reply_to'] == 'outage'
    assert len(adapter.store.inputs(['completed'])) == 2
    await adapter.disconnect()


async def test_inline_control_has_receipt_outcome(tmp_path):
    adapter = await adapter_ready(tmp_path)
    target = adapter.settings.targets[0]
    incoming = message('control', text='/status')
    await adapter._record(target, incoming)
    event = adapter._event(target, incoming)
    adapter.set_message_handler(AsyncMock(return_value=None))
    await adapter._dispatch_inline_reply(event)
    assert adapter.store.inputs(['completed'])[0]['message_id'] == 'control'
    await adapter.disconnect()


async def test_resync_history_commits_receipts_before_new_cursor(tmp_path):
    adapter = await adapter_ready(tmp_path)
    target = adapter.settings.targets[0]
    adapter.client.history = AsyncMock(return_value={
        'messages': [message('missed')], 'state': {'last_state_id': 'recovered-state'}, 'has_more': False})
    await adapter.reconcile(target, deep=True)
    assert adapter.store.chat(target.key)['cursor'] == 'recovered-state'
    assert adapter.store.inputs(['pending'])[0]['message_id'] == 'missed'
    from anytype_plugin.client import ResyncRequired
    with pytest.raises(ResyncRequired):
        await adapter._frame(target, Frame('resync_required', '{"type":"resync_required"}'))
    assert adapter.store.chat(target.key)['cursor'] == 'recovered-state'
    await adapter.disconnect()


async def test_prefix_admin_and_attachment_only_behavior(tmp_path):
    from dataclasses import replace
    from gateway.slash_access import policy_from_extra
    adapter = await adapter_ready(tmp_path, ['*'])
    adapter.settings = replace(adapter.settings, trigger='prefix')
    target = adapter.settings.targets[0]
    unrelated = adapter._event(target, message(text='ordinary conversation'))
    assert not adapter._trigger(unrelated)
    prefixed = adapter._event(target, message(text='!hermes research this'))
    assert adapter._trigger(prefixed) and prefixed.text == 'research this'
    wrong_prefix = adapter._event(target, message(text='!hermesian no'))
    assert not adapter._trigger(wrong_prefix)
    command = adapter._event(target, message(text='/stop'))
    assert adapter._trigger(command)
    policy = policy_from_extra(adapter.config.extra, 'group')
    assert policy.can_run('alice', 'model')
    assert not policy.can_run('bob', 'model') and policy.can_run('bob', 'stop')
    attached = message(text='')
    attached['attachments'] = [{'id': 'report', 'type': 'LINK'}]
    event = adapter._event(target, attached)
    assert 'report' in event.channel_context and event.text
    await adapter.disconnect()


def test_operator_status_contains_counts_without_message_bodies(tmp_path):
    from anytype_plugin.cli import local_status
    path = tmp_path / 'state.sqlite3'
    assert local_status(path)['status'] == 'not_initialized'
    state = Store(path, 'node|bot')
    state.baseline('space/chat', [], None)
    state.record('space/chat', message(text='private message content'), 'pending', 'cursor')
    report = local_status(path)
    assert report['inputs'] == {'pending': 1}
    assert 'private message content' not in json.dumps(report)
    state.close()
