import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import create_app, db_open, migrate_user_accounts
from test_room_hosts import env, finish_match, post, request
from test_uid_accounts import legacy_database, snapshot


def test_owner_or_admin_can_delete_rooms_and_never_reuse_room_ids(env):
    path, app, admin, host, player, outsider = env
    first = post(host, '/api/rooms', {'title': '房主删除'})
    base = f'/api/rooms/{first["room_id"]}'
    post(player, '/api/join', {'code': first['code']})
    for client in (host, admin):
        card = next(r for r in client.get('/api/me').json()['rooms'] if r['id'] == first['room_id'])
        assert card['can_manage'] and card['match_number'] == 1
    assert not player.get('/api/me').json()['rooms'][0]['can_manage']
    body = {'confirm': True, 'match_number': 1}
    for client in (player, outsider):
        request(client, 'DELETE', base, body, 403)
    request(TestClient(app), 'DELETE', base, body, 401)
    assert host.request('DELETE', base, json=body).status_code == 403
    request(host, 'DELETE', base, {'confirm': False, 'match_number': 1}, 422)
    request(host, 'DELETE', base, {'confirm': True, 'match_number': 2}, 409)
    with player.websocket_connect(f'/ws/rooms/{first["room_id"]}') as ws:
        ws.receive_json()
        request(host, 'DELETE', base, body)
        closed = ws.receive()
        assert closed['type'] == 'websocket.close'
        assert closed['reason'] == '房间已删除'
    assert host.get(base).status_code == 404
    post(player, '/api/join', {'code':first['code']}, 404)
    second = post(host, '/api/rooms', {'title':'不复用旧链接'})
    assert second['room_id'] > first['room_id']
    request(admin, 'DELETE', f'/api/rooms/{second["room_id"]}', body)
    third = post(player, '/api/rooms', {'title':'已删最高ID也不复用'})
    assert third['room_id'] > second['room_id']


def test_delete_finished_room_cascades_all_room_data_but_preserves_other_rooms_accounts(env):
    path, app, admin, host, player, judge = env
    created = post(host, '/api/rooms', {'title':'完整清理'})
    rid = created['room_id']
    base = f'/api/rooms/{rid}'
    finish_match(host, player, base, created['code'])
    other = post(player, '/api/rooms', {'title':'保留房间'})
    other_base=f'/api/rooms/{other["room_id"]}'
    post(player, other_base + '/chat', {'match_number':1,'text':'另一房间的聊天'})
    with db_open(path) as db:
        player_user=player.get('/api/me').json()['user']
        sender_id=int(player_user['uid'][2:])
        db.execute('''INSERT INTO chat_messages(room_id,match_number,sender_user_id,sender_uid,sender_name,text,created_at)
            VALUES(?,?,?,?,?,?,?)''',(rid,1,sender_id,player_user['uid'],'房间玩家','待级联清除','2026-11-24T00:00:00+00:00'))
        db.execute("INSERT INTO room_judges(room_id,user_id,label,created_at) VALUES(?,4,'真人裁判','2026')", (rid,))
        db.execute("INSERT INTO judge_questions(room_id,round_number,judge_user_id,text,created_at) VALUES(?,1,4,'问题','2026')", (rid,))
        db.execute("INSERT INTO judge_verdicts VALUES(?,4,'{}','2026')", (rid,))
        jid = db.execute("INSERT INTO ai_judges(room_id,label,created_at) VALUES(?,'AI裁判','2026')", (rid,)).lastrowid
        db.execute("INSERT INTO ai_judge_questions(room_id,round_number,ai_judge_id,text,created_at) VALUES(?,1,?,'问题','2026')", (rid,jid))
        db.execute("INSERT INTO ai_judge_verdicts VALUES(?,?,'{}','2026')", (rid,jid))
        users = [dict(r) for r in db.execute('SELECT * FROM users')]
    request(admin, 'DELETE', base, {'confirm':True,'match_number':1})
    assert player.get(other_base).status_code == 200
    assert player.get(other_base + '/chat?match_number=1').json()['messages'][0]['text'] == '另一房间的聊天'
    assert player.get('/api/me').json()['finished_rooms'] == []
    with db_open(path) as db:
        assert [dict(r) for r in db.execute('SELECT * FROM users')] == users
        assert not db.execute('SELECT 1 FROM answers').fetchone()
        for table in ('members','ai_players','rounds','suggestions','chat_messages','events','room_judges','judge_questions',
                      'judge_verdicts','ai_judges','ai_judge_questions','ai_judge_verdicts','ai_answer_revisions','packets'):
            assert not db.execute(f'SELECT 1 FROM {table} WHERE room_id=?', (rid,)).fetchone(), table
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_cleared_records_disappear_but_room_can_be_reused(env):
    _, _, admin, host, player, outsider = env
    created = post(host, '/api/rooms', {'title':'清空后列表移除'})
    base = f'/api/rooms/{created["room_id"]}'
    finish_match(host, player, base, created['code'])
    assert outsider.get('/api/me').json()['finished_rooms']
    request(outsider, 'DELETE', base + '/records', {'confirm':True,'match_number':1})
    assert outsider.get('/api/me').json()['finished_rooms'] == []
    assert host.get('/api/me').json()['rooms'][0]['id'] == created['room_id']
    post(host, base + '/restart', {'confirm':True,'match_number':1})
    request(host, 'DELETE', base, {'confirm':True,'match_number':1}, 409)


def make_v15(path):
    legacy_database(path)
    with db_open(path) as db:
        migrate_user_accounts(db)
        db.execute('ALTER TABLE users ADD COLUMN deleted_at TEXT')
        db.execute('ALTER TABLE rooms ADD COLUMN owner_id INTEGER REFERENCES users(id)')
        db.execute('UPDATE rooms SET owner_id=7')
        db.execute('ALTER TABLE rooms ADD COLUMN match_number INTEGER NOT NULL DEFAULT 1')
        db.execute('ALTER TABLE rooms ADD COLUMN records_deleted INTEGER NOT NULL DEFAULT 0')
        return snapshot(db)


def test_room_id_migration_preserves_every_row_and_backup(tmp_path):
    path = tmp_path / 'v15.sqlite3'
    before = make_v15(path)
    create_app(path)
    with db_open(path) as db:
        assert snapshot(db) == before
        assert 'AUTOINCREMENT' in db.execute("SELECT sql FROM sqlite_master WHERE name='rooms'").fetchone()[0]
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    backups = list(tmp_path.glob('v15.pre-v1_9-*.sqlite3'))
    assert len(backups) == 1
    with db_open(backups[0]) as db:
        assert snapshot(db) == before
    create_app(path)
    assert len(list(tmp_path.glob('v15.pre-v1_9-*.sqlite3'))) == 1


def test_room_id_migration_rolls_back_on_copy_denial(tmp_path, monkeypatch):
    import app as module
    path = tmp_path / 'rollback.sqlite3'
    before = make_v15(path)
    def denied(db_path):
        db = db_open(db_path)
        db.set_authorizer(lambda op, table, *args:
            sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_INSERT and table == 'rooms_id_upgrade' else sqlite3.SQLITE_OK)
        return db
    monkeypatch.setattr(module, 'db_open', denied)
    with pytest.raises(sqlite3.DatabaseError):
        create_app(path)
    with db_open(path) as db:
        assert snapshot(db) == before
        assert not db.execute('PRAGMA foreign_key_check').fetchall()

