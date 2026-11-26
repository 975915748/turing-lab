import subprocess
from pathlib import Path
from test_uid_ui import site

def test_departures_browser(site):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(["node",str(root/"tests"/"room_departures_ui.cjs"),site],cwd=root,capture_output=True,text=True,encoding="utf-8",timeout=100)
    assert result.returncode==0,result.stdout+result.stderr
