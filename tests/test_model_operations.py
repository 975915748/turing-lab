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
