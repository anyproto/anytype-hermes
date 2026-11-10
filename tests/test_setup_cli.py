"""Exercise the real Hermes plugin loader and top-level process exit handling."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_actual_cli_available_without_key_and_returns_automation_exit_codes(tmp_path, pytestconfig):
    home = tmp_path / 'profile'
    plugin = home / 'plugins/anytype-platform'
    plugin.mkdir(parents=True)
    for source in [*ROOT.glob('*.py'), ROOT / 'plugin.yaml']:
        shutil.copy2(source, plugin / source.name)
    shutil.copytree(ROOT / 'skills', plugin / 'skills')
    config = home / 'config.yaml'
    config.write_text(json.dumps({'plugins': {'enabled': ['anytype-platform']}}))
    before = config.read_bytes()
    env = {key: value for key, value in os.environ.items()
           if key in {'PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT'}}
    env.update(HOME=str(tmp_path), HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(tmp_path / 'runtime'),
               PYTHONPATH=str(Path(pytestconfig.getoption('--hermes-source')).resolve()),
               HERMES_DISABLE_LAZY_INSTALLS='1', PYTHONDONTWRITEBYTECODE='1')
    malformed = tmp_path / 'malformed.json'
    malformed.write_text('{')
    cases = [
        (['anytype', '--help'], 0, None),
        (['anytype', 'doctor', '--json'], 2, None),
        (['anytype', 'setup', '--non-interactive', '--json'], 2, 'missing_choice'),
        (['anytype', 'setup', '--non-interactive', '--json', '--input', str(malformed)], 2, 'input_document'),
    ]
    for args, expected_code, error in cases:
        run = subprocess.run([sys.executable, '-m', 'hermes_cli.main', *args], cwd=tmp_path,
                             env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=45)
        assert run.returncode == expected_code, (args, run.stdout, run.stderr)
        if '--help' in args:
            assert all(command in run.stdout for command in ['setup', 'doctor', 'join'])
        else:
            result = json.loads(run.stdout)
            assert result['schema_version'] == 1
            assert result['state'] == 'needs_input'
            assert result['changed'] is False
            assert result['next_action']
            if error:
                assert result['error']['type'] == error
            else:
                assert result['gateway'] == 'not_configured'
        assert config.read_bytes() == before
        assert not (home / '.env').exists()
