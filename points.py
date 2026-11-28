"""Exact account-point calculation and atomic match settlement helpers."""

import json

from fastapi import HTTPException


def calculate_streak_reward(streak_after: int) -> int:
    """ROUND_HALF_UP(10 * 1.2 ** (streak_after - 1)) using integer arithmetic."""
    if type(streak_after) is not int or streak_after < 1:
        raise ValueError("streak_after must be a positive integer")
    numerator = 10 * (6 ** (streak_after - 1))
    denominator = 5 ** (streak_after - 1)
    return (2 * numerator + denominator) // (2 * denominator)


def calculate_account_result(points: int, streak: int, won: bool):
    """Return (new_points, new_streak, actual_delta) without floating-point math."""
    if type(points) is not int or type(streak) is not int or streak < 0:
        raise ValueError("invalid account-point state")
    if points < -100:
        raise ValueError("account points are below the supported floor")
    if won:
        next_streak = streak + 1
        delta = calculate_streak_reward(next_streak)
        return points + delta, next_streak, delta
    next_points = max(-100, points - 10)
    return next_points, 0, next_points - points


def points_state(db, user_id: int, now: str):
    db.execute(
        "INSERT OR IGNORE INTO account_points(user_id,points,streak,updated_at) VALUES(?,?,?,?)",
        (user_id, "0", "0", now),
    )
    row = db.execute(
        "SELECT points,streak FROM account_points WHERE user_id=?", (user_id,)
    ).fetchone()
    if not row:
        raise HTTPException(500, "账号积分状态不可用。")
    return {"total_points": str(row["points"]), "current_streak": str(row["streak"])}


def _load_predictions(raw, roster, known_roster=None):
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise HTTPException(409, "最终裁判判断数据损坏，无法结算积分。")
    predictions = payload.get("predictions") if isinstance(payload, dict) else None
    required = set(roster)
    allowed = set(known_roster if known_roster is not None else roster)
    if (
        not isinstance(predictions, dict)
        or not required.issubset(set(predictions))
        or not set(predictions).issubset(allowed)
        or any(value not in ("Human", "AI") for value in predictions.values())
    ):
        raise HTTPException(409, "最终裁判判断不完整或包含非法值，无法结算积分。")
    return predictions


def settle_match_points(db, room, human_rows, roster, judges, now: str):
    """Settle every eligible human in the caller's existing SQLite transaction."""
    room_id = int(room["id"])
    match_number = int(room["match_number"])
    if db.execute(
        "SELECT 1 FROM match_settlements WHERE room_id=? AND match_number=?",
        (room_id, match_number),
    ).fetchone():
        raise HTTPException(409, "本场比赛已经结算过积分。")

    historical_roster = {
        row["alias"] for row in db.execute(
            """SELECT alias FROM members WHERE room_id=?
               UNION SELECT alias FROM ai_players WHERE room_id=?""",
            (room_id, room_id),
        ).fetchall()
    }
    ballots = []
    if judges:
        for judge in judges:
            if not judge.get("locked"):
                raise HTTPException(409, "仍有裁判未锁定最终判断，不能结算积分。")
            if judge.get("kind") == "human":
                judge_id = judge.get("user_id") or judge.get("id")
                row = db.execute(
                    "SELECT verdict_json FROM judge_verdicts WHERE room_id=? AND judge_user_id=?",
                    (room_id, judge_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT verdict_json FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?",
                    (room_id, judge.get("id")),
                ).fetchone()
            if not row:
                raise HTTPException(409, "缺少裁判最终判断，无法结算积分。")
            ballots.append(_load_predictions(row["verdict_json"], roster, historical_roster))
    else:
        if not room["verdict_json"]:
            raise HTTPException(409, "缺少最终裁判判断，无法结算积分。")
        ballots.append(_load_predictions(room["verdict_json"], roster, historical_roster))

    if not ballots:
        raise HTTPException(409, "没有有效最终裁判判断，无法结算积分。")

    # This marker and every account update/ledger row commit or roll back together.
    db.execute(
        "INSERT INTO match_settlements(room_id,match_number,settled_at) VALUES(?,?,?)",
        (room_id, match_number, now),
    )

    results = {}
    voter_count = len(ballots)
    for member in human_rows:
        alias = member["alias"]
        votes = [ballot.get(alias) for ballot in ballots]
        if len(votes) != voter_count or any(vote not in ("Human", "AI") for vote in votes):
            raise HTTPException(409, "存在缺失或非法的参赛者判断，无法结算积分。")
        won = 2 * sum(vote == "AI" for vote in votes) > voter_count
        user_id = int(member["user_id"])
        points_state(db, user_id, now)
        current = db.execute(
            "SELECT points,streak FROM account_points WHERE user_id=?", (user_id,)
        ).fetchone()
        try:
            old_points = int(current["points"])
            old_streak = int(current["streak"])
            new_points, new_streak, delta = calculate_account_result(
                old_points, old_streak, won
            )
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(409, "账号积分状态无效，无法结算本场比赛。")

        db.execute(
            "UPDATE account_points SET points=?,streak=?,updated_at=? WHERE user_id=?",
            (str(new_points), str(new_streak), now, user_id),
        )
        outcome = "win" if won else "loss"
        db.execute(
            """INSERT INTO points_ledger(
                room_id,match_number,user_id,outcome,delta,points_after,streak_after,settled_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                room_id,
                match_number,
                user_id,
                outcome,
                str(delta),
                str(new_points),
                str(new_streak),
                now,
            ),
        )
        results[user_id] = {
            "outcome": outcome,
            "delta": str(delta),
            "points_after": str(new_points),
            "streak_after": str(new_streak),
            "settled_at": now,
        }
    return results
