from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from anytype_plugin.setup_store import ProfileStore, SetupError, owned, publish, restore_consumers


class FakeLifecycle:
    def __init__(self, *, running=(True, False), fail_stop=None):
        self.consumers = [{'id': 'consumer-' + str(i), 'running': state, 'stopped': False}
                          for i, state in enumerate(running)]
        self.stopped, self.restored = [], []
        self.fail_stop = fail_stop
        self.held = False

    @contextmanager
    def hold(self):
        self.held = True
        try:
            yield
        finally:
            self.held = False

    def inspect(self):
        return deepcopy(self.consumers)

    def stop(self, consumer):
        self.consumers[int(consumer['id'][-1])]['running'] = False
        self.stopped.append(consumer['id'])
        if self.fail_stop == consumer['id']:
            raise SetupError('stop_failed', 'Injected stop failure')

    def verify_stopped(self, consumers):
        return not any(c['running'] for c in self.consumers)

    def restore(self, consumer):
        self.restored.append(consumer['id'])
        self.consumers[int(consumer['id'][-1])]['running'] = True
        return True

    def start(self, consumers):
        assert not self.held
        for consumer in self.consumers:
            consumer['running'] = True
        return True


def prepared(home):
    store = ProfileStore(home)
    config = {'model': {'default': 'preserved'}, 'gateway': {'session_timeout': 42, 'platforms': {
        'other': {'enabled': True}, 'anytype': {'enabled': True, 'channel_overrides': {'a': 'b'},
        'extra': {'base_url': 'http://127.0.0.1:31009', 'api_key_env': 'ANYTYPE_OLD', 'unknown': 'keep'}}}},
        'mcp_servers': {'other': {'command': 'safe'}}}
    store.config_path.write_text(json.dumps(config))
    current, revision = store.snapshot()
    record = store.begin(current, revision)
    record['step'] = 'prepared'
    record['candidate'] = deepcopy(record['prior_owned'])
    record['candidate']['extra'].update(base_url='http://127.0.0.1:31012', api_key_env='ANYTYPE_NEW')
    store.save(record)
    return store, record


def test_defer_keeps_live_configuration_byte_for_byte(isolated_home):
    store, record = prepared(isolated_home)
    before = store.config_path.read_bytes()
    lifecycle = FakeLifecycle()
    result = publish(store, record, lifecycle, activation='defer')
    assert result['state'] == 'restart_required'
    assert result['publication'] == 'prepared'
    assert store.config_path.read_bytes() == before
    assert not lifecycle.stopped


def test_publication_merges_only_owned_settings_and_starts_after_boundary(isolated_home):
    store, record = prepared(isolated_home)
    lifecycle = FakeLifecycle()
    result = publish(store, record, lifecycle, activation='activate')
    current, _ = store.snapshot()
    assert result['publication'] == 'active'
    assert current['gateway']['platforms']['anytype']['extra']['api_key_env'] == 'ANYTYPE_NEW'
    assert current['gateway']['platforms']['anytype']['extra']['unknown'] == 'keep'
    assert current['gateway']['platforms']['anytype']['channel_overrides'] == {'a': 'b'}
    assert current['model'] == {'default': 'preserved'}
    assert current['mcp_servers']['other'] == {'command': 'safe'}


@pytest.mark.parametrize('phase', ['before_stop', 'after_stop', 'before_commit', 'after_commit'])
def test_failure_at_each_boundary_leaves_complete_old_or_new_and_restores(isolated_home, phase):
    store, record = prepared(isolated_home)
    lifecycle = FakeLifecycle()
    class Crash(BaseException):
        pass
    def fault(current):
        if current == phase:
            raise Crash()
    with pytest.raises(Crash):
        publish(store, record, lifecycle, activation='save_stopped', fault=fault)
    current, _ = store.snapshot()
    extra = current['gateway']['platforms']['anytype']['extra']
    if phase == 'after_commit':
        assert (extra['base_url'], extra['api_key_env']) == ('http://127.0.0.1:31012', 'ANYTYPE_NEW')
        assert not lifecycle.restored
        assert store.load()['step'] == 'committed'
    else:
        assert (extra['base_url'], extra['api_key_env']) == ('http://127.0.0.1:31009', 'ANYTYPE_OLD')
        assert lifecycle.consumers[0]['running']
        assert not lifecycle.consumers[1]['running']


def test_partial_stop_failure_restores_only_attempt_owned_running_consumers(isolated_home):
    store, record = prepared(isolated_home)
    lifecycle = FakeLifecycle(running=(True, True, False), fail_stop='consumer-1')
    with pytest.raises(SetupError):
        publish(store, record, lifecycle, activation='save_stopped')
    assert lifecycle.restored == ['consumer-0', 'consumer-1']
    assert not lifecycle.consumers[2]['running']


def test_external_edit_blocks_publication_and_unsafe_restoration(isolated_home):
    store, record = prepared(isolated_home)
    lifecycle = FakeLifecycle()
    def fault(phase):
        if phase == 'after_stop':
            config, _ = store.snapshot()
            config['model']['default'] = 'external'
            store.config_path.write_text(json.dumps(config))
    with pytest.raises(SetupError) as error:
        publish(store, record, lifecycle, activation='save_stopped', fault=fault)
    assert error.value.code == 'config_changed'
    assert store.load()['stopped_consumers'] == ['consumer-0']
    assert not lifecycle.restored
    assert store.snapshot()[0]['model']['default'] == 'external'


def test_ambiguous_commit_reconciles_actual_config_before_retry(isolated_home):
    store, record = prepared(isolated_home)
    record['step'] = 'committing'
    record['commit_base_revision'] = record['prior_revision']
    record['commit_expected'] = 'not-current'
    store.save(record)
    store.reconcile_commit(record)
    assert record['step'] == 'prepared'
    record['step'] = 'committing'
    store.config_path.write_text('{"external": true}')
    with pytest.raises(SetupError) as error:
        store.reconcile_commit(record)
    assert error.value.code == 'ambiguous_commit'


def test_profile_setup_lock_is_exclusive(isolated_home):
    store = ProfileStore(isolated_home)
    with store.locked():
        with pytest.raises(SetupError) as error:
            with ProfileStore(isolated_home).locked():
                pass
    assert error.value.code == 'setup_busy'


def test_production_managed_boundary_uses_public_lifecycle_and_restores_on_failure(isolated_home, monkeypatch):
    import hermes_cli.gateway as gateway
    import gateway.status as status
    from anytype_plugin.setup_store import ManagedGatewayBoundary
    running = [True]
    calls = []
    monkeypatch.setattr(gateway, 'host_multiplexer_serving', lambda: None)
    monkeypatch.setattr(gateway, 'get_gateway_runtime_snapshot', lambda: gateway.GatewayRuntimeSnapshot(
        manager='systemd', service_installed=True, service_running=running[0],
        gateway_pids=(42,) if running[0] else (), service_scope='user'))
    monkeypatch.setattr(status, 'get_process_start_time', lambda pid: 100)
    monkeypatch.setattr(status, 'owns_gateway_runtime_lock', lambda: False)
    monkeypatch.setattr(status, 'acquire_gateway_runtime_lock', lambda: calls.append('reserve') or True)
    monkeypatch.setattr(status, 'release_gateway_runtime_lock', lambda: calls.append('release'))
    def command(args):
        calls.append(args.gateway_command)
        running[0] = args.gateway_command == 'start'
    monkeypatch.setattr(gateway, 'gateway_command', command)
    store, record = prepared(isolated_home)
    boundary = ManagedGatewayBoundary(isolated_home, {'profile_home': str(isolated_home.resolve()),
        'exclusive': True, 'mcp_clients_stopped': True})
    result = publish(store, record, boundary, activation='activate')
    assert result['publication'] == 'active'
    assert calls == ['stop', 'reserve', 'release', 'start']


def test_production_managed_boundary_refuses_shared_host_without_stopping(isolated_home, monkeypatch):
    import hermes_cli.gateway as gateway
    from types import SimpleNamespace
    from anytype_plugin.setup_store import ManagedGatewayBoundary
    monkeypatch.setattr(gateway, 'host_multiplexer_serving', lambda: SimpleNamespace(profiles=['default', 'other']))
    monkeypatch.setattr(gateway, 'gateway_command', lambda args: pytest.fail('must not stop shared host'))
    boundary = ManagedGatewayBoundary(isolated_home, {'profile_home': str(isolated_home.resolve()),
        'exclusive': True, 'mcp_clients_stopped': True})
    with pytest.raises(SetupError) as error:
        with boundary.hold():
            pass
    assert error.value.code == 'shared_host'


def test_direct_publication_refuses_managed_anytype_before_consumer_stop(isolated_home, monkeypatch):
    store, record = prepared(isolated_home)
    before = store.config_path.read_bytes()
    directory = isolated_home.parent / 'managed'
    directory.mkdir()
    (directory / 'config.yaml').write_text(json.dumps({'gateway': {'platforms': {'anytype': {
        'extra': {'base_url': 'https://administrator.invalid'}}}}}))
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(directory))
    lifecycle = FakeLifecycle()
    with pytest.raises(SetupError) as error:
        publish(store, record, lifecycle, activation='save_stopped')
    assert error.value.code == 'managed_config'
    assert not lifecycle.stopped
    assert store.config_path.read_bytes() == before


def test_admin_pin_appearing_during_stop_blocks_commit_and_unsafe_restart(isolated_home, monkeypatch):
    store, record = prepared(isolated_home)
    before = store.config_path.read_bytes()
    directory = isolated_home.parent / 'managed'
    directory.mkdir()
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(directory))
    lifecycle = FakeLifecycle()
    def changed_policy(phase):
        if phase == 'after_stop':
            (directory / 'config.yaml').write_text(json.dumps({'mcp_servers': {'anytype': {'enabled': False}}}))
    with pytest.raises(SetupError) as error:
        publish(store, record, lifecycle, activation='save_stopped', fault=changed_policy)
    assert error.value.code == 'managed_config'
    assert store.config_path.read_bytes() == before
    assert store.load()['stopped_consumers'] == ['consumer-0']
    assert not lifecycle.restored
