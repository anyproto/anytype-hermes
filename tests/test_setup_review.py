import pytest
import httpx
from test_setup import choices
from test_setup_diagnostics import FakeAPI
from test_setup_publication import FakeLifecycle
from anytype_plugin.onboarding import Wizard

pytestmark = pytest.mark.asyncio

async def test_interactive_resume_can_select_publication(isolated_home, monkeypatch):
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    assert (await wizard.run(choices()))['publication'] == 'prepared'
    prompts = []
    def pick(label, options, default=None):
        prompts.append(label)
        return 'save_stopped'
    monkeypatch.setattr('anytype_plugin.onboarding.choose', pick)
    wizard.interactive = True
    result = await wizard.run(resume=True)
    assert result['state'] == 'saved', {'result': result, 'prompts': prompts}

async def test_external_interactive_can_supply_origin(isolated_home, monkeypatch):
    api = FakeAPI()
    prompts = []
    def answer(label, default=None, **kwargs):
        prompts.append(label)
        return 'https://anytype.example.test'
    monkeypatch.setattr('anytype_plugin.onboarding.prompt', answer)
    wizard = Wizard(isolated_home, interactive=True, api_transport=api.transport)
    result = await wizard.run(choices(mode='external'))
    assert result['publication'] == 'prepared'
    assert wizard.store.load()['candidate']['extra']['base_url'] == 'https://anytype.example.test', prompts

async def test_join_preserves_current_remote_name(isolated_home):
    api = FakeAPI(spaces=('space.full', 'other.full'))
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    assert (await wizard.run(choices(activation='save_stopped')))['state'] == 'saved'
    api.name = 'Renamed in Anytype'
    result = await wizard.join({'joined_space_id': 'other.full', 'discovery_action': 'all'})
    assert result['state'] == 'restart_required', result

async def test_activation_blocks_newly_missing_activity(isolated_home):
    api = FakeAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    assert (await wizard.run(choices()))['publication'] == 'prepared'
    api.schema['paths'].pop('/v2/spaces/{space_id}/chats/{chat_id}/status')
    result = await wizard.run({'activation': 'save_stopped'}, resume=True)
    assert result['state'] == 'needs_input', result
    assert not wizard.store.config_path.exists()

async def test_rotation_keeps_application_key_name(isolated_home, monkeypatch):
    class NamedKeys(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == '/v2/auth/whoami':
                data = response.json()
                data['key']['name'] = 'Different application' if request.headers.get('Authorization') == 'Bearer rotated-key' else 'Hermes'
                return httpx.Response(200, json=data)
            return response
    api = NamedKeys()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    assert (await wizard.run(choices(activation='save_stopped')))['state'] == 'saved'
    monkeypatch.setenv('ANYTYPE_ROTATED', 'rotated-key')
    result = await wizard.run(choices(action='repair', activation='save_stopped', api_key_ref={'env': 'ANYTYPE_ROTATED'}))
    assert result['state'] == 'needs_input', result

async def test_join_accepts_expanded_named_space_grant(isolated_home, monkeypatch):
    class NamedSpaceKeys(FakeAPI):
        def handle(self, request):
            response = super().handle(request)
            allowed = ('space.full', 'other.full') if request.headers.get('Authorization') == 'Bearer expanded-key' else ('space.full',)
            if request.url.path == '/v2/auth/whoami':
                data = response.json()
                data['grant'] = {'all_spaces': False, 'permission': 'readwrite', 'spaces': [{'id': s} for s in allowed]}
                return httpx.Response(200, json=data)
            if request.url.path == '/v2/spaces':
                return httpx.Response(200, json={'data': [{'id': s, 'name': 'Space'} for s in allowed]})
            return response
    api = NamedSpaceKeys(all_spaces=False)
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    initial = choices(scope={'mode': 'single', 'space_id': 'space.full'}, activation='save_stopped')
    assert (await wizard.run(initial))['state'] == 'saved'
    monkeypatch.setenv('ANYTYPE_EXPANDED', 'expanded-key')
    result = await wizard.join({'joined_space_id': 'other.full', 'discovery_action': 'switch',
                                'api_key_ref': {'env': 'ANYTYPE_EXPANDED'}})
    assert result['state'] == 'restart_required', result

async def test_existing_mcp_can_be_removed_interactively(isolated_home, monkeypatch):
    import json
    old = {'mcp_servers': {'anytype': {'url': 'https://mcp.example.test',
             'headers': {'Authorization': 'Bearer ${ANYTYPE_MCP_TOKEN}'}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(old))
    api = FakeAPI()
    selected = choices(base_url='http://127.0.0.1:31009')
    selected.pop('mcp')
    answers = []
    def pick(label, options, default=None):
        answers.append((label, options))
        return 'none' if 'none' in options else 'remove'
    monkeypatch.setattr('anytype_plugin.onboarding.choose', pick)
    wizard = Wizard(isolated_home, interactive=True, api_transport=api.transport)
    result = await wizard.run(selected)
    assert result['state'] == 'restart_required', {'result': result, 'prompts': answers}


@pytest.mark.parametrize('activation', ['defer', 'save_stopped'])
async def test_interrupted_precommit_restores_before_defer_or_unavailable_candidate(isolated_home, activation):
    from copy import deepcopy
    api = FakeAPI()
    lifecycle = FakeLifecycle(running=(True, False))
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=lifecycle)
    await wizard.run(choices())
    record = wizard.store.load()
    record['consumers'] = deepcopy(lifecycle.consumers)
    record['consumers'][0].update(stop_intent=True, stopped=True)
    record['stopped_consumers'] = ['consumer-0']
    lifecycle.consumers[0]['running'] = False
    wizard.store.save(record)
    calls = []
    def unavailable(request):
        calls.append(request.url.path)
        assert lifecycle.restored == ['consumer-0']
        return httpx.Response(503)
    wizard.api_transport = httpx.MockTransport(unavailable)
    result = await wizard.run({'activation': activation}, resume=True)
    assert lifecycle.restored == ['consumer-0']
    assert lifecycle.consumers[0]['running']
    assert not lifecycle.consumers[1]['running']
    assert not wizard.store.load().get('stopped_consumers')
    assert not wizard.store.config_path.exists()
    if activation == 'defer':
        assert result['publication'] == 'prepared'
        assert not calls
    else:
        assert result['state'] == 'unavailable', result
        assert calls


async def test_interrupted_restore_failure_reports_exact_consumer_and_preserves_journal(isolated_home):
    api = FakeAPI()
    lifecycle = FakeLifecycle(running=(False,))
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=lifecycle)
    await wizard.run(choices())
    record = wizard.store.load()
    record['consumers'] = [{'id': 'consumer-0', 'running': True, 'stop_intent': True}]
    wizard.store.save(record)
    lifecycle.restore = lambda consumer: False
    wizard.api_transport = httpx.MockTransport(lambda request: pytest.fail('validation before restoration'))
    result = await wizard.run(resume=True)
    assert result['error']['type'] == 'restore_required'
    assert result['stopped_consumers'] == ['consumer-0']
    assert 'consumer-0' in result['next_action']
    assert wizard.store.load()['stopped_consumers'] == ['consumer-0']


class NamedGrantAPI(FakeAPI):
    """The old named grant stays valid while a replacement gains a second space."""
    def handle(self, request):
        response = super().handle(request)
        expanded = request.headers.get('Authorization') == 'Bearer expanded-key'
        allowed = ['space.full', 'other.full'] if expanded else ['space.full']
        if request.url.path == '/v2/auth/whoami':
            data = response.json()
            data['grant'] = {'all_spaces': False, 'permission': 'readwrite', 'spaces': [{'id': s} for s in allowed]}
            data['key']['id'] = 'expanded-id' if expanded else 'legacy-id'
            return httpx.Response(200, json=data)
        if request.url.path == '/v2/spaces':
            return httpx.Response(200, json={'data': [{'id': s, 'name': 'Space'} for s in allowed]})
        return response


@pytest.mark.parametrize('first_activation', ['defer', 'save_stopped'])
async def test_rollback_resumes_prior_named_grant_and_implicit_legacy_selector(isolated_home, monkeypatch, first_activation):
    import json
    from test_setup_diagnostics import extra
    old = {'gateway': {'platforms': {'anytype': {'enabled': True,
        'extra': extra(discovery={'space_id': 'space.full'})}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(old))
    api = NamedGrantAPI()
    lifecycle = FakeLifecycle(running=(False,))
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=lifecycle)
    monkeypatch.setenv('ANYTYPE_EXPANDED', 'expanded-key')
    result = await wizard.run(choices(action='repair', activation='save_stopped',
        api_key_ref={'env': 'ANYTYPE_EXPANDED'},
        api_grant={'all_spaces': False, 'space_ids': ['space.full', 'other.full']}))
    assert result['state'] == 'saved', result
    committed = wizard.store.load()
    live = wizard.store.config_path.read_bytes()
    # A failed publication barrier is also a resumable rollback, not a retry of
    # the forward grant. Defer covers the same state without stopping anything.
    lifecycle.verify_stopped = lambda consumers: False
    result = await wizard.run({'activation': first_activation}, rollback=True)
    assert result['state'] in {'restart_required', 'needs_input'}, result
    rollback = wizard.store.load()
    assert rollback['id'] != committed['id']
    assert rollback['rollback'] is True
    assert rollback['grant']['space_ids'] == ['space.full']
    assert rollback['credential']['id'] == 'legacy-id'
    assert 'api_key_env' not in rollback['candidate']['extra']
    assert wizard.store.config_path.read_bytes() == live
    lifecycle.verify_stopped = lambda consumers: True
    result = await wizard.run({'activation': 'save_stopped'}, resume=True)
    assert result['state'] == 'saved', result
    extra_now = wizard.store.snapshot()[0]['gateway']['platforms']['anytype']['extra']
    assert extra_now == old['gateway']['platforms']['anytype']['extra']


async def test_join_all_discovers_only_explicitly_expanded_named_grant(isolated_home, monkeypatch):
    api = NamedGrantAPI()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    result = await wizard.run(choices(scope={'mode': 'single', 'space_id': 'space.full'}, activation='save_stopped'))
    assert result['state'] == 'saved', result
    monkeypatch.setenv('ANYTYPE_EXPANDED', 'expanded-key')
    result = await wizard.join({'joined_space_id': 'other.full', 'discovery_action': 'all',
                               'api_key_ref': {'env': 'ANYTYPE_EXPANDED'}})
    assert result['publication'] == 'prepared', result
    record = wizard.store.load()
    assert record['grant'] == {'all_spaces': False, 'space_ids': ['other.full', 'space.full'], 'permission': 'readwrite'}
    assert record['candidate']['extra']['discovery']['spaces'] == 'all'


async def test_candidate_broader_than_intended_named_grant_stays_unpublished(isolated_home, monkeypatch):
    monkeypatch.setenv('ANYTYPE_EXPANDED', 'expanded-key')
    wizard = Wizard(isolated_home, interactive=False, api_transport=NamedGrantAPI().transport)
    result = await wizard.run(choices(api_key_ref={'env': 'ANYTYPE_EXPANDED'},
        api_grant={'all_spaces': False, 'space_ids': ['space.full']}, activation='save_stopped'))
    assert result['error']['type'] == 'grant_mismatch'
    assert not wizard.store.config_path.exists()


async def test_interactive_resume_honors_explicit_defer(isolated_home, monkeypatch):
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport)
    await wizard.run(choices())
    wizard.interactive = True
    monkeypatch.setattr('anytype_plugin.onboarding.choose', lambda *args, **kwargs: pytest.fail('explicit choice must not prompt'))
    result = await wizard.run({'activation': 'defer'}, resume=True)
    assert result['publication'] == 'prepared'


async def test_interactive_origin_defaults_to_current_and_explicit_value_does_not_prompt(isolated_home, monkeypatch):
    import json
    from test_setup_diagnostics import extra
    current = 'https://current.example.test'
    (isolated_home / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': extra(base_url=current)}}}}))
    seen = []
    def answer(label, default=None, **kwargs):
        seen.append(default)
        return default
    monkeypatch.setattr('anytype_plugin.onboarding.prompt', answer)
    wizard = Wizard(isolated_home, interactive=True, api_transport=FakeAPI().transport)
    result = await wizard.run(choices(action='repair', mode='external'))
    assert result['publication'] == 'prepared', result
    assert seen == [current]
    await wizard.run(cancel=True)
    seen.clear()
    result = await wizard.run(choices(action='repair', mode='external', base_url=current))
    assert result['publication'] == 'prepared', result
    assert seen == []


async def test_mcp_replacement_prompts_and_preserves_stable_application_name(isolated_home, monkeypatch):
    from test_setup_diagnostics import FakeMCP
    api, mcp = FakeAPI(), FakeMCP()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport,
                    mcp_transport=mcp.transport, lifecycle=FakeLifecycle(running=(False,)))
    original_mcp = {'mode': 'http', 'url': 'https://mcp.example.test/mcp', 'shared_api_key': True}
    assert (await wizard.run(choices(mcp=original_mcp, activation='save_stopped')))['state'] == 'saved'
    answers = []
    def pick(label, options, default=None):
        answers.append(options)
        return 'replace'
    monkeypatch.setattr('anytype_plugin.onboarding.choose', pick)
    wizard.interactive = True
    result = await wizard.run(choices(action='repair', base_url='http://127.0.0.1:31009',
        mcp={**original_mcp, 'url': 'https://mcp.example.test/new-mcp'}))
    assert result['publication'] == 'prepared', result
    assert answers == [['replace', 'keep']]
    await wizard.run(cancel=True)
    wizard.interactive = False
    def rotated_mcp(request):
        mcp.key_name = 'Changed application' if request.headers.get('Authorization') == 'Bearer rotated-key' else 'Hermes'
        return mcp.handle(request)
    wizard.mcp_transport = httpx.MockTransport(rotated_mcp)
    monkeypatch.setenv('ANYTYPE_ROTATED', 'rotated-key')
    result = await wizard.run(choices(action='repair', api_key_ref={'env': 'ANYTYPE_ROTATED'},
        mcp={**original_mcp, 'conflict': 'replace'}))
    assert result['error']['type'] == 'application_name_mismatch', result
    assert wizard.store.snapshot()[0]['mcp_servers']['anytype']['url'] == original_mcp['url']


async def test_prepared_key_application_name_drift_blocks_publication(isolated_home):
    class MutableName(FakeAPI):
        key_name = 'Hermes'
        def handle(self, request):
            response = super().handle(request)
            if request.url.path == '/v2/auth/whoami':
                data = response.json()
                data['key']['name'] = self.key_name
                return httpx.Response(200, json=data)
            return response
    api = MutableName()
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    await wizard.run(choices())
    api.key_name = 'Unrelated application'
    result = await wizard.run({'activation': 'save_stopped'}, resume=True)
    assert result['error']['type'] == 'application_name_mismatch'
    assert not wizard.store.config_path.exists()


async def test_explicitly_disabled_activity_survives_missing_optional_route(isolated_home):
    api = FakeAPI(activity=False)
    wizard = Wizard(isolated_home, interactive=False, api_transport=api.transport, lifecycle=FakeLifecycle(running=(False,)))
    result = await wizard.run(choices(activity_status=False, activation='save_stopped'))
    assert result['state'] == 'saved', result
    assert wizard.store.snapshot()[0]['gateway']['platforms']['anytype']['extra']['activity_status'] is False


@pytest.mark.parametrize('failure_kind', ['typed', 'interrupt', 'unexpected'])
async def test_failed_rollback_stop_reports_its_own_recovery_consumers(isolated_home, failure_kind):
    import json
    from test_setup_diagnostics import extra
    config = {'gateway': {'platforms': {'anytype': {'enabled': True, 'extra': extra()}}}}
    (isolated_home / 'config.yaml').write_text(json.dumps(config))
    lifecycle = FakeLifecycle(running=(False,))
    wizard = Wizard(isolated_home, interactive=False, api_transport=FakeAPI().transport, lifecycle=lifecycle)
    assert (await wizard.run(choices(action='repair', activation='save_stopped')))['state'] == 'saved'
    lifecycle.consumers[0]['running'] = True
    if failure_kind == 'typed':
        lifecycle.fail_stop = 'consumer-0'
    else:
        original_stop = lifecycle.stop
        def fail_after_stop(consumer):
            original_stop(consumer)
            raise (KeyboardInterrupt() if failure_kind == 'interrupt' else RuntimeError('private-error-detail'))
        lifecycle.stop = fail_after_stop
    lifecycle.restore = lambda consumer: False
    result = await wizard.run(rollback=True)
    assert result['error']['type'] == {'typed': 'stop_failed', 'interrupt': 'cancelled', 'unexpected': 'setup_failed'}[failure_kind]
    assert result['stopped_consumers'] == ['consumer-0']
    assert 'consumer-0' in result['next_action']
    assert 'private-error-detail' not in json.dumps(result)
    assert wizard.store.load()['rollback'] is True
    assert wizard.store.load()['stopped_consumers'] == ['consumer-0']
