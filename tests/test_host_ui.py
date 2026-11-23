import subprocess
from pathlib import Path

from test_uid_ui import site


def test_room_host_browser(site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'host_ui.cjs'), site],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
