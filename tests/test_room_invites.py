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
    reissued = post(host, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [player_uid], "role": "player",
    })
    assert reissued["created"] == 1
    with db_open(path) as db:
        statuses = [row["status"] for row in db.execute(
            "SELECT status FROM room_invites WHERE room_id=? AND invitee_user_id=? ORDER BY id",
            (room["room_id"], player.get("/api/me").json()["user"]["id"]),
        )]
    assert statuses == ["expired", "pending"]


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





def test_only_room_manager_can_send_invites(lab):
    _, _, _, host, player, other, _ = lab
    room = create_room(host, "Permission")
    post(player, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [other.get("/api/me").json()["user"]["uid"]], "role": "player",
    }, status=403)


def test_concurrent_duplicate_invite_creation_and_acceptance_are_idempotent(lab):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient

    path, app, _, host, player, _, _ = lab
    _, player_uid = make_friends(host, player)
    host_uid = host.get("/api/me").json()["user"]["uid"]
    room = create_room(host, "Concurrent invite")
    route = f"/api/rooms/{room['room_id']}/invites"
    second_host = TestClient(app)
    post(second_host, "/api/login", {"uid": host_uid, "password": "player-password"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda client: post(client, route, {"uids": [player_uid], "role": "player"}),
            (host, second_host),
        ))
    assert sum(result["created"] for result in results) == 1
    assert sum(result["existing"] for result in results) == 1
    invite_id = results[0]["results"][0]["invite_id"]

    second_player = TestClient(app)
    post(second_player, "/api/login", {"uid": player_uid, "password": "player-password"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        accepted = list(pool.map(
            lambda client: post(client, f"/api/room-invites/{invite_id}/accept"),
            (player, second_player),
        ))
    assert all(item["room_id"] == room["room_id"] for item in accepted)
    assert sorted(item.get("already_joined", False) for item in accepted) == [False, True]
    with db_open(path) as db:
        assert db.execute("SELECT COUNT(*) FROM room_invites WHERE id=?", (invite_id,)).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL",
            (room["room_id"], int(player_uid[2:])),
        ).fetchone()[0] == 1


def test_reject_and_sender_cancel_have_distinct_terminal_states(lab):
    path, _, _, host, player, _, _ = lab
    _, player_uid = make_friends(host, player)
    room = create_room(host, "Reject and cancel")

    rejected = post(host, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [player_uid], "role": "player",
    })["results"][0]
    post(player, f"/api/room-invites/{rejected['invite_id']}/reject")
    with db_open(path) as db:
        assert db.execute("SELECT status FROM room_invites WHERE id=?",
                          (rejected["invite_id"],)).fetchone()["status"] == "rejected"

    cancelled = post(host, f"/api/rooms/{room['room_id']}/invites", {
        "uids": [player_uid], "role": "player",
    })["results"][0]
    post(host, f"/api/room-invites/{cancelled['invite_id']}/cancel")
    with db_open(path) as db:
        assert db.execute("SELECT status FROM room_invites WHERE id=?",
                          (cancelled["invite_id"],)).fetchone()["status"] == "cancelled"
