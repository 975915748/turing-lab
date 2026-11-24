import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from cryptography.fernet import Fernet

import app as app_module
from app import COOKIE, create_app, db_open, hash_password, seed_admin


def request(client, method, path, body=None, expected=200, headers=None):
    current = client.get('/api/me')
    merged = dict(headers or {})
    if current.status_code == 200:
        merged.setdefault('X-CSRF-Token', current.json()['user']['csrf'])
    response = client.request(method, path, json=body, headers=merged)
    assert response.status_code == expected, (method, path, response.status_code, response.text)
    return response.json() if response.content and expected == 200 else {}


def post(client, path, body=None, expected=200):
    return request(client, 'POST', path, body or {}, expected)


def put(client, path, body=None, expected=200):
    return request(client, 'PUT', path, body or {}, expected)


def delete(client, path, body=None, expected=200):
    return request(client, 'DELETE', path, body or {}, expected)


def register(client, username, display_name, password='player-password'):
    return post(client, '/api/register', {
        'username': username, 'display_name': display_name, 'password': password,
    })


def make_profile(client, name='个人模型', api_key='sk-personal-test'):
    return post(client, '/api/me/models', {
        'name': name, 'base_url': 'https://api.example.com/v1',
        'model': 'test-model', 'api_key': api_key,
        'temperature': 0.7, 'max_tokens': 2048, 'timeout_seconds': 60,
    })


@pytest.fixture
def lab(tmp_path, monkeypatch):
    monkeypatch.setenv('MODEL_CREDENTIAL_KEY', Fernet.generate_key().decode('ascii'))
    monkeypatch.setattr(app_module, 'validate_base_url', lambda value: value.strip().rstrip('/'))
    path = tmp_path / 'room-chat-personal.sqlite3'
    seed_admin(path, 'room_admin', '房主管理员', 'room-admin-password')
    app = create_app(path)
    admin = TestClient(app)
    post(admin, '/api/login', {'username': 'room_admin', 'password': 'room-admin-password'})

    admin2_id = None
    with db_open(path) as db:
        cur = db.execute('''INSERT INTO users(username,legacy_username,display_name,pw_hash,is_admin,created_at)
            VALUES(?,?,?,?,1,'2026-11-24T00:00:00+00:00')''',
            ('second_admin', 'second_admin', '第二管理员', hash_password('second-admin-password')))
        admin2_id = cur.lastrowid
    admin2 = TestClient(app)
    post(admin2, '/api/login', {'username': 'second_admin', 'password': 'second-admin-password'})

    room = post(admin, '/api/rooms', {'title': '公共聊天与个人中心验收', 'judge': '旧裁判'})
    base = f"/api/rooms/{room['room_id']}"
    p1, p2, judge, outsider = (TestClient(app) for _ in range(4))
    a = register(p1, 'same_name_one', '同名玩家')
    b = register(p2, 'same_name_two', '同名玩家')
    j = register(judge, 'human_judge', '真人裁判')
    register(outsider, 'outsider', '房间外用户')
    post(p1, '/api/join', {'code': room['code']})
    post(p2, '/api/join', {'code': room['code']})
    post(judge, '/api/join', {'code': room['code'], 'role': 'judge'})
    with db_open(path) as db:
        p1_id = db.execute('SELECT id FROM users WHERE username=?', ('same_name_one',)).fetchone()[0]
        p2_id = db.execute('SELECT id FROM users WHERE username=?', ('same_name_two',)).fetchone()[0]
        judge_id = db.execute('SELECT id FROM users WHERE username=?', ('human_judge',)).fetchone()[0]
    return {
        'path': path, 'app': app, 'admin': admin, 'admin2': admin2, 'admin2_id': admin2_id,
        'p1_id': p1_id, 'p2_id': p2_id, 'judge_id': judge_id,
        'p1': p1, 'p2': p2, 'judge': judge, 'outsider': outsider,
        'room_id': room['room_id'], 'base': base, 'code': room['code'],
        'uid1': a['uid'], 'uid2': b['uid'], 'judge_uid': j['uid'],
    }


def downgrade_profiles_to_room_scope(path, room_id, invalid_ai_ref=False):
    with db_open(path) as db:
        db.commit()
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('PRAGMA legacy_alter_table=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute('ALTER TABLE model_profiles RENAME TO model_profiles_personal_before_test')
        db.execute('''CREATE TABLE model_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            name TEXT NOT NULL,base_url TEXT NOT NULL,model TEXT NOT NULL,
            api_key_encrypted TEXT NOT NULL,temperature REAL NOT NULL DEFAULT 0.7,
            max_tokens INTEGER NOT NULL DEFAULT 2048,timeout_seconds INTEGER NOT NULL DEFAULT 60,
            created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''')
        db.execute('''INSERT INTO model_profiles(id,room_id,name,base_url,model,api_key_encrypted,
            temperature,max_tokens,timeout_seconds,created_at,updated_at)
            SELECT id,?,name,base_url,model,api_key_encrypted,temperature,max_tokens,timeout_seconds,
                created_at,updated_at FROM model_profiles_personal_before_test''',(room_id,))
        db.execute('DROP TABLE model_profiles_personal_before_test')
        if invalid_ai_ref:
            db.execute('UPDATE ai_players SET model_profile_id=999999 WHERE room_id=?',(room_id,))
        db.commit()
        db.execute('PRAGMA legacy_alter_table=OFF')
        db.execute('PRAGMA foreign_keys=ON')


class FakeSocket:
    def __init__(self, token):
        self.cookies = {COOKIE: token}
        self.messages = []
        self.closed = False

    async def send_json(self, message):
        self.messages.append(message)

    async def close(self, code=1000, reason=None):
        self.closed = True


def test_public_chat_permissions_identity_and_judge_websocket_isolation(lab):
    app, base = lab['app'], lab['base']
    admin, admin2 = lab['admin'], lab['admin2']
    p1, p2, judge, outsider = lab['p1'], lab['p2'], lab['judge'], lab['outsider']

    first = post(p1, base + '/chat', {'match_number': 1, 'text': '  第一条消息  '})['message']
    second = post(p2, base + '/chat', {'match_number': 1, 'text': '第二条消息'})['message']
    post(admin, base + '/chat', {'match_number': 1, 'text': '房主消息'})
    post(admin2, base + '/chat', {'match_number': 1, 'text': '全站管理员消息'})

    assert first['text'] == '第一条消息'
    assert first['sender_uid'] == lab['uid1']
    assert second['sender_uid'] == lab['uid2']
    assert first['sender_name'] == second['sender_name'] == '同名玩家'
    history = p1.get(base + '/chat?match_number=1').json()['messages']
    assert [m['id'] for m in history] == sorted(m['id'] for m in history)
    assert len(history) == 4

    state = p1.get(base).json()
    assert 'chat' not in state and 'chat_messages' not in state
    judge_state = judge.get(base).json()
    assert judge_state['is_judge'] is True
    assert 'chat' not in judge_state and 'chat_messages' not in judge_state
    assert '第一条消息' not in json.dumps(judge_state, ensure_ascii=False)
    assert judge.get(base + '/chat?match_number=1').status_code == 403
    assert post(judge, base + '/chat', {'match_number': 1, 'text': '裁判越权'}, 403) == {}
    assert outsider.get(base + '/chat?match_number=1').status_code == 403
    assert post(outsider, base + '/chat', {'match_number': 1, 'text': '房间外越权'}, 403) == {}

    # The WebSocket fan-out re-authenticates each socket and never sends chat data to a judge.
    player_socket = FakeSocket(p1.cookies.get(COOKIE))
    judge_socket = FakeSocket(judge.cookies.get(COOKIE))
    app.state.hub.groups.setdefault(lab['room_id'], set()).update({player_socket, judge_socket})
    try:
        asyncio.run(app.state.hub.notify_chat(lab['room_id'], {
            'id': 999, 'match_number': 1, 'sender_uid': lab['uid1'],
            'sender_name': '同名玩家', 'text': '<script>alert(1)</script>',
            'created_at': '2026-11-24T00:00:00+00:00',
        }))
        assert len(player_socket.messages) == 1
        assert judge_socket.messages == []
        assert player_socket.messages[0]['message']['text'] == '<script>alert(1)</script>'
    finally:
        app.state.hub.groups[lab['room_id']].discard(player_socket)
        app.state.hub.groups[lab['room_id']].discard(judge_socket)

    assert post(p1, base + '/suggestions', {'text': '旧写接口'}, 410) == {}
    assert p1.post(base + '/chat', json={'match_number': 1, 'text': '缺少 CSRF'}).status_code == 403
    assert post(p1, base + '/chat', {'match_number': 1, 'text': '   '}, 422) == {}
    assert post(p1, base + '/chat', {'match_number': 1, 'text': 'x' * 2001}, 422) == {}
    assert post(p1, base + '/chat', {'text': '缺少场次'}, 422) == {}
    assert p1.get(base + '/chat').status_code == 422

    # A room's history is not visible in another room.
    other = post(admin, '/api/rooms', {'title': '隔离房间'})
    other_base = f"/api/rooms/{other['room_id']}"
    assert p1.get(other_base + '/chat?match_number=1').status_code == 403
    post(admin, other_base + '/chat', {'match_number': 1, 'text': 'B 房间消息'})
    assert p1.get(base + '/chat?match_number=1').json()['messages'][0]['text'] == '第一条消息'
    assert admin.get(other_base + '/chat?match_number=1').json()['messages'][0]['text'] == 'B 房间消息'

    # Removed members and deleted accounts lose access immediately.
    with db_open(lab['path']) as db:
        db.execute('UPDATE members SET removed_at=? WHERE room_id=? AND user_id=?',
                   ('2026-11-24T01:00:00+00:00', lab['room_id'], lab['p2_id']))
    assert p2.get(base + '/chat?match_number=1').status_code == 403
    assert len(p1.get(base + '/chat?match_number=1').json()['messages']) == 4
    delete(admin, f"/api/admin/users/{lab['uid1']}", {'confirm': True})
    assert p1.get('/api/me').status_code == 401
    assert p1.get(base + '/chat?match_number=1').status_code == 401


def test_chat_is_cleared_on_successful_reveal_but_not_failed_reveal(lab):
    admin, p1, p2 = lab['admin'], lab['p1'], lab['p2']
    room=post(admin,'/api/rooms',{'title':'独立完赛清聊测试'})
    base=f"/api/rooms/{room['room_id']}"
    post(p1,'/api/join',{'code':room['code']})
    post(p2,'/api/join',{'code':room['code']})
    post(p1, base + '/chat', {'match_number': 1, 'text': '本场聊天不得进入比赛记录'})
    assert post(admin, base + '/reveal', {}, 409) == {}
    assert len(p1.get(base + '/chat?match_number=1').json()['messages']) == 1

    post(admin, base + '/question', {'text': '本场正式问题'})
    post(p1, base + '/answers', {'text': '人类回答 A'})
    post(p2, base + '/answers', {'text': '人类回答 B'})
    post(admin, base + '/packets', {})
    post(admin, base + '/verdict', {
        'predictions': {'A': 'Human', 'B': 'Human'},
        'confidence': 90, 'evidence': '匿名回答依据',
    })
    post(admin, base + '/reveal', {})

    final = p1.get(base).json()
    assert final['status'] == 'finished'
    assert final['rounds'][0]['answers']['A']['text'] == '人类回答 A'
    assert final['rounds'][0]['answers']['B']['text'] == '人类回答 B'
    assert final['verdict']['evidence'] == '匿名回答依据'
    assert p1.get(base + '/chat?match_number=1').json()['messages'] == []
    assert post(p1, base + '/chat', {'match_number': 1, 'text': '完赛后发送'}, 409) == {}
    assert '本场聊天不得进入比赛记录' not in p1.get(base + '/rounds/1/log').text

    with db_open(lab['path']) as db:
        assert db.execute('SELECT COUNT(*) FROM packets WHERE room_id=?', (room['room_id'],)).fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM answers WHERE round_id=(SELECT id FROM rounds WHERE room_id=? AND number=1)',
                          (room['room_id'],)).fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM events WHERE room_id=? AND text LIKE ?',
                          (room['room_id'], '%本场聊天不得进入比赛记录%')).fetchone()[0] == 0
        # Simulate an old residual row to verify the restart / delete-records cleanup paths too.
        db.execute('''INSERT INTO chat_messages(room_id,match_number,sender_user_id,sender_uid,sender_name,text,created_at)
            VALUES(?,?,?,?,?,?,?)''',(room['room_id'],1,lab['p1_id'],lab['uid1'],'同名玩家','模拟残留', '2026-11-24T00:00:00+00:00'))

    post(admin, base + '/restart', {'match_number': 1})
    new_room = admin.get(base).json()
    assert new_room['match_number'] == 2
    assert p1.get(base + '/chat?match_number=2').json()['messages'] == []
    assert post(p1, base + '/chat', {'match_number': 1, 'text': '旧标签页串场'}, 409) == {}

    post(p1, base + '/chat', {'match_number': 2, 'text': '第二场消息'})
    post(admin, base + '/question', {'text': '第二场正式问题'})
    post(p1, base + '/answers', {'text': '第二场回答 A'})
    post(p2, base + '/answers', {'text': '第二场回答 B'})
    post(admin, base + '/verdict', {
        'predictions': {'A': 'Human', 'B': 'Human'},
        'confidence': 80, 'evidence': '第二场依据',
    })
    post(admin, base + '/reveal', {})
    with db_open(lab['path']) as db:
        db.execute('''INSERT INTO chat_messages(room_id,match_number,sender_user_id,sender_uid,sender_name,text,created_at)
            VALUES(?,?,?,?,?,?,?)''',(room['room_id'],2,lab['p1_id'],lab['uid1'],'同名玩家','模拟残留2', '2026-11-24T00:00:00+00:00'))
    delete(admin, base + '/records', {'match_number': 2})
    assert admin.get(base).json()['records_deleted'] is True
    with db_open(lab['path']) as db:
        assert db.execute('SELECT COUNT(*) FROM chat_messages WHERE room_id=?', (room['room_id'],)).fetchone()[0] == 0


def test_personal_name_update_and_password_rotation_revoke_every_session(lab):
    p1, base = lab['p1'], lab['base']
    original = p1.get('/api/me').json()['user']
    with db_open(lab['path']) as db:
        db.execute('UPDATE users SET legacy_username=? WHERE id=?', ('legacy_player_one', lab['p1_id']))
    p1_second = TestClient(lab['app'])
    post(p1_second, '/api/login', {'uid': original['uid'], 'password': 'player-password'})

    changed = put(p1, '/api/me/profile', {'display_name': '改名后的玩家'})
    assert changed['user']['uid'] == original['uid']
    assert changed['user']['display_name'] == '改名后的玩家'
    assert p1.get('/api/me').json()['user']['username'] == original['username']
    assert p1.get(base).json()['humans'][0]['name'] == '改名后的玩家'
    with db_open(lab['path']) as db:
        row = db.execute('SELECT username,legacy_username FROM users WHERE id=?',(lab['p1_id'],)).fetchone()
        assert row['username'] == original['username']
        assert row['legacy_username'] == 'legacy_player_one'
    legacy_login = TestClient(lab['app'])
    post(legacy_login, '/api/login', {'username': 'legacy_player_one', 'password': 'player-password'})
    assert legacy_login.get('/api/me').json()['user']['uid'] == original['uid']

    assert p1.post('/api/me/password', json={
        'current_password': 'wrong-password', 'new_password': 'new-password-1',
        'confirm_password': 'new-password-1',
    }, headers={'X-CSRF-Token': p1.get('/api/me').json()['user']['csrf']}).status_code == 403
    assert post(p1, '/api/me/password', {
        'current_password': 'player-password', 'new_password': 'new-password-1',
        'confirm_password': 'new-password-2',
    }, 422) == {}
    assert p1.post('/api/me/password', json={
        'current_password': 'player-password', 'new_password': 'new-password-1',
        'confirm_password': 'new-password-1',
    }).status_code == 403

    result = post(p1, '/api/me/password', {
        'current_password': 'player-password', 'new_password': 'new-password-1',
        'confirm_password': 'new-password-1',
    })
    assert result['signed_out'] is True
    assert p1.get('/api/me').status_code == 401
    assert p1_second.get('/api/me').status_code == 401
    new_login = TestClient(lab['app'])
    post(new_login, '/api/login', {'uid': original['uid'], 'password': 'new-password-1'})
    assert new_login.get('/api/me').json()['user']['uid'] == original['uid']
    old_login = TestClient(lab['app'])
    post(old_login, '/api/login', {'uid': original['uid'], 'password': 'player-password'}, 401)


def test_concurrent_password_changes_cannot_both_use_the_same_old_password(lab):
    p1 = lab['p1']
    original = p1.get('/api/me').json()['user']
    one, two = TestClient(lab['app']), TestClient(lab['app'])
    post(one, '/api/login', {'uid': original['uid'], 'password': 'player-password'})
    post(two, '/api/login', {'uid': original['uid'], 'password': 'player-password'})
    barrier = __import__('threading').Barrier(2)

    def change(client, new_password):
        me = client.get('/api/me').json()
        barrier.wait()
        return client.post('/api/me/password', json={
            'current_password': 'player-password',
            'new_password': new_password,
            'confirm_password': new_password,
        }, headers={'X-CSRF-Token': me['user']['csrf']}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda args: change(*args), [(one, 'parallel-pass-1'), (two, 'parallel-pass-2')]))
    assert sorted(statuses) == [200, 401]


def test_personal_model_ownership_room_binding_and_cross_room_reuse(lab):
    p1, admin2, p2 = lab['p1'], lab['admin2'], lab['p2']
    room_a = post(p1, '/api/rooms', {'title': '配置所有权 A'})
    room_b = post(p1, '/api/rooms', {'title': '配置所有权 B'})
    post(p2, '/api/join', {'code': room_a['code']})
    base_a, base_b = f"/api/rooms/{room_a['room_id']}", f"/api/rooms/{room_b['room_id']}"
    profile_a = make_profile(p1, 'A 的个人配置', 'sk-user-a')
    profile_admin = make_profile(admin2, '管理员自己的配置', 'sk-admin-two')
    unused = make_profile(admin2, '未绑定的其他配置', 'sk-admin-unused')

    assert [x['id'] for x in p1.get('/api/me/models').json()['models']] == [profile_a['id']]
    assert {x['id'] for x in admin2.get('/api/me/models').json()['models']} == {profile_admin['id'], unused['id']}
    assert p2.get('/api/me/models').json()['models'] == []
    assert put(p1, f"/api/me/models/{profile_admin['id']}", {
        'name': '越权修改', 'base_url': 'https://api.example.com/v1',
        'model': 'test-model', 'api_key': '', 'temperature': 0.7,
        'max_tokens': 2048, 'timeout_seconds': 60,
    }, 404) == {}
    assert delete(p1, f"/api/me/models/{profile_admin['id']}", {}, 404) == {}
    assert post(p1, f"/api/me/models/{profile_admin['id']}/test", {}, 404) == {}

    # A room owner can use only their own profile; a global admin can use their own profile in that room.
    assert post(p1, base_a + '/ai', {'name': '拒绝外部配置', 'model_profile_id': profile_admin['id']}, 404) == {}
    ai_admin = post(admin2, base_a + '/ai', {'name': '管理员配置 AI', 'model_profile_id': profile_admin['id']})
    ai_owner = post(p1, base_a + '/ai', {'name': '房主配置 AI', 'model_profile_id': profile_a['id']})
    post(p1, base_b + '/ai', {'name': '跨房间复用', 'model_profile_id': profile_a['id']})
    assert ai_admin['alias'] != ai_owner['alias']

    manager_view = p1.get(base_a).json()
    bound = {x['id']: x for x in manager_view['model_profiles']}
    assert set(bound) == {profile_admin['id'], profile_a['id']}
    assert bound[profile_admin['id']]['owned_by_me'] is False
    assert bound[profile_a['id']]['owned_by_me'] is True
    assert 'base_url' not in bound[profile_admin['id']]
    assert 'api_key' not in bound[profile_admin['id']]
    assert 'api_key_encrypted' not in bound[profile_admin['id']]
    assert unused['id'] not in bound
    member_view = p2.get(base_a).json()
    assert member_view['model_profiles'] == []
    assert all(a['model_profile_id'] is None and a['model_profile'] is None for a in member_view['ais'])

    # Keep, unbind, and rebind a seat without recreating it or changing its alias.
    original_alias = ai_owner['alias']
    assert put(p1, base_a + f"/ai/{original_alias}/model", {'keep_existing': True})['unchanged'] is True
    put(p1, base_a + f"/ai/{original_alias}/model", {'model_profile_id': None})
    state = p1.get(base_a).json()
    seat = next(a for a in state['ais'] if a['alias'] == original_alias)
    assert seat['model_profile_id'] is None
    put(p1, base_a + f"/ai/{original_alias}/model", {'model_profile_id': profile_a['id']})
    state = p1.get(base_a).json()
    assert next(a for a in state['ais'] if a['alias'] == original_alias)['model_profile_id'] == profile_a['id']

    # Deleting one room never deletes personal profiles or their bindings in another room.
    delete(p1, base_a, {'match_number': 1})
    assert {x['id'] for x in p1.get('/api/me/models').json()['models']} == {profile_a['id']}
    room_b_state = p1.get(base_b).json()
    assert any(a['name'] == '跨房间复用' and a['model_profile_id'] == profile_a['id'] for a in room_b_state['ais'])

    # Deleting a profile detaches every bound seat but preserves the AI seats and their history.
    delete(p1, f"/api/me/models/{profile_a['id']}", {})
    after = p1.get(base_b).json()
    reused = next(a for a in after['ais'] if a['name'] == '跨房间复用')
    assert reused['alias'] == 'A'
    assert reused['model_profile_id'] is None


def test_deleted_profile_owner_is_retained_but_cannot_trigger_new_model_calls(lab, monkeypatch):
    p1, admin2 = lab['p1'], lab['admin2']
    room = post(p1, '/api/rooms', {'title': '账号删除后模型配置'})
    base = f"/api/rooms/{room['room_id']}"
    profile = make_profile(p1, '删除账号后停用')
    ai = post(p1, base + '/ai', {'name': '已绑定 AI', 'model_profile_id': profile['id']})
    called = []
    async def fake_chat(*args, **kwargs):
        called.append(True)
        return {'text': '不应发生', 'usage': {}}
    monkeypatch.setattr(app_module, 'chat_completion', fake_chat)

    delete(admin2, f"/api/admin/users/{lab['uid1']}", {'confirm': True})
    with db_open(lab['path']) as db:
        row = db.execute('SELECT owner_user_id,api_key_encrypted FROM model_profiles WHERE id=?',
                         (profile['id'],)).fetchone()
        assert row['owner_user_id'] == lab['p1_id'] and row['api_key_encrypted']
    state = admin2.get(base).json()
    seat = next(a for a in state['ais'] if a['alias'] == ai['alias'])
    assert seat['model_profile']['credential_status'] == 'owner_unavailable'
    post(admin2, base + '/question', {'text': '开始 AI 回答'})
    generated = post(admin2, base + '/ai/answers/generate')
    assert generated['generated'] == 0
    assert called == []


def test_migrates_room_profiles_to_original_owner_and_keeps_ai_references(lab):
    path, admin, base = lab['path'], lab['admin'], lab['base']
    profile = make_profile(admin, '迁移旧配置', 'sk-legacy-key')
    ai = post(admin, base + '/ai', {'name': '迁移 AI', 'model_profile_id': profile['id']})
    with db_open(path) as db:
        stamp='2026-11-24T00:00:00+00:00'
        db.execute('UPDATE rounds SET question=?,status=?,revealed_at=? WHERE room_id=? AND number=1',
                   ('旧版已完成问题','revealed',stamp,lab['room_id']))
        db.execute('INSERT INTO answers(round_id,alias,text,submitted_at) VALUES(?,?,?,?)',
                   (1, ai['alias'], '旧比赛回答', stamp))
        db.execute('INSERT INTO judge_questions(room_id,round_number,judge_user_id,text,created_at) VALUES(?,?,?,?,?)',
                   (lab['room_id'],1,lab['judge_id'],'旧版裁判问题',stamp))
        db.execute('INSERT INTO judge_verdicts(room_id,judge_user_id,verdict_json,locked_at) VALUES(?,?,?,?)',
                   (lab['room_id'],lab['judge_id'],'{"predictions":{"A":"Human"}}',stamp))
        db.execute('INSERT INTO packets(room_id,through_round,version,content,sha256,created_at,sent_at) VALUES(?,?,?,?,?,?,?)',
                   (lab['room_id'],1,1,'旧版匿名快照','legacy-sha',stamp,stamp))
        db.execute('UPDATE rooms SET status=?,verdict_json=?,verdict_locked_at=?,revealed_at=? WHERE id=?',
                   ('finished','{"predictions":{"A":"Human"}}',stamp,stamp,lab['room_id']))
    downgrade_profiles_to_room_scope(path, lab['room_id'])

    # Re-running application initialization performs the versioned SQLite Backup API migration.
    migrated_app = create_app(path)
    with db_open(path) as db:
        cols = {row['name'] for row in db.execute('PRAGMA table_info(model_profiles)')}
        assert 'room_id' not in cols and 'owner_user_id' in cols
        migrated = db.execute('SELECT * FROM model_profiles WHERE id=?',(profile['id'],)).fetchone()
        assert migrated['owner_user_id'] == 1
        assert migrated['legacy_room_id'] == lab['room_id']
        assert migrated['ownership_issue'] is None
        assert db.execute('SELECT model_profile_id FROM ai_players WHERE room_id=? AND alias=?',
                          (lab['room_id'],ai['alias'])).fetchone()[0] == profile['id']
        assert db.execute('SELECT text FROM answers WHERE round_id=1 AND alias=?',(ai['alias'],)).fetchone()[0] == '旧比赛回答'
        assert db.execute('SELECT status FROM rounds WHERE room_id=? AND number=1',(lab['room_id'],)).fetchone()[0] == 'revealed'
        assert db.execute('SELECT text FROM judge_questions WHERE room_id=? AND round_number=1',(lab['room_id'],)).fetchone()[0] == '旧版裁判问题'
        assert db.execute('SELECT verdict_json FROM judge_verdicts WHERE room_id=?',(lab['room_id'],)).fetchone()[0] == '{"predictions":{"A":"Human"}}'
        assert db.execute('SELECT content FROM packets WHERE room_id=?',(lab['room_id'],)).fetchone()[0] == '旧版匿名快照'
        assert db.execute('SELECT status FROM rooms WHERE id=?',(lab['room_id'],)).fetchone()[0] == 'finished'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    backups = list(Path(path).parent.glob(path.stem + '.pre-v1_7-*.sqlite3'))
    assert len(backups) == 1
    # A second initialization is idempotent and does not create another migration backup.
    create_app(path)
    assert len(list(Path(path).parent.glob(path.stem + '.pre-v1_7-*.sqlite3'))) == 1


def test_migration_preserves_unowned_legacy_profile_without_reassigning_it(lab):
    admin, admin2 = lab['admin'], lab['admin2']
    room = post(admin, '/api/rooms', {'title': '旧房主缺失的模型配置'})
    base = f"/api/rooms/{room['room_id']}"
    profile = make_profile(admin, '不可自动归属', 'sk-orphaned')
    ai = post(admin, base + '/ai', {'name': '保留原绑定', 'model_profile_id': profile['id']})
    with db_open(lab['path']) as db:
        db.execute('UPDATE rooms SET owner_id=NULL WHERE id=?',(room['room_id'],))
    downgrade_profiles_to_room_scope(lab['path'], room['room_id'])

    create_app(lab['path'])
    with db_open(lab['path']) as db:
        row=db.execute('SELECT * FROM model_profiles WHERE id=?',(profile['id'],)).fetchone()
        assert row['owner_user_id'] is None
        assert row['legacy_room_id'] == room['room_id']
        assert row['ownership_issue'] == 'legacy_owner_missing'
        assert row['api_key_encrypted']
        assert db.execute('SELECT model_profile_id FROM ai_players WHERE room_id=? AND alias=?',
                          (room['room_id'],ai['alias'])).fetchone()[0] == profile['id']
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert admin.get('/api/me/models').json()['models'] == []
    summary=admin2.get(base).json()['model_profiles']
    assert next(x for x in summary if x['id']==profile['id'])['credential_status']=='owner_unavailable'
    assert post(admin2, base + '/ai', {'name':'不能接管','model_profile_id':profile['id']}, 404) == {}


def test_failed_profile_migration_rolls_back_and_can_be_retried_after_source_repair(lab):
    admin, base = lab['admin'], lab['base']
    profile = make_profile(admin, '迁移失败恢复', 'sk-migration-retry')
    ai = post(admin, base + '/ai', {'name': '迁移引用', 'model_profile_id': profile['id']})
    downgrade_profiles_to_room_scope(lab['path'], lab['room_id'], invalid_ai_ref=True)

    with pytest.raises(sqlite3.IntegrityError):
        create_app(lab['path'])
    with db_open(lab['path']) as db:
        columns={row['name'] for row in db.execute('PRAGMA table_info(model_profiles)')}
        assert 'room_id' in columns and 'owner_user_id' not in columns
        row=db.execute('SELECT api_key_encrypted FROM model_profiles WHERE id=?',(profile['id'],)).fetchone()
        assert row['api_key_encrypted']
        assert db.execute('SELECT model_profile_id FROM ai_players WHERE room_id=? AND alias=?',
                          (lab['room_id'],ai['alias'])).fetchone()[0] == 999999
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_profiles_user_upgrade'").fetchone()[0] == 0
    assert list(Path(lab['path']).parent.glob(Path(lab['path']).stem + '.pre-v1_7-*.sqlite3'))

    with db_open(lab['path']) as db:
        db.execute('UPDATE ai_players SET model_profile_id=? WHERE room_id=? AND alias=?',
                   (profile['id'],lab['room_id'],ai['alias']))
    create_app(lab['path'])
    with db_open(lab['path']) as db:
        migrated=db.execute('SELECT owner_user_id,api_key_encrypted FROM model_profiles WHERE id=?',
                            (profile['id'],)).fetchone()
        assert migrated['owner_user_id'] == 1
        assert migrated['api_key_encrypted']
        assert db.execute('SELECT model_profile_id FROM ai_players WHERE room_id=? AND alias=?',
                          (lab['room_id'],ai['alias'])).fetchone()[0] == profile['id']
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


