"""UID 账号的真实 HTTP、SQLite 迁移及匿名权限回归。"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import create_app, db_open, hash_password, seed_admin


def post(client, url, body=None, status=200):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']} if me.status_code == 200 else {}
    result = client.post(url, json=body or {}, headers=headers)
    assert result.status_code == status, (url, result.status_code, result.text)
    return result.json()


@pytest.fixture
def accounts(tmp_path):
    path = tmp_path / 'accounts.sqlite3'
    seed_admin(path, 'original_admin', '主持人', 'admin-password')
    app = create_app(path)
    admin = TestClient(app)
    post(admin, '/api/login', {'username': 'original_admin', 'password': 'admin-password'})
    return path, app, admin


def test_invite_free_registration_duplicate_names_and_uid_login(accounts):
    path, app, admin = accounts
    one, two = TestClient(app), TestClient(app)
    first = post(one, '/api/register', {'username': '同名玩家', 'password': 'same-password'})
    second = post(two, '/api/register', {'username': '同名玩家', 'password': 'same-password'})
    assert first['uid'] == 'TL00000002'
    assert second['uid'] == 'TL00000003'
    assert 'room_id' not in first
    for client, uid in ((one, first['uid']), (two, second['uid'])):
        me = client.get('/api/me').json()
        assert me['rooms'] == []
        assert me['user']['username'] == me['user']['display_name'] == '同名玩家'
        assert me['user']['uid'] == uid
        assert not me['user']['is_admin']
        post(client, '/api/logout')
        post(client, '/api/login', {'uid': uid.lower(), 'password': 'same-password'})
        assert client.get('/api/me').json()['user']['uid'] == uid
    # New display names never become login identifiers, even if unique today.
    post(TestClient(app), '/api/login', {'username': '同名玩家', 'password': 'same-password'}, 401)
    with db_open(path) as db:
        assert db.execute('SELECT COUNT(*) FROM users WHERE username=?', ('同名玩家',)).fetchone()[0] == 2


@pytest.mark.parametrize('extra', [{'uid': 'TL00000001'}, {'id': 1}, {'is_admin': 1}])
def test_registration_cannot_choose_identity_or_privilege(accounts, extra):
    _, app, admin = accounts
    room = post(admin, '/api/rooms', {'title': '防止指定身份'})
    post(TestClient(app), '/api/register', {'username': 'spoof_target', 'display_name': '普通玩家',
        'code': room['code'], 'password': 'password123', **extra}, 422)


@pytest.mark.parametrize('body', [
    {'username': '  ', 'password': 'password123'},
    {'username': '玩家', 'password': 'short'},
    {'username': 'x' * 41, 'password': 'password123'},
])
def test_registration_validation_does_not_create_accounts(accounts, body):
    path, app, _ = accounts
    post(TestClient(app), '/api/register', body, 422)
    with db_open(path) as db:
        assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1


def test_invite_is_only_needed_for_join_and_old_registration_still_atomic(accounts):
    path, app, admin = accounts
    room = post(admin, '/api/rooms', {'title': '邀请码保留'})
    base = f'/api/rooms/{room["room_id"]}'
    player = TestClient(app)
    post(player, '/api/register', {'username': '新玩家', 'password': 'password123'})
    assert player.get(base).status_code == 403
    post(player, '/api/join', {'code': 'INVALID'}, 404)
    post(player, '/api/join', {'code': room['code']})
    assert player.get(base).json()['mine'] == 'A'
    old_caller = TestClient(app)
    result = post(old_caller, '/api/register', {'username': 'old_player', 'display_name': '旧调用者',
        'password': 'password123', 'code': room['code']})
    assert result['room_id'] == room['room_id'] and result['uid'] == 'TL00000003'
    assert old_caller.get(base).json()['mine'] == 'B'
    post(admin, base + '/question', {'text': '已经开局'})
    post(TestClient(app), '/api/register', {'username': 'atomic', 'display_name': '失败回滚',
        'password': 'password123', 'code': room['code']}, 409)
    with db_open(path) as db:
        assert not db.execute('SELECT 1 FROM users WHERE username=?', ('atomic',)).fetchone()


def test_uid_login_errors_csrf_origin_and_throttling(accounts):
    _, app, _ = accounts
    player = TestClient(app)
    uid = post(player, '/api/register', {'username': '安全检查', 'password': 'password123'})['uid']
    csrf = player.get('/api/me').json()['user']['csrf']
    assert player.post('/api/logout', json={}).status_code == 403
    assert TestClient(app).post('/api/register', json={'username': '跨站', 'password': 'password123'},
        headers={'origin': 'https://evil.example'}).status_code == 403
    assert player.post('/api/logout', json={}, headers={'X-CSRF-Token': csrf}).status_code == 200
    for value in ('not-a-uid', 'TL00000000', 'TL0000000002', 'TL99999999999999999999999999'):
        post(player, '/api/login', {'uid': value, 'password': 'password123'}, 401)
    for _ in range(8):
        post(player, '/api/login', {'uid': uid, 'password': 'incorrect'}, 401)
    post(player, '/api/login', {'uid': uid, 'password': 'password123'}, 429)


def test_concurrent_same_name_registrations_get_unique_uids(accounts):
    _, app, _ = accounts
    def register(_):
        with TestClient(app) as client:
            return post(client, '/api/register', {'username': '并发重名', 'password': 'password123'})['uid']
    with ThreadPoolExecutor(max_workers=4) as pool:
        uids = list(pool.map(register, range(8)))
    assert len(set(uids)) == 8


def test_uid_not_reused_after_last_account_removed(accounts):
    path, app, _ = accounts
    first = post(TestClient(app), '/api/register', {'username': '可重复', 'password': 'password123'})
    with db_open(path) as db:
        db.execute('DELETE FROM users WHERE id=2')
    second = post(TestClient(app), '/api/register', {'username': '可重复', 'password': 'password123'})
    assert first['uid'] == 'TL00000002' and second['uid'] == 'TL00000003'


def legacy_database(path):
    schema = (Path(__file__).parent / 'fixtures' / 'v1_3_schema.sql').read_text(encoding='utf-8')
    with db_open(path) as db:
        db.executescript(schema)
        password = hash_password('original-password')
        for id_, username, admin in ((7, 'legacy_admin', 1), (23, 'legacy_player', 0), (30, 'TL00000023', 0)):
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',
                (id_, username, '原始昵称', password, admin, '2026-11-22'))
        db.execute("INSERT INTO rooms(id,title,code,created_at) VALUES(4,'原始比赛','LEGACYROOM','2026-11-22')")
        db.execute("INSERT INTO sessions VALUES('session-marker',23,'csrf-marker','2099-01-01')")
        db.execute("INSERT INTO members(room_id,user_id,alias,joined_at) VALUES(4,23,'A','2026-11-22')")
        db.execute("INSERT INTO rounds(id,room_id,number,question,status,created_at) VALUES(8,4,1,'原始问题','revealed','2026-11-22')")
        db.execute("INSERT INTO answers(id,round_id,alias,text,submitted_at) VALUES(9,8,'A','原始回答','2026-11-22')")
        db.execute("INSERT INTO suggestions(room_id,round_number,sender_id,text,created_at) VALUES(4,1,23,'玩家追问','2026-11-22')")
        db.execute("INSERT INTO room_judges VALUES(4,30,'原裁判','2026-11-22')")
        db.execute("INSERT INTO judge_questions(room_id,round_number,judge_user_id,text,created_at) VALUES(4,1,30,'裁判问题','2026-11-22')")
        db.execute("INSERT INTO judge_verdicts VALUES(4,30,'{}','2026-11-22')")
        db.execute("INSERT INTO ai_players VALUES(4,'B','原 AI')")
        db.execute("INSERT INTO ai_judges VALUES(3,4,'原 AI 裁判','2026-11-22')")
        db.execute("INSERT INTO ai_judge_questions(room_id,round_number,ai_judge_id,text,created_at) VALUES(4,1,3,'原 AI 提问','2026-11-22')")
        db.execute("INSERT INTO ai_judge_verdicts VALUES(4,3,'{}','2026-11-22')")
        db.execute("INSERT INTO ai_answer_revisions(room_id,round_number,alias,previous_text,replacement_text,changed_at) VALUES(4,1,'B','原文','修订','2026-11-22')")
        db.execute("INSERT INTO packets(room_id,through_round,version,content,sha256,created_at) VALUES(4,1,1,'历史快照','existing-marker','2026-11-22')")
        db.execute("INSERT INTO events(room_id,round_number,kind,actor,text,created_at) VALUES(4,1,'joined','A','历史事件','2026-11-22')")
        return snapshot(db)


def snapshot(db):
    # Compare pre-existing application data, excluding later schema additions.
    schema_additions = (
        'model_profiles', 'chat_messages', 'model_operation_tasks', 'model_operation_idempotency',
        'account_points', 'match_settlements', 'points_ledger',
        'friend_requests', 'friendships', 'friend_notes',
        'dm_conversations', 'dm_messages', 'dm_read_positions', 'api_rate_limits',
        'round_workflows', 'round_timeouts',
        'match_persona_freezes', 'match_seat_persona_snapshots',
    )
    placeholders = ','.join('?' for _ in schema_additions)
    tables = [r[0] for r in db.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ({placeholders})",
        schema_additions,
    )]
    result = {}
    for name in tables:
        rows = [dict(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
        if name in ('ai_players', 'ai_judges'):
            for row in rows:
                row.pop('model_profile_id', None)
        if name == 'rooms':
            for row in rows:
                row.pop('model_request_limit', None)
                row.pop('match_started_at', None)
        # Persona migration adds non-null defaults, which are not legacy data.
        persona_columns={
            'ai_players':('persona_preset_id','persona_preset_version','persona_custom_prompt',
                          'persona_output_length','persona_history_rounds','persona_revision'),
            'ai_judges':('persona_preset_id','persona_preset_version','persona_custom_prompt',
                         'persona_output_length','persona_history_rounds','persona_revision'),
        }.get(name,())
        for column in persona_columns:
            for row in rows:
                row.pop(column,None)
        # Exit, timer and workflow migrations add nullable columns.
        added_columns={
            'rooms':('closed_at','round_duration_seconds'),
            'members':('leave_reason',),
            'room_judges':('left_at',),
            'rounds':('started_at','duration_seconds','deadline_at','timeout_ended_at'),
        }.get(name,())
        for column in added_columns:
            for row in rows:
                assert row.pop(column,None) is None
        result[name] = rows
    return result


def test_legacy_migration_preserves_all_records_backup_and_passwords(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    before = legacy_database(path)
    app = create_app(path)
    with db_open(path) as db:
        after = snapshot(db)
        for row in after['users']:
            assert row.pop('legacy_username') == row['username']
            assert row.pop('deleted_at') is None
        for row in after['rooms']:
            assert row.pop('owner_id') == 7
            assert row.pop('match_number') == 1
            assert row.pop('records_deleted') == 0
        assert after == before
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    backups = list(tmp_path.glob('legacy.pre-v1_12-*.sqlite3'))
    assert len(backups) == 1
    with db_open(backups[0]) as db:
        assert snapshot(db) == before
    create_app(path)
    assert len(list(tmp_path.glob('legacy.pre-v1_12-*.sqlite3'))) == 1
    assert seed_admin(path, 'legacy_admin', '不覆盖', 'new-password') is False
    # All original identities retain their IDs; old username login is explicit.
    for old_name, uid in (('legacy_admin', 'TL00000007'), ('legacy_player', 'TL00000023'), ('TL00000023', 'TL00000030')):
        client = TestClient(app)
        post(client, '/api/login', {'username': old_name, 'password': 'original-password'})
        assert client.get('/api/me').json()['user']['uid'] == uid
        post(client, '/api/logout')
        post(client, '/api/login', {'uid': uid, 'password': 'original-password'})
        assert client.get('/api/me').json()['user']['uid'] == uid
    newcomer = TestClient(app)
    registered = post(newcomer, '/api/register', {'username': 'legacy_admin', 'password': 'another-password'})
    assert registered['uid'] == 'TL00000031'
    post(TestClient(app), '/api/login', {'username': 'legacy_admin', 'password': 'another-password'}, 401)


def test_migration_rolls_back_if_copy_fails(tmp_path, monkeypatch):
    path = tmp_path / 'rollback.sqlite3'
    before = legacy_database(path)
    # Real SQLite authorizer simulates a write denial during the transactional copy.
    import app as module
    def failing_open(db_path):
        db = db_open(db_path)
        db.set_authorizer(lambda op, table, *rest:
            sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_INSERT and table == 'users_uid_upgrade'
            else sqlite3.SQLITE_OK)
        return db
    with monkeypatch.context() as patch:
        patch.setattr(module, 'db_open', failing_open)
        with pytest.raises(sqlite3.DatabaseError):
            create_app(path)
    with db_open(path) as db:
        assert snapshot(db) == before
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='users_uid_upgrade'").fetchone()


def test_same_named_judges_assign_by_uid_and_remain_anonymous(accounts):
    _, app, admin = accounts
    room = post(admin, '/api/rooms', {'title': 'UID 裁判绑定'})
    base = f'/api/rooms/{room["room_id"]}'
    one, two = TestClient(app), TestClient(app)
    first = post(one, '/api/register', {'username': '同名账号', 'password': 'password123'})
    second = post(two, '/api/register', {'username': '同名账号', 'password': 'password123'})
    post(admin, base + '/judges/assign', {'uid': first['uid'], 'label': '真人一'})
    assert one.get(base).json()['is_judge']
    assert two.get(base).status_code == 403
    post(admin, base + '/judges/assign', {'username': '同名账号', 'label': '歧义'}, 404)
    judge = post(admin, base + '/judges', {'username': '同名账号', 'display_name': '真人二', 'password': 'password123'})
    assert judge['uid'] == 'TL00000004'
    post(two, '/api/join', {'code': room['code']})
    post(admin, base + '/ai', {'name': 'AI 选手'})
    post(admin, base + '/judges/assign', {'uid': second['uid'], 'label': '不能兼任'}, 409)
    post(admin, base + '/judges/assign', {'uid': 'TL00000001', 'label': '不能绑定管理员'}, 404)
    post(admin, base + '/judges/assign', {'uid': 'TL9999999999999999999999', 'label': '无效 UID'}, 404)
    me = two.get('/api/me').json()
    roster = two.get(base).json()['humans']
    assert roster[0]['uid'] == me['user']['uid']
    assert admin.get(base).json()['judges'][0]['uid'] == first['uid']
    # UID cannot leak through judge room data or judge logs.
    for text in (json.dumps(one.get(base).json()), one.get(base + '/rounds/1/log').text):
        for uid in (first['uid'], second['uid'], judge['uid']):
            assert uid not in text
    # Public chat is visible to room members but remains inaccessible to the human judge.
    chat=post(two,base+'/chat',{'match_number':1,'text':'玩家公共聊天消息'})
    assert chat['message']['sender_uid']==second['uid']
    assert one.get(base+'/chat?match_number=1').status_code==403
    one_me=one.get('/api/me').json()
    assert one.post(base+'/chat',json={'match_number':1,'text':'裁判越权消息'},
        headers={'X-CSRF-Token':one_me['user']['csrf']}).status_code==403
    visible=two.get(base+'/chat?match_number=1').json()['messages']
    assert len(visible)==1 and visible[0]['text']=='玩家公共聊天消息'
    judge_state=one.get(base).json()
    assert 'chat' not in judge_state and 'chat_messages' not in judge_state
    assert '玩家公共聊天消息' not in json.dumps(judge_state,ensure_ascii=False)
    # The retired suggestions write path is explicitly disabled.
    post(two,base+'/suggestions',{'text':'旧版追问入口'},410)

