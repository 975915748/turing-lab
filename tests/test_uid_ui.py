"""使用本机 Edge + Node Playwright 对真实本地服务验收。"""
import socket
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet

import app as app_module
from app import create_app, db_open, seed_admin


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

@pytest.fixture
def model_site(tmp_path, monkeypatch):
    path = tmp_path / 'model-browser.sqlite3'
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))

    async def fake_chat(profile, messages, api_key):
        return {'text': 'OK', 'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}

    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
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
            pytest.fail('模型配置浏览器验收服务启动失败')

        with httpx.Client(base_url=origin, timeout=5) as client:
            login = client.post('/api/login', json={
                'username': 'browser_admin', 'password': 'browser-password',
            })
            assert login.status_code == 200, login.text
            me = client.get('/api/me').json()['user']
            headers = {'X-CSRF-Token': me['csrf']}
            profiles = [
                {'name': '重复配置', 'model': 'fold-model-a'},
                {'name': '重复配置', 'model': 'fold-model-b'},
                {'name': '长名称-' + '测试' * 25, 'model': 'fold-long-name'},
                {'name': '待修复配置', 'model': 'fold-repair'},
            ]
            ids = []
            for item in profiles:
                response = client.post('/api/me/models', headers=headers, json={
                    'name': item['name'], 'base_url': 'https://api.example.com/v1',
                    'model': item['model'], 'api_key': 'sk-browser-' + item['model'],
                    'temperature': 0.7, 'max_tokens': 512, 'timeout_seconds': 30,
                })
                assert response.status_code == 200, response.text
                ids.append(response.json()['id'])
        with db_open(path) as db:
            db.execute('UPDATE model_profiles SET api_key_encrypted=? WHERE id=?', ('', ids[3]))
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_model_profile_collapse_browser(model_site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'uid_ui.cjs'), model_site, 'models'],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr

def test_model_copy_cleanup_browser(model_site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'uid_ui.cjs'), model_site, 'copy'],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr

def test_ai_question_entrypoints_browser(model_site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'uid_ui.cjs'), model_site, 'questions'],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr

def test_persona_settings_browser(model_site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'uid_ui.cjs'), model_site, 'personas'],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr


def test_model_operations_browser(model_site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'model_operations_ui.cjs'), model_site],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr



def test_social_points_browser(site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ['node', str(root / 'tests' / 'social_points_ui.cjs'), site],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_model_list_account_switch_race_browser(model_site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'uid_ui.cjs'), model_site, 'model-account-race'],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr


def test_personal_model_navigation_race_browser(model_site):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(['node', str(root / 'tests' / 'uid_ui.cjs'), model_site, 'model-page-race'],
        cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=150)
    assert result.returncode == 0, result.stdout + result.stderr
