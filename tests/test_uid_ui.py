"""使用本机 Edge + Node Playwright 对真实本地服务验收。"""
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from app import create_app, seed_admin


@pytest.fixture
def site(tmp_path):
    path = tmp_path / 'browser.sqlite3'
    seed_admin(path, 'browser_admin', '浏览器主持人', 'browser-password')
    app = create_app(path)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            try:
                if httpx.get(origin + '/api/health', timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail('本地验收服务启动失败')
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


@pytest.mark.parametrize('scenario', ['register', 'judges'])
def test_uid_browser(site, scenario):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'uid_ui.cjs'), site, scenario],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_social_points_browser(site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', str(root / 'tests' / 'social_points_ui.cjs'), site],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
