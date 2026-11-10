"""Native contract transport and durable orchestration without live native state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys

import pytest

from anytype_plugin.onboarding import Wizard
from anytype_plugin.setup_backend import NativeCLI, private_process
from anytype_plugin.setup_store import ProfileStore, SetupError
from test_setup import choices
from test_setup_diagnostics import FakeAPI, HeartFullMCP
from test_setup_publication import FakeLifecycle


def envelope(result=None, *, state='ready', code=None, changed=False):
    value = {'schema_version': 1, 'state': state, 'changed': changed, 'result': result or {}, 'next_action': ''}
    if code:
        value['error'] = {'code': code, 'message': 'PRIVATE-ERROR-SECRET', 'retryable': state == 'unavailable'}
        value['next_action'] = 'PRIVATE-NEXT-SECRET'
    return value


CAPABILITIES = {'instance_selection': ['default'], 'isolated_instances': False,
    'account_create': True, 'account_login': True, 'space_join': True, 'scoped_api_keys': True,
    'operation_recovery': 'durable_checkpoints_with_explicit_uncertainty',
    'embedded_http_api': {'whoami': '/v2/auth/whoami'},
    'credential_backend_selection': {'values': ['private_file']},
    'limits': {'display_name_max_bytes': 4096, 'application_name_max_bytes': 128}}


class NativeFixture:
    def __init__(self, home, *, account='bot-key', running=True):
        self.home, self.account, self.running = home, account, running
        self.calls = []
        self.receipts = {}
        self.members = {'space.full': 'joined'}
        self.join_state = 'joined'
        self.origin = 'http://127.0.0.1:45123'
        self.name = 'Hermes'
        self.failure = None
        self.key_count = 0

    def factory(self, executable='anytype', instance=None):
        return NativeCLI(sys.executable, instance, runner=self.run)

    def info(self):
        return {'instance': {'id': 'default', 'isolated': False,
            **{k: str(self.home / k) for k in ('config_path', 'data_path', 'work_path')},
            'grpc_address': '127.0.0.1:31007', 'grpc_web_address': '127.0.0.1:31008'},
            'account': {'id': self.account, 'display_name': self.name, 'authenticated': bool(self.account),
                        'observation': 'live' if self.running else 'configured'},
            'network': {'id': 'network-A' if self.account else '', 'config_path': '', 'observation': 'live'},
            'service': {'installed': self.running, 'running': self.running, 'binding_verified': self.running,
                        'listen_address': '127.0.0.1:45123' if self.running else ''},
            'api': {'origin': self.origin if self.account else '', 'configured_origin': self.origin,
                    'listen_address': '127.0.0.1:45123' if self.account else '',
                    'bound': bool(self.account), 'observation': 'live'},
            'mcp': {'url': self.origin + '/mcp/full', 'authentication': 'bearer',
                    'token_source': 'same_api_key', 'same_grant': True}}

    def run(self, argv, secret=None, *, env=None):
        self.calls.append((list(argv), secret, dict(env or {})))
        args = argv[1:]
        if '--' in args:
            boundary = args.index('--')
            positional = args[boundary + 1:]
            args = args[:boundary]
            count = 3 if args[:3] == ['auth', 'apikey', 'create'] else 2
            args = [*args[:count], *positional, *args[count:]]
        op = args[args.index('--operation-id') + 1] if '--operation-id' in args else None
        command = ' '.join(args[:3])
        if args[0] == 'capabilities':
            result = envelope(CAPABILITIES)
        elif args[0] == 'inspect':
            result = envelope(self.info()) if self.running else envelope(self.info(), state='unavailable', code='rpc_unavailable')
        elif args[0] == 'service':
            self.running = True
            result = envelope({'operation_id': op}, changed=True)
        elif args[:2] in [['auth', 'create'], ['auth', 'login']]:
            self.account = 'bot-key'
            if args[1] == 'create':
                self.name = args[2]
            result = envelope({'operation_id': op, 'account': self.info()['account']}, changed=op not in self.receipts)
        elif args[:2] == ['space', 'join']:
            space = args[args.index('--space-id') + 1] if '--space-id' in args else 'other.full'
            self.members[space] = self.join_state
            result = envelope({'operation_id': op, 'space': {'id': space, 'state': self.join_state}},
                              state='ready' if self.join_state == 'joined' else self.join_state, changed=op not in self.receipts)
        elif args[:2] == ['space', 'list']:
            result = envelope({'account_id': self.account, 'spaces': [{'id': k, 'state': v} for k, v in self.members.items()]})
        elif args[:2] == ['space', 'status']:
            state = self.members.get(args[2], 'unknown')
            result = envelope({'id': args[2], 'state': state}, state='ready' if state == 'joined' else 'needs_input',
                              code=None if state == 'joined' else 'membership_' + state)
        elif command == 'auth apikey list':
            result = envelope({'account_id': self.account, 'credentials': [
                {k: v for k, v in r['result'].items() if k != 'api_key'} for r in self.receipts.values()
                if r['result'].get('credential_id')]})
        elif command == 'auth apikey create':
            if op not in self.receipts:
                self.key_count += 1
            spaces = [args[i+1] for i, value in enumerate(args) if value == '--space']
            suffix = '' if self.key_count == 1 else '-' + str(self.key_count)
            result = self.receipts.get(op) or envelope({'operation_id': op, 'account_id': self.account, 'application_name': args[3],
                               'credential_id': 'key-id' + suffix, 'grant_verified': True, 'api_key': 'native-private-api-key' + suffix,
                               'grant': {'all_spaces': '--all-spaces' in args, 'space_ids': spaces, 'permission': 'read_write'}},
                               changed=op not in self.receipts)
        else:
            raise AssertionError(args)
        if op:
            self.receipts[op] = deepcopy(result)
        if self.failure and self.failure(args):
            self.failure = None
            raise SetupError('cli_timeout', 'Native command timed out.', state='unavailable')
        codes = {'ready': 0, 'needs_input': 2, 'pending_approval': 3, 'request_sent': 3, 'syncing': 3, 'unavailable': 4, 'incompatible': 1}
        return codes[result['state']], json.dumps(result).encode()

    def mutations(self):
        return [args for args, _, _ in self.calls if '--operation-id' in args]


def native_choices(**kwargs):
    result = choices(**{'mode': 'cli', 'service': {'action': 'reuse'}, 'account': {'action': 'reuse'},
                     'application_name': 'Hermes', **kwargs})
    # Deliberately inherited stale reference: a returned native key must win.
    return result


def wizard(home, native, api=None, **kwargs):
    return Wizard(home, interactive=False, native_factory=native.factory,
                  api_transport=(api or FakeAPI()).transport, **kwargs)


async def test_native_reuse_uses_actual_origin_and_returned_key(isolated_home):
    native, api = NativeFixture(isolated_home), FakeAPI()
    result = await wizard(isolated_home, native, api).run(native_choices())
    assert result['state'] == 'restart_required', result
    record = ProfileStore(isolated_home).load()
    assert record['candidate']['extra']['base_url'] == native.origin
    assert record['native']['credential_id'] == 'key-id'
    assert all(headers['authorization'] == 'Bearer native-private-api-key' for _, _, headers in api.requests)
    assert 'native-private-api-key' not in json.dumps(record)
    assert 'native-private-api-key' in (isolated_home / '.env').read_text()
    assert [a[1:4] for a in native.mutations()] == [['auth', 'apikey', 'create']]


async def test_native_create_private_invitation_pending_resume_without_input(isolated_home):
    native = NativeFixture(isolated_home, account='', running=False)
    native.join_state = 'pending_approval'
    value = native_choices(service={'action': 'install'}, account={'action': 'create'}, agent_name='Агент 🌿',
                           join_space=True, invitation_ref={'env': 'INVITE'}, scope={'mode': 'single'})
    api = FakeAPI(spaces=('other.full',), name='Агент 🌿', all_spaces=False)
    first = wizard(isolated_home, native, api, secret_reader=lambda ref: 'private-invitation')
    result = await first.run(value)
    assert result['state'] == 'pending_approval', result
    assert not native.key_count
    record = first.store.load()
    join_id = record['operations']['native_join']['id']
    assert 'private-invitation' not in json.dumps(record)
    native.join_state = 'joined'
    second = wizard(isolated_home, native, api, secret_reader=lambda ref: pytest.fail('resume read the invitation'))
    result = await second.run({}, resume=True)
    assert result['state'] == 'restart_required', result
    joins = [(a, secret) for a, secret, _ in native.calls if a[1:3] == ['space', 'join']]
    assert joins[0][1] == 'private-invitation' and joins[1][1] is None
    assert all(join_id in a for a, _ in joins)
    assert second.store.load()['choices']['scope']['space_id'] == 'other.full'
    assert all('--include-secret' not in a for a, _, _ in native.calls if a[1:3] == ['auth', 'create'])


async def test_native_key_timeout_recovers_exact_operation(isolated_home):
    native = NativeFixture(isolated_home)
    native.failure = lambda args: args[:3] == ['auth', 'apikey', 'create']
    first = wizard(isolated_home, native)
    assert (await first.run(native_choices()))['state'] == 'unavailable'
    key_id = first.store.load()['operations']['native_key']['id']
    assert (await wizard(isolated_home, native).run({}, resume=True))['state'] == 'restart_required'
    keys = [a for a in native.mutations() if a[1:4] == ['auth', 'apikey', 'create']]
    assert len(keys) == 2 and all(key_id in a for a in keys) and native.key_count == 1


@pytest.mark.parametrize('changes', [
    {'scope': {'mode': 'bogus'}}, {'service': {'action': 'restart'}}, {'account': {'action': 'rename'}},
    {'network': {'mode': 'custom', 'config_file': 'relative'}}, {'access': 'bogus'},
    {'activation': 'bogus'}, {'mcp': {'mode': 'bogus'}}, {'home': {'action': 'bogus'}},
    {'mcp': {'mode': 'http', 'shared_api_key': False}}, {'mcp': {'mode': 'http', 'url': 'http://127.0.0.1:99/mcp'}},
    {'expected_account': 'other-account'}, {'base_url': 'http://127.0.0.1:9999'},
])
async def test_native_invalid_choices_precede_mutations(isolated_home, changes):
    native = NativeFixture(isolated_home)
    result = await wizard(isolated_home, native).run(native_choices(**changes))
    assert result['state'] in {'needs_input', 'incompatible'}, result
    assert not native.mutations()


async def test_native_ignores_unrelated_removed_membership(isolated_home):
    native = NativeFixture(isolated_home)
    native.members['old.full'] = 'removed'
    native.members['rejected.full'] = 'rejected'
    assert (await wizard(isolated_home, native).run(native_choices()))['state'] == 'restart_required'
    assert not any('old.full' in a or 'rejected.full' in a for a, _, _ in native.calls)


@pytest.mark.parametrize('raw,code', [(b'not json SECRET', 1), (b'{}\n{}', 0),
    (json.dumps(envelope()).replace('"schema_version": 1', '"schema_version": 2').encode(), 0),
    (json.dumps(envelope()).encode(), 4), (b'{"schema_version":1,"schema_version":1}', 0)])
def test_native_transport_rejects_malformed_output(raw, code):
    native = NativeCLI(sys.executable, runner=lambda *a, **k: (code, raw))
    with pytest.raises(SetupError) as error:
        native.call(['capabilities'])
    assert error.value.code == 'cli_protocol' and 'SECRET' not in str(error.value)


def test_real_private_subprocess_channel_bounds_and_sanitizes(tmp_path):
    script = tmp_path / 'fake.py'
    script.write_text('import sys\nvalue=sys.stdin.read()\nsys.stderr.write(value)\nprint(len(value))\n')
    status, output = private_process([sys.executable, str(script)], 'private-value')
    assert status == 0 and output == b'14\n'
    script.write_text('print("x" * 100000)\n')
    with pytest.raises(SetupError, match='output limit'):
        private_process([sys.executable, str(script)], limit=1000)
    script.write_text('import time\ntime.sleep(30)\n')
    with pytest.raises(SetupError) as error:
        private_process([sys.executable, str(script)], timeout=.1)
    assert error.value.code == 'cli_timeout'


async def test_native_error_text_is_never_journaled_or_returned(isolated_home):
    native = NativeFixture(isolated_home)
    original = native.run
    def failure(args, *a, **k):
        if args[1] == 'inspect':
            return 1, json.dumps(envelope(native.info(), state='incompatible', code='service_upgrade_required')).encode()
        return original(args, *a, **k)
    native.run = failure
    result = await wizard(isolated_home, native).run(native_choices())
    assert result['error']['type'] == 'cli_service_upgrade_required'
    assert 'PRIVATE-' not in json.dumps(result) + (isolated_home / 'anytype/setup.json').read_text()
    assert not native.mutations()


async def test_native_login_input_only_first_attempt_and_explicit_file_backend(isolated_home):
    native = NativeFixture(isolated_home, account='')
    native.failure = lambda args: args[:2] == ['auth', 'login']
    supplied = native_choices(account={'action': 'login', 'key_ref': {'env': 'RECOVERY'}},
                              cli={'credential_backend': 'private_file'})
    first = wizard(isolated_home, native, secret_reader=lambda ref: 'account-recovery-secret')
    assert (await first.run(supplied))['state'] == 'unavailable'
    second = wizard(isolated_home, native, secret_reader=lambda ref: pytest.fail('read recovery key twice'))
    result = await second.run({}, resume=True)
    assert result['state'] == 'restart_required', result
    logins = [(argv, secret, env) for argv, secret, env in native.calls if argv[1:3] == ['auth', 'login']]
    assert logins[0][1] == 'account-recovery-secret' and logins[1][1] is None
    assert '--key-stdin' in logins[0][0] and '--key-stdin' not in logins[1][0]
    assert all(env['ANYTYPE_CLI_CREDENTIAL_BACKEND'] == 'private_file' for _, _, env in logins)
    assert 'account-recovery-secret' not in (isolated_home / 'anytype/setup.json').read_text()
    assert 'account-recovery-secret' not in (isolated_home / '.env').read_text()


async def test_native_existing_hermes_identity_checked_before_service_mutation(isolated_home):
    from test_setup_diagnostics import extra
    (isolated_home / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {
        'enabled': True, 'extra': extra()}}}}))
    native = NativeFixture(isolated_home, account='wrong-account')
    result = await wizard(isolated_home, native).run(native_choices(action='repair', service={'action': 'install'},
                                                                 ack_origin_change=True))
    assert result['error']['type'] == 'account_mismatch', result
    assert not native.mutations()


async def test_native_binding_change_and_grant_change_cannot_mint_replacement(isolated_home):
    native = NativeFixture(isolated_home)
    native.failure = lambda args: args[:3] == ['auth', 'apikey', 'create']
    first = wizard(isolated_home, native)
    await first.run(native_choices())
    count = len(native.mutations())
    changed = await wizard(isolated_home, native).run({'scope': {'mode': 'single', 'space_id': 'space.full'},
        'api_grant': {'all_spaces': False, 'space_ids': ['space.full'], 'permission': 'readwrite'}}, resume=True)
    assert changed['error']['type'] == 'native_operation_conflict', changed
    assert len(native.mutations()) == count
    native.home = isolated_home / 'changed-instance'
    result = await wizard(isolated_home, native).run({}, resume=True)
    assert result['error']['type'] == 'cli_binding', result
    assert len(native.mutations()) == count


async def test_native_http_mcp_prefills_reported_url_with_generated_key(isolated_home):
    native = NativeFixture(isolated_home)
    mcp = HeartFullMCP()
    result = await wizard(isolated_home, native, mcp_transport=mcp.transport).run(native_choices(mcp={'mode': 'http'}))
    assert result['state'] == 'restart_required', result
    record = ProfileStore(isolated_home).load()
    assert record['candidate']['mcp']['url'] == native.origin + '/mcp/full'
    assert record['candidate']['extra']['api_key_env'] in record['candidate']['mcp']['headers']['Authorization']
    assert result['mcp']['state'] == 'ready'


async def test_later_native_join_expands_named_grant_and_preserves_provenance(isolated_home):
    import httpx
    native = NativeFixture(isolated_home)
    class NativeGrantAPI(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            token = request.headers.get('Authorization', '').removeprefix('Bearer ')
            receipt = next((r['result'] for r in native.receipts.values() if r['result'].get('api_key') == token), None)
            if receipt and request.url.path == '/v2/auth/whoami':
                grant = receipt['grant']
                return httpx.Response(200, json={'key': {'id': receipt['credential_id'], 'name': 'Hermes'},
                    'grant': {'all_spaces': False, 'permission': 'readwrite', 'spaces': [{'id': s} for s in grant['space_ids']]}})
            if receipt and request.url.path == '/v2/spaces':
                return httpx.Response(200, json={'data': [{'id': s, 'name': 'Space'} for s in receipt['grant']['space_ids']]})
            return response
    api = NativeGrantAPI()
    lifecycle = FakeLifecycle(running=(False,))
    w = wizard(isolated_home, native, api, lifecycle=lifecycle)
    initial = await w.run(native_choices(scope={'mode': 'single', 'space_id': 'space.full'}, activation='save_stopped'))
    assert initial['state'] == 'saved', initial
    native.members['other.full'] = 'joined'
    result = await w.join({'joined_space_id': 'other.full', 'discovery_action': 'switch'})
    assert result['state'] == 'restart_required', result
    record = w.store.load()
    assert record['mode'] == 'cli' and record['join_followup'] == 'other.full'
    assert record['candidate']['extra']['discovery']['space_id'] == 'other.full'
    assert record['native']['grant']['space_ids'] == ['other.full', 'space.full']
    assert record['choices']['account'] == {'action': 'reuse'}
    assert 'api_key_ref' not in record['choices']


def test_reviewed_cli_read_only_machine_contract():
    binary = Path(__file__).resolve().parents[1] / '.runtime/setup-implementation-20261009/anytype-cli/dist/anytype'
    if not binary.exists():
        pytest.skip('reviewed CLI build is an optional local contract smoke artifact')
    native = NativeCLI(str(binary))
    result = native.ready(native.call(['capabilities']))
    assert result['instance_selection'] == ['default']
    assert result['operation_recovery'] == CAPABILITIES['operation_recovery']
    assert result['embedded_http_mcp']['token_source'] == 'same_api_key'
    # Machine help and parse failure return before CLI engine/config creation.
    assert native.call(['auth', 'create', '--help'])['state'] == 'ready'
    invalid = native.call(['capabilities', '--not-a-native-option'])
    assert invalid['state'] == 'needs_input'


async def test_later_join_pending_resume_and_completed_retain_start_distinct_operations(isolated_home):
    native = NativeFixture(isolated_home)
    api = FakeAPI(spaces=('space.full', 'other.full'))
    w = wizard(isolated_home, native, api, lifecycle=FakeLifecycle(running=(False,)),
               secret_reader=lambda ref: 'private-first-invitation')
    first = await w.run(native_choices(scope={'mode': 'single', 'space_id': 'space.full', 'grant': 'all'}, activation='save_stopped'))
    assert first['state'] == 'saved', first
    native.join_state = 'pending_approval'
    pending = await w.join({'invitation_ref': {'env': 'FIRST'}, 'discovery_action': 'retain'})
    assert pending['state'] == 'pending_approval', pending
    id1 = w.store.load()['join_attempt']['operations']['native_join']['id']
    native.join_state = 'joined'
    fresh = wizard(isolated_home, native, api, secret_reader=lambda ref: pytest.fail('resume reread invitation'))
    ready = await fresh.join({}, resume=True)
    assert ready['state'] == 'ready' and 'outside gateway' in ready['gateway'], ready
    assert 'join_attempt' not in fresh.store.load()
    again = await w.join({'invitation_ref': {'env': 'SECOND'}, 'discovery_action': 'retain'})
    assert again['state'] == 'ready', again
    ids = [a[a.index('--operation-id')+1] for a, _, _ in native.calls if a[1:3] == ['space', 'join']]
    assert ids[:2] == [id1, id1] and ids[2] != id1


async def test_native_join_requires_valid_discovery_choice_before_invitation(isolated_home):
    native = NativeFixture(isolated_home)
    w = wizard(isolated_home, native, lifecycle=FakeLifecycle(running=(False,)))
    await w.run(native_choices(activation='save_stopped'))
    before = len(native.mutations())
    result = await w.join({'invitation_ref': {'env': 'INVITE'}, 'discovery_action': 'bogus'})
    assert result['error']['type'] == 'discovery_choice', result
    assert len(native.mutations()) == before


async def test_native_transport_real_subprocess_end_to_end_private_key(isolated_home, tmp_path):
    native = NativeFixture(isolated_home)
    script = tmp_path / 'fake-anytype'
    # Static native responses live only in this private test directory. The real
    # transport still runs every command, parses JSON and receives a pipe secret.
    script.write_text('#!' + sys.executable + '\n' + '''import json,sys
args = sys.argv[1:]
responses = json.loads(RESPONSES)
if args[0] == 'capabilities':
    out = responses['capabilities']
elif args[0] == 'inspect':
    out = responses['inspect']
elif args[:2] == ['space','list']:
    out = responses['list']
elif args[:2] == ['space','status']:
    out = responses['status']
else:
    out = responses['key']
    out['result']['operation_id'] = args[args.index('--operation-id')+1]
sys.stderr.write('PRIVATE-STDERR-MUST-NOT-ESCAPE')
print(json.dumps(out))
'''.replace('RESPONSES', repr(json.dumps({
        'capabilities': envelope(CAPABILITIES), 'inspect': envelope(native.info()),
        'list': envelope({'account_id': 'bot-key', 'spaces': [{'id': 'space.full', 'state': 'joined'}]}),
        'status': envelope({'id': 'space.full', 'state': 'joined'}),
        'key': envelope({'account_id': 'bot-key', 'application_name': 'Hermes', 'credential_id': 'key-id',
                         'api_key': 'real-pipe-api-secret', 'grant_verified': True,
                         'grant': {'all_spaces': True, 'space_ids': [], 'permission': 'read_write'}})}))))
    script.chmod(0o700)
    api = FakeAPI()
    w = Wizard(isolated_home, interactive=False, api_transport=api.transport)
    result = await w.run(native_choices(cli={'executable': str(script)}))
    assert result['state'] == 'restart_required', result
    assert all(headers['authorization'] == 'Bearer real-pipe-api-secret' for _, _, headers in api.requests)
    assert 'real-pipe-api-secret' not in json.dumps(w.store.load()) + json.dumps(result)
    assert 'PRIVATE-STDERR' not in json.dumps(result)


async def test_native_interactive_prompts_use_private_inputs_and_generated_api_key(isolated_home, monkeypatch):
    from anytype_plugin import onboarding
    native = NativeFixture(isolated_home, account='', running=False)
    chosen = {'Native service': 'install', 'Native account': 'create', 'Native network': 'default',
              'Join a space by private invitation': 'yes', 'Gateway discovery (all spaces accessible to the API grant)': 'single',
              'Sender access': 'operator', 'Home chat': 'skip', 'Anytype MCP tools': 'none',
              'Publication (defer keeps live config unchanged)': 'defer'}
    private = []
    def answer(label, default=None, **kwargs):
        if kwargs.get('private'):
            private.append(label)
            assert label == 'Space invitation'
            return 'interactive-invitation'
        return {'Agent display name': 'Агент 🌿', 'Operator account identity': 'alice-key'}[label]
    monkeypatch.setattr(onboarding, 'choose', lambda label, options, default=None: chosen[label])
    monkeypatch.setattr(onboarding, 'prompt', answer)
    result = await Wizard(isolated_home, interactive=True, native_factory=native.factory,
        api_transport=FakeAPI(spaces=('other.full',), name='Агент 🌿', all_spaces=False).transport).run({'mode': 'cli', 'application_name': 'Hermes'})
    assert result['state'] == 'restart_required', result
    assert private == ['Space invitation']


async def test_native_all_space_join_reuses_exact_existing_credential(isolated_home):
    native = NativeFixture(isolated_home)
    api = FakeAPI(spaces=('space.full', 'other.full'))
    w = wizard(isolated_home, native, api, lifecycle=FakeLifecycle(running=(False,)))
    assert (await w.run(native_choices(activation='save_stopped')))['state'] == 'saved'
    native.members['other.full'] = 'joined'
    result = await w.join({'joined_space_id': 'other.full', 'discovery_action': 'all'})
    assert result['state'] == 'restart_required', result
    assert native.key_count == 1
    record = w.store.load()
    assert record['native_reuse_key'] is True
    assert 'native_key' not in record['operations']
    assert record['native']['credential_id'] == 'key-id'
