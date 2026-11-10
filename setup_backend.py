"""Native CLI capability boundary; no parsing of private human auth output."""
from copy import deepcopy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata
import tempfile
import selectors
import time

from .setup_store import SetupError, account_evidence, safe_url


def agent_name(value, limits=None):
    if not isinstance(value, str):
        raise SetupError('agent_name', 'Supply an explicit agent_name when creating an account.')
    value = value.strip()
    if not value or any(unicodedata.category(char) in {'Cc', 'Cs'} for char in value):
        raise SetupError('agent_name', 'Agent name must be nonempty and contain no control characters.')
    limits = limits or {}
    if limits.get('max_characters') and len(value) > limits['max_characters']:
        raise SetupError('agent_name', 'The agent name exceeds the backend’s reported character limit.')
    if limits.get('max_bytes') and len(value.encode('utf-8')) > limits['max_bytes']:
        raise SetupError('agent_name', 'The agent name exceeds the backend’s reported UTF-8 limit.')
    return value


def application_name(value, max_bytes=128):
    """Application ownership uses the exact name bytes, including outer spaces."""
    try:
        if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > max_bytes:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise SetupError('application_name', 'Use a nonblank UTF-8 application name within the CLI byte limit.') from None
    return value


# Never copy native error messages/next_action into Hermes output. Even a broken
# or older executable can echo the private input there.
ERROR_ACTIONS = {
    'operation_uncertain': 'Resume the same setup; inspect the native operation receipt before taking further action.',
    'operation_conflict': 'Restore the original choices for this setup operation; do not create a replacement operation.',
    'private_input_required': 'Supply the original private account key or invitation reference, then resume.',
    'service_upgrade_required': 'Explicitly upgrade/restart the selected native service, then resume; Hermes never restarts it automatically.',
    'authentication_required': 'Choose account.action login with the expected account and a private account key reference.',
    'account_transition': 'Resume the interrupted native account operation or inspect it with the owning CLI.',
    'account_mismatch': 'The native account differs from this profile; preserve both and inspect the selected instance.',
    'instance_mismatch': 'Inspect the default native instance binding before resuming.',
    'settings_conflict': 'Preserve the existing native network and listener settings.',
    'instance_busy': 'Wait for the other native operation, then resume this setup.',
    'rpc_unavailable': 'Start the selected native service explicitly, then resume this setup.',
    'rpc_timeout': 'Resume this setup with its existing operation IDs after checking the native service.',
    'membership_rejected': 'Ask the space operator to inspect the rejected membership.',
    'membership_removed': 'Ask the space operator to inspect the removed membership.',
    'membership_unknown': 'Inspect native membership before submitting another invitation.',
}


def native_error(envelope):
    code = (envelope.get('error') or {}).get('code')
    known = code if code in ERROR_ACTIONS else 'failure'
    state = envelope['state'] if envelope['state'] != 'ready' else 'needs_input'
    if state == 'request_sent':
        state = 'pending_approval'
    return SetupError('cli_' + known, 'The native CLI could not complete the requested operation.',
                      state=state, next_action=ERROR_ACTIONS.get(code,
                      'Inspect the selected CLI with its native status commands, then resume the same setup.'))


def private_process(argv, secret=None, *, env=None, timeout=50, limit=1024 * 1024):
    """Bounded stdout pipe, discarded stderr, private stdin; never inherit a TTY."""
    data = bytearray()
    with tempfile.TemporaryFile() as source:
        if secret is not None:
            if not isinstance(secret, str) or not secret or len(secret.encode('utf-8')) > 65536:
                raise SetupError('private_input', 'Private native input must be nonempty and at most 64 KiB.')
            source.write(secret.encode('utf-8') + b'\n')
            source.seek(0)
        try:
            process = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=env, close_fds=True)
        except OSError:
            raise SetupError('cli_unavailable', 'The selected CLI could not be started.', state='unavailable') from None
        try:
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SetupError('cli_timeout', 'The native command timed out; its outcome may be uncertain.',
                                         state='unavailable', next_action='Resume this setup with its existing operation IDs.')
                    for key, _ in selector.select(min(remaining, .1)):
                        block = os.read(key.fileobj.fileno(), 8192)
                        if not block:
                            selector.unregister(key.fileobj)
                            break
                        if len(data) + len(block) > limit:
                            raise SetupError('cli_protocol', 'The native CLI exceeded the machine output limit.', state='incompatible')
                        data.extend(block)
                try:
                    process.wait(timeout=max(.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise SetupError('cli_timeout', 'The native command timed out; its outcome may be uncertain.',
                                     state='unavailable', next_action='Resume this setup with its existing operation IDs.') from None
            return process.returncode, bytes(data)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()


def listener_identity(value):
    try:
        host, port = value.rsplit(':', 1)
        host = str(ipaddress.ip_address(host.strip('[]')))
        return ('*' if host in {'0.0.0.0', '::'} else host, int(port))
    except (ValueError, AttributeError):
        raise SetupError('native_listener', 'Use a numeric IP:port listener address.') from None


class NativeCLI:
    """Schema-1 native operations. Only sanitized facts enter the setup journal."""
    def __init__(self, executable='anytype', instance=None, *, runner=private_process):
        self.executable, self.instance, self.runner = executable, instance or 'default', runner
        self.private = {}
        self.backend = None
        self.path = None

    def call(self, args, *, secret=None, expected=None, positional=()):
        if self.instance != 'default':
            raise SetupError('cli_instance', 'Only the default native instance is supported; Hermes profiles do not isolate it.', state='incompatible')
        if not self.path:
            self.path = shutil.which(self.executable)
            if not self.path:
                raise SetupError('cli_missing', 'Anytype CLI is not installed or not on PATH.', state='unavailable',
                                 next_action='Install a reviewed compatible CLI explicitly, or select desktop/external mode.')
            self.path = str(Path(self.path).resolve())
        argv = [self.path, *args, '--json', '--non-interactive', '--instance', 'default', '--timeout', '45s']
        if expected:
            argv += ['--expect-account', expected]
        if positional:
            argv += ['--', *positional]
        env = dict(os.environ)
        # A profile choice, not an ambient shell override, selects headless storage.
        env.pop('ANYTYPE_CLI_CREDENTIAL_BACKEND', None)
        if self.backend:
            env['ANYTYPE_CLI_CREDENTIAL_BACKEND'] = self.backend
        code, raw = self.runner(argv, secret, env=env)
        try:
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError()
                    obj[key] = value
                return obj
            envelope = json.loads(raw, object_pairs_hook=unique)
            if (not isinstance(envelope, dict) or type(envelope.get('schema_version')) is not int
                    or envelope['schema_version'] != 1 or type(envelope.get('changed')) is not bool
                    or envelope.get('state') not in {'ready', 'needs_input', 'unavailable', 'incompatible',
                                                     'request_sent', 'pending_approval', 'syncing'}
                    or not isinstance(envelope.get('next_action'), str)
                    or not isinstance(envelope.get('result'), (dict, type(None)))):
                raise ValueError()
            error = envelope.get('error')
            if error is not None and (not isinstance(error, dict) or not isinstance(error.get('code'), str)
                                      or not isinstance(error.get('message'), str) or type(error.get('retryable')) is not bool):
                raise ValueError()
            expected_code = {'ready': 0, 'needs_input': 2, 'unavailable': 4, 'incompatible': 1,
                             'request_sent': 3, 'pending_approval': 3, 'syncing': 3}[envelope['state']]
            if code != expected_code or (envelope['state'] == 'ready' and error) or (code in {1, 2, 4} and not error):
                raise ValueError()
            return envelope
        except (ValueError, TypeError, UnicodeError):
            raise SetupError('cli_protocol', 'The selected CLI returned an unsupported machine response.', state='incompatible') from None

    @staticmethod
    def ready(envelope):
        if envelope['state'] != 'ready':
            raise native_error(envelope)
        return envelope['result'] or {}

    def inspect(self):
        capabilities = self.ready(self.call(['capabilities']))
        if (capabilities.get('instance_selection') != ['default'] or capabilities.get('isolated_instances') is not False
                or any(capabilities.get(key) is not True for key in ('account_create', 'account_login', 'space_join', 'scoped_api_keys'))
                or capabilities.get('operation_recovery') != 'durable_checkpoints_with_explicit_uncertainty'
                or capabilities.get('embedded_http_api', {}).get('whoami') != '/v2/auth/whoami'):
            raise SetupError('cli_capabilities', 'The selected CLI lacks the reviewed automation capabilities.', state='incompatible')
        self.capabilities = capabilities
        return self.call(['inspect'])

    def bind(self, envelope, record, store, *, expected=None, recovering=False):
        value = envelope.get('result') or {}
        instance = value.get('instance', {})
        fields = ('config_path', 'data_path', 'work_path', 'grpc_address', 'grpc_web_address')
        if (instance.get('id') != 'default' or instance.get('isolated') is not False
                or any(not isinstance(instance.get(k), str) or not instance[k] for k in fields)
                or any(not Path(instance[k]).is_absolute() for k in fields[:3])):
            raise SetupError('cli_binding', 'The CLI did not report a complete default-instance binding.', state='incompatible')
        binding = {'executable': self.path, 'id': 'default', **{k: instance[k] for k in fields}}
        native = record.setdefault('native', deepcopy(record.get('prior_connection', {}).get('native', {})))
        if native.get('binding') and native['binding'] != binding:
            raise SetupError('cli_binding', 'The native executable or instance paths changed; inspect before resuming.')
        account = value.get('account', {})
        identity = account.get('id')
        prior = native.get('account_id') or account_evidence(record).expected_identity
        if (identity and expected and expected != 'none' and identity != expected) or (identity and prior and identity != prior):
            raise SetupError('account_mismatch', 'The selected native account differs from this profile; account migration is separate.')
        network = value.get('network', {})
        for key in ('id', 'config_path'):
            missing_live_id = recovering and key == 'id' and not network.get(key)
            if native.get('network', {}).get(key) and network.get(key) != native['network'][key] and not missing_live_id:
                raise SetupError('native_network', 'The selected native network changed; preserve the original binding.')
        api, service = value.get('api', {}), value.get('service', {})
        listener = api.get('listen_address') or service.get('listen_address')
        if listener and native.get('listen_address') and listener_identity(listener) != listener_identity(native['listen_address']):
            raise SetupError('native_listener', 'The native listener changed; preserve the original binding.')
        native['binding'] = binding
        if network.get('id'):
            native['network'] = {k: network.get(k, '') for k in ('id', 'config_path')}
        if listener:
            native['listen_address'] = listener
        if identity:
            native['account_id'] = identity
        store.save(record)
        return value

    def operation(self, record, store, name, args, *, expected=None, secret=None, input_flag=None):
        intent = {'args': args, 'expected': expected, 'backend': self.backend}
        operations = record.setdefault('operations', {})
        key = 'native_' + name
        operation = operations.get(key)
        if operation and operation['intent'] != intent:
            raise SetupError('native_operation_conflict', 'Native operation choices changed; restore the original intent before resuming.')
        if not operation:
            operation = {'id': record['id'] + '-' + name, 'intent': intent, 'state': 'prepared'}
            operations[key] = operation
            store.save(record)
        operation['state'] = 'unconfirmed'
        store.save(record)
        # Keep the saved logical intent stable for old receipts. Only the wire
        # representation moves names after --; every flag precedes that delimiter.
        wire_args = list(args)
        positional = []
        if args[:2] == ['auth', 'create']:
            positional = [wire_args.pop(2)]
        elif args[:3] == ['auth', 'apikey', 'create']:
            positional = [wire_args.pop(3)]
        envelope = self.call([*wire_args, '--operation-id', operation['id'], *([input_flag] if secret is not None and input_flag else [])],
                             secret=secret, expected=expected, positional=positional)
        if envelope['changed']:
            record['changed'] = True
        result = envelope.get('result') or {}
        if result.get('operation_id') not in {None, operation['id']}:
            raise SetupError('cli_receipt', 'The native operation returned a different receipt.', state='incompatible')
        operation['state'] = envelope['state']
        store.save(record)
        return envelope

    def secret(self, kind, reference, reader):
        if kind in self.private:
            return self.private[kind]
        if reference:
            return reader(reference)
        raise SetupError('missing_secret_reference', 'Supply the private ' + kind + ' reference to continue.')

    def prepare(self, choices, record, store, read_secret):
        service = choices.get('service', {})
        account = choices.get('account', {})
        network = choices.get('network', {'mode': 'preserve'})
        if service.get('action') not in {'reuse', 'install', 'start'}:
            raise SetupError('native_service_choice', 'Choose service.action reuse, install, or start; install also starts the matching service.')
        if account.get('action') not in {'reuse', 'login', 'create'}:
            raise SetupError('native_account_choice', 'Choose account.action reuse, login, or create.')
        if network.get('mode') not in {'preserve', 'default', 'custom'}:
            raise SetupError('native_network', 'Choose network.mode preserve, default, or custom.')
        if network.get('mode') == 'custom':
            path = Path(network.get('config_file', '')).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise SetupError('native_network', 'Supply an absolute readable network.config_file for custom mode.')
            network = {**network, 'config_file': str(path.resolve())}
            choices['network'] = network
        elif network.get('config_file'):
            raise SetupError('native_network', 'A network config file requires custom mode.')
        mcp_choice = choices.get('mcp', {})
        if mcp_choice.get('mode') == 'http' and (mcp_choice.get('shared_api_key', True) is not True or mcp_choice.get('token_ref')):
            raise SetupError('native_mcp', 'Native HTTP MCP uses the same integration key and grant as the API.')
        listener = service.get('listen_address')
        if listener:
            _, port = listener_identity(listener)
            if not 1 <= port <= 65535:
                raise SetupError('native_listener', 'Use a listener port from 1 through 65535.')
        backend = choices.get('cli', {}).get('credential_backend')
        if backend not in {None, 'private_file'}:
            raise SetupError('native_credentials', 'The only explicit credential backend is private_file.')
        self.backend = backend
        envelope = self.inspect()
        if backend and self.capabilities.get('credential_backend_selection', {}).get('values') != ['private_file']:
            raise SetupError('cli_capabilities', 'The CLI does not advertise explicit private-file credential storage.', state='incompatible')
        limits = self.capabilities.get('limits', {})
        if 'agent_name' in choices:
            choices['agent_name'] = agent_name(choices['agent_name'], {'max_bytes': limits.get('display_name_max_bytes', 4096)})
        action = account['action']
        if action == 'create' and not choices.get('agent_name'):
            raise SetupError('agent_name', 'Supply the chosen agent_name before creating the native account.')
        evidence = account_evidence(record)
        expected = evidence.expected_identity or choices.get('expected_account')
        if evidence.existing and not expected:
            raise SetupError('account_unverified', 'Supply the existing profile’s expected_account before native provisioning.')
        op = record.get('operations', {}).get('native_account')
        code = (envelope.get('error') or {}).get('code')
        # WalletRecover temporarily clears live identity/network/name. Only an
        # owned durable account operation can bridge that missing observation.
        recovering = bool(op and action in {'create', 'login'} and
                          op.get('intent', {}).get('args', [])[:2] == ['auth', action] and
                          record.get('native', {}).get('binding') and
                          code in {'account_transition', 'authentication_required', 'rpc_unavailable'})
        info = self.bind(envelope, record, store, expected=expected, recovering=recovering)
        identity = info.get('account', {}).get('id')
        if action == 'create' and (evidence.existing or (identity and not op)):
            raise SetupError('account_mismatch', 'Account creation requires an empty native instance and a new Hermes connection.')
        if expected and identity and identity != expected:
            raise SetupError('account_mismatch', 'The selected native account differs from the expected account.')
        observed_name = info.get('account', {}).get('display_name')
        name_pending_login = action == 'login' and not observed_name and code in {'authentication_required', 'rpc_unavailable', 'account_transition'}
        if choices.get('agent_name') and action != 'create' and not name_pending_login and observed_name != choices['agent_name']:
            raise SetupError('rename_unsupported', 'Reuse/login preserves the native name; account rename is unsupported.', state='incompatible')
        reported_mcp_url = info.get('mcp', {}).get('url')
        if mcp_choice.get('mode') == 'http' and mcp_choice.get('url') and reported_mcp_url and mcp_choice['url'] != reported_mcp_url:
            raise SetupError('native_mcp', 'The requested MCP URL differs from the native-reported endpoint.')
        current_network = info.get('network', {})
        if network['mode'] == 'default' and current_network.get('config_path'):
            raise SetupError('native_network', 'Default network cannot replace a configured custom network.')
        if network['mode'] == 'custom':
            if identity and current_network.get('config_path') != network['config_file']:
                raise SetupError('native_network', 'Custom network must match the existing native account configuration.')
            try:
                checksum = hashlib.sha256(Path(network['config_file']).read_bytes()).hexdigest()
            except OSError:
                raise SetupError('native_network', 'The selected network configuration cannot be read.') from None
            old = record['native'].get('network_file_sha256')
            if old and checksum != old:
                raise SetupError('native_network', 'The selected network file changed during setup.')
            record['native']['network_file_sha256'] = checksum
        known_network_id = current_network.get('id') or (record['native'].get('network', {}).get('id') if recovering else None)
        if network.get('expected_id') and known_network_id != network['expected_id']:
            raise SetupError('native_network', 'The native network differs from network.expected_id.')
        current_listener = record['native'].get('listen_address')
        if listener and current_listener and listener_identity(listener) != listener_identity(current_listener):
            raise SetupError('native_listener', 'The requested listener differs from the existing native service/account.')
        observed_origin = info.get('api', {}).get('origin') or info.get('api', {}).get('configured_origin')
        if choices.get('base_url') and observed_origin and choices['base_url'] != observed_origin:
            raise SetupError('native_origin', 'The requested API origin differs from the selected native listener.')
        old_origin = record['prior_owned']['extra'].get('base_url', 'http://127.0.0.1:31009')
        if evidence.existing and observed_origin and observed_origin != old_origin and not choices.get('ack_origin_change'):
            raise SetupError('origin_migration', 'Changing API origin requires explicit ack_origin_change before native provisioning.')
        prior_name = record.get('prior_connection', {}).get('credential', {}).get('name')
        name = choices.get('application_name') or prior_name
        if evidence.existing and not name:
            raise SetupError('application_name_unknown', 'Supply the existing stable application_name before native key creation.')
        name = name or ('Hermes-' + hashlib.sha256(str(store.home).encode()).hexdigest()[:16])
        name = application_name(name, limits.get('application_name_max_bytes', 128))
        if prior_name and name != prior_name:
            raise SetupError('application_name_mismatch', 'Retain the existing integration application name.')
        choices['application_name'] = name
        record['choices'] = deepcopy(choices)
        store.save(record)
        login_secret = None
        if action == 'login' and not op:
            login_secret = self.secret('account_key', account.get('key_ref'), read_secret)
        # Only unavailable transport or missing login can be remedied by these
        # explicitly selected steps. Incompatible or mismatched service errors stop.
        code = (envelope.get('error') or {}).get('code')
        allowed = {'rpc_unavailable', 'authentication_required'}
        if op:
            allowed.add('account_transition')
        if envelope['state'] != 'ready' and code not in allowed:
            raise native_error(envelope)
        if service['action'] != 'reuse':
            args = ['service', 'install' if service['action'] == 'install' else 'start']
            if listener:
                args += ['--listen-address', listener]
            service_expected = record['operations'].get('native_service', {}).get('intent', {}).get('expected', expected)
            self.ready(self.operation(record, store, 'service', args, expected=service_expected))
            if service['action'] == 'install':
                self.ready(self.operation(record, store, 'start', ['service', 'start'] +
                                         (['--listen-address', listener] if listener else []), expected=record['operations'].get('native_start', {}).get('intent', {}).get('expected', expected)))
        elif code == 'rpc_unavailable':
            raise native_error(envelope)
        if action in {'create', 'login'}:
            args = ['auth', action]
            if action == 'create':
                args += [choices['agent_name']]
            if network['mode'] == 'custom':
                args += ['--network-config', network['config_file']]
            if listener:
                args += ['--listen-address', listener]
            account_expected = op['intent']['expected'] if op else (expected or identity or 'none')
            secret = None
            if action == 'login' and not op:
                secret = login_secret
            response = self.account_operation(record, store, args, account_expected, secret)
            if (response.get('error') or {}).get('code') == 'private_input_required' and action == 'login' and secret is None:
                secret = self.secret('account_key', account.get('key_ref'), read_secret)
                response = self.account_operation(record, store, args, account_expected, secret)
            result = self.ready(response)
            created = result.get('account', {})
            if not created.get('id') or (expected and created['id'] != expected):
                raise SetupError('cli_account', 'The native account receipt did not match the expected identity.', state='incompatible')
            record['native']['account_id'] = created['id']
            store.save(record)
        envelope = self.call(['inspect'])
        info = self.bind(envelope, record, store, expected=expected)
        self.ready(envelope)
        account = info.get('account', {})
        if not account.get('id') or account.get('authenticated') is not True or account.get('observation') != 'live':
            raise SetupError('cli_authentication_required', 'Reuse requires a verified authenticated native account; choose login explicitly.')
        expected = account['id']
        record['account_identity'] = expected
        if choices.get('agent_name') and account.get('display_name') != choices['agent_name']:
            raise SetupError('rename_unsupported', 'The authenticated native account name does not match the requested name; rename is unsupported.', state='incompatible')
        if network.get('expected_id') and info.get('network', {}).get('id') != network['expected_id']:
            raise SetupError('native_network', 'The loaded account does not match network.expected_id.')
        api = info.get('api', {})
        if api.get('bound') is not True or api.get('observation') != 'live' or not api.get('origin'):
            raise SetupError('native_api_unverified', 'The native API binding is not proven yet.', state='syncing',
                             next_action='Inspect native status; resume the account operation or explicitly restart/rebind the native service if advised.')
        base = safe_url(api['origin'], origin=True)
        if choices.get('base_url') and choices['base_url'] != base:
            raise SetupError('native_origin', 'The selected API origin differs from the live native listener.')
        choices['base_url'] = base
        mcp = info.get('mcp', {})
        if (mcp.get('authentication') == 'bearer' and mcp.get('token_source') == 'same_api_key'
                and mcp.get('same_grant') is True and mcp.get('url')):
            record['native']['mcp'] = {'url': safe_url(mcp['url']), 'shared_api_key': True}
        if mcp_choice.get('mode') == 'http' and (not mcp.get('url') or mcp_choice.get('url', mcp['url']) != mcp['url']):
            raise SetupError('native_mcp', 'The CLI did not report the requested native HTTP MCP endpoint.', state='incompatible')
        if choices.get('invitation_ref') or choices.get('join_space') or record.get('operations', {}).get('native_join'):
            joined = self.join(choices, record, store, read_secret, inspected=True)
            if choices['scope']['mode'] == 'single' and not choices['scope'].get('space_id'):
                choices['scope']['space_id'] = joined['space_id']
        from .onboarding import requested_grant
        grant = requested_grant(choices, choices['scope'])
        choices['api_grant'] = grant
        spaces = self.ready(self.call(['space', 'list'], expected=expected))
        if spaces.get('account_id') != expected or not isinstance(spaces.get('spaces'), list):
            raise SetupError('cli_membership', 'The native space list did not match the selected account.', state='incompatible')
        selected = [s.get('id') for s in spaces['spaces'] if s.get('state') == 'joined'] if grant['all_spaces'] else grant['space_ids']
        if not selected:
            raise SetupError('native_membership', 'No usable native space membership is available yet.', state='syncing',
                             next_action='Join a space or wait for approval/sync, then resume setup.')
        for space in selected:
            self.membership(self.call(['space', 'status', space], expected=expected), space)
        if record.get('native_reuse_key'):
            listing = self.ready(self.call(['auth', 'apikey', 'list'], expected=expected))
            credential_id = record['native'].get('credential_id')
            matches = [row for row in listing.get('credentials', []) if row.get('credential_id') == credential_id]
            expected_grant = {**grant, 'permission': 'read_write'}
            if (listing.get('account_id') != expected or len(matches) != 1 or
                    matches[0].get('application_name') != name or
                    {**(matches[0].get('grant') or {}), 'space_ids': sorted((matches[0].get('grant') or {}).get('space_ids') or [])} != expected_grant):
                raise SetupError('cli_key_receipt', 'The existing native credential no longer matches its exact recorded identity and grant.')
            record['choices'] = deepcopy(choices)
            store.save(record)
            return {'choices': choices, 'reuse_api_key': True}
        args = ['auth', 'apikey', 'create', name, '--read-write', '--include-secret']
        if grant['all_spaces']:
            args += ['--all-spaces']
        else:
            for space in grant['space_ids']:
                args += ['--space', space]
        result = self.ready(self.operation(record, store, 'key', args, expected=expected))
        native_grant = {'all_spaces': grant['all_spaces'], 'space_ids': grant['space_ids'], 'permission': 'read_write'}
        actual = result.get('grant') or {}
        actual = {**actual, 'space_ids': sorted(actual.get('space_ids') or [])}
        if (result.get('account_id') != expected or result.get('application_name') != name
                or result.get('grant_verified') is not True or actual != native_grant
                or not result.get('credential_id') or not isinstance(result.get('api_key'), str) or not result['api_key']):
            raise SetupError('cli_key_receipt', 'The native key receipt did not prove the exact account, application and grant.', state='incompatible')
        key_operation = record['operations']['native_key']
        prior_key = key_operation.get('credential_id')
        if prior_key and prior_key != result['credential_id']:
            raise SetupError('cli_key_receipt', 'The recovered native operation returned a different credential.', state='incompatible')
        key_operation['credential_id'] = result['credential_id']
        record['native']['credential_id'] = result['credential_id']
        record['native']['application_name'] = name
        record['native']['grant'] = grant
        record['choices'] = deepcopy(choices)
        store.save(record)
        return {'choices': choices, 'api_key': result['api_key']}

    def account_operation(self, record, store, args, expected, secret):
        # The input flag is deliberately excluded from intent so a receipt can be
        # recovered without re-reading a private key after a process interruption.
        return self.operation(record, store, 'account', args, expected=expected,
                              secret=secret, input_flag='--key-stdin')

    @staticmethod
    def membership(envelope, expected=None):
        # Errors may legitimately contain null or an operation-stage receipt,
        # not a membership object. Preserve their input/temporary/terminal state.
        if envelope.get('error') or envelope['state'] not in {'ready', 'request_sent', 'pending_approval', 'syncing'}:
            raise native_error(envelope)
        result = envelope.get('result') or {}
        space = result.get('space', result)
        if (not isinstance(space, dict) or not isinstance(space.get('id'), str) or not space['id'] or
                (expected and space['id'] != expected)):
            raise SetupError('cli_membership', 'The native membership response lacks the requested full space ID.', state='incompatible')
        if envelope['state'] in {'request_sent', 'pending_approval', 'syncing'}:
            raise SetupError('native_membership', 'Native membership is awaiting approval or local sync.',
                             state=envelope['state'],
                             next_action='Resume the same setup/join to poll membership; do not submit a new invitation.')
        if space.get('state') != 'joined':
            raise SetupError('cli_membership', 'The successful native response did not prove joined membership.', state='incompatible')
        return space

    def join(self, choices, record, store, read_secret, *, inspected=False):
        expected = record.get('account_identity') or record.get('native', {}).get('account_id')
        if not expected:
            raise SetupError('account_unverified', 'Native join requires the previously verified full account identity.')
        if not inspected:
            self.backend = record.get('choices', {}).get('cli', {}).get('credential_backend')
            envelope = self.inspect()
            info = self.bind(envelope, record, store, expected=expected)
            self.ready(envelope)
            connection_origin = record.get('prior_connection', {}).get('base_url')
            if (not connection_origin or info.get('api', {}).get('bound') is not True or
                    info.get('api', {}).get('origin') != connection_origin):
                raise SetupError('native_origin', 'The native listener does not match the currently verified profile connection.')
        operation = record.get('operations', {}).get('native_join')
        space_id = choices.get('joined_space_id')
        if space_id and not operation and not choices.get('invitation_ref') and not choices.get('join_space'):
            space = self.membership(self.call(['space', 'status', space_id], expected=expected), space_id)
        else:
            args = ['space', 'join']
            desired = (operation or {}).get('intent', {}).get('args')
            if desired:
                args = desired
            elif space_id or choices.get('scope', {}).get('space_id'):
                args += ['--space-id', space_id or choices['scope']['space_id']]
            secret = None if operation else self.secret('invitation', choices.get('invitation_ref'), read_secret)
            response = self.operation(record, store, 'join', args, expected=expected,
                                      secret=secret, input_flag='--invite-stdin')
            if (response.get('error') or {}).get('code') == 'private_input_required' and secret is None:
                response = self.operation(record, store, 'join', args, expected=expected,
                    secret=self.secret('invitation', choices.get('invitation_ref'), read_secret), input_flag='--invite-stdin')
            space = self.membership(response, space_id)
        record.setdefault('native', {})['joined_space_id'] = space['id']
        store.save(record)
        return {'state': 'joined', 'space_id': space['id']}
