import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
def pytest_addoption(parser):
    parser.addoption('--hermes-source', default=os.environ.get('HERMES_SOURCE', str(ROOT / '.dev')),
                     help='Hermes checkout under test (survives the canonical runner environment scrub)')


def pytest_configure(config):
    sys.path.insert(0, str(Path(config.getoption('--hermes-source')).resolve()))


spec = importlib.util.spec_from_file_location('anytype_plugin', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('ANYTYPE_API_KEY', 'test-key')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for key in list(os.environ):
        if key.startswith(('GATEWAY_', 'ANYTYPE_ALLOWED', 'ANYTYPE_ALLOW_ALL')):
            monkeypatch.delenv(key, raising=False)
    from gateway.platform_registry import platform_registry, PlatformEntry
    from anytype_plugin.adapter import AnytypeAdapter
    platform_registry.register(PlatformEntry(name='anytype', label='Anytype', source='plugin',
                                             adapter_factory=AnytypeAdapter, check_fn=lambda: True,
                                             allowed_users_env='ANYTYPE_ALLOWED_USERS',
                                             allow_all_env='ANYTYPE_ALLOW_ALL_USERS'))
    return home


@pytest.fixture
def settings():
    from anytype_plugin.config import Settings
    return Settings.load({'chats': [{'space_id': 'space.full', 'chat_id': 'chat'}],
                          'group_allow_from': ['alice-key'], 'group_allow_admin_from': ['alice-key']})
