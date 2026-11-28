import pytest

from app import db_open
from test_social_points import fixture_clients, make_friends, post, request


@pytest.fixture
def lab(tmp_path):
    path, app, admin, host, player, other, judge = fixture_clients(tmp_path)
    return path, app, admin, host, player, other, judge


def create_room(host, title="Community room", **extra):
    return post(host, "/api/rooms", {"title": title, **extra})


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
    post(host, "/api/join", {"code": room["code"], "role": "player"})
    post(player, "/api/join", {"code": room["code"], "role": "player"})
    post(judge, "/api/join", {"code": room["code"], "role": "judge"})
    post(judge, base + "/judge-questions", {"text": "已提交的裁判问题"}, match=1)
    items = other.get("/api/public/rooms").json()["rooms"]
    assert all(item["room_id"] != room["room_id"] for item in items)