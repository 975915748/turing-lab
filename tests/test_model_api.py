"""v1.6.0 OpenAI-compatible model configuration and autonomous AI workflow tests."""
import asyncio
import json
import socket
import sqlite3
import threading

import httpcore
import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app as app_module
import model_api
import railway_start
from app import create_app, db_open, seed_admin
from model_api import ModelAPIError, has_valid_credential_key, validate_credential_key


def post(client, path, body=None, expected=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']} if me.status_code == 200 else {}
    response = client.post(path, json=body or {}, headers=headers)
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json() if response.content else {}


@pytest.fixture
def model_lab(tmp_path, monkeypatch):
    credential_key = Fernet.generate_key().decode('ascii')
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', credential_key)
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))

    async def fake_chat(profile, messages, api_key):
        assert api_key == 'sk-test-secret-never-return-this'
        system = messages[0]['content']
        if 'connection test' in system:
            return {'text': 'OK', 'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}}
        if '严格返回 JSON 对象' in system:
            return {'text': json.dumps({
                'predictions': {'A': 'Human', 'B': 'Human', 'C': 'AI'},
                'evidence': '匿名回答中出现了稳定的语言与推理模式差异。',
                'confidence': 82,
                'weakness': '样本轮数较少。',
                'possible_error': '人类也可能使用相似的表达方式。'
            }, ensure_ascii=False), 'usage': {'prompt_tokens': 120, 'completion_tokens': 50, 'total_tokens': 170}}
        if '你是多人图灵测试的独立裁判' in system:
            return {'text': '如果你最近改变过一个重要看法，是什么促使你改变？', 'usage': {}}
        return {'text': '我会先观察具体情况，再决定如何处理。', 'usage': {'prompt_tokens': 30, 'completion_tokens': 16, 'total_tokens': 46}}

    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
    path = tmp_path / 'model-lab.sqlite3'
    seed_admin(path, 'model_admin', '模型实验主持人', 'model-admin-password')
    app = create_app(path)
    admin, p1, p2 = TestClient(app), TestClient(app), TestClient(app)
    post(admin, '/api/login', {'username': 'model_admin', 'password': 'model-admin-password'})
    room = post(admin, '/api/rooms', {'title': '模型 API 自动化验收'})
    room_id = room['room_id']
    base = f'/api/rooms/{room_id}'
    post(p1, '/api/register', {'username': 'human_a', 'display_name': '真人 A',
                               'password': 'human-a-password', 'code': room['code']})
    post(p2, '/api/register', {'username': 'human_b', 'display_name': '真人 B',
                               'password': 'human-b-password', 'code': room['code']})
    profile_body = {
        'name': '测试模型配置',
        'base_url': 'https://api.example.com/v1',
        'model': 'test-chat-model',
        'api_key': 'sk-test-secret-never-return-this',
        'temperature': 0.4,
        'max_tokens': 1024,
        'timeout_seconds': 30,
    }
    profile = post(admin, '/api/me/models', profile_body)
    return {
        'path': path, 'app': app, 'admin': admin, 'p1': p1, 'p2': p2,
        'base': base, 'profile_id': profile['id'], 'profile_body': profile_body,
    }


def test_model_profile_key_is_encrypted_and_never_returned_to_browser(model_lab):
    admin, base, path = model_lab['admin'], model_lab['base'], model_lab['path']
    info = admin.get('/api/me/models').json()
    assert info['model_api_enabled'] is True
    assert info['models'][0]['has_api_key'] is True
    assert 'api_key' not in info['models'][0]
    assert 'api_key_encrypted' not in info['models'][0]
    with db_open(path) as db:
        encrypted = db.execute('SELECT api_key_encrypted FROM model_profiles').fetchone()[0]
    assert encrypted != model_lab['profile_body']['api_key']
    assert model_lab['profile_body']['api_key'] not in encrypted
    tested = post(admin, f"/api/me/models/{model_lab['profile_id']}/test")
    assert tested['ok'] is True
    assert tested['preview'] == 'OK'
    assert tested['usage']['total_tokens'] == 5
    post(admin, base + '/ai', {'name': 'API AI', 'model_profile_id': model_lab['profile_id']})
    player_info = model_lab['p1'].get(base).json()
    assert player_info['model_profiles'] == []
    assert player_info['ais'][0]['model_profile_id'] is None
    assert model_lab['p1'].post(base + '/models', json=model_lab['profile_body'],
                                headers={'X-CSRF-Token': model_lab['p1'].get('/api/me').json()['user']['csrf']}).status_code == 403


def test_model_profile_update_keeps_key_when_input_is_blank(model_lab):
    admin, base, profile_id = model_lab['admin'], model_lab['base'], model_lab['profile_id']
    payload = dict(model_lab['profile_body'])
    payload['name'] = '更新后的模型配置'
    payload['api_key'] = ''
    me = admin.get('/api/me').json()
    response = admin.put(f'/api/me/models/{profile_id}', json=payload,
                         headers={'X-CSRF-Token': me['user']['csrf']})
    assert response.status_code == 200, response.text
    with db_open(model_lab['path']) as db:
        row = db.execute('SELECT name,api_key_encrypted FROM model_profiles WHERE id=?',
                         (profile_id,)).fetchone()
    assert row['name'] == '更新后的模型配置'
    assert model_api.decrypt_api_key(row['api_key_encrypted']) == model_lab['profile_body']['api_key']


def test_models_answer_ask_and_judge_without_manual_transcription(model_lab):
    admin, p1, p2, base, profile_id = (
        model_lab['admin'], model_lab['p1'], model_lab['p2'],
        model_lab['base'], model_lab['profile_id']
    )
    ai = post(admin, base + '/ai', {'name': 'API 参赛者', 'model_profile_id': profile_id})
    judge = post(admin, base + '/ai-judges', {'name': 'API 独立裁判', 'model_profile_id': profile_id})
    assert ai['alias'] == 'C'
    assert judge['id'] > 0

    questions = post(admin, base + '/ai-judges/questions/generate')
    assert questions['generated'] == 1
    question = admin.get(base).json()['rounds'][0]['questions']
    assert len(question) == 1
    assert '改变过一个重要看法' in question[0]['text']

    post(admin, base + '/start')
    answers = post(admin, base + '/ai/answers/generate')
    assert answers['generated'] == 1
    assert answers['results'][0]['alias'] == 'C'
    assert answers['results'][0]['status'] == 'generated'

    post(p1, base + '/answers', {'text': '我会先询问当事人的想法，再作出判断。'})
    post(p2, base + '/answers', {'text': '我通常会比较不同信息来源，然后再决定。'})
    assert admin.get(base).json()['rounds'][0]['status'] == 'revealed'

    verdicts = post(admin, base + '/ai-judges/verdicts/generate')
    assert verdicts['generated'] == 1
    room = admin.get(base).json()
    assert room['judges'][0]['locked'] is True
    assert '真人 A' not in json.dumps(room['rounds'], ensure_ascii=False)
    assert '真人 B' not in json.dumps(room['rounds'], ensure_ascii=False)

    post(admin, base + '/reveal')
    final = p1.get(base).json()
    assert final['status'] == 'finished'
    assert len(final['verdicts']) == 1
    assert final['verdicts'][0]['judge'] == 'API 独立裁判'
    assert final['verdicts'][0]['predictions'] == {'A': 'Human', 'B': 'Human', 'C': 'AI'}


def test_credential_key_validation_handles_missing_invalid_and_valid_values(monkeypatch):
    monkeypatch.delenv('MODEL_CREDENTIAL_KEY', raising=False)
    assert validate_credential_key() is False
    assert has_valid_credential_key() is False
    with pytest.raises(ModelAPIError):
        validate_credential_key(required=True)

    invalid_key = 'not-a-valid-fernet-key'
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', invalid_key)
    with pytest.raises(ModelAPIError) as error:
        validate_credential_key()
    assert invalid_key not in str(error.value)
    assert has_valid_credential_key() is False

    valid_key = Fernet.generate_key().decode('ascii')
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', valid_key)
    assert validate_credential_key() is True
    assert has_valid_credential_key() is True


def test_decrypt_api_key_rejects_a_different_valid_fernet_key_without_leaking_plaintext(monkeypatch):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    encrypted = model_api.encrypt_api_key('sk-sensitive-value')
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))

    with pytest.raises(ModelAPIError) as error:
        model_api.decrypt_api_key(encrypted)
    assert '无法解密' in str(error.value)
    assert 'sk-sensitive-value' not in str(error.value)


@pytest.mark.parametrize(
    ('replacement', 'expected_status'),
    [('missing', 'key_missing'), ('different', 'cannot_decrypt')],
)
def test_railway_start_keeps_website_available_when_saved_keys_need_repair(
        model_lab, monkeypatch, capsys, replacement, expected_status):
    monkeypatch.setenv('DB_PATH', str(model_lab['path']))
    if replacement == 'missing':
        monkeypatch.delenv('MODEL_CREDENTIAL_KEY', raising=False)
    else:
        monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))

    launched = []
    monkeypatch.setattr(railway_start.os, 'execv', lambda *args: launched.append(args))
    railway_start.main()

    assert len(launched) == 1
    assert 'MODEL CREDENTIAL RECOVERY REQUIRED' in capsys.readouterr().err
    issues = railway_start.validate_stored_model_credentials(model_lab['path'])
    assert issues[0]['status'] == expected_status
    assert issues[0]['profile_id'] == model_lab['profile_id']


@pytest.mark.parametrize('key_state', ['valid', 'missing'])
def test_empty_model_ciphertext_is_detected_without_blocking_website(
        model_lab, monkeypatch, capsys, key_state):
    with db_open(model_lab['path']) as db:
        db.execute(
            'UPDATE model_profiles SET api_key_encrypted=? WHERE id=?',
            ('', model_lab['profile_id']),
        )
    monkeypatch.setenv('DB_PATH', str(model_lab['path']))
    if key_state == 'missing':
        monkeypatch.delenv('MODEL_CREDENTIAL_KEY', raising=False)

    launched = []
    monkeypatch.setattr(railway_start.os, 'execv', lambda *args: launched.append(args))
    railway_start.main()

    assert len(launched) == 1
    assert 'status=empty_ciphertext' in capsys.readouterr().err
    issues = railway_start.validate_stored_model_credentials(model_lab['path'])
    assert issues[0]['status'] == 'empty_ciphertext'


def test_damaged_ciphertext_is_reported_but_does_not_block_manual_site_access(
        model_lab, monkeypatch, capsys):
    with db_open(model_lab['path']) as db:
        db.execute(
            'UPDATE model_profiles SET api_key_encrypted=? WHERE id=?',
            ('not-a-valid-fernet-token', model_lab['profile_id']),
        )
    monkeypatch.setenv('DB_PATH', str(model_lab['path']))
    launched = []
    monkeypatch.setattr(railway_start.os, 'execv', lambda *args: launched.append(args))

    railway_start.main()

    assert len(launched) == 1
    assert 'status=cannot_decrypt' in capsys.readouterr().err


def test_broken_model_profile_can_be_repaired_without_changing_match_data(model_lab):
    admin, base, profile_id, path = (
        model_lab['admin'], model_lab['base'], model_lab['profile_id'], model_lab['path']
    )
    with db_open(path) as db:
        db.execute(
            "UPDATE model_profiles SET api_key_encrypted='' WHERE id=?",
            (profile_id,),
        )
        before = {
            'rooms': db.execute('SELECT COUNT(*) FROM rooms').fetchone()[0],
            'users': db.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'members': db.execute('SELECT COUNT(*) FROM members').fetchone()[0],
        }

    state = admin.get('/api/me/models').json()
    assert state['models'][0]['credential_status'] == 'missing'
    payload = dict(model_lab['profile_body'])
    payload['api_key'] = 'sk-recovered-provider-key'
    me = admin.get('/api/me').json()
    response = admin.put(
        f'/api/me/models/{profile_id}',
        json=payload,
        headers={'X-CSRF-Token': me['user']['csrf']},
    )
    assert response.status_code == 200, response.text

    with db_open(path) as db:
        row = db.execute(
            'SELECT api_key_encrypted FROM model_profiles WHERE id=?', (profile_id,)
        ).fetchone()
        after = {
            'rooms': db.execute('SELECT COUNT(*) FROM rooms').fetchone()[0],
            'users': db.execute('SELECT COUNT(*) FROM users').fetchone()[0],
            'members': db.execute('SELECT COUNT(*) FROM members').fetchone()[0],
        }
    assert model_api.decrypt_api_key(row['api_key_encrypted']) == 'sk-recovered-provider-key'
    assert after == before


def test_railway_start_allows_manual_mode_without_model_profiles(tmp_path, monkeypatch):
    path = tmp_path / 'manual-mode.sqlite3'
    seed_admin(path, 'manual_admin', '手动模式主持人', 'manual-admin-password')
    monkeypatch.setenv('DB_PATH', str(path))
    monkeypatch.delenv('MODEL_CREDENTIAL_KEY', raising=False)

    launched = []
    monkeypatch.setattr(railway_start.os, 'execv', lambda *args: launched.append(args))
    railway_start.main()

    assert len(launched) == 1
    assert launched[0][1][2:5] == ['uvicorn', 'app:app', '--host']


def test_model_api_encryption_and_dns_pinning_preserves_tls_hostname(monkeypatch):
    key = Fernet.generate_key().decode('ascii')
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', key)
    token = model_api.encrypt_api_key('sk-private-value')
    assert token != 'sk-private-value'
    assert model_api.decrypt_api_key(token) == 'sk-private-value'

    with pytest.raises(ModelAPIError):
        model_api.validate_base_url('http://api.example.com/v1')
    with pytest.raises(ModelAPIError):
        model_api.validate_base_url('https://127.0.0.1/v1')

    dns_answers = ['93.184.216.34', '127.0.0.1']
    dns_calls = []

    def rebinding_getaddrinfo(host, port, type, proto):
        dns_calls.append((host, port))
        ip = dns_answers[min(len(dns_calls) - 1, len(dns_answers) - 1)]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port))]

    monkeypatch.setattr(model_api.socket, 'getaddrinfo', rebinding_getaddrinfo)
    observed = {'connect_hosts': [], 'sni': None, 'request': b''}
    response_body = json.dumps({
        'choices': [{'message': {'content': 'mock response'}}],
        'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5},
    }).encode('utf-8')
    wire_response = (
        b'HTTP/1.1 200 OK\r\n'
        b'Content-Type: application/json\r\n'
        + f'Content-Length: {len(response_body)}\r\n'.encode('ascii')
        + b'Connection: close\r\n\r\n'
        + response_body
    )

    class FakeStream(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.remaining = wire_response

        async def read(self, max_bytes, timeout=None):
            if not self.remaining:
                return b''
            chunk, self.remaining = self.remaining[:max_bytes], self.remaining[max_bytes:]
            return chunk

        async def write(self, buffer, timeout=None):
            observed['request'] += buffer

        async def aclose(self):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            observed['sni'] = server_hostname
            return self

        def get_extra_info(self, info):
            return None

    class FakeBackend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            observed['connect_hosts'].append(host)
            return FakeStream()

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise AssertionError('unix socket must not be used')

        async def sleep(self, seconds):
            pass

    profile = {'base_url': 'https://api.example.com/v1', 'model': 'mock-model',
               'temperature': 0.3, 'max_tokens': 256, 'timeout_seconds': 10}
    result = asyncio.run(model_api.chat_completion(
        profile, [{'role': 'user', 'content': 'test'}], 'sk-private-value',
        network_backend=FakeBackend()
    ))
    assert result['text'] == 'mock response'
    assert result['usage']['total_tokens'] == 5
    assert dns_calls == [('api.example.com', 443)]
    assert observed['connect_hosts'] == ['93.184.216.34']
    assert observed['sni'] == 'api.example.com'
    assert b'Host: api.example.com' in observed['request']
    assert b'Authorization: Bearer sk-private-value' in observed['request']


async def async_post(client, path, body=None):
    me = await client.get('/api/me')
    headers = {}
    if me.status_code == 200 and me.json().get('user'):
        headers['X-CSRF-Token'] = me.json()['user']['csrf']
    return await client.post(path, json=body or {}, headers=headers)


def test_slow_dns_validation_does_not_hold_sqlite_write_lock(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    path = tmp_path / 'dns-lock.sqlite3'
    seed_admin(path, 'lock_admin', '锁测试主持人', 'lock-admin-password')
    app = create_app(path)

    original_db_open = app_module.db_open

    def short_timeout_db_open(db_path):
        db = sqlite3.connect(str(db_path), timeout=0.2, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=200')
        return db

    entered = threading.Event()
    release = threading.Event()

    def slow_validate(value):
        entered.set()
        if not release.wait(3):
            raise ModelAPIError('测试 DNS 等待超时')
        return value.strip().rstrip('/')

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://testserver') as admin:
            login = await async_post(admin, '/api/login',
                                     {'username': 'lock_admin', 'password': 'lock-admin-password'})
            assert login.status_code == 200, login.text
            room = await async_post(admin, '/api/rooms', {'title': 'DNS 锁探针'})
            assert room.status_code == 200, room.text
            base = f"/api/rooms/{room.json()['room_id']}"

            monkeypatch.setattr(app_module, 'db_open', short_timeout_db_open)
            monkeypatch.setattr(app_module, 'validate_base_url', slow_validate)
            save_task = asyncio.create_task(async_post(admin, base + '/models', {
                'name': '慢 DNS',
                'base_url': 'https://api.example.com/v1',
                'model': 'slow-model',
                'api_key': 'sk-lock-test',
                'temperature': 0.7,
                'max_tokens': 256,
                'timeout_seconds': 30,
            }))
            try:
                assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), 2)
                concurrent = await asyncio.wait_for(
                    async_post(admin, base + '/ai', {'name': '并发写入 AI'}),
                    1.5,
                )
                assert concurrent.status_code == 200, concurrent.text
            finally:
                release.set()
            saved = await asyncio.wait_for(save_task, 2)
            assert saved.status_code == 200, saved.text

    try:
        asyncio.run(scenario())
    finally:
        monkeypatch.setattr(app_module, 'db_open', original_db_open)


def test_ai_judge_discards_verdict_if_match_evidence_changes(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))
    path = tmp_path / 'stale-verdict.sqlite3'
    seed_admin(path, 'stale_admin', '版本探针主持人', 'stale-admin-password')
    app = create_app(path)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as admin,
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as p1,
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as p2,
        ):
            assert (await async_post(admin, '/api/login', {
                'username': 'stale_admin', 'password': 'stale-admin-password'
            })).status_code == 200
            room_resp = await async_post(admin, '/api/rooms', {'title': '判决版本探针'})
            assert room_resp.status_code == 200, room_resp.text
            room = room_resp.json()
            base = f"/api/rooms/{room['room_id']}"

            assert (await async_post(p1, '/api/register', {
                'username': 'stale_a', 'display_name': 'A 真人',
                'password': 'stale-a-password', 'code': room['code']
            })).status_code == 200
            assert (await async_post(p2, '/api/register', {
                'username': 'stale_b', 'display_name': 'B 真人',
                'password': 'stale-b-password', 'code': room['code']
            })).status_code == 200

            profile_resp = await async_post(admin, base + '/models', {
                'name': '判决模型',
                'base_url': 'https://api.example.com/v1',
                'model': 'judge-model',
                'api_key': 'sk-stale-test',
                'temperature': 0.2,
                'max_tokens': 512,
                'timeout_seconds': 30,
            })
            assert profile_resp.status_code == 200, profile_resp.text
            judge_resp = await async_post(admin, base + '/ai-judges', {
                'name': '版本探针 AI 裁判',
                'model_profile_id': profile_resp.json()['id'],
            })
            assert judge_resp.status_code == 200, judge_resp.text
            judge_id = judge_resp.json()['id']

            assert (await async_post(admin, base + f'/ai-judges/{judge_id}/questions',
                                     {'text': '第一轮问题'})).status_code == 200
            assert (await async_post(admin, base + '/start')).status_code == 200
            assert (await async_post(p1, base + '/answers', {'text': '第一轮 A'})).status_code == 200
            assert (await async_post(p2, base + '/answers', {'text': '第一轮 B'})).status_code == 200

            started = asyncio.Event()
            release = asyncio.Event()

            async def delayed_verdict(profile, messages, api_key):
                started.set()
                await release.wait()
                return {
                    'text': json.dumps({
                        'predictions': {'A': 'Human', 'B': 'AI'},
                        'evidence': '只根据第一轮资料生成的旧判决。',
                        'confidence': 70,
                        'weakness': '只看过第一轮。',
                        'possible_error': '资料可能变化。',
                    }, ensure_ascii=False),
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20},
                }

            monkeypatch.setattr(app_module, 'chat_completion', delayed_verdict)
            verdict_task = asyncio.create_task(async_post(admin, base + '/ai-judges/verdicts/generate'))
            await asyncio.wait_for(started.wait(), 1)

            assert (await async_post(admin, base + '/next')).status_code == 200
            assert (await async_post(admin, base + f'/ai-judges/{judge_id}/questions',
                                     {'text': '第二轮新增问题'})).status_code == 200
            assert (await async_post(admin, base + '/start')).status_code == 200
            assert (await async_post(p1, base + '/answers', {'text': '第二轮 A'})).status_code == 200
            assert (await async_post(p2, base + '/answers', {'text': '第二轮 B'})).status_code == 200
            release.set()

            verdict_resp = await asyncio.wait_for(verdict_task, 2)
            assert verdict_resp.status_code == 200, verdict_resp.text
            result = verdict_resp.json()
            assert result['generated'] == 0
            assert len(result['failed']) == 1
            assert '资料在模型判断期间发生变化' in result['failed'][0]['error']
            state = (await admin.get(base)).json()
            assert state['judges'][0]['locked'] is False
            assert state['rounds'][-1]['number'] == 2
            assert state['rounds'][-1]['status'] == 'revealed'

    asyncio.run(scenario())

@pytest.mark.parametrize('mutation', ['profile_deleted', 'owner_deleted', 'session_revoked', 'seat_rebound'])
def test_queued_model_answer_is_revalidated_before_provider_call(tmp_path, monkeypatch, mutation):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))
    path = tmp_path / f'queued-preflight-{mutation}.sqlite3'
    seed_admin(path, 'queue_admin', '排队调用主持人', 'queue-admin-password')
    app = create_app(path)

    started = 0
    started_aliases = []
    four_started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_chat(profile, messages, api_key):
        nonlocal started
        started += 1
        started_aliases.append(profile.get('alias'))
        if started == 4:
            four_started.set()
        await release.wait()
        return {'text': '排队调用结果', 'usage': {}}

    monkeypatch.setattr(app_module, 'chat_completion', blocked_chat)

    async def scenario():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with (
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as admin,
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as mutator,
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as p1,
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as p2,
        ):
            assert (await async_post(admin, '/api/login', {
                'username': 'queue_admin', 'password': 'queue-admin-password',
            })).status_code == 200
            admin_me = (await admin.get('/api/me')).json()['user']
            room_response = await async_post(admin, '/api/rooms', {'title': '排队调用二次校验'})
            assert room_response.status_code == 200, room_response.text
            room = room_response.json()
            base = f"/api/rooms/{room['room_id']}"
            for client, username in ((p1, 'queue_human_a'), (p2, 'queue_human_b')):
                response = await async_post(client, '/api/register', {
                    'username': username, 'password': 'queue-human-password', 'code': room['code'],
                })
                assert response.status_code == 200, response.text
            profile_response = await async_post(admin, '/api/me/models', {
                'name': '排队模型', 'base_url': 'https://api.example.com/v1',
                'model': 'queued-model', 'api_key': 'sk-queued',
                'temperature': 0.7, 'max_tokens': 512, 'timeout_seconds': 30,
            })
            assert profile_response.status_code == 200, profile_response.text
            profile_id = profile_response.json()['id']
            ai_aliases = []
            for index in range(5):
                response = await async_post(admin, base + '/ai', {
                    'name': f'排队 AI {index + 1}', 'model_profile_id': profile_id,
                })
                assert response.status_code == 200, response.text
                ai_aliases.append(response.json()['alias'])
            question = await async_post(admin, base + '/question', {'text': '排队校验问题'})
            assert question.status_code == 200, question.text

            if mutation == 'owner_deleted':
                registered = await async_post(mutator, '/api/register', {
                    'username': 'queue_second_admin', 'password': 'second-admin-password',
                })
                assert registered.status_code == 200, registered.text
                with db_open(path) as db:
                    second_id = db.execute(
                        'SELECT id FROM users WHERE username=?', ('queue_second_admin',)
                    ).fetchone()[0]
                    db.execute('UPDATE users SET is_admin=1 WHERE id=?', (second_id,))
            else:
                assert (await async_post(mutator, '/api/login', {
                    'uid': admin_me['uid'], 'password': 'queue-admin-password',
                })).status_code == 200

            generate_task = asyncio.create_task(async_post(admin, base + '/ai/answers/generate'))
            await asyncio.wait_for(four_started.wait(), 3)
            assert started == 4

            me = (await mutator.get('/api/me')).json()['user']
            if mutation == 'profile_deleted':
                changed = await mutator.request(
                    'DELETE', f'/api/me/models/{profile_id}', json={},
                    headers={'X-CSRF-Token': me['csrf']},
                )
            elif mutation == 'owner_deleted':
                changed = await mutator.request(
                    'DELETE', f"/api/admin/users/{admin_me['uid']}", json={'confirm': True},
                    headers={'X-CSRF-Token': me['csrf']},
                )
            elif mutation == 'session_revoked':
                changed = await mutator.post(
                    '/api/me/password',
                    json={
                        'current_password': 'queue-admin-password',
                        'new_password': 'queue-admin-password-new',
                        'confirm_password': 'queue-admin-password-new',
                    },
                    headers={'X-CSRF-Token': me['csrf']},
                )
            else:
                queued_alias = next(alias for alias in ai_aliases if alias not in started_aliases)
                changed = await mutator.put(
                    base + f"/ai/{queued_alias}/model",
                    json={'model_profile_id': None},
                    headers={'X-CSRF-Token': me['csrf']},
                )
            assert changed.status_code == 200, changed.text

            release.set()
            generated = await asyncio.wait_for(generate_task, 3)
            assert generated.status_code == 200, generated.text
            payload = generated.json()
            assert started == 4, '第五个排队任务不应在状态失效后触发服务商调用'
            if mutation == 'seat_rebound':
                assert payload['generated'] == 4
                assert payload['skipped'] == 1
            else:
                assert payload['generated'] == 0
                assert payload['skipped'] == 5

    asyncio.run(scenario())

@pytest.mark.parametrize('participant_count', [0, 1])
def test_ai_judge_question_generation_rejects_insufficient_roster_before_provider_call(
        model_lab, monkeypatch, participant_count):
    admin, base, path = model_lab['admin'], model_lab['base'], model_lab['path']
    post(admin, base + '/ai-judges', {
        'name': '前置条件 AI 裁判',
        'model_profile_id': model_lab['profile_id'],
    })
    if participant_count == 0:
        with db_open(path) as db:
            db.execute('DELETE FROM members WHERE room_id=?', (int(base.rsplit('/', 1)[1]),))
    else:
        p2_id = model_lab['p2'].get('/api/me').json()['user']['id']
        with db_open(path) as db:
            db.execute('DELETE FROM members WHERE room_id=? AND user_id=?',
                       (int(base.rsplit('/', 1)[1]), p2_id))

    provider_calls = []

    async def unexpected_provider_call(*args, **kwargs):
        provider_calls.append(True)
        raise AssertionError('人数不足时不应调用模型服务商')

    monkeypatch.setattr(app_module, 'chat_completion', unexpected_provider_call)
    result = post(admin, base + '/ai-judges/questions/generate', expected=409)
    assert '两名在场参赛者' in result['detail']
    assert provider_calls == []


def test_ai_judge_question_generation_reports_provider_failure(model_lab, monkeypatch):
    admin, base = model_lab['admin'], model_lab['base']
    post(admin, base + '/ai-judges', {
        'name': '失败反馈 AI 裁判',
        'model_profile_id': model_lab['profile_id'],
    })

    async def provider_failure(*args, **kwargs):
        raise ModelAPIError('模型服务返回 HTTP 503。')

    monkeypatch.setattr(app_module, 'chat_completion', provider_failure)
    result = post(admin, base + '/ai-judges/questions/generate')
    assert result['generated'] == 0
    assert len(result['failed']) == 1
    assert 'HTTP 503' in result['failed'][0]['error']
    room = admin.get(base).json()
    assert room['rounds'][0]['questions'] == []


def test_queued_ai_judge_question_is_discarded_when_roster_changes(model_lab, monkeypatch):
    admin, base = model_lab['admin'], model_lab['base']
    for index in range(5):
        post(admin, base + '/ai-judges', {
            'name': f'排队 AI 裁判 {index + 1}',
            'model_profile_id': model_lab['profile_id'],
        })
    room_info = admin.get(base).json()
    invite_code = room_info['invite_code']
    cookie = admin.cookies.get(app_module.COOKIE)
    started = 0

    async def scenario():
        nonlocal started
        transport = httpx.ASGITransport(app=model_lab['app'], raise_app_exceptions=False)
        four_started = asyncio.Event()
        release = asyncio.Event()

        async def blocked_provider(*args, **kwargs):
            nonlocal started
            started += 1
            if started == 4:
                four_started.set()
            await release.wait()
            return {'text': '名单变化后不应保存的问题', 'usage': {}}

        monkeypatch.setattr(app_module, 'chat_completion', blocked_provider)
        async with (
            httpx.AsyncClient(transport=transport, base_url='http://testserver',
                              cookies={app_module.COOKIE: cookie}) as async_admin,
            httpx.AsyncClient(transport=transport, base_url='http://testserver') as third_player,
        ):
            generate_task = asyncio.create_task(
                async_post(async_admin, base + '/ai-judges/questions/generate'))
            await asyncio.wait_for(four_started.wait(), 3)
            assert started == 4
            registered = await async_post(third_player, '/api/register', {
                'username': 'question_roster_change',
                'display_name': '新增参赛者',
                'password': 'question-player-password',
                'code': invite_code,
            })
            assert registered.status_code == 200, registered.text
            release.set()
            response = await asyncio.wait_for(generate_task, 3)
            assert response.status_code == 200, response.text
            payload = response.json()
            assert started == 4, '第五个排队任务在名单变化后不应触发服务商调用'
            assert payload['generated'] == 0
            assert payload['skipped'] == 5
            assert all(item.get('reason') for item in payload['results'] if item['status'] == 'skipped')
            current = (await async_admin.get(base)).json()
            assert current['rounds'][0]['questions'] == []

    asyncio.run(scenario())

@pytest.mark.parametrize('failure,expected_message', [
    ('http', 'HTTP 503'),
    ('timeout', '超时'),
    ('empty', 'Chat Completions 格式'),
    ('malformed', 'Chat Completions 格式'),
    ('invalid_json', 'Chat Completions 格式'),
])
def test_chat_completion_reports_http_timeout_and_invalid_content_safely(
        monkeypatch, failure, expected_message):
    endpoint = model_api.ResolvedEndpoint(
        base_url='https://api.example.com/v1',
        host='api.example.com',
        port=443,
        addresses=('93.184.216.34',),
    )
    monkeypatch.setattr(model_api, 'resolve_base_url', lambda value: endpoint)

    if failure == 'http':
        body = b'{"error":{"message":"provider unavailable"}}'
        status = 503
    elif failure == 'empty':
        body = b'{"choices":[{"message":{"content":"  "}}]}'
        status = 200
    elif failure == 'malformed':
        body = b'{"choices":[]}'
        status = 200
    elif failure == 'invalid_json':
        body = b'{not-json'
        status = 200
    else:
        body = b''
        status = 200

    wire_response = (
        f'HTTP/1.1 {status} TEST\\r\\n'.encode('ascii')
        + f'Content-Length: {len(body)}\\r\\n'.encode('ascii')
        + b'Connection: close\\r\\n\\r\\n'
        + body
    )

    class FakeStream(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.remaining = wire_response

        async def read(self, max_bytes, timeout=None):
            if failure == 'timeout':
                raise httpcore.ReadTimeout('simulated timeout')
            if not self.remaining:
                return b''
            chunk, self.remaining = self.remaining[:max_bytes], self.remaining[max_bytes:]
            return chunk

        async def write(self, buffer, timeout=None):
            pass

        async def aclose(self):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            return self

        def get_extra_info(self, info):
            return None

    class FakeBackend(httpcore.AsyncNetworkBackend):
        async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
            assert host == '93.184.216.34'
            assert port == 443
            return FakeStream()

        async def connect_unix_socket(self, path, timeout=None, socket_options=None):
            raise AssertionError('model API must not use unix sockets')

        async def sleep(self, seconds):
            pass

    profile = {
        'base_url': endpoint.base_url,
        'model': 'mock-model',
        'temperature': 0.3,
        'max_tokens': 128,
        'timeout_seconds': 10,
    }
    with pytest.raises(ModelAPIError) as caught:
        asyncio.run(model_api.chat_completion(
            profile, [{'role': 'user', 'content': 'test'}], 'sk-never-log-this',
            network_backend=FakeBackend(),
        ))
    assert expected_message in str(caught.value)
    assert 'sk-never-log-this' not in str(caught.value)


def test_ai_judge_question_generation_reports_empty_model_content(model_lab, monkeypatch):
    admin, base = model_lab['admin'], model_lab['base']
    post(admin, base + '/ai-judges', {
        'name': '空内容 AI 裁判',
        'model_profile_id': model_lab['profile_id'],
    })

    async def empty_provider_response(*args, **kwargs):
        return {'text': '   ', 'usage': {}}

    monkeypatch.setattr(app_module, 'chat_completion', empty_provider_response)
    result = post(admin, base + '/ai-judges/questions/generate')
    assert result['generated'] == 0
    assert len(result['failed']) == 1
    assert '内容需要 1～3000 字' in result['failed'][0]['error']
    room = admin.get(base).json()
    assert room['rounds'][0]['questions'] == []

