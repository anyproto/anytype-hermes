import pytest
import httpx
from test_setup import choices
from test_setup_diagnostics import FakeAPI, FakeMCP
from anytype_plugin.onboarding import Wizard

pytestmark = pytest.mark.asyncio

@pytest.mark.parametrize('reference_changed', [False, True])
async def test_resume_uses_corrected_api_secret(isolated_home, monkeypatch, reference_changed):
    class AuthenticatedAPI(FakeAPI):
        def handle(self, request):
            if request.headers.get('Authorization') != 'Bearer correct-api-key':
                return httpx.Response(401)
            return super().handle(request)
    api = AuthenticatedAPI()
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'incorrect-api-key')
    first = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    result = await first.run(choices(api_key_ref={'env': 'ANYTYPE_CANDIDATE'}))
    assert result['error']['type'] == 'credential_rejected'
    ref = 'ANYTYPE_CORRECTED' if reference_changed else 'ANYTYPE_CANDIDATE'
    monkeypatch.setenv(ref, 'correct-api-key')
    second = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    result = await second.run({'api_key_ref': {'env': ref}}, resume=True)
    assert result.get('publication') == 'prepared', result

async def test_resume_uses_corrected_mcp_secret(isolated_home, monkeypatch):
    class AuthenticatedMCP(FakeMCP):
        def handle(self, request):
            if request.headers.get('Authorization') != 'Bearer correct-mcp-key':
                return httpx.Response(401)
            return super().handle(request)
    api, mcp = FakeAPI(), AuthenticatedMCP()
    monkeypatch.setenv('ANYTYPE_MCP_CANDIDATE', 'incorrect-mcp-key')
    first = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=mcp.transport)
    result = await first.run(choices(mcp={'mode': 'http', 'url': 'https://mcp.example.test/mcp',
        'token_ref': {'env': 'ANYTYPE_MCP_CANDIDATE'}}))
    assert result['error']['type'] == 'mcp_not_ready'
    monkeypatch.setenv('ANYTYPE_MCP_CANDIDATE', 'correct-mcp-key')
    second = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=mcp.transport)
    result = await second.run(resume=True)
    assert result.get('publication') == 'prepared', result


@pytest.mark.parametrize('older_journal', [False, True])
async def test_early_missing_choice_resume_cannot_replace_existing_account(isolated_home, monkeypatch, older_journal):
    import json
    from test_setup_diagnostics import extra
    from test_setup_publication import FakeLifecycle
    class Accounts(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith('/members/me'):
                data = response.json()
                data['identity'] = 'prior-account' if request.headers['Authorization'] == 'Bearer prior-key' else 'replacement-account'
                return httpx.Response(200, json=data)
            return response
    api = Accounts()
    config = {'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': extra()}}}}
    path = isolated_home / 'config.yaml'
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    monkeypatch.setenv('ANYTYPE_API_KEY', 'prior-key')
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'replacement-key')
    first = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    result = await first.run({'action': 'repair'})
    assert result['error']['type'] == 'missing_choice'
    record = first.store.load()
    assert record['existing_connection'] is True
    assert record['choices']['action'] == 'repair'
    if older_journal:
        record.pop('existing_connection')
        first.store.path.write_text(json.dumps(record))
    lifecycle = FakeLifecycle(running=(True,))
    second = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=lifecycle)
    result = await second.run(choices(api_key_ref={'env': 'ANYTYPE_CANDIDATE'}, activation='save_stopped'), resume=True)
    assert result['error']['type'] == 'account_mismatch', result
    assert any(headers['authorization'] == 'Bearer prior-key' for method, route, headers in api.requests)
    assert path.read_bytes() == before
    assert not lifecycle.stopped


async def test_initial_repair_journal_keeps_verified_prior_metadata(isolated_home):
    from test_setup_publication import FakeLifecycle
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport, lifecycle=FakeLifecycle(running=(False,)))
    assert (await wizard.run(choices(activation='save_stopped')))['state'] == 'saved'
    result = await wizard.run({'action': 'repair'})
    assert result['error']['type'] == 'missing_choice'
    record = wizard.store.load()
    assert record['existing_connection'] is True
    assert record['prior_account_identity'] == 'bot-key'
    assert record['prior_connection']['credential']['name'] == 'Hermes'


@pytest.mark.parametrize('change_reference', [False, True])
async def test_corrected_mcp_keeps_completed_home_and_old_secret_slots(isolated_home, monkeypatch, change_reference):
    from hermes_cli.config import load_env
    class AuthenticatedMCP(FakeMCP):
        def handle(self, request):
            if request.headers.get('Authorization') != 'Bearer correct-mcp-key':
                return httpx.Response(401)
            return super().handle(request)
    api, mcp = FakeAPI(), AuthenticatedMCP()
    monkeypatch.setenv('ANYTYPE_MCP_CANDIDATE', 'incorrect-mcp-key')
    original_mcp = {'mode': 'http', 'url': 'https://mcp.example.test/mcp', 'token_ref': {'env': 'ANYTYPE_MCP_CANDIDATE'}}
    first = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=mcp.transport)
    result = await first.run(choices(home={'action': 'create', 'space_id': 'space.full'}, mcp=original_mcp))
    assert result['error']['type'] == 'mcp_not_ready'
    before = first.store.load()
    original_slots = load_env()
    assert before['operations']['home']['chat_id'] == 'new-home'
    name = 'ANYTYPE_MCP_CORRECTED' if change_reference else 'ANYTYPE_MCP_CANDIDATE'
    monkeypatch.setenv(name, 'correct-mcp-key')
    second = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=mcp.transport)
    update = {'mcp': {**original_mcp, 'token_ref': {'env': name}}} if change_reference else {}
    result = await second.run(update, resume=True)
    assert result['publication'] == 'prepared', result
    after = second.store.load()
    assert after['id'] == before['id']
    assert after['operations']['home'] == before['operations']['home']
    assert api.created == ['/v2/spaces/space.full/chats']
    assert after['mcp_token_env'] != before['mcp_token_env']
    assert after['api_key_env'] == before['api_key_env']
    assert all(load_env()[key] == value for key, value in original_slots.items())
    assert load_env()[after['mcp_token_env']] == 'correct-mcp-key'


@pytest.mark.parametrize('source', ['active_scope', 'external_scope', 'scoped_missing'])
async def test_keep_mcp_uses_runtime_scope_instead_of_stale_dotenv(isolated_home, monkeypatch, source):
    import json
    from agent.secret_scope import set_secret_scope, reset_secret_scope, build_profile_secret_scope
    class ScopedMCP(FakeMCP):
        def handle(self, request):
            self.identity = 'bot-key' if request.headers.get('Authorization') == 'Bearer stale-file-key' else 'other-account'
            return super().handle(request)
    old = {'mcp_servers': {'anytype': {'url': 'https://mcp.example.test/mcp',
                                    'headers': {'Authorization': 'Bearer ${ANYTYPE_KEEP}'}}}}
    path = isolated_home / 'config.yaml'
    path.write_text(json.dumps(old))
    before = path.read_bytes()
    (isolated_home / '.env').write_text('ANYTYPE_KEEP=stale-file-key\n')
    if source == 'external_scope':
        monkeypatch.setattr('hermes_cli.env_loader.get_secret_source_values',
                            lambda home: {'ANYTYPE_KEEP': 'external-account-key'})
        scope = build_profile_secret_scope(isolated_home)
        assert scope['ANYTYPE_KEEP'] == 'external-account-key'
    else:
        scope = {} if source == 'scoped_missing' else {'ANYTYPE_KEEP': 'active-account-key'}
    scope['ANYTYPE_API_KEY'] = 'test-key'
    bound = set_secret_scope(scope, profile_home=str(isolated_home))
    try:
        api, mcp = FakeAPI(), ScopedMCP()
        wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=mcp.transport)
        result = await wizard.run(choices(mcp={'mode': 'keep'}))
        assert result['error']['type'] == 'mcp_not_ready', result
        assert path.read_bytes() == before
        if source == 'scoped_missing':
            assert not mcp.methods
        else:
            assert 'tools/call' in mcp.methods
    finally:
        reset_secret_scope(bound)


async def test_rollback_api_uses_active_scope_over_stale_dotenv(isolated_home, monkeypatch):
    import json
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from test_setup_diagnostics import extra
    from test_setup_publication import FakeLifecycle
    class Accounts(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith('/members/me') and request.headers['Authorization'] == 'Bearer external-account-key':
                data = response.json()
                data['identity'] = 'other-account'
                return httpx.Response(200, json=data)
            return response
    (isolated_home / '.env').write_text('ANYTYPE_PRIOR=stale-file-key\n')
    monkeypatch.setenv('ANYTYPE_PRIOR', 'stale-file-key')
    (isolated_home / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {
        'enabled': True, 'extra': extra(api_key_env='ANYTYPE_PRIOR')}}}}))
    api = Accounts()
    lifecycle = FakeLifecycle(running=(False,))
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=lifecycle)
    assert (await wizard.run(choices(action='repair', activation='save_stopped')))['state'] == 'saved'
    before = wizard.store.config_path.read_bytes()
    bound = set_secret_scope({'ANYTYPE_PRIOR': 'external-account-key'}, profile_home=str(isolated_home))
    try:
        api.requests.clear()
        result = await wizard.run(rollback=True)
        assert result['error']['type'] == 'account_mismatch', result
        assert api.requests[0][2]['authorization'] == 'Bearer external-account-key'
        assert wizard.store.config_path.read_bytes() == before
        assert not lifecycle.stopped
    finally:
        reset_secret_scope(bound)


async def test_fresh_staged_values_work_with_scope_snapshot_but_do_not_override_it(isolated_home):
    from types import MappingProxyType
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from test_setup_publication import FakeLifecycle
    class Accounts(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith('/members/me') and request.headers['Authorization'] == 'Bearer different-account-key':
                data = response.json()
                data['identity'] = 'other-account'
                return httpx.Response(200, json=data)
            return response
    api = Accounts()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    bound = set_secret_scope(MappingProxyType({'ANYTYPE_API_KEY': 'test-key'}), profile_home=str(isolated_home))
    try:
        result = await wizard.run(choices())
        assert result['publication'] == 'prepared', result
        record = wizard.store.load()
        fresh = record['api_key_env']
        assert wizard.selected_key(record['candidate']['extra'], record) == 'test-key'
    finally:
        reset_secret_scope(bound)
    bound = set_secret_scope({fresh: 'different-account-key'}, profile_home=str(isolated_home))
    try:
        result = await wizard.run({'activation': 'save_stopped'}, resume=True)
        assert result['error']['type'] == 'account_mismatch', result
        assert not wizard.store.config_path.exists()
    finally:
        reset_secret_scope(bound)


@pytest.mark.parametrize('managed', [
    {'gateway': {'platforms': {'anytype': {'extra': {'base_url': 'https://administrator.invalid'}}}}},
    {'mcp_servers': {'anytype': {'enabled': False}}},
])
async def test_administrator_anytype_pins_refuse_before_staging_or_stopping(isolated_home, monkeypatch, managed):
    import json
    from test_setup_publication import FakeLifecycle
    directory = isolated_home.parent / 'managed'
    directory.mkdir()
    (directory / 'config.yaml').write_text(json.dumps(managed))
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(directory))
    lifecycle = FakeLifecycle(running=(True,))
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=lifecycle)
    result = await wizard.run(choices(activation='save_stopped'))
    assert result['error']['type'] == 'managed_config', result
    assert 'administrator' in result['next_action']
    assert not lifecycle.stopped and lifecycle.consumers[0]['running']
    assert not api.requests
    assert not wizard.store.config_path.exists()
    assert not (isolated_home / '.env').exists()


async def test_managed_pin_added_after_prepare_blocks_publication(isolated_home, monkeypatch):
    import json
    from test_setup_publication import FakeLifecycle
    lifecycle = FakeLifecycle(running=(True,))
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport, lifecycle=lifecycle)
    assert (await wizard.run(choices()))['publication'] == 'prepared'
    directory = isolated_home.parent / 'managed'
    directory.mkdir()
    (directory / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {'enabled': False}}}}))
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(directory))
    result = await wizard.run({'activation': 'save_stopped'}, resume=True)
    assert result['error']['type'] == 'managed_config'
    assert not lifecycle.stopped
    assert not wizard.store.config_path.exists()
    assert wizard.store.load()['step'] == 'prepared'


async def test_unrelated_administrator_model_does_not_block_owned_publication(isolated_home, monkeypatch):
    import json
    from hermes_cli.config import load_config_readonly
    from test_setup_publication import FakeLifecycle
    directory = isolated_home.parent / 'managed'
    directory.mkdir()
    (directory / 'config.yaml').write_text(json.dumps({'model': {'default': 'administrator/model'}}))
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(directory))
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport, lifecycle=FakeLifecycle(running=(False,)))
    result = await wizard.run(choices(base_url='https://candidate.example.test', activation='save_stopped'))
    assert result['state'] == 'saved', result
    effective = load_config_readonly()
    assert effective['gateway']['platforms']['anytype']['extra']['base_url'] == 'https://candidate.example.test'
    assert effective['model']['default'] == 'administrator/model'


async def test_mcp_only_rollback_without_identity_proof_cannot_report_saved(isolated_home, monkeypatch):
    import json
    from test_setup_publication import FakeLifecycle
    old = {'mcp_servers': {'anytype': {'url': 'https://mcp.example.test/mcp',
                                    'headers': {'Authorization': 'Bearer ${ANYTYPE_OLD_MCP}'}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(old))
    monkeypatch.setenv('ANYTYPE_OLD_MCP', 'old-mcp-key')
    lifecycle = FakeLifecycle(running=(False,))
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport, mcp_transport=FakeMCP().transport, lifecycle=lifecycle)
    result = await wizard.run(choices(mcp={'mode': 'none', 'conflict': 'remove'}, activation='save_stopped'))
    assert result['state'] == 'saved', result
    before = wizard.store.config_path.read_bytes()
    monkeypatch.delenv('ANYTYPE_OLD_MCP')
    result = await wizard.run(rollback=True)
    assert result['error']['type'] == 'rollback_mcp_unverified', result
    assert result['next_action']
    assert wizard.store.config_path.read_bytes() == before
    assert not lifecycle.stopped


@pytest.mark.parametrize('lose_secret', [False, True])
async def test_disabled_gateway_mcp_rollback_validates_independently_and_again_on_resume(isolated_home, monkeypatch, lose_secret):
    import json
    from test_setup_diagnostics import extra
    from test_setup_publication import FakeLifecycle
    old = {'gateway': {'platforms': {'anytype': {'enabled': False, 'extra': extra(api_key_env='ANYTYPE_PRIOR')}}},
           'mcp_servers': {'anytype': {'url': 'https://mcp.example.test/mcp',
                                     'headers': {'Authorization': 'Bearer ${ANYTYPE_OLD_MCP}'}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(old))
    monkeypatch.setenv('ANYTYPE_PRIOR', 'prior-api-key')
    monkeypatch.setenv('ANYTYPE_OLD_MCP', 'old-mcp-key')
    api, mcp = FakeAPI(), FakeMCP()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=mcp.transport, lifecycle=FakeLifecycle(running=(False,)))
    result = await wizard.run(choices(action='repair', mcp={'mode': 'none', 'conflict': 'remove'}, activation='save_stopped'))
    assert result['state'] == 'saved', result
    # The inactive API key is not needed to validate the independent MCP server.
    monkeypatch.delenv('ANYTYPE_PRIOR')
    api.requests.clear()
    before = wizard.store.config_path.read_bytes()
    result = await wizard.run({'activation': 'defer'}, rollback=True)
    assert result['publication'] == 'prepared', result
    assert result['mcp']['state'] == 'ready'
    assert 'tools/call' in mcp.methods
    assert not api.requests
    if lose_secret:
        monkeypatch.delenv('ANYTYPE_OLD_MCP')
    result = await wizard.run({'activation': 'save_stopped'}, resume=True)
    if lose_secret:
        assert result['error']['type'] == 'mcp_not_ready', result
        assert wizard.store.config_path.read_bytes() == before
    else:
        assert result['state'] == 'saved', result
        current = wizard.store.snapshot()[0]
        assert current['gateway']['platforms']['anytype']['enabled'] is False
        assert current['mcp_servers']['anytype'] == old['mcp_servers']['anytype']


async def test_corrected_private_file_uses_fresh_api_slot_and_retains_original(isolated_home):
    from hermes_cli.config import load_env
    class AuthenticatedAPI(FakeAPI):
        def handle(self, request):
            if request.headers.get('Authorization') != 'Bearer corrected-file-key':
                return httpx.Response(401)
            return super().handle(request)
    private = isolated_home.parent / 'private-key'
    private.write_text('incorrect-file-key')
    private.chmod(0o600)
    api = AuthenticatedAPI()
    first = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    result = await first.run(choices(api_key_ref={'file': str(private)}))
    assert result['error']['type'] == 'credential_rejected'
    before = first.store.load()
    private.write_text('corrected-file-key')
    second = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    result = await second.run(resume=True)
    assert result['publication'] == 'prepared', result
    after = second.store.load()
    assert after['id'] == before['id']
    assert after['api_key_env'] != before['api_key_env']
    assert load_env()[before['api_key_env']] == 'incorrect-file-key'
    assert load_env()[after['api_key_env']] == 'corrected-file-key'


async def test_mcp_rollback_uses_scoped_token_even_when_old_dotenv_key_still_matches(isolated_home, monkeypatch):
    import json
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    from test_setup_diagnostics import extra
    from test_setup_publication import FakeLifecycle
    class ScopedMCP(FakeMCP):
        def handle(self, request):
            self.identity = 'other-account' if request.headers.get('Authorization') == 'Bearer scoped-other-key' else 'bot-key'
            return super().handle(request)
    old = {'gateway': {'platforms': {'anytype': {'enabled': False, 'extra': extra()}}},
           'mcp_servers': {'anytype': {'url': 'https://mcp.example.test/mcp',
                                     'headers': {'Authorization': 'Bearer ${ANYTYPE_OLD_MCP}'}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(old))
    (isolated_home / '.env').write_text('ANYTYPE_OLD_MCP=stale-file-key\n')
    monkeypatch.setenv('ANYTYPE_OLD_MCP', 'stale-file-key')
    api, mcp = FakeAPI(), ScopedMCP()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=mcp.transport,
                    lifecycle=FakeLifecycle(running=(False,)))
    result = await wizard.run(choices(action='repair', mcp={'mode': 'none', 'conflict': 'remove'}, activation='save_stopped'))
    assert result['state'] == 'saved', result
    before = wizard.store.config_path.read_bytes()
    bound = set_secret_scope({'ANYTYPE_OLD_MCP': 'scoped-other-key'}, profile_home=str(isolated_home))
    try:
        result = await wizard.run(rollback=True)
        assert result['error']['type'] == 'rollback_mcp', result
        assert result['state'] == 'incompatible'
        assert wizard.store.config_path.read_bytes() == before
        assert 'tools/call' in mcp.methods
    finally:
        reset_secret_scope(bound)


async def test_independent_mcp_rollback_rechecks_current_write_membership(isolated_home):
    import json
    from test_setup_diagnostics import extra
    from test_setup_publication import FakeLifecycle
    old = {'gateway': {'platforms': {'anytype': {'enabled': False, 'extra': extra()}}},
           'mcp_servers': {'anytype': {'url': 'https://mcp.example.test/mcp',
                                     'headers': {'Authorization': 'Bearer ${ANYTYPE_API_KEY}'}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(old))
    mcp = FakeMCP()
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport, mcp_transport=mcp.transport,
                    lifecycle=FakeLifecycle(running=(False,)))
    result = await wizard.run(choices(action='repair', mcp={'mode': 'none', 'conflict': 'remove'}, activation='save_stopped'))
    assert result['state'] == 'saved', result
    before = wizard.store.config_path.read_bytes()
    assert (await wizard.run({'activation': 'defer'}, rollback=True))['publication'] == 'prepared'
    mcp.role = 'viewer'
    result = await wizard.run({'activation': 'save_stopped'}, resume=True)
    assert result['error']['type'] == 'mcp_not_ready', result
    assert wizard.store.config_path.read_bytes() == before


@pytest.mark.parametrize('via_rollback', [False, True])
async def test_fresh_setup_after_empty_anytype_placeholder(isolated_home, monkeypatch, via_rollback):
    import json
    from test_setup_publication import FakeLifecycle
    monkeypatch.delenv('ANYTYPE_API_KEY', raising=False)
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'candidate-key')
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport,
                    lifecycle=FakeLifecycle(running=(False,)))
    if via_rollback:
        result = await wizard.run(choices(api_key_ref={'env': 'ANYTYPE_CANDIDATE'}, activation='save_stopped'))
        assert result['state'] == 'saved', result
        result = await wizard.run(rollback=True)
        assert result['state'] == 'saved', result
        assert wizard.store.snapshot()[0]['gateway']['platforms']['anytype'] == {'extra': {}}
    else:
        (isolated_home / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {'enabled': False, 'extra': {}}}}}))
    result = await wizard.run(choices(action='repair', api_key_ref={'env': 'ANYTYPE_CANDIDATE'}))
    assert result.get('publication') == 'prepared', result


@pytest.mark.parametrize('legacy_missing_alias', [False, True])
async def test_repeated_wrong_account_resume_uses_prior_proof_then_accepts_same_account(isolated_home, monkeypatch, legacy_missing_alias):
    import json
    from test_setup_diagnostics import extra
    from test_setup_publication import FakeLifecycle
    class Accounts(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith('/members/me'):
                data = response.json()
                data['identity'] = 'replacement-account' if request.headers['Authorization'] == 'Bearer wrong-account-key' else 'prior-account'
                return httpx.Response(200, json=data)
            return response
    api = Accounts()
    monkeypatch.setenv('ANYTYPE_API_KEY', 'prior-key')
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'wrong-account-key')
    monkeypatch.setenv('ANYTYPE_CORRECTED', 'new-key-for-prior-account')
    path = isolated_home / 'config.yaml'
    path.write_text(json.dumps({'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': extra()}}}}))
    before = path.read_bytes()
    lifecycle = FakeLifecycle(running=(False,))
    def wizard():
        return Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=lifecycle)
    first = wizard()
    result = await first.run(choices(action='repair', api_key_ref={'env': 'ANYTYPE_CANDIDATE'}, activation='save_stopped'))
    assert result['error']['type'] == 'account_mismatch', result
    saved = json.loads(first.store.path.read_text())
    assert saved['prior_connection']['account_identity'] == 'prior-account'
    assert saved['prior_account_identity'] == 'prior-account'
    assert 'account_identity' not in saved
    assert path.read_bytes() == before
    for _ in range(3):
        if legacy_missing_alias:
            saved = json.loads(first.store.path.read_text())
            saved.pop('prior_account_identity', None)
            saved['existing_connection'] = False
            first.store.path.write_text(json.dumps(saved))
        api.requests.clear()
        result = await wizard().run(resume=True)
        assert result['error']['type'] == 'account_mismatch', result
        assert path.read_bytes() == before
        assert not lifecycle.stopped
        # The saved identity is sufficient; retaining its proof must not require
        # the old token to remain available for every wrong-candidate rejection.
        assert not any(headers['authorization'] == 'Bearer prior-key' for _, _, headers in api.requests)
        saved = json.loads(first.store.path.read_text())
        assert saved['prior_account_identity'] == 'prior-account'
        assert saved['existing_connection'] is True
    result = await wizard().run({'api_key_ref': {'env': 'ANYTYPE_CORRECTED'}}, resume=True)
    assert result['state'] == 'saved', result
    assert result['account_identity'] == 'prior-account'
    assert first.store.load()['account_identity'] == 'prior-account'


async def test_conflicting_saved_identity_cannot_be_overridden_by_expected_account(isolated_home):
    import json
    from test_setup_diagnostics import extra
    from anytype_plugin.onboarding import connection_metadata
    from anytype_plugin.diagnostics import inspect_api
    (isolated_home / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': extra()}}}}))
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    assert (await wizard.run({'action': 'repair'}))['error']['type'] == 'missing_choice'
    record = wizard.store.load()
    record['prior_connection'] = connection_metadata(await inspect_api(extra(), 'test-key', transport=api.transport))
    record['prior_account_identity'] = 'contradictory-account'
    wizard.store.path.write_text(json.dumps(record))
    before = wizard.store.config_path.read_bytes()
    api.requests.clear()
    result = await wizard.run(choices(expected_account='contradictory-account'), resume=True)
    assert result['error']['type'] == 'account_evidence_conflict'
    assert not api.requests
    assert wizard.store.config_path.read_bytes() == before


async def test_first_setup_account_proof_survives_partial_work_without_a_prior_connection(isolated_home, monkeypatch):
    class Accounts(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith('/members/me') and request.headers['Authorization'] == 'Bearer wrong-account-key':
                data = response.json()
                data['identity'] = 'other-account'
                return httpx.Response(200, json=data)
            return response
    api = Accounts()
    bad_mcp = httpx.MockTransport(lambda request: httpx.Response(401))
    first = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=bad_mcp)
    result = await first.run(choices(home={'action': 'create', 'space_id': 'space.full'},
        mcp={'mode': 'http', 'url': 'https://mcp.example.test/mcp', 'shared_api_key': True}))
    assert result['error']['type'] == 'mcp_not_ready'
    record = first.store.load()
    assert record['existing_connection'] is False
    assert record['account_identity'] == 'bot-key'
    assert record['operations']['home']['state'] == 'complete'
    monkeypatch.setenv('ANYTYPE_WRONG', 'wrong-account-key')
    second = Wizard(isolated_home, interactive=False, api_transport=api.transport, mcp_transport=FakeMCP().transport)
    result = await second.run({'api_key_ref': {'env': 'ANYTYPE_WRONG'}}, resume=True)
    assert result['error']['type'] == 'account_mismatch', result
    assert api.created == ['/v2/spaces/space.full/chats']
    assert not second.store.config_path.exists()


@pytest.mark.parametrize('platform', [
    {'enabled': False, 'extra': {}},
    {'extra': {}},
    {'enabled': False, 'extra': {'chats': [], 'group_allow_from': [], 'activity_status': False}},
])
async def test_unconfigured_scaffold_does_not_require_existing_account_action(isolated_home, monkeypatch, platform):
    import json
    monkeypatch.delenv('ANYTYPE_API_KEY', raising=False)
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'fresh-key')
    (isolated_home / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': platform}}}))
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport)
    result = await wizard.run(choices(api_key_ref={'env': 'ANYTYPE_CANDIDATE'}))
    assert result['publication'] == 'prepared', result
    assert wizard.store.load()['existing_connection'] is False


@pytest.mark.parametrize('enabled', [False, True])
async def test_real_prior_connection_remains_protected_when_disabled(isolated_home, monkeypatch, enabled):
    import json
    from test_setup_diagnostics import extra
    class Accounts(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith('/members/me') and request.headers['Authorization'] == 'Bearer replacement-key':
                data = response.json()
                data['identity'] = 'replacement-account'
                return httpx.Response(200, json=data)
            return response
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'replacement-key')
    path = isolated_home / 'config.yaml'
    path.write_text(json.dumps({'gateway': {'platforms': {'anytype': {'enabled': enabled, 'extra': extra()}}}}))
    before = path.read_bytes()
    wizard = Wizard(isolated_home, interactive=False, api_transport=Accounts().transport)
    result = await wizard.run(choices(action='repair', api_key_ref={'env': 'ANYTYPE_CANDIDATE'}))
    assert result['error']['type'] == 'account_mismatch', result
    assert wizard.store.load()['existing_connection'] is True
    assert path.read_bytes() == before


async def test_legacy_incomplete_empty_scaffold_flag_is_reconstructed(isolated_home, monkeypatch):
    import json
    from anytype_plugin.setup_store import ProfileStore
    monkeypatch.delenv('ANYTYPE_API_KEY', raising=False)
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'fresh-key')
    config = {'gateway': {'platforms': {'anytype': {'enabled': False, 'extra': {}}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(config))
    store = ProfileStore(isolated_home)
    config, revision = store.snapshot()
    record = store.begin(config, revision, choices={'action': 'repair'})
    record['existing_connection'] = True  # Earlier versions wrote this for any nonempty dict.
    store.path.write_text(json.dumps(record))
    result = await Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport).run(
        choices(api_key_ref={'env': 'ANYTYPE_CANDIDATE'}), resume=True)
    assert result['publication'] == 'prepared', result
    assert store.load()['existing_connection'] is False


async def test_enabled_gateway_is_not_demoted_to_empty_scaffold(isolated_home, monkeypatch):
    import json
    monkeypatch.setenv('ANYTYPE_CANDIDATE', 'candidate-key')
    config = {'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': {}}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(config))
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport)
    first = await wizard.run({'action': 'repair'})
    assert first['error']['type'] == 'missing_choice'
    assert wizard.store.load()['existing_connection'] is True
    before = wizard.store.config_path.read_bytes()
    result = await wizard.run(choices(api_key_ref={'env': 'ANYTYPE_CANDIDATE'}, activation='save_stopped'), resume=True)
    assert result.get('publication') not in {'prepared', 'committed', 'active'}, result
    assert wizard.store.config_path.read_bytes() == before
