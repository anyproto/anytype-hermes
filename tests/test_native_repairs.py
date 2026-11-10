"""Regression boundaries from the native wiring contract/recovery/lifecycle review."""
import json
from pathlib import Path
import sys

import httpx
import pytest

from anytype_plugin.setup_backend import NativeCLI, private_process
from anytype_plugin.setup_store import ProfileStore, SetupError, exit_code
from test_native_setup import NativeFixture, native_choices, wizard, envelope
from test_setup_diagnostics import FakeAPI
from test_setup_publication import FakeLifecycle


@pytest.mark.parametrize('state,code,payload,expected_exit', [
    ('unavailable', 'rpc_unavailable', None, 4),
    ('needs_input', 'authentication_required', None, 2),
    ('needs_input', 'space_not_found', None, 2),
    ('unavailable', 'rpc_timeout', {'operation_id': 'test-operation', 'space_id': 'space.full', 'stage': 'join_sent'}, 4),
])
def test_membership_failures_preserve_native_exit_without_identity_payload(state, code, payload, expected_exit):
    response = envelope(state=state, code=code)
    response['result'] = payload
    native = NativeCLI(sys.executable, runner=lambda *args, **kwargs: (expected_exit, json.dumps(response).encode()))
    with pytest.raises(SetupError) as caught:
        native.membership(native.call(['space', 'status', 'space.full']), 'space.full')
    assert exit_code({'state': caught.value.state}) == expected_exit
    assert caught.value.code != 'cli_membership'
    assert 'PRIVATE-' not in str(caught.value) + caught.value.next_action


@pytest.mark.parametrize('state', ['request_sent', 'pending_approval', 'syncing'])
def test_pending_membership_requires_correct_identity_and_keeps_exit_three(state):
    response = envelope({'space': {'id': 'space.full', 'state': state}}, state=state)
    with pytest.raises(SetupError) as caught:
        NativeCLI.membership(response, 'space.full')
    assert caught.value.state == state and exit_code({'state': caught.value.state}) == 3
    with pytest.raises(SetupError) as caught:
        NativeCLI.membership(response, 'wrong.full')
    assert caught.value.code == 'cli_membership' and caught.value.state == 'incompatible'


class ReceiptAPI(FakeAPI):
    def __init__(self, native, **kwargs):
        self.native = native
        super().__init__(**kwargs)

    def handle(self, request):
        response = super().handle(request)
        if request.url.path == '/v2/auth/whoami':
            key = request.headers.get('Authorization', '').removeprefix('Bearer ')
            receipt = next((r['result'] for r in self.native.receipts.values() if r['result'].get('api_key') == key), None)
            if receipt:
                body = response.json()
                body['key'] = {'id': receipt['credential_id'], 'name': receipt['application_name']}
                return httpx.Response(200, json=body)
        return response


async def test_exact_application_name_survives_creation_and_existing_key_replacement(isolated_home):
    native = NativeFixture(isolated_home)
    api = ReceiptAPI(native)
    w = wizard(isolated_home, native, api, lifecycle=FakeLifecycle(running=(False,)))
    value = native_choices(application_name=' Hermes ', activation='save_stopped')
    assert (await w.run(value))['state'] == 'saved'
    result = await w.run({**value, 'action': 'repair', 'activation': 'defer'})
    assert result['state'] == 'restart_required', result
    assert w.store.load()['native']['application_name'] == ' Hermes '
    assert all(r['result']['application_name'] == ' Hermes ' for r in native.receipts.values() if r['result'].get('api_key'))
    assert native.key_count == 2


async def test_dash_prefixed_names_use_delimited_positionals_and_stable_logical_intent(isolated_home):
    native = NativeFixture(isolated_home, account='')
    api = ReceiptAPI(native, name='--Agent')
    result = await wizard(isolated_home, native, api).run(native_choices(
        account={'action': 'create'}, agent_name='--Agent', application_name='--Integration'))
    assert result['state'] == 'restart_required', result
    calls = [(a, '--Agent' if a[1:3] == ['auth', 'create'] else '--Integration') for a in native.mutations()
             if a[1:3] == ['auth', 'create'] or a[1:4] == ['auth', 'apikey', 'create']]
    assert len(calls) == 2
    for argv, name in calls:
        assert argv[argv.index('--') + 1:] == [name]
        assert argv.index('--operation-id') < argv.index('--')
        assert argv.index('--expect-account') < argv.index('--')
        assert argv.index('--json') < argv.index('--')
    record = ProfileStore(isolated_home).load()
    assert record['operations']['native_account']['intent']['args'] == ['auth', 'create', '--Agent']
    assert record['operations']['native_key']['intent']['args'][3] == '--Integration'
    binary = Path(__file__).resolve().parents[1] / '.runtime/setup-implementation-20261009/anytype-cli/dist/anytype'
    if binary.exists():
        for argv, _ in calls:
            boundary = argv.index('--')
            # Actual parser only: --help exits before engine/config construction.
            status, raw = private_process([str(binary), *argv[1:boundary], '--help', *argv[boundary:]])
            assert status == 0 and json.loads(raw)['state'] == 'ready'


class InterruptedLogin(NativeFixture):
    transition = False
    first_login = True
    transition_network = ''
    restored_network = 'network-A'
    restored_name = 'Hermes'

    def info(self):
        result = super().info()
        if not self.first_login and not self.transition:
            result['network']['id'] = self.restored_network
            result['account']['display_name'] = self.restored_name
        return result

    def run(self, argv, secret=None, *, env=None):
        if argv[1] == 'inspect' and (self.first_login or self.transition):
            self.calls.append((list(argv), secret, dict(env or {})))
            info = self.info()
            info['account'].pop('display_name', None)
            info['account']['authenticated'] = False
            if self.transition:
                info['account']['id'] = ''
                info['network']['id'] = self.transition_network
                info['api'].update(origin='', bound=False, listen_address='')
            code = 'account_transition' if self.transition else 'authentication_required'
            return 2, json.dumps(envelope(info, state='needs_input', code=code)).encode()
        if argv[1:3] == ['auth', 'login'] and self.first_login:
            self.calls.append((list(argv), secret, dict(env or {})))
            self.first_login, self.transition = False, True
            raise SetupError('cli_timeout', 'Native command timed out.', state='unavailable')
        if argv[1:3] == ['auth', 'login']:
            self.transition = False
        return super().run(argv, secret, env=env)


async def interrupted_login(home, native):
    supplied = native_choices(account={'action': 'login', 'key_ref': {'env': 'RECOVERY'}},
                              agent_name='Hermes', network={'mode': 'preserve', 'expected_id': 'network-A'})
    w = wizard(home, native, secret_reader=lambda ref: 'private-recovery')
    result = await w.run(supplied)
    assert result['state'] == 'unavailable', result
    return w.store.load()


async def test_interrupted_login_retains_missing_network_and_defers_missing_name_until_auth(isolated_home):
    native = InterruptedLogin(isolated_home)
    before = await interrupted_login(isolated_home, native)
    assert before['native']['network']['id'] == 'network-A'
    result = await wizard(isolated_home, native, secret_reader=lambda ref: pytest.fail('reread private recovery')).run({}, resume=True)
    assert result['state'] == 'restart_required', result
    after = ProfileStore(isolated_home).load()
    assert after['native']['network']['id'] == 'network-A'
    assert after['operations']['native_account']['id'] == before['operations']['native_account']['id']
    assert after['operations']['native_account']['intent'] == before['operations']['native_account']['intent']


@pytest.mark.parametrize('failure,expected_code,replays', [
    ('transition_network', 'native_network', 0),
    ('restored_network', 'native_network', 1),
    ('restored_name', 'rename_unsupported', 1),
])
async def test_interrupted_login_never_accepts_changed_network_or_unverified_name(isolated_home, failure, expected_code, replays):
    native = InterruptedLogin(isolated_home)
    await interrupted_login(isolated_home, native)
    setattr(native, failure, 'different-proof')
    count = len(native.mutations())
    result = await wizard(isolated_home, native, secret_reader=lambda ref: pytest.fail('reread private recovery')).run({}, resume=True)
    assert result['error']['type'] == expected_code, result
    assert len(native.mutations()) - count == replays
    assert native.key_count == 0
    assert ProfileStore(isolated_home).load()['native']['network']['id'] == 'network-A'


async def joined_profile(home, *, fixed=False):
    native = NativeFixture(home)
    api = FakeAPI(spaces=('space.full', 'other.full'))
    w = wizard(home, native, api, lifecycle=FakeLifecycle(running=(False,)),
               secret_reader=lambda ref: 'private-invitation')
    value = native_choices(activation='save_stopped')
    if fixed:
        value['scope'] = {'mode': 'single', 'space_id': 'space.full', 'grant': 'all'}
    result = await w.run(value)
    assert result['state'] == 'saved', result
    return native, api, w


@pytest.mark.parametrize('pending', [False, True])
async def test_changed_authoritative_profile_blocks_new_or_resumed_native_join(isolated_home, pending):
    native, api, w = await joined_profile(isolated_home)
    if pending:
        native.join_state = 'pending_approval'
        result = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'all'})
        assert result['state'] == 'pending_approval' and result['changed'] is True
    config, _ = w.store.snapshot()
    config['gateway']['platforms']['anytype']['extra']['base_url'] = 'http://127.0.0.1:45124'
    (isolated_home / 'config.yaml').write_text(json.dumps(config))
    api.identity = 'different-live-bot'
    before = len(native.mutations())
    result = await w.join({} if pending else {'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'all'}, resume=pending)
    assert result['error']['type'] == 'account_mismatch', result
    assert len(native.mutations()) == before
    assert result['changed'] is pending


@pytest.mark.parametrize('change', ['origin', 'credential', 'native_binding'])
async def test_native_join_requires_same_current_connection_and_native_provenance(isolated_home, change):
    native, api, w = await joined_profile(isolated_home)
    if change == 'origin':
        config, _ = w.store.snapshot()
        config['gateway']['platforms']['anytype']['extra']['base_url'] = 'http://127.0.0.1:45124'
        (isolated_home / 'config.yaml').write_text(json.dumps(config))
    elif change == 'credential':
        original = api.handle
        def replaced(request):
            response = original(request)
            if request.url.path == '/v2/auth/whoami':
                data = response.json()
                data['key']['id'] = 'externally-replaced-id'
                return httpx.Response(200, json=data)
            return response
        w.api_transport = httpx.MockTransport(replaced)
    else:
        native.home = isolated_home / 'other-native-instance'
    before = len(native.mutations())
    result = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'all'})
    assert result['error']['type'] in {'native_origin', 'native_provenance', 'cli_binding'}, result
    assert len(native.mutations()) == before


async def test_native_join_allows_unrelated_profile_edit_and_reports_observed_change(isolated_home):
    native, api, w = await joined_profile(isolated_home, fixed=True)
    config, _ = w.store.snapshot()
    config['model'] = {'default': 'unrelated-change'}
    (isolated_home / 'config.yaml').write_text(json.dumps(config))
    result = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'retain'})
    assert result['state'] == 'ready' and result['changed'] is True, result
    assert result['gateway'] == 'joined; outside gateway discovery scope'
    assert w.store.snapshot()[0]['model']['default'] == 'unrelated-change'


async def test_native_join_preserves_changed_when_post_submission_api_probe_fails(isolated_home):
    native, api, w = await joined_profile(isolated_home)
    original = native.run
    def submit(argv, *args, **kwargs):
        result = original(argv, *args, **kwargs)
        if argv[1:3] == ['space', 'join']:
            api.identity = 'changed-after-submission'
        return result
    native.run = submit
    result = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'all'})
    assert result['error']['type'] == 'account_mismatch' and result['changed'] is True, result


async def test_interrupted_login_changed_expected_network_rejected_before_replay(isolated_home):
    native = InterruptedLogin(isolated_home)
    await interrupted_login(isolated_home, native)
    before = len(native.mutations())
    result = await wizard(isolated_home, native).run({'network': {'mode': 'preserve', 'expected_id': 'different-network'}}, resume=True)
    assert result['error']['type'] == 'native_network', result
    assert len(native.mutations()) == before


@pytest.mark.parametrize('state', ['request_sent', 'pending_approval', 'syncing'])
async def test_pending_join_changed_survives_unrelated_edit_and_resume(isolated_home, state):
    native, api, w = await joined_profile(isolated_home, fixed=True)
    native.join_state = state
    result = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'retain'})
    assert result['state'] == state and result['changed'] is True, result
    attempt_id = w.store.load()['join_attempt']['operations']['native_join']['id']
    config, _ = w.store.snapshot()
    config['model'] = {'default': 'unrelated-change'}
    (isolated_home / 'config.yaml').write_text(json.dumps(config))
    native.join_state = 'joined'
    w.secret_reader = lambda ref: pytest.fail('checkpointed invitation reread')
    result = await w.join({}, resume=True)
    assert result['state'] == 'ready' and result['changed'] is True, result
    joins = [a for a in native.mutations() if a[1:3] == ['space', 'join']]
    assert len(joins) == 2 and all(attempt_id in args for args in joins)


def managed_settings(home, monkeypatch, settings):
    directory = home.parent / 'managed-join-policy'
    directory.mkdir()
    (directory / 'config.yaml').write_text(json.dumps(settings))
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(directory))


@pytest.mark.parametrize('pending', [False, True])
@pytest.mark.parametrize('pin', [
    {'gateway': {'platforms': {'anytype': {'extra': {'base_url': 'http://127.0.0.1:45124'}}}}},
    {'gateway': {'platforms': {'anytype': {'extra': {'api_key_env': 'ANYTYPE_MANAGED_JOIN_KEY'}}}}},
    {'mcp_servers': {'anytype': {'enabled': False}}},
])
async def test_native_managed_pins_block_new_and_pending_join_before_preflight(isolated_home, monkeypatch, pending, pin):
    native, api, w = await joined_profile(isolated_home, fixed=True)
    if pending:
        native.join_state = 'pending_approval'
        initial = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'retain'})
        assert initial['state'] == 'pending_approval' and initial['changed'] is True
    native_calls, api_calls = len(native.calls), len(api.requests)
    before_config = (isolated_home / 'config.yaml').read_bytes()
    before_record = w.store.path.read_bytes()
    managed_settings(isolated_home, monkeypatch, pin)
    result = await w.join({} if pending else {'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'retain'}, resume=pending)
    assert result['error']['type'] == 'managed_config', result
    assert result['changed'] is pending
    assert len(native.calls) == native_calls and len(api.requests) == api_calls
    assert (isolated_home / 'config.yaml').read_bytes() == before_config
    assert w.store.path.read_bytes() == before_record


@pytest.mark.parametrize('pending', [False, True])
async def test_native_join_allows_unrelated_managed_settings(isolated_home, monkeypatch, pending):
    native, api, w = await joined_profile(isolated_home, fixed=True)
    if pending:
        native.join_state = 'pending_approval'
        initial = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'retain'})
        assert initial['state'] == 'pending_approval'
        native.join_state = 'joined'
        w.secret_reader = lambda ref: pytest.fail('checkpointed invitation reread')
    managed_settings(isolated_home, monkeypatch, {'model': {'default': 'administrator-model'}})
    result = await w.join({} if pending else {'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'retain'}, resume=pending)
    assert result['state'] == 'ready' and result['changed'] is True, result
    assert result['gateway'] == 'joined; outside gateway discovery scope'


async def test_native_join_followup_resume_retains_setup_managed_guard(isolated_home, monkeypatch):
    native, api, w = await joined_profile(isolated_home)
    native.members['other.full'] = 'joined'
    result = await w.join({'joined_space_id': 'other.full', 'discovery_action': 'all'})
    assert result['state'] == 'restart_required', result
    assert w.store.load()['join_followup'] == 'other.full'
    calls, requests = len(native.calls), len(api.requests)
    before_config = (isolated_home / 'config.yaml').read_bytes()
    managed_settings(isolated_home, monkeypatch, {'gateway': {'platforms': {'anytype': {'extra': {
        'base_url': 'http://127.0.0.1:45124', 'api_key_env': 'ANYTYPE_MANAGED_JOIN_KEY'}}}}})
    result = await w.join({'activation': 'save_stopped'}, resume=True)
    assert result['error']['type'] == 'managed_config', result
    assert len(native.calls) == calls and len(api.requests) == requests
    assert (isolated_home / 'config.yaml').read_bytes() == before_config
