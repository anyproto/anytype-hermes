"""Bounded, read-only API and MCP probes. Never return bodies or credentials."""
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import json
import os
import re
from urllib.parse import quote, urlsplit

import httpx

from .client import APIError, Client, ProtocolError, parse_sse
from .config import Settings, Target, resolve_api_key
from .setup_store import SetupError, outcome, ENV_REF


REQUIRED_TOOLS = {'search_space', 'get_object', 'create_object', 'patch_object',
                  'list_types', 'list_properties'}


@dataclass
class Connection:
    identity: str
    name: str
    grant: dict
    spaces: list
    members: dict
    chats: dict
    checks: list = field(default_factory=list)
    key: dict = field(default_factory=dict)
    operations: dict = field(default_factory=dict)
    base_url: str = ''

    def report(self):
        return {'account_identity': self.identity, 'agent_name': self.name,
                'grant': self.grant, 'space_ids': [s['id'] for s in self.spaces],
                'checks': self.checks, 'limits': {'discovery_spaces': 16,
                  'source': 'gateway implementation limit; not a universal Heart limit'}}


def normalized_grant(whoami):
    grant = whoami.get('grant')
    if not isinstance(grant, dict) or not isinstance(grant.get('all_spaces'), bool):
        raise SetupError('grant_unknown', 'The backend does not report an explicit API grant.', state='incompatible')
    permission = grant.get('permission')
    if permission not in {'read', 'readwrite'}:
        raise SetupError('grant_unknown', 'The backend reports an unsupported permission.', state='incompatible')
    ids = [row.get('id') for row in grant.get('spaces', []) if isinstance(row, dict)]
    if any(not isinstance(value, str) or not value for value in ids):
        raise SetupError('grant_unknown', 'The backend returned incomplete grant identifiers.', state='incompatible')
    return {'all_spaces': grant['all_spaces'], 'space_ids': [] if grant['all_spaces'] else sorted(ids),
            'permission': permission}


def same_grant(actual, requested):
    return (actual['all_spaces'] == requested['all_spaces'] and
            actual['space_ids'] == sorted(requested.get('space_ids', [])) and
            actual['permission'] == requested.get('permission', 'readwrite'))


def integration_key(whoami):
    key = whoami.get('key', {})
    if not isinstance(key, dict) or not isinstance(key.get('name'), str) or not key['name']:
        raise SetupError('application_identity', 'The backend must report the integration key’s stable application name.', state='incompatible')
    return {name: key.get(name) for name in ('id', 'name')}


def require_application_name(current, previous, *, expected_name=None):
    name = previous.get('name')
    if name and expected_name and name != expected_name:
        raise SetupError('application_name_mismatch', 'The requested application name differs from the previous integration key.')
    name = name or expected_name
    if not name:
        raise SetupError('application_name_unknown', 'The previous integration application name could not be verified.',
                         next_action='Inspect the old integration key and supply its known application_name before repairing.')
    if current.get('name') != name:
        raise SetupError('application_name_mismatch', 'Use the same stable application name when replacing an integration key; object ownership depends on it.')


def check_activity(connection):
    if any(c['name'] == 'activity' and c['state'] == 'incompatible' for c in connection.checks):
        raise SetupError('activity_unavailable', 'Chat is usable, but this backend lacks the optional activity endpoint.',
                         next_action='Explicitly disable activity in a new candidate, or update the backend and resume.')


def check_member_write(me):
    # Heart MemberRow maps participant ACL permissions to these exact role names.
    if me.get('role') in {'editor', 'admin', 'owner'}:
        return
    if me.get('role') in {'viewer', 'no_permission'}:
        raise SetupError('membership_write_required', 'The bot needs editor, admin or owner membership in every selected space.')
    raise SetupError('membership_write_unverified', 'The backend did not report a recognized writable bot membership role.', state='incompatible')


def tool_enabled(name, server):
    """Mirror Hermes include/empty/glob/precedence semantics without private APIs."""
    filters = server.get('tools') or {}
    def normalize(value):
        if isinstance(value, str):
            return {value}
        if isinstance(value, (list, tuple, set)):
            return {str(item) for item in value}
        return set()
    def matches(patterns):
        return name in patterns or any(fnmatchcase(name, pattern) for pattern in patterns
                                       if any(marker in pattern for marker in '*?['))
    include = filters.get('include')
    if isinstance(include, (str, list, tuple, set)):
        return matches(normalize(include))
    return not matches(normalize(filters.get('exclude')))


async def probe_stream(client, method, path, *, body=None, params=None):
    async with asyncio.timeout(12):
        async with client.http.stream(method, path, json=body, params=params) as response:
            if response.status_code != 200:
                raise APIError(response.status_code)
            if 'text/event-stream' not in response.headers.get('content-type', ''):
                raise ProtocolError('Required endpoint did not serve SSE')
            # Headers are sufficient for route/auth/capacity validation. Close
            # without consuming or exposing the initial chat-message window.


async def inspect_api(extra, token, *, expected_identity=None, requested_grant=None,
                      home=None, transport=None):
    settings = Settings.load(extra)
    client = Client(settings.base_url, token, transport=transport)
    try:
        whoami = await client.request('GET', '/v2/auth/whoami', params={'spaces': 'true', 'ids': 'full'})
        grant = normalized_grant(whoami)
        key = integration_key(whoami)
        if grant['permission'] != 'readwrite':
            raise SetupError('write_required', 'Gateway replies and activity require an API grant with readwrite permission.')
        if requested_grant and not same_grant(grant, requested_grant):
            raise SetupError('grant_mismatch', 'The candidate credential does not match the requested space grant.')
        spaces = [row async for row in client.rows('/v2/spaces', ids='full')]
        ids = []
        for space in spaces:
            ids.append(Target(space.get('id'), 'validate').space_id)
        if len(ids) != len(set(ids)):
            raise ProtocolError('Duplicate full space identifiers')
        selected = ids if settings.discovery_all_spaces else list(settings.spaces)
        if not selected:
            raise SetupError('space_syncing', 'No accessible spaces are usable yet.', state='syncing',
                             next_action='Wait for membership approval/sync and resume; do not resubmit the invitation.')
        if any(space not in ids for space in selected):
            raise SetupError('space_unavailable', 'A configured full space ID is not visible to this credential.', state='syncing')
        if settings.discovery_enabled and len(selected) > 16:
            raise SetupError('space_limit', 'This gateway supports at most 16 discovery spaces; narrow the grant.', state='incompatible')
        # Served schema is the capability authority for write routes; do not send
        # dummy messages, objects or activity to discover whether they exist.
        schema = await client.request('GET', '/v2/docs/openapi.json')
        paths = schema.get('paths', {})
        ops = {definition.get('operationId'): {'path': path, 'method': method}
               for path, methods in paths.items() for method, definition in methods.items()
               if isinstance(definition, dict) and definition.get('operationId')}
        message_path = '/v2/spaces/{space_id}/chats/{chat_id}/messages'
        if 'post' not in paths.get(message_path, {}):
            raise SetupError('message_write_missing', 'The served API schema does not expose message creation.', state='incompatible')
        activity = 'post' in paths.get('/v2/spaces/{space_id}/chats/{chat_id}/status', {})
        checks = [{'name': 'api_grant', 'state': 'ready'}, {'name': 'activity',
            'state': 'ready' if activity and settings.activity_status else
                     'disabled' if not settings.activity_status else 'incompatible'}]
        identities, members, chats, names = set(), {}, {}, []
        for space in selected:
            root = '/v2/spaces/' + quote(space, safe='')
            me = await client.request('GET', root + '/members/me', params={'ids': 'full'})
            Target(space, me.get('id'))
            if not isinstance(me.get('identity'), str) or not me['identity']:
                raise ProtocolError('The bot member lacks account identity')
            check_member_write(me)
            identities.add(me['identity'])
            names.append(me.get('name', ''))
            members[space] = [row async for row in client.rows(root + '/members', ids='full')]
            for member in members[space]:
                Target(space, member.get('id'))
                if not member.get('identity'):
                    raise ProtocolError('A member lacks account identity')
            chats[space] = [row async for row in client.rows(root + '/chats', include='discussions', ids='full')]
            for chat in chats[space]:
                Target(space, chat.get('id'))
                if chat.get('kind') == 'discussion':
                    Target(space, chat.get('parent_id'))
            required_chats = {t.chat_id for t in settings.targets if t.space_id == space}
            if home and Target.parse(home['chat_id']).space_id == space:
                required_chats.add(Target.parse(home['chat_id']).chat_id)
            available = {row['id'] for row in chats[space]}
            if not required_chats <= available:
                raise SetupError('home_or_pin_missing', 'A pinned/home chat is not accessible in its full space.')
            if settings.discovery_enabled:
                await probe_stream(client, 'GET', root + '/chats/stream', params={'include': 'discussions', 'ids': 'full'})
                if settings.watch_agent_objects:
                    await probe_stream(client, 'POST', root + '/search/stream',
                        body={'filter': 'creator = ' + json.dumps(me['id']), 'fields': ['name', 'discussion']},
                        params={'ids': 'full'})
            # Exercise one available chat plus each selected pin. No chat is
            # created solely for a diagnostic, and absent chat surfaces are not
            # falsely reported as live-verified.
            sampled = required_chats | ({chats[space][0]['id']} if chats[space] else set())
            for chat in chats[space]:
                if chat['id'] not in sampled:
                    continue
                path = root + '/chats/' + quote(chat['id'], safe='')
                await client.request('GET', path + '/messages', params={'limit': 1})
                await probe_stream(client, 'GET', path + '/messages/stream', params={'limit': 1})
                if chat.get('kind') == 'discussion':
                    await client.request('GET', root + '/objects/' + quote(chat['parent_id'], safe=''),
                                         params={'format': 'md', 'ids': 'full'})
            checks.append({'name': 'space', 'space_id': space, 'state': 'ready',
                           'messages': 'ready' if sampled else 'schema_only_no_chats'})
        if len(identities) != 1:
            raise SetupError('account_mismatch', 'Spaces resolve to different bot accounts.', state='incompatible')
        identity = identities.pop()
        if expected_identity and identity != expected_identity:
            raise SetupError('account_mismatch', 'The endpoint is logged in to a different account; account migration is a separate operation.')
        if home and Target.parse(home['chat_id']).space_id not in selected:
            raise SetupError('home_scope', 'The home belongs to a space outside gateway discovery.')
        selected_spaces = [row for row in spaces if row['id'] in selected]
        return Connection(identity, names[0], grant, selected_spaces, members, chats, checks,
                          key, ops, settings.base_url)
    except APIError as error:
        if error.status in {401, 403}:
            raise SetupError('credential_rejected', f'The API rejected the credential or grant (HTTP {error.status}).') from error
        if error.status in {404, 405}:
            raise SetupError('api_capability', 'A required API v2 route is unavailable.', state='incompatible') from error
        raise SetupError('api_unavailable', f'The API is temporarily unavailable (HTTP {error.status}).', state='unavailable') from error
    except (httpx.HTTPError, TimeoutError) as error:
        raise SetupError('backend_unavailable', 'The API cannot be reached from this Hermes process; check the origin and service/account readiness.', state='unavailable') from error
    except (ProtocolError, KeyError, TypeError, ValueError) as error:
        raise SetupError('api_protocol', 'The backend returned an incompatible API schema or identifier.', state='incompatible') from error
    finally:
        await client.close()


def check_policy(connection, extra):
    settings = Settings.load(extra)
    confirmed = {row['identity'] for members in connection.members.values() for row in members}
    if not settings.allow or not settings.admins:
        raise SetupError('operator_required', 'Choose explicit operator/admin account identities; unresolved access stays denied.')
    if not settings.admins <= confirmed or (settings.allow != {'*'} and not settings.allow <= confirmed):
        raise SetupError('operator_unconfirmed', 'An operator/admin identity is not confirmed in the accessible membership list.')
    if connection.identity in settings.admins:
        raise SetupError('operator_is_bot', 'Select the operator’s other account as administrator, rather than the bot account.')


def expanded(value, secrets):
    if isinstance(value, str):
        def replace(match):
            token = secrets(match.group(1))
            if not token:
                raise SetupError('mcp_credential_missing', 'The selected MCP secret reference is missing.')
            return token
        return ENV_REF.sub(replace, value)
    return value


class MCPProbe:
    """Minimal bounded JSON-RPC client for initialization and read-only checks."""
    def __init__(self, server, secrets, *, transport=None):
        self.server, self.secrets, self.transport = server, secrets, transport
        self.http = None
        self.process = None
        self.session = None
        self.sequence = 0
        self.version = '2025-11-25'

    async def __aenter__(self):
        if self.server.get('url'):
            parsed = urlsplit(self.server['url'])
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise SetupError('mcp_url', 'Use the backend-reported HTTP MCP URL without URL credentials.')
            headers = {key: expanded(value, self.secrets) for key, value in self.server.get('headers', {}).items()}
            self.http = httpx.AsyncClient(headers=headers, timeout=12, trust_env=False,
                                          follow_redirects=False, transport=self.transport)
        else:
            # --offline/--no-install avoid installation as a diagnostic side effect.
            if self.server.get('command') != 'npx' or self.server.get('args') != ['-y', '@anyproto/anytype-mcp@2.0.1']:
                raise SetupError('mcp_wrapper', 'Only the explicit documented Node wrapper is supported for setup validation.', state='incompatible')
            env = {key: os.environ[key] for key in ('PATH', 'HOME', 'SYSTEMROOT', 'TEMP', 'TMPDIR') if key in os.environ}
            env.update({key: expanded(value, self.secrets) for key, value in self.server.get('env', {}).items()})
            try:
                self.process = await asyncio.create_subprocess_exec('npx', '--no-install', '--offline',
                    '@anyproto/anytype-mcp@2.0.1', stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env, limit=2_000_000)
            except OSError as error:
                raise SetupError('mcp_wrapper_missing', 'Install the documented Node wrapper explicitly, then resume.', state='unavailable') from error
        return self

    async def __aexit__(self, *args):
        if self.http:
            if self.session:
                with suppress(Exception):
                    await self.http.delete(self.server['url'], headers={'Mcp-Session-Id': self.session})
            await self.http.aclose()
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 2)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()

    async def rpc(self, method, params=None, *, notification=False):
        self.sequence += 1
        request = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
        if not notification:
            request['id'] = self.sequence
        async with asyncio.timeout(15):
            if self.http:
                headers = {'Accept': 'application/json, text/event-stream'}
                # Initialization negotiates the protocol before a transport
                # version header is valid for this server.
                if method != 'initialize':
                    headers['MCP-Protocol-Version'] = self.version
                if self.session:
                    headers['Mcp-Session-Id'] = self.session
                async with self.http.stream('POST', self.server['url'], json=request, headers=headers) as response:
                    if response.status_code not in {200, 202, 204}:
                        raise SetupError('mcp_http', f'MCP rejected the request (HTTP {response.status_code}).', state='unavailable')
                    self.session = response.headers.get('Mcp-Session-Id', self.session)
                    if notification:
                        return {}
                    if 'text/event-stream' in response.headers.get('content-type', ''):
                        async for frame in parse_sse(response.aiter_lines()):
                            if frame.data:
                                value = json.loads(frame.data)
                                if value.get('id') == self.sequence:
                                    break
                        else:
                            raise ProtocolError('No MCP response')
                    else:
                        raw = await response.aread()
                        if len(raw) > 2_000_000:
                            raise ProtocolError('MCP response exceeds limit')
                        value = json.loads(raw)
            else:
                self.process.stdin.write((json.dumps(request) + '\n').encode())
                await self.process.stdin.drain()
                if notification:
                    return {}
                for _ in range(100):
                    line = await self.process.stdout.readline()
                    if not line:
                        raise SetupError('mcp_wrapper_unavailable', 'The cached MCP wrapper could not start; install it explicitly and retry.', state='unavailable')
                    value = json.loads(line)
                    if value.get('id') == self.sequence:
                        break
                else:
                    raise ProtocolError('Too many MCP notifications')
            if value.get('id') != self.sequence or 'error' in value or not isinstance(value.get('result'), dict):
                raise ProtocolError('MCP request failed')
            return value['result']


def tool_result(result):
    if result.get('isError'):
        raise ProtocolError('MCP read failed')
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    for item in result.get('content', []):
        if item.get('type') == 'text':
            with suppress(ValueError):
                value = json.loads(item['text'])
                if isinstance(value, dict):
                    return value
    raise ProtocolError('MCP read is not structured')


def heart_full_binding(server, connection, initialized):
    """The pinned Heart HTTP full contract forwards this exact bearer to /v2.

    Generic external MCP servers must prove their grant through their own tool;
    a convenient neighboring REST endpoint is not a grant authority for them.
    """
    url, origin = urlsplit(server.get('url', '')), urlsplit(connection.base_url)
    return (bool(origin.hostname) and (url.scheme, url.hostname, url.port) == (origin.scheme, origin.hostname, origin.port)
            and url.path == '/mcp/full' and not url.query and not url.fragment
            and initialized.get('serverInfo') == {'name': 'anytype', 'version': '1'}
            and initialized.get('protocolVersion') in {'2025-06-18', '2025-11-25'})


async def inspect_mcp(server, connection, secrets, *, transport=None, api_transport=None):
    if not server or server.get('enabled') is False:
        return {'state': 'disabled'}
    try:
        async with MCPProbe(server, secrets, transport=transport) as probe:
            initialized = await probe.rpc('initialize', {'protocolVersion': probe.version, 'capabilities': {},
                                          'clientInfo': {'name': 'hermes-anytype-doctor', 'version': '1'}})
            probe.version = initialized['protocolVersion']
            if probe.version not in {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'}:
                raise SetupError('mcp_protocol', 'MCP negotiated an unsupported protocol revision.', state='incompatible')
            heart_full = heart_full_binding(server, connection, initialized)
            await probe.rpc('notifications/initialized', notification=True)
            tools, cursor, seen = {}, None, set()
            for _ in range(30):
                page = await probe.rpc('tools/list', {'cursor': cursor} if cursor else {})
                for tool in page.get('tools', []):
                    name = tool['name']
                    normalized = name.removeprefix('API-').replace('-', '_')
                    tools[normalized] = name
                cursor = page.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise ProtocolError('MCP pagination repeated')
                seen.add(cursor)
            else:
                raise ProtocolError('MCP tool list exceeds limit')
            # Tool preferences apply to the bundled skill too, not just discovery.
            usable = {op for op, name in tools.items() if tool_enabled(name, server)}
            required = REQUIRED_TOOLS | {'get_member_me'} | (set() if heart_full else {'auth_whoami'})
            missing = sorted(required - usable)
            if missing:
                return {'state': 'incompatible', 'missing_operations': missing}
            if heart_full:
                authorization = probe.http.headers.get('Authorization', '')
                if not authorization.startswith('Bearer ') or not authorization[7:]:
                    raise SetupError('mcp_auth', 'Heart HTTP MCP requires a bearer integration credential.')
                client = Client(connection.base_url, authorization[7:], transport=api_transport)
                try:
                    whoami = await client.request('GET', '/v2/auth/whoami', params={'spaces': 'true', 'ids': 'full'})
                finally:
                    await client.close()
            else:
                whoami = tool_result(await probe.rpc('tools/call', {'name': tools['auth_whoami'],
                                                    'arguments': {'spaces': True, 'ids': 'full'}}))
            grant = normalized_grant(whoami)
            key = integration_key(whoami)
            if not same_grant(grant, connection.grant):
                raise SetupError('mcp_grant_mismatch', 'MCP tools use a different access grant from the gateway.', state='incompatible')
            for space in connection.spaces:
                me = tool_result(await probe.rpc('tools/call', {'name': tools['get_member_me'],
                                           'arguments': {'space_id': space['id']}}))
                if me.get('identity') != connection.identity:
                    raise SetupError('mcp_account_mismatch', 'MCP tools resolve to a different bot account.', state='incompatible')
                check_member_write(me)
            return {'state': 'ready', 'tool_count': len(tools), 'account_identity': connection.identity,
                    'grant': grant, 'credential': key}
    except SetupError as error:
        return {'state': error.state, 'error': {'type': error.code, 'message': str(error)}}
    except (OSError, TimeoutError, httpx.HTTPError, APIError, ProtocolError, ValueError, KeyError, TypeError):
        return {'state': 'unavailable', 'error': {'type': 'mcp_probe', 'message': 'MCP initialization or read-only tool checks failed.'}}


async def doctor(config, *, api_transport=None, mcp_transport=None):
    platform = config.get('gateway', {}).get('platforms', {}).get('anytype')
    if not platform:
        return outcome('needs_input', next_action='Run hermes anytype setup to prepare a connection.', gateway='not_configured', mcp='not_checked')
    try:
        extra = platform.get('extra', {})
        connection = await inspect_api(extra, resolve_api_key(extra), home=platform.get('home_channel'), transport=api_transport)
        check_policy(connection, extra)
        from gateway.platforms._shared import get_scoped_secret
        mcp = await inspect_mcp(config.get('mcp_servers', {}).get('anytype'), connection,
                                lambda name: get_scoped_secret(name, '', external_fallback=True), transport=mcp_transport,
                                api_transport=api_transport)
        missing_activity = any(c['name'] == 'activity' and c['state'] == 'incompatible' for c in connection.checks)
        state = 'ready' if mcp['state'] in {'ready', 'disabled'} and not missing_activity else 'needs_input'
        return outcome(state, gateway='ready', mcp=mcp, **connection.report(),
            next_action=None if state == 'ready' else 'Resolve the reported MCP/activity capability, or explicitly choose chat-only/disable activity in setup.')
    except SetupError as error:
        return outcome(error.state, error=error, next_action=error.next_action, gateway=error.state, mcp='not_checked')
    except (TypeError, ValueError):
        error = SetupError('configuration', 'The Anytype configuration or selected secret is invalid.')
        return outcome(error.state, error=error, next_action=error.next_action, gateway='needs_input', mcp='not_checked')
