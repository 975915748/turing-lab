"""Room-owner capabilities, reusable matches and admin account lifecycle."""
import json

import pytest
from fastapi.testclient import TestClient

from app import create_app, db_open, seed_admin


def request(client, method, route, body=None, status=200, match=None):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']} if me.status_code == 200 else {}
    if match is not None:
        headers['X-Match-Number'] = str(match)
    result = client.request(method, route, json=body, headers=headers)
    assert result.status_code == status, (route, result.status_code, result.text)
    return result.json()


def post(client, route, body=None, status=200, match=None):
    return request(client, 'POST', route, body or {}, status, match)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'hosts.sqlite3'
    seed_admin(path, 'admin', 'Admin', 'admin-password')
    app = create_app(path)
    admin = TestClient(app)
    post(admin, '/api/login', {'username': 'admin', 'password': 'admin-password'})
    users = [TestClient(app) for _ in range(3)]
    for i, user in enumerate(users):
        post(user, '/api/register', {'username': f'玩家{i}', 'password': 'user-password'})
    return path, app, admin, *users


def test_nonadmin_hosts_have_full_controls_without_global_privilege(env):
    path, app, admin, host, player, outsider = env
    created = post(host, '/api/rooms', {'title': '普通房主'})
    base = f'/api/rooms/{created["room_id"]}'
    info = host.get(base).json()
    assert info['can_manage'] and info['is_owner'] and not info['is_admin']
    assert info['invite_code'] == created['code']
    assert host.get('/api/me').json()['rooms'][0]['role'] == 'owner'
    assert admin.get(base).json()['can_manage']
    assert outsider.get(base).status_code == 403
    post(outsider, base + '/ai', {'name': '越权'}, 403)
    post(player, '/api/join', {'code': created['code']})
    assert not player.get(base).json()['can_manage']
    post(player, base + '/ai-judges', {'name': '越权'}, 403)
    ai = post(host, base + '/ai', {'name': 'AI'})['alias']
    judge = post(host, base + '/judges', {'username': '裁判', 'display_name': '裁判一', 'password': 'judge-password'})
    post(host, base + '/judges/assign', {'uid': host.get('/api/me').json()['user']['uid'], 'label': '冒充裁判'}, 409)
    bot = post(host, base + '/ai-judges', {'name': 'AI裁判'})['id']
    human = TestClient(app)
    post(human, '/api/login', {'uid': judge['uid'], 'password': 'judge-password'})
    post(human, base + '/judge-questions', {'text': '真人裁判问题'})
    post(host, base + f'/ai-judges/{bot}/questions', {'text': 'AI裁判问题'})
    post(host, base + '/start')
    post(player, base + '/chat', {'match_number':1,'text': '追问保留'})
    post(player, base + '/answers', {'text': '秘密真人回答'})
    assert human.get(base).json()['rounds'][0]['answers'] == {}
    post(host, base + f'/ai/{ai}/answer', {'text': 'AI回答'})
    request(host, 'PUT', base + f'/ai/{ai}/answer', {'text': 'AI修订'})
    packet = post(host, base + '/packets')['packet_id']
    assert 'AI修订' in host.get(base + f'/packets/{packet}').json()['content']
    post(host, base + f'/packets/{packet}/sent')
    blind = human.get(base).json()
    assert blind['is_judge'] and not blind['can_manage']
    for key in ('humans', 'invite_code', 'owner_id', 'owner_uid', 'ai_edits', 'judges'):
        assert key not in blind
    assert 'TL00000002' not in json.dumps(blind, ensure_ascii=False)
    verdict = {'predictions': {'A': 'Human', ai: 'AI'}, 'evidence': '依据', 'confidence': 50}
    post(human, base + '/judge-verdict', verdict)
    post(host, base + f'/ai-judges/{bot}/verdict', verdict)
    post(admin, base + '/reveal')
    assert host.get(base + '/log').status_code == 200


def test_old_rooms_migrate_to_existing_admin_without_changing_matches(env):
    path, app, admin, host, _, _ = env
    created = post(admin, '/api/rooms', {'title': '原有房间'})
    with db_open(path) as db:
        # Simulate v1.4 schema by removing only additive room columns.
        for column in ('owner_id', 'match_number', 'records_deleted'):
            if column in {r['name'] for r in db.execute('PRAGMA table_info(rooms)')}:
                db.execute(f'ALTER TABLE rooms DROP COLUMN {column}')
        before = tuple(db.execute('SELECT title,code,status,current_round,created_at FROM rooms').fetchone())
    upgraded = create_app(path)
    with db_open(path) as db:
        row = db.execute('SELECT * FROM rooms').fetchone()
        assert row['owner_id'] == 1 and row['match_number'] == 1
        assert tuple(row[k] for k in ('title','code','status','current_round','created_at')) == before
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert list(path.parent.glob('hosts.pre-v1_5-*.sqlite3'))


def finish_match(host, player, base, code, ai='B', match_number=1):
    post(player, '/api/join', {'code': code})
    if not host.get(base).json()['ais']:
        post(host, base + '/ai', {'name': '保留AI'})
    post(host, base + '/question', {'text': '上一场的问题'})
    post(player, base + '/chat', {'match_number':match_number,'text': '上一场追问'})
    post(player, base + '/answers', {'text': '上一场人类回答'})
    post(host, base + f'/ai/{ai}/answer', {'text': '上一场AI回答'})
    request(host, 'PUT', base + f'/ai/{ai}/answer', {'text': '上一场修订'})
    post(host, base + '/packets')
    post(host, base + '/verdict', {'predictions': {'A': 'Human', ai: 'AI'}, 'evidence': '旧依据', 'confidence': 60})
    post(host, base + '/reveal')


def test_any_authenticated_user_can_clear_only_finished_records_and_room_survives(env):
    path, app, admin, host, player, outsider = env
    created = post(host, '/api/rooms', {'title': '清空而不删房间'})
    base = f'/api/rooms/{created["room_id"]}'
    body = {'match_number': 1, 'confirm': True}
    request(outsider, 'DELETE', base + '/records', body, 409)
    finish_match(host, player, base, created['code'])
    assert outsider.get(base).status_code == 403  # no extra read access
    completed = outsider.get('/api/me').json()['finished_rooms']
    assert any(r['id'] == created['room_id'] for r in completed)
    assert 'code' not in json.dumps(completed) and 'uid' not in json.dumps(completed)
    request(TestClient(app), 'DELETE', base + '/records', body, 401)
    assert outsider.request('DELETE', base + '/records', json=body).status_code == 403
    request(outsider, 'DELETE', base + '/records', {'match_number': 1, 'confirm': False}, 422)
    with host.websocket_connect(f'/ws/rooms/{created["room_id"]}') as ws:
        assert ws.receive_json()['type'] == 'connected'
        request(outsider, 'DELETE', base + '/records', body)
        assert ws.receive_json()['reason'] == 'records_deleted'
    info = host.get(base).json()
    assert info['records_deleted'] and info['rounds'] == [] and info['invite_code'] == created['code']
    assert info['status'] == 'finished' and len(info['ais']) == len(info['humans']) == 1
    assert host.get(base + '/log').status_code == 409
    assert host.get(base + '/packets/1').status_code == 404
    with db_open(path) as db:
        for table in ('rounds','answers','suggestions','chat_messages','events','judge_questions','judge_verdicts',
                      'ai_judge_questions','ai_judge_verdicts','ai_answer_revisions','packets'):
            assert db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0, table
        assert db.execute('SELECT verdict_json FROM rooms').fetchone()[0] is None
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_admin_account_reset_delete_and_last_admin_guard(env):
    path, app, admin, host, player, outsider = env
    host_uid = host.get('/api/me').json()['user']['uid']
    assert host.get('/api/admin/users').status_code == 403
    listing = admin.get('/api/admin/users').json()['users']
    assert {u['uid'] for u in listing} == {f'TL{i:08d}' for i in range(1, 5)}
    assert not any('pw_hash' in u or 'csrf' in u for u in listing)
    body = {'password': 'new-password', 'confirm': True}
    post(outsider, f'/api/admin/users/{host_uid}/password', body, 403)
    post(admin, f'/api/admin/users/{host_uid}/password', {**body, 'password': 'short'}, 422)
    assert host.get('/api/me').status_code == 200
    for _ in range(8):
        post(TestClient(app), '/api/login', {'uid': host_uid, 'password': 'wrong-password'}, 401)
    post(admin, f'/api/admin/users/{host_uid}/password', body)
    assert host.get('/api/me').status_code == 401
    post(host, '/api/login', {'uid': host_uid, 'password': 'user-password'}, 401)
    post(host, '/api/login', {'uid': host_uid, 'password': 'new-password'})
    assert admin.get('/api/me').status_code == 200
    created = post(host, '/api/rooms', {'title': '删除账号保留比赛'})
    base = f'/api/rooms/{created["room_id"]}'
    finish_match(host, player, base, created['code'])
    with host.websocket_connect(f'/ws/rooms/{created["room_id"]}') as ws:
        ws.receive_json()
        request(admin, 'DELETE', f'/api/admin/users/{host_uid}', {'confirm': True})
        assert ws.receive()['type'] == 'websocket.close'
    assert host.get('/api/me').status_code == 401
    post(host, '/api/login', {'uid': host_uid, 'password': 'new-password'}, 401)
    assert host_uid not in {u['uid'] for u in admin.get('/api/admin/users').json()['users']}
    assert '上一场人类回答' in admin.get(base + '/log').text
    assert admin.get(base).json()['can_manage']
    post(admin, f'/api/admin/users/{host_uid}/password', body, 404)
    request(admin, 'DELETE', '/api/admin/users/TL00000001', {'confirm': True}, 409)
    assert admin.get('/api/me').status_code == 200
    assert post(TestClient(app), '/api/register', {'username': '玩家0', 'password': 'user-password'})['uid'] == 'TL00000005'
    with db_open(path) as db:
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_deleted_participants_and_pending_judges_do_not_block_active_match(env):
    path, app, admin, host, player, judge = env
    created = post(host, '/api/rooms', {'title': '比赛中撤销账号'})
    base = f'/api/rooms/{created["room_id"]}'
    post(player, '/api/join', {'code': created['code']})
    post(host, base + '/ai', {'name': 'AI'})
    judge_uid = judge.get('/api/me').json()['user']['uid']
    post(host, base + '/judges/assign', {'uid': judge_uid, 'label': '原裁判'})
    post(judge, base + '/judge-questions', {'text': '问题原文应保留'})
    post(host, base + '/start')
    post(player, base + '/chat', {'match_number':1,'text': '保留历史追问'})
    post(host, base + '/ai/B/answer', {'text': 'AI已答'})
    request(admin, 'DELETE', '/api/admin/users/TL00000003', {'confirm': True})
    info = host.get(base).json()
    assert info['rounds'][0]['status'] == 'revealed'
    assert not info['humans']
    request(admin, 'DELETE', f'/api/admin/users/{judge_uid}', {'confirm': True})
    info = host.get(base).json()
    assert not info['judges'] and info['rounds'][0]['questions'][0]['text'] == '问题原文应保留'
    post(host, base + '/next', status=409)
    post(host, base + '/verdict', {'predictions': {'B': 'AI'}, 'evidence': '退赛后判断', 'confidence': 50})
    post(host, base + '/reveal')
    assert '保留历史追问' in host.get(base + '/log').text
    post(host, base + '/restart', {'match_number': 1, 'confirm': True})
    info = host.get(base).json()
    assert not info['humans'] and not info['judges']
    post(host, base + '/question', {'text': '不足两人不可开始'}, 409)


def test_restart_keeps_judges_and_clears_their_questions_and_verdicts(env):
    _, _, admin, host, player, judge = env
    created = post(host, '/api/rooms', {'title': '多裁判重复使用'})
    base = f'/api/rooms/{created["room_id"]}'
    post(player, '/api/join', {'code': created['code']})
    post(host, base + '/ai', {'name': 'AI'})
    post(host, base + '/judges/assign', {'uid': judge.get('/api/me').json()['user']['uid'], 'label': '裁判'})
    bot = post(host, base + '/ai-judges', {'name': 'AI裁判'})['id']
    post(judge, base + '/judge-questions', {'text': '上一场真人问题'})
    post(host, base + f'/ai-judges/{bot}/questions', {'text': '上一场AI问题'})
    post(host, base + '/start')
    post(player, base + '/answers', {'text': '真人答'})
    post(host, base + '/ai/B/answer', {'text': 'AI答'})
    verdict = {'predictions': {'A': 'Human', 'B': 'AI'}, 'evidence': '旧结论', 'confidence': 50}
    post(judge, base + '/judge-verdict', verdict)
    post(host, base + f'/ai-judges/{bot}/verdict', verdict)
    post(host, base + '/reveal')
    post(admin, base + '/restart', {'match_number': 1, 'confirm': True})
    assert len(host.get(base).json()['judges']) == 2
    info = judge.get(base).json()
    assert not info['my_verdict'] and not info['rounds'][0]['questions']
    post(judge, base + '/judge-questions', {'text': '新的问题'}, match=2)


def test_host_can_participate_remove_members_advance_and_reuse_freed_alias(env):
    path, app, admin, host, player, outsider = env
    created = post(host, '/api/rooms', {'title': '房主参赛'})
    base = f'/api/rooms/{created["room_id"]}'
    for client in (host, player, outsider):
        post(client, '/api/join', {'code': created['code']})
    post(host, base + '/ai', {'name': 'AI'})
    post(host, base + '/members/4/remove')
    assert outsider.get(base).status_code == 403
    for number in (1, 2):
        post(host, base + '/question', {'text': f'第{number}轮'})
        post(host, base + '/answers', {'text': '房主回答'})
        post(player, base + '/answers', {'text': '玩家回答'})
        post(host, base + '/ai/D/answer', {'text': 'AI回答'})
        if number == 1:
            post(host, base + '/next')
    post(host, base + '/verdict', {'predictions': {'A': 'Human', 'B': 'Human', 'D': 'AI'}, 'evidence': '判决', 'confidence': 70})
    post(host, base + '/reveal')
    post(host, base + '/restart', {'match_number': 1, 'confirm': True})
    assert post(outsider, '/api/join', {'code': created['code']})['alias'] == 'C'


def test_room_management_endpoints_deny_members_and_outsiders(env):
    _, _, _, host, member, outsider = env
    created = post(host, '/api/rooms', {'title': '权限边界'})
    base = f'/api/rooms/{created["room_id"]}'
    post(member, '/api/join', {'code': created['code']})
    cases = [('POST','/ai',{'name':'AI'}), ('POST','/ai-judges',{'name':'裁判'}),
        ('POST','/judges',{'username':'裁判','display_name':'裁判','password':'password123'}),
        ('POST','/judges/assign',{'uid':'TL00000004','label':'裁判'}),
        ('POST','/ai-judges/1/questions',{'text':'题目'}), ('POST','/start',{}),
        ('POST','/question',{'text':'题目'}), ('POST','/ai/B/answer',{'text':'回答'}),
        ('PUT','/ai/B/answer',{'text':'回答'}), ('POST','/members/3/remove',{}),
        ('POST','/next',{}), ('POST','/packets',{}), ('GET','/packets/1',None),
        ('POST','/packets/1/sent',{}),
        ('POST','/verdict',{'predictions':{},'evidence':'依据','confidence':50}),
        ('POST','/ai-judges/1/verdict',{'predictions':{},'evidence':'依据','confidence':50}),
        ('POST','/reveal',{}), ('POST','/restart',{'match_number':1,'confirm':True})]
    for client in (member, outsider):
        for method, suffix, body in cases:
            request(client, method, base + suffix, body, 403)


def test_admin_can_reset_self_and_remove_another_admin(env):
    path, app, admin, host, _, _ = env
    with db_open(path) as db:
        db.execute('UPDATE users SET is_admin=1 WHERE id=2')
    request(admin, 'DELETE', '/api/admin/users/TL00000002', {'confirm':True})
    assert host.get('/api/me').status_code == 401
    result = post(admin, '/api/admin/users/TL00000001/password', {'password':'self-new-password','confirm':True})
    assert result['signed_out']
    assert admin.get('/api/me').status_code == 401
    post(admin, '/api/login', {'username':'admin','password':'self-new-password'})


def test_finished_deleted_member_is_not_retained_on_restart(env):
    _, _, admin, host, player, _ = env
    created = post(host, '/api/rooms', {'title': '完赛后删号'})
    base = f'/api/rooms/{created["room_id"]}'
    finish_match(host, player, base, created['code'])
    request(admin, 'DELETE', '/api/admin/users/TL00000003', {'confirm':True})
    assert len(host.get(base).json()['humans']) == 1  # preserve previous match roster
    post(host, base + '/restart', {'match_number':1,'confirm':True})
    assert host.get(base).json()['humans'] == []
    post(host, base + '/ai', {'name':'补位AI'})
    assert len(host.get(base).json()['ais']) == 2


@pytest.mark.parametrize('number', [1, 2])
def test_deleted_players_in_draft_can_be_replaced_and_start_still_needs_two(env, number):
    _, _, admin, host, p1, judge = env
    created = post(host, '/api/rooms', {'title':'草稿轮恢复'})
    base = f'/api/rooms/{created["room_id"]}'
    for player in (host, p1):
        post(player, '/api/join', {'code':created['code']})
    post(host, base + '/judges/assign', {'uid':'TL00000004','label':'裁判'})
    if number == 2:
        post(judge, base + '/judge-questions', {'text':'首轮问题'})
        post(host, base + '/start')
        for player in (host, p1):
            post(player, base + '/answers', {'text':'历史回答'})
        post(host, base + '/next')
    post(judge, base + '/judge-questions', {'text':'本轮问题'})
    request(admin, 'DELETE', '/api/admin/users/TL00000003', {'confirm':True})
    post(host, base + '/start', status=409)
    # Deleting a host-participant leaves admin able to manage the room.
    request(admin, 'DELETE', '/api/admin/users/TL00000002', {'confirm':True})
    for name in ('补位1','补位2'):
        post(admin, base + '/ai', {'name':name})
    post(admin, base + '/start')
    for alias in ('C','D'):
        post(admin, base + f'/ai/{alias}/answer', {'text':'新回答'})
    assert admin.get(base).json()['rounds'][-1]['status'] == 'revealed'
    post(judge, base + '/judge-verdict', {'predictions':{'C':'AI','D':'AI'},'evidence':'恢复后判决','confidence':50})
    post(admin, base + '/reveal')
    if number == 2:
        assert '历史回答' in admin.get(base + '/log').text


def test_replacements_keep_old_aliases_and_eight_active_player_limit(env):
    _, app, admin, host, player, judge = env
    created = post(host, '/api/rooms', {'title':'八代号占满后的恢复'})
    base = f'/api/rooms/{created["room_id"]}'
    players = [player] + [TestClient(app) for _ in range(7)]
    for i, client in enumerate(players):
        if i:
            post(client, '/api/register', {'username':'玩家','password':'password123'})
        post(client, '/api/join', {'code':created['code']})
    post(host, base + '/judges/assign', {'uid':'TL00000004','label':'裁判'})
    post(judge, base + '/judge-questions', {'text':'保持匿名历史'})
    for client in players:
        uid = client.get('/api/me').json()['user']['uid']
        request(admin, 'DELETE', f'/api/admin/users/{uid}', {'confirm':True})
    assert post(host, base + '/ai', {'name':'补位1'})['alias'] == 'I'
    assert post(host, base + '/ai', {'name':'补位2'})['alias'] == 'J'
    post(host, base + '/ai', {'name':'名单仍锁定'}, 409)
    post(host, base + '/start')
    for alias in ('I','J'):
        post(host, base + f'/ai/{alias}/answer', {'text':'补位回答'})
    assert host.get(base).json()['rounds'][0]['status'] == 'revealed'


def test_room_reusable_repeatedly_default_clears_logs_and_stale_clients_rejected(env):
    path, app, admin, host, player, outsider = env
    created = post(host, '/api/rooms', {'title': '重复比赛'})
    base = f'/api/rooms/{created["room_id"]}'
    post(host, base + '/restart', {'match_number': 1, 'confirm': True}, 409)
    for number in (1, 2):
        finish_match(host, player, base, created['code'], match_number=number)
        post(player, base + '/restart', {'match_number': number, 'confirm': True}, 403)
        post(host, base + '/restart', {'match_number': number, 'confirm': False}, 422)
        with player.websocket_connect(f'/ws/rooms/{created["room_id"]}') as ws:
            ws.receive_json()
            post(host, base + '/restart', {'match_number': number, 'confirm': True})
            message = ws.receive_json()
            assert message['reason'] == 'match_restarted' and message['match_number'] == number + 1
        info = player.get(base).json()
        assert info['match_number'] == number + 1 and info['status'] == 'open'
        assert len(info['rounds']) == 1 and info['rounds'][0]['status'] == 'draft'
        assert not info['records_deleted'] and info['current_round'] == 1
        assert not info['suggestions'] and not info['packets'] and not info['verdicts']
        assert player.get(base + f'/chat?match_number={number+1}').json()['messages'] == []
        post(host, base + '/question', {'text': '旧标签页题目'}, 409, match=number)
        post(host, base + '/restart', {'match_number': number, 'confirm': True}, 409)
        request(outsider, 'DELETE', base + '/records', {'match_number': number, 'confirm': True}, 409)
        with db_open(path) as db:
            assert db.execute('SELECT COUNT(*) FROM events WHERE kind!=?', ('match_restarted',)).fetchone()[0] == 0
            assert not db.execute('PRAGMA foreign_key_check').fetchall()
