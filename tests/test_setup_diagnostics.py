import json
from pathlib import Path

import httpx
import pytest

from anytype_plugin.diagnostics import inspect_api, inspect_mcp, doctor, REQUIRED_TOOLS
from anytype_plugin.setup_store import SetupError


class FakeAPI:
    def __init__(self, *, spaces=('space.full',), identity='bot-key', name='Hermes', permission='readwrite',
                 all_spaces=True, activity=True, role='editor'):
        self.spaces, self.identity, self.name = spaces, identity, name
        self.permission, self.all_spaces, self.activity = permission, all_spaces, activity
        self.role = role
        self.requests = []
        self.created = []
        self.schema = json.loads((Path(__file__).resolve().parents[1] / 'docs/anytype-openapi-v2.json').read_text())
        if not activity:
            self.schema['paths'].pop('/v2/spaces/{space_id}/chats/{chat_id}/status')
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path, dict(request.headers)))
        if path == '/v2/auth/whoami':
            return httpx.Response(200, json={'grant': {'all_spaces': self.all_spaces, 'permission': self.permission,
                'spaces': [{'id': s} for s in self.spaces]}, 'key': {'id': 'key-id', 'name': 'Hermes'}})
        if path == '/v2/docs/openapi.json':
            return httpx.Response(200, json=self.schema)
        if path == '/v2/spaces':
            return httpx.Response(200, json={'data': [{'id': s, 'name': 'Space'} for s in self.spaces]})
        if path.endswith('/members/me'):
            return httpx.Response(200, json={'id': 'bot', 'identity': self.identity, 'name': self.name, 'role': self.role})
        if path.endswith('/members'):
            return httpx.Response(200, json={'data': [{'id': 'bot', 'identity': self.identity, 'name': self.name},
                {'id': 'alice', 'identity': 'alice-key', 'name': 'Alice'}]})
        if path.endswith('/stream'):
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=b': heartbeat\n\n')
        if path.endswith('/messages'):
            if request.method == 'POST':
                self.created.append(path)
                return httpx.Response(201, json={'id': 'message1'})
            return httpx.Response(200, json={'messages': [], 'has_more': False})
        if path.endswith('/chats'):
            if request.method == 'POST':
                self.created.append(path)
                return httpx.Response(201, json={'id': 'new-home'})
            return httpx.Response(200, json={'data': [{'id': 'chat', 'kind': 'chat', 'name': 'General'},
                *([{'id': 'new-home', 'kind': 'chat', 'name': 'Hermes Home'}] if self.created else [])]})
        return httpx.Response(404)


def extra(**overrides):
    return {'base_url': 'http://127.0.0.1:31009', 'discovery': {'spaces': 'all'}, 'chats': [],
            'group_allow_from': ['alice-key'], 'group_allow_admin_from': ['alice-key'], **overrides}


async def test_api_readiness_is_read_only_and_discovers_full_ids():
    api = FakeAPI()
    result = await inspect_api(extra(), 'secret', transport=api.transport)
    assert result.identity == 'bot-key'
    assert result.grant == {'all_spaces': True, 'space_ids': [], 'permission': 'readwrite'}
    assert all(method == 'GET' or path.endswith('/search/stream') for method, path, _ in api.requests)
    assert not api.created
    assert 'secret' not in json.dumps(result.report())
    assert result.report()['limits']['discovery_spaces'] == 16


@pytest.mark.parametrize('change,code', [({'permission': 'read'}, 'write_required'),
                                         ({'identity': 'intruder'}, 'account_mismatch'),
                                         ({'spaces': ()}, 'space_syncing'),
                                         ({'spaces': tuple('space.' + str(i) for i in range(17))}, 'space_limit')])
async def test_api_failures_are_typed(change, code):
    api = FakeAPI(**change)
    with pytest.raises(SetupError) as result:
        await inspect_api(extra(), 'secret', expected_identity='bot-key', transport=api.transport)
    assert result.value.code == code


async def test_missing_activity_is_separate_from_gateway_readiness():
    result = await doctor({'gateway': {'platforms': {'anytype': {'extra': extra()}}}}, api_transport=FakeAPI(activity=False).transport)
    assert result['state'] == 'needs_input'
    assert result['gateway'] == 'ready'
    assert {'name': 'activity', 'state': 'incompatible'} in result['checks']


class FakeMCP:
    def __init__(self, *, identity='bot-key', all_spaces=True, missing=(), key_name='Hermes', role='editor'):
        self.identity, self.all_spaces, self.missing = identity, all_spaces, missing
        self.key_name = key_name
        self.role = role
        self.methods = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        if request.method == 'DELETE':
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body['method']
        self.methods.append(method)
        if method == 'notifications/initialized':
            return httpx.Response(202)
        if method == 'initialize':
            result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'API-' + name.replace('_', '-'), 'inputSchema': {'type': 'object'}}
                for name in REQUIRED_TOOLS | {'auth_whoami', 'get_member_me'} if name not in self.missing]}
        else:
            name = body['params']['name']
            data = ({'grant': {'all_spaces': self.all_spaces, 'permission': 'readwrite', 'spaces': []},
                     'key': {'id': 'mcp-key-id', 'name': self.key_name}}
                    if name == 'API-auth-whoami' else {'identity': self.identity, 'role': self.role})
            result = {'structuredContent': data}
        return httpx.Response(200, headers={'Mcp-Session-Id': 'session'}, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})


@pytest.mark.parametrize('options,state', [({}, 'ready'), ({'identity': 'different'}, 'incompatible'),
    ({'all_spaces': False}, 'incompatible'), ({'missing': ['create_object']}, 'incompatible')])
async def test_mcp_checks_bundled_skill_and_account_scope(options, state):
    connection = await inspect_api(extra(), 'secret', transport=FakeAPI().transport)
    mcp = FakeMCP(**options)
    result = await inspect_mcp({'url': 'http://127.0.0.1:31012/mcp',
        'headers': {'Authorization': 'Bearer ${ANYTYPE_SELECTED}'}}, connection,
        lambda name: 'private-token', transport=mcp.transport)
    assert result['state'] == state
    assert 'private-token' not in json.dumps(result)
    assert set(mcp.methods) <= {'initialize', 'notifications/initialized', 'tools/list', 'tools/call'}


async def test_doctor_never_repairs_and_rejects_unresolved_operator():
    api = FakeAPI()
    result = await doctor({'gateway': {'platforms': {'anytype': {'extra': extra(group_allow_from=['unknown'])}}}}, api_transport=api.transport)
    assert result['state'] == 'needs_input'
    assert result['error']['type'] == 'operator_unconfirmed'
    assert not api.created


@pytest.mark.parametrize('role', ['editor', 'admin', 'owner'])
async def test_writable_membership_roles(role):
    result = await inspect_api(extra(), 'secret', transport=FakeAPI(role=role).transport)
    assert result.identity == 'bot-key'


@pytest.mark.parametrize('role,code', [('viewer', 'membership_write_required'),
    ('no_permission', 'membership_write_required'), (None, 'membership_write_unverified'),
    ('future-role', 'membership_write_unverified')])
async def test_readwrite_api_grant_does_not_override_membership_acl(role, code):
    api = FakeAPI(role=role)
    with pytest.raises(SetupError) as error:
        await inspect_api(extra(), 'secret', transport=api.transport)
    assert error.value.code == code
    assert not api.created


async def test_each_discovery_space_must_have_writable_membership():
    class MixedRoleAPI(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == '/v2/spaces/other.full/members/me':
                data = response.json()
                data['role'] = 'viewer'
                return httpx.Response(200, json=data)
            return response
    with pytest.raises(SetupError) as error:
        await inspect_api(extra(), 'secret', transport=MixedRoleAPI(spaces=('space.full', 'other.full')).transport)
    assert error.value.code == 'membership_write_required'


@pytest.mark.parametrize('filters,ready', [
    ({'include': []}, False),
    ({'include': ['API-*']}, True),
    ({'include': 'API-*', 'exclude': ['*']}, True),
    ({'exclude': ['API-create-*']}, False),
    ({'include': ['API-*-object', 'API-search-space', 'API-list-*', 'API-auth-whoami', 'API-get-member-me']}, True),
])
async def test_mcp_readiness_uses_actual_host_tool_filter(filters, ready):
    from anytype_plugin.diagnostics import tool_enabled
    from tools.mcp_tool_registration import _make_tool_filter
    server = {'url': 'https://mcp.example.test', 'tools': filters}
    host_filter = _make_tool_filter('anytype', server)
    for operation in REQUIRED_TOOLS | {'auth_whoami', 'get_member_me'}:
        name = 'API-' + operation.replace('_', '-')
        assert tool_enabled(name, server) == host_filter(name)
    connection = await inspect_api(extra(), 'secret', transport=FakeAPI().transport)
    result = await inspect_mcp(server, connection, lambda name: 'token', transport=FakeMCP().transport)
    assert result['state'] == ('ready' if ready else 'incompatible'), result


class HeartFullMCP:
    """Pinned Heart 92ed139 HTTP contract, including real flat tool names/schema."""
    fixture = json.loads((Path(__file__).parent / 'fixtures/heart-http-mcp-contract.json').read_text())

    def __init__(self, version='2025-11-25', server_name='anytype'):
        self.version, self.server_name = version, server_name
        self.requests = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request):
        assert request.method == 'POST'  # Full mode is stateless, with no session DELETE.
        body = json.loads(request.content)
        self.requests.append((body, dict(request.headers)))
        method = body['method']
        if method == 'initialize':
            # Pinned Heart rejects a 2025-03-26 HTTP header before initialize.
            assert 'mcp-protocol-version' not in request.headers
            result = {'protocolVersion': self.version, 'capabilities': {'tools': {}},
                      'serverInfo': {'name': self.server_name, 'version': '1'}}
        else:
            assert request.headers['mcp-protocol-version'] == self.version
            if method == 'notifications/initialized':
                return httpx.Response(202)
            if method == 'tools/list':
                assert 'auth_whoami' not in self.fixture['tool_names']
                result = {'tools': [{'name': name} for name in self.fixture['tool_names']]}
            else:
                assert method == 'tools/call'
                assert body['params']['name'] == 'get_member_me'
                schema = self.fixture['get_member_me']['inputSchema']
                arguments = body['params']['arguments']
                assert set(arguments) == set(schema['required'])
                assert arguments['space_id'] == 'space.full'
                result = {'content': [{'type': 'text', 'text': json.dumps({'identity': 'bot-key', 'role': 'editor'})}]}
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': body['id'], 'result': result})


@pytest.mark.parametrize('version', ['2025-06-18', '2025-11-25'])
async def test_pinned_heart_full_mcp_uses_negotiated_protocol_and_actual_bearer(version):
    api = FakeAPI()
    connection = await inspect_api(extra(), 'gateway-private', transport=api.transport)
    api.requests.clear()
    mcp = HeartFullMCP(version)
    result = await inspect_mcp({'url': 'http://127.0.0.1:31009/mcp/full',
        'headers': {'Authorization': 'Bearer ${ANYTYPE_MCP_SELECTED}'}}, connection,
        lambda name: 'mcp-private', transport=mcp.transport, api_transport=api.transport)
    assert result['state'] == 'ready', result
    assert [(method, path) for method, path, headers in api.requests] == [('GET', '/v2/auth/whoami')]
    assert api.requests[0][2]['authorization'] == 'Bearer mcp-private'
    assert 'mcp-private' not in json.dumps(result)
    assert all(headers['authorization'] == 'Bearer mcp-private' for body, headers in mcp.requests)
    assert [body['params']['name'] for body, headers in mcp.requests if body['method'] == 'tools/call'] == ['get_member_me']


async def test_heart_full_mcp_checks_own_grant_instead_of_gateway_key():
    gateway = FakeAPI()
    connection = await inspect_api(extra(), 'gateway-private', transport=gateway.transport)
    mcp_api = FakeAPI(all_spaces=False)
    result = await inspect_mcp({'url': 'http://127.0.0.1:31009/mcp/full',
        'headers': {'Authorization': 'Bearer ${ANYTYPE_MCP_SELECTED}'}}, connection,
        lambda name: 'other-private', transport=HeartFullMCP().transport, api_transport=mcp_api.transport)
    assert result['state'] == 'incompatible', result
    assert result['error']['type'] == 'mcp_grant_mismatch'
    assert mcp_api.requests[0][2]['authorization'] == 'Bearer other-private'


@pytest.mark.parametrize('url,server_name', [
    ('http://127.0.0.1:31012/mcp/full', 'anytype'),
    ('http://127.0.0.1:31009/mcp/small', 'anytype'),
    ('http://127.0.0.1:31009/mcp/full', 'external-mcp'),
])
async def test_generic_or_unbound_mcp_cannot_borrow_rest_grant(url, server_name):
    connection = await inspect_api(extra(), 'gateway-private', transport=FakeAPI().transport)
    no_rest = httpx.MockTransport(lambda request: pytest.fail('REST must not stand in for generic MCP credentials'))
    result = await inspect_mcp({'url': url}, connection, lambda name: 'token',
        transport=HeartFullMCP(server_name=server_name).transport, api_transport=no_rest)
    assert result['state'] == 'incompatible', result
    assert 'auth_whoami' in result['missing_operations']
