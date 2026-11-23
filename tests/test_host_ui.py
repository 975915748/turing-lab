import subprocess
from pathlib import Path

import pytest
from test_uid_ui import site


@pytest.mark.parametrize('script', ['host_ui.cjs', 'room_delete_ui.cjs'])
def test_room_host_browser(site, script):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / script), site],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
