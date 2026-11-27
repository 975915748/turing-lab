import sqlite3

import pytest

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
