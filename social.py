"""Private friend, direct-message and points-ledger APIs for Turing Lab."""

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, StrictStr


class FriendRequestData(BaseModel, extra="forbid"):
    uid: str = Field(min_length=1, max_length=40)


class FriendNoteData(BaseModel, extra="forbid"):
    note: Optional[str] = Field(default=None, max_length=40)
    reset_default: bool = False


class DirectMessageData(BaseModel, extra="forbid"):
    text: str = Field(min_length=1, max_length=2000)
    client_message_id: str = Field(min_length=1, max_length=128)


class ReadPositionData(BaseModel, extra="forbid"):
    last_read_message_id: int = Field(ge=0)


class FriendDeleteData(BaseModel, extra="forbid"):
    confirm: bool


class RoomInviteBatchData(BaseModel, extra="forbid"):
    uids: list[StrictStr] = Field(min_length=1, max_length=20)
    role: Literal["player", "judge"]


def uid_to_id(uid, user_uid):
    value = str(uid or "").strip().upper()
    if not re.fullmatch(r"TL[0-9]{8,19}", value):
        return None
    try:
        user_id = int(value[2:])
    except ValueError:
        return None
    if user_id < 1 or user_uid(user_id) != value:
        return None
    return user_id


def _pair(a, b):
    return (min(int(a), int(b)), max(int(a), int(b)))


def friendship_exists(db, a, b):
    low, high = _pair(a, b)
    return bool(
        db.execute(
            "SELECT 1 FROM friendships WHERE user_low_id=? AND user_high_id=?",
            (low, high),
        ).fetchone()
    )


def conversation_row(db, a, b):
    low, high = _pair(a, b)
    return db.execute(
        "SELECT * FROM dm_conversations WHERE user_low_id=? AND user_high_id=?",
        (low, high),
    ).fetchone()


def ensure_app_schema(db, now):
    """Add social/points tables and the per-match start timestamp without resetting data."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS account_points (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE RESTRICT,
            points TEXT NOT NULL DEFAULT '0',
            streak TEXT NOT NULL DEFAULT '0',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS match_settlements (
            room_id INTEGER NOT NULL,
            match_number INTEGER NOT NULL,
            settled_at TEXT NOT NULL,
            PRIMARY KEY(room_id,match_number)
        );
        CREATE TABLE IF NOT EXISTS points_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            match_number INTEGER NOT NULL,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            outcome TEXT NOT NULL CHECK(outcome IN ('win','loss')),
            delta TEXT NOT NULL,
            points_after TEXT NOT NULL,
            streak_after TEXT NOT NULL,
            settled_at TEXT NOT NULL,
            UNIQUE(room_id,match_number,user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_points_ledger_user
            ON points_ledger(user_id,id DESC);

        CREATE TABLE IF NOT EXISTS friend_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','withdrawn')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(sender_user_id<>recipient_user_id),
            UNIQUE(sender_user_id,recipient_user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_friend_requests_incoming
            ON friend_requests(recipient_user_id,status,created_at);
        CREATE INDEX IF NOT EXISTS idx_friend_requests_outgoing
            ON friend_requests(sender_user_id,status,created_at);

        CREATE TABLE IF NOT EXISTS friendships (
            user_low_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            user_high_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_low_id,user_high_id),
            CHECK(user_low_id<user_high_id)
        );
        CREATE TABLE IF NOT EXISTS friend_notes (
            owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            friend_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            note TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(owner_user_id,friend_user_id),
            CHECK(owner_user_id<>friend_user_id)
        );

        CREATE TABLE IF NOT EXISTS room_invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            inviter_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            invitee_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('player','judge')),
            status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','cancelled','expired')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            responded_at TEXT,
            updated_at TEXT NOT NULL,
            CHECK(inviter_user_id<>invitee_user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_room_invites_received
            ON room_invites(invitee_user_id,status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_room_invites_sent
            ON room_invites(inviter_user_id,status,created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_room_invites_room_status
            ON room_invites(room_id,status);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_room_invites_pending_room_invitee
            ON room_invites(room_id,invitee_user_id) WHERE status='pending';

        CREATE TABLE IF NOT EXISTS dm_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_low_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            user_high_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            UNIQUE(user_low_id,user_high_id),
            CHECK(user_low_id<user_high_id)
        );
        CREATE TABLE IF NOT EXISTS dm_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
            sender_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            recipient_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            client_message_id TEXT NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(sender_user_id<>recipient_user_id),
            UNIQUE(sender_user_id,client_message_id)
        );
        CREATE INDEX IF NOT EXISTS idx_dm_messages_conversation
            ON dm_messages(conversation_id,id);
        CREATE INDEX IF NOT EXISTS idx_dm_messages_unread
            ON dm_messages(recipient_user_id,conversation_id,id);
        CREATE TABLE IF NOT EXISTS dm_read_positions (
            conversation_id INTEGER NOT NULL REFERENCES dm_conversations(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            last_read_message_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(conversation_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS api_rate_limits (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            count INTEGER NOT NULL,
            PRIMARY KEY(user_id,action,window_start)
        );
        """
    )
    room_columns = {row["name"] for row in db.execute("PRAGMA table_info(rooms)")}
    if "match_started_at" not in room_columns:
        db.execute("ALTER TABLE rooms ADD COLUMN match_started_at TEXT")
    if "closed_at" not in room_columns:
        db.execute("ALTER TABLE rooms ADD COLUMN closed_at TEXT")
    member_columns = {row["name"] for row in db.execute("PRAGMA table_info(members)")}
    if "leave_reason" not in member_columns:
        db.execute("ALTER TABLE members ADD COLUMN leave_reason TEXT")
    judge_columns = {row["name"] for row in db.execute("PRAGMA table_info(room_judges)")}
    if "left_at" not in judge_columns:
        db.execute("ALTER TABLE room_judges ADD COLUMN left_at TEXT")
    # Older active matches are backfilled from the server-side start event.
    db.execute(
        """UPDATE rooms SET match_started_at=(
            SELECT MIN(e.created_at) FROM events e
            WHERE e.room_id=rooms.id AND e.round_number=1 AND e.kind='judge_question'
        )
        WHERE status='open' AND match_started_at IS NULL
          AND EXISTS(SELECT 1 FROM rounds r
                     WHERE r.room_id=rooms.id AND r.number=1 AND r.status<>'draft')"""
    )
    # A legacy match without the start event still needs a conservative lock.
    db.execute(
        """UPDATE rooms SET match_started_at=(
            SELECT r.created_at FROM rounds r
            WHERE r.room_id=rooms.id AND r.number=1
        )
        WHERE status='open' AND match_started_at IS NULL
          AND EXISTS(SELECT 1 FROM rounds r
                     WHERE r.room_id=rooms.id AND r.number=1 AND r.status<>'draft')"""
    )
    db.execute(
        """INSERT OR IGNORE INTO account_points(user_id,points,streak,updated_at)
           SELECT id,'0','0',? FROM users""",
        (now,),
    )


def cleanup_deleted_account(db, user_id):
    friend_ids = set()
    rows = db.execute(
        """SELECT CASE WHEN user_low_id=? THEN user_high_id ELSE user_low_id END AS friend_id
           FROM friendships WHERE user_low_id=? OR user_high_id=?""",
        (user_id, user_id, user_id),
    ).fetchall()
    friend_ids.update(int(row["friend_id"]) for row in rows)
    invite_rows = db.execute(
        """SELECT inviter_user_id,invitee_user_id FROM room_invites
           WHERE status='pending' AND (inviter_user_id=? OR invitee_user_id=?)""",
        (user_id, user_id),
    ).fetchall()
    for row in invite_rows:
        friend_ids.add(int(row["invitee_user_id"] if row["inviter_user_id"] == user_id else row["inviter_user_id"]))
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        """UPDATE room_invites SET status='cancelled',responded_at=?,updated_at=?
           WHERE status='pending' AND (inviter_user_id=? OR invitee_user_id=?)""",
        (now, now, user_id, user_id),
    )
    db.execute(
        "DELETE FROM friend_requests WHERE sender_user_id=? OR recipient_user_id=?",
        (user_id, user_id),
    )
    db.execute(
        "DELETE FROM friendships WHERE user_low_id=? OR user_high_id=?",
        (user_id, user_id),
    )
    db.execute(
        "DELETE FROM friend_notes WHERE owner_user_id=? OR friend_user_id=?",
        (user_id, user_id),
    )
    db.execute("DELETE FROM api_rate_limits WHERE user_id=?", (user_id,))
    return sorted(friend_ids)


def _rate_limit(db, user_id, action, limit, window_seconds):
    window_start = int(time.time()) // window_seconds
    row = db.execute(
        "SELECT count FROM api_rate_limits WHERE user_id=? AND action=? AND window_start=?",
        (user_id, action, window_start),
    ).fetchone()
    if row and int(row["count"]) >= limit:
        raise HTTPException(429, "操作过于频繁，请稍后再试。")
    if row:
        db.execute(
            "UPDATE api_rate_limits SET count=count+1 WHERE user_id=? AND action=? AND window_start=?",
            (user_id, action, window_start),
        )
    else:
        db.execute(
            "INSERT INTO api_rate_limits(user_id,action,window_start,count) VALUES(?,?,?,1)",
            (user_id, action, window_start),
        )
    db.execute(
        "DELETE FROM api_rate_limits WHERE user_id=? AND action=? AND window_start<?",
        (user_id, action, window_start - 2),
    )


def _public_account(db, user_id, user_uid, points_state, now):
    row = db.execute(
        "SELECT id,display_name,deleted_at FROM users WHERE id=?", (user_id,)
    ).fetchone()
    if not row or row["deleted_at"] is not None:
        return None
    points = points_state(db, user_id, now)
    return {
        "uid": user_uid(user_id),
        "display_name": row["display_name"],
        "total_points": points["total_points"],
        "current_streak": points["current_streak"],
    }


def _social_summary(db, user_id, user_uid, points_state, now):
    friends = []
    rows = db.execute(
        """SELECT CASE WHEN f.user_low_id=? THEN f.user_high_id ELSE f.user_low_id END AS friend_id
           FROM friendships f WHERE f.user_low_id=? OR f.user_high_id=?
           ORDER BY f.created_at DESC""",
        (user_id, user_id, user_id),
    ).fetchall()
    for item in rows:
        friend_id = int(item["friend_id"])
        account = _public_account(db, friend_id, user_uid, points_state, now)
        if not account:
            continue
        note = db.execute(
            "SELECT note FROM friend_notes WHERE owner_user_id=? AND friend_user_id=?",
            (user_id, friend_id),
        ).fetchone()
        conversation = conversation_row(db, user_id, friend_id)
        friends.append(
            {
                **account,
                "note": note["note"] if note else account["display_name"],
                "conversation_id": int(conversation["id"]) if conversation else None,
            }
        )

    incoming = []
    for row in db.execute(
        """SELECT r.id,r.created_at,u.id AS other_id,u.display_name
           FROM friend_requests r JOIN users u ON u.id=r.sender_user_id
           WHERE r.recipient_user_id=? AND r.status='pending' AND u.deleted_at IS NULL
           ORDER BY r.created_at DESC""",
        (user_id,),
    ):
        incoming.append(
            {
                "id": int(row["id"]),
                "uid": user_uid(int(row["other_id"])),
                "display_name": row["display_name"],
                "created_at": row["created_at"],
            }
        )

    outgoing = []
    for row in db.execute(
        """SELECT r.id,r.created_at,u.id AS other_id,u.display_name
           FROM friend_requests r JOIN users u ON u.id=r.recipient_user_id
           WHERE r.sender_user_id=? AND r.status='pending' AND u.deleted_at IS NULL
           ORDER BY r.created_at DESC""",
        (user_id,),
    ):
        outgoing.append(
            {
                "id": int(row["id"]),
                "uid": user_uid(int(row["other_id"])),
                "display_name": row["display_name"],
                "created_at": row["created_at"],
            }
        )

    conversations = []
    conv_rows = db.execute(
        """SELECT * FROM dm_conversations
           WHERE user_low_id=? OR user_high_id=? ORDER BY id DESC""",
        (user_id, user_id),
    ).fetchall()
    for conv in conv_rows:
        other_id = int(conv["user_high_id"] if conv["user_low_id"] == user_id else conv["user_low_id"])
        other = db.execute(
            "SELECT id,display_name,deleted_at FROM users WHERE id=?", (other_id,)
        ).fetchone()
        if not other:
            continue
        active_friend = friendship_exists(db, user_id, other_id) and other["deleted_at"] is None
        note = None
        if active_friend:
            note_row = db.execute(
                "SELECT note FROM friend_notes WHERE owner_user_id=? AND friend_user_id=?",
                (user_id, other_id),
            ).fetchone()
            note = note_row["note"] if note_row else other["display_name"]
        read = db.execute(
            """SELECT last_read_message_id FROM dm_read_positions
               WHERE conversation_id=? AND user_id=?""",
            (conv["id"], user_id),
        ).fetchone()
        last_read_id = int(read["last_read_message_id"]) if read else 0
        unread = db.execute(
            """SELECT COUNT(*) FROM dm_messages
               WHERE conversation_id=? AND recipient_user_id=? AND id>?""",
            (conv["id"], user_id, last_read_id),
        ).fetchone()[0]
        last = db.execute(
            "SELECT id,created_at FROM dm_messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1",
            (conv["id"],),
        ).fetchone()
        point_row = db.execute(
            "SELECT points,streak FROM account_points WHERE user_id=?", (other_id,)
        ).fetchone()
        conversations.append(
            {
                "conversation_id": int(conv["id"]),
                "uid": user_uid(other_id),
                "display_name": "账号已删除" if other["deleted_at"] is not None else other["display_name"],
                "note": note,
                "can_send": bool(active_friend),
                "unread_count": int(unread),
                "last_message_id": int(last["id"]) if last else None,
                "last_message_at": last["created_at"] if last else None,
                "total_points": str(point_row["points"]) if point_row and other["deleted_at"] is None else None,
                "current_streak": str(point_row["streak"]) if point_row and other["deleted_at"] is None else None,
            }
        )

    return {
        "friends": friends,
        "incoming": incoming,
        "outgoing": outgoing,
        "conversations": conversations,
        "points": points_state(db, user_id, now),
    }


def register_social_routes(
    app,
    *,
    connection,
    caller,
    hub,
    auth,
    db_open,
    check_origin,
    cookie,
    user_uid,
    lookup_account,
    norm_text,
    utcnow,
    points_state,
    require_room_manager,
    validate_discovery_room,
    validate_discovery_join,
    join_room_seat,
):
    @app.get("/api/me/room-invites")
    def my_room_invites(request: Request):
        with connection(immediate=True) as db:
            user = caller(request, db)
            now = utcnow()
            db.execute(
                """UPDATE room_invites SET status='expired',responded_at=?,updated_at=?
                   WHERE status='pending' AND expires_at<=?
                     AND (invitee_user_id=? OR inviter_user_id=?)""",
                (now, now, now, user["id"], user["id"]),
            )
            pending_count = db.execute(
                """SELECT COUNT(*) FROM room_invites
                   WHERE invitee_user_id=? AND status='pending' AND expires_at>?""",
                (user["id"], now),
            ).fetchone()[0]
            received = db.execute(
                """SELECT i.id,i.room_id,r.title AS room_title,i.role,i.created_at,i.expires_at,
                          u.display_name AS inviter_name
                   FROM room_invites i
                   JOIN rooms r ON r.id=i.room_id
                   JOIN users u ON u.id=i.inviter_user_id
                   WHERE i.invitee_user_id=? AND i.status='pending' AND i.expires_at>?
                   ORDER BY i.created_at DESC,i.id DESC LIMIT 100""",
                (user["id"], now),
            ).fetchall()
            sent = db.execute(
                """SELECT i.id,i.room_id,r.title AS room_title,i.role,i.created_at,i.expires_at,
                          u.display_name AS invitee_name
                   FROM room_invites i
                   JOIN rooms r ON r.id=i.room_id
                   JOIN users u ON u.id=i.invitee_user_id
                   WHERE i.inviter_user_id=? AND i.status='pending' AND i.expires_at>?
                   ORDER BY i.created_at DESC,i.id DESC LIMIT 100""",
                (user["id"], now),
            ).fetchall()
            return {
                "received": [dict(row) for row in received],
                "sent": [dict(row) for row in sent],
                "pending_count": int(pending_count),
                "truncated": pending_count > len(received) or len(sent) >= 100,
            }

    @app.post("/api/rooms/{room_id}/invites")
    async def send_room_invites(room_id: int, body: RoomInviteBatchData, request: Request):
        normalized_uids = [uid.strip().upper() for uid in body.uids]
        if len(set(normalized_uids)) != len(normalized_uids):
            raise HTTPException(422, "邀请 UID 不能重复。")
        notifications = {}
        results = []
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            room, _ = require_room_manager(db, room_id, user)
            validate_discovery_room(db, room)
            _rate_limit(db, user["id"], "room_invite", 20, 3600)
            now = utcnow()
            expires_at = (datetime.fromisoformat(now) + timedelta(minutes=30)).isoformat(timespec="seconds")
            for raw_uid in normalized_uids:
                try:
                    target = lookup_account(db, uid=raw_uid)
                except HTTPException:
                    target = None
                if not target or target["deleted_at"] is not None:
                    results.append({"uid": raw_uid, "status": "skipped", "reason": "账号不存在或已删除。"})
                    continue
                target_id = int(target["id"])
                normalized_uid = user_uid(target_id)
                if target_id == int(user["id"]):
                    results.append({"uid": normalized_uid, "status": "skipped", "reason": "不能邀请自己。"})
                    continue
                if not friendship_exists(db, user["id"], target_id):
                    results.append({"uid": normalized_uid, "status": "skipped", "reason": "对方不是你的好友。"})
                    continue
                db.execute(
                    """UPDATE room_invites SET status='expired',responded_at=?,updated_at=?
                       WHERE room_id=? AND invitee_user_id=? AND status='pending' AND expires_at<=?""",
                    (now, now, room_id, target_id, now),
                )
                pending = db.execute(
                    """SELECT id,role FROM room_invites
                       WHERE room_id=? AND invitee_user_id=? AND status='pending' LIMIT 1""",
                    (room_id, target_id),
                ).fetchone()
                if pending:
                    if pending["role"] == body.role:
                        invite_id = int(pending["id"])
                        results.append({"uid": normalized_uid, "invite_id": invite_id,
                                        "status": "sent", "reused": True})
                        notifications[target_id] = invite_id
                    else:
                        results.append({"uid": normalized_uid, "status": "skipped",
                                        "reason": "已有不同身份的待处理邀请，请先撤回。"})
                    continue
                cur = db.execute(
                    """INSERT INTO room_invites(
                        room_id,inviter_user_id,invitee_user_id,role,status,created_at,expires_at,updated_at
                    ) VALUES(?,?,?,?,'pending',?,?,?)""",
                    (room_id, user["id"], target_id, body.role, now, expires_at, now),
                )
                invite_id = int(cur.lastrowid)
                notifications[target_id] = invite_id
                results.append({"uid": normalized_uid, "invite_id": invite_id, "status": "sent", "reused": False})
        for target_id, invite_id in sorted(notifications.items()):
            await hub.notify_account(target_id, {"type": "room_invite_created", "invite_id": invite_id})
        return {"results": results}

    @app.post("/api/room-invites/{invite_id}/accept")
    async def accept_room_invite(invite_id: int, request: Request):
        notify_users = set()
        room_id = None
        joined = None
        expired = False
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            invite = db.execute(
                "SELECT * FROM room_invites WHERE id=? AND invitee_user_id=?",
                (invite_id, user["id"]),
            ).fetchone()
            if not invite:
                raise HTTPException(404, "邀请不存在。")
            room_id = int(invite["room_id"])
            if invite["status"] == "accepted":
                if invite["role"] == "player":
                    existing = db.execute(
                        """SELECT alias FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL""",
                        (room_id, user["id"]),
                    ).fetchone()
                    if existing:
                        return {"ok": True, "room_id": room_id, "role": "player",
                                "alias": existing["alias"], "already_joined": True}
                else:
                    existing = db.execute(
                        """SELECT label FROM room_judges WHERE room_id=? AND user_id=? AND left_at IS NULL""",
                        (room_id, user["id"]),
                    ).fetchone()
                    if existing:
                        return {"ok": True, "room_id": room_id, "role": "judge",
                                "label": existing["label"], "already_joined": True}
                raise HTTPException(409, "该邀请已处理，当前席位不再有效。")
            if invite["status"] != "pending":
                raise HTTPException(409, "该邀请已失效或已处理。")
            now = utcnow()
            if invite["expires_at"] <= now:
                db.execute(
                    "UPDATE room_invites SET status='expired',responded_at=?,updated_at=? WHERE id=?",
                    (now, now, invite_id),
                )
                notify_users.update((int(invite["inviter_user_id"]), int(user["id"])))
                expired = True
            elif not friendship_exists(db, invite["inviter_user_id"], user["id"]):
                db.execute(
                    "UPDATE room_invites SET status='cancelled',responded_at=?,updated_at=? WHERE id=?",
                    (now, now, invite_id),
                )
                notify_users.update((int(invite["inviter_user_id"]), int(user["id"])))
            else:
                room = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
                if not room:
                    db.execute(
                        "UPDATE room_invites SET status='cancelled',responded_at=?,updated_at=? WHERE id=?",
                        (now, now, invite_id),
                    )
                    notify_users.update((int(invite["inviter_user_id"]), int(user["id"])))
                    expired = True
                else:
                    validate_discovery_join(db, room, user, invite["role"])
                    joined = join_room_seat(
                        db, room, user, invite["role"], allow_frozen=False, source="friend_invite"
                    )
                    db.execute(
                        "UPDATE room_invites SET status='accepted',responded_at=?,updated_at=? WHERE id=? AND status='pending'",
                        (now, now, invite_id),
                    )
                    notify_users.add(int(invite["inviter_user_id"]))
                    notify_users.add(int(user["id"]))
        if expired:
            for target_id in sorted(notify_users):
                await hub.notify_account(target_id, {"type": "room_invite_updated", "invite_id": invite_id})
            raise HTTPException(409, "邀请已过期或房间已不可加入。")
        if joined is None:
            for target_id in sorted(notify_users):
                await hub.notify_account(target_id, {"type": "room_invite_updated", "invite_id": invite_id})
            raise HTTPException(409, "好友关系已解除，邀请已失效。")
        await hub.notify(room_id, "judge_added" if joined["role"] == "judge" else "joined")
        for target_id in sorted(notify_users):
            await hub.notify_account(target_id, {"type": "room_invite_updated", "invite_id": invite_id})
        return {"ok": True, **joined}

    @app.post("/api/room-invites/{invite_id}/reject")
    async def reject_room_invite(invite_id: int, request: Request):
        inviter_id = None
        expired = False
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            invite = db.execute(
                "SELECT inviter_user_id,status,expires_at FROM room_invites WHERE id=? AND invitee_user_id=?",
                (invite_id, user["id"]),
            ).fetchone()
            if not invite:
                raise HTTPException(404, "邀请不存在。")
            inviter_id = int(invite["inviter_user_id"])
            if invite["status"] == "rejected":
                return {"ok": True}
            if invite["status"] != "pending":
                raise HTTPException(409, "该邀请已处理。")
            now = utcnow()
            if invite["expires_at"] <= now:
                db.execute(
                    "UPDATE room_invites SET status='expired',responded_at=?,updated_at=? WHERE id=?",
                    (now, now, invite_id),
                )
                expired = True
            else:
                db.execute(
                    "UPDATE room_invites SET status='rejected',responded_at=?,updated_at=? WHERE id=?",
                    (now, now, invite_id),
                )
        if expired:
            raise HTTPException(409, "邀请已过期。")
        await hub.notify_account(inviter_id, {"type": "room_invite_updated", "invite_id": invite_id})
        await hub.notify_account(user["id"], {"type": "room_invite_updated", "invite_id": invite_id})
        return {"ok": True}

    @app.post("/api/room-invites/{invite_id}/cancel")
    async def cancel_room_invite(invite_id: int, request: Request):
        invitee_id = None
        expired = False
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            invite = db.execute(
                "SELECT * FROM room_invites WHERE id=?",
                (invite_id,),
            ).fetchone()
            if not invite:
                raise HTTPException(404, "邀请不存在。")
            if int(invite["inviter_user_id"]) != int(user["id"]):
                require_room_manager(db, invite["room_id"], user)
            invitee_id = int(invite["invitee_user_id"])
            if invite["status"] == "cancelled":
                return {"ok": True}
            if invite["status"] != "pending":
                raise HTTPException(409, "该邀请已处理。")
            now = utcnow()
            if invite["expires_at"] <= now:
                db.execute(
                    "UPDATE room_invites SET status='expired',responded_at=?,updated_at=? WHERE id=?",
                    (now, now, invite_id),
                )
                expired = True
            else:
                db.execute(
                    "UPDATE room_invites SET status='cancelled',responded_at=?,updated_at=? WHERE id=?",
                    (now, now, invite_id),
                )
        if expired:
            raise HTTPException(409, "邀请已过期。")
        await hub.notify_account(invitee_id, {"type": "room_invite_updated", "invite_id": invite_id})
        await hub.notify_account(user["id"], {"type": "room_invite_updated", "invite_id": invite_id})
        return {"ok": True}

    @app.get("/api/me/social")
    def my_social(request: Request):
        with connection() as db:
            user = caller(request, db)
            return _social_summary(db, user["id"], user_uid, points_state, utcnow())

    @app.get("/api/users/search")
    def search_user(request: Request, uid: str = Query(..., min_length=1, max_length=40)):
        with connection(immediate=True) as db:
            user = caller(request, db)
            _rate_limit(db, user["id"], "uid_search", 60, 60)
            target = lookup_account(db, uid=uid)
            if not target:
                raise HTTPException(404, "未找到该 UID，或账号已删除。")
            if int(target["id"]) == int(user["id"]):
                raise HTTPException(409, "不能搜索或添加自己的账号。")
            target_id = int(target["id"])
            if friendship_exists(db, user["id"], target_id):
                status = "friend"
                request_id = None
            else:
                incoming = db.execute(
                    """SELECT id FROM friend_requests
                       WHERE sender_user_id=? AND recipient_user_id=? AND status='pending'""",
                    (target_id, user["id"]),
                ).fetchone()
                outgoing = db.execute(
                    """SELECT id FROM friend_requests
                       WHERE sender_user_id=? AND recipient_user_id=? AND status='pending'""",
                    (user["id"], target_id),
                ).fetchone()
                if incoming:
                    status, request_id = "incoming", int(incoming["id"])
                elif outgoing:
                    status, request_id = "outgoing", int(outgoing["id"])
                else:
                    status, request_id = "none", None
            points = points_state(db, target_id, utcnow())
            return {
                "uid": user_uid(target_id),
                "display_name": target["display_name"],
                "status": status,
                "request_id": request_id,
                "total_points": points["total_points"],
                "current_streak": points["current_streak"],
            }

    @app.post("/api/friends/requests")
    async def send_friend_request(body: FriendRequestData, request: Request):
        recipient_id = None
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            target = lookup_account(db, uid=body.uid)
            if not target:
                raise HTTPException(404, "未找到该 UID，或账号已删除。")
            recipient_id = int(target["id"])
            if recipient_id == int(user["id"]):
                raise HTTPException(409, "不能添加自己为好友。")
            if friendship_exists(db, user["id"], recipient_id):
                raise HTTPException(409, "你们已经是好友。")
            _rate_limit(db, user["id"], "friend_request", 20, 3600)
            reverse = db.execute(
                """SELECT id FROM friend_requests
                   WHERE sender_user_id=? AND recipient_user_id=? AND status='pending'""",
                (recipient_id, user["id"]),
            ).fetchone()
            if reverse:
                raise HTTPException(409, "对方已向你发送好友申请，请在收到的申请中接受。")
            existing = db.execute(
                "SELECT id,status FROM friend_requests WHERE sender_user_id=? AND recipient_user_id=?",
                (user["id"], recipient_id),
            ).fetchone()
            now = utcnow()
            if existing and existing["status"] == "pending":
                raise HTTPException(409, "好友申请已经发送，请等待对方处理。")
            if existing:
                db.execute(
                    """UPDATE friend_requests SET status='pending',created_at=?,updated_at=?
                       WHERE id=?""",
                    (now, now, existing["id"]),
                )
                request_id = int(existing["id"])
            else:
                cur = db.execute(
                    """INSERT INTO friend_requests(
                        sender_user_id,recipient_user_id,status,created_at,updated_at
                    ) VALUES(?,?,'pending',?,?)""",
                    (user["id"], recipient_id, now, now),
                )
                request_id = int(cur.lastrowid)
        await hub.notify_account(recipient_id, {"type": "social_updated"})
        return {"ok": True, "request_id": request_id}

    @app.post("/api/friends/requests/{request_id}/accept")
    async def accept_friend_request(request_id: int, request: Request):
        sender_id = None
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            row = db.execute(
                "SELECT * FROM friend_requests WHERE id=? AND recipient_user_id=?",
                (request_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(404, "好友申请不存在。")
            sender_id = int(row["sender_user_id"])
            if row["status"] == "accepted" and friendship_exists(db, user["id"], sender_id):
                return {"ok": True, "already_friends": True}
            if row["status"] != "pending":
                raise HTTPException(409, "该好友申请已处理。")
            sender = db.execute(
                "SELECT id,display_name,deleted_at FROM users WHERE id=?", (sender_id,)
            ).fetchone()
            recipient = db.execute(
                "SELECT id,display_name,deleted_at FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            if not sender or sender["deleted_at"] is not None:
                raise HTTPException(404, "申请账号已删除。")
            low, high = _pair(user["id"], sender_id)
            now = utcnow()
            db.execute(
                "INSERT OR IGNORE INTO friendships(user_low_id,user_high_id,created_at) VALUES(?,?,?)",
                (low, high, now),
            )
            db.execute(
                "INSERT OR IGNORE INTO dm_conversations(user_low_id,user_high_id,created_at) VALUES(?,?,?)",
                (low, high, now),
            )
            db.execute(
                """INSERT OR IGNORE INTO friend_notes(owner_user_id,friend_user_id,note,updated_at)
                   VALUES(?,?,?,?)""",
                (user["id"], sender_id, sender["display_name"], now),
            )
            db.execute(
                """INSERT OR IGNORE INTO friend_notes(owner_user_id,friend_user_id,note,updated_at)
                   VALUES(?,?,?,?)""",
                (sender_id, user["id"], recipient["display_name"], now),
            )
            db.execute(
                "UPDATE friend_requests SET status='accepted',updated_at=? WHERE id=?",
                (now, request_id),
            )
        await hub.notify_account(sender_id, {"type": "social_updated"})
        await hub.notify_account(user["id"], {"type": "social_updated"})
        return {"ok": True}

    @app.post("/api/friends/requests/{request_id}/reject")
    async def reject_friend_request(request_id: int, request: Request):
        sender_id = None
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            row = db.execute(
                "SELECT sender_user_id,status FROM friend_requests WHERE id=? AND recipient_user_id=?",
                (request_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(404, "好友申请不存在。")
            sender_id = int(row["sender_user_id"])
            if row["status"] == "rejected":
                return {"ok": True}
            if row["status"] != "pending":
                raise HTTPException(409, "该好友申请已处理。")
            db.execute(
                "UPDATE friend_requests SET status='rejected',updated_at=? WHERE id=?",
                (utcnow(), request_id),
            )
        await hub.notify_account(sender_id, {"type": "social_updated"})
        return {"ok": True}

    @app.post("/api/friends/requests/{request_id}/withdraw")
    async def withdraw_friend_request(request_id: int, request: Request):
        recipient_id = None
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            row = db.execute(
                "SELECT recipient_user_id,status FROM friend_requests WHERE id=? AND sender_user_id=?",
                (request_id, user["id"]),
            ).fetchone()
            if not row:
                raise HTTPException(404, "好友申请不存在。")
            recipient_id = int(row["recipient_user_id"])
            if row["status"] == "withdrawn":
                return {"ok": True}
            if row["status"] != "pending":
                raise HTTPException(409, "该好友申请已处理。")
            db.execute(
                "UPDATE friend_requests SET status='withdrawn',updated_at=? WHERE id=?",
                (utcnow(), request_id),
            )
        await hub.notify_account(recipient_id, {"type": "social_updated"})
        return {"ok": True}

    @app.delete("/api/friends/{uid}")
    async def delete_friend(uid: str, body: FriendDeleteData, request: Request):
        if not body.confirm:
            raise HTTPException(422, "请确认删除好友。")
        target_id = uid_to_id(uid, user_uid)
        if target_id is None:
            raise HTTPException(404, "好友不存在。")
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            if not friendship_exists(db, user["id"], target_id):
                raise HTTPException(404, "好友关系不存在。")
            low, high = _pair(user["id"], target_id)
            db.execute(
                "DELETE FROM friendships WHERE user_low_id=? AND user_high_id=?",
                (low, high),
            )
            db.execute(
                """DELETE FROM friend_notes
                   WHERE (owner_user_id=? AND friend_user_id=?)
                      OR (owner_user_id=? AND friend_user_id=?)""",
                (user["id"], target_id, target_id, user["id"]),
            )
            now = utcnow()
            db.execute(
                """UPDATE room_invites SET status='cancelled',responded_at=?,updated_at=?
                   WHERE status='pending' AND
                     ((inviter_user_id=? AND invitee_user_id=?) OR
                      (inviter_user_id=? AND invitee_user_id=?))""",
                (now, now, user["id"], target_id, target_id, user["id"]),
            )
        await hub.notify_account(target_id, {"type": "social_updated"})
        await hub.notify_account(user["id"], {"type": "social_updated"})
        return {"ok": True, "history_retained": True}

    @app.put("/api/friends/{uid}/note")
    def update_friend_note(uid: str, body: FriendNoteData, request: Request):
        target_id = uid_to_id(uid, user_uid)
        if target_id is None:
            raise HTTPException(404, "好友不存在。")
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            if not friendship_exists(db, user["id"], target_id):
                raise HTTPException(404, "好友关系不存在。")
            target = db.execute(
                "SELECT display_name,deleted_at FROM users WHERE id=?", (target_id,)
            ).fetchone()
            if not target or target["deleted_at"] is not None:
                raise HTTPException(404, "好友账号已删除。")
            if body.reset_default:
                note = target["display_name"]
            else:
                if body.note is None:
                    raise HTTPException(422, "请输入好友备注，或选择恢复默认。")
                note = norm_text(body.note, 40)
            now = utcnow()
            db.execute(
                """INSERT INTO friend_notes(owner_user_id,friend_user_id,note,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(owner_user_id,friend_user_id)
                   DO UPDATE SET note=excluded.note,updated_at=excluded.updated_at""",
                (user["id"], target_id, note, now),
            )
        return {"ok": True, "note": note}

    @app.get("/api/messages/{uid}")
    def list_direct_messages(
        uid: str,
        request: Request,
        before_id: Optional[int] = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
    ):
        other_id = uid_to_id(uid, user_uid)
        if other_id is None:
            raise HTTPException(404, "会话不存在。")
        with connection() as db:
            user = caller(request, db)
            conv = conversation_row(db, user["id"], other_id)
            if not conv:
                raise HTTPException(404, "会话不存在。")
            query = """SELECT m.id,m.sender_user_id,m.recipient_user_id,m.text,m.created_at,
                              su.display_name AS sender_name,su.deleted_at AS sender_deleted
                       FROM dm_messages m JOIN users su ON su.id=m.sender_user_id
                       WHERE m.conversation_id=?"""
            args = [conv["id"]]
            if before_id is not None:
                query += " AND m.id<?"
                args.append(before_id)
            query += " ORDER BY m.id DESC LIMIT ?"
            args.append(limit + 1)
            rows = db.execute(query, args).fetchall()
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
            messages = [
                {
                    "id": int(row["id"]),
                    "sender_uid": user_uid(int(row["sender_user_id"])),
                    "recipient_uid": user_uid(int(row["recipient_user_id"])),
                    "sender_name": "账号已删除" if row["sender_deleted"] is not None else row["sender_name"],
                    "mine": int(row["sender_user_id"]) == int(user["id"]),
                    "text": row["text"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
            other = db.execute(
                "SELECT display_name,deleted_at FROM users WHERE id=?", (other_id,)
            ).fetchone()
            active_friend = bool(
                other and other["deleted_at"] is None and friendship_exists(db, user["id"], other_id)
            )
            last_received = db.execute(
                """SELECT MAX(id) FROM dm_messages
                   WHERE conversation_id=? AND recipient_user_id=?""",
                (conv["id"], user["id"]),
            ).fetchone()[0]
            return {
                "conversation_id": int(conv["id"]),
                "other_uid": user_uid(other_id),
                "other_name": "账号已删除" if not other or other["deleted_at"] is not None else other["display_name"],
                "can_send": active_friend,
                "last_received_message_id": int(last_received) if last_received is not None else None,
                "messages": messages,
                "has_more": has_more,
                "next_before_id": messages[0]["id"] if messages and has_more else None,
            }

    @app.post("/api/messages/{uid}")
    async def send_direct_message(uid: str, body: DirectMessageData, request: Request):
        other_id = uid_to_id(uid, user_uid)
        if other_id is None:
            raise HTTPException(404, "好友不存在。")
        notify_recipient = None
        notify_sender = None
        payload = None
        duplicate = False
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            other = db.execute(
                "SELECT id,display_name,deleted_at FROM users WHERE id=?", (other_id,)
            ).fetchone()
            if not other:
                raise HTTPException(404, "账号不存在。")
            conv = conversation_row(db, user["id"], other_id)
            client_id = norm_text(body.client_message_id, 128)
            existing = db.execute(
                "SELECT * FROM dm_messages WHERE sender_user_id=? AND client_message_id=?",
                (user["id"], client_id),
            ).fetchone()
            if existing:
                if not conv or int(existing["conversation_id"]) != int(conv["id"]) or int(existing["recipient_user_id"]) != other_id:
                    raise HTTPException(409, "消息请求标识已用于其他会话。")
                duplicate = True
                message_row = existing
            else:
                if other["deleted_at"] is not None or not friendship_exists(db, user["id"], other_id):
                    raise HTTPException(403, "只有当前好友可以互发私信。")
                if not conv:
                    raise HTTPException(404, "好友会话不存在，请刷新好友列表。")
                _rate_limit(db, user["id"], "direct_message", 60, 60)
                message_text = norm_text(body.text, 2000)
                created = utcnow()
                cur = db.execute(
                    """INSERT INTO dm_messages(
                        conversation_id,sender_user_id,recipient_user_id,client_message_id,text,created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (conv["id"], user["id"], other_id, client_id, message_text, created),
                )
                message_row = db.execute(
                    "SELECT * FROM dm_messages WHERE id=?", (cur.lastrowid,)
                ).fetchone()
                notify_recipient = other_id
                notify_sender = int(user["id"])
            sender = db.execute(
                "SELECT display_name,deleted_at FROM users WHERE id=?", (message_row["sender_user_id"],)
            ).fetchone()
            payload = {
                "id": int(message_row["id"]),
                "sender_uid": user_uid(int(message_row["sender_user_id"])),
                "recipient_uid": user_uid(int(message_row["recipient_user_id"])),
                "sender_name": "账号已删除" if not sender or sender["deleted_at"] is not None else sender["display_name"],
                "text": message_row["text"],
                "created_at": message_row["created_at"],
            }
        sender_payload = {**payload, "mine": True}
        if not duplicate:
            recipient_payload = {**payload, "mine": False}
            await hub.notify_account(
                notify_recipient,
                {"type": "dm_message", "other_uid": user_uid(notify_sender), "message": recipient_payload},
            )
            await hub.notify_account(
                notify_sender,
                {"type": "dm_message", "other_uid": user_uid(notify_recipient), "message": sender_payload},
            )
        return {"ok": True, "duplicate": duplicate, "message": sender_payload}

    @app.post("/api/messages/{uid}/read")
    async def mark_direct_messages_read(uid: str, body: ReadPositionData, request: Request):
        other_id = uid_to_id(uid, user_uid)
        if other_id is None:
            raise HTTPException(404, "会话不存在。")
        with connection(immediate=True) as db:
            user = caller(request, db, mutate=True)
            conv = conversation_row(db, user["id"], other_id)
            if not conv:
                raise HTTPException(404, "会话不存在。")
            message_id = int(body.last_read_message_id)
            if message_id:
                row = db.execute(
                    "SELECT id FROM dm_messages WHERE id=? AND conversation_id=? AND recipient_user_id=?",
                    (message_id, conv["id"], user["id"]),
                ).fetchone()
                if not row:
                    raise HTTPException(404, "已读位置不属于当前收件人的消息。")
            now = utcnow()
            db.execute(
                """INSERT INTO dm_read_positions(conversation_id,user_id,last_read_message_id,updated_at)
                   VALUES(?,?,?,?)
                   ON CONFLICT(conversation_id,user_id)
                   DO UPDATE SET last_read_message_id=MAX(last_read_message_id,excluded.last_read_message_id),
                                 updated_at=excluded.updated_at""",
                (conv["id"], user["id"], message_id, now),
            )
            stored = db.execute(
                "SELECT last_read_message_id FROM dm_read_positions WHERE conversation_id=? AND user_id=?",
                (conv["id"], user["id"]),
            ).fetchone()
            effective_id = int(stored["last_read_message_id"]) if stored else message_id
            unread = db.execute(
                """SELECT COUNT(*) FROM dm_messages
                   WHERE conversation_id=? AND recipient_user_id=? AND id>?""",
                (conv["id"], user["id"], effective_id),
            ).fetchone()[0]
            user_id = int(user["id"])
        await hub.notify_account(user_id, {"type": "social_updated"})
        return {"ok": True, "unread_count": int(unread)}

    @app.get("/api/me/points/ledger")
    def my_points_ledger(
        request: Request,
        before_id: Optional[int] = Query(default=None, ge=1),
        limit: int = Query(default=20, ge=1, le=50),
    ):
        with connection() as db:
            user = caller(request, db)
            query = """SELECT id,room_id,match_number,outcome,delta,points_after,streak_after,settled_at
                       FROM points_ledger WHERE user_id=?"""
            args = [user["id"]]
            if before_id is not None:
                query += " AND id<?"
                args.append(before_id)
            query += " ORDER BY id DESC LIMIT ?"
            args.append(limit + 1)
            rows = db.execute(query, args).fetchall()
            has_more = len(rows) > limit
            rows = rows[:limit]
            return {
                "entries": [dict(row) for row in rows],
                "has_more": has_more,
                "next_before_id": int(rows[-1]["id"]) if rows and has_more else None,
            }

    @app.websocket("/ws/messages")
    async def direct_message_socket(websocket: WebSocket):
        if not check_origin(websocket.headers):
            await websocket.close(code=1008)
            return
        try:
            with connection() as db:
                user = auth(db, websocket.cookies.get(cookie))
        except HTTPException:
            await websocket.close(code=1008)
            return
        user_id = int(user["id"])
        await websocket.accept()
        clients = hub.account_groups.setdefault(user_id, set())
        clients.add(websocket)
        try:
            await websocket.send_json({"type": "connected"})
            while True:
                raw = await websocket.receive_text()
                if len(raw) > 128:
                    continue
                # The account channel is server-push only; client messages are ignored.
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)
            if not clients and hub.account_groups.get(user_id) is clients:
                hub.account_groups.pop(user_id, None)

