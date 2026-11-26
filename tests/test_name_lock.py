"""Account-name locking across room roles and match lifecycles."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import create_app, seed_admin


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


def put(client, route, body=None, status=200):
    return request(client, "PUT", route, body or {}, status)


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "name-lock.sqlite3"
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


def create_room(host, title):
    created = post(host, "/api/rooms", {"title": title})
    return created, f"/api/rooms/{created['room_id']}"


def join_as_player(client, code):
    return post(client, "/api/join", {"code": code, "role": "player"})


def test_legacy_start_locks_active_roles_and_unlocks_removed_member(env):
    _, _, admin, host, player, third, judge = env
    created, base = create_room(host, "手动裁判改名锁")
    host_alias = join_as_player(host, created["code"])["alias"]
    player_alias = join_as_player(player, created["code"])["alias"]
    join_as_player(third, created["code"])

    # A room that has not started yet does not lock any account.
    for client in (host, player, third, judge, admin):
        assert client.get("/api/me").json()["user"]["name_change_locked"] is False
    put(player, "/api/me/profile", {"display_name": "开局前可改名"})
    assert player.get("/api/me").json()["user"]["display_name"] == "开局前可改名"

    # The legacy manual-question path is also the first successful start.
    post(host, base + "/question", {"text": "第一轮问题"}, match=1)
    for client in (host, player, third):
        assert client.get("/api/me").json()["user"]["name_change_locked"] is True
        put(client, "/api/me/profile", {"display_name": "不应生效"}, status=409)
    assert admin.get("/api/me").json()["user"]["name_change_locked"] is False

    # Removal ends the participant role and pushes the updated lock state to an open personal-center session.
    third_id = third.get("/api/me").json()["user"]["id"]
    with third.websocket_connect("/ws/messages") as socket:
        assert socket.receive_json()["type"] == "connected"
        post(host, f"{base}/members/{third_id}/remove", match=1)
        update = socket.receive_json()
        assert update["type"] == "name_lock_updated"
        assert third.get("/api/me").json()["user"]["name_change_locked"] is False
        renamed = put(third, "/api/me/profile", {"display_name": "移出后可改名"})
        assert renamed["user"]["display_name"] == "移出后可改名"

    post(host, base + "/answers", {"text": "房主回答"}, match=1)
    post(player, base + "/answers", {"text": "玩家回答"}, match=1)
    post(host, base + "/verdict", {
        "predictions": {host_alias: "Human", player_alias: "AI"},
        "evidence": "测试判决",
        "confidence": 70,
    }, match=1)
    post(host, base + "/reveal", match=1)

    for client in (host, player, third, judge):
        assert client.get("/api/me").json()["user"]["name_change_locked"] is False
        result = put(client, "/api/me/profile", {"display_name": "完赛后可改名"})
        assert result["user"]["name_change_locked"] is False
    assert third.get("/api/me").json()["user"]["display_name"] == "完赛后可改名"


def test_independent_judge_start_locks_judge_and_room_roles(env):
    _, _, admin, host, player, third, judge = env
    created, base = create_room(host, "独立裁判改名锁")
    host_alias = join_as_player(host, created["code"])["alias"]
    player_alias = join_as_player(player, created["code"])["alias"]
    post(judge, "/api/join", {"code": created["code"], "role": "judge"})
    post(judge, base + "/judge-questions", {"text": "真人裁判问题"}, match=1)
    post(host, base + "/start", match=1)

    for client in (host, player, judge):
        assert client.get("/api/me").json()["user"]["name_change_locked"] is True
        put(client, "/api/me/profile", {"display_name": "不能改"}, status=409)
    assert admin.get("/api/me").json()["user"]["name_change_locked"] is False

    # Deleting an unfinished room releases only this room's lock.
    request(host, "DELETE", base, {"match_number": 1, "confirm": True})
    for client in (host, player, judge):
        assert client.get("/api/me").json()["user"]["name_change_locked"] is False
        put(client, "/api/me/profile", {"display_name": "删除房间后可改"})
    assert third.get("/api/me").json()["user"]["name_change_locked"] is False


def test_lock_is_account_wide_across_rooms_until_last_room_finishes_or_is_deleted(env):
    _, _, admin, first_host, player, second_host, third = env
    room1, base1 = create_room(first_host, "第一间")
    join_as_player(first_host, room1["code"])
    join_as_player(player, room1["code"])
    join_as_player(third, room1["code"])
    post(first_host, base1 + "/question", {"text": "第一间问题"}, match=1)

    room2, base2 = create_room(second_host, "第二间")
    join_as_player(second_host, room2["code"])
    join_as_player(player, room2["code"])
    post(second_host, base2 + "/question", {"text": "第二间问题"}, match=1)

    assert player.get("/api/me").json()["user"]["name_change_locked"] is True
    player_id = player.get("/api/me").json()["user"]["id"]
    post(first_host, f"{base1}/members/{player_id}/remove", match=1)
    # Removal unlocks the first room's participant role, but the second room still locks the account.
    assert player.get("/api/me").json()["user"]["name_change_locked"] is True
    put(player, "/api/me/profile", {"display_name": "仍被第二间锁定"}, status=409)
    request(first_host, "DELETE", base1, {"match_number": 1, "confirm": True})
    assert player.get("/api/me").json()["user"]["name_change_locked"] is True

    request(second_host, "DELETE", base2, {"match_number": 1, "confirm": True})
    assert player.get("/api/me").json()["user"]["name_change_locked"] is False
    put(player, "/api/me/profile", {"display_name": "两间均结束"})
    assert admin.get("/api/me").json()["user"]["name_change_locked"] is False



def test_admin_who_owns_and_plays_a_match_is_locked_but_observer_is_not(env):
    _, _, admin, player, observer, _, _ = env
    created, base = create_room(admin, "管理员实际参赛改名锁")
    admin_alias = join_as_player(admin, created["code"])["alias"]
    player_alias = join_as_player(player, created["code"])["alias"]

    post(admin, base + "/question", {"text": "管理员作为参赛者开局"}, match=1)
    assert admin.get("/api/me").json()["user"]["name_change_locked"] is True
    assert player.get("/api/me").json()["user"]["name_change_locked"] is True
    assert observer.get("/api/me").json()["user"]["name_change_locked"] is False
    put(admin, "/api/me/profile", {"display_name": "管理员绕过锁名"}, status=409)

    post(admin, base + "/answers", {"text": "管理员回答"}, match=1)
    post(player, base + "/answers", {"text": "玩家回答"}, match=1)
    post(admin, base + "/verdict", {
        "predictions": {admin_alias: "Human", player_alias: "AI"},
        "evidence": "管理员参赛锁名测试",
        "confidence": 70,
    }, match=1)
    post(admin, base + "/reveal", match=1)

    assert admin.get("/api/me").json()["user"]["name_change_locked"] is False
    put(admin, "/api/me/profile", {"display_name": "管理员完赛后改名"})

def test_profile_update_and_match_start_are_serialized(env):
    _, app, _, host, player, _, _ = env
    created, base = create_room(host, "改名开局并发")
    join_as_player(host, created["code"])
    join_as_player(player, created["code"])

    host_uid = host.get("/api/me").json()["user"]["uid"]
    profile_session = TestClient(app)
    post(profile_session, "/api/login", {
        "uid": host_uid,
        "password": "player-password",
    })
    profile_csrf = profile_session.get("/api/me").json()["user"]["csrf"]
    host_csrf = host.get("/api/me").json()["user"]["csrf"]

    def update_profile():
        return profile_session.put(
            "/api/me/profile",
            json={"display_name": "并发改名"},
            headers={"X-CSRF-Token": profile_csrf},
        )

    def start_match():
        return host.post(
            base + "/question",
            json={"text": "并发开局问题"},
            headers={"X-CSRF-Token": host_csrf, "X-Match-Number": "1"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        profile_future = pool.submit(update_profile)
        start_future = pool.submit(start_match)
        profile_response = profile_future.result()
        start_response = start_future.result()

    assert start_response.status_code == 200, start_response.text
    assert profile_response.status_code in (200, 409), profile_response.text
    current = host.get("/api/me").json()["user"]
    assert current["name_change_locked"] is True
    if profile_response.status_code == 200:
        assert current["display_name"] == "并发改名"
    else:
        assert current["display_name"] == "玩家0"
