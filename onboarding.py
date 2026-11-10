"""Standalone, profile-scoped Anytype wizard. Importing it provisions nothing."""
import asyncio
from copy import deepcopy
import getpass
import json
import os
from pathlib import Path
import stat
import sys
from urllib.parse import quote
import uuid

from .client import Client, APIError
from .config import Settings, Target, resolve_api_key, api_key_name
from .diagnostics import (Connection, inspect_api, inspect_mcp, check_policy, check_activity,
                          doctor, require_application_name)
from .setup_backend import NativeCLI, agent_name
from .setup_store import (SetupError, ProfileStore, OperatorBoundary, ManagedGatewayBoundary,
                          outcome, owned, publish, safe_mcp, restore_consumers, safe_choices,
                          safe_reference, safe_url, account_evidence)


def load_input(path):
    try:
        raw = Path(path).read_text()
        if len(raw) > 1_000_000:
            raise ValueError()
        value = json.loads(raw)
    except (OSError, ValueError):
        raise SetupError('input_document', 'Setup input must be a readable versioned JSON document.')
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise SetupError('input_schema', 'Use schema_version 1 and the fields documented in docs/SETUP.md.')
    return safe_choices(value)


def validate_reference(reference):
    safe_reference(reference)


def connection_metadata(connection):
    return {'account_identity': connection.identity, 'observed_name': connection.name,
            'grant': deepcopy(connection.grant), 'credential': dict(connection.key),
            'space_ids': [s['id'] for s in connection.spaces], 'base_url': connection.base_url}


def record_connection(record):
    return {key: deepcopy(record[key]) for key in ('account_identity', 'observed_name', 'desired_name',
        'grant', 'credential', 'space_ids', 'mode', 'mcp_readiness', 'base_url', 'native') if key in record}


def metadata_connection(value):
    return Connection(value['account_identity'], value.get('observed_name', ''), value['grant'],
                      [{'id': space} for space in value['space_ids']], {}, {}, key=value.get('credential', {}),
                      base_url=value.get('base_url', ''))


def requested_grant(choices, scope):
    grant = choices.get('api_grant')
    if grant is None:
        all_spaces = scope['mode'] == 'all' or scope.get('grant') == 'all'
        grant = {'all_spaces': all_spaces, 'space_ids': [] if all_spaces else [scope['space_id']], 'permission': 'readwrite'}
    if (not isinstance(grant, dict) or not isinstance(grant.get('all_spaces'), bool)
            or grant.get('permission', 'readwrite') != 'readwrite'
            or not isinstance(grant.get('space_ids', []), list)):
        raise SetupError('api_grant', 'Choose api_grant.all_spaces and an explicit space_ids list with readwrite permission.')
    ids = grant.get('space_ids', [])
    for space in ids:
        Target(space, 'validate')
    if (len(ids) != len(set(ids)) or (grant['all_spaces'] and ids) or
            (not grant['all_spaces'] and not ids)):
        raise SetupError('api_grant', 'Use all_spaces true with no IDs, or false with distinct full space IDs.')
    if scope['mode'] == 'single' and not grant['all_spaces'] and scope['space_id'] not in ids:
        raise SetupError('scope_grant', 'The discovery space must be included in the intended API grant.')
    return {'all_spaces': grant['all_spaces'], 'space_ids': sorted(ids), 'permission': 'readwrite'}


def read_secret(reference):
    validate_reference(reference)
    if 'env' in reference:
        from gateway.platforms._shared import get_scoped_secret
        name = reference['env']
        if not name or not all(char.isalnum() or char == '_' for char in name) or not name.isascii():
            raise SetupError('secret_reference', 'Use a valid environment secret reference.')
        value = get_scoped_secret(name, '', external_fallback=True)
    else:
        path = Path(reference['file'])
        if not path.is_absolute():
            raise SetupError('secret_file', 'Private secret file paths must be absolute.')
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'r', encoding='utf-8') as file:
                info = os.fstat(file.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or
                        info.st_uid != os.geteuid() or info.st_size > 65536):
                    raise SetupError('secret_file', 'Secret files must be private regular files owned by the current user (mode 0600).')
                value = file.read(65537).rstrip('\r\n')
        except SetupError:
            raise
        except (OSError, ValueError, UnicodeError) as error:
            raise SetupError('secret_file', 'The private secret file could not be read safely.') from error
    if not value:
        raise SetupError('secret_missing', 'The supplied private secret reference is empty or unavailable.')
    return value


def prompt(text, default=None, *, private=False):
    label = text + (f' [{default}]' if default is not None else '') + ': '
    if private:
        # getpass can fall back to echoing input when no TTY exists; never allow it.
        if not sys.stdin.isatty():
            raise SetupError('private_input', 'Use a private file/environment reference for non-TTY secret input.')
        return getpass.getpass(label, stream=sys.stderr)
    print(label, file=sys.stderr, end='', flush=True)
    answer = input().strip()
    return answer if answer else default


def choose(text, options, default=None):
    answer = prompt(text + ' (' + '/'.join(options) + ')', default)
    if answer not in options:
        raise SetupError('choice', 'Choose one of the displayed setup actions.')
    return answer


def wrapper_config(base_url, key_env):
    return {'command': 'npx', 'args': ['-y', '@anyproto/anytype-mcp@2.0.1'], 'env': {
        'ANYTYPE_API_BASE_URL': base_url,
        'OPENAPI_MCP_HEADERS': json.dumps({'Authorization': 'Bearer ${' + key_env + '}', 'Anytype-Version': '2025-11-08'})}}


class Wizard:
    def __init__(self, home, *, interactive=True, api_transport=None, mcp_transport=None,
                 native_factory=NativeCLI, lifecycle=None, secret_reader=read_secret):
        self.store = ProfileStore(home)
        self.interactive = interactive
        self.api_transport, self.mcp_transport = api_transport, mcp_transport
        self.native_factory, self.lifecycle = native_factory, lifecycle
        self.secret_reader = secret_reader
        self.private = {}

    def need(self, choices, key, label, default=None, options=None):
        if key in choices:
            return choices[key]
        if not self.interactive:
            raise SetupError('missing_choice', f'Set the non-secret setup choice {key}.')
        choices[key] = choose(label, options, default) if options else prompt(label, default)
        return choices[key]

    def credential(self, choices, key, label):
        if key in choices:
            return self.secret_reader(choices[key])
        if key in self.private:
            return self.private[key]
        if not self.interactive:
            raise SetupError('missing_secret_reference', f'Supply {key} through a private secret reference.')
        value = prompt(label, private=True)
        self.private[key] = value
        return value

    def boundary(self, choices):
        if self.lifecycle:
            return self.lifecycle
        boundary = ManagedGatewayBoundary if choices.get('manage_gateway') is True else OperatorBoundary
        return boundary(self.store.home, choices.get('offline_boundary'))

    def secret(self, name, record=None):
        """Existing references use exactly the runtime authority and precedence.

        Only this incomplete attempt's private new slots may need a file fallback
        when an already-bound scope predates staging. A scoped/external value,
        including an explicit empty value, always wins over the file.
        """
        from gateway.platforms._shared import get_scoped_secret
        value = get_scoped_secret(name, None, external_fallback=True)
        if value is not None:
            return value
        if record and record['step'] not in {'committed', 'active', 'cancelled'}:
            names = set(record.get('staged_env', []))
            # Accept journals written before independent correction slots were
            # introduced, whose fresh names were derived from the attempt ID.
            names.update('ANYTYPE_' + kind + '_SETUP_' + record['id']
                         for kind in ('API_KEY', 'MCP_TOKEN'))
            if name in names:
                from hermes_cli.config import load_env
                return load_env().get(name, '')
        return ''

    def stage_credential(self, record, kind, value):
        """A correction selects a fresh immutable slot; old references stay valid."""
        from hermes_cli.config import load_env
        field = 'api_key_env' if kind == 'API_KEY' else 'mcp_token_env'
        name = record.get(field)
        stored = load_env()
        if not name or stored.get(name) != value or self.secret(name, record) != value:
            name = 'ANYTYPE_' + kind + '_SETUP_' + uuid.uuid4().hex.upper()
            record[field] = name
            record.setdefault('staged_env', []).append(name)
            self.store.save(record)
        self.store.stage(name, value)
        return name

    async def run(self, choices=None, *, resume=False, cancel=False, rollback=False):
        choices = deepcopy(choices or {})
        explicit_activation = 'activation' in choices
        record = None
        try:
            with self.store.locked():
                config, revision = self.store.snapshot()
                record = self.store.load()
                if record:
                    self.store.reconcile_commit(record)
                    if record['step'] not in {'active', 'committed', 'cancelled'}:
                        recovery_choices = {**record.get('choices', {}), **choices}
                        remaining = restore_consumers(self.store, record, self.boundary(recovery_choices))
                        if remaining:
                            raise SetupError('restore_required', 'An interrupted setup left consumers stopped.',
                                             next_action='Restore these consumers through Hermes: ' + ', '.join(remaining))
                        # Restoration may update host-owned state. Re-read before
                        # candidate work and retain normal conflict detection.
                        config, revision = self.store.snapshot()
                choices = safe_choices(choices)
                if not cancel:
                    self.store.require_config_ownership()
                if rollback:
                    return await self.rollback(record, choices, config, revision)
                if cancel:
                    return self.cancel(record, choices)
                incomplete = record and record['step'] not in {'active', 'committed', 'cancelled'}
                if resume and not incomplete and not (record and record['step'] == 'committed'):
                    raise SetupError('no_setup', 'There is no incomplete setup to resume.')
                if incomplete or resume:
                    previous = deepcopy(record.get('choices', {}))
                    previous.update(choices)
                    choices = previous
                    if self.interactive and not explicit_activation:
                        choices.pop('activation', None)
                    if record.get('candidate') and record['step'] in {'prepared', 'committed'}:
                        # Prepared choices are immutable except activation/boundary.
                        allowed = {'schema_version', 'activation', 'offline_boundary', 'manage_gateway'}
                        supplied = {k: v for k, v in choices.items() if k not in allowed}
                        old = {k: v for k, v in record.get('choices', {}).items() if k not in allowed}
                        if supplied != old:
                            raise SetupError('prepared_choices', 'Cancel this prepared attempt before changing its connection or policy.')
                        return await self.finish(record, choices)
                    if revision != record['prior_revision']:
                        raise SetupError('config_changed', 'Configuration changed since setup began; preserve the candidate and inspect.')
                else:
                    prior_connection = (record_connection(record) if record and
                                        record.get('committed_revision') == revision else None)
                    current = account_evidence({'prior_owned': owned(config)}, prior_connection=prior_connection)
                    if current.existing:
                        action = self.need(choices, 'action', 'Existing connection', 'keep', ['keep', 'repair', 'change'])
                        if action == 'keep':
                            return await doctor(config, api_transport=self.api_transport, mcp_transport=self.mcp_transport)
                    # Persist identity protection in the first journal write,
                    # before any prompt or other resumable failure can occur.
                    record = self.store.begin(config, revision, prior_connection=prior_connection, choices=choices)
                return await self.prepare(record, choices, config)
        except (EOFError, KeyboardInterrupt):
            error = SetupError('cancelled', 'Setup interrupted; completed external operations and prepared credentials are retained.')
            return self.failure(error, record, 'Resume setup to reconcile its recorded progress.')
        except SetupError as error:
            return self.failure(error, record)
        except Exception:
            # CLI stderr, HTTP bodies, JSON parse errors and arbitrary exception
            # messages may contain credentials. Never emit them as diagnostics.
            error = SetupError('setup_failed', 'Setup could not complete; prior live settings were preserved unless the journal reports a commit.', state='error')
            return self.failure(error, record, 'Resume setup to reconcile the journal before retrying any operation.')

    @staticmethod
    def failure(error, record, next_action=None):
        details = {}
        if record and record.get('stopped_consumers'):
            details['stopped_consumers'] = record['stopped_consumers']
            next_action = 'Restore these consumers through Hermes before resuming: ' + ', '.join(record['stopped_consumers'])
        return outcome(error.state, changed=bool(record and record.get('changed')), error=error,
                       next_action=next_action or error.next_action, **details)

    def native_choices(self, choices, record):
        if self.interactive:
            if 'service' not in choices:
                choices['service'] = {'action': choose('Native service', ['reuse', 'install', 'start'], 'reuse')}
            if 'account' not in choices:
                choices['account'] = {'action': choose('Native account', ['reuse', 'login', 'create'], 'reuse')}
            action = choices['account'].get('action')
            if action == 'create' and 'agent_name' not in choices:
                choices['agent_name'] = agent_name(prompt('Agent display name'))
            if action == 'login' and not choices['account'].get('key_ref') and not record.get('operations', {}).get('native_account'):
                self.private['account_key'] = prompt('Native account recovery key (stored only by Anytype CLI)', private=True)
            if 'network' not in choices:
                selected = choose('Native network', ['preserve', 'default', 'custom'], 'preserve')
                choices['network'] = {'mode': selected}
                if selected == 'custom':
                    choices['network']['config_file'] = prompt('Absolute network configuration path')
            if 'join_space' not in choices and not choices.get('invitation_ref'):
                choices['join_space'] = choose('Join a space by private invitation', ['yes', 'no'], 'no') == 'yes'
            if choices.get('join_space') and not choices.get('invitation_ref') and not record.get('operations', {}).get('native_join'):
                self.private['invitation'] = prompt('Space invitation', private=True)
        choices.setdefault('network', {'mode': 'preserve'})

    @staticmethod
    def validate_decisions(choices):
        for key, allowed in {'access': {'operator', 'anyone'}, 'activation': {'defer', 'save_stopped', 'activate'}}.items():
            if key in choices and choices[key] not in allowed:
                raise SetupError(key, 'Choose a documented ' + key + ' action.')
        for key, field, allowed in [('home', 'action', {'keep', 'select', 'create', 'skip'}),
                                    ('mcp', 'mode', {'keep', 'http', 'wrapper', 'none'})]:
            if key in choices and choices[key].get(field) not in allowed:
                raise SetupError(key + '_choice', 'Choose a documented ' + key + ' action.')
        if 'mcp' in choices and choices['mcp'].get('conflict', 'replace') not in {'replace', 'remove', 'keep'}:
            raise SetupError('mcp_choice', 'Choose an explicit MCP conflict action.')

    async def prepare(self, record, choices, config):
        evidence = account_evidence(record)
        self.store.save(record)
        mode = self.need(choices, 'mode', 'Connection backend', 'cli', ['cli', 'desktop', 'external'])
        if mode not in {'cli', 'desktop', 'external'}:
            raise SetupError('mode', 'Choose cli, desktop, or external mode.')
        old = record['prior_owned']
        # Existing profile identity/application evidence must precede all native
        # service/account/key mutations, including when a previous key expired.
        if mode == 'cli' and evidence.existing and not evidence.prior_identity:
            old_token = resolve_api_key(old['extra'], required=False)
            if old_token:
                try:
                    previous = await inspect_api(old['extra'], old_token, transport=self.api_transport)
                except SetupError:
                    if not choices.get('expected_account'):
                        raise
                else:
                    evidence = account_evidence(record, prior_connection={**record.get('prior_connection', {}), **connection_metadata(previous)})
                    self.store.save(record)
            if not evidence.expected_identity and not choices.get('expected_account'):
                raise SetupError('account_unverified', 'Supply the known expected_account before native provisioning.')
        if evidence.expected_identity and choices.get('expected_account') and evidence.expected_identity != choices['expected_account']:
            raise SetupError('account_mismatch', 'The expected account differs from the previous connection; account migration is separate.')
        if mode == 'cli':
            self.native_choices(choices, record)
        elif choices.get('account', {}).get('action', 'reuse') != 'reuse':
            raise SetupError('account_backend', 'Manage desktop/external account login or naming through its supported client; setup only attaches.')
        if 'agent_name' in choices:
            choices['agent_name'] = agent_name(choices['agent_name'])
        scope = choices.get('scope')
        if scope is None and self.interactive:
            selection = choose('Gateway discovery (all spaces accessible to the API grant)', ['all', 'single'], 'all')
            scope = {'mode': selection}
            if selection == 'single' and not choices.get('join_space'):
                scope['space_id'] = prompt('Full space ID')
            choices['scope'] = scope
        if not isinstance(scope, dict) or scope.get('mode') not in {'all', 'single'}:
            raise SetupError('scope', 'Choose scope.mode all or single, with the full space_id for single.')
        unresolved_space = (mode == 'cli' and scope['mode'] == 'single' and not scope.get('space_id')
                            and (choices.get('join_space') or choices.get('invitation_ref')))
        if scope['mode'] == 'single' and not unresolved_space:
            Target(scope.get('space_id'), 'validate')
        if unresolved_space and choices.get('api_grant'):
            raise SetupError('scope_grant', 'Resolve the invitation space before supplying an explicit named API grant.')
        grant = None if unresolved_space else requested_grant(choices, scope)
        if grant:
            choices['api_grant'] = grant
        # Validate declarative decisions before creating external resources.
        self.validate_decisions(choices)
        if mode == 'cli':
            cli = choices.get('cli', {})
            native = self.native_factory(cli.get('executable', 'anytype'), cli.get('instance'))
            native.private = self.private
            record['choices'] = deepcopy(choices)
            self.store.save(record)
            prepared = native.prepare(choices, record, self.store, self.secret_reader)
            if asyncio.iscoroutine(prepared):
                prepared = await prepared
            choices.update(prepared.get('choices', {}))
            base = safe_url(choices['base_url'], origin=True)
            scope = choices['scope']
            grant = requested_grant(choices, scope)
            if prepared.get('reuse_api_key') is True and record.get('native_reuse_key'):
                native_token = self.selected_key(old['extra'])
            elif prepared.get('api_key'):
                native_token = prepared['api_key']
            else:
                raise SetupError('cli_key_receipt', 'Native preparation did not return its integration credential.', state='incompatible')
        else:
            default_base = old['extra'].get('base_url') or ('http://127.0.0.1:31009' if mode == 'desktop' else 'http://127.0.0.1:31012')
            base = self.need(choices, 'base_url', 'Anytype API origin reachable from this Hermes process', default_base) if self.interactive else choices.get('base_url', default_base)
            base = safe_url(base, origin=True)
            choices['base_url'] = base
        choices['api_grant'] = grant
        discovery = {'spaces': 'all'} if scope['mode'] == 'all' else {'space_id': scope['space_id']}
        prior_discovery = old['extra'].get('discovery', {})
        discovery.update(watch_agent_objects=prior_discovery.get('watch_agent_objects', True),
                         response=prior_discovery.get('response', 'all'))
        extra = deepcopy(config.get('gateway', {}).get('platforms', {}).get('anytype', {}).get('extra', {}))
        extra.update(base_url=base, discovery=discovery)
        extra.setdefault('chats', [])
        extra.setdefault('group_allow_from', [])
        extra.setdefault('group_allow_admin_from', [])
        extra['activity_status'] = choices.get('activity_status', extra.get('activity_status', True))
        # Re-read private references on incomplete resume: their contents can be
        # corrected without changing the reference itself. Completed operations
        # remain attached to the same attempt and account.
        if mode == 'cli':
            token = native_token
        elif 'api_key_ref' in choices or 'api_key_ref' in self.private:
            token = self.credential(choices, 'api_key_ref', 'Anytype application API key')
        elif record.get('api_key_validated') and record.get('api_key_env'):
            token = self.secret(record['api_key_env'], record)
        elif evidence.existing:
            token = resolve_api_key(old['extra'], required=False)
        else:
            token = ''
        token = token or self.credential(choices, 'api_key_ref', 'Anytype application API key')
        record['choices'] = deepcopy(choices)
        record['api_key_validated'] = False
        self.store.save(record)
        name = self.stage_credential(record, 'API_KEY', token)
        extra['api_key_env'] = name
        if evidence.existing and not evidence.prior_identity:
            old_token = resolve_api_key(old['extra'], required=False)
            if old_token:
                try:
                    previous = await inspect_api(old['extra'], old_token, transport=self.api_transport)
                except SetupError:
                    if not choices.get('expected_account'):
                        raise
                else:
                    evidence = account_evidence(record, prior_connection={**record.get('prior_connection', {}), **connection_metadata(previous)})
                    self.store.save(record)
        expected = evidence.expected_identity
        if evidence.existing and not expected and not choices.get('expected_account'):
            raise SetupError('account_unverified', 'The existing account cannot be verified; supply its known expected_account before repairing.')
        if expected and choices.get('expected_account') and expected != choices['expected_account']:
            raise SetupError('account_mismatch', 'The expected account differs from the previous connection; account migration is separate.')
        expected = expected or choices.get('expected_account') or record.get('native', {}).get('account_id')
        if old['extra'].get('base_url', 'http://127.0.0.1:31009') != base and evidence.existing and not choices.get('ack_origin_change'):
            raise SetupError('origin_migration', 'Changing API origin selects a different transport namespace; watches and receipts are not copied.',
                             next_action='Set ack_origin_change true after reviewing the migration guidance; keep the same bot account.')
        # Defer old home/pins scope conflicts until the explicit home choice, but
        # never silently discard them to make a changed candidate validate.
        if scope['mode'] == 'single' and any(t['space_id'] != scope['space_id'] for t in extra['chats']):
            if choices.get('drop_outside_pins') is True:
                extra['chats'] = [t for t in extra['chats'] if t['space_id'] == scope['space_id']]
            else:
                raise SetupError('pinned_scope', 'Existing pinned chats conflict with the new single-space scope.',
                                 next_action='Explicitly set drop_outside_pins true and select or skip the new home.')
        connection = await inspect_api(extra, token, expected_identity=expected, requested_grant=grant, transport=self.api_transport)
        record['api_key_validated'] = True
        if mode == 'cli':
            receipt = record['native']
            if connection.key.get('id') != receipt['credential_id'] or connection.key.get('name') != receipt['application_name']:
                raise SetupError('cli_key_probe', 'The HTTP API did not confirm the exact native integration credential.')
        if evidence.existing:
            require_application_name(connection.key, record.get('prior_connection', {}).get('credential', {}),
                                     expected_name=choices.get('application_name'))
        if choices.get('agent_name') and choices['agent_name'] != connection.name:
            raise SetupError('rename_unsupported', 'The requested name differs from the existing account; an explicit supported rename is required.', state='incompatible')
        record.update(connection_metadata(connection))
        record.update(step='space_usable', desired_name=choices.get('agent_name', connection.name), mode=mode)
        self.store.save(record)
        if self.interactive:
            print('Connected account: ' + (connection.name or '(unnamed)') + ' (' + connection.identity + ')', file=sys.stderr)
        access = self.need(choices, 'access', 'Sender access', 'operator', ['operator', 'anyone'])
        if access not in {'operator', 'anyone'}:
            raise SetupError('access', 'Choose explicit operator access or anyone access.')
        operators = choices.get('operator_identities')
        if operators is None and self.interactive:
            people = {m['identity']: m.get('name', '') for rows in connection.members.values() for m in rows
                      if m['identity'] != connection.identity}
            for identity, display in people.items():
                print(display + ' — ' + identity, file=sys.stderr)
            operators = [prompt('Operator account identity')]
            choices['operator_identities'] = operators
        admins = choices.get('admin_identities', operators)
        if not operators or not admins:
            raise SetupError('operator_required', 'Supply operator_identities and explicit administrator identities.')
        extra['group_allow_from'] = ['*'] if access == 'anyone' else operators
        extra['group_allow_admin_from'] = admins
        check_policy(connection, extra)
        home = await self.home(choices, record, connection, extra, token)
        mcp = await self.mcp(choices, record, connection, base, name, token)
        check_activity(connection)
        Settings.load(extra)
        record['candidate'] = {'platform': {**old['platform'], 'enabled': True},
                               'extra': {k: v for k, v in extra.items() if k in old['extra'] or k in (
                                   'base_url', 'api_key_env', 'discovery', 'chats', 'group_allow_from',
                                   'group_allow_admin_from', 'activity_status')}, 'mcp': mcp}
        if home:
            record['candidate']['platform']['home_channel'] = home
        else:
            record['candidate']['platform'].pop('home_channel', None)
        record['choices'] = deepcopy(choices)
        record['step'] = 'prepared'
        self.store.save(record)
        return await self.finish(record, choices)

    async def home(self, choices, record, connection, extra, token):
        choice = choices.get('home')
        old = record['prior_owned']['platform'].get('home_channel')
        if choice is None and self.interactive:
            action = choose('Home chat', ['keep', 'select', 'create', 'skip'] if old else ['select', 'create', 'skip'], 'keep' if old else 'skip')
            choice = {'action': action}
            if action in {'select', 'create'}:
                choice['space_id'] = prompt('Home full space ID', connection.spaces[0]['id'])
            if action == 'select':
                for row in connection.chats.get(choice['space_id'], []):
                    print(row.get('name', '') + ' — ' + row['id'], file=sys.stderr)
                choice['chat_id'] = prompt('Home chat ID')
            choices['home'] = choice
        if not isinstance(choice, dict) or choice.get('action') not in {'keep', 'select', 'create', 'skip'}:
            raise SetupError('home_choice', 'Choose home.action keep, select, create, or skip.')
        action = choice['action']
        if action == 'skip':
            return None
        if action == 'keep':
            if not old:
                raise SetupError('home_choice', 'There is no existing home to keep; choose select, create, or skip.')
            target = Target.parse(old['chat_id'])
        else:
            space = choice.get('space_id')
            if space not in connection.chats:
                raise SetupError('home_space', 'The home must belong to a validated space.')
            if action == 'create':
                operation = record['operations'].get('home')
                if operation and operation.get('state') != 'complete':
                    raise SetupError('home_ambiguous', 'Home creation was interrupted; select the already-created chat by ID after inspection.')
                if not operation:
                    if 'create_chat' not in connection.operations:
                        raise SetupError('home_capability', 'This backend does not report chat creation.', state='incompatible')
                    operation = {'id': record['id'] + '-home', 'space_id': space, 'state': 'pending'}
                    record['operations']['home'] = operation
                    self.store.save(record)
                    client = Client(extra['base_url'], token, transport=self.api_transport)
                    try:
                        result = await client.request('POST', '/v2/spaces/' + quote(space, safe='') + '/chats',
                                                       body={'name': 'Hermes Home'}, key=operation['id'])
                        target = Target(space, result.get('id'))
                        operation.update(state='complete', chat_id=target.chat_id)
                        record['changed'] = True
                        self.store.save(record)
                    finally:
                        await client.close()
                target = Target(space, operation['chat_id'])
                connection.chats[space].append({'id': target.chat_id, 'name': 'Hermes Home'})
            else:
                target = Target(space, choice.get('chat_id'))
        if target.space_id not in connection.chats or target.chat_id not in {c['id'] for c in connection.chats[target.space_id]}:
            raise SetupError('home_unavailable', 'The chosen home is not a confirmed accessible chat.')
        pin = {'space_id': target.space_id, 'chat_id': target.chat_id}
        if pin not in extra['chats']:
            extra['chats'].append(pin)
        return {'platform': 'anytype', 'chat_id': target.key, 'name': 'Hermes Home'}

    async def mcp(self, choices, record, connection, base, key_env, token):
        choice = choices.get('mcp')
        old = record['prior_owned']['mcp']
        if choice is None and self.interactive:
            mode = choose('Anytype MCP tools', ['keep', 'http', 'wrapper', 'none'] if old else ['http', 'wrapper', 'none'], 'keep' if old else 'none')
            choice = {'mode': mode}
            if mode == 'http':
                reported = record.get('native', {}).get('mcp', {})
                if reported:
                    choice.update(reported)
                else:
                    choice['url'] = safe_url(prompt('Backend-reported HTTP MCP URL'))
                    choice['shared_api_key'] = choose('Does the backend report shared API-key authentication?', ['yes', 'no'], 'no') == 'yes'
            choices['mcp'] = choice
        if not isinstance(choice, dict) or choice.get('mode') not in {'keep', 'http', 'wrapper', 'none'}:
            raise SetupError('mcp_choice', 'Choose mcp.mode keep, http, wrapper, or none.')
        mode = choice['mode']
        if mode == 'http' and record.get('native', {}).get('mcp'):
            reported = record['native']['mcp']
            if (choice.get('url', reported['url']) != reported['url'] or
                    choice.get('shared_api_key', True) is not True or choice.get('token_ref')):
                raise SetupError('native_mcp', 'Use the native-reported HTTP MCP endpoint and shared integration key.')
            choice.update(reported)
        conflict_action = 'remove' if mode == 'none' else 'replace'
        conflict_needed = old and mode != 'keep' and (mode != 'none' or old.get('enabled') is not False)
        if conflict_needed and 'conflict' not in choice and self.interactive:
            choice['conflict'] = choose('Existing Anytype MCP connection', [conflict_action, 'keep'], 'keep')
        if conflict_needed and choice.get('conflict') == 'keep':
            raise SetupError('mcp_keep_existing', 'The existing connection is retained; this candidate has not been activated.',
                             next_action='Cancel the candidate, or explicitly resolve the MCP replacement/removal choice.')
        if mode == 'none':
            if old and old.get('enabled') is not False and choice.get('conflict') != 'remove':
                raise SetupError('mcp_conflict', 'Chat-only setup must explicitly remove the existing Anytype MCP connection.',
                                 next_action='Set mcp.conflict to remove, or keep the existing connection and cancel this candidate.')
            return None
        if mode == 'keep':
            if not old:
                raise SetupError('mcp_missing', 'There is no existing MCP connection to keep.')
            server = deepcopy(old)
        elif mode == 'wrapper':
            server = wrapper_config(base, key_env)
        else:
            url = choice.get('url')
            if not url:
                raise SetupError('mcp_endpoint', 'Supply the actual HTTP MCP URL reported by the backend; no endpoint is assumed.')
            if choice.get('shared_api_key') is True:
                name = key_env
            else:
                reference = choice.get('token_ref')
                value = self.secret_reader(reference) if reference else self.credential({}, 'mcp_token_ref', 'MCP integration token')
                name = self.stage_credential(record, 'MCP_TOKEN', value)
            server = {'url': url, 'headers': {'Authorization': 'Bearer ${' + name + '}'}}
        if mode != 'keep' and old:
            if choice.get('conflict') != 'replace':
                raise SetupError('mcp_conflict', 'An Anytype MCP server already exists; explicitly choose replacement or keep the prior connection.')
            for key in ('tools', 'timeout', 'startup_timeout', 'connect_timeout'):
                if key in old:
                    server[key] = deepcopy(old[key])
        server = safe_mcp(server)
        result = await inspect_mcp(server, connection, lambda name: self.secret(name, record),
                                   transport=self.mcp_transport, api_transport=self.api_transport)
        if result['state'] not in {'ready', 'disabled'}:
            raise SetupError('mcp_not_ready', 'MCP failed independent tool/account/grant validation; the candidate remains unpublished.',
                state=result['state'], next_action='Fix MCP, or explicitly choose mcp.mode none and mcp.conflict remove for chat-only setup.')
        if old and old.get('enabled') is not False and result['state'] == 'ready':
            prior = record.get('prior_connection', {})
            prior_mcp = prior.get('mcp_readiness', {})
            if prior_mcp.get('state') != 'ready':
                old_connection = metadata_connection(prior) if prior else connection
                prior_mcp = await inspect_mcp(old, old_connection, self.secret,
                    transport=self.mcp_transport, api_transport=self.api_transport)
                if prior and prior_mcp.get('state') == 'ready':
                    prior['mcp_readiness'] = prior_mcp
            same_endpoint = (server.get('url') == old.get('url') if server.get('url') or old.get('url') else
                server.get('env', {}).get('ANYTYPE_API_BASE_URL') == old.get('env', {}).get('ANYTYPE_API_BASE_URL'))
            if prior_mcp.get('account_identity') == connection.identity:
                require_application_name(result['credential'], prior_mcp.get('credential', {}),
                                         expected_name=choice.get('application_name'))
            elif same_endpoint:
                require_application_name(result['credential'], {}, expected_name=choice.get('application_name'))
        record['mcp_readiness'] = result
        self.store.save(record)
        return server

    def selected_key(self, extra, record=None):
        value = self.secret(api_key_name(extra), record)
        if not value:
            raise SetupError('secret_missing', 'The selected Anytype integration credential is unavailable in the active secret scope.')
        return value

    async def validate_candidate(self, record):
        candidate = record['candidate']
        if candidate['platform'].get('enabled'):
            key = self.selected_key(candidate['extra'], record)
            connection = await inspect_api(candidate['extra'], key, expected_identity=record.get('account_identity'),
                requested_grant=record.get('grant'), home=candidate['platform'].get('home_channel'), transport=self.api_transport)
            check_policy(connection, candidate['extra'])
            check_activity(connection)
            require_application_name(connection.key, record.get('credential', {}))
        elif candidate['mcp'] and candidate['mcp'].get('enabled') is not False:
            connection = self.mcp_rollback_connection(record.get('mcp_expected_connection'))
        else:
            return
        result = await inspect_mcp(candidate['mcp'], connection,
            lambda name: self.secret(name, record), transport=self.mcp_transport, api_transport=self.api_transport)
        if result['state'] not in {'ready', 'disabled'}:
            raise SetupError('mcp_not_ready', 'Prepared MCP connection no longer validates; live configuration is unchanged.', state=result['state'])
        if result['state'] == 'ready':
            require_application_name(result['credential'], record.get('mcp_readiness', {}).get('credential', {}))
        record.update(connection_metadata(connection))
        record['mcp_readiness'] = result
        self.store.save(record)

    async def finish(self, record, choices):
        activation = self.need(choices, 'activation', 'Publication (defer keeps live config unchanged)', 'defer', ['defer', 'save_stopped', 'activate'])
        if activation not in {'defer', 'save_stopped', 'activate'}:
            raise SetupError('activation', 'Choose defer, save_stopped, or activate.')
        if activation != 'defer' and self.interactive and not choices.get('offline_boundary') and not self.lifecycle:
            print('Publication affects this profile’s gateway and every client of its Anytype MCP server. '
                  'Close other MCP clients and prevent concurrent starts or config edits until setup finishes.', file=sys.stderr)
            strategy = choose('Gateway coordination', ['managed', 'already_stopped', 'defer'], 'defer')
            if strategy == 'defer':
                choices['activation'] = activation = 'defer'
            elif choose('Other clients are stopped and you maintain exclusive access', ['yes', 'no'], 'no') == 'yes':
                choices['manage_gateway'] = strategy == 'managed'
                choices['offline_boundary'] = {'profile_home': str(self.store.home), 'exclusive': True,
                                               'mcp_clients_stopped': True, 'gateway_stopped': strategy == 'already_stopped'}
        record['choices'] = deepcopy(choices)
        self.store.save(record)
        # Revalidate on every activation/resume; a prepared endpoint or token can
        # expire or switch accounts while the operator is away.
        if activation != 'defer' and record['step'] == 'prepared':
            await self.validate_candidate(record)
        result = publish(self.store, record, self.boundary(choices), activation=activation)
        result['account_identity'] = record.get('account_identity')
        result['agent_name'] = record.get('observed_name')
        result['mcp'] = record.get('mcp_readiness', {'state': 'disabled'})
        if choices.get('send_test_message') is True:
            if result.get('publication') not in {'committed', 'active'}:
                result['test_message'] = 'deferred_until_commit'
            else:
                result['test_message'] = await self.send_test(record)
        return result

    async def send_test(self, record):
        home = record['candidate']['platform'].get('home_channel')
        if not home:
            return 'no_home_selected'
        operation = record['operations'].get('test_message')
        if operation:
            return operation['state']
        operation = {'id': record['id'] + '-test', 'state': 'unconfirmed'}
        record['operations']['test_message'] = operation
        self.store.save(record)
        extra = record['candidate']['extra']
        client = Client(extra['base_url'], self.selected_key(extra, record), transport=self.api_transport)
        try:
            await client.send(Target.parse(home['chat_id']), {'text': 'Hermes setup connection test.'}, operation['id'])
            operation['state'] = 'sent'
            self.store.save(record)
            return 'sent'
        finally:
            await client.close()

    def cancel(self, record, choices):
        if not record:
            return outcome('ready', next_action=None)
        if record['step'] in {'committed', 'active'}:
            raise SetupError('rollback_required', 'The candidate was committed; use an explicit rollback under the consumer boundary.')
        combined = {**record.get('choices', {}), **choices}
        remaining = restore_consumers(self.store, record, self.boundary(combined))
        if remaining:
            raise SetupError('restore_required', 'Cancellation could not safely restore previous consumers.',
                             next_action='Restore these consumers through Hermes: ' + ', '.join(remaining))
        record['step'] = 'cancelled'
        self.store.save(record)
        return outcome('ready', changed=record.get('changed', False), publication='cancelled',
                       next_action='Live configuration is unchanged; created accounts, memberships, homes and unused credentials are retained.')

    @staticmethod
    def mcp_rollback_connection(previous):
        if not previous or not all(key in previous for key in ('account_identity', 'grant', 'space_ids', 'base_url')):
            raise SetupError('rollback_mcp_unverified', 'The prior MCP-only account and grant were not recorded, so rollback cannot verify that connection.',
                next_action='Keep the current configuration and validate/reconfigure the prior MCP connection through Hermes before restoring it.')
        return metadata_connection(previous)

    async def rollback(self, record, choices, config, revision):
        if not record or record['step'] not in {'committed', 'active'}:
            raise SetupError('rollback_missing', 'There is no committed setup candidate to roll back.')
        if revision != record['committed_revision']:
            raise SetupError('config_changed', 'Configuration changed after setup; automatic rollback would overwrite external edits.')
        rollback_choices = {key: deepcopy(value) for key, value in choices.items()
                            if key in {'schema_version', 'activation', 'offline_boundary', 'manage_gateway'}}
        rollback_choices.setdefault('activation', 'save_stopped')
        rollback_record = {'schema_version': 1, 'id': uuid.uuid4().hex.upper(), 'step': 'prepared',
            'prior_revision': revision, 'prior_owned': owned(config), 'candidate': deepcopy(record['prior_owned']),
            'prior_connection': record_connection(record), 'consumers': [], 'operations': {},
            'choices': rollback_choices, 'changed': False, 'rollback': True}
        # Verify that old referenced credentials are still present. Never revoke
        # either set of credentials when publication or rollback is ambiguous.
        candidate = rollback_record['candidate']
        previous = record.get('prior_connection', {})
        connection = None
        if candidate['platform'].get('enabled'):
            connection = await inspect_api(candidate['extra'], self.selected_key(candidate['extra']),
                expected_identity=previous.get('account_identity', record.get('account_identity')),
                requested_grant=previous.get('grant'), home=candidate['platform'].get('home_channel'), transport=self.api_transport)
            if previous.get('credential'):
                require_application_name(connection.key, previous['credential'])
        elif candidate['mcp'] and candidate['mcp'].get('enabled') is not False:
            # MCP is an independent consumer even when the prior gateway was
            # absent/disabled. Never restore it merely because gateway checks
            # were skipped. Retain validated account/grant proof across resume.
            connection = self.mcp_rollback_connection(previous)
            rollback_record['mcp_expected_connection'] = deepcopy(previous)
        if connection:
            rollback_record.update(connection_metadata(connection))
            rollback_record['mode'] = previous.get('mode', record.get('mode', 'external'))
            mcp = await inspect_mcp(candidate['mcp'], connection, self.secret,
                                   transport=self.mcp_transport, api_transport=self.api_transport)
            if mcp['state'] not in {'ready', 'disabled'}:
                raise SetupError('rollback_mcp', 'The previous MCP connection cannot be safely restored.', state=mcp['state'])
            if mcp['state'] == 'ready' and previous.get('mcp_readiness', {}).get('credential'):
                require_application_name(mcp['credential'], previous['mcp_readiness']['credential'])
            rollback_record['mcp_readiness'] = mcp
            await self.validate_candidate(rollback_record)
        # Keep the caller's recovery view on this attempt too, so a failed
        # rollback stop reports its exact outstanding consumers.
        record.clear()
        record.update(rollback_record)
        self.store.save(record)
        return await self.finish(record, rollback_choices)

    async def verify_native_join_connection(self, record, extra, choices, attempt=None):
        """The selected runtime credential is authoritative, not a stale journal."""
        expected = record.get('account_identity')
        if not expected or (choices.get('expected_account') and choices['expected_account'] != expected):
            raise SetupError('account_mismatch', 'Join requires the current profile’s verified account identity.')
        current = await inspect_api(extra, resolve_api_key(extra), expected_identity=expected,
                                    requested_grant=record.get('grant'), transport=self.api_transport)
        native = record.get('native', {})
        if (not native.get('binding') or native.get('account_id') != expected or
                current.key.get('id') != native.get('credential_id') or
                current.key.get('name') != native.get('application_name')):
            raise SetupError('native_provenance', 'The current profile credential does not match its recorded native connection; repair setup before joining.')
        if current.base_url != record.get('base_url') or choices.get('base_url', current.base_url) != current.base_url:
            raise SetupError('native_origin', 'The current profile origin differs from the recorded native connection; repair setup before joining.')
        if choices.get('cli', record.get('choices', {}).get('cli', {})) != record.get('choices', {}).get('cli', {}):
            raise SetupError('native_provenance', 'Join must preserve the recorded native executable and instance selection.')
        if attempt:
            prior = attempt.get('prior_connection', {})
            if (prior.get('account_identity') != current.identity or prior.get('base_url') != current.base_url or
                    prior.get('native', {}).get('binding') != native['binding'] or
                    prior.get('credential') != current.key):
                raise SetupError('native_provenance', 'The pending join belongs to a different connection; preserve its receipt before repair.')
        return current

    async def join(self, choices=None, *, resume=False):
        choices = deepcopy(choices or {})
        join_attempt = None
        try:
            choices = safe_choices(choices)
            previous = self.store.load()
            if (previous and previous.get('join_followup') and
                    previous['step'] not in {'committed', 'active', 'cancelled'}):
                result = await self.run(choices, resume=True)
                result['space_id'] = previous['join_followup']
                result['membership'] = 'joined'
                if result.get('publication') == 'prepared':
                    result['gateway'] = 'joined; gateway restart required'
                return result
            with self.store.locked():
                config, revision = self.store.snapshot()
                record = self.store.load()
                if not record:
                    raise SetupError('setup_required', 'Run Anytype setup before the space-join workflow.')
                if record['step'] not in {'committed', 'active'}:
                    raise SetupError('setup_incomplete', 'Resume or cancel the current setup candidate before joining another space.')
                platform = config.get('gateway', {}).get('platforms', {}).get('anytype', {})
                extra = platform.get('extra', {})
                mode = choices.get('mode', record.get('mode', 'desktop'))
                if mode != record.get('mode', 'desktop'):
                    raise SetupError('join_backend', 'Join must use this connection’s recorded backend; change connections through setup.')
                action = choices.get('discovery_action')
                saved_join = record.get('join_attempt')
                join_attempt = saved_join
                if resume and mode == 'cli' and not saved_join:
                    raise SetupError('no_join', 'There is no native join attempt to resume.')
                if saved_join:
                    prior_choices = saved_join.get('choices', {})
                    choices = {**prior_choices, **choices}
                    action = choices.get('discovery_action')
                if action is None and self.interactive:
                    action = choose('Gateway discovery after joining', ['retain', 'switch', 'all'], 'retain')
                    choices['discovery_action'] = action
                if action not in {'retain', 'switch', 'all'}:
                    raise SetupError('discovery_choice', 'Choose discovery_action retain, switch, or all before joining.')
                self.validate_decisions(choices)
                space_id = choices.get('joined_space_id')
                if space_id:
                    Target(space_id, 'validate')
                if mode == 'cli':
                    # Managed leaves override the raw profile at runtime; native
                    # membership must never act on a shadowed connection.
                    self.store.require_config_ownership()
                    await self.verify_native_join_connection(record, extra, choices, saved_join)
                    if saved_join and saved_join.get('choices', {}).get('discovery_action') != action:
                        raise SetupError('join_choices', 'Resume the original discovery action for this invitation.')
                    if self.interactive and not space_id and not choices.get('invitation_ref') and not saved_join:
                        self.private['invitation'] = prompt('Space invitation', private=True)
                        choices['join_space'] = True
                    if not saved_join:
                        saved_join = {'schema_version': 1, 'id': uuid.uuid4().hex.upper(), 'step': 'native_join',
                            'prior_owned': owned(config), 'prior_connection': record_connection(record),
                            'account_identity': record['account_identity'], 'native': deepcopy(record.get('native', {})),
                            'operations': {}, 'choices': deepcopy(choices), 'changed': False}
                        record['join_attempt'] = saved_join
                        join_attempt = saved_join
                        self.store.save(record)
                    cli = record.get('choices', {}).get('cli', {})
                    native = self.native_factory(cli.get('executable', 'anytype'), cli.get('instance'))
                    native.private = self.private
                    # Keep native operation durability inside the current committed
                    # connection until the ordinary publication candidate begins.
                    class JoinStore:
                        home = self.store.home
                        def save(inner, attempt):
                            record['join_attempt'] = attempt
                            self.store.save(record)
                    result = native.join(choices, saved_join, JoinStore(), self.secret_reader)
                    if asyncio.iscoroutine(result):
                        result = await result
                    space_id = result['space_id']
                elif not space_id:
                    if not self.interactive:
                        raise SetupError('join_in_client', 'Join the space in the desktop or externally managed Anytype client first.',
                                         next_action='Then supply joined_space_id with its full ID to refresh membership.')
                    print('Join the invitation in the Anytype app/service that owns this account; then return here.', file=sys.stderr)
                    space_id = prompt('Joined full space ID (after approval)')
                Target(space_id, 'validate')
                settings = Settings.load(extra)
                token = resolve_api_key(extra)
                client = Client(settings.base_url, token, transport=self.api_transport)
                try:
                    from .diagnostics import normalized_grant
                    grant = normalized_grant(await client.request('GET', '/v2/auth/whoami', params={'spaces': 'true', 'ids': 'full'}))
                    spaces = [row async for row in client.rows('/v2/spaces', ids='full')]
                    visible = space_id in {row.get('id') for row in spaces}
                    if visible:
                        me = await client.request('GET', '/v2/spaces/' + quote(space_id, safe='') + '/members/me', params={'ids': 'full'})
                        if me.get('identity') != record.get('account_identity'):
                            raise SetupError('account_mismatch', 'The joined space resolves to a different account; migration is separate.')
                        await client.request('GET', '/v2/spaces/' + quote(space_id, safe='') + '/chats', params={'ids': 'full', 'include': 'discussions'})
                        await client.request('GET', '/v2/spaces/' + quote(space_id, safe='') + '/members', params={'ids': 'full'})
                finally:
                    await client.close()
                if not visible:
                    if grant['all_spaces']:
                        return outcome('syncing', changed=bool(join_attempt and join_attempt.get('changed')), next_action='Wait for approval/sync, then refresh; do not continually resubmit the invitation.',
                                       membership='not_yet_usable', space_id=space_id)
                    if action == 'retain' and mode != 'cli':
                        return outcome('needs_input', next_action='Inspect native membership in the owning client; the current grant cannot confirm this new space.',
                                       membership='unverified_outside_api_grant', space_id=space_id)
                    if 'api_key_ref' not in choices and mode != 'cli':
                        raise SetupError('grant_refresh', 'Create a replacement integration key with the same application name and expanded grant.',
                                         next_action='Supply api_key_ref for the replacement; it will be staged before policy publication.')
                if action == 'retain' and not settings.discovers(space_id) and space_id not in settings.spaces:
                    if mode == 'cli':
                        record.pop('join_attempt', None)
                        self.store.save(record)
                    return outcome('ready', changed=bool(join_attempt and join_attempt.get('changed')), membership='usable' if visible else 'joined', space_id=space_id,
                                   gateway='joined; outside gateway discovery scope', next_action=None)
                if action == 'retain':
                    scope = {'mode': 'all'} if settings.discovery_all_spaces else {'mode': 'single', 'space_id': settings.discovery_space}
                else:
                    scope = {'mode': 'all'} if action == 'all' else {'mode': 'single', 'space_id': space_id}
                intended_grant = choices.get('api_grant') or {
                    'all_spaces': grant['all_spaces'], 'permission': 'readwrite',
                    'space_ids': [] if grant['all_spaces'] else sorted(set(grant['space_ids']) | {space_id})}
                # Reuse the exact setup publication machinery; joining never
                # edits the live gateway grant/discovery/MCP independently.
                followup = {**record.get('choices', {}), **choices, 'schema_version': 1,
                    'action': 'repair', 'mode': mode,
                    'scope': scope, 'expected_account': record['account_identity'],
                    'api_grant': intended_grant, 'base_url': settings.base_url}
                if 'agent_name' not in choices:
                    followup.pop('agent_name', None)
                for key in ('joined_space_id', 'discovery_action', 'invitation_ref', 'join_space'):
                    followup.pop(key, None)
                if mode == 'cli':
                    followup.pop('api_key_ref', None)
                    followup['account'] = {'action': 'reuse'}
                    followup['service'] = {'action': 'reuse'}
                else:
                    followup['api_key_ref'] = choices.get('api_key_ref', {'env': settings.api_key_env})
                followup['home'] = choices.get('home', {'action': 'keep' if platform.get('home_channel') else 'skip'})
                followup['mcp'] = choices.get('mcp', {'mode': 'keep' if config.get('mcp_servers', {}).get('anytype') else 'none'})
                followup['activation'] = choices.get('activation', 'defer')
                followup.pop('offline_boundary', None)
                if choices.get('offline_boundary'):
                    followup['offline_boundary'] = choices['offline_boundary']
                if mode == 'cli':
                    candidate = self.store.begin(config, revision, prior_connection=record_connection(record), choices=followup)
                    candidate['join_followup'] = space_id
                    candidate['changed'] = bool(join_attempt and join_attempt.get('changed'))
                    candidate['native_reuse_key'] = intended_grant == grant
                    self.store.save(candidate)
            result = await self.run(followup)
            result['space_id'] = space_id
            result['membership'] = 'usable' if visible else 'joined_grant_refresh_required'
            if result.get('publication') == 'prepared':
                result['gateway'] = 'joined; gateway restart required'
            result['chat_enrollment'] = 'After startup discovery, mention/creator/pin rules still determine watched chats.'
            return result
        except SetupError as error:
            return outcome(error.state, changed=bool(join_attempt and join_attempt.get('changed')), error=error, next_action=error.next_action)
        except (EOFError, KeyboardInterrupt):
            return outcome('needs_input', changed=bool(join_attempt and join_attempt.get('changed')), next_action='Resume join to reconcile its existing native operation; no invitation is stored.')
        except Exception:
            error = SetupError('join_unavailable', 'Membership could not be confirmed; no live gateway policy was changed.', state='unavailable')
            return outcome('unavailable', changed=bool(join_attempt and join_attempt.get('changed')), error=error, next_action='Refresh membership after checking the backend and API grant.')
