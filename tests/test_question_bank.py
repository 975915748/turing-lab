import pytest
from fastapi.testclient import TestClient

from app import create_app, seed_admin
from question_bank import BANK_VERSION, CATEGORIES, QUESTIONS, choose_random_question, list_questions


def test_question_bank_has_stable_unique_ids_and_four_categories():
    ids = [item["id"] for item in QUESTIONS]
    assert len(ids) == len(set(ids))
    assert len(CATEGORIES) == 4
    assert {item["category"] for item in QUESTIONS} == {item["id"] for item in CATEGORIES}
    assert all(len(item["text"]) <= 3000 for item in QUESTIONS)
    assert BANK_VERSION


def test_question_bank_filters_by_category_and_keyword():
    logic = list_questions(category="logic", limit=100)
    assert logic and all(item["category"] == "logic" for item in logic)
    matches = list_questions(query="因果", limit=100)
    assert matches and any("因果" in item["text"] or "因果" in item["tags"] for item in matches)
    assert len(list_questions(limit=3)) == 3
    with pytest.raises(ValueError, match="分类"):
        list_questions(category="not-a-category")


def test_random_question_uses_only_given_options():
    options = list_questions(category="experience")
    selected = choose_random_question(options)
    assert selected in options
    assert choose_random_question([]) is None


def test_authenticated_question_bank_api_is_read_only_and_filterable(tmp_path):
    path = tmp_path / "question-bank.sqlite3"
    seed_admin(path, "question_bank_admin", "出题管理员", "question-bank-admin-password")
    app = create_app(path)
    client = TestClient(app)

    assert client.get("/api/question-bank").status_code == 401
    registered = client.post("/api/register", json={
        "username": "question_bank_user",
        "display_name": "题库用户",
        "password": "question-bank-user-password",
    })
    assert registered.status_code == 200

    response = client.get("/api/question-bank?category=language&limit=2")
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == BANK_VERSION
    assert len(data["questions"]) == 2
    assert all(item["category"] == "language" for item in data["questions"])
    assert len(data["categories"]) == 4

    searched = client.get("/api/question-bank?q=草地")
    assert searched.status_code == 200
    assert any("草地" in item["title"] or "草地" in item["text"] for item in searched.json()["questions"])
    assert client.get("/api/question-bank?category=unknown").status_code == 422
    assert client.post("/api/question-bank").status_code in (405, 422)
