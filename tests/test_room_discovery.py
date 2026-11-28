import pytest

from app import db_open
from test_social_points import fixture_clients, make_friends, post, request


@pytest.fixture
def lab(tmp_path):
    path, app, admin, host, player, other, judge = fixture_clients(tmp_path)
    return path, app, admin, host, player, other, judge


def create_room(host, title="Community room", **extra):
    return post(host, "/api/rooms", {"title": title, **extra})


def test_friend_invite_batch_deduplicates_and_accept_is_idempotent(lab):
    path, _, _, host, player, other, _ = lab
    host_uid, player_uid = make_friends(host, player)
    room = create_room(host)
    base = f"/api/rooms/{room['room_id']}"

    sent = post(host, base + "/invites", {
        "uids": [player_uid, host_uid, "TL99999999"],
        "role": "player",
    })
    assert sent["created"] == 1
    assert sent["skipped"] == 2

    repeated = post(host, base + "/invites", {"uids": [player_uid], "role": "player"})
    assert repeated["created"] == 0
    assert repeated["existing"] == 1

    conflicting = post(host, base + "/invites", {"uids": [player_uid], "role": "judge"})
    assert conflicting["skipped"] == 1

    inbox = player.get("/api/me/room-invites").json()
    assert len(inbox["incoming"]) == 1
    invite = inbox["incoming"][0]
    assert invite["room_title"] == "Community room"
    assert invite["inviter_uid"] == host_uid
    assert "code" not in invite and "invite_code" not in invite

    accepted = post(player, f"/api/room-invites/{invite['id']}/accept")
    assert accepted["room_id"] == room["room_id"]
    assert accepted["role"] == "player"
    assert accepted["alias"]

    replay = post(player, f"/api/room-invites/{invite['id']}/accept")
    assert replay["already_joined"] is True
    assert replay["alias"] == accepted["alias"]
    with db_open(path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL",
            (room["room_id"], player.get("/api/me").json()["user"]["id"]),
        ).fetchone()[0] == 1
        assert db.execute("SELECT status FROM room_invites WHERE id=?", (invite["id"],)).fetchone()["status"] == "accepted"


def test_judge_invite_uses_existing_judge_registration_rules(lab):
    _, _, _, host, _, _, judge = lab
    _, judge_uid = make_friends(host, judge)
    room = create_room(host, "Judge invite")
    base = f"/api/rooms/{room['room_id']}"

    sent = post(host, base + "/invites", {"uids": [judge_uid], "role": "judge"})
    invite_id = sent["results"][0]["invite_id"]
    accepted = post(judge, f"/api/room-invites/{invite_id}/accept")
    assert accepted["role"] == "judge"
    assert judge.get(base).json()["is_judge"] is True
    replay = post(judge, f"/api/room-invites/{invite_id}/accept")
    assert replay["already_joined"] is True


def test_judge_invite_rejects_a_judge_already_active_elsewhere(lab):
    _, _, _, host, _, _, judge = lab
    _, judge_uid = make_friends(host, judge)
    first = create_room(host, "First judge room")
    second = create_room(host, "Second judge room")
    post(judge, "/api/join", {"code": first["code"], "role": "judge"})
    invite = post(host, f"/api/rooms/{second['room_id']}/invites", {
        "uids": [judge_uid], "role": "judge",
    })["results"][0]
    post(judge, f"/api/room-invites/{invite['invite_id']}/accept", status=409)
    inbox = judge.get("/api/me/room-invites").json()
    assert any(item["id"] == invite["invite_id"] for item in inbox["incoming"])


def test_expired_invitation_is_persisted_and_can_be_reissued(lab):
    path, _, _, host, player, _, _ = lab
    _, player_uid = make_friends(host, player)
    room = create_room(host, "Expiry")
    invite = post(host, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [player_uid], "role": "player",
    })["results"][0]
    with db_open(path) as db:
        db.execute("UPDATE room_invites SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                   (invite["invite_id"],))
    post(player, f"/api/room-invites/{invite['invite_id']}/accept", status=410)
    with db_open(path) as db:
        assert db.execute("SELECT status FROM room_invites WHERE id=?",
                          (invite["invite_id"],)).fetchone()["status"] == "expired"
    reissued = post(host, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [player_uid], "role": "player",
    })
    assert reissued["created"] == 1


def test_friend_deletion_cancels_pending_invites(lab):
    path, _, _, host, player, _, _ = lab
    host_uid, player_uid = make_friends(host, player)
    room = create_room(host, "Friend removal")
    invite = post(host, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [player_uid], "role": "player",
    })["results"][0]
    request(host, "DELETE", f"/api/friends/{player_uid}", {"confirm": True})
    with db_open(path) as db:
        assert db.execute("SELECT status FROM room_invites WHERE id=?",
                          (invite["invite_id"],)).fetchone()["status"] == "cancelled"
    assert player.get("/api/me/room-invites").json()["incoming"] == []


def test_room_start_and_owner_close_cancel_pending_invites(lab):
    path, _, _, host, player, other, _ = lab
    _, other_uid = make_friends(host, other)

    started_room = create_room(host, "Starts")
    start_base = f"/api/rooms/{started_room['room_id']}"
    post(host, "/api/join", {"code": started_room["code"]})
    post(player, "/api/join", {"code": started_room["code"]})
    start_invite = post(host, start_base + "/invites", {
        "uids": [other_uid], "role": "player",
    })["results"][0]
    post(host, start_base + "/question", {"text": "Start and cancel invites"}, match=1)
    with db_open(path) as db:
        assert db.execute("SELECT status FROM room_invites WHERE id=?",
                          (start_invite["invite_id"],)).fetchone()["status"] == "cancelled"

    closed_room = create_room(host, "Closes")
    close_invite = post(host, f"/api/rooms/{closed_room['room_id']}/invites", {
        "uids": [other_uid], "role": "player",
    })["results"][0]
    post(host, f"/api/rooms/{closed_room['room_id']}/leave", {
        "match_number": 1, "confirm": True,
    })
    with db_open(path) as db:
        assert db.execute("SELECT status FROM room_invites WHERE id=?",
                          (close_invite["invite_id"],)).fetchone()["status"] == "cancelled"


def test_public_lobby_lists_only_joinable_rooms_and_joins_by_role(lab):
    _, _, _, host, player, other, judge = lab
    private_room = create_room(host, "Private")
    public_room = create_room(host, "Public", public_listing=True, public_description="公开测试说明")
    response = player.get("/api/public/rooms")
    assert response.status_code == 200
    items = response.json()["rooms"]
    public = next(item for item in items if item["room_id"] == public_room["room_id"])
    assert all(item["room_id"] != private_room["room_id"] for item in items)
    assert public["public_description"] == "公开测试说明"
    assert public["can_join_player"] is True
    assert public["can_join_judge"] is True
    assert public["max_players"] == 8
    assert "code" not in public and "invite_code" not in public and "owner_uid" not in public

    joined = post(player, f"/api/rooms/{public_room['room_id']}/join", {"role": "player"})
    assert joined["role"] == "player"
    post(player, f"/api/rooms/{public_room['room_id']}/join", {"role": "judge"}, status=409)

    judge_join = post(judge, f"/api/rooms/{public_room['room_id']}/join", {"role": "judge"})
    assert judge_join["role"] == "judge"
    judge_replay = post(judge, f"/api/rooms/{public_room['room_id']}/join", {"role": "judge"})
    assert judge_replay["already_joined"] is True

    outsider = other.get("/api/public/rooms").json()["rooms"]
    assert any(item["room_id"] == public_room["room_id"] for item in outsider)


def test_public_visibility_can_be_changed_before_start_but_not_after(lab):
    _, _, _, host, player, _, _ = lab
    room = create_room(host, "Visibility")
    base = f"/api/rooms/{room['room_id']}"
    saved = request(host, "PUT", base + "/visibility", {
        "public_listing": True, "public_description": "可在大厅找到",
    })
    assert saved["public_listing"] is True
    assert any(item["room_id"] == room["room_id"] for item in player.get("/api/public/rooms").json()["rooms"])

    post(host, "/api/join", {"code": room["code"]})
    post(player, "/api/join", {"code": room["code"]})
    post(host, base + "/question", {"text": "首轮开始"}, match=1)
    request(host, "PUT", base + "/visibility", {
        "public_listing": False, "public_description": "",
    }, status=409)
    assert all(item["room_id"] != room["room_id"] for item in player.get("/api/public/rooms").json()["rooms"])


def test_public_room_disappears_when_first_judge_question_is_submitted(lab):
    _, _, _, host, player, other, judge = lab
    room = create_room(host, "Question submitted", public_listing=True)
    base = f"/api/rooms/{room['room_id']}"
    post(judge, "/api/join", {"code": room["code"], "role": "judge"})
    post(judge, base + "/judge-questions", {"text": "已提交的裁判问题"}, match=1)
    items = other.get("/api/public/rooms").json()["rooms"]
    assert all(item["room_id"] != room["room_id"] for item in items)


def test_only_room_manager_can_send_invites(lab):
    _, _, _, host, player, other, _ = lab
    room = create_room(host, "Permission")
    post(player, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [other.get("/api/me").json()["user"]["uid"]], "role": "player",
    }, status=403)
