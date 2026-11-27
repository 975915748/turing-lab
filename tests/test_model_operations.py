"""Durable per-seat model tasks, quota reservations, retries and owner-scoped usage."""
import asyncio
import concurrent.futures
import json
import threading

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import app as app_module
from app import (
    anonymous_match_packet, anonymous_player_history, create_app, db_open, seed_admin,
)
from model_api import ModelAPIError


def post(client, path, body=None, expected=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']} if me.status_code == 200 else {}
    response = client.post(path, json=body or {}, headers=headers)
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json() if response.content else {}


def put(client, path, body, expected=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']}
    response = client.put(path, json=body, headers=headers)
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json() if response.content else {}


def delete(client, path, expected=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']}
    response = client.delete(path, headers=headers)
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json() if response.content else {}


@pytest.fixture
def operation_lab(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))
    state = {'calls': 0, 'fail_next': 0, 'block': None, 'requests': []}
    lock = threading.Lock()

    async def fake_chat(profile, messages, api_key):
        with lock:
            state['calls'] += 1
            state['requests'].append({
                'model': profile.get('model'),
                'max_tokens': profile.get('max_tokens'),
                'temperature': profile.get('temperature'),
                'api_key': api_key,
                'system': messages[0]['content'] if messages else '',
            })
            fail = state['fail_next'] > 0
            if fail:
                state['fail_next'] -= 1
            block = state['block']
        if block:
            block['profile_model']=profile['model']
            block['started'].set()
            await asyncio.to_thread(block['release'].wait, 10)
        if fail:
            raise ModelAPIError('模拟模型服务超时')
        system = messages[0]['content']
        if 'connection test' in system:
            return {'text': 'OK', 'usage': {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}}
        if '严格返回 JSON 对象' in system:
            return {'text': json.dumps({
                'predictions': {'A': 'Human', 'B': 'AI'},
                'evidence': '匿名回答中存在可复核的表达差异。',
                'confidence': 80,
                'weakness': '样本数量有限。',
                'possible_error': '真人也可能采用类似表达。',
            }, ensure_ascii=False), 'usage': {'prompt_tokens': 12, 'completion_tokens': 8, 'total_tokens': 20}}
        if '你是多人图灵测试的独立裁判' in system:
            return {'text': '如果你最近改变过一个重要看法，是什么促使你改变？', 'usage': {}}
        return {'text': '我会先观察具体情况，再决定如何处理。', 'usage': {'prompt_tokens': 8, 'completion_tokens': 4, 'total_tokens': 12}}

    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
    path = tmp_path / 'model-operations.sqlite3'
    seed_admin(path, 'ops_admin', '调用管理管理员', 'ops-admin-password')
    app = create_app(path)
    admin, p1, p2 = TestClient(app), TestClient(app), TestClient(app)
    post(admin, '/api/login', {'username': 'ops_admin', 'password': 'ops-admin-password'})
    room = post(admin, '/api/rooms', {'title': '模型调用管理验收'})
    room_id = room['room_id']
    base = f'/api/rooms/{room_id}'
    post(p1, '/api/register', {'username': 'ops_player_a', 'display_name': '真人 A',
                               'password': 'ops-player-a-password', 'code': room['code']})
    post(p2, '/api/register', {'username': 'ops_player_b', 'display_name': '真人 B',
                               'password': 'ops-player-b-password', 'code': room['code']})
    profile_body = {
        'name': '调用记录测试配置',
        'base_url': 'https://api.example.com/v1',
        'model': 'ops-test-model',
        'api_key': 'sk-ops-secret-never-return',
        'temperature': 0.4,
        'max_tokens': 1024,
        'timeout_seconds': 30,
    }
    profile = post(admin, '/api/me/models', profile_body)
    return {'path': path, 'app': app, 'admin': admin, 'p1': p1, 'p2': p2,
            'base': base, 'room_id': room_id, 'profile_id': profile['id'],
            'profile_body': profile_body, 'state': state}


def add_ai_judge(client, base, name, profile_id):
    return post(client, base + '/ai-judges', {'name': name, 'model_profile_id': profile_id})


def test_persona_history_windows_are_bounded_and_participant_history_is_private(operation_lab):
    lab = operation_lab
    admin, base = lab['admin'], lab['base']
    ai = post(admin, base + '/ai', {'name': '仅使用自身历史的 AI'})
    room = admin.get(base).json()
    human_aliases = [item['alias'] for item in room['humans']]
    assert len(human_aliases) == 2

    with db_open(lab['path']) as db:
        first = db.execute('SELECT id FROM rounds WHERE room_id=? AND number=1',
                           (lab['room_id'],)).fetchone()
        for number in range(1, 7):
            if number == 1:
                round_id = first['id']
                db.execute("UPDATE rounds SET question=?,status='revealed' WHERE id=?",
                           (f'QUESTION_{number}', round_id))
            else:
                cur = db.execute('''INSERT INTO rounds(room_id,number,question,status,created_at)
                    VALUES(?,?,?,'revealed',?)''',
                    (lab['room_id'], number, f'QUESTION_{number}', f'2026-11-27T12:0{number}:00+00:00'))
                round_id = cur.lastrowid
            for alias, marker in ((human_aliases[0], 'HUMAN_A'), (human_aliases[1], 'HUMAN_B'),
                                  (ai['alias'], 'AI_SELF')):
                db.execute('''INSERT INTO answers(round_id,alias,text,submitted_at)
                    VALUES(?,?,?,?)''',
                    (round_id, alias, f'{marker}_ANSWER_{number}', f'2026-11-27T12:1{number}:00+00:00'))

        packet_zero = anonymous_match_packet(db, lab['room_id'], 6, history_rounds=0)
        packet_five = anonymous_match_packet(db, lab['room_id'], 6, history_rounds=5)
        packet_hundred = anonymous_match_packet(db, lab['room_id'], 6, history_rounds=100)
        full_packet = anonymous_match_packet(db, lab['room_id'])
        own_zero = anonymous_player_history(db, lab['room_id'], ai['alias'], 6, history_rounds=0)
        own_five = anonymous_player_history(db, lab['room_id'], ai['alias'], 6, history_rounds=5)
        own_hundred = anonymous_player_history(db, lab['room_id'], ai['alias'], 6, history_rounds=100)

    assert 'QUESTION_1' not in packet_zero and 'QUESTION_6' not in packet_zero
    assert 'QUESTION_1' not in packet_five and all(f'QUESTION_{n}' in packet_five for n in range(2, 7))
    assert all(f'QUESTION_{n}' in packet_hundred for n in range(1, 7))
    assert all(f'QUESTION_{n}' in full_packet for n in range(1, 7))
    assert 'AI_SELF_ANSWER_1' not in own_zero
    assert 'AI_SELF_ANSWER_1' not in own_five
    assert all(f'AI_SELF_ANSWER_{n}' in own_five for n in range(2, 7))
    assert all(f'AI_SELF_ANSWER_{n}' in own_hundred for n in range(1, 7))
    assert 'HUMAN_A_ANSWER_' not in own_hundred
    assert 'HUMAN_B_ANSWER_' not in own_hundred


def test_persona_presets_change_mock_prompts_and_output_limits(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    judge = add_ai_judge(admin, base, '语言细节裁判', profile_id)
    analytical = post(admin, base + '/ai', {'name': '严谨参赛席', 'model_profile_id': profile_id})
    custom = post(admin, base + '/ai', {'name': '自定义参赛席', 'model_profile_id': profile_id})

    put(admin, base + f"/ai-judges/{judge['id']}/persona", {
        'preset_id': 'language_detail', 'custom_system_prompt': '',
        'output_length': 'short', 'history_rounds': 0,
    })
    put(admin, base + f"/ai/{analytical['alias']}/persona", {
        'preset_id': 'analytical', 'custom_system_prompt': '',
        'output_length': 'medium', 'history_rounds': 5,
    })
    custom_style = '使用自然口语，只给出直接回应，不使用分点列表。'
    put(admin, base + f"/ai/{custom['alias']}/persona", {
        'preset_id': 'custom', 'custom_system_prompt': custom_style,
        'output_length': 'short', 'history_rounds': 2,
    })

    questions = post(admin, base + '/ai-judges/questions/generate')
    assert questions['generated'] == 1
    post(admin, base + '/start')
    answers = post(admin, base + '/ai/answers/generate')
    assert answers['generated'] == 2

    requests = lab['state']['requests']
    judge_request = next(x for x in requests if '重点检查措辞' in x['system'])
    analytical_request = next(x for x in requests if '事实依据' in x['system'])
    custom_request = next(x for x in requests if custom_style in x['system'])
    assert judge_request['max_tokens'] == 256
    assert '约 80 个中文字符' in judge_request['system']
    assert analytical_request['max_tokens'] == 768
    assert '约 300 个中文字符' in analytical_request['system']
    assert custom_request['max_tokens'] == 256
    assert '约 80 个中文字符' in custom_request['system']
    assert {x['model'] for x in requests} == {'ops-test-model'}
    assert {x['temperature'] for x in requests} == {0.4}
    assert lab['state']['calls'] == 3


def test_concurrent_independent_question_generation_freezes_once(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    add_ai_judge(admin, base, '并发冻结裁判', profile_id)
    second_admin = TestClient(lab['app'])
    post(second_admin, '/api/login', {
        'username': 'ops_admin', 'password': 'ops-admin-password',
    })
    block = {'started': threading.Event(), 'release': threading.Event()}
    lab['state']['block'] = block

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, admin, base + '/ai-judges/questions/generate')
        assert block['started'].wait(5), '首个并发请求没有进入模型调用'
        second = pool.submit(post, second_admin, base + '/ai-judges/questions/generate')
        second_result = second.result(timeout=10)
        block['release'].set()
        first_result = first.result(timeout=15)

    lab['state']['block'] = None
    assert first_result['generated'] == 1
    assert second_result['generated'] == 0
    assert lab['state']['calls'] == 1
    room = admin.get(base).json()
    assert len(room['rounds'][0]['questions']) == 1
    with db_open(lab['path']) as db:
        assert db.execute(
            'SELECT COUNT(*) FROM match_persona_freezes WHERE room_id=?',
            (lab['room_id'],),
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM model_operation_tasks WHERE room_id=? AND purpose='judge_question'",
            (lab['room_id'],),
        ).fetchone()[0] == 1


def test_connection_and_game_calls_are_owner_scoped_and_cleaned_differently(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']

    # A connection test is retained in the personal ledger, but never consumes room quota.
    tested = post(admin, f'/api/me/models/{profile_id}/test')
    assert tested['ok'] is True
    assert admin.get(base).json()['model_request_count'] == 0
    connection_records = admin.get('/api/me/model-operations').json()
    assert connection_records['total'] == 1
    assert connection_records['records'][0]['purpose'] == 'connection_test'
    assert connection_records['records'][0]['usage_known'] is True
    assert connection_records['records'][0]['total_tokens'] == 5

    judge = add_ai_judge(admin, base, '自动出题裁判', profile_id)
    generated = post(admin, base + '/ai-judges/questions/generate')
    assert generated['generated'] == 1
    room = admin.get(base).json()
    assert len(room['model_operations']) == 1
    task = room['model_operations'][0]
    assert task['state'] == 'completed'
    assert task['request_succeeded'] is True
    assert task['result_applied'] is True
    assert task['usage_known'] is False
    assert room['model_request_count'] == 1

    records = admin.get('/api/me/model-operations').json()
    assert records['total'] == 2
    game_record = next(x for x in records['records'] if x['purpose'] == 'judge_question')
    assert game_record['request_started_at'] is not None
    assert game_record['request_succeeded'] is True
    assert game_record['result_applied'] is True
    assert game_record['total_tokens'] is None
    assert records['stats']['known_usage_records'] == 1
    assert records['stats']['total_tokens'] == 5

    # Other participants cannot inspect the room's model identities or operation list.
    player_room = lab['p1'].get(base).json()
    assert 'model_operations' not in player_room
    assert 'readiness' not in player_room
    assert player_room['ais'] == []
    assert player_room['judges']
    assert all('model_profile' not in judge and 'model_profile_id' not in judge
               for judge in player_room['judges'])
    assert lab['p1'].get('/api/me/model-operations').json()['total'] == 0

    # Deleting a profile removes its standalone tests but preserves game-call history.
    delete(admin, f'/api/me/models/{profile_id}')
    after_delete = admin.get('/api/me/model-operations').json()
    assert after_delete['total'] == 1
    assert after_delete['records'][0]['purpose'] == 'judge_question'
    assert after_delete['records'][0]['profile_name'] == '调用记录测试配置'
    assert 'sk-ops-secret-never-return' not in json.dumps(after_delete, ensure_ascii=False)
    forbidden_fields = {'api_key', 'api_key_encrypted', 'prompt', 'messages',
                        'response', 'request_body', 'response_body'}
    assert forbidden_fields.isdisjoint(after_delete['records'][0])


def test_atomic_request_limit_blocks_last_slot_and_retries_only_blocked_seat(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    add_ai_judge(admin, base, '额度席位 A', profile_id)
    add_ai_judge(admin, base, '额度席位 B', profile_id)
    put(admin, base + '/model-request-limit', {'limit': 1})

    result = post(admin, base + '/ai-judges/questions/generate')
    assert result['generated'] == 1
    assert result['blocked'] == 1
    assert lab['state']['calls'] == 1
    room = admin.get(base).json()
    assert room['model_request_count'] == 1
    assert room['model_request_limit'] == 1
    blocked = next(task for task in room['model_operations'] if task['state'] == 'blocked')
    completed = next(task for task in room['model_operations'] if task['state'] == 'completed')
    assert blocked['request_started_at'] is None
    assert completed['request_started_at'] is not None

    put(admin, base + '/model-request-limit', {'limit': 2})
    retried = post(admin, f"{base}/model-operations/{blocked['id']}/retry")
    assert retried['generated'] == 1
    room = admin.get(base).json()
    assert room['model_request_count'] == 2
    assert lab['state']['calls'] == 2
    assert len(room['rounds'][0]['questions']) == 2
    retry_task = next(task for task in room['model_operations'] if task['retry_of'] == blocked['id'])
    assert retry_task['state'] == 'completed'
    assert retry_task['target_key'] == blocked['target_key']
    assert completed['id'] != retry_task['id']


def test_failed_attempt_is_preserved_when_single_seat_retry_succeeds(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    judge = add_ai_judge(admin, base, '会失败一次的裁判', profile_id)
    lab['state']['fail_next'] = 1

    result = post(admin, base + '/ai-judges/questions/generate')
    assert len(result['failed']) == 1
    room = admin.get(base).json()
    failed = next(task for task in room['model_operations'] if task['state'] == 'failed')
    assert failed['target_key'] == str(judge['id'])
    assert failed['request_started_at'] is not None
    assert failed['request_succeeded'] is False
    assert room['model_request_count'] == 1

    retried = post(admin, f"{base}/model-operations/{failed['id']}/retry")
    assert retried['generated'] == 1
    room = admin.get(base).json()
    old_attempt = next(task for task in room['model_operations'] if task['id'] == failed['id'])
    new_attempt = next(task for task in room['model_operations'] if task['retry_of'] == failed['id'])
    assert old_attempt['state'] == 'failed'
    assert new_attempt['state'] == 'completed'
    assert new_attempt['result_applied'] is True
    assert room['model_request_count'] == 2
    assert len(room['rounds'][0]['questions']) == 1


def test_frozen_model_retry_uses_rotated_api_key_and_records_credential_version(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    judge = add_ai_judge(admin, base, '凭据轮换恢复裁判', profile_id)
    lab['state']['fail_next'] = 1

    first = post(admin, base + '/ai-judges/questions/generate')
    assert len(first['failed']) == 1
    room = admin.get(base).json()
    failed = next(task for task in room['model_operations'] if task['state'] == 'failed')
    assert failed['target_key'] == str(judge['id'])
    assert lab['state']['requests'][-1]['api_key'] == lab['profile_body']['api_key']

    rotated_key = 'sk-ops-rotated-provider-key'
    updated_profile = dict(lab['profile_body'], api_key=rotated_key)
    put(admin, f'/api/me/models/{profile_id}', updated_profile)
    retried = post(admin, f"{base}/model-operations/{failed['id']}/retry")
    assert retried['generated'] == 1
    assert lab['state']['requests'][-1]['api_key'] == rotated_key

    room = admin.get(base).json()
    retry_task = next(task for task in room['model_operations'] if task['retry_of'] == failed['id'])
    assert retry_task['state'] == 'completed'
    assert retry_task['result_applied'] is True
    assert retry_task['credential_version_changed'] is True
    assert room['model_request_count'] == 2
    records = admin.get('/api/me/model-operations').json()
    assert any(item['id'] == retry_task['id'] and item['credential_version_changed']
               for item in records['records'])
    assert rotated_key not in json.dumps(records, ensure_ascii=False)


def test_other_manager_trigger_is_recorded_under_configuration_owner(operation_lab):
    lab = operation_lab
    admin = lab['admin']
    owner = TestClient(lab['app'])
    post(owner, '/api/register', {
        'username': 'operation_profile_owner',
        'display_name': '配置所有者',
        'password': 'profile-owner-password',
    })
    room = post(owner, '/api/rooms', {'title': '配置所有者的房间'})
    base = f"/api/rooms/{room['room_id']}"
    p1, p2 = TestClient(lab['app']), TestClient(lab['app'])
    post(p1, '/api/register', {'username': 'owner_room_a', 'display_name': '玩家 A',
                               'password': 'owner-room-a-password', 'code': room['code']})
    post(p2, '/api/register', {'username': 'owner_room_b', 'display_name': '玩家 B',
                               'password': 'owner-room-b-password', 'code': room['code']})
    profile = post(owner, '/api/me/models', lab['profile_body'])
    add_ai_judge(owner, base, '所有者房间 AI 裁判', profile['id'])

    # 全站管理员触发房间调用，但配置记录仍归配置所有者。
    post(admin, base + '/ai-judges/questions/generate')
    owner_records = owner.get('/api/me/model-operations').json()
    assert owner_records['total'] == 1
    assert owner_records['records'][0]['trigger_uid'] == admin.get('/api/me').json()['user']['uid']
    assert admin.get('/api/me/model-operations').json()['total'] == 0


def test_inflight_result_does_not_resurrect_tasks_after_match_cleanup(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    add_ai_judge(admin, base, '清理期间正在出题的裁判', profile_id)
    put(admin, base + '/model-request-limit', {'limit': 1})
    block = {'started': threading.Event(), 'release': threading.Event()}
    lab['state']['block'] = block

    second_admin = TestClient(lab['app'])
    post(second_admin, '/api/login', {'username': 'ops_admin', 'password': 'ops-admin-password'})
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, second_admin, base + '/ai-judges/questions/generate')
        assert block['started'].wait(5), '模拟模型请求没有进入请求阶段'
        with db_open(lab['path']) as db:
            db.execute("UPDATE rooms SET status='finished' WHERE id=?", (lab['room_id'],))
        post(admin, base + '/restart', {'match_number': 1, 'confirm': True})
        block['release'].set()
        result = future.result(timeout=15)

    lab['state']['block'] = None
    assert result['generated'] == 0
    room = admin.get(base).json()
    assert room['match_number'] == 2
    assert room['model_request_count'] == 0
    assert room['model_request_limit'] == 1
    assert room['model_operations'] == []
    assert room['rounds'][0]['questions'] == []
    assert admin.get('/api/me/model-operations').json()['total'] == 0



def test_ai_question_uses_frozen_profile_when_profile_changes_during_provider_call(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    add_ai_judge(admin, base, '配置修改期间出题的裁判', profile_id)
    block = {'started': threading.Event(), 'release': threading.Event()}
    lab['state']['block'] = block
    second_admin = TestClient(lab['app'])
    post(second_admin, '/api/login', {'username': 'ops_admin', 'password': 'ops-admin-password'})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, second_admin, base + '/ai-judges/questions/generate')
        assert block['started'].wait(5), '模拟模型请求没有进入请求阶段'
        updated = dict(lab['profile_body'])
        updated['model'] = 'ops-test-model-updated'
        updated['api_key'] = ''
        put(admin, f'/api/me/models/{profile_id}', updated)
        block['release'].set()
        result = future.result(timeout=15)

    assert block['profile_model'] == lab['profile_body']['model']
    lab['state']['block'] = None
    assert result['generated'] == 1
    assert result['skipped'] == 0
    room = admin.get(base).json()
    assert len(room['rounds'][0]['questions']) == 1
    assert room['rounds'][0]['questions'][0]['text'] == '如果你最近改变过一个重要看法，是什么促使你改变？'
    task = room['model_operations'][0]
    assert task['state'] == 'completed'
    assert task['request_succeeded'] is True
    assert task['result_applied'] is True
    assert room['model_request_count'] == 1

def test_cancel_preparation_discards_inflight_persona_question_result(operation_lab):
    lab = operation_lab
    admin, base = lab['admin'], lab['base']
    judge = add_ai_judge(admin, base, '取消准备期间正在出题的裁判', lab['profile_id'])
    block = {'started': threading.Event(), 'release': threading.Event()}
    lab['state']['block'] = block
    second_admin = TestClient(lab['app'])
    post(second_admin, '/api/login', {'username': 'ops_admin', 'password': 'ops-admin-password'})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, second_admin, base + '/ai-judges/questions/generate')
        try:
            assert block['started'].wait(5), '模拟模型请求没有进入请求阶段'
            reset = post(admin, base + '/personas/reset-preparation', {'confirm': True})
            assert reset['reset'] is True
        finally:
            block['release'].set()
        result = future.result(timeout=15)

    lab['state']['block'] = None
    assert result['generated'] == 0
    room = admin.get(base).json()
    assert room['personas_frozen'] is False
    assert room['rounds'][0]['questions'] == []
    with db_open(lab['path']) as db:
        task = db.execute(
            "SELECT state,result_applied FROM model_operation_tasks WHERE room_id=?",
            (lab['room_id'],)).fetchone()
        assert task['state'] == 'stale'
        assert task['result_applied'] == 0
        assert db.execute(
            'SELECT 1 FROM ai_judge_questions WHERE room_id=? AND ai_judge_id=?',
            (lab['room_id'], judge['id'])).fetchone() is None
        assert db.execute(
            'SELECT 1 FROM match_persona_freezes WHERE room_id=?',
            (lab['room_id'],)).fetchone() is None
        assert db.execute(
            'SELECT 1 FROM match_seat_persona_snapshots WHERE room_id=?',
            (lab['room_id'],)).fetchone() is None


def test_cancel_preparation_invalidates_completed_persona_question_task(operation_lab):
    lab = operation_lab
    admin, base = lab['admin'], lab['base']
    judge = add_ai_judge(admin, base, '取消准备后不可重试的裁判', lab['profile_id'])

    generated = post(admin, base + '/ai-judges/questions/generate')
    assert generated['generated'] == 1
    assert lab['state']['calls'] == 1
    room = admin.get(base).json()
    assert len(room['rounds'][0]['questions']) == 1
    task = next(item for item in room['model_operations'] if item['purpose'] == 'judge_question')
    assert task['state'] == 'completed'
    assert task['result_applied'] is True

    reset = post(admin, base + '/personas/reset-preparation', {'confirm': True})
    assert reset['reset'] is True
    room = admin.get(base).json()
    assert room['personas_frozen'] is False
    assert room['rounds'][0]['questions'] == []
    with db_open(lab['path']) as db:
        invalidated = db.execute(
            'SELECT state,result_applied FROM model_operation_tasks WHERE id=?',
            (task['id'],),
        ).fetchone()
    assert invalidated['state'] == 'stale'
    assert invalidated['result_applied'] == 0
    assert lab['state']['calls'] == 1

    response = admin.post(base + f"/model-operations/{task['id']}/retry",
                          headers={'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf']})
    assert response.status_code == 409
    assert response.json()['detail'] == '只有失败、中断或额度阻止的任务可以重试。'
    assert lab['state']['calls'] == 1


def test_same_model_seats_use_distinct_frozen_persona_prompts(operation_lab):
    lab = operation_lab
    admin, p1, p2, base, profile_id = (
        lab['admin'], lab['p1'], lab['p2'], lab['base'], lab['profile_id'])
    participant = post(admin, base + '/ai', {
        'name': '同模型参赛者', 'model_profile_id': profile_id,
    })
    judge = add_ai_judge(admin, base, '同模型裁判', profile_id)
    put(admin, base + f"/ai/{participant['alias']}/persona", {
        'preset_id': 'analytical', 'custom_system_prompt': '',
        'output_length': 'short', 'history_rounds': 0,
    })
    put(admin, base + f"/ai-judges/{judge['id']}/persona", {
        'preset_id': 'language_detail', 'custom_system_prompt': '',
        'output_length': 'medium', 'history_rounds': 0,
    })

    question = post(admin, base + '/ai-judges/questions/generate')
    assert question['generated'] == 1
    judge_request = lab['state']['requests'][-1]
    assert judge_request['model'] == lab['profile_body']['model']
    assert judge_request['max_tokens'] == 768
    assert '重点检查措辞' in judge_request['system']

    post(admin, base + '/start')
    post(p1, base + '/answers', {'text': '我会先核对信息，再修正判断。'})
    post(p2, base + '/answers', {'text': '我会结合上下文说明理由。'})
    answer = post(admin, base + '/ai/answers/generate')
    assert answer['generated'] == 1
    participant_request = lab['state']['requests'][-1]
    assert participant_request['model'] == judge_request['model']
    assert participant_request['max_tokens'] == 256
    assert '表达结构清晰' in participant_request['system']
    assert participant_request['system'] != judge_request['system']


def test_legacy_in_progress_match_freezes_personas_before_ai_verdict(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    judge = add_ai_judge(admin, base, '旧比赛首次使用 AI 判决', profile_id)
    post(admin, base + f"/ai-judges/{judge['id']}/questions", {
        'text': '请判断匿名参赛者的身份并给出依据。',
    })
    post(admin, base + '/start')
    post(lab['p1'], base + '/answers', {'text': '我会先核对事实，再解释结论。'})
    post(lab['p2'], base + '/answers', {'text': '我会结合上下文说明理由。'})
    assert admin.get(base).json()['rounds'][0]['status'] == 'revealed'

    # Simulate a match that was already active before persona snapshots existed.
    with db_open(lab['path']) as db:
        db.execute('DELETE FROM match_seat_persona_snapshots WHERE room_id=?', (lab['room_id'],))
        db.execute('DELETE FROM match_persona_freezes WHERE room_id=?', (lab['room_id'],))
        db.execute(
            "UPDATE model_profiles SET model='legacy-current-model' WHERE id=?",
            (profile_id,),
        )

    verdict = post(admin, base + '/ai-judges/verdicts/generate')
    assert verdict['generated'] == 1
    assert lab['state']['calls'] == 1
    with db_open(lab['path']) as db:
        freeze = db.execute(
            'SELECT source FROM match_persona_freezes WHERE room_id=?',
            (lab['room_id'],),
        ).fetchone()
        snapshot = db.execute(
            "SELECT model_json FROM match_seat_persona_snapshots "
            "WHERE room_id=? AND seat_type='judge' AND seat_key=?",
            (lab['room_id'], str(judge['id'])),
        ).fetchone()
        task = db.execute(
            "SELECT persona_snapshot_id FROM model_operation_tasks "
            "WHERE room_id=? AND purpose='judge_verdict' ORDER BY id DESC LIMIT 1",
            (lab['room_id'],),
        ).fetchone()
    assert freeze['source'] == 'legacy_verdict_generation'
    assert json.loads(snapshot['model_json'])['model'] == 'legacy-current-model'
    assert task['persona_snapshot_id'] is not None


def test_short_persona_keeps_full_frozen_token_budget_for_final_verdict(operation_lab):
    lab = operation_lab
    admin, p1, p2, base, profile_id = (
        lab['admin'], lab['p1'], lab['p2'], lab['base'], lab['profile_id'])
    judge = add_ai_judge(admin, base, '短档最终判断裁判', profile_id)
    put(admin, base + f"/ai-judges/{judge['id']}/persona", {
        "preset_id": "language_detail",
        "custom_system_prompt": "",
        "output_length": "short",
        "history_rounds": 0,
    })

    # First AI judge question freezes the persona and model parameters.
    question_result = post(admin, base + "/ai-judges/questions/generate")
    assert question_result['generated'] == 1
    assert lab['state']['requests'][-1]['max_tokens'] == 256

    # Later edits to the personal profile must not replace the frozen model.
    changed = dict(lab['profile_body'], model='changed-live-model', api_key='')
    put(admin, f"/api/me/models/{profile_id}", changed)

    post(admin, base + "/start")
    post(p1, base + "/answers", {"text": "我会先核对事实，再调整自己的判断。"})
    post(p2, base + "/answers", {"text": "我会结合上下文分析表达习惯。"})
    assert admin.get(base).json()['rounds'][0]['status'] == 'revealed'

    verdict_result = post(admin, base + "/ai-judges/verdicts/generate")
    assert verdict_result['generated'] == 1
    request = lab['state']['requests'][-1]
    assert request['model'] == lab['profile_body']['model']
    assert request['max_tokens'] == lab['profile_body']['max_tokens']
    assert request['temperature'] == lab['profile_body']['temperature']
    assert '约 80 个中文字符' in request['system']
    assert '不得截断或省略 JSON 字段' in request['system']
    with db_open(lab['path']) as db:
        row = db.execute(
            'SELECT verdict_json,locked_at FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
            (lab['room_id'], judge['id'])).fetchone()
        assert row and row['locked_at']
        verdict = json.loads(row['verdict_json'])
        assert set(verdict['predictions']) == {'A', 'B'}
        task = db.execute(
            "SELECT state,result_applied FROM model_operation_tasks "
            "WHERE room_id=? AND purpose='judge_verdict' ORDER BY id DESC LIMIT 1",
            (lab['room_id'],)).fetchone()
        assert task['state'] == 'completed' and task['result_applied'] == 1


def test_startup_marks_unfinished_tasks_interrupted_without_replaying(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    judge = add_ai_judge(admin, base, '启动恢复裁判', profile_id)
    post(admin, base + '/ai-judges/questions/generate')
    with db_open(lab['path']) as db:
        # Simulate a crash before the response was applied to the match.
        db.execute('DELETE FROM ai_judge_questions WHERE room_id=? AND ai_judge_id=?',
                   (lab['room_id'],judge['id']))
        db.execute("""UPDATE model_operation_tasks SET state='requesting',
            request_finished_at=NULL,finished_at=NULL,result_applied=0
            WHERE room_id=?""", (lab['room_id'],))
    calls_before_restart = lab['state']['calls']

    restarted_app = create_app(lab['path'])
    restarted_admin = TestClient(restarted_app)
    post(restarted_admin, '/api/login', {'username': 'ops_admin', 'password': 'ops-admin-password'})
    room = restarted_admin.get(base).json()
    assert room['model_operations'][0]['state'] == 'interrupted'
    assert lab['state']['calls'] == calls_before_restart


def post_with_idempotency_key(client, path, key, body=None, expected=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf'], 'Idempotency-Key': key}
    response = client.post(path, json=body or {}, headers=headers)
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json() if response.content else {}


def test_connection_test_same_key_concurrent_replay_does_not_duplicate(operation_lab):
    lab = operation_lab
    admin, profile_id = lab['admin'], lab['profile_id']
    path = f'/api/me/models/{profile_id}/test'
    key = 'connection-test-concurrent'
    block = {'started': threading.Event(), 'release': threading.Event()}
    lab['state']['block'] = block
    second_admin = TestClient(lab['app'])
    post(second_admin, '/api/login', {'username': 'ops_admin', 'password': 'ops-admin-password'})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post_with_idempotency_key, admin, path, key)
        try:
            assert block['started'].wait(5), '首个同键请求没有进入模型调用阶段'
            me = second_admin.get('/api/me').json()
            response = second_admin.post(path, headers={
                'X-CSRF-Token': me['user']['csrf'],
                'Idempotency-Key': key,
            })
            assert response.status_code == 409, response.text
            assert lab['state']['calls'] == 1, '同键并发请求不能重复调用服务商'
        finally:
            block['release'].set()
        first = future.result(timeout=15)

    lab['state']['block'] = None
    assert first['ok'] is True
    replay = post_with_idempotency_key(admin, path, key)
    assert replay['replayed'] is True
    assert replay['state'] == 'completed'
    assert lab['state']['calls'] == 1, '并发结束后的终态重放不能再次调用服务商'


def test_connection_test_idempotency_replays_completion_and_rejects_changed_input(operation_lab):
    lab = operation_lab
    admin, profile_id = lab['admin'], lab['profile_id']
    path = f'/api/me/models/{profile_id}/test'

    first = post_with_idempotency_key(admin, path, 'connection-test-once')
    assert first['ok'] is True
    assert lab['state']['calls'] == 1

    replay = post_with_idempotency_key(admin, path, 'connection-test-once')
    assert replay['replayed'] is True
    assert replay['state'] == 'completed'
    assert replay['ok'] is True
    assert lab['state']['calls'] == 1, '终态幂等重放不能再次调用服务商'

    updated = dict(lab['profile_body'])
    updated['model'] = 'ops-test-model-changed'
    updated['api_key'] = ''
    put(admin, f'/api/me/models/{profile_id}', updated)
    conflict = admin.post(path, headers={
        'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf'],
        'Idempotency-Key': 'connection-test-once',
    })
    assert conflict.status_code == 409, conflict.text
    assert lab['state']['calls'] == 1

    new_attempt = post_with_idempotency_key(admin, path, 'connection-test-second-attempt')
    assert new_attempt['ok'] is True
    assert lab['state']['calls'] == 2


def test_batch_idempotency_is_per_seat_and_failed_replay_does_not_retry(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    add_ai_judge(admin, base, '幂等批次裁判 A', profile_id)
    add_ai_judge(admin, base, '幂等批次裁判 B', profile_id)
    lab['state']['fail_next'] = 1

    first = post_with_idempotency_key(admin, base + '/ai-judges/questions/generate', 'judge-batch-once')
    assert first['generated'] == 1
    assert len(first['failed']) == 1
    assert lab['state']['calls'] == 2, '同一批次的相同键必须按席位分别创建任务'

    replay = post_with_idempotency_key(admin, base + '/ai-judges/questions/generate', 'judge-batch-once')
    assert replay['generated'] == 0
    assert replay['failed'] == []
    assert len(replay['results']) == 1
    assert replay['results'][0]['status'] == 'failed'
    assert replay['results'][0]['replayed'] is True
    assert lab['state']['calls'] == 2, '失败任务的相同键重放不能再次请求模型'

    updated = dict(lab['profile_body'])
    updated['temperature'] = 0.5
    updated['api_key'] = ''
    put(admin, f'/api/me/models/{profile_id}', updated)
    changed = admin.post(base + '/ai-judges/questions/generate', headers={
        'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf'],
        'Idempotency-Key': 'judge-batch-once',
    })
    assert changed.status_code == 409, changed.text
    assert lab['state']['calls'] == 2

    retried = post_with_idempotency_key(admin, base + '/ai-judges/questions/generate', 'judge-batch-new-attempt')
    assert retried['generated'] == 1
    assert lab['state']['calls'] == 3

    # The same key in another room is a separate logical operation.
    second_room = post(admin, '/api/rooms', {'title': '另一房间幂等隔离'})
    second_base = f"/api/rooms/{second_room['room_id']}"
    for index in range(2):
        player = TestClient(lab['app'])
        post(player, '/api/register', {
            'username': f'idempotency_room_two_{index}',
            'display_name': f'第二房间参赛者 {index}',
            'password': 'idempotency-player-password',
            'code': second_room['code'],
        })
    add_ai_judge(admin, second_base, '另一房间裁判', profile_id)
    isolated = post_with_idempotency_key(
        admin, second_base + '/ai-judges/questions/generate', 'judge-batch-once')
    assert isolated['generated'] == 1
    assert lab['state']['calls'] == 4


def test_batch_question_idempotency_conflict_rolls_back_all_queued_seats(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    judge_a = add_ai_judge(admin, base, '原先未绑定模型的裁判 A', None)
    judge_b = add_ai_judge(admin, base, '先失败并留下幂等键的裁判 B', profile_id)
    key = 'judge-batch-atomic-conflict'
    lab['state']['fail_next'] = 1

    first = post_with_idempotency_key(admin, base + '/ai-judges/questions/generate', key)
    assert first['generated'] == 0
    assert first['skipped'] == 1
    assert len(first['failed']) == 1
    assert first['failed'][0]['judge_id'] == judge_b['id']
    assert lab['state']['calls'] == 1

    with db_open(lab['path']) as db:
        rows = db.execute("""SELECT target_key,state FROM model_operation_tasks
            WHERE room_id=? AND purpose='judge_question' ORDER BY id""",
            (lab['room_id'],)).fetchall()
    assert [(row['target_key'], row['state']) for row in rows] == [
        (str(judge_b['id']), 'failed')
    ]

    put(admin, f"{base}/ai-judges/{judge_a['id']}/model", {
        'model_profile_id': profile_id,
    })
    updated = dict(lab['profile_body'])
    updated['temperature'] = 0.5
    updated['api_key'] = ''
    put(admin, f"/api/me/models/{profile_id}", updated)

    conflict = admin.post(base + '/ai-judges/questions/generate', headers={
        'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf'],
        'Idempotency-Key': key,
    })
    assert conflict.status_code == 409, conflict.text
    assert lab['state']['calls'] == 1

    # The conflict on B must roll back the task tentatively created for A.
    with db_open(lab['path']) as db:
        rows = db.execute("""SELECT target_key,state FROM model_operation_tasks
            WHERE room_id=? AND purpose='judge_question' ORDER BY id""",
            (lab['room_id'],)).fetchall()
        idempotency_count = db.execute("""SELECT COUNT(*) FROM model_operation_idempotency
            WHERE idempotency_key=?""", (key,)).fetchone()[0]
    assert [(row['target_key'], row['state']) for row in rows] == [
        (str(judge_b['id']), 'failed')
    ]
    assert idempotency_count == 1

    # A fresh key can now queue and execute both seats; no orphan queued task blocks A.
    retried = post_with_idempotency_key(
        admin, base + '/ai-judges/questions/generate', 'judge-batch-atomic-retry')
    assert retried['generated'] == 2
    assert retried['failed'] == []
    assert lab['state']['calls'] == 3
    with db_open(lab['path']) as db:
        rows = db.execute("""SELECT target_key,state FROM model_operation_tasks
            WHERE room_id=? AND purpose='judge_question' ORDER BY id""",
            (lab['room_id'],)).fetchall()
    by_target = {}
    for row in rows:
        by_target.setdefault(row['target_key'], []).append(row['state'])
    assert by_target == {
        str(judge_a['id']): ['completed'],
        str(judge_b['id']): ['failed', 'completed'],
    }


def test_idempotency_key_is_scoped_to_triggering_account(operation_lab):
    lab = operation_lab
    admin, base, profile_id = lab['admin'], lab['base'], lab['profile_id']
    judge = add_ai_judge(admin, base, '跨账号幂等裁判', profile_id)
    key = 'same-room-same-target-different-account'

    first = post_with_idempotency_key(admin, base + '/ai-judges/questions/generate', key)
    assert first['generated'] == 1
    assert lab['state']['calls'] == 1

    # Remove only the generated question so the second authorized manager has
    # the same logical target and input; the completed task and its key remain.
    with db_open(lab['path']) as db:
        db.execute('DELETE FROM ai_judge_questions WHERE room_id=? AND round_number=1 AND ai_judge_id=?',
                   (lab['room_id'], judge['id']))

    other = TestClient(lab['app'])
    post(other, '/api/register', {
        'username': 'idempotency_other_admin',
        'display_name': '幂等隔离管理员',
        'password': 'idempotency-other-admin-password',
    })
    with db_open(lab['path']) as db:
        user = db.execute('SELECT id FROM users WHERE username=?',
                          ('idempotency_other_admin',)).fetchone()
        db.execute('UPDATE users SET is_admin=1 WHERE id=?', (user['id'],))

    second = post_with_idempotency_key(other, base + '/ai-judges/questions/generate', key)
    assert second['generated'] == 1, '另一授权账号使用同一键应创建自己的逻辑操作'
    assert lab['state']['calls'] == 2, '不同触发账号不能重放另一账号的终态任务'

    with db_open(lab['path']) as db:
        rows = db.execute("""SELECT trigger_user_id,idempotency_key,state
            FROM model_operation_tasks
            WHERE room_id=? AND match_number=1 AND round_number=1
              AND purpose='judge_question' AND target_type='ai_judge' AND target_key=?
            ORDER BY id""", (lab['room_id'], str(judge['id']))).fetchall()
    assert len(rows) == 2
    assert rows[0]['trigger_user_id'] != rows[1]['trigger_user_id']
    assert [row['idempotency_key'] for row in rows] == [key, key]


def test_room_mutations_reject_stale_round_preconditions(operation_lab):
    lab = operation_lab
    admin, base = lab['admin'], lab['base']
    room = admin.get(base).json()
    headers = {
        'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf'],
        'X-Match-Number': str(room['match_number']),
        'X-Round-Number': str(room['current_round'] + 1),
    }
    start = admin.post(base + '/start', headers=headers)
    assert start.status_code == 409
    assert '轮次已变化' in start.json()['detail']
    next_round = admin.post(base + '/next', headers=headers)
    assert next_round.status_code == 409
    assert '轮次已变化' in next_round.json()['detail']
    after = admin.get(base).json()
    assert after['current_round'] == room['current_round']
    assert after['rounds'][0]['status'] == 'draft'
