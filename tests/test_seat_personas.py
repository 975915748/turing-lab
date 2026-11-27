import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import create_app, seed_admin
from seat_personas import (
    compose_system_prompt,
    freeze_match_personas,
    normalize_persona_config,
    output_token_limit,
    revealed_persona_rows,
)


def test_participant_presets_and_output_limits():
    persona = normalize_persona_config("participant", preset_id="analytical", output_length="short")
    assert persona["display_name"] == "严谨分析者"
    assert "事实依据" in persona["style_prompt"]
    assert output_token_limit(persona, 8192) == 256
    assert output_token_limit(persona, 128) == 128


def test_final_verdict_keeps_full_model_token_budget():
    persona = normalize_persona_config("judge", preset_id="language_detail", output_length="short")
    assert output_token_limit(persona, 8192, final_verdict=True) == 8192
    prompt = compose_system_prompt("只返回完整 JSON。", persona, task="judge_verdict", final_verdict=True)
    assert "完整 JSON" in prompt
    assert "不得截断或省略 JSON 字段" in prompt


def test_custom_prompt_is_required_and_bounded():
    with pytest.raises(ValueError, match="必须填写"):
        normalize_persona_config("participant", preset_id="custom")
    with pytest.raises(ValueError, match="8000"):
        normalize_persona_config("participant", preset_id="custom", custom_system_prompt="x" * 8001)
    persona = normalize_persona_config("participant", preset_id="custom", custom_system_prompt="温和、简短")
    assert persona["style_prompt"] == "温和、简短"


@pytest.mark.parametrize("history", [-1, 101, True, 1.5])
def test_history_window_validation(history):
    with pytest.raises(ValueError):
        normalize_persona_config("participant", history_rounds=history)


def test_seat_type_preset_is_validated():
    with pytest.raises(ValueError, match="不适用于"):
        normalize_persona_config("judge", preset_id="minimal")


def test_match_persona_snapshot_is_immutable_and_revealed_without_secrets():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE match_persona_freezes(
            room_id INTEGER, match_number INTEGER, frozen_at TEXT, source TEXT,
            participant_roster_revision TEXT, PRIMARY KEY(room_id,match_number));
        CREATE TABLE match_seat_persona_snapshots(
            id INTEGER PRIMARY KEY, room_id INTEGER, match_number INTEGER,
            seat_type TEXT, seat_key TEXT, seat_label TEXT, config_json TEXT,
            model_json TEXT, config_hash TEXT, frozen_at TEXT);
        CREATE TABLE ai_players(
            room_id INTEGER, alias TEXT, name TEXT, model_profile_id INTEGER,
            persona_preset_id TEXT, persona_preset_version INTEGER,
            persona_custom_prompt TEXT, persona_output_length TEXT,
            persona_history_rounds INTEGER, persona_revision INTEGER);
        CREATE TABLE ai_judges(
            id INTEGER, room_id INTEGER, label TEXT, model_profile_id INTEGER,
            persona_preset_id TEXT, persona_preset_version INTEGER,
            persona_custom_prompt TEXT, persona_output_length TEXT,
            persona_history_rounds INTEGER, persona_revision INTEGER);
        CREATE TABLE model_profiles(
            id INTEGER, owner_user_id INTEGER, name TEXT, model TEXT, base_url TEXT,
            temperature REAL, max_tokens INTEGER, timeout_seconds INTEGER);
    """)
    db.execute("INSERT INTO model_profiles VALUES(11,7,'private config','frozen-model',"
               "'https://provider.example/v1',0.3,1024,30)")
    db.execute("INSERT INTO ai_players VALUES(1,'A','Bot A',11,'analytical',1,'','short',0,1)")
    room = {"id": 1, "match_number": 3, "match_started_at": None}
    assert freeze_match_personas(db, room, participant_roster_revision="roster-v1") is True
    assert freeze_match_personas(db, room, participant_roster_revision="roster-v2") is False
    assert db.execute("SELECT participant_roster_revision FROM match_persona_freezes").fetchone()[0] == "roster-v1"
    db.execute("UPDATE ai_players SET persona_preset_id='casual' WHERE room_id=1 AND alias='A'")
    db.execute("UPDATE model_profiles SET model='changed-model' WHERE id=11")
    rows = revealed_persona_rows(db, 1, 3)
    assert rows[0]["persona_name"] == "严谨分析者"
    assert "事实依据" in rows[0]["style_prompt"]
    assert rows[0]["output_length"] == "short"
    assert rows[0]["model_name"] == "frozen-model"
    assert rows[0]["model_temperature"] == 0.3
    assert rows[0]["model_max_tokens"] == 1024
    assert rows[0]["model_timeout_seconds"] == 30
    assert "base_url" not in rows[0] and "profile_name" not in rows[0] and "api_key" not in rows[0]


def test_prestart_deleted_roster_recovery_updates_frozen_revision(tmp_path):
    path = tmp_path / "persona-recovery.sqlite3"
    seed_admin(path, "persona_admin", "人设管理员", "persona-admin-password")
    app = create_app(path)
    admin, player_a, player_b = (TestClient(app) for _ in range(3))

    def request(client, method, url, data=None, expected=200):
        me = client.get("/api/me")
        headers = {"X-CSRF-Token": me.json()["user"]["csrf"]} if me.status_code == 200 else {}
        response = client.request(method, url, json=data or {}, headers=headers)
        assert response.status_code == expected, (url, response.status_code, response.text)
        return response.json() if response.content else {}

    request(admin, "POST", "/api/login",
            {"username": "persona_admin", "password": "persona-admin-password"})
    room = request(admin, "POST", "/api/rooms", {"title": "冻结后的账号删除补位"})
    base = f"/api/rooms/{room['room_id']}"
    players = (player_a, player_b)
    for index, client in enumerate(players):
        request(client, "POST", "/api/register", {
            "username": f"persona_recovery_player_{index}",
            "display_name": f"参赛者 {index}",
            "password": "persona-player-password",
            "code": room["code"],
        })
    request(admin, "POST", base + "/ai", {"name": "保留 AI"})
    judge = request(admin, "POST", base + "/ai-judges", {"name": "手动 AI 裁判"})

    # Preparation freezes the original two-human/one-AI roster.
    request(admin, "POST", base + "/ai-judges/questions/generate")
    request(admin, "POST", base + f"/ai-judges/{judge['id']}/questions",
            {"text": "请描述一次你修正错误判断的经历。"})

    for client in players:
        uid = client.get("/api/me").json()["user"]["uid"]
        request(admin, "DELETE", "/api/admin/users/" + uid, {"confirm": True})
    assert admin.get(base).json()["roster_recovery"] is True

    # The only permitted post-freeze replacement is a manual AI seat. Its
    # frozen roster revision must advance with the recovery snapshot.
    request(admin, "POST", base + "/ai", {"name": "账号删除补位 AI"})
    request(admin, "POST", base + "/start")
    info = admin.get(base).json()
    assert info["match_started"] is True
    assert info["personas_frozen"] is True
    assert len(info["ais"]) == 2
    assert not info.get("seat_personas")  # details remain hidden until formal reveal


def test_persona_api_freezes_and_allows_preparation_reset(tmp_path):
    path = tmp_path / "personas.sqlite3"
    seed_admin(path, "persona_admin", "人设管理员", "persona-admin-password")
    app = create_app(path)
    admin, player_a, player_b = (TestClient(app) for _ in range(3))

    def request(client, method, url, data=None, expected=200):
        me = client.get("/api/me")
        headers = {"X-CSRF-Token": me.json()["user"]["csrf"]} if me.status_code == 200 else {}
        response = client.request(method, url, json=data or {}, headers=headers)
        assert response.status_code == expected, (url, response.status_code, response.text)
        return response.json() if response.content else {}

    request(admin, "POST", "/api/login",
            {"username": "persona_admin", "password": "persona-admin-password"})
    room = request(admin, "POST", "/api/rooms", {"title": "人设冻结验收"})
    base = f"/api/rooms/{room['room_id']}"
    for index, client in enumerate((player_a, player_b)):
        request(client, "POST", "/api/register", {
            "username": f"persona_player_{index}",
            "display_name": f"参赛者 {index}",
            "password": "persona-player-password",
            "code": room["code"],
        })
    human_judge = TestClient(app)
    request(human_judge, "POST", "/api/register", {
        "username": "persona_human_judge", "display_name": "真人裁判",
        "password": "persona-judge-password",
    })
    request(human_judge, "POST", "/api/join", {"code": room["code"], "role": "judge"})
    ai = request(admin, "POST", base + "/ai", {"name": "AI 参赛席"})
    judge = request(admin, "POST", base + "/ai-judges", {"name": "AI 裁判席"})
    catalog = admin.get("/api/ai-seat-personas").json()
    assert any(item["id"] == "analytical" for item in catalog["participant"])
    assert any(item["id"] == "language_detail" for item in catalog["judge"])

    participant_config = {
        "preset_id": "analytical", "custom_system_prompt": "",
        "output_length": "short", "history_rounds": 2,
    }
    judge_config = {
        "preset_id": "language_detail", "custom_system_prompt": "",
        "output_length": "medium", "history_rounds": 4,
    }
    request(admin, "PUT", base + f"/ai/{ai['alias']}/persona", participant_config)
    request(admin, "PUT", base + f"/ai-judges/{judge['id']}/persona", judge_config)
    request(player_a, "PUT", base + f"/ai/{ai['alias']}/persona", participant_config, 403)
    info = admin.get(base).json()
    assert info["ais"][0]["persona"]["preset_id"] == "analytical"
    assert next(x for x in info["judges"] if x["id"] == judge["id"])["persona"]["preset_id"] == "language_detail"
    player_view = player_a.get(base).json()
    assert player_view["personas_revealed"] is False
    assert not player_view.get("seat_personas")
    assert all(seat.get("persona") is None for seat in player_view["ais"])
    judge_view = human_judge.get(base).json()
    assert judge_view["is_judge"] is True
    assert judge_view["personas_revealed"] is False
    assert not judge_view.get("seat_personas")

    # A preparation attempt freezes both seat configs even if the AI judge is manual.
    request(admin, "POST", base + "/ai-judges/questions/generate")
    assert admin.get(base).json()["personas_frozen"] is True
    request(admin, "PUT", base + f"/ai/{ai['alias']}/persona", participant_config, 409)

    # A late registration changes the frozen contestant roster. A manually
    # entered AI judge question based on the old roster must not start the round.
    late_player = TestClient(app)
    request(late_player, "POST", "/api/register", {
        "username": "persona_player_late", "display_name": "迟到参赛者",
        "password": "persona-player-password", "code": room["code"],
    })
    request(admin, "POST", base + f"/ai-judges/{judge['id']}/questions",
            {"text": "请描述一次你修正错误判断的经历。"})
    request(admin, "POST", base + "/start", expected=409)

    reset = request(admin, "POST", base + "/personas/reset-preparation", {"confirm": True})
    assert reset["reset"] is True
    assert admin.get(base).json()["personas_frozen"] is False
    request(admin, "PUT", base + f"/ai/{ai['alias']}/persona", participant_config)

    request(admin, "POST", base + f"/ai-judges/{judge['id']}/questions",
            {"text": "请描述一次你修正错误判断的经历。"})
    request(admin, "POST", base + "/start")
    info = admin.get(base).json()
    assert info["personas_frozen"] is True
    assert info["match_started"] is True
    request(admin, "PUT", base + f"/ai-judges/{judge['id']}/persona", judge_config, 409)
    request(admin, "POST", base + "/personas/reset-preparation", {"confirm": True}, 409)

    # Formal reveal is the only point at which ordinary players and human judges
    # receive the immutable persona details for this match.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE rooms SET status='finished', revealed_at=?, verdict_locked_at=? WHERE id=?",
                   ("2026-11-27T12:00:00+00:00", "2026-11-27T12:00:00+00:00", room["room_id"]))
    for viewer in (player_a, human_judge):
        revealed = viewer.get(base).json()
        assert revealed["personas_revealed"] is True
        personas = revealed["seat_personas"]
        assert len(personas) == 2
        assert any(item["persona_name"] == "严谨分析者" for item in personas)
        assert any(item["persona_name"] == "语言细节" for item in personas)
        assert all("seat_label" not in item and "profile_name" not in item
                   and "base_url" not in item and "api_key" not in item for item in personas)
        exported = viewer.get(base + "/log")
        assert exported.status_code == 200, exported.text
        assert "严谨分析者" not in exported.text
        assert "语言细节" not in exported.text
