import argparse
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from anytype_plugin.config import Settings, resolve_api_key
from anytype_plugin.onboarding import Wizard, load_input, read_secret
from anytype_plugin.setup_backend import agent_name, NativeCLI
from anytype_plugin.setup_store import ProfileStore, SetupError, OperatorBoundary
from test_setup_diagnostics import FakeAPI, extra
from test_setup_publication import FakeLifecycle


def choices(**changes):
    return {'schema_version': 1, 'mode': 'desktop', 'scope': {'mode': 'all'}, 'access': 'operator',
            'operator_identities': ['alice-key'], 'home': {'action': 'skip'}, 'mcp': {'mode': 'none'},
            'activation': 'defer', 'api_key_ref': {'env': 'ANYTYPE_API_KEY'}, **changes}


async def test_desktop_setup_prepares_staged_secret_without_changing_live_config(isolated_home):
    config = {'model': {'default': 'preserve'}, 'gateway': {'sessions': {'group': 'thread'}}}
    path = isolated_home / 'config.yaml'
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    api = FakeAPI()
    result = await Wizard(isolated_home, interactive=False, api_transport=api.transport).run(choices())
    assert result['state'] == 'restart_required', result
    assert path.read_bytes() == before
    record = ProfileStore(isolated_home).load()
    assert record['step'] == 'prepared'
    key_name = record['candidate']['extra']['api_key_env']
    assert key_name.startswith('ANYTYPE_API_KEY_SETUP_')
    assert key_name in (isolated_home / '.env').read_text()
    assert 'test-key' not in (isolated_home / 'anytype/setup.json').read_text()
    assert not api.created


async def test_desktop_prepared_resume_commits_stopped_and_preserves_host_settings(isolated_home):
    (isolated_home / 'config.yaml').write_text(json.dumps({'model': {'default': 'preserved'}}))
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    assert (await wizard.run(choices()))['state'] == 'restart_required'
    result = await wizard.run({'activation': 'save_stopped'}, resume=True)
    assert result['state'] == 'saved', result
    config, _ = wizard.store.snapshot()
    assert config['model'] == {'default': 'preserved'}
    assert config['gateway']['platforms']['anytype']['enabled']
    assert config['gateway']['platforms']['anytype']['extra']['group_allow_from'] == ['alice-key']


async def test_unknown_live_consumers_block_commit_and_keep_candidate(isolated_home):
    api = FakeAPI()
    result = await Wizard(isolated_home, interactive=False, api_transport=api.transport).run(choices(activation='save_stopped'))
    assert result['state'] == 'needs_input'
    assert result['error']['type'] == 'consumer_boundary'
    assert not (isolated_home / 'config.yaml').exists()
    assert ProfileStore(isolated_home).load()['step'] == 'prepared'


async def test_mcp_conflict_blocks_chat_only_candidate(isolated_home):
    (isolated_home / 'config.yaml').write_text(json.dumps({'mcp_servers': {'anytype': {
        'url': 'http://127.0.0.1:31012/mcp', 'headers': {'Authorization': 'Bearer ${ANYTYPE_OTHER}'}}}}))
    result = await Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport).run(choices())
    assert result['error']['type'] == 'mcp_conflict'
    assert ProfileStore(isolated_home).snapshot()[0]['mcp_servers']['anytype']['url'].endswith('/mcp')


async def test_explicit_chat_only_removal_resolves_mcp_conflict(isolated_home):
    (isolated_home / 'config.yaml').write_text(json.dumps({'mcp_servers': {'anytype': {
        'url': 'http://127.0.0.1:31012/mcp', 'headers': {'Authorization': 'Bearer ${ANYTYPE_OTHER}'}}}}))
    result = await Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport,
                          lifecycle=FakeLifecycle(running=(False,))).run(choices(
        activation='save_stopped', mcp={'mode': 'none', 'conflict': 'remove'}))
    assert result['state'] == 'saved', result
    assert 'anytype' not in ProfileStore(isolated_home).snapshot()[0]['mcp_servers']


async def test_old_account_cannot_be_replaced_during_repair(isolated_home):
    (isolated_home / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': extra()}}}}))
    result = await Wizard(isolated_home, interactive=False, api_transport=FakeAPI(identity='other').transport).run(
        choices(action='repair', expected_account='bot-key'))
    assert result['error']['type'] == 'account_mismatch'
    assert ProfileStore(isolated_home).snapshot()[0]['gateway']['platforms']['anytype']['extra'] == extra()


async def test_existing_name_is_preserved_and_unsupported_rename_does_not_create(isolated_home):
    api = FakeAPI(name='Дружній агент 🤖')
    result = await Wizard(isolated_home, interactive=False, api_transport=api.transport).run(choices())
    assert result['agent_name'] == 'Дружній агент 🤖'
    assert not api.created
    await Wizard(isolated_home, interactive=False).run(cancel=True)
    result = await Wizard(isolated_home, interactive=False, api_transport=api.transport).run(choices(agent_name='Different'))
    assert result['error']['type'] == 'rename_unsupported'
    assert not api.created


@pytest.mark.parametrize('name', ['', ' \n ', 'Bot\nname', 'Bot\x00name'])
def test_invalid_agent_names_are_rejected(name):
    with pytest.raises(SetupError):
        agent_name(name)


def test_agent_name_preserves_unicode_and_internal_spaces_and_applies_reported_limits():
    assert agent_name('  良い  agent 👩‍💻  ') == '良い  agent 👩‍💻'
    with pytest.raises(SetupError):
        agent_name('🤖', {'max_bytes': 3})


async def test_optional_home_creation_is_explicit_and_recorded(isolated_home):
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    result = await wizard.run(choices(home={'action': 'create', 'space_id': 'space.full'}))
    assert result['state'] == 'restart_required', result
    assert api.created == ['/v2/spaces/space.full/chats']
    record = wizard.store.load()
    assert record['operations']['home']['state'] == 'complete'
    assert record['candidate']['platform']['home_channel']['chat_id'] == 'space.full/new-home'
    await wizard.run({}, resume=True)
    assert len(api.created) == 1


async def test_interrupted_home_is_not_blindly_created_again(isolated_home):
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    await wizard.run(choices())
    record = wizard.store.load()
    record.pop('candidate')
    record['step'] = 'space_usable'
    record['operations']['home'] = {'id': 'ambiguous', 'state': 'pending', 'space_id': 'space.full'}
    record['choices']['home'] = {'action': 'create', 'space_id': 'space.full'}
    wizard.store.save(record)
    result = await wizard.run({}, resume=True)
    assert result['error']['type'] == 'home_ambiguous'
    assert not api.created


async def test_prepared_candidate_cannot_silently_change_scope(isolated_home):
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport)
    await wizard.run(choices())
    result = await wizard.run({'scope': {'mode': 'single', 'space_id': 'other.full'}}, resume=True)
    assert result['error']['type'] == 'prepared_choices'


def test_secret_reference_is_private_and_input_rejects_raw_credentials(tmp_path):
    secret = tmp_path / 'secret'
    secret.write_text('private-value\n')
    secret.chmod(0o600)
    assert read_secret({'file': str(secret)}) == 'private-value'
    secret.chmod(0o644)
    with pytest.raises(SetupError):
        read_secret({'file': str(secret)})
    source = tmp_path / 'input.json'
    source.write_text(json.dumps({'schema_version': 1, 'account': {'action': 'login', 'key': 'do-not-persist'}}))
    with pytest.raises(SetupError):
        load_input(source)


def test_selected_secret_is_authoritative_and_profile_scoped(monkeypatch):
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    monkeypatch.setenv('ANYTYPE_API_KEY', 'legacy')
    token = set_secret_scope({'ANYTYPE_SELECTED': 'selected'})
    try:
        assert resolve_api_key({'api_key_env': 'ANYTYPE_SELECTED'}) == 'selected'
        assert resolve_api_key({'api_key_env': 'ANYTYPE_MISSING'}, required=False) == ''
        with pytest.raises(ValueError):
            resolve_api_key({'api_key_env': 'ANYTYPE_MISSING'})
    finally:
        reset_secret_scope(token)


def test_setup_registration_is_available_without_credentials_and_has_no_platform_callback(monkeypatch):
    from anytype_plugin.adapter import register
    class Context:
        def register_platform(self, **kwargs):
            self.platform = kwargs
        def register_cli_command(self, **kwargs):
            self.command = kwargs
        def register_hook(self, *args, **kwargs):
            pass
    context = Context()
    register(context)
    assert context.platform['required_env'] == []
    assert 'setup_fn' not in context.platform
    parser = argparse.ArgumentParser()
    context.command['setup_fn'](parser)
    assert parser.parse_args(['setup', '--non-interactive', '--json']).anytype_command == 'setup'
    assert parser.parse_args(['doctor', '--json']).anytype_command == 'doctor'
    assert parser.parse_args(['join']).anytype_command == 'join'


async def test_noninteractive_missing_choice_never_prompts(isolated_home, monkeypatch):
    monkeypatch.setattr('builtins.input', lambda: pytest.fail('unexpected prompt'))
    result = await Wizard(isolated_home, interactive=False).run({})
    assert result['state'] == 'needs_input'
    assert result['error']['type'] == 'missing_choice'


async def test_join_retaining_fixed_scope_reports_outside_scope(isolated_home):
    api = FakeAPI(spaces=('space.full', 'other.full'))
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    result = await wizard.run(choices(scope={'mode': 'single', 'space_id': 'space.full', 'grant': 'all'}, activation='save_stopped'))
    assert result['state'] == 'saved', result
    before = (isolated_home / 'config.yaml').read_bytes()
    result = await wizard.join({'joined_space_id': 'other.full', 'discovery_action': 'retain'})
    assert result['state'] == 'ready', result
    assert result['gateway'] == 'joined; outside gateway discovery scope'
    assert (isolated_home / 'config.yaml').read_bytes() == before

async def test_runtime_gateway_activity_and_standalone_use_selected_reference(isolated_home, monkeypatch):
    import anytype_plugin.adapter as module
    from anytype_plugin.client import Client
    from gateway.config import PlatformConfig
    monkeypatch.setenv('ANYTYPE_SELECTED', 'selected-runtime-key')
    api = FakeAPI()
    tokens = []
    def factory(base, token):
        tokens.append(token)
        return Client(base, token, transport=api.transport)
    monkeypatch.setattr(module, 'Client', factory)
    settings = extra(api_key_env='ANYTYPE_SELECTED', discovery={}, chats=[{'space_id': 'space.full', 'chat_id': 'chat'}])
    config = PlatformConfig(enabled=True, extra=settings)
    adapter = module.AnytypeAdapter(config)
    try:
        assert await adapter.connect(receive=False)
        assert tokens == ['selected-runtime-key']
    finally:
        await adapter.disconnect()
    result = await module.standalone_send(config, 'space.full/chat', 'explicit test')
    assert result['success'], result
    assert tokens == ['selected-runtime-key', 'selected-runtime-key']


def test_inline_secret_cannot_hide_beside_reference_in_journal():
    from anytype_plugin.setup_store import safe_mcp
    with pytest.raises(SetupError):
        safe_mcp({'headers': {'Authorization': 'Bearer private-value ${ANYTYPE_OLD}'}})
    with pytest.raises(SetupError):
        safe_mcp({'env': {'OPENAPI_MCP_HEADERS': '{"Authorization":"Bearer private-value"}'}})


@pytest.mark.parametrize('endpoint', [
    'https://user:sentinel-url-secret@example.test/mcp',
    'https://example.test/mcp?token=sentinel-url-secret',
    'https://example.test/mcp#sentinel-url-secret',
])
async def test_rejected_mcp_url_never_enters_journal(isolated_home, endpoint):
    result = await Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport).run(
        choices(mcp={'mode': 'http', 'url': endpoint, 'shared_api_key': True}))
    assert result['error']['type'] == 'endpoint'
    path = isolated_home / 'anytype/setup.json'
    assert not path.exists() or 'sentinel-url-secret' not in path.read_text()
    assert 'sentinel-url-secret' not in json.dumps(result)
    assert not (isolated_home / 'config.yaml').exists()


async def test_existing_oauth_secret_is_rejected_before_journal_admission(isolated_home):
    config = {'mcp_servers': {'anytype': {'url': 'https://example.test/mcp',
        'oauth': {'client_id': 'public-client', 'client_secret': 'sentinel-oauth-secret'}}}}
    path = isolated_home / 'config.yaml'
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    result = await Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport).run(choices())
    assert result['error']['type'] == 'mcp_config'
    assert not (isolated_home / 'anytype/setup.json').exists()
    assert 'sentinel-oauth-secret' not in json.dumps(result)
    assert path.read_bytes() == before


async def test_interactive_url_rejected_before_secret_or_journal_write(isolated_home, monkeypatch):
    monkeypatch.setattr('anytype_plugin.onboarding.choose', lambda *args: 'http')
    monkeypatch.setattr('anytype_plugin.onboarding.prompt', lambda *args, **kwargs: 'https://example.test/mcp?token=sentinel-private')
    selected = choices(base_url='http://127.0.0.1:31009')
    selected.pop('mcp')
    result = await Wizard(isolated_home, interactive=True, api_transport=FakeAPI().transport).run(selected)
    assert result['error']['type'] == 'endpoint', result
    assert 'sentinel-private' not in (isolated_home / 'anytype/setup.json').read_text()


def test_journal_save_rechecks_mutated_choices_and_existing_mcp_fields(isolated_home):
    store = ProfileStore(isolated_home)
    record = store.begin({}, 'absent')
    before = store.path.read_bytes()
    record['choices'] = {'mcp': {'mode': 'http', 'url': 'https://example.test?token=sentinel'}}
    with pytest.raises(SetupError):
        store.save(record)
    assert store.path.read_bytes() == before
    record['choices'] = {}
    record['prior_owned']['mcp'] = {'url': 'https://example.test/mcp', 'oauth': {'client_secret': 'sentinel'}}
    with pytest.raises(SetupError):
        store.save(record)
    assert store.path.read_bytes() == before
