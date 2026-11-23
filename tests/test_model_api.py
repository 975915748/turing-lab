"""v1.6.0 OpenAI-compatible model configuration and autonomous AI workflow tests."""
import asyncio
import json
import socket

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app as app_module
import model_api
from app import create_app, db_open, seed_admin
from model_api import ModelAPIError


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
    profile = post(admin, base + '/models', profile_body)
    return {
        'path': path, 'app': app, 'admin': admin, 'p1': p1, 'p2': p2,
        'base': base, 'profile_id': profile['id'], 'profile_body': profile_body,
    }


def test_model_profile_key_is_encrypted_and_never_returned_to_browser(model_lab):
    admin, base, path = model_lab['admin'], model_lab['base'], model_lab['path']
    info = admin.get(base).json()
    assert info['model_api_enabled'] is True
    assert info['model_profiles'][0]['has_api_key'] is True
    assert 'api_key' not in info['model_profiles'][0]
    assert 'api_key_encrypted' not in info['model_profiles'][0]
    with db_open(path) as db:
        encrypted = db.execute('SELECT api_key_encrypted FROM model_profiles').fetchone()[0]
    assert encrypted != model_lab['profile_body']['api_key']
    assert model_lab['profile_body']['api_key'] not in encrypted
    tested = post(admin, base + f"/models/{model_lab['profile_id']}/test")
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
    response = admin.put(base + f'/models/{profile_id}', json=payload,
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


def test_model_api_encryption_url_validation_and_chat_completion(monkeypatch):
    key = Fernet.generate_key().decode('ascii')
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', key)
    token = model_api.encrypt_api_key('sk-private-value')
    assert token != 'sk-private-value'
    assert model_api.decrypt_api_key(token) == 'sk-private-value'

    with pytest.raises(ModelAPIError):
        model_api.validate_base_url('http://api.example.com/v1')
    with pytest.raises(ModelAPIError):
        model_api.validate_base_url('https://127.0.0.1/v1')

    monkeypatch.setattr(model_api.socket, 'getaddrinfo',
                        lambda host, port, type: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', port))])
    assert model_api.validate_base_url('https://api.example.com/v1') == 'https://api.example.com/v1'

    async def handler(request):
        assert request.url.path == '/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer sk-private-value'
        payload = json.loads(request.content)
        assert payload['model'] == 'mock-model'
        return httpx.Response(200, json={
            'choices': [{'message': {'content': 'mock response'}}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5},
        })

    transport = httpx.MockTransport(handler)
    profile = {'base_url': 'https://api.example.com/v1', 'model': 'mock-model',
               'temperature': 0.3, 'max_tokens': 256, 'timeout_seconds': 10}
    result = asyncio.run(model_api.chat_completion(profile, [{'role': 'user', 'content': 'test'}],
                                                    'sk-private-value', transport=transport))
    assert result['text'] == 'mock response'
    assert result['usage']['total_tokens'] == 5
