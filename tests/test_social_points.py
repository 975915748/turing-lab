"""HTTP, WebSocket and transaction regression tests for social features and points."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import create_app, db_open, seed_admin


def request(client, method, route, body=None, status=200, match=None):
    me = client.get("/api/me")
    headers = {"X-CSRF-Token": me.json()["user"]["csrf"]} if me.status_code == 200 else {}
    if match is not None:
        headers["X-Match-Number"] = str(match)
    response = client.request(method, route, json=body, headers=headers)
    assert response.status_code == status, (route, response.status_code, response.text)
    return response.json() if response.content else {}


def post(client, route, body=None, status=200, match=None):
    return request(client, "POST", route, body or {}, status, match)


def fixture_clients(tmp_path):
    path = tmp_path / "social-points.sqlite3"
    seed_admin(path, "admin", "Admin", "admin-password")
    app = create_app(path)
    admin = TestClient(app)
    post(admin, "/api/login", {"username": "admin", "password": "admin-password"})
    users = [TestClient(app) for _ in range(4)]
    for index, client in enumerate(users):
        post(client, "/api/register", {
            "username": f"玩家{index}",
            "password": "player-password",
        })
    return path, app, admin, *users


@pytest.fixture
def env(tmp_path):
    return fixture_clients(tmp_path)


def make_friends(first, second):
    first_uid = first.get("/api/me").json()["user"]["uid"]
    second_uid = second.get("/api/me").json()["user"]["uid"]
    request_row = post(first, "/api/friends/requests", {"uid": second_uid})
    post(second, f"/api/friends/requests/{request_row['request_id']}/accept")
    return first_uid, second_uid


def start_legacy_match(host, player, base, code, *, ai_name=None, match_number=1):
    host_alias = post(host, "/api/join", {"code": code})["alias"]
    player_alias = post(player, "/api/join", {"code": code})["alias"]
    predictions = {host_alias: "AI", player_alias: "Human"}
    ai_alias = None
    if ai_name:
        ai_alias = post(host, base + "/ai", {"name": ai_name})["alias"]
        predictions[ai_alias] = "AI"
    post(host, base + "/question", {"text": "请回答这一轮问题"}, match=match_number)
    post(host, base + "/answers", {"text": "房主回答"}, match=match_number)
    post(player, base + "/answers", {"text": "玩家回答"}, match=match_number)
    if ai_alias:
        post(host, f"{base}/ai/{ai_alias}/answer", {"text": "AI 原始回答"}, match=match_number)
    return host_alias, player_alias, ai_alias, predictions


def test_friend_requests_private_notes_messages_unread_pagination_and_delete(env):
    _, _, admin, first, second, third, _ = env
    first_uid = first.get("/api/me").json()["user"]["uid"]
    second_uid = second.get("/api/me").json()["user"]["uid"]

    assert first.get(f"/api/users/search?uid={first_uid}").status_code == 409
    assert first.get("/api/users/search?uid=TL99999999").status_code == 404
    found = first.get(f"/api/users/search?uid={second_uid.lower()}").json()
    assert found["uid"] == second_uid
    assert found["status"] == "none"
    assert set(found) == {"uid", "display_name", "status", "request_id", "total_points", "current_streak"}

    third_uid = third.get("/api/me").json()["user"]["uid"]
    withdrawn = post(first, "/api/friends/requests", {"uid": third_uid})
    post(first, f"/api/friends/requests/{withdrawn['request_id']}/withdraw")
    assert third.get("/api/me/social").json()["incoming"] == []
    assert first.get("/api/me/social").json()["outgoing"] == []

    first_request = post(first, "/api/friends/requests", {"uid": second_uid})
    assert second.get("/api/me/social").json()["incoming"][0]["id"] == first_request["request_id"]
    post(second, "/api/friends/requests", {"uid": first_uid}, status=409)
    post(second, f"/api/friends/requests/{first_request['request_id']}/reject")
    assert first.get("/api/me/social").json()["outgoing"] == []

    second_request = post(first, "/api/friends/requests", {"uid": second_uid})
    post(second, f"/api/friends/requests/{second_request['request_id']}/accept")
    assert post(second, f"/api/friends/requests/{second_request['request_id']}/accept")["already_friends"] is True

    first_social = first.get("/api/me/social").json()
    second_social = second.get("/api/me/social").json()
    assert first_social["friends"][0]["note"] == "玩家1"
    assert second_social["friends"][0]["note"] == "玩家0"

    request(first, "PUT", f"/api/friends/{second_uid}/note", {"note": "只属于我的备注"})
    assert first.get("/api/me/social").json()["friends"][0]["note"] == "只属于我的备注"
    assert second.get("/api/me/social").json()["friends"][0]["note"] == "玩家0"

    request(first, "PUT", "/api/me/profile", {"display_name": "改名后的玩家0"})
    second_friend = second.get("/api/me/social").json()["friends"][0]
    assert second_friend["display_name"] == "改名后的玩家0"
    assert second_friend["note"] == "玩家0"
    request(second, "PUT", f"/api/friends/{first_uid}/note", {"reset_default": True})
    assert second.get("/api/me/social").json()["friends"][0]["note"] == "改名后的玩家0"

    messages = []
    for index in range(3):
        result = post(first, f"/api/messages/{second_uid}", {
            "text": f"<b>私信 {index}</b>",
            "client_message_id": f"stable-message-{index}",
        })
        assert result["message"]["mine"] is True
        messages.append(result["message"])
    duplicate = post(first, f"/api/messages/{second_uid}", {
        "text": "<b>私信 0</b>",
        "client_message_id": "stable-message-0",
    })
    assert duplicate["duplicate"] is True
    assert duplicate["message"]["id"] == messages[0]["id"]

    received = second.get(f"/api/messages/{first_uid}?limit=2").json()
    assert received["can_send"] is True
    assert received["last_received_message_id"] == messages[-1]["id"]
    assert [message["id"] for message in received["messages"]] == [
        messages[1]["id"], messages[2]["id"]
    ]
    assert all(message["mine"] is False for message in received["messages"])
    assert received["has_more"] is True
    older = second.get(
        f"/api/messages/{first_uid}?limit=2&before_id={received['next_before_id']}"
    ).json()
    assert [message["id"] for message in older["messages"]] == [messages[0]["id"]]

    conversation = second.get("/api/me/social").json()["conversations"][0]
    assert conversation["unread_count"] == 3
    read = post(second, f"/api/messages/{first_uid}/read", {
        "last_read_message_id": received["last_received_message_id"]
    })
    assert read["unread_count"] == 0
    assert second.get("/api/me/social").json()["conversations"][0]["unread_count"] == 0

    # Administrators do not get a private-message back door.
    assert admin.get(f"/api/messages/{second_uid}").status_code == 404

    request(first, "DELETE", f"/api/friends/{second_uid}", {"confirm": True})
    history = first.get(f"/api/messages/{second_uid}").json()
    assert history["can_send"] is False
    assert len(history["messages"]) == 3
    post(first, f"/api/messages/{second_uid}", {
        "text": "删除好友后不能发送",
        "client_message_id": "after-friend-delete",
    }, status=403)
    conversation_id = history["conversation_id"]
    re_request = post(first, "/api/friends/requests", {"uid": second_uid})
    post(second, f"/api/friends/requests/{re_request['request_id']}/accept")
    reopened = first.get(f"/api/messages/{second_uid}").json()
    assert reopened["can_send"] is True
    assert reopened["conversation_id"] == conversation_id
    assert len(reopened["messages"]) == 3


def test_account_websocket_uses_viewer_specific_message_flags_and_logout_is_session_scoped(env):
    _, app, _, first, second, _, _ = env
    first_uid, second_uid = make_friends(first, second)
    second_session = TestClient(app)
    post(second_session, "/api/login", {"uid": first_uid, "password": "player-password"})

    with first.websocket_connect("/ws/messages") as first_ws:
        with second_session.websocket_connect("/ws/messages") as second_ws:
            with second.websocket_connect("/ws/messages") as recipient_ws:
                assert first_ws.receive_json()["type"] == "connected"
                assert second_ws.receive_json()["type"] == "connected"
                assert recipient_ws.receive_json()["type"] == "connected"

                sent = post(first, f"/api/messages/{second_uid}", {
                    "text": "在线私信",
                    "client_message_id": "socket-message-1",
                })
                event_first = first_ws.receive_json()
                event_second = second_ws.receive_json()
                event_recipient = recipient_ws.receive_json()
                assert event_first["message"]["mine"] is True
                assert event_second["message"]["mine"] is True
                assert event_recipient["message"]["mine"] is False
                assert event_first["message"]["id"] == sent["message"]["id"]

                post(first, "/api/logout")
                with pytest.raises(WebSocketDisconnect):
                    first_ws.receive_json()

                # Logging out one browser session must not revoke the other session.
                post(second, f"/api/messages/{first_uid}", {
                    "text": "另一个会话仍在线",
                    "client_message_id": "socket-message-2",
                })
                event_second = second_ws.receive_json()
                event_recipient = recipient_ws.receive_json()
                assert event_second["message"]["mine"] is False
                assert event_recipient["message"]["mine"] is True


def test_deleted_account_notifies_friend_and_leaves_read_only_history(env):
    _, _, admin, first, second, _, _ = env
    first_uid, second_uid = make_friends(first, second)
    post(first, f"/api/messages/{second_uid}", {
        "text": "保留的历史消息",
        "client_message_id": "before-delete",
    })

    with second.websocket_connect("/ws/messages") as socket:
        assert socket.receive_json()["type"] == "connected"
        request(admin, "DELETE", f"/api/admin/users/{first_uid}", {"confirm": True})
        update = socket.receive_json()
        assert update["type"] == "social_updated"

    assert first.get("/api/me").status_code == 401
    social = second.get("/api/me/social").json()
    assert social["friends"] == []
    history = second.get(f"/api/messages/{first_uid}").json()
    assert history["other_name"] == "账号已删除"
    assert history["can_send"] is False
    assert history["messages"][0]["text"] == "保留的历史消息"
    post(second, f"/api/messages/{first_uid}", {
        "text": "已删除账号不可收信",
        "client_message_id": "after-delete",
    }, status=403)
    assert admin.get(f"/api/messages/{second_uid}").status_code == 404


def test_match_points_settle_once_and_survive_clear_restart_and_room_delete(env):
    path, _, admin, host, player, _, _ = env
    created = post(host, "/api/rooms", {"title": "积分场次复用"})
    base = f"/api/rooms/{created['room_id']}"

    host_alias, player_alias, ai_alias, predictions = start_legacy_match(
        host, player, base, created["code"], ai_name="AI 席位"
    )
    post(host, base + "/verdict", {
        "predictions": predictions,
        "evidence": "一位裁判的正式判断",
        "confidence": 80,
    }, match=1)
    post(host, base + "/reveal", match=1)

    host_social = host.get("/api/me/social").json()["points"]
    player_social = player.get("/api/me/social").json()["points"]
    assert host_social == {"total_points": "10", "current_streak": "1"}
    assert player_social == {"total_points": "-10", "current_streak": "0"}
    host_result = host.get(base).json()["my_points_result"]
    player_result = player.get(base).json()["my_points_result"]
    assert host_result["outcome"] == "win" and host_result["delta"] == "10"
    assert player_result["outcome"] == "loss" and player_result["delta"] == "-10"
    assert host.get("/api/me/points/ledger").json()["entries"][0]["match_number"] == 1
    post(host, base + "/reveal", {}, status=409, match=1)

    request(host, "DELETE", base + "/records", {"match_number": 1, "confirm": True})
    assert host.get("/api/me/social").json()["points"]["total_points"] == "10"
    with db_open(path) as db:
        assert db.execute("SELECT COUNT(*) FROM match_settlements").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 2

    post(host, base + "/restart", {"match_number": 1, "confirm": True})
    assert host.get(base).json()["match_number"] == 2
    host_alias, player_alias, ai_alias, predictions = start_legacy_match(
        host, player, base, created["code"], ai_name=None, match_number=2
    )
    # The AI seat is retained by the room and needs its own answer in match two.
    ai_alias = host.get(base).json()["ais"][0]["alias"]
    predictions[ai_alias] = "AI"
    post(host, f"{base}/ai/{ai_alias}/answer", {"text": "第二场 AI 回答"}, match=2)
    post(host, base + "/verdict", {
        "predictions": predictions,
        "evidence": "第二场正式判断",
        "confidence": 75,
    }, match=2)
    post(host, base + "/reveal", match=2)

    assert host.get("/api/me/social").json()["points"] == {
        "total_points": "22", "current_streak": "2"
    }
    assert player.get("/api/me/social").json()["points"] == {
        "total_points": "-20", "current_streak": "0"
    }
    ledger = host.get("/api/me/points/ledger").json()["entries"]
    assert [entry["match_number"] for entry in ledger] == [2, 1]
    request(host, "DELETE", base, {"match_number": 2, "confirm": True})
    assert host.get("/api/me/social").json()["points"]["total_points"] == "22"
    with db_open(path) as db:
        assert db.execute("SELECT COUNT(*) FROM match_settlements").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 4


def test_mixed_human_ai_judges_tie_is_loss_and_removed_player_is_not_settled(env):
    path, _, admin, host, player, removed, judge = env
    created = post(host, "/api/rooms", {"title": "混合裁判与退赛"})
    base = f"/api/rooms/{created['room_id']}"
    host_alias = post(host, "/api/join", {"code": created["code"]})["alias"]
    player_alias = post(player, "/api/join", {"code": created["code"]})["alias"]
    post(removed, "/api/join", {"code": created["code"]})
    post(judge, "/api/join", {"code": created["code"], "role": "judge"})
    ai_judge = post(host, base + "/ai-judges", {"name": "手动 AI 裁判"})["id"]

    post(judge, base + "/judge-questions", {"text": "真人裁判问题"}, match=1)
    post(host, f"{base}/ai-judges/{ai_judge}/questions", {"text": "AI 裁判问题"}, match=1)
    post(host, base + "/start", match=1)

    removed_id = removed.get("/api/me").json()["user"]["id"]
    with db_open(path) as db:
        db.execute(
            "INSERT INTO account_points(user_id,points,streak,updated_at) VALUES(?,?,?,?)",
            (removed_id, "36", "3", "2026-11-26T00:00:00+00:00"),
        )
    post(host, f"{base}/members/{removed_id}/remove", match=1)
    assert removed.get("/api/me").json()["user"]["name_change_locked"] is False
    request(removed, "PUT", "/api/me/profile", {"display_name": "移出后可改名"})

    post(host, base + "/answers", {"text": "房主回答"}, match=1)
    post(player, base + "/answers", {"text": "玩家回答"}, match=1)
    human_ballot = {
        "predictions": {host_alias: "AI", player_alias: "Human"},
        "evidence": "真人裁判判断",
        "confidence": 60,
    }
    ai_ballot = {
        "predictions": {host_alias: "Human", player_alias: "Human"},
        "evidence": "AI 裁判判断",
        "confidence": 60,
    }
    post(judge, base + "/judge-verdict", human_ballot, match=1)
    post(host, f"{base}/ai-judges/{ai_judge}/verdict", ai_ballot, match=1)
    post(host, base + "/reveal", match=1)

    assert removed.get("/api/me").json()["user"]["name_change_locked"] is False
    renamed = request(removed, "PUT", "/api/me/profile", {"display_name": "完赛后可改名"})
    assert renamed["user"]["display_name"] == "完赛后可改名"
    assert host.get("/api/me/social").json()["points"]["total_points"] == "-10"
    assert player.get("/api/me/social").json()["points"]["total_points"] == "-10"
    assert removed.get("/api/me/social").json()["points"] == {
        "total_points": "36", "current_streak": "3"
    }
    assert admin.get("/api/me/social").json()["points"] == {
        "total_points": "0", "current_streak": "0"
    }
    assert host.get("/api/me/points/ledger").json()["entries"][0]["outcome"] == "loss"
    assert player.get("/api/me/points/ledger").json()["entries"][0]["outcome"] == "loss"
    assert removed.get("/api/me/points/ledger").json()["entries"] == []


def test_invalid_final_judgment_rolls_back_match_finish_chat_and_points(env):
    path, _, _, host, player, _, _ = env
    created = post(host, "/api/rooms", {"title": "结算事务回滚"})
    base = f"/api/rooms/{created['room_id']}"
    start_legacy_match(host, player, base, created["code"])
    post(host, base + "/chat", {
        "match_number": 1,
        "text": "揭晓失败后仍应保留的聊天",
    }, match=1)
    post(host, base + "/verdict", {
        "predictions": {
            host.get(base).json()["humans"][0]["alias"]: "AI",
            host.get(base).json()["humans"][1]["alias"]: "Human",
        },
        "evidence": "合法锁定后再模拟损坏",
        "confidence": 50,
    }, match=1)

    with db_open(path) as db:
        db.execute(
            "UPDATE rooms SET verdict_json=? WHERE id=?",
            ('{"predictions":{"A":"AI"}}', created["room_id"]),
        )

    post(host, base + "/reveal", {}, status=409, match=1)
    assert host.get(base).json()["status"] == "open"
    assert host.get(base + "/chat?match_number=1").json()["messages"][0]["text"] == \
        "揭晓失败后仍应保留的聊天"
    with db_open(path) as db:
        assert db.execute("SELECT COUNT(*) FROM match_settlements").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM points_ledger").fetchone()[0] == 0

