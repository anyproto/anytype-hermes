"""Durable, non-secret setup journal and one-point profile publication.

The journal is never consulted by the transport. Secrets are written first under
fresh names; only the complete config replacement makes them live. A lifecycle
provider must hold an exclusive consumer boundary through that replacement.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from urllib.parse import urlsplit


class SetupError(Exception):
    def __init__(self, code, message, *, state='needs_input', next_action=None):
        super().__init__(message)
        self.code, self.state, self.next_action = code, state, next_action or message


def outcome(state, *, changed=False, next_action=None, error=None, **details):
    result = dict(schema_version=1, state=state, changed=changed, next_action=next_action)
    if error:
        result['error'] = {'type': error.code, 'message': str(error)}
    result.update(details)
    return result


def exit_code(result):
    return {'ready': 0, 'prepared': 0, 'saved': 0, 'needs_input': 2,
            'restart_required': 2, 'request_sent': 3, 'pending_approval': 3, 'syncing': 3,
            'unavailable': 4, 'incompatible': 1, 'error': 1}.get(result['state'], 1)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.setup-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


EXTRA_KEYS = ('base_url', 'api_key_env', 'discovery', 'chats', 'group_allow_from',
              'group_allow_admin_from', 'activity_status')
PLATFORM_KEYS = ('enabled', 'home_channel')
ENV_REF = re.compile(r'\$\{(ANYTYPE_[A-Z0-9_]+)\}')
INPUT_KEYS = {'schema_version', 'action', 'mode', 'base_url', 'cli', 'service', 'network',
              'account', 'agent_name', 'expected_account', 'application_name', 'scope', 'api_grant',
              'api_key_ref', 'invitation_ref', 'access', 'operator_identities', 'admin_identities',
              'home', 'mcp', 'activity_status', 'activation', 'offline_boundary', 'manage_gateway',
              'ack_origin_change', 'send_test_message', 'join_space', 'joined_space_id', 'discovery_action', 'drop_outside_pins'}


def safe_url(value, *, origin=False):
    """Reject credential-bearing URLs before they enter any journal or output."""
    if not isinstance(value, str) or any(ord(c) <= 32 for c in value):
        raise SetupError('endpoint', 'Use an HTTP(S) endpoint without credentials, query or fragment.')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or (origin and parsed.path.rstrip('/'))):
        raise SetupError('endpoint', 'Use an HTTP(S) endpoint without credentials, query or fragment.')
    return value.rstrip('/') if origin else value


def safe_reference(reference):
    if (not isinstance(reference, dict) or len(reference) != 1 or
            next(iter(reference)) not in {'file', 'env'} or
            not isinstance(next(iter(reference.values())), str)):
        raise SetupError('secret_reference', 'Secrets must use a private file or environment reference.')
    if 'file' in reference and not Path(reference['file']).is_absolute():
        raise SetupError('secret_reference', 'Private secret file references must be absolute paths.')
    if 'env' in reference and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', reference['env']):
        raise SetupError('secret_reference', 'Use a valid environment secret reference.')
    return dict(reference)


def safe_choices(value):
    """Whitelist the non-secret input grammar, also for interactive/resume writes."""
    if not isinstance(value, dict) or set(value) - INPUT_KEYS or value.get('schema_version', 1) != 1:
        raise SetupError('input_schema', 'Use schema_version 1 and the fields documented in docs/SETUP.md.')
    fields = {'cli': {'executable', 'instance', 'credential_backend'}, 'service': {'action', 'listen_address'},
              'network': {'mode', 'config_file', 'expected_id'}, 'account': {'action', 'key_ref'},
              'scope': {'mode', 'space_id', 'grant'}, 'api_grant': {'all_spaces', 'space_ids', 'permission'},
              'home': {'action', 'space_id', 'chat_id'},
              'mcp': {'mode', 'conflict', 'url', 'shared_api_key', 'token_ref', 'application_name'},
              'offline_boundary': {'profile_home', 'exclusive', 'gateway_stopped', 'mcp_clients_stopped'}}
    result = {}
    for key, item in value.items():
        if key in fields:
            if not isinstance(item, dict) or set(item) - fields[key]:
                raise SetupError('input_schema', 'Unknown nested setup fields; raw credentials are never accepted.')
            result[key] = {}
            for nested, val in item.items():
                if nested.endswith('_ref'):
                    result[key][nested] = safe_reference(val)
                elif nested == 'url':
                    result[key][nested] = safe_url(val)
                elif nested == 'space_ids':
                    if not isinstance(val, list) or any(not isinstance(s, str) for s in val):
                        raise SetupError('input_schema', 'Grant space_ids must be a list of full space IDs.')
                    result[key][nested] = list(val)
                elif val is None and key == 'cli' and nested == 'instance':
                    result[key][nested] = None
                elif not isinstance(val, (str, bool)):
                    raise SetupError('input_schema', 'Setup fields must use the documented scalar types.')
                else:
                    result[key][nested] = val
        elif key.endswith('_ref'):
            result[key] = safe_reference(item)
        elif key == 'base_url':
            result[key] = safe_url(item, origin=True)
        elif key in {'operator_identities', 'admin_identities'}:
            if not isinstance(item, list) or any(not isinstance(s, str) or not s for s in item):
                raise SetupError('input_schema', 'Operator identities must be nonempty strings.')
            result[key] = list(item)
        elif key == 'schema_version' or isinstance(item, (str, bool)):
            result[key] = item
        else:
            raise SetupError('input_schema', 'Setup fields must use the documented scalar types.')
    return result


def safe_mcp(server):
    """Journal only declarative endpoints/commands and secret *references*."""
    if server is None:
        return None
    if not isinstance(server, dict):
        raise SetupError('mcp_config', 'The existing Anytype MCP configuration must be an object.')
    allowed = {'url', 'headers', 'command', 'args', 'env', 'tools', 'enabled',
               'timeout', 'startup_timeout', 'connect_timeout'}
    if set(server) - allowed:
        raise SetupError('mcp_config', 'The existing MCP server has unsupported fields; move credentials to references before setup.')
    result = {}
    if 'url' in server:
        result['url'] = safe_url(server['url'])
    if not isinstance(server.get('headers', {}), dict) or not isinstance(server.get('env', {}), dict):
        raise SetupError('mcp_config', 'MCP headers and environment must be mappings of supported fields.')
    if 'headers' in server:
        result['headers'] = {}
    for key, value in (server.get('headers') or {}).items():
        literal = (key.lower() in {'accept', 'content-type'} and value in {
            'application/json', 'text/event-stream', 'application/json, text/event-stream'}) or (
            key.lower() == 'anytype-version' and isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value))
        if not literal and (not isinstance(value, str) or not re.fullmatch(r'(?:Bearer )?\$\{ANYTYPE_[A-Z0-9_]+\}', value)):
            raise SetupError('inline_mcp_secret', 'Move MCP header credentials to profile secret references.')
        result['headers'][key] = value
    if 'env' in server:
        result['env'] = {}
    for key, value in (server.get('env') or {}).items():
        if key == 'OPENAPI_MCP_HEADERS':
            try:
                headers = json.loads(value)
                safe_mcp({'headers': headers})
            except (ValueError, TypeError):
                raise SetupError('inline_mcp_secret', 'Use declarative MCP headers with profile secret references.')
        elif key == 'ANYTYPE_API_BASE_URL':
            safe_url(value, origin=True)
        elif key == 'ANYTYPE_API_VERSION':
            if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise SetupError('mcp_config', 'The wrapper API version must be a date.')
        elif (
                not isinstance(value, str) or not ENV_REF.fullmatch(value)):
            raise SetupError('inline_mcp_secret', 'Move MCP environment credentials to profile secret references.')
        result['env'][key] = value
    # Arbitrary argv may contain secrets. The supported wrapper argv is fixed.
    if 'args' in server and server['args'] != ['-y', '@anyproto/anytype-mcp@2.0.1']:
        raise SetupError('mcp_command', 'Use the documented Anytype wrapper or HTTP MCP before setup.')
    if 'command' in server:
        if server['command'] != 'npx':
            raise SetupError('mcp_command', 'Use the documented Anytype wrapper or HTTP MCP before setup.')
        result['command'] = 'npx'
    if 'args' in server:
        result['args'] = list(server['args'])
    if 'enabled' in server:
        if not isinstance(server['enabled'], bool):
            raise SetupError('mcp_config', 'MCP enabled must be boolean.')
        result['enabled'] = server['enabled']
    for key in ('timeout', 'startup_timeout', 'connect_timeout'):
        if key in server:
            if not isinstance(server[key], (int, float)) or isinstance(server[key], bool) or server[key] <= 0:
                raise SetupError('mcp_config', 'MCP timeouts must be positive numbers.')
            result[key] = server[key]
    if 'tools' in server:
        filters = server['tools']
        if not isinstance(filters, dict) or set(filters) - {'include', 'exclude', 'resources', 'prompts'}:
            raise SetupError('mcp_config', 'Use documented MCP tool filters.')
        result['tools'] = {}
        for key, value in filters.items():
            if key in {'resources', 'prompts'}:
                valid = isinstance(value, bool)
            else:
                valid = value is None or isinstance(value, str) or (
                    isinstance(value, list) and all(isinstance(name, str) for name in value))
            if not valid:
                raise SetupError('mcp_config', 'Use strings or lists of strings for MCP tool filters.')
            result['tools'][key] = list(value) if isinstance(value, list) else value
    return result


def owned(config):
    platform = config.get('gateway', {}).get('platforms', {}).get('anytype', {})
    if platform.get('api_key') or platform.get('token'):
        raise SetupError('inline_api_secret', 'Move inline gateway credentials into the profile secret store.')
    if 'base_url' in platform.get('extra', {}):
        safe_url(platform['extra']['base_url'], origin=True)
    return {'platform': {k: deepcopy(platform[k]) for k in PLATFORM_KEYS if k in platform},
            'extra': {k: deepcopy(platform.get('extra', {})[k]) for k in EXTRA_KEYS
                      if k in platform.get('extra', {})},
            'mcp': safe_mcp(config.get('mcp_servers', {}).get('anytype'))}


@dataclass(frozen=True)
class AccountEvidence:
    existing: bool
    prior_identity: str | None
    expected_identity: str | None


def has_account_configuration(settings):
    """An empty/disabled placeholder has never selected an attached account.

    Enabled gateways and explicit origins, credential selectors or chat/space
    targets are real prior connections, including when currently disabled.
    Access/activity preferences alone do not establish an account attachment.
    """
    platform, extra = settings.get('platform', {}), settings.get('extra', {})
    discovery = extra.get('discovery') or {}
    return (platform.get('enabled') is True or bool(platform.get('home_channel')) or
            any(extra.get(key) for key in ('base_url', 'api_key_env', 'chats')) or
            bool(discovery.get('spaces') or discovery.get('space_id')))


def account_evidence(record, *, prior_connection=None):
    """Resolve and normalize durable account proof at every journal boundary.

    The old flag is derived, never an authority. The identity in the verified
    prior connection and its legacy alias must agree; observed candidate identity
    also remains binding after resumable external work. No failed candidate can
    erase or override earlier proof.
    """
    connection = prior_connection if prior_connection is not None else record.get('prior_connection', {})
    previous = record.get('prior_connection', {})
    identities = [connection.get('account_identity'), previous.get('account_identity'),
                  record.get('prior_account_identity')]
    candidate = record.get('account_identity')
    if any(value is not None and (not isinstance(value, str) or not value)
           for value in [*identities, candidate]):
        raise SetupError('account_evidence', 'The setup record contains invalid account evidence; inspect it before resuming.')
    proven = {value for value in identities if value is not None}
    if len(proven) > 1 or (proven and candidate is not None and candidate not in proven):
        raise SetupError('account_evidence_conflict', 'Saved setup account identities conflict; preserve the record and inspect before resuming.')
    identity = next(iter(proven), None)
    existing = has_account_configuration(record['prior_owned']) or identity is not None
    if prior_connection is not None:
        record['prior_connection'] = deepcopy(prior_connection)
    record['existing_connection'] = existing
    if identity is None:
        record.pop('prior_account_identity', None)
    else:
        record['prior_account_identity'] = identity
    return AccountEvidence(existing, identity, identity or candidate)


def merge_owned(config, replacement):
    result = deepcopy(config)
    platform = result.setdefault('gateway', {}).setdefault('platforms', {}).setdefault('anytype', {})
    extra = platform.setdefault('extra', {})
    for key in PLATFORM_KEYS:
        platform.pop(key, None)
    for key in EXTRA_KEYS:
        extra.pop(key, None)
    platform.update(deepcopy(replacement['platform']))
    extra.update(deepcopy(replacement['extra']))
    if replacement['mcp'] is None:
        if 'mcp_servers' in result:
            result['mcp_servers'].pop('anytype', None)
    else:
        result.setdefault('mcp_servers', {})['anytype'] = deepcopy(replacement['mcp'])
    return result


class ProfileStore:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.directory = self.home / 'anytype'
        self.path = self.directory / 'setup.json'
        self.config_path = self.home / 'config.yaml'

    @contextmanager
    def locked(self):
        try:
            import fcntl
        except ImportError as error:
            raise SetupError('setup_lock', 'Setup requires supported cross-process file locking.',
                             state='incompatible') from error
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.directory / 'setup.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise SetupError('setup_busy', 'Another setup owns this profile; wait for it to finish.') from error
            yield
        finally:
            os.close(fd)

    def load(self):
        if not self.path.exists():
            return None
        try:
            record = json.loads(self.path.read_text())
            if record['schema_version'] != 1 or not isinstance(record['id'], str):
                raise ValueError()
            account_evidence(record)
            return record
        except (ValueError, KeyError, TypeError):
            raise SetupError('setup_record', 'The setup record is unsupported or damaged; preserve it for recovery.')

    def save(self, record):
        account_evidence(record)
        if 'choices' in record:
            record['choices'] = safe_choices(record['choices'])
        for key in ('prior_owned', 'candidate'):
            if key in record:
                item = record[key]
                item['mcp'] = safe_mcp(item.get('mcp'))
                if 'base_url' in item.get('extra', {}):
                    safe_url(item['extra']['base_url'], origin=True)
        atomic_json(self.path, record)

    def snapshot(self):
        from hermes_cli.config import require_readable_config_before_write
        try:
            before = self.revision()
            config = require_readable_config_before_write(self.config_path)
            if self.revision() != before:
                raise SetupError('config_changed', 'Configuration changed while it was read; retry setup.')
            return config, before
        except SetupError:
            raise
        except Exception as error:
            raise SetupError('config_read', 'Cannot safely read the profile configuration; repair it before setup.') from error

    def revision(self):
        try:
            return hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        except FileNotFoundError:
            return 'absent'

    def stage(self, name, value):
        from hermes_cli.config import get_env_path, load_env, require_env_writable, save_env_value
        from hermes_constants import get_hermes_home
        if Path(get_hermes_home()).resolve() != self.home or get_env_path().resolve() != self.home / '.env':
            raise SetupError('profile_changed', 'The active profile changed; re-run setup in the intended profile.')
        if not re.fullmatch(r'ANYTYPE_(?:API_KEY|MCP_TOKEN)_SETUP_[A-F0-9]{32}', name):
            raise SetupError('secret_reference', 'Invalid staged secret reference.', state='error')
        if not isinstance(value, str) or not value or not value.isascii() or any(ord(c) < 33 for c in value):
            raise SetupError('credential_format', 'Supply a nonempty private API credential without whitespace.')
        previous = load_env().get(name)
        if previous is not None and previous != value:
            raise SetupError('secret_conflict', 'The staged reference is already occupied; preserve it and start a new setup.')
        if previous == value:
            return
        require_env_writable(name, 'set')
        save_env_value(name, value)
        if load_env().get(name) != value:
            raise SetupError('secret_write', 'The staged credential was not durably stored.', state='error')
        with get_env_path().open('rb') as file:
            os.fsync(file.fileno())
        sync_dir(self.home)

    def begin(self, config, revision, *, prior_connection=None, choices=None):
        record = {'schema_version': 1, 'id': uuid.uuid4().hex.upper(), 'step': 'selected',
                  'prior_revision': revision, 'prior_owned': owned(config), 'operations': {},
                  'consumers': [], 'changed': False,
                  'choices': safe_choices(choices or {})}
        account_evidence(record, prior_connection=prior_connection)
        self.save(record)
        return record

    def require_config_ownership(self):
        """Reject administrator-owned Anytype settings before any consumer stop.

        The raw user config is the publication target, but managed leaves win at
        runtime. Unrelated administrator settings do not block this plugin.
        """
        from hermes_cli.config import is_managed
        from hermes_cli.managed_scope import managed_config_keys
        if is_managed():
            raise SetupError('managed_config', 'The active profile configuration is managed externally.',
                next_action='Use the owning configuration manager to change Anytype settings.')
        roots = ('gateway.platforms.anytype', 'mcp_servers.anytype')
        if any(key == root or key.startswith(root + '.') or root.startswith(key + '.')
               for key in managed_config_keys() for root in roots):
            raise SetupError('managed_config', 'Administrator-managed settings control this Anytype connection.',
                next_action='Have the administrator update the Anytype gateway/MCP configuration or remove its managed pins before running setup; unrelated profile settings may remain managed.')

    def commit(self, record, config, revision, *, fault=lambda phase: None):
        from hermes_cli.config import atomic_config_replace
        self.require_config_ownership()
        if self.revision() != revision:
            raise SetupError('config_changed', 'Configuration changed before publication; inspect and resume.')
        result = merge_owned(config, record['candidate'])
        record['commit_expected'] = digest(result)
        record['commit_base_revision'] = revision
        record['step'] = 'committing'
        self.save(record)
        fault('before_commit')
        self.require_config_ownership()
        if self.revision() != revision:
            raise SetupError('config_changed', 'Configuration changed before publication; inspect and resume.')
        atomic_config_replace(self.config_path, result)
        with self.config_path.open('rb') as file:
            os.fsync(file.fileno())
        sync_dir(self.home)
        fault('after_commit')
        record.update(step='committed', committed_revision=self.revision(), changed=True)
        self.save(record)

    def reconcile_commit(self, record):
        if record['step'] != 'committing':
            return
        current, revision = self.snapshot()
        if digest(current) == record.get('commit_expected'):
            record.update(step='committed', committed_revision=revision, changed=True)
        elif revision == record.get('commit_base_revision'):
            record['step'] = 'prepared'
        else:
            raise SetupError('ambiguous_commit', 'Configuration changed across an interrupted commit; preserve both keys and inspect.')
        self.save(record)


class OperatorBoundary:
    """The baseline host has no registry/exclusive lease for all MCP consumers.

    An operator may maintain an offline boundary. A runtime lock additionally
    excludes profile gateway starts; the operator still owns MCP/shared-host
    exclusion. We never infer that arbitrary clients are coordinated.
    """
    def __init__(self, home, acknowledgement=None):
        self.home = Path(home).resolve()
        self.ack = acknowledgement or {}

    @contextmanager
    def hold(self):
        if (self.ack.get('profile_home') != str(self.home)
                or self.ack.get('exclusive') is not True
                or self.ack.get('gateway_stopped') is not True
                or self.ack.get('mcp_clients_stopped') is not True):
            raise SetupError('consumer_boundary', 'Hermes cannot exclusively coordinate every MCP client on this host.',
                next_action='Stop this profile through Hermes, close its MCP clients, prevent concurrent starts/config edits, '
                            'then explicitly supply the offline_boundary acknowledgement described in docs/SETUP.md.')
        from gateway.status import (acquire_gateway_runtime_lock, release_gateway_runtime_lock,
                                    owns_gateway_runtime_lock)
        from hermes_cli.gateway import find_gateway_pids
        from hermes_cli.gateway_multiplex_served import live_default_gateway_pid
        from hermes_constants import get_default_hermes_root
        if find_gateway_pids() or owns_gateway_runtime_lock():
            raise SetupError('consumer_running', 'A gateway still owns this profile; stop it through Hermes first.')
        # The profile runtime lock does not exclude a running multiplexer loading
        # a satellite. The operator must stop that host, and be told its impact.
        if self.home != Path(get_default_hermes_root()).resolve() and live_default_gateway_pid():
            raise SetupError('shared_host', 'The shared host gateway is running; this boundary cannot isolate its profile.',
                next_action='Coordinate an offline window with the shared host operator, or keep the candidate prepared.')
        if not acquire_gateway_runtime_lock():
            raise SetupError('consumer_running', 'Could not reserve the stopped profile gateway.')
        try:
            yield
        finally:
            release_gateway_runtime_lock()

    def inspect(self):
        return [{'id': 'operator-offline:' + str(self.home), 'running': False,
                 'management': 'operator', 'stopped': False}]

    def verify_stopped(self, consumers):
        return all(not item['running'] for item in consumers)

    def stop(self, consumer):
        raise SetupError('consumer_boundary', 'Automatic stop is unavailable for this consumer.')

    def restore(self, consumer):
        return False

    def start(self, consumers):
        return False


class ManagedGatewayBoundary(OperatorBoundary):
    """Coordinate one installed, unshared Hermes gateway via its public CLI.

    Other profile MCP clients still require an operator-maintained exclusion.
    No native Anytype lifecycle operation is part of this boundary.
    """
    @contextmanager
    def hold(self):
        from hermes_cli.gateway import get_gateway_runtime_snapshot, host_multiplexer_serving
        if (self.ack.get('profile_home') != str(self.home) or self.ack.get('exclusive') is not True
                or self.ack.get('mcp_clients_stopped') is not True):
            raise SetupError('consumer_boundary', 'Close other profile MCP clients and prevent concurrent starts/config edits before managed activation.')
        owner = host_multiplexer_serving()
        if owner is not None and len(owner.profiles) > 1:
            raise SetupError('shared_host', 'This gateway serves other profiles; automatic setup will not stop the shared host.',
                             next_action='Coordinate an offline window with the host operator, or defer publication.')
        snapshot = get_gateway_runtime_snapshot()
        if not snapshot.service_installed or snapshot.has_process_service_mismatch:
            raise SetupError('unmanaged_gateway', 'The profile does not have an unambiguous managed gateway service.',
                             next_action='Stop the profile through Hermes and use an explicit offline boundary.')
        self.reserved = False
        try:
            yield
        finally:
            if self.reserved:
                from gateway.status import release_gateway_runtime_lock
                release_gateway_runtime_lock()
                self.reserved = False

    def inspect(self):
        from hermes_cli.gateway import get_gateway_runtime_snapshot
        from gateway.status import get_process_start_time
        snapshot = get_gateway_runtime_snapshot()
        return [{'id': 'managed-gateway:' + str(self.home), 'running': snapshot.running,
                 'manager': snapshot.manager, 'scope': snapshot.service_scope,
                 'pids': [[pid, get_process_start_time(pid)] for pid in snapshot.gateway_pids],
                 'stopped': False}]

    @staticmethod
    def command(action):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        from types import SimpleNamespace
        from hermes_cli.gateway import gateway_command
        try:
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                gateway_command(SimpleNamespace(gateway_command=action, system=False, force=False, **{'all': False}))
        except (Exception, SystemExit) as error:
            raise SetupError('lifecycle_failed', 'Hermes could not complete the requested managed gateway action.') from error

    def stop(self, consumer):
        if self.inspect()[0] != {k: v for k, v in consumer.items() if k not in {'stop_intent', 'restored'}}:
            raise SetupError('consumer_changed', 'The gateway identity changed before stop; inspect it before resuming.')
        self.command('stop')

    def verify_stopped(self, consumers):
        from gateway.status import acquire_gateway_runtime_lock, owns_gateway_runtime_lock
        if self.inspect()[0]['running'] or owns_gateway_runtime_lock():
            return False
        self.reserved = acquire_gateway_runtime_lock()
        return self.reserved

    def restore(self, consumer):
        current = self.inspect()[0]
        if (current['id'], current['manager'], current['scope']) != (consumer['id'], consumer['manager'], consumer['scope']):
            return False
        if current['running']:
            return current['pids'] == consumer['pids']
        self.command('start')
        return self.inspect()[0]['running']

    def start(self, consumers):
        self.command('start')
        return self.inspect()[0]['running']


def restore_consumers(store, record, lifecycle):
    """Only undo stops performed by this attempt against unchanged prior config."""
    if not record.get('consumers'):
        return []
    current, revision = store.snapshot()
    pending = [c for c in record['consumers'] if c.get('running') and
               (c.get('stopped') or c.get('stop_intent')) and not c.get('restored')]
    if not pending:
        if record.pop('stopped_consumers', None):
            store.save(record)
        return []
    try:
        store.require_config_ownership()
    except SetupError:
        record['stopped_consumers'] = [c['id'] for c in pending]
        store.save(record)
        return record['stopped_consumers']
    if owned(current) != record['prior_owned'] or revision != record.get('restore_revision', record['prior_revision']):
        record['stopped_consumers'] = [c['id'] for c in pending]
        store.save(record)
        return record['stopped_consumers']
    remaining = []
    for consumer in pending:
        try:
            if lifecycle.restore(consumer):
                consumer['restored'] = True
            else:
                remaining.append(consumer['id'])
        except Exception:
            remaining.append(consumer['id'])
        store.save(record)
    if remaining:
        record['stopped_consumers'] = remaining
    else:
        record.pop('stopped_consumers', None)
    store.save(record)
    return remaining


def publish(store, record, lifecycle, *, activation='defer', fault=lambda phase: None):
    store.reconcile_commit(record)
    store.require_config_ownership()
    if record['step'] == 'committed':
        if store.revision() != record['committed_revision']:
            raise SetupError('config_changed', 'Configuration changed after publication; inspect before activation.')
        if activation == 'activate':
            with lifecycle.hold():
                consumers = lifecycle.inspect()
                if any(c['running'] for c in consumers):
                    raise SetupError('consumer_running', 'A consumer started after the commit; inspect adoption before proceeding.')
                if not lifecycle.verify_stopped(consumers):
                    raise SetupError('consumer_boundary', 'The committed profile cannot be verified stopped.')
            if lifecycle.start(consumers):
                record['step'] = 'active'
                store.save(record)
                return outcome('saved', changed=True, publication='active', gateway='running',
                               next_action='Run anytype doctor to confirm gateway readiness and MCP adoption.')
        return outcome('saved', changed=True, publication='committed', gateway='stopped_or_unverified',
                       next_action='Start this profile through Hermes, then run anytype doctor; MCP adoption is separate.')
    if activation == 'defer':
        return outcome('restart_required', changed=record.get('changed', False), publication='prepared',
                       next_action='Resume with an exclusive offline boundary to publish this candidate. Live configuration is unchanged.')
    try:
        with lifecycle.hold():
            # Resume/abandon after a partial stop restores old consumers before
            # another attempt; an unverified restoration blocks further work.
            remaining = restore_consumers(store, record, lifecycle)
            if remaining:
                raise SetupError('restore_required', 'Some previous consumers remain stopped.',
                                 next_action='Restore these consumers through Hermes: ' + ', '.join(remaining))
            config, revision = store.snapshot()
            if revision != record['prior_revision']:
                raise SetupError('config_changed', 'Configuration changed since preparation; the candidate was not published.')
            record['consumers'] = lifecycle.inspect()
            store.save(record)
            fault('before_stop')
            for consumer in record['consumers']:
                if not consumer['running']:
                    continue
                consumer['stop_intent'] = True
                store.save(record)
                lifecycle.stop(consumer)
                consumer['stopped'] = True
                store.save(record)
                fault('after_stop')
            config, revision = store.snapshot()
            # Allow only lifecycle-reported changes, never unrelated edits.
            allowed = getattr(lifecycle, 'config_after_stop', lambda prior, current: prior == current)
            if not allowed(record['prior_revision'], revision):
                raise SetupError('config_changed', 'Configuration changed while consumers stopped; inspect before resuming.')
            if owned(config) != record['prior_owned']:
                raise SetupError('config_changed', 'Anytype settings changed while consumers stopped; candidate not published.')
            record['restore_revision'] = revision
            store.save(record)
            if not lifecycle.verify_stopped(record['consumers']):
                raise SetupError('consumer_boundary', 'Some affected consumers are not verifiably stopped.')
            store.commit(record, config, revision, fault=fault)
        # Release the reservation before the supported host start command.
        if activation == 'activate' and lifecycle.start(record['consumers']):
            record['step'] = 'active'
            store.save(record)
            return outcome('saved', changed=True, publication='active', gateway='running', mcp='adoption_requires_doctor',
                           next_action='Run anytype doctor to confirm readiness and MCP adoption.')
        return outcome('saved', changed=True, publication='committed', gateway='stopped',
                       next_action='Start the profile through Hermes when ready, then run anytype doctor.')
    except BaseException:
        # Do not revert an already committed candidate or delete either key.
        store.reconcile_commit(record)
        if record['step'] not in {'committed', 'active'}:
            remaining = restore_consumers(store, record, lifecycle)
            if remaining:
                record['stopped_consumers'] = remaining
                store.save(record)
        raise
