import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from cryptography.fernet import Fernet

import app as app_module
from app import create_app, seed_admin


@pytest.fixture
def model_config_site(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))

    async def fake_chat(profile, messages, api_key):
        system = messages[0]['content']
        if 'connection test' in system:
            return {'text': 'OK', 'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}}
        if '你是多人图灵测试的独立裁判' in system:
            return {'text': '请描述一次你改变看法的经历。', 'usage': {}}
        return {'text': '我会先了解具体情况，再决定如何处理。', 'usage': {}}

    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
    path = tmp_path / 'model-config-browser.sqlite3'
    seed_admin(path, 'browser_admin', '浏览器主持人', 'browser-password')
    app = create_app(path)
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        import httpx

        for _ in range(100):
            try:
                if httpx.get(origin + '/api/health', timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail('模型配置浏览器验收服务启动失败')
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_model_config_and_question_workflow_browser(model_config_site):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.setdefault('PLAYWRIGHT_CHANNEL', 'default')
    result = subprocess.run(
        ['node', str(root / 'tests' / 'model_config_ui.cjs'), model_config_site],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=150, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
