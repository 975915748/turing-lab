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
            PRIMARY KEY(room_id,match_number));
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
    db.execute("INSERT INTO ai_players VALUES(1,'A','Bot A',NULL,'analytical',1,'','short',0,1)")
    room = {"id": 1, "match_number": 3, "match_started_at": None}
    assert freeze_match_personas(db, room) is True
    assert freeze_match_personas(db, room) is False
    db.execute("UPDATE ai_players SET persona_preset_id='casual' WHERE room_id=1 AND alias='A'")
    rows = revealed_persona_rows(db, 1, 3)
    assert rows[0]["persona_name"] == "严谨分析者"
    assert rows[0]["output_length"] == "short"
    assert "api_key" not in rows[0]


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

    # A preparation attempt freezes both seat configs even if the AI judge is manual.
    request(admin, "POST", base + "/ai-judges/questions/generate")
    assert admin.get(base).json()["personas_frozen"] is True
    request(admin, "PUT", base + f"/ai/{ai['alias']}/persona", participant_config, 409)

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
