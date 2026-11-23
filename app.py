"""多人图灵测试：单进程 FastAPI + WebSocket + SQLite，纯手动 AI 转录。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from model_api import (ModelAPIError, chat_completion, decrypt_api_key,
                       encrypt_api_key, parse_json_object, validate_base_url)

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / 'data' / 'game.sqlite3'
COOKIE = 'turing_sid'
MAX_PLAYERS = 8
MAX_ROUNDS = 100
MAX_JUDGES = 12
DURATION_DAYS = 7


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    if len(password) < 8:
        raise ValueError('密码至少需要 8 个字符。')
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 310_000)
    return f'pbkdf2_sha256$310000${salt.hex()}${digest.hex()}'


def verify_password(password: str, stored: str) -> bool:
    try:
        alg, rounds, salt, checksum = stored.split('$')
        if alg != 'pbkdf2_sha256':
            return False
        calc = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(calc, bytes.fromhex(checksum))
    except (ValueError, TypeError):
        return False


def db_open(path):
    db = sqlite3.connect(str(path), timeout=20, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=20000')
    return db


def init_db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 首次升级旧版数据库前制作一致性快照（SQLite Backup API 兼容 WAL）。
    # 备份位于与数据库相同的本地目录，不会包含在提供给其他人的源码 ZIP 中。
    if path.is_file() and path.stat().st_size > 0:
        with db_open(path) as old:
            old_tables = {row['name'] for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            room_sql = old.execute("SELECT sql FROM sqlite_master WHERE name='rooms'").fetchone()
            needs_room_ids = bool(room_sql) and 'AUTOINCREMENT' not in room_sql['sql'].upper()
            if 'users' in old_tables and (
                'model_profiles' not in old_tables or
                ('ai_players' in old_tables and 'model_profile_id' not in {
                    x['name'] for x in old.execute('PRAGMA table_info(ai_players)')}) or
                ('ai_judges' in old_tables and 'model_profile_id' not in {
                    x['name'] for x in old.execute('PRAGMA table_info(ai_judges)')}) or
                'room_judges' not in old_tables or 'ai_judges' not in old_tables or
                'legacy_username' not in {x['name'] for x in old.execute('PRAGMA table_info(users)')} or
                'owner_id' not in {x['name'] for x in old.execute('PRAGMA table_info(rooms)')} or
                'deleted_at' not in {x['name'] for x in old.execute('PRAGMA table_info(users)')} or
                needs_room_ids or
                ('members' in old_tables and 'removed_at' not in {
                    x['name'] for x in old.execute('PRAGMA table_info(members)')}) or
                ('answers' in old_tables and 'edited_at' not in {
                    x['name'] for x in old.execute('PRAGMA table_info(answers)')})):
                stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
                backup_tag = ('v1_6' if ('model_profiles' not in old_tables or
                                   ('ai_players' in old_tables and 'model_profile_id' not in {
                                       x['name'] for x in old.execute('PRAGMA table_info(ai_players)')}) or
                                   ('ai_judges' in old_tables and 'model_profile_id' not in {
                                       x['name'] for x in old.execute('PRAGMA table_info(ai_judges)')})) else
                              'v1_1' if 'room_judges' not in old_tables else
                              'v1_2' if 'ai_judges' not in old_tables else
                              'v1_3' if ('members' in old_tables and 'removed_at' not in {
                                  x['name'] for x in old.execute('PRAGMA table_info(members)')}) or
                                  ('answers' in old_tables and 'edited_at' not in {
                                      x['name'] for x in old.execute('PRAGMA table_info(answers)')}) else
                              'v1_4' if 'legacy_username' not in {
                                  x['name'] for x in old.execute('PRAGMA table_info(users)')} else
                              'v1_5' if 'deleted_at' not in {
                                  x['name'] for x in old.execute('PRAGMA table_info(users)')} or 'owner_id' not in {
                                  x['name'] for x in old.execute('PRAGMA table_info(rooms)')} else 'v1_5_1')
                backup_path = path.with_name(path.stem + f'.pre-{backup_tag}-{stamp}-{secrets.token_hex(2)}.sqlite3')
                with sqlite3.connect(str(backup_path)) as backup:
                    old.backup(backup)
                print(f'[数据保护] 升级前数据库备份：{backup_path}')
    with db_open(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL COLLATE NOCASE,
            legacy_username TEXT UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL, pw_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts (
            identity TEXT PRIMARY KEY, failure_count INTEGER NOT NULL,
            first_failed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
            judge TEXT NOT NULL DEFAULT '裁判', status TEXT NOT NULL DEFAULT 'open',
            current_round INTEGER NOT NULL DEFAULT 1,
            verdict_json TEXT, verdict_locked_at TEXT, revealed_at TEXT,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS members (
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            alias TEXT NOT NULL, joined_at TEXT NOT NULL,
            PRIMARY KEY (room_id,user_id), UNIQUE(room_id,alias));
        CREATE TABLE IF NOT EXISTS ai_players (
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            alias TEXT NOT NULL, name TEXT NOT NULL,
            PRIMARY KEY(room_id,alias));
        CREATE TABLE IF NOT EXISTS rounds (
            id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            number INTEGER NOT NULL, question TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft', created_at TEXT NOT NULL,
            revealed_at TEXT, UNIQUE(room_id,number));
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
            alias TEXT NOT NULL, text TEXT NOT NULL, submitted_at TEXT NOT NULL,
            UNIQUE(round_id,alias));
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL, sender_id INTEGER NOT NULL REFERENCES users(id),
            text TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
            text TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS room_judges (
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            label TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (room_id,user_id));
        CREATE TABLE IF NOT EXISTS judge_questions (
            id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL, judge_user_id INTEGER NOT NULL REFERENCES users(id),
            text TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(room_id,round_number,judge_user_id));
        CREATE TABLE IF NOT EXISTS judge_verdicts (
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            judge_user_id INTEGER NOT NULL REFERENCES users(id),
            verdict_json TEXT NOT NULL, locked_at TEXT NOT NULL,
            PRIMARY KEY(room_id,judge_user_id));
        -- AI 裁判不创建登录账户，所有读写由管理员代理。
        CREATE TABLE IF NOT EXISTS ai_judges (
            id INTEGER PRIMARY KEY,
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            label TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS model_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            name TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL,
            api_key_encrypted TEXT NOT NULL, temperature REAL NOT NULL DEFAULT 0.7,
            max_tokens INTEGER NOT NULL DEFAULT 2048, timeout_seconds INTEGER NOT NULL DEFAULT 60,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_judge_questions (
            id INTEGER PRIMARY KEY,
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL,
            ai_judge_id INTEGER NOT NULL REFERENCES ai_judges(id) ON DELETE CASCADE,
            text TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(room_id,round_number,ai_judge_id));
        CREATE TABLE IF NOT EXISTS ai_judge_verdicts (
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            ai_judge_id INTEGER NOT NULL REFERENCES ai_judges(id) ON DELETE CASCADE,
            verdict_json TEXT NOT NULL, locked_at TEXT NOT NULL,
            PRIMARY KEY(room_id,ai_judge_id));
        CREATE TABLE IF NOT EXISTS ai_answer_revisions (
            id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL, alias TEXT NOT NULL,
            previous_text TEXT NOT NULL, replacement_text TEXT NOT NULL, changed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS packets (
            id INTEGER PRIMARY KEY, room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            through_round INTEGER NOT NULL, version INTEGER NOT NULL, content TEXT NOT NULL,
            sha256 TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT,
            UNIQUE(room_id,through_round,version));
        ''')
        if 'model_profile_id' not in {x['name'] for x in db.execute('PRAGMA table_info(ai_players)')}:
            db.execute('ALTER TABLE ai_players ADD COLUMN model_profile_id INTEGER REFERENCES model_profiles(id) ON DELETE SET NULL')
        if 'model_profile_id' not in {x['name'] for x in db.execute('PRAGMA table_info(ai_judges)')}:
            db.execute('ALTER TABLE ai_judges ADD COLUMN model_profile_id INTEGER REFERENCES model_profiles(id) ON DELETE SET NULL')
        if 'removed_at' not in {x['name'] for x in db.execute('PRAGMA table_info(members)')}:
            db.execute('ALTER TABLE members ADD COLUMN removed_at TEXT')
        if 'edited_at' not in {x['name'] for x in db.execute('PRAGMA table_info(answers)')}:
            db.execute('ALTER TABLE answers ADD COLUMN edited_at TEXT')
        if 'legacy_username' not in {x['name'] for x in db.execute('PRAGMA table_info(users)')}:
            migrate_user_accounts(db)
        if 'owner_id' not in {x['name'] for x in db.execute('PRAGMA table_info(rooms)')}:
            db.execute('ALTER TABLE rooms ADD COLUMN owner_id INTEGER REFERENCES users(id)')
            db.execute('UPDATE rooms SET owner_id=(SELECT MIN(id) FROM users WHERE is_admin=1)')
        if 'match_number' not in {x['name'] for x in db.execute('PRAGMA table_info(rooms)')}:
            db.execute('ALTER TABLE rooms ADD COLUMN match_number INTEGER NOT NULL DEFAULT 1')
        if 'records_deleted' not in {x['name'] for x in db.execute('PRAGMA table_info(rooms)')}:
            db.execute('ALTER TABLE rooms ADD COLUMN records_deleted INTEGER NOT NULL DEFAULT 0')
        if 'deleted_at' not in {x['name'] for x in db.execute('PRAGMA table_info(users)')}:
            db.execute('ALTER TABLE users ADD COLUMN deleted_at TEXT')
        room_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='rooms'").fetchone()['sql']
        if 'AUTOINCREMENT' not in room_sql.upper():
            migrate_room_ids(db,room_sql)


def migrate_room_ids(db, definition):
    """保留原房间和所有子表，使删除的房间编号不再被新房间复用。"""
    db.commit()
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        schema = 'CREATE TABLE rooms_id_upgrade ' + definition[definition.index('('):]
        schema = re.sub(r'\bid INTEGER PRIMARY KEY\b', 'id INTEGER PRIMARY KEY AUTOINCREMENT',
                        schema, count=1, flags=re.IGNORECASE)
        db.execute(schema)
        db.execute('INSERT INTO rooms_id_upgrade SELECT * FROM rooms')
        db.execute('DROP TABLE rooms')
        db.execute('ALTER TABLE rooms_id_upgrade RENAME TO rooms')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('房间编号升级未通过外键检查，已停止迁移。')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')


def migrate_user_accounts(db):
    """在同一个事务中移除用户名唯一约束，保留主键和所有外键引用。"""
    db.commit()
    # SQLite 必须在事务外关闭此连接的外键，才能安全替换被引用的 users 表。
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE users_uid_upgrade (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL COLLATE NOCASE,
            legacy_username TEXT UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL, pw_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL)''')
        db.execute('''INSERT INTO users_uid_upgrade
            (id,username,legacy_username,display_name,pw_hash,is_admin,created_at)
            SELECT id,username,username,display_name,pw_hash,is_admin,created_at FROM users''')
        db.execute('DROP TABLE users')
        db.execute('ALTER TABLE users_uid_upgrade RENAME TO users')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('账号升级未通过外键检查，已停止迁移。')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')


def user_uid(user_id):
    return f'TL{user_id:08d}'


def lookup_account(db, uid=None, username=None):
    """显式区分 UID 与旧登录名，避免旧用户名形似 UID 时选错账号。"""
    if (uid is None) == (username is None):
        raise HTTPException(422, '请提供 UID 或旧登录名中的一项。')
    if uid is not None:
        value = uid.strip().upper()
        if not re.fullmatch(r'TL[0-9]{8,19}', value):
            return None
        user_id = int(value[2:])
        if not 1 <= user_id <= 9223372036854775807 or user_uid(user_id) != value:
            return None
        return db.execute('SELECT * FROM users WHERE id=? AND deleted_at IS NULL', (user_id,)).fetchone()
    return db.execute('SELECT * FROM users WHERE legacy_username=? AND deleted_at IS NULL', (username.strip(),)).fetchone()


def seed_admin(path, username, display, password):
    """首次初始化唯一管理员；不会重置已存在的管理员密码。"""
    init_db(path)
    with db_open(path) as db:
        if db.execute('SELECT 1 FROM users WHERE is_admin=1').fetchone():
            return False
        if not re.fullmatch(r'[A-Za-z0-9_]{3,30}', username):
            raise ValueError('管理员登录名须为 3–30 位英文、数字或下划线。')
        db.execute('INSERT INTO users (username,legacy_username,display_name,pw_hash,is_admin,created_at) VALUES (?,?,?,?,?,?)',
                   (username, username, display, hash_password(password), 1, utcnow()))
        return True


class LoginData(BaseModel):
    uid: Optional[str] = Field(default=None, min_length=1, max_length=40)
    username: Optional[str] = Field(default=None, min_length=3, max_length=30)
    password: str = Field(min_length=1, max_length=200)


class RegisterData(BaseModel, extra='forbid'):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=200)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=40)
    code: Optional[str] = Field(default=None, min_length=6, max_length=20)


class CodeData(BaseModel):
    code: str = Field(min_length=6, max_length=20)


class MatchActionData(BaseModel):
    match_number: int = Field(ge=1)
    confirm: Literal[True]


class ConfirmData(BaseModel):
    confirm: Literal[True]


class PasswordResetData(ConfirmData):
    password: str = Field(min_length=8, max_length=200)


class RoomData(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    judge: str = Field(default='裁判', min_length=1, max_length=40)


class AIData(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    model_profile_id: Optional[int] = Field(default=None, ge=1)


class ModelProfileData(BaseModel, extra='forbid'):
    name: str = Field(min_length=1, max_length=60)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=120)
    api_key: Optional[str] = Field(default=None, max_length=2000)
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=64, le=8192)
    timeout_seconds: int = Field(default=60, ge=10, le=120)


class JudgeAccountData(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    display_name: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=8, max_length=200)


class ExistingJudgeData(BaseModel):
    uid: Optional[str] = Field(default=None, min_length=1, max_length=40)
    username: Optional[str] = Field(default=None, min_length=3, max_length=30)
    label: str = Field(min_length=1, max_length=40)


class AIAnswerEditData(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class TextData(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class VerdictData(BaseModel):
    predictions: dict[str, str]
    evidence: str = Field(min_length=1, max_length=5000)
    confidence: int = Field(ge=0, le=100)
    weakness: str = Field(default='', max_length=5000)
    possible_error: str = Field(default='', max_length=5000)


def norm_text(value, max_len):
    value = value.strip()
    if not value or len(value) > max_len:
        raise HTTPException(422, f'内容需要 1～{max_len} 字。')
    return value


def auth(db, token):
    if not token:
        raise HTTPException(401, '请先登录。')
    row = db.execute('''SELECT u.id,u.username,u.display_name,u.is_admin,s.csrf FROM sessions s
        JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.deleted_at IS NULL''',
                     (hashlib.sha256(token.encode()).hexdigest(), utcnow())).fetchone()
    if not row:
        raise HTTPException(401, '登录已过期，请重新登录。')
    return dict(row, uid=user_uid(row['id']))


def require_admin(u):
    if not u['is_admin']:
        raise HTTPException(403, '此操作仅管理员可用。')


def check_csrf(request, u):
    if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), u['csrf']):
        raise HTTPException(403, '请求校验失败，请刷新页面。')


def check_origin(headers):
    """阻止其他站点借用浏览器 Cookie 发起 POST / WebSocket 请求。"""
    origin = headers.get('origin')
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
        host = headers.get('host', '')
        allowed = [v.strip() for v in os.getenv('ALLOWED_ORIGINS', '').split(',') if v.strip()]
        return parsed.scheme in ('http', 'https') and (parsed.netloc == host or origin in allowed)
    except ValueError:
        return False


def require_origin(headers):
    if not check_origin(headers):
        raise HTTPException(403, '不允许跨站请求。')


def is_judge(db, room_id, user):
    return db.execute('SELECT label FROM room_judges WHERE room_id=? AND user_id=?',
                      (room_id, user['id'])).fetchone()


def permission(db, room_id, user):
    room = db.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
    if not room:
        raise HTTPException(404, '房间不存在。')
    membership = db.execute('SELECT alias FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL',
                            (room_id, user['id'])).fetchone()
    if not membership and not can_manage_room(room, user) and not is_judge(db, room_id, user):
        raise HTTPException(403, '你不属于这个房间。')
    return room, (membership['alias'] if membership else None)


def can_manage_room(room, user):
    return bool(user['is_admin'] or room['owner_id'] == user['id'])


def require_room_manager(db, room_id, user):
    room, mine = permission(db, room_id, user)
    if not can_manage_room(room, user):
        raise HTTPException(403, '此操作仅房主或管理员可用。')
    return room, mine


def require_judge(db, room_id, user):
    # 管理员、普通参赛者不能冒充裁判（即使拥有其他房间的裁判资格）。
    record = is_judge(db, room_id, user)
    if not record or user['is_admin']:
        raise HTTPException(403, '只有本房间的独立裁判可以执行此操作。')
    return record


def judge_records(db, room_id):
    """统一返回真人和 AI 裁判状态；绝不在此读取判决内容。"""
    people = db.execute("""SELECT j.user_id,j.label,u.username,j.created_at,
        EXISTS(SELECT 1 FROM judge_verdicts v WHERE v.room_id=j.room_id AND v.judge_user_id=j.user_id) AS locked
        FROM room_judges j JOIN users u ON u.id=j.user_id
        WHERE j.room_id=? AND (u.deleted_at IS NULL OR EXISTS(
            SELECT 1 FROM judge_verdicts v WHERE v.room_id=j.room_id AND v.judge_user_id=j.user_id))""", (room_id,)).fetchall()
    ai = db.execute("""SELECT j.id,j.label,j.created_at,j.model_profile_id,
        EXISTS(SELECT 1 FROM ai_judge_verdicts v WHERE v.room_id=j.room_id AND v.ai_judge_id=j.id) AS locked
        FROM ai_judges j WHERE j.room_id=?""", (room_id,)).fetchall()
    data = [dict(r, id=r['user_id'], kind='human') for r in people]
    data += [dict(r, kind='ai', username=None, user_id=None) for r in ai]
    return sorted(data, key=lambda r: (r['created_at'], r['kind'], r['id']))


def check_judge_registration(db, room, user_id):
    if room['owner_id'] == user_id:
        raise HTTPException(409, '房主不能同时担任本房间的独立裁判。')
    if room['status'] != 'open' or room['verdict_locked_at'] or db.execute(
        'SELECT 1 FROM judge_verdicts WHERE room_id=? LIMIT 1',(room['id'],)).fetchone() or db.execute(
        'SELECT 1 FROM ai_judge_verdicts WHERE room_id=? LIMIT 1',(room['id'],)).fetchone():
        raise HTTPException(409, '比赛已结束或有人锁定结论，不能再添加裁判。')
    if db.execute('SELECT 1 FROM members WHERE room_id=? AND user_id=?',(room['id'],user_id)).fetchone():
        raise HTTPException(409, '本房间的参赛者不能同时担任裁判。')
    if len(judge_records(db,room['id'])) >= MAX_JUDGES:
        raise HTTPException(409,f'每个房间最多 {MAX_JUDGES} 位裁判。')


def aliases(db, room_id):
    humans = db.execute('''SELECT m.alias,m.user_id,u.display_name,u.username FROM members m
        JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND m.removed_at IS NULL ORDER BY m.alias''', (room_id,)).fetchall()
    ais = db.execute('SELECT alias,name,model_profile_id FROM ai_players WHERE room_id=? ORDER BY alias', (room_id,)).fetchall()
    all_aliases = sorted([r['alias'] for r in humans] + [r['alias'] for r in ais])
    return humans, ais, all_aliases


def new_alias(db, room_id):
    _, _, taken = aliases(db, room_id)
    reserved = {x['alias'] for x in db.execute('SELECT alias FROM members WHERE room_id=?',(room_id,))}
    reserved.update(x['alias'] for x in db.execute('SELECT alias FROM ai_players WHERE room_id=?',(room_id,)))
    if len(taken) >= MAX_PLAYERS:
        raise HTTPException(409, f'最多支持 {MAX_PLAYERS} 位参赛者。')
    index = 0
    while True:
        alias = chr(ord('A') + index) if index < 26 else f'P{index + 1}'
        if alias not in reserved:
            return alias
        index += 1


def needs_roster_recovery(db, room):
    return room['status'] == 'open' and current_round(db, room)['status'] == 'draft' and (
        len(aliases(db, room['id'])[2]) < 2) and bool(db.execute('''SELECT 1 FROM members m
        JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND u.deleted_at IS NOT NULL LIMIT 1''',
        (room['id'],)).fetchone())


def ensure_roster_unlocked(db, room):
    if room['status'] != 'open':
        raise HTTPException(409, '比赛已结束。')
    if needs_roster_recovery(db, room):
        return
    r = db.execute('SELECT question FROM rounds WHERE room_id=? AND number=1', (room['id'],)).fetchone()
    if (r and r['question']) or db.execute(
        'SELECT 1 FROM judge_questions WHERE room_id=? AND round_number=1 LIMIT 1',(room['id'],)).fetchone():
        raise HTTPException(409, '首轮裁判问题已提交，无法再修改参赛名单。')


def current_round(db, room, number=None):
    num = number or room['current_round']
    rnd = db.execute('SELECT * FROM rounds WHERE room_id=? AND number=?', (room['id'], num)).fetchone()
    if not rnd:
        raise HTTPException(404, '轮次不存在。')
    return rnd


def clear_match_records(db, room_id):
    # 只清除比赛内容，房间、成员、AI 参赛位及裁判配置保留。
    for table in ('rounds', 'suggestions', 'events', 'judge_questions', 'judge_verdicts',
                  'ai_judge_questions', 'ai_judge_verdicts', 'ai_answer_revisions', 'packets'):
        db.execute(f'DELETE FROM {table} WHERE room_id=?', (room_id,))
    db.execute('''UPDATE rooms SET verdict_json=NULL,verdict_locked_at=NULL,
        revealed_at=NULL,records_deleted=1 WHERE id=?''', (room_id,))


def check_match_number(room, number):
    if str(room['match_number']) != str(number):
        raise HTTPException(409, '房间已开始另一场比赛，请刷新后重试。')


def public_room(db, room, user, mine):
    is_admin = bool(user['is_admin'])
    manager = can_manage_room(room, user)
    judge = is_judge(db, room['id'], user)
    is_judge_view = bool(judge) and not is_admin
    humans, ais, roster = aliases(db, room['id'])
    judges = judge_records(db,room['id'])
    rounds = db.execute('SELECT * FROM rounds WHERE room_id=? ORDER BY number', (room['id'],)).fetchall()
    body = []
    for r in rounds:
        got = db.execute('SELECT alias,text,submitted_at,edited_at FROM answers WHERE round_id=? ORDER BY alias', (r['id'],)).fetchall()
        answer_map = {a['alias']: {'text': a['text'], 'submitted_at': a['submitted_at'], 'edited_at':a['edited_at']}
                      for a in got if r['status'] == 'revealed' or (not is_judge_view and a['alias'] == mine)}
        qrows = db.execute('''SELECT q.id,q.text,q.created_at,q.judge_user_id,j.label,'human' AS kind,q.judge_user_id AS judge_id
            FROM judge_questions q JOIN room_judges j ON q.room_id=j.room_id AND q.judge_user_id=j.user_id
            WHERE q.room_id=? AND q.round_number=?
            UNION ALL
            SELECT q.id,q.text,q.created_at,NULL AS judge_user_id,j.label,'ai' AS kind,q.ai_judge_id AS judge_id
            FROM ai_judge_questions q JOIN ai_judges j ON q.ai_judge_id=j.id
            WHERE q.room_id=? AND q.round_number=?
            ORDER BY 3,6,1''',(room['id'],r['number'],room['id'],r['number'])).fetchall()
        body.append(dict(number=r['number'], question=r['question'], status=r['status'],
                         revealed_at=r['revealed_at'], submitted=[a['alias'] for a in got if a['alias'] in roster or r['status']=='revealed'],
                         questions=[{'id':x['id'],'text':x['text'],'created_at':x['created_at'],
                                     'judge':x['label'],'kind':x['kind'],'judge_id':x['judge_id'],
                                     'mine':x['kind']=='human' and x['judge_user_id']==user['id']} for x in qrows],
                         answers=answer_map))
    pkts = db.execute('SELECT id,through_round,version,sha256,created_at,sent_at FROM packets WHERE room_id=? ORDER BY id DESC',
                      (room['id'],)).fetchall()
    verdict = None
    if room['revealed_at'] and room['verdict_json'] and not is_judge_view:
        verdict = json.loads(room['verdict_json'])
    verdicts = []
    if room['revealed_at'] and not is_judge_view:
        if verdict:
            verdicts.append({'judge':room['judge'],'legacy':True,**verdict})
        for row in db.execute('''SELECT j.label,v.verdict_json FROM judge_verdicts v
            JOIN room_judges j ON j.room_id=v.room_id AND j.user_id=v.judge_user_id
            WHERE v.room_id=? ORDER BY j.created_at,j.user_id''',(room['id'],)):
            verdicts.append({'judge':row['label'],'kind':'human','legacy':False,
                             **json.loads(row['verdict_json'])})
        for row in db.execute('''SELECT j.label,v.verdict_json FROM ai_judge_verdicts v
            JOIN ai_judges j ON j.id=v.ai_judge_id
            WHERE v.room_id=? ORDER BY j.created_at,j.id''',(room['id'],)):
            verdicts.append({'judge':row['label'],'kind':'ai','legacy':False,
                             **json.loads(row['verdict_json'])})
    my_verdict = None
    if is_judge_view:
        row = db.execute('SELECT verdict_json FROM judge_verdicts WHERE room_id=? AND judge_user_id=?',
                         (room['id'],user['id'])).fetchone()
        if row:
            my_verdict = json.loads(row['verdict_json'])
    # 裁判永远只收到代号，不收到 A/B 属于人类还是 AI、任何玩家账户信息或他人裁判结论。
    if is_judge_view:
        suggestions = []
        for s in db.execute('''SELECT s.round_number,s.text,s.created_at,m.alias
            FROM suggestions s LEFT JOIN members m ON m.room_id=s.room_id AND m.user_id=s.sender_id
            WHERE s.room_id=? ORDER BY s.id''', (room['id'],)):
            suggestions.append({'round_number':s['round_number'],'text':s['text'],
                'created_at':s['created_at'],'alias':s['alias'] or '主持人'})
        return {'id':room['id'],'title':room['title'],'status':room['status'],
            'match_number':room['match_number'],'records_deleted':bool(room['records_deleted']),
            'can_manage':False,'is_owner':False,
            'current_round':room['current_round'],'verdict_locked':bool(my_verdict),
            'revealed_at':room['revealed_at'],'mine':None,'is_admin':False,'is_judge':True,
            'my_judge_label':judge['label'],'my_verdict':my_verdict,
            'aliases':roster,'rounds':body,'suggestions':suggestions,
            'judges_count':len(judges)}
    suggestions = db.execute('''SELECT s.id,s.round_number,s.text,s.created_at,u.display_name,
        m.alias FROM suggestions s JOIN users u ON u.id=s.sender_id
        LEFT JOIN members m ON m.room_id=s.room_id AND m.user_id=s.sender_id
        WHERE s.room_id=? ORDER BY s.id''', (room['id'],)).fetchall()
    ai_edits = {}
    model_profiles = []
    if manager:
        model_profiles = [dict(row) for row in db.execute(
            '''SELECT id,name,base_url,model,temperature,max_tokens,timeout_seconds,
                      CASE WHEN api_key_encrypted<>'' THEN 1 ELSE 0 END AS has_api_key
               FROM model_profiles WHERE room_id=? ORDER BY id''', (room['id'],))]
    if manager and not room['records_deleted']:
        now_round = current_round(db,room)
        ai_edits = {row['alias']:row['text'] for row in db.execute('''SELECT a.alias,a.text FROM answers a
            JOIN ai_players ai ON ai.room_id=? AND ai.alias=a.alias
            WHERE a.round_id=?''',(room['id'],now_round['id']))}
    return {'id':room['id'],'title':room['title'],'judge':room['judge'],
            'match_number':room['match_number'],'records_deleted':bool(room['records_deleted']),
            'can_manage':manager,'is_owner':room['owner_id']==user['id'],
            'roster_recovery':manager and needs_roster_recovery(db,room),
            'status':room['status'],'current_round':room['current_round'],
            'verdict_locked':bool(room['verdict_locked_at']) or (bool(judges) and all(j['locked'] for j in judges)),
            'revealed_at':room['revealed_at'],'verdict':verdict,'verdicts':verdicts,
            'mine':mine,'is_admin':is_admin,'is_judge':False,
            'invite_code':room['code'] if manager else None,
            'humans':[{'alias':r['alias'],'user_id':r['user_id'],'uid':user_uid(r['user_id']),
                       'name':r['display_name'], 'username':r['username']} for r in humans],
            'ais':[{'alias':r['alias'],'name':r['name'],
                    'model_profile_id':r['model_profile_id'] if manager else None} for r in ais],
            'judges': [{'id':r['id'],'kind':r['kind'],'user_id':r['user_id'],
                        'label':r['label'],'username':r['username'],
                        'model_profile_id':r['model_profile_id'] if manager and r['kind']=='ai' else None,
                        'uid':user_uid(r['user_id']) if r['kind']=='human' else None,
                        'locked':bool(r['locked'])} for r in judges] if manager else
                      [{'kind':r['kind'],'label':r['label'],'locked':bool(r['locked'])} for r in judges],
            'rounds':body,'suggestions':[dict(s) for s in suggestions],
            'ai_edits':ai_edits,'packets':[dict(p) for p in pkts],
            'model_profiles':model_profiles,'model_api_enabled':bool(os.getenv('MODEL_CREDENTIAL_KEY'))}


def anonymous_match_packet(db, room_id, through_round=None):
    """Build a full-match judging packet containing aliases and text only."""
    _, _, roster = aliases(db, room_id)
    lines = ['多人图灵测试 · 全场匿名裁判资料',
             '只包含匿名代号、每轮裁判问题和参赛者原始回答；不包含真实身份或模型来源。']
    rounds = db.execute('''SELECT number,question FROM rounds WHERE room_id=?
        AND (? IS NULL OR number<=?) ORDER BY number''',
        (room_id, through_round, through_round)).fetchall()
    for rnd in rounds:
        lines += ['', f'===== 第 {rnd["number"]} 轮 =====', '', '【裁判问题】', rnd['question']]
        answers = {row['alias']: row['text'] for row in db.execute(
            '''SELECT alias,text FROM answers WHERE round_id=(
                   SELECT id FROM rounds WHERE room_id=? AND number=?)''',
            (room_id, rnd['number']))}
        for alias in roster:
            lines += ['', f'【参与者 {alias}】', answers.get(alias, '（本轮没有有效回答）')]
    lines += ['', '—— 全场匿名资料结束 ——']
    return '\n'.join(lines) + '\n'


def anonymous_player_history(db, room_id, alias, through_round):
    """Return only this AI seat's own prior answers, never other contestants' text."""
    lines = []
    rounds = db.execute('''SELECT id,number,question FROM rounds
        WHERE room_id=? AND number<=? ORDER BY number''',(room_id,through_round)).fetchall()
    for rnd in rounds:
        answer=db.execute('SELECT text FROM answers WHERE round_id=? AND alias=?',
                          (rnd['id'],alias)).fetchone()
        if answer:
            lines += [f'第 {rnd["number"]} 轮问题：',rnd['question'],
                      f'你在第 {rnd["number"]} 轮的回答：',answer['text'],'']
    return '\n'.join(lines) if lines else '（此前没有已完成轮次。）'


def is_complete(db, rnd, room_id):
    _, _, expected = aliases(db, room_id)
    collected = {r['alias'] for r in db.execute('SELECT alias FROM answers WHERE round_id=?', (rnd['id'],))}
    original_count = db.execute('SELECT COUNT(*) FROM members WHERE room_id=?', (room_id,)).fetchone()[0]
    original_count += db.execute('SELECT COUNT(*) FROM ai_players WHERE room_id=?', (room_id,)).fetchone()[0]
    if original_count >= 2 and set(expected).issubset(collected):
        db.execute('UPDATE rounds SET status=?,revealed_at=? WHERE id=?', ('revealed', utcnow(), rnd['id']))
        return True
    return False


def log_event(db, room_id, number, kind, actor, text):
    db.execute('INSERT INTO events (room_id,round_number,kind,actor,text,created_at) VALUES(?,?,?,?,?,?)',
               (room_id,number,kind,actor,text,utcnow()))


def judge_packet(db, room, through):
    rnd = current_round(db, room, through)
    if rnd['status'] != 'revealed':
        raise HTTPException(409,'本轮尚未统一公开，不能生成快照。')
    entries=db.execute('SELECT alias,text FROM answers WHERE round_id=? ORDER BY alias',(rnd['id'],)).fetchall()
    if len(entries)<2:
        raise HTTPException(409,'本轮有效答复不足。')
    lines = ['AI 图灵测试 · 单轮匿名裁判快照', f'范围：仅第 {through} 轮',
             '请根据本轮匿名答复判断，不包含之前轮次资料。',
             '', '【本轮裁判问题】', rnd['question']]
    for a in entries:
        lines += ['', f'【参与者 {a["alias"]} · 原始回答】', a['text']]
    lines += ['', '—— 本轮资料结束 ——']
    return '\n'.join(lines) + '\n'


def round_markdown(db, room, number, user, mine):
    data = public_room(db, room, user, mine)
    r = data['rounds'][number-1]
    if data['is_judge']:
        lines = [f'# {room["title"]} · 第 {number} 轮 · 裁判匿名记录', '',
                 f'状态：{r["status"]}', '', '## 本轮问题', '',
                 r['question'] or '（等待管理员开启作答）', '', '## 匿名参赛者回答', '']
        for alias in sorted(set(data['aliases']) | set(r['answers'])):
            answer = r['answers'].get(alias)
            lines += [f'### 参与者 {alias}',
                      answer['text'] if answer else '（已提交，待统一揭晓）' if alias in r['submitted'] else '（未提交）', '']
        lines += ['## 本轮匿名提问与追问', '']
        for suggestion in data['suggestions']:
            if suggestion['round_number']==number:
                lines.append(f'- {suggestion["alias"]}: {suggestion["text"]}')
        return '\n'.join(lines) + '\n'
    lines = [f'# {room["title"]} · 第 {number} 轮', '',f'状态：{r["status"]}',
             f'裁判：{room["judge"]}', '', '## 裁判问题', '', r['question'] or '（尚未发布）',
             '', '## 本轮玩家提议 / 追问', '']
    for s in data['suggestions']:
        if s['round_number'] == number:
            lines += [f'- {s["created_at"]} {s["alias"] or "管理员"}（{s["display_name"]}）：{s["text"]}']
    lines += ['', '## 参赛者回答', '']
    for h in data['humans']:
        a = h['alias']
        answer = r['answers'].get(a)
        # 未公开时只显示提交状态，自己可以看到自己的回答。
        lines += [f'### {a} · 人类 · {h["name"]} (@{h["username"]}) · UID {h["uid"]}',
                  f'提交时间：{answer["submitted_at"]}' if answer else '',
                  answer['text'] if answer else '（已提交，待统一揭晓）' if a in r['submitted'] else '（未提交）', '']
    for departed in db.execute('''SELECT m.alias,u.id AS user_id,u.display_name,u.username,m.removed_at FROM members m
        JOIN users u ON m.user_id=u.id WHERE m.room_id=? AND m.removed_at IS NOT NULL ORDER BY m.alias''',(room['id'],)):
        a=departed['alias']
        answer=r['answers'].get(a)
        lines += [f'### {a} · 已移出的原人类参赛者 · {departed["display_name"]} (@{departed["username"]}) · UID {user_uid(departed["user_id"])}',
                  answer['text'] if answer else '（本轮未提交）', '']
    for ai in data['ais']:
        a = ai['alias']
        answer = r['answers'].get(a)
        lines += [f'### {a} · AI · {ai["name"]}', '', answer['text'] if answer else '（已提交，待统一揭晓）' if a in r['submitted'] else '（未提交）', '']
    lines += ['', '## 本轮事件时间线', '']
    for event in db.execute('SELECT kind,actor,text,created_at FROM events WHERE room_id=? AND round_number=? ORDER BY id',
                            (room['id'],number)):
        lines.append(f'- {event["created_at"]} · {event["actor"]} · {event["text"]}')
    return '\n'.join(lines) + '\n'


class Hub:
    def __init__(self, path):
        self.path = path
        self.groups: dict[int, set[WebSocket]] = {}
        self.typing: dict[int, dict[WebSocket, str]] = {}

    async def notify(self, room_id, reason):
        for ws in list(self.groups.get(room_id, set())):
            try:
                # 登出/会话失效后，不再向旧 WebSocket 推送房间变更。
                with db_open(self.path) as db:
                    user = auth(db, ws.cookies.get(COOKIE))
                    room, _ = permission(db, room_id, user)
                await ws.send_json({'type':'room_updated','room_id':room_id,'reason':reason,
                                    'match_number':room['match_number']})
            except Exception:
                self.groups.get(room_id, set()).discard(ws)
                try:
                    await ws.close(code=1008,reason='房间已删除' if reason=='room_deleted' else '')
                except Exception:
                    pass

    async def typing_status(self, room_id):
        # Only active human contestants receive typing info; never judges, AI or admin observers.
        active = self.typing.get(room_id,{})
        for ws in list(self.groups.get(room_id,set())):
            try:
                with db_open(self.path) as db:
                    u=auth(db,ws.cookies.get(COOKIE))
                    room,mine=permission(db,room_id,u)
                    if not mine:
                        continue
                    rnd=current_round(db,room)
                    roster=set(aliases(db,room_id)[2])
                    states={alias:True for alias in active.values() if alias in roster and rnd['status']=='collecting'}
                    await ws.send_json({'type':'typing_status','room_id':room_id,
                                        'round':rnd['number'],'match_number':room['match_number'],'states':states})
            except Exception:
                self.groups.get(room_id,set()).discard(ws)



def create_app(db_path=None):
    path = Path(db_path or os.getenv('DB_PATH', str(DATABASE)))
    init_db(path)
    app = FastAPI(title='图灵测试多人主持台', docs_url=None, redoc_url=None)
    app.state.path = path
    app.state.hub = Hub(path)
    app.state.model_semaphore = asyncio.Semaphore(4)

    async def model_call(profile, messages, api_key):
        async with app.state.model_semaphore:
            return await chat_completion(profile, messages, api_key)

    app.mount('/static', StaticFiles(directory=ROOT/'static'), name='static')

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api/') or request.url.path == '/':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @contextmanager
    def connection(immediate=False):
        db = db_open(path)
        try:
            if immediate:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def caller(request, db, mutate=False):
        if mutate:
            require_origin(request.headers)
        u = auth(db, request.cookies.get(COOKIE))
        if mutate:
            check_csrf(request, u)
            match = re.match(r'^/api/rooms/(\d+)(?:/|$)', request.url.path)
            if match and 'x-match-number' in request.headers:
                room = db.execute('SELECT * FROM rooms WHERE id=?', (int(match[1]),)).fetchone()
                if room:
                    check_match_number(room, request.headers['x-match-number'])
        return u

    def set_session(response, db, user_id):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(28)
        expires = (datetime.now(timezone.utc)+timedelta(days=DURATION_DAYS)).isoformat(timespec='seconds')
        db.execute('INSERT INTO sessions(token_hash,user_id,csrf,expires_at) VALUES(?,?,?,?)',
                   (hashlib.sha256(token.encode()).hexdigest(),user_id,csrf,expires))
        secure = os.getenv('COOKIE_SECURE', '0') == '1'
        response.set_cookie(COOKIE,token,httponly=True,secure=secure,samesite='lax',max_age=86400*DURATION_DAYS,path='/')

    @app.get('/')
    def home():
        return FileResponse(ROOT/'static'/'index.html')

    @app.get('/api/health')
    def health():
        return {'ok': True}

    @app.get('/api/me')
    def me(request:Request):
        with connection() as db:
            u=caller(request,db)
            rooms = db.execute('''SELECT DISTINCT r.id,r.title,r.status,r.current_round,r.created_at,r.match_number,
                CASE WHEN ?=1 THEN 'admin'
                     WHEN r.owner_id=? THEN 'owner'
                     WHEN EXISTS(SELECT 1 FROM room_judges j WHERE j.room_id=r.id AND j.user_id=?) THEN 'judge'
                     ELSE 'player' END AS role
                FROM rooms r LEFT JOIN members m ON r.id=m.room_id
                LEFT JOIN room_judges j ON j.room_id=r.id
                WHERE (m.user_id=? AND m.removed_at IS NULL) OR j.user_id=? OR r.owner_id=? OR ?=1 ORDER BY r.id DESC''',
                (u['is_admin'],u['id'],u['id'],u['id'],u['id'],u['id'],u['is_admin'])).fetchall()
            finished = db.execute('''SELECT id,title,match_number,records_deleted FROM rooms
                WHERE status='finished' AND records_deleted=0 ORDER BY id DESC''').fetchall()
            return {'user':u, 'rooms':[dict(x,can_manage=x['role'] in ('owner','admin')) for x in rooms],
                    'finished_rooms':[dict(x) for x in finished]}

    @app.post('/api/login')
    def login(body:LoginData, request:Request, response:Response):
        require_origin(request.headers)
        if (body.uid is None) == (body.username is None):
            raise HTTPException(422, '请提供 UID 或旧登录名中的一项。')
        login_key = 'uid:' + body.uid.strip().upper() if body.uid is not None else 'legacy:' + body.username.strip().lower()
        identity=(request.client.host if request.client else 'unknown')+':'+login_key
        now=datetime.now(timezone.utc)
        blocked=False
        invalid=False
        with connection(immediate=True) as db:
            attempt=db.execute('SELECT * FROM login_attempts WHERE identity=?',(identity,)).fetchone()
            if attempt:
                first=datetime.fromisoformat(attempt['first_failed_at'])
                if now-first>timedelta(minutes=15):
                    db.execute('DELETE FROM login_attempts WHERE identity=?',(identity,))
                    attempt=None
            if attempt and attempt['failure_count']>=8:
                blocked=True
            else:
                row=lookup_account(db, body.uid, body.username)
                if not row or not verify_password(body.password,row['pw_hash']):
                    invalid=True
                    if attempt:
                        db.execute('UPDATE login_attempts SET failure_count=failure_count+1 WHERE identity=?',(identity,))
                    else:
                        db.execute('INSERT INTO login_attempts(identity,failure_count,first_failed_at) VALUES(?,?,?)',
                                   (identity,1,now.isoformat(timespec='seconds')))
                else:
                    db.execute('DELETE FROM login_attempts WHERE identity=?',(identity,))
                    set_session(response,db,row['id'])
        if blocked:
            raise HTTPException(429,'登录失败次数太多，请 15 分钟后重试。')
        if invalid:
            raise HTTPException(401,'账号或密码错误。')
        return {'ok':True,'uid':user_uid(row['id'])}

    @app.post('/api/register')
    async def register(body:RegisterData, request:Request, response:Response):
        require_origin(request.headers)
        with connection(immediate=True) as db:
            room=None
            if body.code is not None:
                room=db.execute('SELECT * FROM rooms WHERE code=?',(body.code.strip().upper(),)).fetchone()
                if not room:
                    raise HTTPException(404,'邀请码无效。')
                ensure_roster_unlocked(db,room)
            username=norm_text(body.username,40)
            try:
                pw_hash=hash_password(body.password)
            except ValueError as exc:
                raise HTTPException(422,str(exc))
            cur=db.execute('INSERT INTO users(username,display_name,pw_hash,is_admin,created_at) VALUES(?,?,?,?,?)',
                           (username,norm_text(body.display_name,40) if body.display_name is not None else username,pw_hash,0,utcnow()))
            uid=cur.lastrowid
            if room:
                alias=new_alias(db,room['id'])
                db.execute('INSERT INTO members(room_id,user_id,alias,joined_at) VALUES(?,?,?,?)',
                           (room['id'],uid,alias,utcnow()))
                log_event(db,room['id'],1,'joined',alias,'加入比赛')
            set_session(response,db,uid)
        result={'ok':True,'uid':user_uid(uid)}
        if room:
            await app.state.hub.notify(room['id'], 'joined')
            result['room_id']=room['id']
        return result

    @app.post('/api/logout')
    def logout(request:Request, response:Response):
        with connection() as db:
            u=caller(request,db,mutate=True)
            tok=request.cookies.get(COOKIE,'')
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(tok.encode()).hexdigest(),))
        response.delete_cookie(COOKIE)
        return {'ok':True}

    @app.post('/api/rooms')
    def create_room(body:RoomData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            code=secrets.token_hex(5).upper()
            cur=db.execute('INSERT INTO rooms(title,code,judge,created_at,owner_id) VALUES(?,?,?,?,?)',
                           (norm_text(body.title,80),code,norm_text(body.judge,40),utcnow(),u['id']))
            room_id=cur.lastrowid
            db.execute('INSERT INTO rounds(room_id,number,created_at) VALUES(?,?,?)',(room_id,1,utcnow()))
            log_event(db,room_id,1,'created','管理员','创建比赛')
            return {'room_id':room_id, 'code':code}

    @app.get('/api/admin/users')
    def list_users(request:Request):
        with connection() as db:
            u=caller(request,db)
            require_admin(u)
            users=db.execute('''SELECT id,username,display_name,is_admin,created_at FROM users
                WHERE deleted_at IS NULL ORDER BY id''').fetchall()
            return {'users':[dict(row,uid=user_uid(row['id'])) for row in users]}

    @app.post('/api/admin/users/{uid}/password')
    async def reset_password(uid:str,body:PasswordResetData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            require_admin(u)
            target=lookup_account(db,uid=uid)
            if not target:
                raise HTTPException(404,'用户不存在或账号已删除。')
            db.execute('UPDATE users SET pw_hash=? WHERE id=?',(hash_password(body.password),target['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(target['id'],))
            for key in (':uid:'+user_uid(target['id']), ':legacy:'+(target['legacy_username'] or '').lower()):
                db.execute('DELETE FROM login_attempts WHERE substr(identity,-length(?))=?',(key,key))
        for room_id in list(app.state.hub.groups):
            await app.state.hub.notify(room_id,'account_sessions_revoked')
        return {'ok':True,'signed_out':target['id']==u['id']}

    @app.delete('/api/admin/users/{uid}')
    async def delete_account(uid:str,body:ConfirmData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            require_admin(u)
            target=lookup_account(db,uid=uid)
            if not target:
                raise HTTPException(404,'用户不存在或账号已删除。')
            if target['is_admin'] and db.execute(
                'SELECT COUNT(*) FROM users WHERE is_admin=1 AND deleted_at IS NULL').fetchone()[0]<=1:
                raise HTTPException(409,'至少需要保留一个管理员账号。')
            # 历史署名和外键保留；清除凭据并撤销会话，不再是可使用的账号。
            db.execute('UPDATE users SET deleted_at=?,pw_hash=?,legacy_username=NULL WHERE id=?',
                       (utcnow(),'',target['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(target['id'],))
            active=db.execute("SELECT * FROM rooms WHERE status='open'").fetchall()
            for room in active:
                member=db.execute('''SELECT alias FROM members WHERE room_id=? AND user_id=?
                    AND removed_at IS NULL''',(room['id'],target['id'])).fetchone()
                if member:
                    db.execute('UPDATE members SET removed_at=? WHERE room_id=? AND user_id=?',
                               (utcnow(),room['id'],target['id']))
                    log_event(db,room['id'],room['current_round'],'account_deleted','管理员',
                              f'参赛者 {member["alias"]} 的账号已删除并退赛；历史回答保留')
                    rnd=current_round(db,room)
                    if rnd['status']=='collecting' and is_complete(db,rnd,room['id']):
                        log_event(db,room['id'],rnd['number'],'round_revealed','系统','在场参赛者已全部提交，统一公开')
        for room_id in list(app.state.hub.groups):
            app.state.hub.typing.pop(room_id,None)
            await app.state.hub.notify(room_id,'account_deleted')
        return {'ok':True,'signed_out':target['id']==u['id']}

    @app.post('/api/join')
    async def join(body:CodeData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room=db.execute('SELECT * FROM rooms WHERE code=?',(body.code.strip().upper(),)).fetchone()
            if not room:
                raise HTTPException(404,'邀请码无效。')
            old=db.execute('SELECT alias FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL',(room['id'],u['id'])).fetchone()
            if old:
                return {'room_id':room['id'],'alias':old['alias']}
            if db.execute('SELECT 1 FROM members WHERE room_id=? AND user_id=? AND removed_at IS NOT NULL',
                          (room['id'],u['id'])).fetchone():
                raise HTTPException(403,'你已被主持人移出这个房间，不能重新加入。')
            ensure_roster_unlocked(db,room)
            if is_judge(db,room['id'],u):
                raise HTTPException(409,'本房间裁判不能作为参赛者加入。')
            alias=new_alias(db,room['id'])
            db.execute('INSERT INTO members(room_id,user_id,alias,joined_at) VALUES(?,?,?,?)',
                       (room['id'],u['id'],alias,utcnow()))
            log_event(db,room['id'],1,'joined',alias,'加入比赛')
            room_id=room['id']
        await app.state.hub.notify(room_id,'joined')
        return {'room_id':room_id,'alias':alias}

    @app.get('/api/rooms/{room_id}')
    def room_info(room_id:int,request:Request):
        with connection() as db:
            u=caller(request,db)
            room,mine=permission(db,room_id,u)
            return public_room(db,room,u,mine)

    @app.post('/api/rooms/{room_id}/models')
    async def create_model_profile(room_id:int,body:ModelProfileData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            try:
                key=(body.api_key or '').strip()
                if not key:
                    raise ModelAPIError('创建模型配置时必须填写 API Key。')
                base=await asyncio.to_thread(validate_base_url,body.base_url)
                encrypted=encrypt_api_key(key)
            except ModelAPIError as exc:
                raise HTTPException(422,str(exc))
            now=utcnow()
            cur=db.execute('''INSERT INTO model_profiles(room_id,name,base_url,model,api_key_encrypted,
                temperature,max_tokens,timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (room_id,norm_text(body.name,60),base,norm_text(body.model,120),encrypted,
                 body.temperature,body.max_tokens,body.timeout_seconds,now,now))
            log_event(db,room_id,room['current_round'],'model_profile_created','房主','新增模型 API 配置')
            profile_id=cur.lastrowid
        await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True,'id':profile_id}

    @app.put('/api/rooms/{room_id}/models/{profile_id}')
    async def update_model_profile(room_id:int,profile_id:int,body:ModelProfileData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            old=db.execute('SELECT * FROM model_profiles WHERE id=? AND room_id=?',(profile_id,room_id)).fetchone()
            if not old:
                raise HTTPException(404,'模型配置不存在。')
            try:
                base=await asyncio.to_thread(validate_base_url,body.base_url)
                key=(body.api_key or '').strip()
                encrypted=encrypt_api_key(key) if key else old['api_key_encrypted']
            except ModelAPIError as exc:
                raise HTTPException(422,str(exc))
            db.execute('''UPDATE model_profiles SET name=?,base_url=?,model=?,api_key_encrypted=?,
                temperature=?,max_tokens=?,timeout_seconds=?,updated_at=? WHERE id=? AND room_id=?''',
                (norm_text(body.name,60),base,norm_text(body.model,120),encrypted,
                 body.temperature,body.max_tokens,body.timeout_seconds,utcnow(),profile_id,room_id))
            log_event(db,room_id,room['current_round'],'model_profile_updated','房主','更新模型 API 配置')
        await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True}

    @app.delete('/api/rooms/{room_id}/models/{profile_id}')
    async def delete_model_profile(room_id:int,profile_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if not db.execute('SELECT 1 FROM model_profiles WHERE id=? AND room_id=?',(profile_id,room_id)).fetchone():
                raise HTTPException(404,'模型配置不存在。')
            db.execute('UPDATE ai_players SET model_profile_id=NULL WHERE room_id=? AND model_profile_id=?',
                       (room_id,profile_id))
            db.execute('UPDATE ai_judges SET model_profile_id=NULL WHERE room_id=? AND model_profile_id=?',
                       (room_id,profile_id))
            db.execute('DELETE FROM model_profiles WHERE id=? AND room_id=?',(profile_id,room_id))
            log_event(db,room_id,room['current_round'],'model_profile_deleted','房主','删除模型 API 配置，关联席位改为手动转录')
        await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/models/{profile_id}/test')
    async def test_model_profile(room_id:int,profile_id:int,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
            require_room_manager(db,room_id,u)
            profile=db.execute('SELECT * FROM model_profiles WHERE id=? AND room_id=?',(profile_id,room_id)).fetchone()
            if not profile:
                raise HTTPException(404,'模型配置不存在。')
            profile=dict(profile)
        try:
            key=decrypt_api_key(profile['api_key_encrypted'])
            started=time.perf_counter()
            result=await model_call(profile,[
                {'role':'system','content':'You are a connection test. Reply with exactly OK.'},
                {'role':'user','content':'Reply with OK.'}],key)
        except ModelAPIError as exc:
            raise HTTPException(502,str(exc))
        return {'ok':True,'model':profile['model'],'preview':result['text'][:120],
                'latency_ms':round((time.perf_counter()-started)*1000), 'usage':result['usage']}

    @app.delete('/api/rooms/{room_id}')
    async def delete_room(room_id:int,body:MatchActionData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            check_match_number(room,body.match_number)
            db.execute('DELETE FROM rooms WHERE id=?',(room_id,))
        app.state.hub.typing.pop(room_id,None)
        await app.state.hub.notify(room_id,'room_deleted')
        return {'ok':True}

    @app.delete('/api/rooms/{room_id}/records')
    async def delete_records(room_id:int,body:MatchActionData,request:Request):
        with connection(immediate=True) as db:
            caller(request,db,mutate=True)
            room=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
            if not room:
                raise HTTPException(404,'房间不存在。')
            check_match_number(room,body.match_number)
            if room['status']!='finished':
                raise HTTPException(409,'只能删除已经完成比赛的记录。')
            clear_match_records(db,room_id)
        app.state.hub.typing.pop(room_id,None)
        await app.state.hub.notify(room_id,'records_deleted')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/restart')
    async def restart_match(room_id:int,body:MatchActionData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            check_match_number(room,body.match_number)
            if room['status']!='finished':
                raise HTTPException(409,'必须先完成当前比赛才能再次开赛。')
            clear_match_records(db,room_id)
            db.execute('''DELETE FROM members WHERE room_id=? AND (removed_at IS NOT NULL OR
                user_id IN (SELECT id FROM users WHERE deleted_at IS NOT NULL))''',(room_id,))
            db.execute('''DELETE FROM room_judges WHERE room_id=? AND user_id IN
                (SELECT id FROM users WHERE deleted_at IS NOT NULL)''',(room_id,))
            db.execute('''UPDATE rooms SET status='open',current_round=1,records_deleted=0,
                match_number=match_number+1 WHERE id=?''',(room_id,))
            db.execute('INSERT INTO rounds(room_id,number,created_at) VALUES(?,?,?)',(room_id,1,utcnow()))
            log_event(db,room_id,1,'match_restarted','主持人','重新开赛，上一场比赛记录已清空')
        app.state.hub.typing.pop(room_id,None)
        await app.state.hub.notify(room_id,'match_restarted')
        return {'ok':True,'match_number':room['match_number']+1}

    @app.post('/api/rooms/{room_id}/judges')
    async def add_judge(room_id:int,body:JudgeAccountData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            username=norm_text(body.username,40)
            check_judge_registration(db,room,-1)
            try:
                pw_hash=hash_password(body.password)
            except ValueError as exc:
                raise HTTPException(422,str(exc))
            label=norm_text(body.display_name,40)
            cur=db.execute('INSERT INTO users(username,display_name,pw_hash,is_admin,created_at) VALUES(?,?,?,?,?)',
                           (username,label,pw_hash,0,utcnow()))
            db.execute('INSERT INTO room_judges(room_id,user_id,label,created_at) VALUES(?,?,?,?)',
                       (room_id,cur.lastrowid,label,utcnow()))
            uid=user_uid(cur.lastrowid)
            log_event(db,room_id,room['current_round'],'judge_added','管理员','已添加独立裁判')
        await app.state.hub.notify(room_id,'judge_added')
        return {'ok':True,'uid':uid,'username':username,'label':label}

    @app.post('/api/rooms/{room_id}/judges/assign')
    async def assign_judge(room_id:int,body:ExistingJudgeData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            target=lookup_account(db, body.uid, body.username)
            if not target or target['is_admin']:
                raise HTTPException(404,'用户不存在或不可作为独立裁判。')
            if is_judge(db,room_id,{'id':target['id']}):
                raise HTTPException(409,'此账号已经是本房间裁判。')
            check_judge_registration(db,room,target['id'])
            label=norm_text(body.label,40)
            db.execute('INSERT INTO room_judges(room_id,user_id,label,created_at) VALUES(?,?,?,?)',
                       (room_id,target['id'],label,utcnow()))
            log_event(db,room_id,room['current_round'],'judge_added','管理员','已绑定独立裁判账号')
        await app.state.hub.notify(room_id,'judge_added')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/ai-judges')
    async def add_ai_judge(room_id:int,body:AIData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            check_judge_registration(db,room,-1)
            label=norm_text(body.name,60)
            if body.model_profile_id is not None:
                profile=db.execute('SELECT api_key_encrypted FROM model_profiles WHERE id=? AND room_id=?',
                                   (body.model_profile_id,room_id)).fetchone()
                if not profile:
                    raise HTTPException(404,'模型配置不存在。')
                try:
                    decrypt_api_key(profile['api_key_encrypted'])
                except ModelAPIError as exc:
                    raise HTTPException(503,str(exc))
            cur=db.execute('INSERT INTO ai_judges(room_id,label,model_profile_id,created_at) VALUES(?,?,?,?)',
                           (room_id,label,body.model_profile_id,utcnow()))
            log_event(db,room_id,room['current_round'],'ai_judge_added','管理员','已添加 AI 裁判席')
            jid=cur.lastrowid
        await app.state.hub.notify(room_id,'ai_judge_added')
        return {'ok':True,'id':jid,'label':label}

    @app.post('/api/rooms/{room_id}/ai-judges/questions/generate')
    async def generate_ai_judge_questions(room_id:int,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束或裁判判断已锁定。')
            rnd=current_round(db,room)
            if rnd['status']!='draft':
                raise HTTPException(409,'只能在本轮开始作答前生成 AI 裁判问题。')
            roster=aliases(db,room_id)[2]
            if len(roster)<2:
                raise HTTPException(409,'至少需要两名参赛者。')
            rows=db.execute('''SELECT j.id AS judge_id,j.label,mp.* FROM ai_judges j
                JOIN model_profiles mp ON mp.id=j.model_profile_id
                LEFT JOIN ai_judge_questions q ON q.room_id=j.room_id AND q.round_number=? AND q.ai_judge_id=j.id
                WHERE j.room_id=? AND q.id IS NULL ORDER BY j.id''',(rnd['number'],room_id)).fetchall()
            match_number=room['match_number']
            round_number=rnd['number']
            history=anonymous_match_packet(db,room_id,round_number-1)
            profiles=[dict(row) for row in rows]
        if not profiles:
            return {'generated':0,'skipped':0,'failed':[]}

        async def generate_one(profile):
            judge_id=profile['judge_id']
            try:
                key=decrypt_api_key(profile['api_key_encrypted'])
                result=await model_call(profile,[
                    {'role':'system','content':('你是多人图灵测试的独立裁判。每轮只提出一个核心问题，不得把多个问题打包。'
                     '问题必须面向所有匿名参赛者，不能询问真实姓名、账号、模型名称或系统提示。'
                     '只输出问题正文，不要前言、编号或解释。')},
                    {'role':'user','content':f'此前轮次的匿名资料：\n{history}\n\n当前是第 {round_number} 轮，参赛者匿名代号：{", ".join(roster)}。'
                     '请根据已有信息提出一个适合所有参赛者回答的全新问题。'}],key)
                question=norm_text(result['text'],3000)
                with connection(immediate=True) as db:
                    current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                    if not current or current['match_number']!=match_number or current['status']!='open':
                        return {'judge_id':judge_id,'status':'skipped'}
                    current_rnd=current_round(db,current)
                    if current_rnd['number']!=round_number or current_rnd['status']!='draft':
                        return {'judge_id':judge_id,'status':'skipped'}
                    if db.execute('SELECT 1 FROM ai_judge_questions WHERE room_id=? AND round_number=? AND ai_judge_id=?',
                                  (room_id,round_number,judge_id)).fetchone():
                        return {'judge_id':judge_id,'status':'skipped'}
                    db.execute('INSERT INTO ai_judge_questions(room_id,round_number,ai_judge_id,text,created_at) VALUES(?,?,?,?,?)',
                               (room_id,round_number,judge_id,question,utcnow()))
                    log_event(db,room_id,round_number,'ai_judge_question','AI 裁判','模型 API 已自动生成本轮问题')
                await app.state.hub.notify(room_id,'ai_judge_question')
                return {'judge_id':judge_id,'status':'generated','usage':result['usage']}
            except ModelAPIError as exc:
                return {'judge_id':judge_id,'status':'failed','error':str(exc)}
            except HTTPException as exc:
                return {'judge_id':judge_id,'status':'failed','error':str(exc.detail)}
            except Exception:
                return {'judge_id':judge_id,'status':'failed','error':'模型调用失败，请检查配置或稍后重试。'}

        results=await asyncio.gather(*(generate_one(profile) for profile in profiles))
        return {'generated':sum(x['status']=='generated' for x in results),
                'skipped':sum(x['status']=='skipped' for x in results),
                'failed':[x for x in results if x['status']=='failed'],'results':results}

    @app.post('/api/rooms/{room_id}/ai-judges/{judge_id}/questions')
    async def ai_judge_question(room_id:int,judge_id:int,body:TextData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if not db.execute('SELECT 1 FROM ai_judges WHERE room_id=? AND id=?',
                              (room_id,judge_id)).fetchone():
                raise HTTPException(404,'AI 裁判席不存在。')
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束。')
            rnd=current_round(db,room)
            if rnd['status']!='draft':
                raise HTTPException(409,'仅可在本轮开启作答前录入 AI 裁判问题。')
            if db.execute('SELECT 1 FROM ai_judge_questions WHERE room_id=? AND round_number=? AND ai_judge_id=?',
                          (room_id,rnd['number'],judge_id)).fetchone():
                raise HTTPException(409,'此 AI 裁判本轮已提交问题。')
            _,_,roster=aliases(db,room_id)
            if len(roster)<2:
                raise HTTPException(409,'至少需要两名参赛者。')
            msg=norm_text(body.text,3000)
            db.execute('INSERT INTO ai_judge_questions(room_id,round_number,ai_judge_id,text,created_at) VALUES(?,?,?,?,?)',
                       (room_id,rnd['number'],judge_id,msg,utcnow()))
            log_event(db,room_id,rnd['number'],'ai_judge_question','管理员','已转录 AI 裁判问题')
        await app.state.hub.notify(room_id,'ai_judge_question')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/judge-questions')
    async def judge_question(room_id:int,body:TextData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=permission(db,room_id,u)
            label=require_judge(db,room_id,u)['label']
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束。')
            rnd=current_round(db,room)
            if rnd['status']!='draft':
                raise HTTPException(409,'仅可在本轮尚未开始作答时提出问题。')
            if db.execute('SELECT 1 FROM judge_questions WHERE room_id=? AND round_number=? AND judge_user_id=?',
                          (room_id,rnd['number'],u['id'])).fetchone():
                raise HTTPException(409,'你已经提交本轮问题；每位裁判每轮限提交一次。')
            _,_,roster=aliases(db,room_id)
            if len(roster)<2:
                raise HTTPException(409,'至少需要两名参赛者。')
            text=norm_text(body.text,3000)
            db.execute('INSERT INTO judge_questions(room_id,round_number,judge_user_id,text,created_at) VALUES(?,?,?,?,?)',
                       (room_id,rnd['number'],u['id'],text,utcnow()))
            # 审计日志只记录提交动作，不存入账号信息或答案。
            log_event(db,room_id,rnd['number'],'judge_question_proposed','裁判','已提交本轮问题')
        await app.state.hub.notify(room_id,'judge_question_proposed')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/start')
    async def start_round(room_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束。')
            rnd=current_round(db,room)
            if rnd['status']!='draft':
                raise HTTPException(409,'本轮已经开始。')
            if len(aliases(db,room_id)[2])<2:
                raise HTTPException(409,'至少需要两名在场参赛者，才能开始统一作答。')
            qs=db.execute('''SELECT q.text,q.created_at,j.label,'human' AS kind,q.id
                FROM judge_questions q JOIN room_judges j
                ON j.room_id=q.room_id AND j.user_id=q.judge_user_id
                WHERE q.room_id=? AND q.round_number=?
                UNION ALL
                SELECT q.text,q.created_at,j.label,'ai' AS kind,q.id
                FROM ai_judge_questions q JOIN ai_judges j ON j.id=q.ai_judge_id
                WHERE q.room_id=? AND q.round_number=?
                ORDER BY 2,4,5''',(room_id,rnd['number'],room_id,rnd['number'])).fetchall()
            if not qs:
                raise HTTPException(409,'还没有裁判提交问题。')
            question='\n\n'.join(f'【裁判问题 {i}】\n{q["text"]}' for i,q in enumerate(qs,1))
            if len(question)>6000:
                raise HTTPException(409,'裁判问题合计超过 6000 字，请减少内容后重新开局。')
            db.execute('UPDATE rounds SET question=?,status=? WHERE id=?',(question,'collecting',rnd['id']))
            log_event(db,room_id,rnd['number'],'judge_question','独立裁判组','主持人已开启统一作答')
        await app.state.hub.notify(room_id,'judge_question')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/ai')
    async def add_ai(room_id:int,body:AIData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            ensure_roster_unlocked(db,room)
            alias=new_alias(db,room_id)
            if body.model_profile_id is not None:
                profile=db.execute('SELECT api_key_encrypted FROM model_profiles WHERE id=? AND room_id=?',
                                   (body.model_profile_id,room_id)).fetchone()
                if not profile:
                    raise HTTPException(404,'模型配置不存在。')
                try:
                    decrypt_api_key(profile['api_key_encrypted'])
                except ModelAPIError as exc:
                    raise HTTPException(503,str(exc))
            db.execute('INSERT INTO ai_players(room_id,alias,name,model_profile_id) VALUES(?,?,?,?)',
                       (room_id,alias,norm_text(body.name,60),body.model_profile_id))
            log_event(db,room_id,1,'ai_joined',alias,'AI 参赛位已添加')
        await app.state.hub.notify(room_id,'ai_joined')
        return {'alias':alias,'automated':body.model_profile_id is not None}

    @app.post('/api/rooms/{room_id}/question')
    async def set_question(room_id:int,body:TextData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open':
                raise HTTPException(409,'比赛已经结束。')
            rnd=current_round(db,room)
            if rnd['status']!='draft':
                raise HTTPException(409,'本轮问题已经发布，无法更改。')
            if judge_records(db,room_id):
                raise HTTPException(409,'已启用真人 / AI 独立裁判，请由各裁判出题或由管理员代理 AI 出题。')
            _,_,roster=aliases(db,room_id)
            if len(roster)<2:
                raise HTTPException(409,'至少要有两名参赛者才能开始。')
            question=norm_text(body.text,6000)
            db.execute('UPDATE rounds SET question=?,status=? WHERE id=?',(question,'collecting',rnd['id']))
            log_event(db,room_id,rnd['number'],'judge_question',room['judge'],question)
        await app.state.hub.notify(room_id,'judge_question')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/suggestions')
    async def suggest(room_id:int,body:TextData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,mine=permission(db,room_id,u)
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已经停止接收问题。')
            if is_judge(db,room_id,u) and not u['is_admin']:
                raise HTTPException(403,'裁判请使用独立裁判提问区。')
            msg=norm_text(body.text,2000)
            db.execute('INSERT INTO suggestions(room_id,round_number,sender_id,text,created_at) VALUES(?,?,?,?,?)',
                       (room_id,room['current_round'],u['id'],msg,utcnow()))
            log_event(db,room_id,room['current_round'],'suggestion',mine or '管理员',msg)
        await app.state.hub.notify(room_id,'suggestion')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/answers')
    async def answer(room_id:int,body:TextData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,mine=permission(db,room_id,u)
            if not mine:
                raise HTTPException(403,'你没有作为参赛者加入该房间。管理员可先输入房间邀请码加入。')
            if room['status']!='open':
                raise HTTPException(409,'比赛已结束。')
            rnd=current_round(db,room)
            if rnd['status']!='collecting':
                raise HTTPException(409,'本轮目前不能提交回答。')
            if db.execute('SELECT 1 FROM answers WHERE round_id=? AND alias=?',(rnd['id'],mine)).fetchone():
                raise HTTPException(409,'本轮已经提交，不能修改回答。')
            ans=norm_text(body.text,12000)
            db.execute('INSERT INTO answers(round_id,alias,text,submitted_at) VALUES(?,?,?,?)',
                       (rnd['id'],mine,ans,utcnow()))
            log_event(db,room_id,rnd['number'],'answer_submitted',mine,'已提交回答（内容暂时隐藏）')
            finished=is_complete(db,rnd,room_id)
            if finished:
                log_event(db,room_id,rnd['number'],'round_revealed','系统','本轮全部回答已公开')
        await app.state.hub.notify(room_id,'round_revealed' if finished else 'answer_submitted')
        return {'ok':True,'revealed':finished}

    @app.post('/api/rooms/{room_id}/ai/{alias}/answer')
    async def ai_answer(room_id:int,alias:str,body:TextData,request:Request):
        alias=alias.upper()
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open':
                raise HTTPException(409,'比赛已结束。')
            if not db.execute('SELECT 1 FROM ai_players WHERE room_id=? AND alias=?',(room_id,alias)).fetchone():
                raise HTTPException(404,'没有这个 AI 参赛位。')
            rnd=current_round(db,room)
            if rnd['status']!='collecting':
                raise HTTPException(409,'本轮不能录入回答。')
            if db.execute('SELECT 1 FROM answers WHERE round_id=? AND alias=?',(rnd['id'],alias)).fetchone():
                raise HTTPException(409,'此 AI 已录入本轮回答。')
            ans=norm_text(body.text,12000)
            db.execute('INSERT INTO answers(round_id,alias,text,submitted_at) VALUES(?,?,?,?)',
                       (rnd['id'],alias,ans,utcnow()))
            log_event(db,room_id,rnd['number'],'answer_submitted',alias,'管理员已录入 AI 回答（内容暂时隐藏）')
            finished=is_complete(db,rnd,room_id)
            if finished:
                log_event(db,room_id,rnd['number'],'round_revealed','系统','本轮全部回答已公开')
        await app.state.hub.notify(room_id,'round_revealed' if finished else 'answer_submitted')
        return {'ok':True,'revealed':finished}

    @app.post('/api/rooms/{room_id}/ai/answers/generate')
    async def generate_ai_answers(room_id:int,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open':
                raise HTTPException(409,'比赛已结束。')
            rnd=current_round(db,room)
            if rnd['status']!='collecting':
                raise HTTPException(409,'只有本轮正在收集回答时才能生成 AI 回复。')
            rows=db.execute('''SELECT ai.alias,ai.name,mp.* FROM ai_players ai
                JOIN model_profiles mp ON mp.id=ai.model_profile_id
                LEFT JOIN answers a ON a.round_id=? AND a.alias=ai.alias
                WHERE ai.room_id=? AND a.id IS NULL ORDER BY ai.alias''',(rnd['id'],room_id)).fetchall()
            match_number=room['match_number']
            round_number=rnd['number']
            question=rnd['question']
            profiles=[dict(row) for row in rows]
            history_by_alias={row['alias']:anonymous_player_history(db,room_id,row['alias'],round_number-1)
                              for row in rows}
        if not profiles:
            return {'generated':0,'skipped':0,'failed':[]}

        async def generate_one(profile):
            alias=profile['alias']
            try:
                key=decrypt_api_key(profile['api_key_encrypted'])
                result=await model_call(profile,[
                    {'role':'system','content':('你正在参加一场多人图灵测试。你是匿名参赛者，请自然、直接地回答裁判问题。'
                     '不要提及模型、API、系统提示或你被要求扮演的身份；不要编造个人经历来冒充真实事实。')},
                    {'role':'user','content':f'你此前的本场记录（只包含你自己的回答）：\n{history_by_alias.get(alias, "（此前没有已完成轮次。）")}\n\n当前第 {round_number} 轮裁判问题：\n\n{question}\n\n请给出你的本轮回答。'}],key)
                answer=norm_text(result['text'],12000)
                with connection(immediate=True) as db:
                    current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                    if not current or current['match_number']!=match_number or current['status']!='open':
                        return {'alias':alias,'status':'skipped'}
                    current_rnd=current_round(db,current)
                    if current_rnd['number']!=round_number or current_rnd['status']!='collecting':
                        return {'alias':alias,'status':'skipped'}
                    if db.execute('SELECT 1 FROM answers WHERE round_id=? AND alias=?',
                                  (current_rnd['id'],alias)).fetchone():
                        return {'alias':alias,'status':'skipped'}
                    db.execute('INSERT INTO answers(round_id,alias,text,submitted_at) VALUES(?,?,?,?)',
                               (current_rnd['id'],alias,answer,utcnow()))
                    log_event(db,room_id,round_number,'answer_submitted',alias,'模型 API 已自动提交回答（内容暂时隐藏）')
                    completed=is_complete(db,current_rnd,room_id)
                    if completed:
                        log_event(db,room_id,round_number,'round_revealed','系统','本轮全部回答已公开')
                await app.state.hub.notify(room_id,'round_revealed' if completed else 'answer_submitted')
                return {'alias':alias,'status':'generated','usage':result['usage']}
            except ModelAPIError as exc:
                return {'alias':alias,'status':'failed','error':str(exc)}
            except HTTPException as exc:
                return {'alias':alias,'status':'failed','error':str(exc.detail)}
            except Exception:
                return {'alias':alias,'status':'failed','error':'模型调用失败，请检查配置或稍后重试。'}

        results=await asyncio.gather(*(generate_one(profile) for profile in profiles))
        return {'generated':sum(x['status']=='generated' for x in results),
                'skipped':sum(x['status']=='skipped' for x in results),
                'failed':[x for x in results if x['status']=='failed'],
                'results':results}

    @app.put('/api/rooms/{room_id}/ai/{alias}/answer')
    async def revise_ai_answer(room_id:int,alias:str,body:AIAnswerEditData,request:Request):
        alias=alias.upper()
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open' or room['revealed_at'] or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束或判决已锁定，不能修改 AI 回答。')
            if db.execute('SELECT 1 FROM judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone() or db.execute(
                'SELECT 1 FROM ai_judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone():
                raise HTTPException(409,'已有裁判锁定判断，不能修改已经展示的回答。')
            if not db.execute('SELECT 1 FROM ai_players WHERE room_id=? AND alias=?',(room_id,alias)).fetchone():
                raise HTTPException(404,'AI 参赛位不存在。')
            rnd=current_round(db,room)
            if rnd['status'] not in ('collecting','revealed'):
                raise HTTPException(409,'仅能修改当前轮次的已提交 AI 回答。')
            old=db.execute('SELECT text FROM answers WHERE round_id=? AND alias=?',(rnd['id'],alias)).fetchone()
            if not old:
                raise HTTPException(409,'该 AI 本轮尚未提交回复。')
            text=norm_text(body.text,12000)
            if text==old['text']:
                return {'ok':True,'changed':False}
            now=utcnow()
            db.execute('INSERT INTO ai_answer_revisions(room_id,round_number,alias,previous_text,replacement_text,changed_at) VALUES(?,?,?,?,?,?)',
                       (room_id,rnd['number'],alias,old['text'],text,now))
            db.execute('UPDATE answers SET text=?, edited_at=? WHERE round_id=? AND alias=?',
                       (text,now,rnd['id'],alias))
            log_event(db,room_id,rnd['number'],'ai_answer_revised','管理员',f'修订了 AI 参赛者 {alias} 的回答（有历史版本存档）')
        await app.state.hub.notify(room_id,'ai_answer_revised')
        return {'ok':True,'changed':True}

    @app.post('/api/rooms/{room_id}/members/{user_id}/remove')
    async def remove_member(room_id:int,user_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open' or room['verdict_locked_at'] or db.execute(
                'SELECT 1 FROM judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone() or db.execute(
                'SELECT 1 FROM ai_judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone():
                raise HTTPException(409,'比赛结束或裁判开始锁定最终判断后不可移除参赛者。')
            member=db.execute('SELECT alias FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL',
                              (room_id,user_id)).fetchone()
            if not member:
                raise HTTPException(404,'此用户并非当前房间的参赛者。')
            _,_,roster=aliases(db,room_id)
            if len(roster)<=2:
                raise HTTPException(409,'移除后至少应保留两名参赛者。')
            db.execute('UPDATE members SET removed_at=? WHERE room_id=? AND user_id=?',
                       (utcnow(),room_id,user_id))
            log_event(db,room_id,room['current_round'],'member_removed','管理员',f'移除参赛者 {member["alias"]}；历史回答保留')
            rnd=current_round(db,room)
            finished=False
            if rnd['status']=='collecting':
                finished=is_complete(db,rnd,room_id)
                if finished:
                    log_event(db,room_id,rnd['number'],'round_revealed','系统','当前在场参赛者已全部提交，统一公开')
        await app.state.hub.notify(room_id,'round_revealed' if finished else 'member_removed')
        return {'ok':True,'revealed':finished}

    @app.post('/api/rooms/{room_id}/next')
    async def next_round(room_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if len(aliases(db,room_id)[2])<2:
                raise HTTPException(409,'在场参赛者不足两人，请结束本场后重新开赛。')
            if room['status']!='open' or room['verdict_locked_at'] or db.execute(
                'SELECT 1 FROM judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone() or db.execute(
                'SELECT 1 FROM ai_judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone():
                raise HTTPException(409,'比赛已结束或已有裁判锁定最终判断，不能再开启新轮次。')
            rnd=current_round(db,room)
            if rnd['status']!='revealed':
                raise HTTPException(409,'必须先完成本轮全部回答。')
            if rnd['number']>=MAX_ROUNDS:
                raise HTTPException(409,'轮次数量已达上限。')
            next_num=rnd['number']+1
            db.execute('INSERT INTO rounds(room_id,number,created_at) VALUES(?,?,?)',
                       (room_id,next_num,utcnow()))
            db.execute('UPDATE rooms SET current_round=? WHERE id=?',(next_num,room_id))
            log_event(db,room_id,next_num,'new_round','管理员','进入下一轮')
        app.state.hub.typing.pop(room_id,None)
        await app.state.hub.notify(room_id,'new_round')
        return {'number':next_num}

    @app.get('/api/rooms/{room_id}/rounds/{number}/log')
    def download_log(room_id:int,number:int,request:Request):
        with connection() as db:
            u=caller(request,db)
            room,mine=permission(db,room_id,u)
            current_round(db,room,number)
            content=round_markdown(db,room,number,u,mine)
        return PlainTextResponse(content,media_type='text/markdown; charset=utf-8',headers={
            'Content-Disposition':f'attachment; filename="room-{room_id}-round-{number}.md"',
            'Cache-Control':'no-store'})

    @app.get('/api/rooms/{room_id}/log')
    def full_match_log(room_id:int,request:Request):
        with connection() as db:
            u=caller(request,db)
            room,mine=permission(db,room_id,u)
            if room['status']!='finished':
                raise HTTPException(409,'比赛尚未结束，不能下载整场日志。')
            if room['records_deleted']:
                raise HTTPException(409,'这场比赛的记录已被清除。')
            data=public_room(db,room,u,mine)
            lines=[f'# {room["title"]} · 整场比赛 Log', '',
                   f'轮次：{room["current_round"]} · 结束时间：{room["revealed_at"]}',
                   '']
            for num in range(1,room['current_round']+1):
                lines.append(round_markdown(db,room,num,u,mine))
            if data['is_judge']:
                lines.extend(['## 我的独立裁判判决', ''])
                verdict=data['my_verdict']
                if verdict:
                    lines.extend([f'{a}：{v}' for a,v in sorted(verdict['predictions'].items())])
                    lines += ['',f'判断依据：{verdict.get("evidence", "")}', '']
                lines.append('本文件仅为匿名裁判私人记录，不含真人昵称及其他裁判判决。')
            else:
                lines += ['## 最终裁判判决', '']
                for v in data['verdicts']:
                    lines += [f'### {v["judge"]}',
                        *[f'- {a}: {guess}' for a,guess in sorted(v.get('predictions',{}).items())],
                        f'- 判断依据：{v.get("evidence", "")}',
                        f'- 置信度：{v.get("confidence", "?")}', '']
            content='\n'.join(lines)+'\n'
        return PlainTextResponse(content,media_type='text/markdown; charset=utf-8',headers={
            'Content-Disposition':f'attachment; filename="room-{room_id}-full-log.md"',
            'Cache-Control':'no-store'})

    @app.post('/api/rooms/{room_id}/packets')
    async def create_packet(room_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['revealed_at']:
                raise HTTPException(409,'已揭晓，不能再制作匿名裁判包。')
            rnd=current_round(db,room)
            last=rnd['number'] if rnd['status']=='revealed' else rnd['number']-1
            if last<1:
                raise HTTPException(409,'目前尚无完成的轮次。')
            packet=judge_packet(db,room,last)
            previous=db.execute('SELECT COALESCE(MAX(version),0) AS n FROM packets WHERE room_id=? AND through_round=?',
                                (room_id,last)).fetchone()['n']
            checksum=hashlib.sha256(packet.encode()).hexdigest()
            cur=db.execute('INSERT INTO packets(room_id,through_round,version,content,sha256,created_at) VALUES(?,?,?,?,?,?)',
                           (room_id,last,previous+1,packet,checksum,utcnow()))
            pid=cur.lastrowid
            log_event(db,room_id,last,'packet_created','管理员',f'已制作裁判包 v{previous+1}，SHA256 {checksum[:12]}')
        await app.state.hub.notify(room_id,'packet_created')
        return {'packet_id':pid,'version':previous+1,'through_round':last,'sha256':checksum}

    @app.get('/api/rooms/{room_id}/packets/{packet_id}')
    def packet_text(room_id:int,packet_id:int,request:Request):
        with connection() as db:
            u=caller(request,db)
            require_room_manager(db,room_id,u)
            r=db.execute('SELECT * FROM packets WHERE id=? AND room_id=?',(packet_id,room_id)).fetchone()
            if not r:
                raise HTTPException(404,'没有这个裁判包。')
            if hashlib.sha256(r['content'].encode()).hexdigest()!=r['sha256']:
                raise HTTPException(500,'快照校验失败。')
            return {'id':r['id'],'content':r['content'],'sha256':r['sha256'],
                    'through_round':r['through_round'],'version':r['version'],'sent_at':r['sent_at']}

    @app.post('/api/rooms/{room_id}/packets/{packet_id}/sent')
    async def mark_sent(room_id:int,packet_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['revealed_at']:
                raise HTTPException(409,'已揭晓。')
            r=db.execute('SELECT * FROM packets WHERE id=? AND room_id=?',(packet_id,room_id)).fetchone()
            if not r:
                raise HTTPException(404,'裁判包不存在。')
            db.execute('UPDATE packets SET sent_at=? WHERE id=?',(utcnow(),packet_id))
            log_event(db,room_id,r['through_round'],'packet_sent','管理员',f'确认已手动发送裁判包 v{r["version"]}')
        await app.state.hub.notify(room_id,'packet_sent')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/verdict')
    async def lock_verdict(room_id:int,body:VerdictData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'裁判结论已锁定或比赛已结束。')
            if judge_records(db,room_id):
                raise HTTPException(409,'本房间启用了独立裁判，请由裁判本人登录后锁定判断。')
            _,_,roster=aliases(db,room_id)
            if set(body.predictions)!=set(roster) or not all(v in ('Human','AI') for v in body.predictions.values()):
                raise HTTPException(422,'需要为全部参赛者选择人类 / AI。')
            rows=db.execute('SELECT status FROM rounds WHERE room_id=?',(room_id,)).fetchall()
            if not rows or any(r['status']!='revealed' for r in rows):
                raise HTTPException(409,'所有已创建的轮次必须结束后才能锁定结论。')
            payload=body.model_dump()
            payload['locked_at']=utcnow()
            db.execute('UPDATE rooms SET verdict_json=?,verdict_locked_at=? WHERE id=?',
                       (json.dumps(payload,ensure_ascii=False),utcnow(),room_id))
            log_event(db,room_id,room['current_round'],'verdict_locked','管理员','裁判结论已锁定（揭晓前保密）')
        await app.state.hub.notify(room_id,'verdict_locked')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/judge-verdict')
    async def lock_judge_verdict(room_id:int,body:VerdictData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=permission(db,room_id,u)
            require_judge(db,room_id,u)
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束或历史判决已锁定。')
            if db.execute('SELECT 1 FROM judge_verdicts WHERE room_id=? AND judge_user_id=?',
                          (room_id,u['id'])).fetchone():
                raise HTTPException(409,'你的裁判结论已锁定，不能修改。')
            _,_,roster=aliases(db,room_id)
            if set(body.predictions)!=set(roster) or not all(
                v in ('Human','AI') for v in body.predictions.values()):
                raise HTTPException(422,'必须给每个匿名代号选择 Human 或 AI。')
            rounds=db.execute('SELECT status FROM rounds WHERE room_id=?',(room_id,)).fetchall()
            if not rounds or any(r['status']!='revealed' for r in rounds):
                raise HTTPException(409,'所有已创建轮次必须统一揭晓后才能锁定结论。')
            payload=body.model_dump()
            payload['locked_at']=utcnow()
            db.execute('INSERT INTO judge_verdicts(room_id,judge_user_id,verdict_json,locked_at) VALUES(?,?,?,?)',
                       (room_id,u['id'],json.dumps(payload,ensure_ascii=False),utcnow()))
            log_event(db,room_id,room['current_round'],'judge_verdict_locked','裁判','独立裁判已锁定判断（不包含判决内容）')
        await app.state.hub.notify(room_id,'judge_verdict_locked')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/ai-judges/verdicts/generate')
    async def generate_ai_judge_verdicts(room_id:int,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束或总判决已锁定。')
            rounds=db.execute('SELECT status FROM rounds WHERE room_id=? ORDER BY number',(room_id,)).fetchall()
            if not rounds or any(r['status']!='revealed' for r in rounds):
                raise HTTPException(409,'所有轮次都完成后，AI 裁判才能做出最终判断。')
            roster=aliases(db,room_id)[2]
            packet=anonymous_match_packet(db,room_id)
            rows=db.execute('''SELECT j.id AS judge_id,j.label,mp.* FROM ai_judges j
                JOIN model_profiles mp ON mp.id=j.model_profile_id
                LEFT JOIN ai_judge_verdicts v ON v.room_id=j.room_id AND v.ai_judge_id=j.id
                WHERE j.room_id=? AND v.ai_judge_id IS NULL ORDER BY j.id''',(room_id,)).fetchall()
            match_number=room['match_number']
            profiles=[dict(row) for row in rows]
        if not profiles:
            return {'generated':0,'skipped':0,'failed':[]}

        async def generate_one(profile):
            judge_id=profile['judge_id']
            try:
                key=decrypt_api_key(profile['api_key_encrypted'])
                result=await model_call(profile,[
                    {'role':'system','content':('你是多人图灵测试的独立裁判。你必须只根据匿名代号及参赛者回答，判断每个代号实际是人类还是 AI。'
                     '不能访问或猜测真实身份、模型来源、账号资料。对所有代号都必须给出判断。'
                     '严格返回 JSON 对象，不要使用 Markdown 代码块或附加说明。结构：'
                     '{"predictions":{"A":"Human"},"evidence":"判断依据","confidence":0,"weakness":"最不确定之处","possible_error":"可能误判原因"}。'
                     'predictions 的值只能是 Human 或 AI，confidence 必须为 0 到 100 的整数。')},
                    {'role':'user','content':packet}],key)
                payload=parse_json_object(result['text'])
                verdict=VerdictData.model_validate(payload)
                if set(verdict.predictions)!=set(roster) or not all(v in ('Human','AI') for v in verdict.predictions.values()):
                    raise ModelAPIError('AI 裁判没有为所有匿名代号给出有效的 Human / AI 判断。')
                if not verdict.evidence.strip():
                    raise ModelAPIError('AI 裁判没有提供判断依据。')
                with connection(immediate=True) as db:
                    current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                    if not current or current['match_number']!=match_number or current['status']!='open':
                        return {'judge_id':judge_id,'status':'skipped'}
                    states=db.execute('SELECT status FROM rounds WHERE room_id=?',(room_id,)).fetchall()
                    if not states or any(x['status']!='revealed' for x in states):
                        return {'judge_id':judge_id,'status':'skipped'}
                    if db.execute('SELECT 1 FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
                                  (room_id,judge_id)).fetchone():
                        return {'judge_id':judge_id,'status':'skipped'}
                    locked=utcnow()
                    data=verdict.model_dump()
                    data['locked_at']=locked
                    db.execute('INSERT INTO ai_judge_verdicts(room_id,ai_judge_id,verdict_json,locked_at) VALUES(?,?,?,?)',
                               (room_id,judge_id,json.dumps(data,ensure_ascii=False),locked))
                    log_event(db,room_id,current['current_round'],'ai_judge_verdict_locked','AI 裁判','模型 API 已自动完成最终判断并锁定')
                await app.state.hub.notify(room_id,'ai_judge_verdict_locked')
                return {'judge_id':judge_id,'status':'generated','usage':result['usage']}
            except ModelAPIError as exc:
                return {'judge_id':judge_id,'status':'failed','error':str(exc)}
            except Exception as exc:
                return {'judge_id':judge_id,'status':'failed','error':'AI 裁判返回格式无效或模型调用失败，请检查配置后重试。'}

        results=await asyncio.gather(*(generate_one(profile) for profile in profiles))
        return {'generated':sum(x['status']=='generated' for x in results),
                'skipped':sum(x['status']=='skipped' for x in results),
                'failed':[x for x in results if x['status']=='failed'],'results':results}

    @app.post('/api/rooms/{room_id}/ai-judges/{judge_id}/verdict')
    async def lock_ai_judge_verdict(room_id:int,judge_id:int,body:VerdictData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if not db.execute('SELECT 1 FROM ai_judges WHERE room_id=? AND id=?',
                              (room_id,judge_id)).fetchone():
                raise HTTPException(404,'AI 裁判席不存在。')
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束或历史判决已锁定。')
            if db.execute('SELECT 1 FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
                          (room_id,judge_id)).fetchone():
                raise HTTPException(409,'此 AI 裁判判断已锁定，不能修改。')
            _,_,roster=aliases(db,room_id)
            if set(body.predictions)!=set(roster) or not all(
                v in ('Human','AI') for v in body.predictions.values()):
                raise HTTPException(422,'必须给每个匿名代号选择 Human 或 AI。')
            rounds=db.execute('SELECT status FROM rounds WHERE room_id=?',(room_id,)).fetchall()
            if not rounds or any(r['status']!='revealed' for r in rounds):
                raise HTTPException(409,'所有已创建轮次必须统一揭晓后才能锁定结论。')
            payload=body.model_dump()
            payload['locked_at']=utcnow()
            db.execute('INSERT INTO ai_judge_verdicts(room_id,ai_judge_id,verdict_json,locked_at) VALUES(?,?,?,?)',
                       (room_id,judge_id,json.dumps(payload,ensure_ascii=False),utcnow()))
            # 日志不包含内容，防止在最终揭晓前泄露判断。
            log_event(db,room_id,room['current_round'],'ai_judge_verdict_locked','管理员','AI 裁判判决已手动转录并锁定')
        await app.state.hub.notify(room_id,'ai_judge_verdict_locked')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/reveal')
    async def reveal(room_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            judges=judge_records(db,room_id)
            if judges:
                if room['status']!='open' or not all(bool(j['locked']) for j in judges):
                    raise HTTPException(409,'所有独立裁判均需锁定结论后才能揭晓。')
            elif room['status']!='open' or not room['verdict_locked_at']:
                raise HTTPException(409,'必须先锁定裁判最终判断才可揭晓。')
            r=current_round(db,room)
            if r['status']!='revealed':
                raise HTTPException(409,'本轮未完成。')
            db.execute('UPDATE rooms SET status=?,revealed_at=? WHERE id=?',('finished',utcnow(),room_id))
            log_event(db,room_id,room['current_round'],'identity_revealed','管理员','已揭晓真实身份与裁判得分')
        await app.state.hub.notify(room_id,'identity_revealed')
        return {'ok':True}

    @app.websocket('/ws/rooms/{room_id}')
    async def socket(websocket:WebSocket,room_id:int):
        if not check_origin(websocket.headers):
            await websocket.close(code=1008)
            return
        try:
            with connection() as db:
                u=auth(db,websocket.cookies.get(COOKIE))
                permission(db,room_id,u)
        except HTTPException:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        clients=app.state.hub.groups.setdefault(room_id,set())
        clients.add(websocket)
        try:
            await websocket.send_json({'type':'connected','room_id':room_id})
            while True:
                raw=await websocket.receive_text()
                if len(raw)>512:
                    continue
                try:
                    msg=json.loads(raw)
                except (json.JSONDecodeError,TypeError):
                    continue
                if not isinstance(msg,dict) or msg.get('type')!='typing' or type(msg.get('active')) is not bool:
                    continue
                try:
                    with connection() as db:
                        user=auth(db,websocket.cookies.get(COOKIE))
                        room,mine=permission(db,room_id,user)
                        rnd=current_round(db,room)
                        valid=bool(mine) and room['status']=='open' and rnd['status']=='collecting' and (
                            msg.get('round')==rnd['number']) and (
                            'match_number' not in msg or msg['match_number']==room['match_number']) and not db.execute(
                            'SELECT 1 FROM answers WHERE round_id=? AND alias=?',(rnd['id'],mine)).fetchone()
                    existing=app.state.hub.typing.get(room_id,{}).get(websocket)
                    if valid and ((bool(existing) and msg['active']) or (not existing and not msg['active'])):
                        continue
                    if not valid:
                        app.state.hub.typing.get(room_id,{}).pop(websocket,None)
                    elif msg['active']:
                        app.state.hub.typing.setdefault(room_id,{})[websocket]=mine
                    else:
                        app.state.hub.typing.get(room_id,{}).pop(websocket,None)
                    await app.state.hub.typing_status(room_id)
                except HTTPException:
                    await websocket.close(code=1008)
                    break
        except WebSocketDisconnect:
            pass
        finally:
            clients.discard(websocket)
            app.state.hub.typing.get(room_id,{}).pop(websocket,None)
            await app.state.hub.typing_status(room_id)

    return app


app=create_app()

