"""Produce a plugin archive from an explicit allowlist, excluding secrets and dev/runtime state."""

from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

root = Path(__file__).resolve().parents[1]
files = ['plugin.yaml', '__init__.py', 'adapter.py', 'client.py', 'cli.py', 'config.py',
         'formatting.py', 'sender.py', 'state.py', 'activity.py', 'README.md', 'SPEC.md',
         'hermes.example.yaml', 'hermes-mcp.example.yaml', 'docs/VALIDATION.md',
         'patches/hermes-delivery-context.patch', 'patches/hermes-activity-callbacks.patch']
output = root / 'dist' / 'anytype-platform.zip'
output.parent.mkdir(exist_ok=True)
with ZipFile(output, 'w', ZIP_DEFLATED) as archive:
    for name in files:
        archive.write(root / name, 'anytype-platform/' + name)
print(output)
