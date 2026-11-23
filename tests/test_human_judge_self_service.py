"""真人裁判自助入房、单未完赛房间占用与兼容性回归。"""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import create_app, db_open, seed_admin


def request(client, method, path, body=None, status=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']} if me.status_code == 200 else {}
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == status, (path, response.status_code, response.text)
    return response.json() if response.content else {}


def post(client, path, body=None, status=200):
    return request(client, 'POST', path, body or {}, status)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'self-service-judges.sqlite3'
    seed_admin(path, 'self_judge_admin', '主持人', 'admin-password')
    app = create_app(path)
    admin = TestClient(app)
    post(admin, '/api/login', {'username': 'self_judge_admin', 'password': 'admin-password'})
    return path, app, admin


def register(app, username, display_name=None, password='user-password'):
    client = TestClient(app)
    result = post(client, '/api/register', {
        'username': username,
        'display_name': display_name or username,
        'password': password,
    })
    return client, result['uid']


def login_uid(app, uid, password='user-password'):
    client = TestClient(app)
    post(client, '/api/login', {'uid': uid, 'password': password})
    return client


def complete_match(admin, player_a, player_b, judge, room):
    base = f'/api/rooms/{room["room_id"]}'
    post(player_a, '/api/join', {'code': room['code']})
    post(player_b, '/api/join', {'code': room['code']})
    post(judge, '/api/join', {'code': room['code'], 'role': 'judge'})
    post(judge, base + '/judge-questions', {'text': '这一场的匿名裁判问题'})
    post(admin, base + '/start')
    post(player_a, base + '/answers', {'text': 'A 的回答'})
    post(player_b, base + '/answers', {'text': 'B 的回答'})
    post(judge, base + '/judge-verdict', {
        'predictions': {'A': 'Human', 'B': 'AI'},
        'evidence': '独立裁判判断依据',
        'confidence': 80,
    })
    post(admin, base + '/reveal')


def test_self_service_judges_repeat_join_and_legacy_player_flows(env):
    path, app, admin = env
    room = post(admin, '/api/rooms', {'title': '自助裁判入房'})
    base = f'/api/rooms/{room["room_id"]}'

    player_a, _ = register(app, '同名参赛者')
    csrfless = player_a.post('/api/join', json={'code': room['code'], 'role': 'judge'})
    assert csrfless.status_code == 403
    post(player_a, '/api/join', {'code': room['code']})  # 旧请求不带 role，仍是参赛者
    player_b = TestClient(app)
    post(player_b, '/api/register', {
        'username': '旧邀请码注册',
        'display_name': '旧注册玩家',
        'password': 'user-password',
        'code': room['code'],
    })  # 旧的带邀请码注册调用仍然可用

    judge_a, uid_a = register(app, '同名裁判', '真人裁判甲')
    post(judge_a, '/api/logout')
    post(judge_a, '/api/login', {'uid': uid_a, 'password': 'user-password'})
    joined = post(judge_a, '/api/join', {'code': room['code'], 'role': 'judge'})
    assert joined['role'] == 'judge' and joined['room_id'] == room['room_id']
    assert judge_a.get(base).json()['is_judge']
    blind = judge_a.get(base).json()
    for key in ('humans', 'ais', 'invite_code', 'judges', 'owner_id'):
        assert key not in blind
    assert '同名裁判' not in str(blind)

    # A second browser session for the same UID is idempotent, not another seat.
    judge_a_second_session = login_uid(app, uid_a)
    repeated = post(judge_a_second_session, '/api/join', {'code': room['code'], 'role': 'judge'})
    assert repeated['role'] == 'judge'
    with db_open(path) as db:
        assert db.execute('SELECT COUNT(*) FROM room_judges WHERE room_id=? AND user_id=?',
                          (room['room_id'], int(uid_a[2:]))).fetchone()[0] == 1

    # Same display/login name does not merge distinct UIDs; multiple judges per room remain supported.
    judge_b, uid_b = register(app, '同名裁判', '真人裁判乙')
    post(judge_b, '/api/join', {'code': room['code'], 'role': 'judge'})
    spoof, spoof_uid = register(app, '客户端指定他人UID')
    post(spoof, '/api/join', {'code': room['code'], 'role': 'judge', 'uid': uid_a, 'user_id': 1})
    host_view = admin.get(base).json()
    human_judges = [j for j in host_view['judges'] if j['kind'] == 'human']
    assert {j['uid'] for j in human_judges} == {uid_a, uid_b, spoof_uid}

    post(judge_a, '/api/join', {'code': room['code']}, 409)  # 裁判不能再以玩家身份加入
    post(player_a, '/api/join', {'code': room['code'], 'role': 'judge'}, 409)  # 玩家不能兼任本房间裁判
    post(admin, '/api/join', {'code': room['code'], 'role': 'judge'}, 403)  # 管理员不能冒充独立裁判
    post(player_a, '/api/join', {'code': 'INVALID', 'role': 'judge'}, 404)
    post(player_a, '/api/join', {'code': room['code'], 'role': 'observer'}, 422)
    post(TestClient(app), '/api/join', {'code': room['code'], 'role': 'judge'}, 401)
    # Legacy register-with-code remains a player path, not a way to choose another identity.
    assert player_b.get(base).json()['mine'] is not None


def test_room_owner_cannot_join_as_judge_and_player_role_is_not_globally_limited(env):
    _, app, admin = env
    owner, _ = register(app, '普通房主')
    room_a = post(owner, '/api/rooms', {'title': '房主不能裁判'})
    post(owner, '/api/join', {'code': room_a['code'], 'role': 'judge'}, 409)

    room_b = post(admin, '/api/rooms', {'title': '跨房间玩家身份'})
    user, uid = register(app, '裁判也是玩家')
    post(user, '/api/join', {'code': room_a['code'], 'role': 'judge'})
    # The rule is only about simultaneous judge seats; joining another room as a player is allowed.
    post(user, '/api/join', {'code': room_b['code']})
    assert user.get(f'/api/rooms/{room_b["room_id"]}').json()['mine'] is not None
    post(admin, f'/api/rooms/{room_b["room_id"]}/judges/assign',
         {'uid': uid, 'label': '旧接口尝试绕过'}, 409)
    post(user, '/api/join', {'code': room_b['code'], 'role': 'judge'}, 409)


def test_two_concurrent_judge_joins_for_one_uid_allow_only_one_room(env):
    path, app, admin = env
    first = post(admin, '/api/rooms', {'title': '并发裁判 A'})
    second = post(admin, '/api/rooms', {'title': '并发裁判 B'})
    one, uid = register(app, '并发裁判')
    two = login_uid(app, uid)

    def attempt(client, code):
        me = client.get('/api/me').json()
        response = client.post('/api/join', json={'code': code, 'role': 'judge'},
                               headers={'X-CSRF-Token': me['user']['csrf']})
        return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: attempt(*args), ((one, first['code']), (two, second['code']))))
    assert sorted(code for code, _ in results) == [200, 409], results
    with db_open(path) as db:
        active = db.execute('''SELECT j.room_id FROM room_judges j JOIN rooms r ON r.id=j.room_id
            WHERE j.user_id=? AND r.status<>'finished' ''', (int(uid[2:]),)).fetchall()
        assert len(active) == 1


def test_completed_room_releases_judge_and_restart_checks_before_clearing_history(env):
    path, app, admin = env
    player_a, _ = register(app, '比赛玩家甲')
    player_b, _ = register(app, '比赛玩家乙')
    judge, uid = register(app, '跨场裁判', '跨场真人裁判')

    room_a = post(admin, '/api/rooms', {'title': '第一场'})
    complete_match(admin, player_a, player_b, judge, room_a)
    base_a = f'/api/rooms/{room_a["room_id"]}'
    assert admin.get(base_a).json()['status'] == 'finished'
    late_judge, _ = register(app, '完赛后不能加入')
    post(late_judge, '/api/join', {'code': room_a['code'], 'role': 'judge'}, 409)

    room_b = post(admin, '/api/rooms', {'title': '第二个未完赛房间'})
    post(player_a, '/api/join', {'code': room_b['code']})
    post(player_b, '/api/join', {'code': room_b['code']})
    post(judge, '/api/join', {'code': room_b['code'], 'role': 'judge'})
    post(judge, base_a + '/restart', {'match_number': 1, 'confirm': True}, 403)

    # A host cannot restart a retained judge seat while that account is active in room B.
    post(admin, base_a + '/restart', {'match_number': 1, 'confirm': True}, 409)
    preserved = admin.get(base_a).json()
    assert preserved['status'] == 'finished' and preserved['match_number'] == 1
    assert '这一场的匿名裁判问题' in admin.get(base_a + '/log').text
    with db_open(path) as db:
        assert db.execute('SELECT COUNT(*) FROM judge_questions WHERE room_id=?',
                          (room_a['room_id'],)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM room_judges WHERE room_id=? AND user_id=?',
                          (room_a['room_id'], int(uid[2:]))).fetchone()[0] == 1

    # Once B is complete, the old retained judge can be reactivated in A.
    complete_match(admin, player_a, player_b, judge, room_b)
    restarted = post(admin, base_a + '/restart', {'match_number': 1, 'confirm': True})
    assert restarted['match_number'] == 2
    assert judge.get(base_a).json()['is_judge']


def test_room_deletion_releases_human_judge_slot(env):
    _, app, admin = env
    judge, _ = register(app, '删除房间后释放')
    first = post(admin, '/api/rooms', {'title': '将被删除'})
    second = post(admin, '/api/rooms', {'title': '加入目标'})
    post(judge, '/api/join', {'code': first['code'], 'role': 'judge'})
    post(judge, '/api/join', {'code': second['code'], 'role': 'judge'}, 409)
    request(admin, 'DELETE', f'/api/rooms/{first["room_id"]}',
            {'match_number': 1, 'confirm': True})
    joined = post(judge, '/api/join', {'code': second['code'], 'role': 'judge'})
    assert joined['role'] == 'judge'


def test_legacy_multi_room_judge_rows_are_not_reassigned_and_block_new_conflicting_actions(env):
    path, app, admin = env
    judge, uid = register(app, '旧数据冲突裁判')
    room_a = post(admin, '/api/rooms', {'title': '旧数据房间 A'})
    room_b = post(admin, '/api/rooms', {'title': '旧数据房间 B'})
    room_c = post(admin, '/api/rooms', {'title': '新目标房间'})
    user_id = int(uid[2:])

    with db_open(path) as db:
        db.execute('INSERT INTO room_judges(room_id,user_id,label,created_at) VALUES(?,?,?,?)',
                   (room_a['room_id'], user_id, '历史裁判 A', '2026-11-22T00:00:00+00:00'))
        db.execute('INSERT INTO room_judges(room_id,user_id,label,created_at) VALUES(?,?,?,?)',
                   (room_b['room_id'], user_id, '历史裁判 B', '2026-11-22T00:00:01+00:00'))
        db.execute("UPDATE rooms SET status='finished' WHERE id=?", (room_a['room_id'],))
        db.execute('''INSERT INTO events(room_id,round_number,kind,actor,text,created_at)
            VALUES(?,1,'legacy_marker','系统','必须保留的旧比赛记录','2026-11-22T00:00:02+00:00')''',
            (room_a['room_id'],))
        round_id = db.execute('SELECT id FROM rounds WHERE room_id=? AND number=1',
                               (room_a['room_id'],)).fetchone()['id']
        db.execute('''INSERT INTO answers(round_id,alias,text,submitted_at)
            VALUES(?,'A','旧回答必须保留','2026-11-22T00:00:03+00:00')''', (round_id,))

    post(judge, '/api/join', {'code': room_c['code'], 'role': 'judge'}, 409)
    post(admin, f'/api/rooms/{room_c["room_id"]}/judges/assign',
         {'uid': uid, 'label': '旧接口不能绕过'}, 409)
    post(admin, f'/api/rooms/{room_a["room_id"]}/restart',
         {'match_number': 1, 'confirm': True}, 409)

    with db_open(path) as db:
        rows = db.execute('SELECT room_id,label FROM room_judges WHERE user_id=? ORDER BY room_id',
                          (user_id,)).fetchall()
        assert [(r['room_id'], r['label']) for r in rows] == [
            (room_a['room_id'], '历史裁判 A'),
            (room_b['room_id'], '历史裁判 B'),
        ]
        assert db.execute('SELECT status,match_number FROM rooms WHERE id=?',
                          (room_a['room_id'],)).fetchone()['status'] == 'finished'
        assert db.execute('SELECT COUNT(*) FROM events WHERE room_id=? AND kind=?',
                          (room_a['room_id'], 'legacy_marker')).fetchone()[0] == 1
        assert db.execute('SELECT text FROM answers WHERE round_id=?',
                          (round_id,)).fetchone()['text'] == '旧回答必须保留'


def test_existing_twelve_judge_limit_still_applies_to_self_service(env):
    _, app, admin = env
    room = post(admin, '/api/rooms', {'title': '十二裁判上限'})
    base = f'/api/rooms/{room["room_id"]}'
    for index in range(12):
        post(admin, base + '/ai-judges', {'name': f'AI 裁判 {index + 1}'})
    judge, _ = register(app, '第十三位裁判')
    post(judge, '/api/join', {'code': room['code'], 'role': 'judge'}, 409)
    assert len(admin.get(base).json()['judges']) == 12


def test_join_and_restart_race_cannot_create_two_active_judge_rooms(env):
    path, app, admin = env
    judge, uid = register(app, '重开入房并发裁判')
    finished = post(admin, '/api/rooms', {'title': '并发重开旧房间'})
    target = post(admin, '/api/rooms', {'title': '并发加入新房间'})
    with db_open(path) as db:
        db.execute("UPDATE rooms SET status='finished' WHERE id=?", (finished['room_id'],))
        db.execute('INSERT INTO room_judges(room_id,user_id,label,created_at) VALUES(?,?,?,?)',
                   (finished['room_id'], int(uid[2:]), '保留裁判', '2026-11-23T00:00:00+00:00'))

    def join_target():
        me = judge.get('/api/me').json()
        response = judge.post('/api/join', json={'code': target['code'], 'role': 'judge'},
                              headers={'X-CSRF-Token': me['user']['csrf']})
        return response.status_code, response.text

    def restart_finished():
        me = admin.get('/api/me').json()
        response = admin.post(f'/api/rooms/{finished["room_id"]}/restart',
                              json={'match_number': 1, 'confirm': True},
                              headers={'X-CSRF-Token': me['user']['csrf']})
        return response.status_code, response.text

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda fn: fn(), (join_target, restart_finished)))
    assert sorted(code for code, _ in results) == [200, 409], results
    with db_open(path) as db:
        active = db.execute('''SELECT j.room_id FROM room_judges j JOIN rooms r ON r.id=j.room_id
            WHERE j.user_id=? AND r.status<>'finished' ORDER BY j.room_id''',
            (int(uid[2:]),)).fetchall()
        assert len(active) == 1
