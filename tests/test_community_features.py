from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import (
    create_app,
    db_open,
    freeze_match_personas,
    needs_roster_recovery,
    participant_roster_revision,
    seed_admin,
)


def request(client, method, path, body=None, status=200, match=None):
    me = client.get('/api/me')
    headers = {'X-CSRF-Token': me.json()['user']['csrf']} if me.status_code == 200 and method != 'GET' else {}
    if match is not None:
        headers['X-Match-Number'] = str(match)
    response = client.request(method, path, json=body, headers=headers)
    assert response.status_code == status, (path, response.status_code, response.text)
    return response.json() if response.content else {}


def post(client, path, body=None, status=200, match=None):
    return request(client, 'POST', path, body or {}, status, match)


def make_friends(first, second):
    uid = second.get('/api/me').json()['user']['uid']
    created = post(first, '/api/friends/requests', {'uid': uid})
    post(second, f"/api/friends/requests/{created['request_id']}/accept")
    return uid


@pytest.fixture
def env(tmp_path):
    path = tmp_path / 'community-features.sqlite3'
    seed_admin(path, 'community_admin', '社区主持人', 'admin-password')
    app = create_app(path)
    admin = TestClient(app)
    post(admin, '/api/login', {'username': 'community_admin', 'password': 'admin-password'})
    users = []
    for index in range(4):
        client = TestClient(app)
        result = post(client, '/api/register', {
            'username': f'community_player_{index}',
            'display_name': f'社区玩家{index}',
            'password': 'player-password',
        })
        users.append((client, result['uid']))
    return path, app, admin, users


def test_friend_room_invite_notifications_acceptance_and_idempotency(env):
    path, app, admin, users = env
    player, player_uid = users[0]
    make_friends(admin, player)
    room = post(admin, '/api/rooms', {'title': '好友邀请测试'})
    room_id = room['room_id']

    with player.websocket_connect('/ws/messages', headers={'origin': 'http://testserver'}) as socket:
        assert socket.receive_json()['type'] == 'connected'
        sent = post(admin, f'/api/rooms/{room_id}/invites', {
            'uids': ['TL99999999', player_uid], 'role': 'player',
        })
        event = socket.receive_json()
        assert event['type'] == 'room_invite_created'

    assert sent['results'][0]['status'] == 'skipped'
    assert sent['results'][1]['status'] == 'sent'
    invite_id = sent['results'][1]['invite_id']
    received = player.get('/api/me/room-invites').json()
    assert received['pending_count'] == 1
    assert received['received'][0]['id'] == invite_id
    assert received['received'][0]['role'] == 'player'
    assert received['received'][0]['inviter_name'] == '社区主持人'
    assert admin.get('/api/me/room-invites').json()['sent'][0]['id'] == invite_id

    repeated = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [player_uid], 'role': 'player',
    })
    assert repeated['results'][0]['invite_id'] == invite_id
    assert repeated['results'][0]['reused'] is True
    conflict = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [player_uid], 'role': 'judge',
    })
    assert conflict['results'][0]['status'] == 'skipped'
    assert '不同身份' in conflict['results'][0]['reason']

    accepted = post(player, f'/api/room-invites/{invite_id}/accept')
    assert accepted['ok'] is True
    assert accepted['role'] == 'player'
    assert accepted['alias']
    replay = post(player, f'/api/room-invites/{invite_id}/accept')
    assert replay['already_joined'] is True
    assert replay['alias'] == accepted['alias']
    assert player.get('/api/rooms/' + str(room_id)).json()['mine'] == accepted['alias']

    with db_open(path) as db:
        row = db.execute('SELECT status FROM room_invites WHERE id=?', (invite_id,)).fetchone()
        assert row['status'] == 'accepted'


def test_invite_role_conflict_is_checked_when_the_invitation_is_accepted(env):
    _, _, admin, users = env
    player, player_uid = users[0]
    make_friends(admin, player)
    room = post(admin, '/api/rooms', {'title': '邀请角色冲突'})
    post(player, '/api/join', {'code': room['code'], 'role': 'player'})

    invitation = post(admin, f"/api/rooms/{room['room_id']}/invites", {
        'uids': [player_uid], 'role': 'judge',
    })['results'][0]
    assert invitation['status'] == 'sent'
    assert post(player, f"/api/room-invites/{invitation['invite_id']}/accept", status=409) is not None


def test_invite_expiry_friend_removal_and_room_close_cancel_pending(env):
    path, _, admin, users = env
    first, first_uid = users[0]
    second, second_uid = users[1]
    make_friends(admin, first)
    make_friends(admin, second)
    room = post(admin, '/api/rooms', {'title': '邀请失效测试'})
    room_id = room['room_id']

    expired = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [first_uid], 'role': 'player',
    })['results'][0]['invite_id']
    with db_open(path) as db:
        db.execute(
            "UPDATE room_invites SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (expired,),
        )
    assert first.get('/api/me/room-invites').json()['received'] == []
    with db_open(path) as db:
        assert db.execute('SELECT status FROM room_invites WHERE id=?', (expired,)).fetchone()['status'] == 'expired'

    friend_removed = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [first_uid], 'role': 'player',
    })['results'][0]['invite_id']
    request(admin, 'DELETE', f'/api/friends/{first_uid}', {'confirm': True})
    with db_open(path) as db:
        assert db.execute('SELECT status FROM room_invites WHERE id=?', (friend_removed,)).fetchone()['status'] == 'cancelled'
    assert first.get('/api/me/room-invites').json()['received'] == []

    pending = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [second_uid], 'role': 'player',
    })['results'][0]['invite_id']
    post(admin, f'/api/rooms/{room_id}/leave', {'match_number': 1, 'confirm': True})
    with db_open(path) as db:
        assert db.execute('SELECT status FROM room_invites WHERE id=?', (pending,)).fetchone()['status'] == 'cancelled'
    assert second.get('/api/me/room-invites').json()['received'] == []


def test_room_start_cancels_outstanding_friend_invites(env):
    path, _, admin, users = env
    player, player_uid = users[0]
    invitee, invitee_uid = users[1]
    make_friends(admin, player)
    make_friends(admin, invitee)
    room = post(admin, '/api/rooms', {'title': '开赛撤销邀请'})
    room_id = room['room_id']
    pending = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [invitee_uid], 'role': 'player',
    })['results'][0]['invite_id']

    post(admin, '/api/join', {'code': room['code'], 'role': 'player'})
    post(player, '/api/join', {'code': room['code'], 'role': 'player'})
    post(admin, f'/api/rooms/{room_id}/question', {'text': '本轮正式问题'}, match=1)

    with db_open(path) as db:
        assert db.execute('SELECT status FROM room_invites WHERE id=?', (pending,)).fetchone()['status'] == 'cancelled'
    assert invitee.get('/api/me/room-invites').json()['received'] == []


def test_public_room_discovery_join_whitelist_and_auto_unlisting(env):
    _, _, admin, users = env
    player, _ = users[0]
    private = post(admin, '/api/rooms', {'title': '默认私有'})
    older_public = post(admin, '/api/rooms', {
        'title': '公开大厅分页旧房间', 'public_listing': True,
    })
    public = post(admin, '/api/rooms', {
        'title': '公开大厅测试',
        'public_listing': True,
        'public_description': '公开简介只允许展示房间信息。',
    })
    response = player.get('/api/public/rooms')
    assert response.status_code == 200
    rooms = response.json()['rooms']
    assert private['room_id'] not in {room['room_id'] for room in rooms}
    first_page = player.get('/api/public/rooms?limit=1').json()
    assert len(first_page['rooms']) == 1 and first_page['has_more'] is True
    cursor = first_page['next_before_id']
    second_page = player.get(f"/api/public/rooms?limit=1&before_id={cursor}").json()
    assert second_page['rooms'] and second_page['rooms'][0]['room_id'] != first_page['rooms'][0]['room_id']
    assert player.get('/api/public/rooms?limit=51').status_code == 422
    item = next(room for room in rooms if room['room_id'] == public['room_id'])
    assert item['public_description'] == '公开简介只允许展示房间信息。'
    empty_description = next(room for room in rooms if room['room_id'] == older_public['room_id'])
    assert empty_description['public_description'] == ''
    request(admin, 'PUT', f"/api/rooms/{private['room_id']}/visibility", {
        'public_listing': False, 'public_description': '',
    })
    assert item['max_players'] == 8
    assert item['max_judges'] == 12
    assert item['can_join_player'] is True
    assert item['can_join_judge'] is True
    assert set(item) == {
        'room_id', 'title', 'public_description', 'owner_display_name', 'created_at',
        'player_count', 'max_players', 'human_judge_count', 'ai_judge_count', 'max_judges', 'ai_player_count',
        'has_model_ai', 'round_duration_seconds', 'tags', 'can_join_player', 'can_join_judge',
    }
    assert 'invite_code' not in item and 'uid' not in str(item).lower() and 'api_key' not in str(item).lower()
    searched = player.get('/api/public/rooms?q=大厅').json()['rooms']
    assert public['room_id'] in {room['room_id'] for room in searched}
    assert private['room_id'] not in {room['room_id'] for room in searched}

    joined = post(player, f"/api/rooms/{public['room_id']}/join", {'role': 'player'})
    assert joined['role'] == 'player'
    assert joined['already_joined'] is False
    repeated = post(player, f"/api/rooms/{public['room_id']}/join", {'role': 'player'})
    assert repeated['already_joined'] is True
    post(player, f"/api/rooms/{public['room_id']}/join", {'role': 'judge'}, status=409)

    post(admin, '/api/join', {'code': public['code'], 'role': 'player'})
    post(admin, f"/api/rooms/{public['room_id']}/question", {'text': '首轮已开始'}, match=1)
    after_start = player.get('/api/public/rooms').json()['rooms']
    assert public['room_id'] not in {room['room_id'] for room in after_start}
    request(admin, 'PUT', f"/api/rooms/{public['room_id']}/visibility", {
        'public_listing': True, 'public_description': '不能在本场开赛后重新公开',
    }, status=409)

    # The prior public flag is retained; after restart the new draft match is discoverable again.
    post(admin, f"/api/rooms/{public['room_id']}/leave", {'match_number': 1, 'confirm': True})
    post(admin, f"/api/rooms/{public['room_id']}/restart", {'confirm': True, 'match_number': 1})
    restarted = player.get('/api/public/rooms').json()['rooms']
    assert public['room_id'] in {room['room_id'] for room in restarted}


def test_invite_code_join_keeps_legacy_frozen_roster_recovery_but_discovery_join_does_not(env):
    path, _, admin, users = env
    legacy_player, _ = users[0]
    discovery_player, _ = users[1]
    room = post(admin, '/api/rooms', {
        'title': '冻结名单兼容性',
        'public_listing': True,
    })
    with db_open(path) as db:
        row = db.execute('SELECT * FROM rooms WHERE id=?', (room['room_id'],)).fetchone()
        freeze_match_personas(
            db, row, source='test',
            participant_roster_revision=participant_roster_revision(db, room['room_id']),
        )

    legacy = post(legacy_player, '/api/join', {'code': room['code'], 'role': 'player'})
    assert legacy['alias']
    replay = post(legacy_player, '/api/join', {'code': room['code'], 'role': 'player'})
    assert replay['alias'] == legacy['alias']
    public_replay = post(legacy_player, f"/api/rooms/{room['room_id']}/join", {'role': 'player'})
    assert public_replay['already_joined'] is True
    post(discovery_player, f"/api/rooms/{room['room_id']}/join", {'role': 'player'}, status=409)
    listed = discovery_player.get('/api/public/rooms').json()['rooms']
    assert room['room_id'] not in {item['room_id'] for item in listed}



def test_discovery_join_cannot_use_roster_recovery_to_bypass_persona_freeze(env):
    path, _, admin, users = env
    existing_player, _ = users[0]
    deleted_player, deleted_uid = users[1]
    invitee, invitee_uid = users[2]
    public_player, _ = users[3]
    make_friends(admin, invitee)

    room = post(admin, '/api/rooms', {
        'title': '冻结名单恢复不能绕过发现入口',
        'public_listing': True,
    })
    post(existing_player, '/api/join', {'code': room['code'], 'role': 'player'})
    post(deleted_player, '/api/join', {'code': room['code'], 'role': 'player'})
    invitation = post(admin, f"/api/rooms/{room['room_id']}/invites", {
        'uids': [invitee_uid], 'role': 'player',
    })['results'][0]['invite_id']

    with db_open(path) as db:
        row = db.execute('SELECT * FROM rooms WHERE id=?', (room['room_id'],)).fetchone()
        freeze_match_personas(
            db, row, source='test',
            participant_roster_revision=participant_roster_revision(db, room['room_id']),
        )
        db.execute(
            'UPDATE users SET deleted_at=? WHERE id=?',
            ('2026-11-28T00:00:00+00:00', int(deleted_uid[2:])),
        )
        assert needs_roster_recovery(db, row) is True

    # An existing same-role seat remains idempotent even while the roster is frozen.
    existing = post(existing_player, f"/api/rooms/{room['room_id']}/join", {'role': 'player'})
    assert existing['already_joined'] is True

    accepted = post(invitee, f"/api/room-invites/{invitation}/accept", status=409)
    assert '冻结' in accepted['detail']
    direct = post(public_player, f"/api/rooms/{room['room_id']}/join", {'role': 'player'}, status=409)
    assert '冻结' in direct['detail']
    with db_open(path) as db:
        assert db.execute(
            'SELECT status FROM room_invites WHERE id=?', (invitation,),
        ).fetchone()['status'] == 'pending'


def test_discovery_judge_conflict_does_not_disclose_other_room_details(env):
    _, _, admin, users = env
    judge, judge_uid = users[0]
    make_friends(admin, judge)

    other_room = post(admin, '/api/rooms', {'title': '不应泄露的其他房间名称'})
    post(judge, '/api/join', {'code': other_room['code'], 'role': 'judge'})
    invited_room = post(admin, '/api/rooms', {'title': '好友邀请裁判目标'})
    invitation = post(admin, f"/api/rooms/{invited_room['room_id']}/invites", {
        'uids': [judge_uid], 'role': 'judge',
    })['results'][0]['invite_id']

    rejected = post(judge, f"/api/room-invites/{invitation}/accept", status=409)
    assert '其他未完赛房间' in rejected['detail']
    assert '不应泄露的其他房间名称' not in rejected['detail']
    assert f"ROOM {other_room['room_id']}" not in rejected['detail']


def test_expired_invite_can_be_resent_without_loading_inbox(env):
    path, _, admin, users = env
    player, player_uid = users[0]
    make_friends(admin, player)
    room = post(admin, '/api/rooms', {'title': '过期邀请直接重发'})
    room_id = room['room_id']
    first = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [player_uid], 'role': 'player',
    })['results'][0]['invite_id']
    with db_open(path) as db:
        db.execute(
            "UPDATE room_invites SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (first,),
        )

    # Do not call GET /api/me/room-invites: sending must expire the old row itself.
    second_result = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [player_uid], 'role': 'player',
    })['results'][0]
    second = second_result['invite_id']
    assert second_result['status'] == 'sent'
    assert second != first
    with db_open(path) as db:
        statuses = {
            row['id']: row['status']
            for row in db.execute('SELECT id,status FROM room_invites WHERE room_id=?', (room_id,))
        }
    assert statuses == {first: 'expired', second: 'pending'}

    post(admin, f'/api/room-invites/{second}/cancel')
    post(admin, f'/api/room-invites/{second}/cancel')
    assert player.get('/api/me/room-invites').json()['received'] == []
    with db_open(path) as db:
        assert db.execute('SELECT status FROM room_invites WHERE id=?', (second,)).fetchone()['status'] == 'cancelled'


def test_concurrent_invite_acceptance_creates_one_seat_and_is_idempotent(env):
    path, app, admin, users = env
    player, player_uid = users[0]
    make_friends(admin, player)
    room = post(admin, '/api/rooms', {'title': '并发接受邀请'})
    room_id = room['room_id']
    invite_id = post(admin, f'/api/rooms/{room_id}/invites', {
        'uids': [player_uid], 'role': 'player',
    })['results'][0]['invite_id']

    second_session = TestClient(app)
    post(second_session, '/api/login', {
        'uid': player_uid, 'password': 'player-password',
    })
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post, player, f'/api/room-invites/{invite_id}/accept')
        second = pool.submit(post, second_session, f'/api/room-invites/{invite_id}/accept')
        results = [first.result(), second.result()]

    assert all(result['ok'] for result in results)
    assert sorted(bool(result.get('already_joined')) for result in results) == [False, True]
    with db_open(path) as db:
        assert db.execute(
            'SELECT COUNT(*) FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL',
            (room_id, int(player_uid[2:])),
        ).fetchone()[0] == 1
        assert db.execute(
            'SELECT status FROM room_invites WHERE id=?', (invite_id,),
        ).fetchone()['status'] == 'accepted'

def test_room_deletion_notifies_invited_accounts_and_cascades_pending_invites(env):
    path, _, admin, users = env
    invitee, invitee_uid = users[0]
    make_friends(admin, invitee)
    room = post(admin, '/api/rooms', {'title': '删除房间时撤销邀请'})
    invite_id = post(admin, f"/api/rooms/{room['room_id']}/invites", {
        'uids': [invitee_uid], 'role': 'player',
    })['results'][0]['invite_id']

    with invitee.websocket_connect('/ws/messages', headers={'origin': 'http://testserver'}) as socket:
        assert socket.receive_json()['type'] == 'connected'
        request(admin, 'DELETE', f"/api/rooms/{room['room_id']}", {
            'match_number': 1, 'confirm': True,
        })
        event = socket.receive_json()
        assert event['type'] == 'room_invite_updated'
        assert event['invite_id'] == invite_id

    assert invitee.get('/api/me/room-invites').json()['received'] == []
    with db_open(path) as db:
        assert db.execute('SELECT 1 FROM room_invites WHERE id=?', (invite_id,)).fetchone() is None


def test_question_bank_is_read_only_bounded_and_versioned(env):
    _, _, admin, users = env
    player, _ = users[0]
    assert TestClient(admin.app).get('/api/question-bank').status_code == 401

    data = player.get('/api/question-bank').json()
    assert data['version']
    assert len(data['questions']) == 24
    assert len({item['id'] for item in data['questions']}) == 24
    counts = Counter(item['category'] for item in data['questions'])
    assert counts == {'logic': 6, 'language': 6, 'experience': 6, 'followup': 6}

    logic = player.get('/api/question-bank?category=logic&q=专家').json()['questions']
    assert [item['id'] for item in logic] == ['logic-004']
    assert len(player.get('/api/question-bank?limit=2').json()['questions']) == 2
    assert all(len(item['text']) <= 3000 for item in data['questions'])
    assert player.get('/api/question-bank?category=unknown').status_code == 422
    assert player.post('/api/question-bank', json={}).status_code == 405


def test_invite_inbox_is_bounded_and_reports_full_pending_count(env):
    path, _, admin, users = env
    invitee, invitee_uid = users[0]
    make_friends(admin, invitee)
    inviter_id = int(admin.get('/api/me').json()['user']['uid'][2:])
    invitee_id = int(invitee_uid[2:])
    created_at = '2026-11-28T00:00:00+00:00'
    expires_at = '2999-01-01T00:00:00+00:00'

    with db_open(path) as db:
        for index in range(101):
            cursor = db.execute(
                'INSERT INTO rooms(title,code,judge,created_at,owner_id) VALUES(?,?,?,?,?)',
                (f'邀请分页房间 {index}', f'INBOX{index:05d}', '裁判', created_at, inviter_id),
            )
            db.execute(
                '''INSERT INTO room_invites(
                    room_id,inviter_user_id,invitee_user_id,role,status,created_at,expires_at,updated_at
                ) VALUES(?,?,?,'player','pending',?,?,?)''',
                (cursor.lastrowid, inviter_id, invitee_id, created_at, expires_at, created_at),
            )

    received = invitee.get('/api/me/room-invites').json()
    assert len(received['received']) == 100
    assert received['pending_count'] == 101
    assert received['truncated'] is True

    sent = admin.get('/api/me/room-invites').json()
    assert len(sent['sent']) == 100
    assert sent['truncated'] is True


def test_accepting_expired_invite_notifies_both_accounts(env):
    path, _, admin, users = env
    invitee, invitee_uid = users[0]
    make_friends(admin, invitee)
    room = post(admin, '/api/rooms', {'title': '接受时过期通知'})
    invite_id = post(admin, f"/api/rooms/{room['room_id']}/invites", {
        'uids': [invitee_uid], 'role': 'player',
    })['results'][0]['invite_id']
    with db_open(path) as db:
        db.execute(
            "UPDATE room_invites SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (invite_id,),
        )

    with admin.websocket_connect('/ws/messages', headers={'origin': 'http://testserver'}) as inviter_socket, \
         invitee.websocket_connect('/ws/messages', headers={'origin': 'http://testserver'}) as invitee_socket:
        assert inviter_socket.receive_json()['type'] == 'connected'
        assert invitee_socket.receive_json()['type'] == 'connected'
        result = post(invitee, f'/api/room-invites/{invite_id}/accept', status=409)
        assert '过期' in result['detail']
        inviter_event = inviter_socket.receive_json()
        invitee_event = invitee_socket.receive_json()
        assert inviter_event == {'type': 'room_invite_updated', 'invite_id': invite_id}
        assert invitee_event == {'type': 'room_invite_updated', 'invite_id': invite_id}

    assert invitee.get('/api/me/room-invites').json()['received'] == []
    with db_open(path) as db:
        assert db.execute(
            'SELECT status FROM room_invites WHERE id=?', (invite_id,),
        ).fetchone()['status'] == 'expired'


def test_account_soft_deletion_cancels_pending_invites_and_notifies_inviter(env):
    path, _, admin, users = env
    invitee, invitee_uid = users[0]
    make_friends(admin, invitee)
    room = post(admin, '/api/rooms', {'title': '账号删除撤销邀请'})
    invite_id = post(admin, f"/api/rooms/{room['room_id']}/invites", {
        'uids': [invitee_uid], 'role': 'player',
    })['results'][0]['invite_id']

    with admin.websocket_connect('/ws/messages', headers={'origin': 'http://testserver'}) as socket:
        assert socket.receive_json()['type'] == 'connected'
        post(admin, f'/api/admin/users/{invitee_uid}', {'confirm': True})
        event = socket.receive_json()
        assert event['type'] == 'social_updated'

    with db_open(path) as db:
        assert db.execute(
            'SELECT status FROM room_invites WHERE id=?', (invite_id,),
        ).fetchone()['status'] == 'cancelled'
    assert admin.get('/api/me/room-invites').json()['sent'] == []
