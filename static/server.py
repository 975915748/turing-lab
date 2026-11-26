#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图灵实验室 Turing Lab —— 零依赖联机后端
------------------------------------------------
仅使用 Python 标准库，无需 pip 安装任何依赖。

启动：
    python server.py            # 默认 8000 端口
    python server.py 8080       # 指定端口

联机：
    房主启动后，所有设备访问  http://房主局域网IP:8000
    邀请码由服务器生成并保证全局唯一。

离线：
    不启动本服务、直接双击 index.html 时，前端自动降级为本地 localStorage 模式。
"""
import hashlib
import json
import os
import random
import secrets
import socket
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT, "data.json")
LOCK = threading.RLock()
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

STATE = {"users": {}, "rooms": {}, "chats": {}}


def now_ms():
    return int(time.time() * 1000)


def load_state():
    global STATE
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        STATE = {
            "users": data.get("users") or {},
            "rooms": data.get("rooms") or {},
            "chats": data.get("chats") or {},
        }
    except Exception:
        STATE = {"users": {}, "rooms": {}, "chats": {}}


def save_state():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)


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


# ---------------- API 实现 ----------------

def api_register(body):
    name = (body.get("name") or "").strip()
    pwd = body.get("pwd") or ""
    if not name or len(name) > 40:
        return 400, {"error": "用户名 1~40 字符"}
    if len(pwd) < 8:
        return 400, {"error": "密码至少 8 位"}
    for u in STATE["users"].values():
        if u.get("name") == name:
            return 409, {"error": "用户名已被占用"}
    uid = next_uid(STATE["users"])
    salt = secrets.token_hex(8)
    STATE["users"][uid] = {
        "uid": uid, "name": name, "salt": salt,
        "hash": hash_pwd(pwd, salt), "createdAt": now_ms(),
    }
    save_state()
    return 200, {"uid": uid, "name": name}


def api_login(body):
    name = (body.get("name") or "").strip()
    pwd = body.get("pwd") or ""
    for uid, u in STATE["users"].items():
        if u.get("name") == name:
            if u.get("hash") == hash_pwd(pwd, u.get("salt", "")):
                return 200, {"uid": uid, "name": name}
            return 401, {"error": "密码错误"}
    return 404, {"error": "账号不存在"}


def api_profile(body):
    uid = body.get("uid") or ""
    name = (body.get("name") or "").strip()
    if not name or len(name) > 40:
        return 400, {"error": "用户名 1~40 字符"}
    if uid not in STATE["users"]:
        return 401, {"error": "账号不存在"}
    for k, u in STATE["users"].items():
        if k != uid and u.get("name") == name:
            return 409, {"error": "用户名已被占用"}
    STATE["users"][uid]["name"] = name
    save_state()
    return 200, {"ok": True}


def api_change_pwd(body):
    uid = body.get("uid") or ""
    old = body.get("oldPwd") or ""
    new = body.get("newPwd") or ""
    u = STATE["users"].get(uid)
    if not u:
        return 401, {"error": "账号不存在"}
    if u.get("hash") != hash_pwd(old, u.get("salt", "")):
        return 401, {"error": "当前密码错误"}
    if len(new) < 8 or len(new) > 200:
        return 400, {"error": "新密码 8~200 位"}
    u["salt"] = secrets.token_hex(8)
    u["hash"] = hash_pwd(new, u["salt"])
    save_state()
    return 200, {"ok": True}


def api_create_room(body):
    name = (body.get("name") or "").strip()
    owner = body.get("owner") or ""
    owner_name = (body.get("ownerName") or "").strip() or "房主"
    if not name:
        return 400, {"error": "请填写比赛名称"}
    if owner and owner not in STATE["users"]:
        return 401, {"error": "账号不存在，请重新登录"}
    code = new_code(STATE["rooms"])
    room = default_room(code, name, body.get("mode") or "classic", owner, owner_name)
    STATE["rooms"][code] = room
    save_state()
    return 200, {"room": room}


def api_join(body):
    code = (body.get("code") or "").strip().upper()
    uid = body.get("uid") or ""
    name = (body.get("name") or "").strip() or "玩家"
    role = body.get("role") or "player"
    room = STATE["rooms"].get(code)
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
    save_state()
    return 200, {"room": room}


def api_save_room(body):
    incoming = body.get("room") or {}
    code = incoming.get("id")
    if not code:
        return 400, {"error": "缺少房间号"}
    old = STATE["rooms"].get(code)
    if not old:
        return 404, {"error": "房间不存在"}
    STATE["rooms"][code] = merge_room(old, incoming)
    save_state()
    return 200, {"ok": True}


def api_delete_room(body):
    code = body.get("code")
    uid = body.get("uid")
    room = STATE["rooms"].get(code)
    if not room:
        return 404, {"error": "房间不存在"}
    if room.get("owner") != uid:
        return 403, {"error": "仅房主可删除房间"}
    STATE["rooms"].pop(code, None)
    STATE["chats"].pop(code, None)
    save_state()
    return 200, {"ok": True}


def api_set_chat(body):
    code = body.get("code")
    if not code:
        return 400, {"error": "缺少房间号"}
    STATE["chats"][code] = body.get("chat") or []
    save_state()
    return 200, {"ok": True}


def dispatch(method, path, body, query):
    if path == "/api/health":
        return 200, {"ok": True, "rooms": len(STATE["rooms"]), "users": len(STATE["users"])}

    if path == "/api/register" and method == "POST":
        return api_register(body)
    if path == "/api/login" and method == "POST":
        return api_login(body)
    if path == "/api/profile" and method == "POST":
        return api_profile(body)
    if path == "/api/changePwd" and method == "POST":
        return api_change_pwd(body)
    if path == "/api/createRoom" and method == "POST":
        return api_create_room(body)
    if path == "/api/join" and method == "POST":
        return api_join(body)
    if path == "/api/room" and method == "POST":
        return api_save_room(body)
    if path == "/api/deleteRoom" and method == "POST":
        return api_delete_room(body)
    if path == "/api/chat" and method == "POST":
        return api_set_chat(body)

    if path == "/api/room" and method == "GET":
        code = (query.get("code") or [""])[0].strip().upper()
        room = STATE["rooms"].get(code)
        return (200, {"room": room}) if room else (404, {"error": "房间不存在"})

    if path == "/api/rooms" and method == "GET":
        uid = (query.get("uid") or [""])[0]
        rooms = []
        for r in STATE["rooms"].values():
            if (not uid or r.get("owner") == uid
                    or any(p.get("uid") == uid for p in r.get("players") or [])
                    or any(j.get("uid") == uid for j in r.get("judges") or [])):
                rooms.append(room_summary(r))
        rooms.sort(key=lambda x: x.get("createdAt") or 0, reverse=True)
        return 200, {"rooms": rooms}

    if path == "/api/chat" and method == "GET":
        code = (query.get("code") or [""])[0].strip().upper()
        return 200, {"chat": STATE["chats"].get(code, [])}

    return 404, {"error": "接口不存在"}


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            with LOCK:
                status, payload = dispatch("GET", parsed.path, {}, parse_qs(parsed.query))
            return self._send_json(status, payload)
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                body = {}
            with LOCK:
                status, payload = dispatch("POST", parsed.path, body, {})
            return self._send_json(status, payload)
        return super().do_POST()

    def _send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 浏览器关闭标签页产生的连接重置属正常现象，不打印堆栈
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def lan_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def main():
    port = 8000
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print("端口参数无效，使用默认 8000")
    load_state()
    server = Server(("0.0.0.0", port), Handler)
    print("=" * 58)
    print("  图灵实验室 Turing Lab —— 联机后端已启动")
    print("=" * 58)
    print("  本机访问 : http://127.0.0.1:%d" % port)
    for ip in lan_ips():
        print("  局域网   : http://%s:%d" % (ip, port))
    print("  数据文件 : %s" % DATA_FILE)
    print("  提示：所有参赛设备访问同一地址即可联机；按 Ctrl+C 停止")
    print("=" * 58)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
