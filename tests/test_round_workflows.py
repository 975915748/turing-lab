import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

import app as app_module
from app import create_app, db_open, seed_admin


@pytest.fixture
def workflow_app(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))
    path = tmp_path / 'round-workflows.sqlite3'
    seed_admin(path, 'workflow_admin', '工作流主持人', 'workflow-admin-password')
    return create_app(path), path


def mutate(client, route, data=None, *, method='POST', expected=200, extra_headers=None):
    me = client.get('/api/me')
    token = me.json()['user']['csrf'] if me.status_code == 200 else None
    headers = {'X-CSRF-Token': token} if token else {}
    headers.update(extra_headers or {})
    response = client.request(method, route, json=data or {}, headers=headers)
    assert response.status_code == expected, f'{method} {route}: {response.status_code} {response.text}'
    return response.json() if response.content else {}


def create_room_and_players(app, admin, count=2):
    room = mutate(admin, '/api/rooms', {'title': '服务端编排测试', 'judge': '独立裁判'})
    players = []
    for index in range(count):
        player = TestClient(app)
        mutate(player, '/api/register', {
            'username': f'workflow_player_{index}',
            'display_name': f'测试玩家{index}',
            'password': f'workflow-player-{index}-password',
            'code': room['code'],
        })
        players.append(player)
    return room, players


def create_profile(admin):
    return mutate(admin, '/api/me/models', {
        'name': '工作流测试模型',
        'base_url': 'https://api.example.com/v1',
        'model': 'workflow-test-model',
        'api_key': 'sk-workflow-test-key',
        'temperature': 0.3,
        'max_tokens': 512,
        'timeout_seconds': 30,
    })['id']


def wait_for_workflow(path, workflow_id, expected='completed', timeout=8):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        with db_open(path) as db:
            row = db.execute('SELECT * FROM round_workflows WHERE id=?', (workflow_id,)).fetchone()
            last = dict(row) if row else None
        if last and last['state'] == expected:
            return last
        if last and last['state'] in ('failed', 'blocked', 'stale', 'cancelled') and last['state'] != expected:
            break
        time.sleep(0.05)
    raise AssertionError(f'workflow did not reach {expected}: {last}')


def test_one_request_workflow_survives_logout_and_idempotent_replay(workflow_app, monkeypatch):
    app, path = workflow_app
    started = threading.Event()
    release = threading.Event()
    calls = []

    async def fake_chat(profile, messages, api_key):
        calls.append(messages[0]['content'])
        if len(calls) == 1:
            started.set()
            await asyncio.to_thread(release.wait, 8)
        if '你是多人图灵测试的独立裁判' in messages[0]['content']:
            return {'text': '请描述一次你改变看法的经历。', 'usage': {'prompt_tokens': 5, 'completion_tokens': 8}}
        return {'text': '我会先了解具体情况，再决定如何处理。', 'usage': {'prompt_tokens': 7, 'completion_tokens': 9}}

    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
    with TestClient(app) as admin:
        mutate(admin, '/api/login', {'username': 'workflow_admin', 'password': 'workflow-admin-password'})
        room, players = create_room_and_players(app, admin, count=2)
        profile_id = create_profile(admin)
        base = f"/api/rooms/{room['room_id']}"
        judge = mutate(admin, base + '/ai-judges', {'name': '自动裁判', 'model_profile_id': profile_id})
        ai = mutate(admin, base + '/ai', {'name': '自动参赛者', 'model_profile_id': profile_id})
        body = {'match_number': 1, 'round_number': 1, 'include_ai_questions': True, 'mode': 'start'}
        first = mutate(admin, base + '/round-workflows', body, expected=202, extra_headers={'Idempotency-Key': 'workflow-first-key-0001'})
        workflow_id = first['workflow']['id']
        assert started.wait(5), 'server worker did not start the model request'

        duplicate = mutate(admin, base + '/round-workflows', body, expected=202,
                           extra_headers={'Idempotency-Key': 'workflow-different-key-0002'})
        assert duplicate['workflow']['id'] == workflow_id
        assert duplicate['created'] is False

        # The original browser session is gone; the durable task must still finish.
        mutate(admin, '/api/logout', {})
        release.set()
        completed = wait_for_workflow(path, workflow_id)
        assert completed['state'] == 'completed'

        with db_open(path) as db:
            room_row = db.execute('SELECT * FROM rooms WHERE id=?', (room['room_id'],)).fetchone()
            round_row = db.execute('SELECT * FROM rounds WHERE room_id=? AND number=1',
                                   (room['room_id'],)).fetchone()
            question = db.execute('SELECT text FROM ai_judge_questions WHERE room_id=? AND round_number=1 AND ai_judge_id=?',
                                  (room['room_id'], judge['id'])).fetchone()
            answer = db.execute('SELECT text FROM answers WHERE round_id=? AND alias=?',
                                (round_row['id'], ai['alias'])).fetchone()
            tasks = db.execute('SELECT * FROM model_operation_tasks WHERE workflow_id=? ORDER BY id',
                               (workflow_id,)).fetchall()
        assert room_row['current_round'] == 1
        assert round_row['status'] == 'collecting'
        assert round_row['started_at']
        assert question['text'] == '请描述一次你改变看法的经历。'
        assert answer['text'] == '我会先了解具体情况，再决定如何处理。'
        assert len(tasks) == 2
        assert all(task['state'] == 'completed' and task['result_applied'] == 1 for task in tasks)
        assert all(task['workflow_id'] == workflow_id for task in tasks)
        assert len(calls) == 2
        for player in players:
            player.close()


def test_workflow_auto_retries_only_transient_failures_and_caps_at_three(workflow_app, monkeypatch):
    app, path = workflow_app
    monkeypatch.setattr(app_module, 'WORKFLOW_RETRY_DELAYS', (0, 0))
    calls = []

    async def fake_chat(profile, messages, api_key):
        calls.append(1)
        if len(calls) < 3:
            raise app_module.ModelAPIError('HTTP 503: temporary upstream failure')
        return {'text': '请说说你最近学到的一件事。', 'usage': {'prompt_tokens': 3, 'completion_tokens': 5}}

    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
    with TestClient(app) as admin:
        mutate(admin, '/api/login', {'username': 'workflow_admin', 'password': 'workflow-admin-password'})
        room, players = create_room_and_players(app, admin, count=2)
        profile_id = create_profile(admin)
        base = f"/api/rooms/{room['room_id']}"
        mutate(admin, base + '/ai-judges', {'name': '重试裁判', 'model_profile_id': profile_id})
        result = mutate(admin, base + '/round-workflows', {
            'match_number': 1, 'round_number': 1, 'include_ai_questions': True, 'mode': 'start',
        }, expected=202, extra_headers={'Idempotency-Key': 'workflow-retry-key-0001'})
        workflow_id = result['workflow']['id']
        completed = wait_for_workflow(path, workflow_id)
        assert completed['state'] == 'completed'
        with db_open(path) as db:
            attempts = db.execute('''SELECT id,state,retry_of,request_started_at
                FROM model_operation_tasks WHERE workflow_id=? AND purpose='judge_question'
                ORDER BY id''', (workflow_id,)).fetchall()
        assert len(attempts) == 3
        assert [row['state'] for row in attempts] == ['failed', 'failed', 'completed']
        assert attempts[0]['retry_of'] is None
        assert attempts[1]['retry_of'] == attempts[0]['id']
        assert attempts[2]['retry_of'] == attempts[1]['id']
        assert all(row['request_started_at'] for row in attempts)
        assert len(calls) == 3
        for player in players:
            player.close()


def test_unknown_provider_outcome_requires_explicit_confirmation_after_restart(workflow_app, monkeypatch):
    app, path = workflow_app
    started = threading.Event()
    release = threading.Event()
    calls = []

    async def fake_chat(profile, messages, api_key):
        calls.append(1)
        if len(calls) == 1:
            started.set()
            await asyncio.to_thread(release.wait, 8)
        return {'text': '你如何判断一段信息是否可信？', 'usage': {'prompt_tokens': 4, 'completion_tokens': 6}}

    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
    with TestClient(app) as admin:
        mutate(admin, '/api/login', {'username': 'workflow_admin', 'password': 'workflow-admin-password'})
        room, players = create_room_and_players(app, admin, count=2)
        profile_id = create_profile(admin)
        base = f"/api/rooms/{room['room_id']}"
        mutate(admin, base + '/ai-judges', {'name': '重启恢复裁判', 'model_profile_id': profile_id})
        result = mutate(admin, base + '/round-workflows', {
            'match_number': 1, 'round_number': 1, 'include_ai_questions': True, 'mode': 'start',
        }, expected=202, extra_headers={'Idempotency-Key': 'workflow-restart-key-0001'})
        workflow_id = result['workflow']['id']
        assert started.wait(5), 'model request did not reach the provider mock'
    # Simulate process startup recovery after a request was sent but before its result was stored.
    app_module.init_db(path)
    with db_open(path) as db:
        workflow = db.execute('SELECT * FROM round_workflows WHERE id=?', (workflow_id,)).fetchone()
        interrupted = db.execute('''SELECT * FROM model_operation_tasks
            WHERE workflow_id=? ORDER BY id DESC LIMIT 1''', (workflow_id,)).fetchone()
    assert workflow['state'] == 'waiting_input'
    assert '结果未知' in workflow['waiting_reason']
    assert interrupted['state'] == 'interrupted'
    assert calls == [1]
    release.set()

    resumed_app = create_app(path)
    with TestClient(resumed_app) as admin:
        mutate(admin, '/api/login', {'username': 'workflow_admin', 'password': 'workflow-admin-password'})
        base = f"/api/rooms/{room['room_id']}"
        refused = admin.post(base + f'/round-workflows/{workflow_id}/resume',
                             json={'confirm_unknown_attempts': False},
                             headers={'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf']})
        assert refused.status_code == 409
        mutate(admin, base + f'/round-workflows/{workflow_id}/resume',
               {'confirm_unknown_attempts': True})
        completed = wait_for_workflow(path, workflow_id)
        assert completed['state'] == 'completed'
        with db_open(path) as db:
            attempts = db.execute('''SELECT id,state,retry_of FROM model_operation_tasks
                WHERE workflow_id=? AND purpose='judge_question' ORDER BY id''',
                (workflow_id,)).fetchall()
        assert len(attempts) == 2
        assert attempts[0]['state'] == 'interrupted'
        assert attempts[1]['state'] == 'completed'
        assert attempts[1]['retry_of'] == attempts[0]['id']
        assert len(calls) == 2
    for player in players:
        player.close()


def test_round_timer_deadline_and_host_timeout_finish(workflow_app, monkeypatch):
    app, path = workflow_app
    with TestClient(app) as admin:
        mutate(admin, '/api/login', {'username': 'workflow_admin', 'password': 'workflow-admin-password'})
        room, players = create_room_and_players(app, admin, count=2)
        base = f"/api/rooms/{room['room_id']}"
        ai = mutate(admin, base + '/ai', {'name': '手动 AI 席位'})
        assert admin.get(base).json()['round_duration_seconds'] is None
        assert admin.put(base + '/round-timer',
                         json={'seconds': 29},
                         headers={'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf']}).status_code == 422
        mutate(admin, base + '/round-timer', {'seconds': 30}, method='PUT')
        mutate(admin, base + '/question', {'text': '请描述一次你解决问题的经历。'})
        opened = admin.get(base).json()['rounds'][0]
        assert opened['status'] == 'collecting'
        assert opened['started_at']
        assert opened['duration_seconds'] == 30
        assert opened['deadline_at']
        assert admin.post(base + '/rounds/1/finish-timeout',
                          json={'match_number': 1, 'confirm': True},
                          headers={'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf']}).status_code == 409

        mutate(players[0], base + '/answers', {'text': '我先拆解问题，再逐项验证。'})
        with db_open(path) as db:
            db.execute("UPDATE rounds SET deadline_at='2000-01-01T00:00:00+00:00' WHERE room_id=? AND number=1",
                       (room['room_id'],))
        assert players[1].post(base + '/answers', json={'text': '迟到回答'} ,
                               headers={'X-CSRF-Token': players[1].get('/api/me').json()['user']['csrf']}).status_code == 409
        assert admin.post(base + f'/ai/{ai["alias"]}/answer', json={'text': '迟到 AI 回答'},
                          headers={'X-CSRF-Token': admin.get('/api/me').json()['user']['csrf']}).status_code == 409
        still_collecting = admin.get(base).json()['rounds'][0]
        assert still_collecting['status'] == 'collecting'
        assert still_collecting['answers'] == {}

        result = mutate(admin, base + '/rounds/1/finish-timeout',
                        {'match_number': 1, 'confirm': True})
        assert result['revealed'] is True
        public = admin.get(base).json()
        round_one = public['rounds'][0]
        assert round_one['status'] == 'revealed'
        assert round_one['answers']['A']['text'] == '我先拆解问题，再逐项验证。'
        assert set(round_one['timed_out']) == {'B', ai['alias']}
        assert ai['alias'] not in round_one['answers']
        log = admin.get(base + '/rounds/1/log').text
        assert '超时未提交' in log
        packet = mutate(admin, base + '/packets', {})
        packet_text = admin.get(base + f"/packets/{packet['packet_id']}").json()['content']
        assert '【参与者 B】' in packet_text and '（超时未提交）' in packet_text
        duplicate = mutate(admin, base + '/rounds/1/finish-timeout',
                           {'match_number': 1, 'confirm': True})
        assert duplicate['already_finished'] is True
        with db_open(path) as db:
            events = db.execute("SELECT COUNT(*) FROM events WHERE room_id=? AND kind='round_timeout'",
                                (room['room_id'],)).fetchone()[0]
            settlements = db.execute('SELECT COUNT(*) FROM match_settlements WHERE room_id=?',
                                     (room['room_id'],)).fetchone()[0]
        assert events == 1
        assert settlements == 0

        # Updating the room default after a round starts affects future rounds only.
        mutate(admin, base + '/round-timer', {'seconds': 60}, method='PUT')
        public = admin.get(base).json()
        assert public['round_duration_seconds'] == 60
        assert public['rounds'][0]['duration_seconds'] == 30
        profile_id = create_profile(admin)
        mutate(admin, base + '/ai-judges', {'name': '下一轮自动裁判', 'model_profile_id': profile_id})
        async def fake_chat(profile, messages, api_key):
            return {'text': '第二轮裁判问题', 'usage': {'prompt_tokens': 3, 'completion_tokens': 4}}
        monkeypatch.setattr(app_module, 'chat_completion', fake_chat)
        next_round = mutate(admin, base + '/next', {})
        assert next_round['workflow_id']
        completed = wait_for_workflow(path, next_round['workflow_id'])
        assert completed['state'] == 'completed'
        public = admin.get(base).json()
        assert public['current_round'] == 2
        assert public['rounds'][1]['status'] == 'draft'
        assert public['round_workflow']['mode'] == 'prepare_only'
        assert public['round_workflow']['state'] == 'completed'
        with db_open(path) as db:
            question = db.execute('''SELECT text FROM ai_judge_questions
                WHERE room_id=? AND round_number=2''',(room['room_id'],)).fetchone()
        assert question['text'] == '第二轮裁判问题'

        # The compatibility /start path must initialize the same timer snapshot.
        mutate(admin, base + '/start', {})
        second_round = admin.get(base).json()['rounds'][1]
        assert second_round['status'] == 'collecting'
        assert second_round['duration_seconds'] == 60
        assert second_round['started_at'] and second_round['deadline_at']
        mutate(players[0], base + '/answers', {'text': '第二轮回答 A'})
        mutate(players[1], base + '/answers', {'text': '第二轮回答 B'})
        mutate(admin, base + f'/ai/{ai["alias"]}/answer', {'text': '第二轮 AI 回答'})
        assert admin.get(base).json()['rounds'][1]['status'] == 'revealed'
        normal_finish = mutate(admin, base + '/rounds/2/finish-timeout',
                               {'match_number': 1, 'confirm': True})
        assert normal_finish['already_finished'] is True
        assert normal_finish['timeout'] is False
        with db_open(path) as db:
            timeout_events = db.execute("SELECT COUNT(*) FROM events WHERE room_id=? AND kind='round_timeout'",
                                         (room['room_id'],)).fetchone()[0]
        assert timeout_events == 1
    for player in players:
        player.close()


def test_worker_lifecycle_start_stop_is_idempotent():
    from match_orchestration import PersistentWorkflowWorker

    async def exercise():
        async def process(workflow):
            raise AssertionError('no workflow should be claimed')

        async def on_error(workflow, error):
            raise error

        worker = PersistentWorkflowWorker(lambda: None, process, on_error, poll_interval=0.05)
        await worker.start()
        await worker.start()
        assert worker.running is True
        await worker.stop()
        assert worker.running is True
        await worker.stop()
        assert worker.running is False
        await worker.stop()
        await worker.start()
        assert worker.running is True
        await worker.stop()
        assert worker.running is False

    asyncio.run(exercise())
