"""网页对局模块的 JSON API，由主 FastAPI 应用挂载到 /game/api。"""
from __future__ import annotations

import hashlib
import json
import os
import random
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def now_ms():
    return int(time.time() * 1000)


def hash_pwd(pwd, salt):
    return hashlib.sha256((salt + pwd).encode("utf-8")).hexdigest()


def next_uid(users):
    i = len(users) + 1
    while True:
        uid = "TL%08d" % i
        if uid not in users:
            return uid
        i += 1


def new_code(rooms):
    for _ in range(500):
        code = "R" + "".join(random.choice(CODE_ALPHABET) for _ in range(6))
        if code not in rooms:
            return code
    raise RuntimeError("邀请码空间耗尽")


def default_room(code, name, mode, owner, owner_name):
    return {
        "id": code,
        "name": name,
        "invite": code,
        "owner": owner,
        "ownerName": owner_name,
        "mode": mode if mode in ("classic", "discern") else "classic",
        "status": "ready",
        "round": 0,
        "startedAt": None,
        "roundStart": None,
        "players": [{"uid": owner, "name": owner_name, "isAI": False, "isOwner": True}] if owner else [],
        "judges": [],
        "aiJudges": [],
        "questions": {},
        "questionImages": {},
        "answers": {},
        "pendingQ": [],
        "roundSources": {},
        "finalJudgments": {},
        "items": [],
        "votes": {},
        "aiVerdicts": {},
        "scores": {},
        "revealed": False,
        "revealResult": None,
        "createdAt": now_ms(),
    }


def room_summary(room):
    keys = ("id", "name", "invite", "owner", "ownerName", "mode", "status",
            "round", "revealed", "createdAt", "players", "judges", "aiJudges")
    return {k: room.get(k) for k in keys}


def merge_room(old, new):
    """把客户端整间房间合并进服务端，避免多人同时操作时互相覆盖。"""
    r = dict(old)

    if new.get("_resetGame"):
        r.update({
            "questions": {}, "questionImages": {}, "answers": {}, "pendingQ": [],
            "roundSources": {}, "finalJudgments": {}, "round": 0,
            "revealed": False, "revealResult": None, "startedAt": None,
            "roundStart": None, "status": "ready",
        })
        for j in r.get("judges") or []:
            j["locked"] = False

    if new.get("_clearItems"):
        r.update({"items": [], "votes": {}, "aiVerdicts": {}, "scores": {}, "revealed": False})

    for k in ("name", "mode", "status", "round", "startedAt", "roundStart",
              "revealed", "revealResult", "owner", "ownerName", "invite"):
        if k in new:
            r[k] = new[k]

    def merge_list(key, id_key):
        cur = {}
        for x in r.get(key) or []:
            if isinstance(x, dict) and x.get(id_key) is not None:
                cur[x[id_key]] = x
        for x in new.get(key) or []:
            if not isinstance(x, dict):
                continue
            k = x.get(id_key)
            if k is None:
                continue
            if k in cur:
                cur[k].update(x)
            else:
                cur[k] = x
        r[key] = list(cur.values())

    merge_list("players", "uid")
    merge_list("judges", "uid")
    merge_list("items", "id")

    if "aiJudges" in new:
        r["aiJudges"] = new["aiJudges"]
    for key in ("aiVerdicts", "scores", "finalJudgments", "questionImages", "questions"):
        if key in new:
            r[key] = new[key]

    votes = dict(r.get("votes") or {})
    for iid, m in (new.get("votes") or {}).items():
        votes[iid] = dict(votes.get(iid) or {})
        votes[iid].update(m or {})
    r["votes"] = votes

    ans = dict(r.get("answers") or {})
    for rnd, m in (new.get("answers") or {}).items():
        ans[rnd] = dict(ans.get(rnd) or {})
        ans[rnd].update(m or {})
    r["answers"] = ans

    if "pendingQ" in new and not new.get("_resetGame"):
        r["pendingQ"] = new["pendingQ"]

    for uid in new.get("_removeFromPlayers") or []:
        r["players"] = [p for p in r.get("players") or [] if p.get("uid") != uid]
    for uid in new.get("_removeFromJudges") or []:
        r["judges"] = [j for j in r.get("judges") or [] if j.get("uid") != uid]

    for flag in ("_resetGame", "_clearItems", "_removeFromPlayers", "_removeFromJudges"):
        r.pop(flag, None)
    return r


class GameStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.state = {"users": {}, "rooms": {}, "chats": {}}
        self.load()

    def load(self):
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self.state = {
                "users": data.get("users") or {},
                "rooms": data.get("rooms") or {},
                "chats": data.get("chats") or {},
            }
        except Exception:
            self.state = {"users": {}, "rooms": {}, "chats": {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False)
        os.replace(tmp, self.path)


def api_register(store, body):
    name = (body.get("name") or "").strip()
    pwd = body.get("pwd") or ""
    if not name or len(name) > 40:
        return 400, {"error": "用户名 1~40 字符"}
    if len(pwd) < 8:
        return 400, {"error": "密码至少 8 位"}
    for u in store.state["users"].values():
        if u.get("name") == name:
            return 409, {"error": "用户名已被占用"}
    uid = next_uid(store.state["users"])
    salt = secrets.token_hex(8)
    store.state["users"][uid] = {
        "uid": uid, "name": name, "salt": salt,
        "hash": hash_pwd(pwd, salt), "createdAt": now_ms(),
    }
    store.save()
    return 200, {"uid": uid, "name": name}


def api_login(store, body):
    name = (body.get("name") or "").strip()
    pwd = body.get("pwd") or ""
    for uid, u in store.state["users"].items():
        if u.get("name") == name:
            if u.get("hash") == hash_pwd(pwd, u.get("salt", "")):
                return 200, {"uid": uid, "name": name}
            return 401, {"error": "密码错误"}
    return 404, {"error": "账号不存在"}


def api_profile(store, body):
    uid = body.get("uid") or ""
    name = (body.get("name") or "").strip()
    if not name or len(name) > 40:
        return 400, {"error": "用户名 1~40 字符"}
    if uid not in store.state["users"]:
        return 401, {"error": "账号不存在"}
    for k, u in store.state["users"].items():
        if k != uid and u.get("name") == name:
            return 409, {"error": "用户名已被占用"}
    store.state["users"][uid]["name"] = name
    store.save()
    return 200, {"ok": True}


def api_change_pwd(store, body):
    uid = body.get("uid") or ""
    old = body.get("oldPwd") or ""
    new = body.get("newPwd") or ""
    u = store.state["users"].get(uid)
    if not u:
        return 401, {"error": "账号不存在"}
    if u.get("hash") != hash_pwd(old, u.get("salt", "")):
        return 401, {"error": "当前密码错误"}
    if len(new) < 8 or len(new) > 200:
        return 400, {"error": "新密码 8~200 位"}
    u["salt"] = secrets.token_hex(8)
    u["hash"] = hash_pwd(new, u["salt"])
    store.save()
    return 200, {"ok": True}


def api_create_room(store, body):
    name = (body.get("name") or "").strip()
    owner = body.get("owner") or ""
    owner_name = (body.get("ownerName") or "").strip() or "房主"
    if not name:
        return 400, {"error": "请填写比赛名称"}
    if owner and owner not in store.state["users"]:
        return 401, {"error": "账号不存在，请重新登录"}
    code = new_code(store.state["rooms"])
    room = default_room(code, name, body.get("mode") or "classic", owner, owner_name)
    store.state["rooms"][code] = room
    store.save()
    return 200, {"room": room}


def api_join(store, body):
    code = (body.get("code") or "").strip().upper()
    uid = body.get("uid") or ""
    name = (body.get("name") or "").strip() or "玩家"
    role = body.get("role") or "player"
    room = store.state["rooms"].get(code)
    if not room:
        return 404, {"error": "房间不存在，请核对邀请码"}
    if not uid:
        return 401, {"error": "请先登录"}
    players = room.setdefault("players", [])
    judges = room.setdefault("judges", [])
    if role == "judge":
        players[:] = [p for p in players if p.get("uid") != uid or p.get("isAI")]
        if not any(j.get("uid") == uid for j in judges):
            judges.append({"uid": uid, "name": name, "locked": False,
                           "joinedAt": now_ms(), "isOwner": room.get("owner") == uid})
    else:
        judges[:] = [j for j in judges if j.get("uid") != uid]
        if not any(p.get("uid") == uid and not p.get("isAI") for p in players):
            players.append({"uid": uid, "name": name, "isAI": False,
                            "isOwner": room.get("owner") == uid})
    store.save()
    return 200, {"room": room}


def api_save_room(store, body):
    incoming = body.get("room") or {}
    code = incoming.get("id")
    if not code:
        return 400, {"error": "缺少房间号"}
    old = store.state["rooms"].get(code)
    if not old:
        return 404, {"error": "房间不存在"}
    store.state["rooms"][code] = merge_room(old, incoming)
    store.save()
    return 200, {"ok": True}


def api_delete_room(store, body):
    code = body.get("code")
    uid = body.get("uid")
    room = store.state["rooms"].get(code)
    if not room:
        return 404, {"error": "房间不存在"}
    if room.get("owner") != uid:
        return 403, {"error": "仅房主可删除房间"}
    store.state["rooms"].pop(code, None)
    store.state["chats"].pop(code, None)
    store.save()
    return 200, {"ok": True}


def api_set_chat(store, body):
    code = body.get("code")
    if not code:
        return 400, {"error": "缺少房间号"}
    store.state["chats"][code] = body.get("chat") or []
    store.save()
    return 200, {"ok": True}


def dispatch(store, method, path, body, query):
    if path == "/api/health":
        return 200, {"ok": True, "rooms": len(store.state["rooms"]), "users": len(store.state["users"])}

    if path == "/api/register" and method == "POST":
        return api_register(store, body)
    if path == "/api/login" and method == "POST":
        return api_login(store, body)
    if path == "/api/profile" and method == "POST":
        return api_profile(store, body)
    if path == "/api/changePwd" and method == "POST":
        return api_change_pwd(store, body)
    if path == "/api/createRoom" and method == "POST":
        return api_create_room(store, body)
    if path == "/api/join" and method == "POST":
        return api_join(store, body)
    if path == "/api/room" and method == "POST":
        return api_save_room(store, body)
    if path == "/api/deleteRoom" and method == "POST":
        return api_delete_room(store, body)
    if path == "/api/chat" and method == "POST":
        return api_set_chat(store, body)

    if path == "/api/room" and method == "GET":
        code = (query.get("code") or [""])[0].strip().upper()
        room = store.state["rooms"].get(code)
        return (200, {"room": room}) if room else (404, {"error": "房间不存在"})

    if path == "/api/rooms" and method == "GET":
        uid = (query.get("uid") or [""])[0]
        rooms = []
        for r in store.state["rooms"].values():
            if (not uid or r.get("owner") == uid
                    or any(p.get("uid") == uid for p in r.get("players") or [])
                    or any(j.get("uid") == uid for j in r.get("judges") or [])):
                rooms.append(room_summary(r))
        rooms.sort(key=lambda x: x.get("createdAt") or 0, reverse=True)
        return 200, {"rooms": rooms}

    if path == "/api/leaderboard" and method == "GET":
        users = store.state["users"]
        board = {}
        for room in store.state["rooms"].values():
            names = {
                p.get("uid"): p.get("name")
                for p in (room.get("players") or [])
                if isinstance(p, dict) and p.get("uid")
            }
            for uid, score in (room.get("scores") or {}).items():
                if not isinstance(uid, str) or not uid:
                    continue
                try:
                    points = float(score)
                except (TypeError, ValueError):
                    continue
                item = board.setdefault(uid, {"uid": uid, "name": "", "score": 0.0, "games": 0})
                item["score"] += points
                item["games"] += 1
                name = names.get(uid) or (users.get(uid) or {}).get("name")
                if name:
                    item["name"] = name
        rows = sorted(board.values(), key=lambda x: (-x["score"], x["uid"]))[:50]
        for row in rows:
            row["score"] = round(row["score"], 1)
            row["name"] = row["name"] or row["uid"]
        return 200, {"leaderboard": rows}

    if path == "/api/chat" and method == "GET":
        code = (query.get("code") or [""])[0].strip().upper()
        return 200, {"chat": store.state["chats"].get(code, [])}

    return 404, {"error": "接口不存在"}


def create_game_router(data_file):
    store = GameStore(data_file)
    router = APIRouter()

    @router.api_route("/{endpoint:path}", methods=["GET", "POST"], include_in_schema=False)
    async def game_endpoint(endpoint: str, request: Request):
        body = {}
        if request.method == "POST":
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            if not isinstance(body, dict):
                body = {}
        with store.lock:
            status, payload = dispatch(
                store, request.method, "/api/" + endpoint, body, parse_qs(request.url.query)
            )
        return JSONResponse(payload, status_code=status)

    return router
