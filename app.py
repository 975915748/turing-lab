"""多人图灵测试：单进程 FastAPI + WebSocket + SQLite，支持手动转录与模型 API。"""
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

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.staticfiles import StaticFiles

from model_api import (ModelAPIError, chat_completion, decrypt_api_key,
                       encrypt_api_key, has_valid_credential_key, parse_json_object,
                       validate_base_url)

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / 'data' / 'game.sqlite3'
COOKIE = 'turing_sid'
MAX_PLAYERS = 8
MAX_ROUNDS = 100
MAX_JUDGES = 12
DURATION_DAYS = 7


class ModelCallCancelled(Exception):
    """Queued model work became unauthorized or stale before the provider call began."""


class ModelRequestLimitReached(Exception):
    """A room has no remaining model-request attempts."""


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
                'model_operation_tasks' not in old_tables or
                ('rooms' in old_tables and 'model_request_limit' not in {
                    x['name'] for x in old.execute('PRAGMA table_info(rooms)')}) or
                'model_profiles' not in old_tables or
                ('model_profiles' in old_tables and 'owner_user_id' not in {
                    x['name'] for x in old.execute('PRAGMA table_info(model_profiles)')}) or
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
                backup_tag = ('v1_9' if 'model_operation_tasks' not in old_tables or
                                  ('rooms' in old_tables and 'model_request_limit' not in {
                                      x['name'] for x in old.execute('PRAGMA table_info(rooms)')}) else
                              'v1_8' if 'model_profiles' in old_tables and 'owner_user_id' not in {
                                  x['name'] for x in old.execute('PRAGMA table_info(model_profiles)')} else
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
                                  x['name'] for x in old.execute('PRAGMA table_info(rooms)')} else
                              'v1_5_1' if needs_room_ids else 'v1_6')
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
            model_request_limit INTEGER,
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
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            match_number INTEGER NOT NULL,
            sender_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            sender_uid TEXT NOT NULL, sender_name TEXT NOT NULL,
            text TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_chat_messages_room_match
            ON chat_messages(room_id,match_number,id);
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
            owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            legacy_room_id INTEGER, ownership_issue TEXT,
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
        CREATE TABLE IF NOT EXISTS model_operation_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER REFERENCES rooms(id) ON DELETE CASCADE,
            match_number INTEGER, round_number INTEGER,
            purpose TEXT NOT NULL, target_type TEXT NOT NULL, target_key TEXT NOT NULL,
            target_label TEXT NOT NULL,
            profile_id INTEGER, profile_owner_user_id INTEGER NOT NULL, trigger_user_id INTEGER NOT NULL,
            profile_name TEXT NOT NULL, model_name TEXT NOT NULL, profile_snapshot_hash TEXT NOT NULL,
            context_revision TEXT,
            state TEXT NOT NULL CHECK(state IN
                ('queued','requesting','completed','failed','blocked','stale','interrupted')),
            retry_of INTEGER REFERENCES model_operation_tasks(id) ON DELETE SET NULL,
            idempotency_key TEXT,
            queued_at TEXT NOT NULL, request_started_at TEXT, request_finished_at TEXT,
            finished_at TEXT, queue_ms INTEGER, duration_ms INTEGER,
            request_succeeded INTEGER, result_applied INTEGER NOT NULL DEFAULT 0,
            usage_known INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER,
            error_message TEXT, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_model_operations_room_match_round
            ON model_operation_tasks(room_id,match_number,round_number,id DESC);
        CREATE INDEX IF NOT EXISTS idx_model_operations_owner
            ON model_operation_tasks(profile_owner_user_id,id DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_operations_active_room_target
            ON model_operation_tasks(room_id,match_number,round_number,purpose,target_type,target_key)
            WHERE room_id IS NOT NULL AND state IN ('queued','requesting');
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_operations_active_profile_test
            ON model_operation_tasks(profile_owner_user_id,profile_id)
            WHERE purpose='connection_test' AND state IN ('queued','requesting');
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
        if 'model_request_limit' not in {x['name'] for x in db.execute('PRAGMA table_info(rooms)')}:
            db.execute('ALTER TABLE rooms ADD COLUMN model_request_limit INTEGER')
        if 'deleted_at' not in {x['name'] for x in db.execute('PRAGMA table_info(users)')}:
            db.execute('ALTER TABLE users ADD COLUMN deleted_at TEXT')
        room_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='rooms'").fetchone()['sql']
        if 'AUTOINCREMENT' not in room_sql.upper():
            migrate_room_ids(db,room_sql)
        migrate_model_profiles(db)
        # Never replay a possibly billed request after restart. First check whether
        # its match context or target content already changed while the process was down.
        interrupted_at=datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        unfinished=db.execute("""SELECT * FROM model_operation_tasks
            WHERE state IN ('queued','requesting') ORDER BY id""").fetchall()
        for task in unfinished:
            next_state='interrupted'
            message='服务重启，无法确认请求是否完成；请先核对比赛内容再重试。'
            if task['room_id'] is not None:
                room=db.execute('SELECT * FROM rooms WHERE id=?',(task['room_id'],)).fetchone()
                stale=not room or room['match_number']!=task['match_number'] or room['current_round']!=task['round_number']
                if not stale and task['purpose']=='judge_question':
                    stale=bool(db.execute('''SELECT 1 FROM ai_judge_questions
                        WHERE room_id=? AND round_number=? AND ai_judge_id=?''',
                        (task['room_id'],task['round_number'],int(task['target_key']))).fetchone())
                    stale=stale or room['status']!='open'
                elif not stale and task['purpose']=='ai_answer':
                    round_row=db.execute('SELECT id,question,status FROM rounds WHERE room_id=? AND number=?',
                                          (task['room_id'],task['round_number'])).fetchone()
                    stale=bool(not round_row or round_row['status']!='collecting' or
                        db.execute('SELECT 1 FROM answers WHERE round_id=? AND alias=?',
                                   (round_row['id'],task['target_key'])).fetchone())
                elif not stale and task['purpose']=='judge_verdict':
                    stale=bool(db.execute('SELECT 1 FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
                                          (task['room_id'],int(task['target_key']))).fetchone())
                    stale=stale or room['status']!='open'
                if stale:
                    next_state='stale'
                    message='服务重启时发现目标内容已完成或比赛上下文已变化；旧任务已作废。'
            db.execute('''UPDATE model_operation_tasks SET state=?,finished_at=?,updated_at=?,
                error_message=? WHERE id=? AND state IN ('queued','requesting')''',
                (next_state,interrupted_at,interrupted_at,message,task['id']))



def migrate_model_profiles(db):
    """Move room-scoped profiles to their original room owner's account without changing IDs."""
    columns={row['name'] for row in db.execute('PRAGMA table_info(model_profiles)')}
    if 'owner_user_id' in columns:
        db.execute('CREATE INDEX IF NOT EXISTS idx_model_profiles_owner ON model_profiles(owner_user_id,id)')
        return
    if 'room_id' not in columns:
        raise sqlite3.DatabaseError('model_profiles 表结构无法识别，已停止迁移。')
    db.commit()
    db.execute('PRAGMA foreign_keys=OFF')
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE model_profiles_user_upgrade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            legacy_room_id INTEGER, ownership_issue TEXT,
            name TEXT NOT NULL, base_url TEXT NOT NULL, model TEXT NOT NULL,
            api_key_encrypted TEXT NOT NULL, temperature REAL NOT NULL DEFAULT 0.7,
            max_tokens INTEGER NOT NULL DEFAULT 2048, timeout_seconds INTEGER NOT NULL DEFAULT 60,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        db.execute('''INSERT INTO model_profiles_user_upgrade
            (id,owner_user_id,legacy_room_id,ownership_issue,name,base_url,model,api_key_encrypted,
             temperature,max_tokens,timeout_seconds,created_at,updated_at)
            SELECT mp.id,
                CASE WHEN r.owner_id IS NOT NULL AND u.id IS NOT NULL AND u.deleted_at IS NULL
                     THEN r.owner_id ELSE NULL END,
                mp.room_id,
                CASE WHEN r.id IS NULL THEN 'legacy_room_missing'
                     WHEN r.owner_id IS NULL THEN 'legacy_owner_missing'
                     WHEN u.id IS NULL OR u.deleted_at IS NOT NULL THEN 'legacy_owner_deleted'
                     ELSE NULL END,
                mp.name,mp.base_url,mp.model,mp.api_key_encrypted,mp.temperature,
                mp.max_tokens,mp.timeout_seconds,mp.created_at,mp.updated_at
            FROM model_profiles mp
            LEFT JOIN rooms r ON r.id=mp.room_id
            LEFT JOIN users u ON u.id=r.owner_id''')
        db.execute('DROP TABLE model_profiles')
        db.execute('ALTER TABLE model_profiles_user_upgrade RENAME TO model_profiles')
        db.execute('CREATE INDEX IF NOT EXISTS idx_model_profiles_owner ON model_profiles(owner_user_id,id)')
        problems=db.execute('''SELECT id,legacy_room_id,ownership_issue FROM model_profiles
            WHERE owner_user_id IS NULL ORDER BY id''').fetchall()
        violations=db.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            raise sqlite3.IntegrityError('个人模型配置迁移未通过外键检查，已回滚。')
        db.commit()
        for row in problems:
            print(f'[数据迁移] 模型配置 {row["id"]}（原房间 {row["legacy_room_id"]}）无法自动归属：{row["ownership_issue"]}；已保留并停用。')
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')


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
    role: Literal['player', 'judge'] = 'player'


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


class ProfileNameData(BaseModel, extra='forbid'):
    display_name: str = Field(min_length=1, max_length=40)


class ChangePasswordData(BaseModel, extra='forbid'):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)
    confirm_password: str = Field(min_length=8, max_length=200)


class ModelBindingData(BaseModel, extra='forbid'):
    model_profile_id: Optional[int] = Field(default=None, ge=1)
    keep_existing: bool = False


class ModelRequestLimitData(BaseModel, extra='forbid'):
    limit: Optional[int] = Field(default=None, ge=1, le=1000000)


class ChatMessageData(BaseModel, extra='forbid'):
    match_number: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)


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


def model_profile_credential_status(profile):
    if not profile.get('owner_user_id') or profile.get('ownership_issue') or profile.get('owner_deleted_at'):
        return 'owner_unavailable'
    encrypted=profile.get('api_key_encrypted') or ''
    if not encrypted:
        return 'missing'
    if not has_valid_credential_key():
        return 'key_unavailable'
    try:
        decrypt_api_key(encrypted)
    except ModelAPIError:
        return 'needs_repair'
    return 'ok'


def model_profile_view(profile, *, include_base_url=True):
    data={'id':profile['id'],'name':profile['name'],'model':profile['model'],
          'temperature':profile['temperature'],'max_tokens':profile['max_tokens'],
          'timeout_seconds':profile['timeout_seconds'],
          'has_api_key':bool(profile['api_key_encrypted']),
          'credential_status':model_profile_credential_status(profile),
          'updated_at':profile['updated_at']}
    if include_base_url:
        data['base_url']=profile['base_url']
    return data


def require_owned_model_profile(db, profile_id, user_id):
    row=db.execute('''SELECT mp.*,u.deleted_at AS owner_deleted_at FROM model_profiles mp
        LEFT JOIN users u ON u.id=mp.owner_user_id
        WHERE mp.id=? AND mp.owner_user_id=?''',(profile_id,user_id)).fetchone()
    if not row or row['ownership_issue']:
        raise HTTPException(404,'模型配置不存在或不属于当前账号。')
    profile=dict(row)
    if profile['owner_deleted_at'] is not None:
        raise HTTPException(403,'模型配置所属账号已停用。')
    status=model_profile_credential_status(profile)
    if status!='ok':
        raise HTTPException(503,'此模型配置的 API Key 不可用，请在个人中心修复后重试。')
    return profile



def model_call_actor_valid(db, room_id, user_id, session_hash):
    actor=db.execute('''SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>? AND u.deleted_at IS NULL''',
        (session_hash,user_id,utcnow())).fetchone()
    room=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
    return bool(actor and room and can_manage_room(room,dict(actor)))


def model_profile_owner_active(db, profile_id):
    return bool(db.execute('''SELECT 1 FROM model_profiles mp JOIN users u ON u.id=mp.owner_user_id
        WHERE mp.id=? AND mp.ownership_issue IS NULL AND u.deleted_at IS NULL''',(profile_id,)).fetchone())


def model_profile_snapshot_valid(db, profile):
    row=db.execute('''SELECT mp.*,u.deleted_at AS owner_deleted_at FROM model_profiles mp
        LEFT JOIN users u ON u.id=mp.owner_user_id WHERE mp.id=?''',(profile['id'],)).fetchone()
    if not row:
        return False
    current=dict(row)
    if model_profile_credential_status(current)!='ok':
        return False
    fields=('owner_user_id','base_url','model','api_key_encrypted','temperature','max_tokens','timeout_seconds','updated_at')
    return all(current.get(field)==profile.get(field) for field in fields)


def model_profile_snapshot_hash(profile):
    fields=('id','owner_user_id','base_url','model','api_key_encrypted','temperature',
            'max_tokens','timeout_seconds','updated_at')
    payload=[profile.get(field) for field in fields]
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()


def model_actor_session_valid(db, user_id, session_hash):
    return bool(db.execute('''SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>? AND u.deleted_at IS NULL''',
        (session_hash,user_id,utcnow())).fetchone())


def require_chat_access(db, room_id, user):
    room=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
    if not room:
        raise HTTPException(404,'房间不存在。')
    if is_judge(db,room_id,user) and not user['is_admin']:
        raise HTTPException(403,'真人裁判不能查看或发送房间公共聊天。')
    member=db.execute('''SELECT 1 FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL''',
                      (room_id,user['id'])).fetchone()
    if not (member or can_manage_room(room,user)):
        raise HTTPException(403,'你不属于这个房间，无法查看公共聊天。')
    return room


def clear_room_chat(db, room_id, match_number=None):
    if match_number is None:
        db.execute('DELETE FROM chat_messages WHERE room_id=?',(room_id,))
    else:
        db.execute('DELETE FROM chat_messages WHERE room_id=? AND match_number=?',(room_id,match_number))


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


def can_view_room(db, room_id, user):
    if not user:
        return False
    if user['is_admin']:
        return True
    room = db.execute('SELECT owner_id FROM rooms WHERE id=?', (room_id,)).fetchone()
    if not room:
        return False
    if room['owner_id'] == user['id']:
        return True
    return bool(db.execute('''SELECT 1 FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL
        UNION ALL SELECT 1 FROM room_judges WHERE room_id=? AND user_id=? LIMIT 1''',
        (room_id,user['id'],room_id,user['id'])).fetchone())


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


def check_no_other_active_judge_room(db, user_id, room_id, viewer=None):
    conflict = db.execute('''SELECT r.id,r.title FROM room_judges j
        JOIN rooms r ON r.id=j.room_id
        JOIN users u ON u.id=j.user_id
        WHERE j.user_id=? AND j.room_id<>? AND r.status<>'finished'
          AND u.deleted_at IS NULL
        ORDER BY r.id LIMIT 1''', (user_id, room_id)).fetchone()
    if conflict:
        if can_view_room(db, conflict['id'], viewer):
            detail = f'你已在未完赛房间「{conflict["title"]}」（ROOM {conflict["id"]}）担任真人裁判，请先完成当前房间的比赛。'
        else:
            detail = '该裁判正在其他未完赛房间任职，请先完成当前房间的比赛。'
        raise HTTPException(409, detail)


def check_restart_judge_conflicts(db, room_id, viewer=None):
    conflicts = db.execute('''SELECT DISTINCT u.id,u.display_name,r.id AS other_room_id,r.title
        FROM room_judges target_judge
        JOIN users u ON u.id=target_judge.user_id AND u.deleted_at IS NULL
        JOIN room_judges other ON other.user_id=target_judge.user_id AND other.room_id<>target_judge.room_id
        JOIN rooms r ON r.id=other.room_id
        WHERE target_judge.room_id=? AND r.status<>'finished'
        ORDER BY u.id,r.id''', (room_id,)).fetchall()
    if conflicts:
        details = []
        for row in conflicts:
            if can_view_room(db, row['other_room_id'], viewer):
                details.append(
                    f'{row["display_name"]}（{user_uid(row["id"])}）仍在未完赛房间「{row["title"]}」（ROOM {row["other_room_id"]}）'
                )
            else:
                details.append('有保留裁判仍在其他未完赛房间任职')
        details = '；'.join(dict.fromkeys(details))
        raise HTTPException(409, f'无法重新开赛：{details}。请先完成其他房间的比赛。')


def check_judge_registration(db, room, user_id, viewer=None):
    if room['owner_id'] == user_id:
        raise HTTPException(409, '房主不能同时担任本房间的独立裁判。')
    if room['status'] != 'open' or room['verdict_locked_at'] or db.execute(
        'SELECT 1 FROM judge_verdicts WHERE room_id=? LIMIT 1',(room['id'],)).fetchone() or db.execute(
        'SELECT 1 FROM ai_judge_verdicts WHERE room_id=? LIMIT 1',(room['id'],)).fetchone():
        raise HTTPException(409, '比赛已结束或有人锁定结论，不能再添加裁判。')
    if user_id > 0:
        account = db.execute('SELECT is_admin FROM users WHERE id=? AND deleted_at IS NULL',
                             (user_id,)).fetchone()
        if not account:
            raise HTTPException(404, '用户不存在或账号已删除。')
        if account['is_admin']:
            raise HTTPException(403, '管理员账号不能作为独立真人裁判加入。')
        check_no_other_active_judge_room(db, user_id, room['id'], viewer)
    if db.execute('SELECT 1 FROM members WHERE room_id=? AND user_id=?',(room['id'],user_id)).fetchone():
        raise HTTPException(409, '本房间的参赛者不能同时担任裁判。')
    if len(judge_records(db,room['id'])) >= MAX_JUDGES:
        raise HTTPException(409,f'每个房间最多 {MAX_JUDGES} 位裁判。')


def aliases(db, room_id):
    humans = db.execute('''SELECT m.alias,m.user_id,u.display_name,u.username FROM members m
        JOIN users u ON u.id=m.user_id WHERE m.room_id=? AND m.removed_at IS NULL
        AND u.deleted_at IS NULL ORDER BY m.alias''', (room_id,)).fetchall()
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
    for table in ('model_operation_tasks', 'rounds', 'suggestions', 'events', 'judge_questions', 'judge_verdicts',
                  'ai_judge_questions', 'ai_judge_verdicts', 'ai_answer_revisions', 'packets', 'chat_messages'):
        db.execute(f'DELETE FROM {table} WHERE room_id=?', (room_id,))
    db.execute('''UPDATE rooms SET verdict_json=NULL,verdict_locked_at=NULL,
        revealed_at=NULL,records_deleted=1 WHERE id=?''', (room_id,))


def check_match_number(room, number):
    if str(room['match_number']) != str(number):
        raise HTTPException(409, '房间已开始另一场比赛，请刷新后重试。')


def active_participant_count(db, room_id):
    humans=db.execute('''SELECT COUNT(*) FROM members m JOIN users u ON u.id=m.user_id
        WHERE m.room_id=? AND m.removed_at IS NULL AND u.deleted_at IS NULL''',(room_id,)).fetchone()[0]
    ais=db.execute('SELECT COUNT(*) FROM ai_players WHERE room_id=?',(room_id,)).fetchone()[0]
    return int(humans)+int(ais)


def round_question_rows(db, room_id, round_number):
    return db.execute('''SELECT q.text,q.created_at,j.label,'human' AS kind,q.id
        FROM judge_questions q JOIN room_judges j
        ON j.room_id=q.room_id AND j.user_id=q.judge_user_id
        WHERE q.room_id=? AND q.round_number=?
        UNION ALL
        SELECT q.text,q.created_at,j.label,'ai' AS kind,q.id
        FROM ai_judge_questions q JOIN ai_judges j ON j.id=q.ai_judge_id
        WHERE q.room_id=? AND q.round_number=?
        ORDER BY 2,4,5''',(room_id,round_number,room_id,round_number)).fetchall()


def combined_round_question(rows):
    return '\n\n'.join(f'【裁判问题 {i}】\n{row["text"]}' for i,row in enumerate(rows,1))


def inspect_round_start(db, room):
    rnd=current_round(db,room)
    participant_count=active_participant_count(db,room['id'])
    rows=round_question_rows(db,room['id'],rnd['number'])
    question=combined_round_question(rows)
    reason=None
    if room['status']!='open' or room['verdict_locked_at']:
        reason='比赛已结束或裁判判断已锁定。'
    elif rnd['status']!='draft':
        reason='本轮已经开始。'
    elif participant_count<2:
        reason='至少需要两名在场参赛者，才能开始统一作答。'
    elif not rows:
        reason='还没有裁判提交问题。'
    elif len(question)>6000:
        reason='裁判问题合计超过 6000 字，请减少内容后重新开局。'
    return {'round':rnd,'participant_count':participant_count,'questions':rows,
            'question':question,'reason':reason,'question_chars':len(question)}


def model_request_count(db, room_id, match_number):
    return int(db.execute('''SELECT COUNT(*) FROM model_operation_tasks
        WHERE room_id=? AND match_number=? AND request_started_at IS NOT NULL''',
        (room_id,match_number)).fetchone()[0])


def match_readiness(db, room):
    rnd=current_round(db,room)
    validation=inspect_round_start(db,room)
    judges=judge_records(db,room['id'])
    question_count=len(validation['questions'])
    human_pending=0
    for judge in judges:
        if judge['kind']=='human' and not db.execute('''SELECT 1 FROM judge_questions
            WHERE room_id=? AND round_number=? AND judge_user_id=?''',
            (room['id'],rnd['number'],judge['user_id'])).fetchone():
            human_pending+=1
    ai_rows=db.execute('''SELECT j.id,j.model_profile_id,q.id AS question_id,
            mp.*,u.deleted_at AS owner_deleted_at
        FROM ai_judges j
        LEFT JOIN model_profiles mp ON mp.id=j.model_profile_id
        LEFT JOIN users u ON u.id=mp.owner_user_id
        LEFT JOIN ai_judge_questions q ON q.room_id=j.room_id AND q.round_number=? AND q.ai_judge_id=j.id
        WHERE j.room_id=? ORDER BY j.id''',(rnd['number'],room['id'])).fetchall()
    auto_pending=0
    manual_ai_pending=0
    for row in ai_rows:
        if row['question_id'] is not None:
            continue
        if row['model_profile_id'] is not None and model_profile_credential_status(dict(row))=='ok':
            auto_pending+=1
        else:
            manual_ai_pending+=1
    manual_pending=human_pending+manual_ai_pending
    attempts=model_request_count(db,room['id'],room['match_number'])
    limit=room['model_request_limit']
    limit_reached=limit is not None and attempts>=limit
    can_generate=(room['status']=='open' and rnd['status']=='draft' and
                  validation['participant_count']>=2 and auto_pending>0 and
                  not limit_reached and validation['question_chars']<=6000)
    blockers=[]
    if validation['participant_count']<2:
        blockers.append('至少需要两名在场参赛者。')
    if room['status']!='open':
        blockers.append('本场比赛已结束。')
    elif rnd['status']!='draft':
        blockers.append('当前轮次已开始作答。')
    elif not question_count and not auto_pending:
        blockers.append('本轮尚无裁判问题。')
    if validation['question_chars']>6000:
        blockers.append('裁判问题合计超过 6000 字。')
    notices=[]
    if manual_pending:
        notices.append(f'{manual_pending} 位真人 / 手动 AI 裁判尚未交题；已有至少一份合规问题时，房主仍可按现有规则确认开局。')
    if limit_reached and auto_pending:
        notices.append('本场模型请求次数已达上限；可调整上限或手动转录。')
    elif auto_pending:
        notices.append(f'{auto_pending} 位可用模型 AI 裁判尚未交题。')
    if not judges:
        notices.append('当前使用外部裁判流程，不需要配置模型 API；可手动录入并发布问题。')
    return {
        'participant_count':validation['participant_count'],
        'question_count':question_count,
        'question_chars':validation['question_chars'],
        'total_judges':len(judges),
        'submitted_judges':sum(1 for j in judges if
            (j['kind']=='human' and db.execute('''SELECT 1 FROM judge_questions
                WHERE room_id=? AND round_number=? AND judge_user_id=?''',
                (room['id'],rnd['number'],j['user_id'])).fetchone()) or
            (j['kind']=='ai' and db.execute('''SELECT 1 FROM ai_judge_questions
                WHERE room_id=? AND round_number=? AND ai_judge_id=?''',
                (room['id'],rnd['number'],j['id'])).fetchone())),
        'auto_pending':auto_pending,'manual_pending':manual_pending,
        'independent_judges':len(judges),'external_judge':not judges,
        'phase':rnd['status'],'can_start':validation['reason'] is None,
        'can_generate_and_start':can_generate,
        'model_request_count':attempts,'model_request_limit':limit,
        'model_request_remaining':None if limit is None else max(0,int(limit)-attempts),
        'model_limit_reached':limit_reached,'blockers':blockers,'notices':notices,
        'manual_question_required':not judges and rnd['status']=='draft' and not question_count,
        'suggested_view':('human-judges' if human_pending else
                          'ai-judges' if manual_ai_pending or auto_pending else 'admin'),
    }


def model_operation_view(row):
    return {
        'id':row['id'],'purpose':row['purpose'],'target_type':row['target_type'],
        'target_key':row['target_key'],'target_label':row['target_label'],
        'profile_name':row['profile_name'],'model_name':row['model_name'],
        'state':row['state'],'retry_of':row['retry_of'],
        'queued_at':row['queued_at'],'request_started_at':row['request_started_at'],
        'request_finished_at':row['request_finished_at'],'finished_at':row['finished_at'],
        'queue_ms':row['queue_ms'],'duration_ms':row['duration_ms'],
        'request_succeeded':None if row['request_succeeded'] is None else bool(row['request_succeeded']),
        'result_applied':bool(row['result_applied']),'usage_known':bool(row['usage_known']),
        'prompt_tokens':row['prompt_tokens'],'completion_tokens':row['completion_tokens'],
        'total_tokens':row['total_tokens'],'error_message':row['error_message'],
    }


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
    profile_summaries = {}
    if manager:
        bound_ids={row['model_profile_id'] for row in db.execute(
            '''SELECT model_profile_id FROM ai_players WHERE room_id=? AND model_profile_id IS NOT NULL
               UNION SELECT model_profile_id FROM ai_judges WHERE room_id=? AND model_profile_id IS NOT NULL''',
            (room['id'],room['id']))}
        for profile_id in sorted(bound_ids):
            row=db.execute('''SELECT mp.*,u.deleted_at AS owner_deleted_at FROM model_profiles mp
                LEFT JOIN users u ON u.id=mp.owner_user_id WHERE mp.id=?''',(profile_id,)).fetchone()
            if not row:
                continue
            profile=dict(row)
            summary={'id':profile['id'],'name':profile['name'],'model':profile['model'],
                     'owned_by_me':profile['owner_user_id']==user['id'],
                     'credential_status':model_profile_credential_status(profile)}
            profile_summaries[profile_id]=summary
            model_profiles.append(summary)
    if manager and not room['records_deleted']:
        now_round = current_round(db,room)
        ai_edits = {row['alias']:row['text'] for row in db.execute('''SELECT a.alias,a.text FROM answers a
            JOIN ai_players ai ON ai.room_id=? AND ai.alias=a.alias
            WHERE a.round_id=?''',(room['id'],now_round['id']))}
    result={'id':room['id'],'title':room['title'],'judge':room['judge'],
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
                    'model_profile_id':r['model_profile_id'] if manager else None,
                    'model_profile':profile_summaries.get(r['model_profile_id']) if manager else None} for r in ais],
            'judges': [{'id':r['id'],'kind':r['kind'],'user_id':r['user_id'],
                        'label':r['label'],'username':r['username'],
                        'model_profile_id':r['model_profile_id'] if manager and r['kind']=='ai' else None,
                        'model_profile':profile_summaries.get(r['model_profile_id']) if manager and r['kind']=='ai' else None,
                        'uid':user_uid(r['user_id']) if r['kind']=='human' else None,
                        'locked':bool(r['locked'])} for r in judges] if manager else
                      [{'kind':r['kind'],'label':r['label'],'locked':bool(r['locked'])} for r in judges],
            'rounds':body,'suggestions':[dict(s) for s in suggestions],
            'ai_edits':ai_edits,'packets':[dict(p) for p in pkts],
            'model_profiles':model_profiles,'model_api_enabled':has_valid_credential_key()}
    if manager:
        operations=db.execute('''SELECT * FROM model_operation_tasks
            WHERE room_id=? AND match_number=? AND round_number=?
            ORDER BY id DESC LIMIT 30''',
            (room['id'],room['match_number'],room['current_round'])).fetchall()
        result.update({
            'readiness':match_readiness(db,room),
            'model_operations':[model_operation_view(row) for row in operations],
            'model_request_count':model_request_count(db,room['id'],room['match_number']),
            'model_request_limit':room['model_request_limit'],
        })
    return result


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

    async def notify_chat(self, room_id, message):
        for ws in list(self.groups.get(room_id,set())):
            try:
                with db_open(self.path) as db:
                    user=auth(db,ws.cookies.get(COOKIE))
                    room=require_chat_access(db,room_id,user)
                    if room['status']!='open' or room['match_number']!=message['match_number']:
                        continue
                await ws.send_json({'type':'chat_message','room_id':room_id,
                                    'match_number':message['match_number'],'message':message})
            except HTTPException as exc:
                if exc.status_code==401:
                    self.groups.get(room_id,set()).discard(ws)
                    try: await ws.close(code=1008)
                    except Exception: pass
            except Exception:
                self.groups.get(room_id,set()).discard(ws)

    async def clear_chat(self, room_id, match_number):
        for ws in list(self.groups.get(room_id,set())):
            try:
                with db_open(self.path) as db:
                    user=auth(db,ws.cookies.get(COOKIE))
                    require_chat_access(db,room_id,user)
                await ws.send_json({'type':'chat_cleared','room_id':room_id,'match_number':match_number})
            except HTTPException as exc:
                if exc.status_code==401:
                    self.groups.get(room_id,set()).discard(ws)
                    try: await ws.close(code=1008)
                    except Exception: pass
            except Exception:
                self.groups.get(room_id,set()).discard(ws)

    async def revoke_user(self, user_id):
        for room_id in list(self.groups):
            for ws in list(self.groups.get(room_id,set())):
                try:
                    with db_open(self.path) as db:
                        user=auth(db,ws.cookies.get(COOKIE))
                    if user['id']==user_id:
                        await ws.close(code=1008,reason='密码已修改，请重新登录。')
                        self.groups.get(room_id,set()).discard(ws)
                except HTTPException:
                    self.groups.get(room_id,set()).discard(ws)
                    try: await ws.close(code=1008,reason='登录会话已失效')
                    except Exception: pass
                except Exception:
                    self.groups.get(room_id,set()).discard(ws)

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
    async def model_call(profile, messages, api_key, preflight=None, operation_id=None):
        loop = asyncio.get_running_loop()
        if getattr(app.state, 'model_semaphore_loop', None) is not loop:
            app.state.model_semaphore = asyncio.Semaphore(4)
            app.state.model_semaphore_loop = loop
        async with app.state.model_semaphore:
            if operation_id is None:
                if preflight is not None and not preflight():
                    raise ModelCallCancelled()
            else:
                outcome=None
                room_id=None
                with connection(immediate=True) as db:
                    task=db.execute('SELECT * FROM model_operation_tasks WHERE id=?',(operation_id,)).fetchone()
                    if not task or task['state']!='queued':
                        outcome='stale'
                    else:
                        room_id=task['room_id']
                        if preflight is not None and not preflight():
                            finish_model_operation_in_db(db,operation_id,'stale',
                                error='排队期间权限、配置或比赛上下文发生变化；未向服务商发送请求。')
                            outcome='stale'
                        else:
                            room=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone() if room_id is not None else None
                            if room_id is not None and (not room or room['match_number']!=task['match_number']):
                                finish_model_operation_in_db(db,operation_id,'stale',
                                    error='房间或比赛场次已变化；未向服务商发送请求。')
                                outcome='stale'
                            else:
                                if room_id is not None:
                                    attempts=model_request_count(db,room_id,task['match_number'])
                                    limit=room['model_request_limit']
                                    if limit is not None and attempts>=limit:
                                        finish_model_operation_in_db(db,operation_id,'blocked',
                                            error='本场模型请求次数已达上限；请提高上限或手动转录。')
                                        outcome='blocked'
                                if outcome is None:
                                    now=operation_timestamp()
                                    try:
                                        queued=datetime.fromisoformat(task['queued_at'])
                                        wait_ms=max(0,int((datetime.now(timezone.utc)-queued).total_seconds()*1000))
                                    except (TypeError,ValueError):
                                        wait_ms=0
                                    db.execute("""UPDATE model_operation_tasks SET state='requesting',
                                        request_started_at=?,queue_ms=?,updated_at=?
                                        WHERE id=? AND state='queued'""",(now,wait_ms,now,operation_id))
                if outcome is not None:
                    if room_id is not None:
                        await app.state.hub.notify(room_id,'model_operation_updated')
                    if outcome=='blocked':
                        raise ModelRequestLimitReached('本场模型请求次数已达上限；请提高上限或手动转录。')
                    raise ModelCallCancelled()
                await notify_model_operation(operation_id)

            started=time.perf_counter()
            try:
                result=await chat_completion(profile,messages,api_key)
            except Exception as exc:
                if operation_id is not None:
                    now=operation_timestamp()
                    with connection(immediate=True) as db:
                        db.execute("""UPDATE model_operation_tasks SET state='failed',
                            request_finished_at=?,finished_at=?,duration_ms=?,request_succeeded=0,
                            error_message=?,result_applied=0,updated_at=?
                            WHERE id=? AND state='requesting'""",
                            (now,now,round((time.perf_counter()-started)*1000),
                             operation_error_message(exc),now,operation_id))
                    await notify_model_operation(operation_id)
                raise

            if operation_id is not None:
                now=operation_timestamp()
                usage=result.get('usage') if isinstance(result,dict) else {}
                usage=usage if isinstance(usage,dict) else {}
                prompt=usage.get('prompt_tokens')
                completion=usage.get('completion_tokens')
                total=usage.get('total_tokens')
                known=any(value is not None for value in (prompt,completion,total))
                with connection(immediate=True) as db:
                    db.execute("""UPDATE model_operation_tasks SET request_finished_at=?,
                        duration_ms=?,request_succeeded=1,usage_known=?,
                        prompt_tokens=?,completion_tokens=?,total_tokens=?,updated_at=?
                        WHERE id=? AND state='requesting'""",
                        (now,round((time.perf_counter()-started)*1000),1 if known else 0,
                         prompt,completion,total,now,operation_id))
                await notify_model_operation(operation_id)
            return result

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


    def operation_timestamp():
        return datetime.now(timezone.utc).isoformat(timespec='milliseconds')

    def operation_error_message(error):
        if isinstance(error, ModelAPIError):
            message=str(error)
        elif isinstance(error, HTTPException):
            message=str(error.detail)
        else:
            message='模型调用失败，请检查配置或稍后重试。'
        return message.replace('\n',' ')[:500]

    def create_model_operation_task(db, *, purpose, target_type, target_key, target_label,
                                    profile, trigger_user_id, room_id=None, match_number=None,
                                    round_number=None, context_revision=None, retry_of=None,
                                    idempotency_key=None):
        if room_id is None:
            active=db.execute("""SELECT id,state FROM model_operation_tasks
                WHERE purpose='connection_test' AND profile_owner_user_id=? AND profile_id=?
                  AND state IN ('queued','requesting') ORDER BY id DESC LIMIT 1""",
                (profile['owner_user_id'],profile['id'])).fetchone()
        else:
            active=db.execute("""SELECT id,state FROM model_operation_tasks
                WHERE room_id=? AND match_number=? AND round_number=? AND purpose=?
                  AND target_type=? AND target_key=? AND state IN ('queued','requesting')
                ORDER BY id DESC LIMIT 1""",
                (room_id,match_number,round_number,purpose,target_type,str(target_key))).fetchone()
        if active:
            return int(active['id']),False,active['state']
        now=operation_timestamp()
        profile_name=profile.get('profile_name') or profile.get('name') or '个人模型配置'
        model_name=profile.get('model_name') or profile.get('model') or '未知模型'
        cur=db.execute('''INSERT INTO model_operation_tasks(
            room_id,match_number,round_number,purpose,target_type,target_key,target_label,
            profile_id,profile_owner_user_id,trigger_user_id,profile_name,model_name,
            profile_snapshot_hash,context_revision,state,retry_of,idempotency_key,queued_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'queued',?,?,?,?)''',
            (room_id,match_number,round_number,purpose,target_type,str(target_key),target_label,
             profile['id'],profile['owner_user_id'],trigger_user_id,profile_name,model_name,
             model_profile_snapshot_hash(profile),context_revision,retry_of,
             (idempotency_key or secrets.token_urlsafe(18))[:128],now,now))
        return int(cur.lastrowid),True,'queued'

    def finish_model_operation_in_db(db, operation_id, state, *, error=None, result_applied=False):
        now=operation_timestamp()
        db.execute('''UPDATE model_operation_tasks SET state=?,finished_at=?,updated_at=?,
            error_message=?,result_applied=?
            WHERE id=? AND state IN ('queued','requesting')''',
            (state,now,now,operation_error_message(error) if error else None,
             1 if result_applied else 0,operation_id))

    def finish_model_operation(operation_id, state, *, error=None, result_applied=False):
        with connection(immediate=True) as db:
            row=db.execute('SELECT room_id FROM model_operation_tasks WHERE id=?',(operation_id,)).fetchone()
            if not row:
                return None
            room_id=row['room_id']
            finish_model_operation_in_db(db,operation_id,state,error=error,result_applied=result_applied)
        return room_id

    async def finish_and_notify_model_operation(operation_id, state, *, error=None, result_applied=False):
        room_id=finish_model_operation(operation_id,state,error=error,result_applied=result_applied)
        if room_id is not None:
            await app.state.hub.notify(room_id,'model_operation_updated')

    async def notify_model_operation(operation_id):
        with connection() as db:
            row=db.execute('SELECT room_id FROM model_operation_tasks WHERE id=?',(operation_id,)).fetchone()
        if row and row['room_id'] is not None:
            await app.state.hub.notify(row['room_id'],'model_operation_updated')

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

    @app.get('/api/me/models')
    def list_my_model_profiles(request:Request):
        with connection() as db:
            u=caller(request,db)
            rows=db.execute('''SELECT mp.*,u.deleted_at AS owner_deleted_at FROM model_profiles mp
                JOIN users u ON u.id=mp.owner_user_id
                WHERE mp.owner_user_id=? AND mp.ownership_issue IS NULL AND u.deleted_at IS NULL
                ORDER BY mp.id''',(u['id'],)).fetchall()
            return {'models':[model_profile_view(dict(row)) for row in rows],
                    'model_api_enabled':has_valid_credential_key()}

    @app.post('/api/me/models')
    async def create_my_model_profile(body:ModelProfileData,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
        try:
            key=(body.api_key or '').strip()
            if not key:
                raise ModelAPIError('创建模型配置时必须填写 API Key。')
            base=await asyncio.to_thread(validate_base_url,body.base_url)
            encrypted=encrypt_api_key(key)
        except ModelAPIError as exc:
            raise HTTPException(422,str(exc))
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            now=utcnow()
            cur=db.execute('''INSERT INTO model_profiles(owner_user_id,name,base_url,model,api_key_encrypted,
                temperature,max_tokens,timeout_seconds,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (u['id'],norm_text(body.name,60),base,norm_text(body.model,120),encrypted,
                 body.temperature,body.max_tokens,body.timeout_seconds,now,now))
            profile_id=cur.lastrowid
        return {'ok':True,'id':profile_id}

    @app.put('/api/me/models/{profile_id}')
    async def update_my_model_profile(profile_id:int,body:ModelProfileData,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
            row=db.execute('''SELECT mp.*,u.deleted_at AS owner_deleted_at FROM model_profiles mp
                JOIN users u ON u.id=mp.owner_user_id WHERE mp.id=? AND mp.owner_user_id=?''',
                (profile_id,u['id'])).fetchone()
            if not row or row['ownership_issue']:
                raise HTTPException(404,'模型配置不存在或不属于当前账号。')
            old=dict(row)
            status=model_profile_credential_status(old)
        try:
            base=await asyncio.to_thread(validate_base_url,body.base_url)
            key=(body.api_key or '').strip()
            if key:
                new_encrypted=encrypt_api_key(key)
            else:
                if status!='ok':
                    raise ModelAPIError('此配置的 API Key 不可用，必须重新填写后才能修复。')
                new_encrypted=None
        except ModelAPIError as exc:
            raise HTTPException(422,str(exc))
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            current=db.execute('''SELECT * FROM model_profiles WHERE id=? AND owner_user_id=?
                AND ownership_issue IS NULL''',(profile_id,u['id'])).fetchone()
            if not current:
                raise HTTPException(404,'模型配置不存在或不属于当前账号。')
            if new_encrypted is None and model_profile_credential_status(dict(current,owner_deleted_at=None))!='ok':
                raise HTTPException(422,'此配置的 API Key 已不可用，必须重新填写后才能修复。')
            encrypted=new_encrypted if new_encrypted is not None else current['api_key_encrypted']
            db.execute('''UPDATE model_profiles SET name=?,base_url=?,model=?,api_key_encrypted=?,
                temperature=?,max_tokens=?,timeout_seconds=?,updated_at=? WHERE id=? AND owner_user_id=?''',
                (norm_text(body.name,60),base,norm_text(body.model,120),encrypted,
                 body.temperature,body.max_tokens,body.timeout_seconds,utcnow(),profile_id,u['id']))
            rooms=[row['room_id'] for row in db.execute('''SELECT room_id FROM ai_players WHERE model_profile_id=?
                UNION SELECT room_id FROM ai_judges WHERE model_profile_id=?''',(profile_id,profile_id))]
        for room_id in set(rooms):
            await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True}

    @app.delete('/api/me/models/{profile_id}')
    async def delete_my_model_profile(profile_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            if not db.execute('SELECT 1 FROM model_profiles WHERE id=? AND owner_user_id=? AND ownership_issue IS NULL',
                              (profile_id,u['id'])).fetchone():
                raise HTTPException(404,'模型配置不存在或不属于当前账号。')
            rooms=[row['room_id'] for row in db.execute('''SELECT room_id FROM ai_players WHERE model_profile_id=?
                UNION SELECT room_id FROM ai_judges WHERE model_profile_id=?''',(profile_id,profile_id))]
            db.execute('UPDATE ai_players SET model_profile_id=NULL WHERE model_profile_id=?',(profile_id,))
            db.execute('UPDATE ai_judges SET model_profile_id=NULL WHERE model_profile_id=?',(profile_id,))
            db.execute("""DELETE FROM model_operation_tasks
                WHERE purpose='connection_test' AND profile_id=? AND profile_owner_user_id=?""",
                (profile_id,u['id']))
            db.execute('DELETE FROM model_profiles WHERE id=? AND owner_user_id=?',(profile_id,u['id']))
        for room_id in set(rooms):
            await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True,'detached_rooms':len(set(rooms))}

    @app.post('/api/me/models/{profile_id}/test')
    async def test_my_model_profile(profile_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            profile=require_owned_model_profile(db,profile_id,u['id'])
            operation_id,created,_=create_model_operation_task(
                db,purpose='connection_test',target_type='model_profile',
                target_key=profile_id,target_label=profile['name'],profile=profile,
                trigger_user_id=u['id'],idempotency_key=request.headers.get('Idempotency-Key'))
            if not created:
                raise HTTPException(409,'该模型配置已有连接测试正在进行。')
        actor_id=u['id']
        session_hash=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
        def preflight():
            with connection() as db:
                return (model_actor_session_valid(db,actor_id,session_hash)
                        and model_profile_snapshot_valid(db,profile)
                        and profile['owner_user_id']==actor_id)
        try:
            key=decrypt_api_key(profile['api_key_encrypted'])
            started=time.perf_counter()
            result=await model_call(profile,[
                {'role':'system','content':'You are a connection test. Reply with exactly OK.'},
                {'role':'user','content':'Reply with OK.'}],key,preflight,operation_id=operation_id)
        except ModelCallCancelled:
            raise HTTPException(409,'测试排队期间模型配置或账号状态已变化，请重试。')
        except ModelAPIError as exc:
            await finish_and_notify_model_operation(operation_id,'failed',error=exc)
            raise HTTPException(502,str(exc))
        with connection() as db:
            token_hash=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
            session=db.execute('''SELECT u.id FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND u.deleted_at IS NULL''',
                (token_hash,utcnow())).fetchone()
            valid=bool(session and session['id']==profile['owner_user_id'] and db.execute(
                '''SELECT 1 FROM model_profiles mp JOIN users u ON u.id=mp.owner_user_id
                WHERE mp.id=? AND mp.owner_user_id=? AND mp.ownership_issue IS NULL AND u.deleted_at IS NULL''',
                (profile_id,profile['owner_user_id'])).fetchone())
        if not valid:
            await finish_and_notify_model_operation(operation_id,'stale',
                error='测试期间模型配置或账号状态已变化；服务商请求结果未作为有效测试应用。')
            raise HTTPException(409,'测试期间模型配置或账号状态已变化，请重试。')
        await finish_and_notify_model_operation(operation_id,'completed',result_applied=True)
        return {'ok':True,'model':profile['model'],'preview':result['text'][:120],
                'latency_ms':round((time.perf_counter()-started)*1000),'usage':result['usage']}


    @app.get('/api/me/model-operations')
    def list_my_model_operations(request:Request,profile_id:Optional[int]=Query(default=None,ge=1),
                                 purpose:Optional[str]=Query(default=None),
                                 status:Optional[str]=Query(default=None),
                                 limit:int=Query(default=20,ge=1,le=100),
                                 offset:int=Query(default=0,ge=0)):
        allowed_purposes={'connection_test','judge_question','ai_answer','judge_verdict'}
        allowed_states={'queued','requesting','completed','failed','blocked','stale','interrupted'}
        if purpose is not None and purpose not in allowed_purposes:
            raise HTTPException(422,'调用用途筛选值无效。')
        if status is not None and status not in allowed_states:
            raise HTTPException(422,'调用状态筛选值无效。')
        with connection() as db:
            u=caller(request,db)
            clauses=['profile_owner_user_id=?']
            params=[u['id']]
            if profile_id is not None:
                clauses.append('profile_id=?')
                params.append(profile_id)
            if purpose is not None:
                clauses.append('purpose=?')
                params.append(purpose)
            if status is not None:
                clauses.append('state=?')
                params.append(status)
            where=' AND '.join(clauses)
            total=int(db.execute(f'SELECT COUNT(*) FROM model_operation_tasks WHERE {where}',params).fetchone()[0])
            stats=db.execute(f'''SELECT COUNT(*) AS task_attempts,
                SUM(CASE WHEN request_started_at IS NOT NULL THEN 1 ELSE 0 END) AS request_attempts,
                SUM(CASE WHEN request_succeeded=1 THEN 1 ELSE 0 END) AS request_successes,
                SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failures,
                SUM(CASE WHEN result_applied=1 THEN 1 ELSE 0 END) AS applied_results,
                SUM(CASE WHEN usage_known=1 THEN 1 ELSE 0 END) AS known_usage_records,
                SUM(prompt_tokens) AS prompt_tokens,
                SUM(completion_tokens) AS completion_tokens,
                SUM(total_tokens) AS total_tokens
                FROM model_operation_tasks WHERE {where}''',params).fetchone()
            rows=db.execute(f'''SELECT t.*,r.title AS room_title,
                    u.id AS trigger_user_id,u.display_name AS trigger_name
                FROM model_operation_tasks t
                LEFT JOIN rooms r ON r.id=t.room_id
                LEFT JOIN users u ON u.id=t.trigger_user_id
                WHERE {where} ORDER BY t.id DESC LIMIT ? OFFSET ?''',
                (*params,limit,offset)).fetchall()
            options=db.execute('''SELECT profile_id,profile_name,model_name,MAX(id) AS latest_id
                FROM model_operation_tasks WHERE profile_owner_user_id=?
                GROUP BY profile_id,profile_name,model_name ORDER BY latest_id DESC''',(u['id'],)).fetchall()
            records=[]
            for row in rows:
                item=model_operation_view(row)
                item.update({
                    'profile_id':row['profile_id'],'room_id':row['room_id'],
                    'room_title':row['room_title'],'match_number':row['match_number'],
                    'round_number':row['round_number'],
                    'trigger_uid':user_uid(row['trigger_user_id']) if row['trigger_user_id'] else None,
                    'trigger_name':row['trigger_name'],
                })
                records.append(item)
            known=int(stats['known_usage_records'] or 0)
            summary={
                'task_attempts':int(stats['task_attempts'] or 0),
                'request_attempts':int(stats['request_attempts'] or 0),
                'request_successes':int(stats['request_successes'] or 0),
                'failures':int(stats['failures'] or 0),
                'applied_results':int(stats['applied_results'] or 0),
                'known_usage_records':known,
                'prompt_tokens':stats['prompt_tokens'] if known else None,
                'completion_tokens':stats['completion_tokens'] if known else None,
                'total_tokens':stats['total_tokens'] if known and stats['total_tokens'] is not None else None,
            }
            profile_options=[{'profile_id':row['profile_id'],'profile_name':row['profile_name'],
                              'model_name':row['model_name']} for row in options]
        return {'records':records,'total':total,'limit':limit,'offset':offset,
                'stats':summary,'profile_options':profile_options}

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

    @app.put('/api/me/profile')
    async def update_my_profile(body:ProfileNameData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            name=norm_text(body.display_name,40)
            db.execute('UPDATE users SET username=?,display_name=? WHERE id=? AND deleted_at IS NULL',(name,name,u['id']))
            row=db.execute('SELECT id,username,display_name,is_admin FROM users WHERE id=?',(u['id'],)).fetchone()
            if u['is_admin']:
                room_ids=[r['id'] for r in db.execute('SELECT id FROM rooms')]
            else:
                room_ids=[r['room_id'] for r in db.execute('''SELECT room_id FROM members WHERE user_id=? AND removed_at IS NULL
                    UNION SELECT room_id FROM room_judges WHERE user_id=?
                    UNION SELECT id AS room_id FROM rooms WHERE owner_id=?''',(u['id'],u['id'],u['id']))]
            result={'ok':True,'user':dict(row,uid=user_uid(row['id']),csrf=u['csrf'])}
        for room_id in room_ids:
            await app.state.hub.notify(room_id,'profile_updated')
        return result

    @app.post('/api/me/password')
    async def change_my_password(body:ChangePasswordData,request:Request,response:Response):
        if body.new_password!=body.confirm_password:
            raise HTTPException(422,'两次输入的新密码不一致。')
        try:
            new_hash=hash_password(body.new_password)
        except ValueError as exc:
            raise HTTPException(422,str(exc))
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            row=db.execute('SELECT pw_hash FROM users WHERE id=? AND deleted_at IS NULL',(u['id'],)).fetchone()
            if not row or not verify_password(body.current_password,row['pw_hash']):
                raise HTTPException(403,'当前密码不正确。')
            db.execute('UPDATE users SET pw_hash=? WHERE id=? AND deleted_at IS NULL',(new_hash,u['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],))
            db.execute('DELETE FROM login_attempts WHERE identity LIKE ?',('%:uid:'+user_uid(u['id']),))
            legacy=db.execute('SELECT legacy_username FROM users WHERE id=?',(u['id'],)).fetchone()
            if legacy and legacy['legacy_username']:
                db.execute('DELETE FROM login_attempts WHERE identity LIKE ?',('%:legacy:'+legacy['legacy_username'].lower(),))
        response.delete_cookie(COOKIE,path='/')
        await app.state.hub.revoke_user(u['id'])
        return {'ok':True,'signed_out':True}

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
            room_id=room['id']
            if body.role == 'judge':
                if u['is_admin']:
                    raise HTTPException(403,'管理员账号不能作为独立真人裁判加入。')
                existing_judge=is_judge(db,room_id,u)
                if existing_judge:
                    return {'room_id':room_id,'role':'judge','label':existing_judge['label']}
                check_judge_registration(db,room,u['id'],viewer=u)
                label=norm_text(u['display_name'],40)
                db.execute('INSERT INTO room_judges(room_id,user_id,label,created_at) VALUES(?,?,?,?)',
                           (room_id,u['id'],label,utcnow()))
                log_event(db,room_id,room['current_round'],'judge_joined','裁判','真人裁判通过邀请码加入')
                role='judge'
            else:
                if is_judge(db,room_id,u):
                    raise HTTPException(409,'本房间裁判不能作为参赛者加入。')
                old=db.execute('SELECT alias FROM members WHERE room_id=? AND user_id=? AND removed_at IS NULL',
                               (room_id,u['id'])).fetchone()
                if old:
                    return {'room_id':room_id,'alias':old['alias']}
                if db.execute('SELECT 1 FROM members WHERE room_id=? AND user_id=? AND removed_at IS NOT NULL',
                              (room_id,u['id'])).fetchone():
                    raise HTTPException(403,'你已被主持人移出这个房间，不能重新加入。')
                ensure_roster_unlocked(db,room)
                alias=new_alias(db,room_id)
                db.execute('INSERT INTO members(room_id,user_id,alias,joined_at) VALUES(?,?,?,?)',
                           (room_id,u['id'],alias,utcnow()))
                log_event(db,room_id,1,'joined',alias,'加入比赛')
                role='player'
        await app.state.hub.notify(room_id,'judge_added' if role=='judge' else 'joined')
        return {'room_id':room_id,'role':'judge'} if role=='judge' else {'room_id':room_id,'alias':alias}

    @app.get('/api/rooms/{room_id}')
    def room_info(room_id:int,request:Request):
        with connection() as db:
            u=caller(request,db)
            room,mine=permission(db,room_id,u)
            return public_room(db,room,u,mine)

    @app.get('/api/rooms/{room_id}/chat')
    def list_room_chat(room_id:int,request:Request,match_number:int=Query(...,ge=1)):
        with connection() as db:
            u=caller(request,db)
            room=require_chat_access(db,room_id,u)
            check_match_number(room,match_number)
            if room['status']=='finished':
                return {'match_number':match_number,'messages':[]}
            rows=db.execute('''SELECT id,match_number,sender_uid,sender_name,text,created_at
                FROM chat_messages WHERE room_id=? AND match_number=? ORDER BY id''',
                (room_id,match_number)).fetchall()
            return {'match_number':match_number,'messages':[dict(row) for row in rows]}

    @app.post('/api/rooms/{room_id}/chat')
    async def send_room_chat(room_id:int,body:ChatMessageData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room=require_chat_access(db,room_id,u)
            check_match_number(room,body.match_number)
            if room['status']!='open':
                raise HTTPException(409,'本场比赛已结束，公共聊天已关闭。')
            message=norm_text(body.text,2000)
            created=utcnow()
            cur=db.execute('''INSERT INTO chat_messages(room_id,match_number,sender_user_id,sender_uid,
                sender_name,text,created_at) VALUES(?,?,?,?,?,?,?)''',
                (room_id,body.match_number,u['id'],u['uid'],u['display_name'],message,created))
            payload={'id':cur.lastrowid,'match_number':body.match_number,'sender_uid':u['uid'],
                     'sender_name':u['display_name'],'text':message,'created_at':created}
        await app.state.hub.notify_chat(room_id,payload)
        return {'ok':True,'message':payload}

    @app.post('/api/rooms/{room_id}/models')
    async def create_model_profile(room_id:int,body:ModelProfileData,request:Request):
        # Compatibility adapter: a room manager may create only a profile owned by themselves.
        with connection() as db:
            u=caller(request,db,mutate=True)
            require_room_manager(db,room_id,u)
        try:
            key=(body.api_key or '').strip()
            if not key:
                raise ModelAPIError('创建模型配置时必须填写 API Key。')
            base=await asyncio.to_thread(validate_base_url,body.base_url)
            encrypted=encrypt_api_key(key)
        except ModelAPIError as exc:
            raise HTTPException(422,str(exc))
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            now=utcnow()
            cur=db.execute('''INSERT INTO model_profiles(owner_user_id,legacy_room_id,name,base_url,model,
                api_key_encrypted,temperature,max_tokens,timeout_seconds,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (u['id'],None,norm_text(body.name,60),base,norm_text(body.model,120),encrypted,
                 body.temperature,body.max_tokens,body.timeout_seconds,now,now))
            log_event(db,room_id,room['current_round'],'model_profile_created','房主','新增个人模型 API 配置')
            profile_id=cur.lastrowid
        await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True,'id':profile_id}

    @app.put('/api/rooms/{room_id}/models/{profile_id}')
    async def update_model_profile(room_id:int,profile_id:int,body:ModelProfileData,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
            require_room_manager(db,room_id,u)
            old=db.execute('''SELECT mp.*,u.deleted_at AS owner_deleted_at FROM model_profiles mp
                JOIN users u ON u.id=mp.owner_user_id WHERE mp.id=? AND mp.owner_user_id=?''',
                (profile_id,u['id'])).fetchone()
            if not old or old['ownership_issue']:
                raise HTTPException(404,'模型配置不存在或不属于当前账号。')
            status=model_profile_credential_status(dict(old))
        try:
            base=await asyncio.to_thread(validate_base_url,body.base_url)
            key=(body.api_key or '').strip()
            if key:
                new_encrypted=encrypt_api_key(key)
            else:
                if status!='ok':
                    raise ModelAPIError('此配置的 API Key 不可用，必须重新填写后才能修复。')
                new_encrypted=None
        except ModelAPIError as exc:
            raise HTTPException(422,str(exc))
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            current=db.execute('SELECT * FROM model_profiles WHERE id=? AND owner_user_id=? AND ownership_issue IS NULL',
                               (profile_id,u['id'])).fetchone()
            if not current:
                raise HTTPException(404,'模型配置不存在或不属于当前账号。')
            if new_encrypted is None and model_profile_credential_status(dict(current,owner_deleted_at=None))!='ok':
                raise HTTPException(422,'此配置的 API Key 已不可用，必须重新填写后才能修复。')
            encrypted=new_encrypted if new_encrypted is not None else current['api_key_encrypted']
            db.execute('''UPDATE model_profiles SET name=?,base_url=?,model=?,api_key_encrypted=?,
                temperature=?,max_tokens=?,timeout_seconds=?,updated_at=? WHERE id=? AND owner_user_id=?''',
                (norm_text(body.name,60),base,norm_text(body.model,120),encrypted,
                 body.temperature,body.max_tokens,body.timeout_seconds,utcnow(),profile_id,u['id']))
            rooms=[row['room_id'] for row in db.execute('''SELECT room_id FROM ai_players WHERE model_profile_id=?
                UNION SELECT room_id FROM ai_judges WHERE model_profile_id=?''',(profile_id,profile_id))]
            log_event(db,room_id,room['current_round'],'model_profile_updated','房主','更新个人模型 API 配置')
        for affected_room in set(rooms)|{room_id}:
            await app.state.hub.notify(affected_room,'model_profile_changed')
        return {'ok':True}

    @app.delete('/api/rooms/{room_id}/models/{profile_id}')
    async def delete_model_profile(room_id:int,profile_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if not db.execute('SELECT 1 FROM model_profiles WHERE id=? AND owner_user_id=? AND ownership_issue IS NULL',
                              (profile_id,u['id'])).fetchone():
                raise HTTPException(404,'模型配置不存在或不属于当前账号。')
            rooms=[row['room_id'] for row in db.execute('''SELECT room_id FROM ai_players WHERE model_profile_id=?
                UNION SELECT room_id FROM ai_judges WHERE model_profile_id=?''',(profile_id,profile_id))]
            db.execute('UPDATE ai_players SET model_profile_id=NULL WHERE model_profile_id=?',(profile_id,))
            db.execute('UPDATE ai_judges SET model_profile_id=NULL WHERE model_profile_id=?',(profile_id,))
            db.execute("""DELETE FROM model_operation_tasks
                WHERE purpose='connection_test' AND profile_id=? AND profile_owner_user_id=?""",
                (profile_id,u['id']))
            db.execute('DELETE FROM model_profiles WHERE id=? AND owner_user_id=?',(profile_id,u['id']))
            log_event(db,room_id,room['current_round'],'model_profile_deleted','房主','删除个人模型配置，关联席位改为手动转录')
        for affected_room in set(rooms)|{room_id}:
            await app.state.hub.notify(affected_room,'model_profile_changed')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/models/{profile_id}/test')
    async def test_model_profile(room_id:int,profile_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            require_room_manager(db,room_id,u)
            profile=require_owned_model_profile(db,profile_id,u['id'])
            operation_id,created,_=create_model_operation_task(
                db,purpose='connection_test',target_type='model_profile',
                target_key=profile_id,target_label=profile['name'],profile=profile,
                trigger_user_id=u['id'],idempotency_key=request.headers.get('Idempotency-Key'))
            if not created:
                raise HTTPException(409,'该模型配置已有连接测试正在进行。')
        actor_id=u['id']
        session_hash=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
        def preflight():
            with connection() as db:
                return (model_call_actor_valid(db,room_id,actor_id,session_hash)
                        and model_profile_snapshot_valid(db,profile)
                        and profile['owner_user_id']==actor_id)
        try:
            key=decrypt_api_key(profile['api_key_encrypted'])
            started=time.perf_counter()
            result=await model_call(profile,[
                {'role':'system','content':'You are a connection test. Reply with exactly OK.'},
                {'role':'user','content':'Reply with OK.'}],key,preflight,operation_id=operation_id)
        except ModelCallCancelled:
            raise HTTPException(409,'测试排队期间房间权限、模型配置或账号状态已变化，请重试。')
        except ModelAPIError as exc:
            await finish_and_notify_model_operation(operation_id,'failed',error=exc)
            raise HTTPException(502,str(exc))
        with connection() as db:
            token_hash=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
            session=db.execute('''SELECT u.id,u.is_admin FROM sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=? AND s.expires_at>? AND u.deleted_at IS NULL''',
                (token_hash,utcnow())).fetchone()
            current_room=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
            valid=bool(session and current_room and can_manage_room(current_room,dict(session)) and
                db.execute('SELECT 1 FROM model_profiles WHERE id=? AND owner_user_id=?',
                           (profile_id,session['id'])).fetchone())
        if not valid:
            await finish_and_notify_model_operation(operation_id,'stale',
                error='测试期间模型配置或账号状态已变化；服务商请求结果未作为有效测试应用。')
            raise HTTPException(409,'测试期间模型配置或账号状态已变化，请重试。')
        await finish_and_notify_model_operation(operation_id,'completed',result_applied=True)
        return {'ok':True,'model':profile['model'],'preview':result['text'][:120],
                'latency_ms':round((time.perf_counter()-started)*1000),'usage':result['usage']}

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
            cleared_match=room['match_number']
        app.state.hub.typing.pop(room_id,None)
        await app.state.hub.notify(room_id,'records_deleted')
        await app.state.hub.clear_chat(room_id,cleared_match)
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/restart')
    async def restart_match(room_id:int,body:MatchActionData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            check_match_number(room,body.match_number)
            if room['status']!='finished':
                raise HTTPException(409,'必须先完成当前比赛才能再次开赛。')
            # Must run before clearing the previous match: a retained judge cannot be
            # reactivated while they are still judging another unfinished room.
            check_restart_judge_conflicts(db,room_id,u)
            clear_match_records(db,room_id)
            db.execute('''DELETE FROM members WHERE room_id=? AND (removed_at IS NOT NULL OR
                user_id IN (SELECT id FROM users WHERE deleted_at IS NOT NULL))''',(room_id,))
            db.execute('''DELETE FROM room_judges WHERE room_id=? AND user_id IN
                (SELECT id FROM users WHERE deleted_at IS NOT NULL)''',(room_id,))
            db.execute('''UPDATE rooms SET status='open',current_round=1,records_deleted=0,
                match_number=match_number+1 WHERE id=?''',(room_id,))
            db.execute('INSERT INTO rounds(room_id,number,created_at) VALUES(?,?,?)',(room_id,1,utcnow()))
            new_match_number=room['match_number']+1
            log_event(db,room_id,1,'match_restarted','主持人','重新开赛，上一场比赛记录已清空')
        app.state.hub.typing.pop(room_id,None)
        await app.state.hub.notify(room_id,'match_restarted')
        await app.state.hub.clear_chat(room_id,new_match_number)
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
                return {'ok':True,'already_joined':True}
            check_judge_registration(db,room,target['id'],viewer=u)
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
                require_owned_model_profile(db,body.model_profile_id,u['id'])
            cur=db.execute('INSERT INTO ai_judges(room_id,label,model_profile_id,created_at) VALUES(?,?,?,?)',
                           (room_id,label,body.model_profile_id,utcnow()))
            log_event(db,room_id,room['current_round'],'ai_judge_added','管理员','已添加 AI 裁判席')
            jid=cur.lastrowid
        await app.state.hub.notify(room_id,'ai_judge_added')
        return {'ok':True,'id':jid,'label':label}

    @app.put('/api/rooms/{room_id}/ai-judges/{judge_id}/model')
    async def bind_ai_judge_model(room_id:int,judge_id:int,body:ModelBindingData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if not db.execute('SELECT 1 FROM ai_judges WHERE room_id=? AND id=?',(room_id,judge_id)).fetchone():
                raise HTTPException(404,'AI 裁判席不存在。')
            if body.keep_existing:
                return {'ok':True,'unchanged':True}
            if room['status']!='open' or room['verdict_locked_at'] or db.execute(
                'SELECT 1 FROM judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone() or db.execute(
                'SELECT 1 FROM ai_judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone():
                raise HTTPException(409,'比赛结束或裁判已锁定判断，不能更换模型绑定。')
            if db.execute('SELECT 1 FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
                          (room_id,judge_id)).fetchone():
                raise HTTPException(409,'此 AI 裁判已锁定判断，不能更换模型绑定。')
            if body.model_profile_id is not None:
                require_owned_model_profile(db,body.model_profile_id,u['id'])
            db.execute('UPDATE ai_judges SET model_profile_id=? WHERE room_id=? AND id=?',
                       (body.model_profile_id,room_id,judge_id))
        await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True}

    @app.post('/api/rooms/{room_id}/ai-judges/questions/generate')
    async def generate_ai_judge_questions(room_id:int,request:Request,
                                          target_judge_id:Optional[int]=Query(default=None,ge=1)):
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
                raise HTTPException(409,f'至少需要两名在场参赛者才能生成 AI 裁判问题；当前仅有 {len(roster)} 位。请先让至少两位参赛者加入。')
            rows=db.execute('''SELECT j.id AS judge_id,j.label,j.model_profile_id,
                    mp.*,u.deleted_at AS owner_deleted_at,q.id AS question_id
                FROM ai_judges j
                LEFT JOIN model_profiles mp ON mp.id=j.model_profile_id
                LEFT JOIN users u ON u.id=mp.owner_user_id
                LEFT JOIN ai_judge_questions q ON q.room_id=j.room_id AND q.round_number=? AND q.ai_judge_id=j.id
                WHERE j.room_id=? ORDER BY j.id''',(rnd['number'],room_id)).fetchall()
            if target_judge_id is not None:
                rows=[row for row in rows if row['judge_id']==target_judge_id]
            match_number=room['match_number']
            actor_id=u['id']
            session_hash=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
            round_number=rnd['number']
            history=anonymous_match_packet(db,room_id,round_number-1)
            profiles=[]
            results=[]
            for raw in rows:
                profile=dict(raw)
                if profile['question_id'] is not None:
                    continue
                if profile['model_profile_id'] is None:
                    results.append({'judge_id':profile['judge_id'],'label':profile['label'],
                                    'status':'skipped','reason':'此 AI 裁判席尚未绑定个人模型配置；请绑定可用配置，或手动转录问题。'})
                    continue
                credential_status=model_profile_credential_status(profile)
                if credential_status!='ok':
                    reasons={
                        'missing':'绑定的模型配置缺少 API Key，请在个人中心补录后保存。',
                        'needs_repair':'绑定的模型配置无法解密，请在个人中心重新录入 API Key。',
                        'key_unavailable':'服务器模型凭据密钥不可用，请联系管理员。',
                        'owner_unavailable':'绑定的模型配置或其所属账号不可用，请重新绑定可用配置。',
                    }
                    results.append({'judge_id':profile['judge_id'],'label':profile['label'],
                                    'status':'skipped','reason':reasons.get(
                                        credential_status,'绑定的模型配置不可用，请在个人中心检查后重新绑定。')})
                    continue
                profiles.append(profile)
            context_revision=hashlib.sha256(
                (str(round_number)+'|'+','.join(roster)+'|'+history).encode('utf-8')).hexdigest()
            retry_of=getattr(request.state,'model_operation_retry_of',None)
            queued_profiles=[]
            for profile in profiles:
                with connection(immediate=True) as db:
                    operation_id,created,active_state=create_model_operation_task(
                        db,purpose='judge_question',target_type='ai_judge',
                        target_key=profile['judge_id'],target_label=profile['label'],
                        profile=profile,trigger_user_id=actor_id,room_id=room_id,
                        match_number=match_number,round_number=round_number,
                        context_revision=context_revision,retry_of=retry_of,
                        idempotency_key=request.headers.get('Idempotency-Key'))
                if created:
                    profile['_operation_id']=operation_id
                    queued_profiles.append(profile)
                else:
                    results.append({'judge_id':profile['judge_id'],'label':profile['label'],
                                    'status':active_state,'reason':'该席位已有进行中的模型任务。'})
            profiles=queued_profiles
        if profiles:
            await app.state.hub.notify(room_id,'model_operation_updated')
        if not rows:
            return {'generated':0,'skipped':0,'failed':[],'results':[],
                    'message':'当前房间没有 AI 裁判席。'}
        pending_count=len(profiles)+len(results)
        if not pending_count:
            return {'generated':0,'skipped':0,'failed':[],'results':[],
                    'message':'本轮 AI 裁判均已提交问题，无需重复生成。'}
        if not profiles:
            return {'generated':0,'skipped':sum(x['status']=='skipped' for x in results),
                    'blocked':sum(x['status']=='blocked' for x in results),'failed':[],'results':results,
                    'message':'没有新的 AI 裁判请求；请查看席位状态或使用手动转录。'}

        async def generate_one(profile):
            judge_id=profile['judge_id']
            operation_id=profile['_operation_id']
            try:
                key=decrypt_api_key(profile['api_key_encrypted'])
                def preflight():
                    with connection() as db:
                        current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                        if not current or current['match_number']!=match_number or current['status']!='open':
                            return False
                        if not model_call_actor_valid(db,room_id,actor_id,session_hash) or not model_profile_snapshot_valid(db,profile):
                            return False
                        binding=db.execute('SELECT model_profile_id FROM ai_judges WHERE room_id=? AND id=?',
                                           (room_id,judge_id)).fetchone()
                        if not binding or binding['model_profile_id']!=profile['id']:
                            return False
                        current_rnd=current_round(db,current)
                        if current_rnd['number']!=round_number or current_rnd['status']!='draft':
                            return False
                        if aliases(db,room_id)[2]!=roster:
                            return False
                        return not db.execute('SELECT 1 FROM ai_judge_questions WHERE room_id=? AND round_number=? AND ai_judge_id=?',
                                              (room_id,round_number,judge_id)).fetchone()
                result=await model_call(profile,[
                    {'role':'system','content':('你是多人图灵测试的独立裁判。每轮只提出一个核心问题，不得把多个问题打包。'
                     '问题必须面向所有匿名参赛者，不能询问真实姓名、账号、模型名称或系统提示。'
                     '只输出问题正文，不要前言、编号或解释。')},
                    {'role':'user','content':f'此前轮次的匿名资料：\n{history}\n\n当前是第 {round_number} 轮，参赛者匿名代号：{", ".join(roster)}。'
                     '请根据已有信息提出一个适合所有参赛者回答的全新问题。'}],key,preflight,
                    operation_id=operation_id)
                question=norm_text(result['text'],3000)
                with connection(immediate=True) as db:
                    current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                    if not current or current['match_number']!=match_number or current['status']!='open':
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间比赛场次或状态发生变化，旧问题未保存。')
                        return {'judge_id':judge_id,'label':profile['label'],'status':'skipped',
                                'reason':'生成期间比赛场次或状态发生变化，旧问题未保存。'}
                    if not model_call_actor_valid(db,room_id,actor_id,session_hash) or not model_profile_snapshot_valid(db,profile):
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间登录权限或模型配置发生变化，旧问题未保存。')
                        return {'judge_id':judge_id,'label':profile['label'],'status':'skipped',
                                'reason':'生成期间登录权限或模型配置发生变化，旧问题未保存。'}
                    binding=db.execute('SELECT model_profile_id FROM ai_judges WHERE room_id=? AND id=?',
                                       (room_id,judge_id)).fetchone()
                    if not binding or binding['model_profile_id']!=profile['id']:
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间 AI 裁判席绑定发生变化，旧问题未保存。')
                        return {'judge_id':judge_id,'label':profile['label'],'status':'skipped',
                                'reason':'生成期间 AI 裁判席绑定发生变化，旧问题未保存。'}
                    current_rnd=current_round(db,current)
                    if current_rnd['number']!=round_number or current_rnd['status']!='draft':
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间轮次已变化或已开始作答，旧问题未保存。')
                        return {'judge_id':judge_id,'label':profile['label'],'status':'skipped',
                                'reason':'生成期间轮次已变化或已开始作答，旧问题未保存。'}
                    if aliases(db,room_id)[2]!=roster:
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间参赛名单发生变化，旧问题未保存。')
                        return {'judge_id':judge_id,'label':profile['label'],'status':'skipped',
                                'reason':'生成期间参赛名单发生变化，旧问题未保存。'}
                    if db.execute('SELECT 1 FROM ai_judge_questions WHERE room_id=? AND round_number=? AND ai_judge_id=?',
                                  (room_id,round_number,judge_id)).fetchone():
                        finish_model_operation_in_db(db,operation_id,'stale',error='该 AI 裁判已提交本轮问题，未重复保存。')
                        return {'judge_id':judge_id,'label':profile['label'],'status':'skipped',
                                'reason':'该 AI 裁判已提交本轮问题，未重复保存。'}
                    db.execute('INSERT INTO ai_judge_questions(room_id,round_number,ai_judge_id,text,created_at) VALUES(?,?,?,?,?)',
                               (room_id,round_number,judge_id,question,utcnow()))
                    log_event(db,room_id,round_number,'ai_judge_question','AI 裁判','模型 API 已自动生成本轮问题')
                    finish_model_operation_in_db(db,operation_id,'completed',result_applied=True)
                await app.state.hub.notify(room_id,'ai_judge_question')
                return {'judge_id':judge_id,'label':profile['label'],'status':'generated','usage':result['usage']}
            except ModelRequestLimitReached as exc:
                return {'judge_id':judge_id,'label':profile['label'],'status':'blocked','error':str(exc)}
            except ModelCallCancelled:
                return {'judge_id':judge_id,'label':profile['label'],'status':'skipped',
                        'reason':'任务排队期间权限、配置、绑定或比赛状态发生变化；未向服务商发送请求。'}
            except ModelAPIError as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'judge_id':judge_id,'label':profile['label'],'status':'failed','error':str(exc)}
            except HTTPException as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'judge_id':judge_id,'label':profile['label'],'status':'failed','error':str(exc.detail)}
            except Exception as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'judge_id':judge_id,'label':profile['label'],'status':'failed',
                        'error':'模型调用失败或返回内容为空，请检查配置、服务状态或稍后重试。'}

        generated_results=await asyncio.gather(*(generate_one(profile) for profile in profiles))
        results.extend(generated_results)
        failed=[x for x in results if x['status']=='failed']
        skipped=sum(x['status']=='skipped' for x in results)
        generated=sum(x['status']=='generated' for x in results)
        return {'generated':generated,'skipped':skipped,
                'blocked':sum(x['status']=='blocked' for x in results),
                'failed':failed,'results':results,
                'message':('AI 裁判问题生成完成。' if generated else
                           '没有 AI 裁判成功生成问题；请查看各席位原因。')}


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


    @app.put('/api/rooms/{room_id}/model-request-limit')
    async def update_model_request_limit(room_id:int,body:ModelRequestLimitData,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            db.execute('UPDATE rooms SET model_request_limit=? WHERE id=?',(body.limit,room_id))
            attempts=model_request_count(db,room_id,room['match_number'])
        await app.state.hub.notify(room_id,'model_request_limit_changed')
        return {'ok':True,'limit':body.limit,'attempted':attempts,
                'remaining':None if body.limit is None else max(0,body.limit-attempts)}

    @app.post('/api/rooms/{room_id}/start')
    async def start_round(room_id:int,request:Request):
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            validation=inspect_round_start(db,room)
            if validation['reason']:
                raise HTTPException(409,validation['reason'])
            rnd=validation['round']
            question=validation['question']
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
                require_owned_model_profile(db,body.model_profile_id,u['id'])
            db.execute('INSERT INTO ai_players(room_id,alias,name,model_profile_id) VALUES(?,?,?,?)',
                       (room_id,alias,norm_text(body.name,60),body.model_profile_id))
            log_event(db,room_id,1,'ai_joined',alias,'AI 参赛位已添加')
        await app.state.hub.notify(room_id,'ai_joined')
        return {'alias':alias,'automated':body.model_profile_id is not None}

    @app.put('/api/rooms/{room_id}/ai/{alias}/model')
    async def bind_ai_player_model(room_id:int,alias:str,body:ModelBindingData,request:Request):
        alias=alias.upper()
        with connection(immediate=True) as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if not db.execute('SELECT 1 FROM ai_players WHERE room_id=? AND alias=?',(room_id,alias)).fetchone():
                raise HTTPException(404,'AI 参赛席不存在。')
            if body.keep_existing:
                return {'ok':True,'unchanged':True}
            if room['status']!='open' or room['verdict_locked_at'] or db.execute(
                'SELECT 1 FROM judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone() or db.execute(
                'SELECT 1 FROM ai_judge_verdicts WHERE room_id=? LIMIT 1',(room_id,)).fetchone():
                raise HTTPException(409,'比赛结束或裁判已锁定判断，不能更换模型绑定。')
            if body.model_profile_id is not None:
                require_owned_model_profile(db,body.model_profile_id,u['id'])
            db.execute('UPDATE ai_players SET model_profile_id=? WHERE room_id=? AND alias=?',
                       (body.model_profile_id,room_id,alias))
        await app.state.hub.notify(room_id,'model_profile_changed')
        return {'ok':True}

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
        with connection() as db:
            u=caller(request,db,mutate=True)
            require_chat_access(db,room_id,u)
        raise HTTPException(410,'旧版提问建议入口已停用，请使用房间公共聊天。')

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
    async def generate_ai_answers(room_id:int,request:Request,
                                  target_alias:Optional[str]=Query(default=None,min_length=1,max_length=8)):
        with connection() as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            if room['status']!='open':
                raise HTTPException(409,'比赛已结束。')
            rnd=current_round(db,room)
            if rnd['status']!='collecting':
                raise HTTPException(409,'只有本轮正在收集回答时才能生成 AI 回复。')
            rows=db.execute('''SELECT ai.alias,ai.name AS ai_name,mp.*,
                    mp.name AS profile_name,mp.model AS model_name FROM ai_players ai
                JOIN model_profiles mp ON mp.id=ai.model_profile_id AND mp.ownership_issue IS NULL
                JOIN users profile_owner ON profile_owner.id=mp.owner_user_id AND profile_owner.deleted_at IS NULL
                LEFT JOIN answers a ON a.round_id=? AND a.alias=ai.alias
                WHERE ai.room_id=? AND a.id IS NULL ORDER BY ai.alias''',(rnd['id'],room_id)).fetchall()
            if target_alias is not None:
                rows=[row for row in rows if row['alias']==target_alias.upper()]
            match_number=room['match_number']
            actor_id=u['id']
            session_hash=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
            round_number=rnd['number']
            question=rnd['question']
            profiles=[dict(row) for row in rows]
            history_by_alias={row['alias']:anonymous_player_history(db,room_id,row['alias'],round_number-1)
                              for row in rows}
            retry_of=getattr(request.state,'model_operation_retry_of',None)
            queued_profiles=[]
            for profile in profiles:
                context_revision=hashlib.sha256(
                    (question+'\0'+history_by_alias.get(profile['alias'],'')).encode('utf-8')).hexdigest()
                with connection(immediate=True) as task_db:
                    operation_id,created,active_state=create_model_operation_task(
                        task_db,purpose='ai_answer',target_type='ai_player',
                        target_key=profile['alias'],
                        target_label=f"{profile['alias']} · {profile.get('ai_name') or profile.get('name')}",
                        profile=profile,trigger_user_id=actor_id,room_id=room_id,
                        match_number=match_number,round_number=round_number,
                        context_revision=context_revision,retry_of=retry_of,
                        idempotency_key=request.headers.get('Idempotency-Key'))
                if created:
                    profile['_operation_id']=operation_id
                    queued_profiles.append(profile)
                else:
                    results.append({'alias':profile['alias'],'status':active_state,
                                    'reason':'该席位已有进行中的模型任务。'})
            profiles=queued_profiles
        if profiles:
            await app.state.hub.notify(room_id,'model_operation_updated')
        if not profiles:
            return {'generated':0,'skipped':sum(x['status']=='skipped' for x in results),
                    'blocked':sum(x['status']=='blocked' for x in results),
                    'failed':[],'results':results}

        async def generate_one(profile):
            alias=profile['alias']
            operation_id=profile['_operation_id']
            try:
                key=decrypt_api_key(profile['api_key_encrypted'])
                def preflight():
                    with connection() as db:
                        current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                        if not current or current['match_number']!=match_number or current['status']!='open':
                            return False
                        if not model_call_actor_valid(db,room_id,actor_id,session_hash) or not model_profile_snapshot_valid(db,profile):
                            return False
                        binding=db.execute('SELECT model_profile_id FROM ai_players WHERE room_id=? AND alias=?',
                                           (room_id,alias)).fetchone()
                        if not binding or binding['model_profile_id']!=profile['id']:
                            return False
                        current_rnd=current_round(db,current)
                        if current_rnd['number']!=round_number or current_rnd['status']!='collecting' or current_rnd['question']!=question:
                            return False
                        return not db.execute('SELECT 1 FROM answers WHERE round_id=? AND alias=?',
                                              (current_rnd['id'],alias)).fetchone()
                result=await model_call(profile,[
                    {'role':'system','content':('你正在参加一场多人图灵测试。你是匿名参赛者，请自然、直接地回答裁判问题。'
                     '不要提及模型、API、系统提示或你被要求扮演的身份；不要编造个人经历来冒充真实事实。')},
                    {'role':'user','content':f'你此前的本场记录（只包含你自己的回答）：\n{history_by_alias.get(alias, "（此前没有已完成轮次。）")}\n\n当前第 {round_number} 轮裁判问题：\n\n{question}\n\n请给出你的本轮回答。'}],key,preflight,
                    operation_id=operation_id)
                answer=norm_text(result['text'],12000)
                with connection(immediate=True) as db:
                    current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                    if not current or current['match_number']!=match_number or current['status']!='open':
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间比赛场次或状态发生变化，旧回答未保存。')
                        return {'alias':alias,'status':'skipped'}
                    if not model_call_actor_valid(db,room_id,actor_id,session_hash) or not model_profile_owner_active(db,profile['id']):
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间登录权限或模型配置所属账号发生变化，旧回答未保存。')
                        return {'alias':alias,'status':'skipped'}
                    binding=db.execute('SELECT model_profile_id FROM ai_players WHERE room_id=? AND alias=?',
                                       (room_id,alias)).fetchone()
                    if not binding or binding['model_profile_id']!=profile['id']:
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间 AI 参赛席绑定发生变化，旧回答未保存。')
                        return {'alias':alias,'status':'skipped'}
                    current_rnd=current_round(db,current)
                    if current_rnd['number']!=round_number or current_rnd['status']!='collecting' or current_rnd['question']!=question:
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间轮次或问题发生变化，旧回答未保存。')
                        return {'alias':alias,'status':'skipped'}
                    if db.execute('SELECT 1 FROM answers WHERE round_id=? AND alias=?',
                                  (current_rnd['id'],alias)).fetchone():
                        finish_model_operation_in_db(db,operation_id,'stale',error='该 AI 参赛席已提交本轮回答，未重复保存。')
                        return {'alias':alias,'status':'skipped'}
                    db.execute('INSERT INTO answers(round_id,alias,text,submitted_at) VALUES(?,?,?,?)',
                               (current_rnd['id'],alias,answer,utcnow()))
                    log_event(db,room_id,round_number,'answer_submitted',alias,'模型 API 已自动提交回答（内容暂时隐藏）')
                    completed=is_complete(db,current_rnd,room_id)
                    if completed:
                        log_event(db,room_id,round_number,'round_revealed','系统','本轮全部回答已公开')
                    finish_model_operation_in_db(db,operation_id,'completed',result_applied=True)
                await app.state.hub.notify(room_id,'round_revealed' if completed else 'answer_submitted')
                return {'alias':alias,'status':'generated','usage':result['usage']}
            except ModelRequestLimitReached as exc:
                return {'alias':alias,'status':'blocked','error':str(exc)}
            except ModelCallCancelled:
                return {'alias':alias,'status':'skipped'}
            except ModelAPIError as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'alias':alias,'status':'failed','error':str(exc)}
            except HTTPException as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'alias':alias,'status':'failed','error':str(exc.detail)}
            except Exception as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'alias':alias,'status':'failed','error':'模型调用失败，请检查配置或稍后重试。'}

        results=await asyncio.gather(*(generate_one(profile) for profile in profiles))
        return {'generated':sum(x['status']=='generated' for x in results),
                'skipped':sum(x['status']=='skipped' for x in results),
                'blocked':sum(x['status']=='blocked' for x in results),
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
    async def generate_ai_judge_verdicts(room_id:int,request:Request,
                                         target_judge_id:Optional[int]=Query(default=None,ge=1)):
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
            evidence_revision=hashlib.sha256(packet.encode('utf-8')).hexdigest()
            rows=db.execute('''SELECT j.id AS judge_id,j.label,mp.* FROM ai_judges j
                JOIN model_profiles mp ON mp.id=j.model_profile_id AND mp.ownership_issue IS NULL
                JOIN users profile_owner ON profile_owner.id=mp.owner_user_id AND profile_owner.deleted_at IS NULL
                LEFT JOIN ai_judge_verdicts v ON v.room_id=j.room_id AND v.ai_judge_id=j.id
                WHERE j.room_id=? AND v.ai_judge_id IS NULL ORDER BY j.id''',(room_id,)).fetchall()
            if target_judge_id is not None:
                rows=[row for row in rows if row['judge_id']==target_judge_id]
            match_number=room['match_number']
            actor_id=u['id']
            session_hash=hashlib.sha256(request.cookies.get(COOKIE,'').encode()).hexdigest()
            profiles=[dict(row) for row in rows]
            retry_of=getattr(request.state,'model_operation_retry_of',None)
            queued_profiles=[]
            for profile in profiles:
                with connection(immediate=True) as task_db:
                    operation_id,created,active_state=create_model_operation_task(
                        task_db,purpose='judge_verdict',target_type='ai_judge',
                        target_key=profile['judge_id'],target_label=profile['label'],
                        profile=profile,trigger_user_id=actor_id,room_id=room_id,
                        match_number=match_number,round_number=room['current_round'],
                        context_revision=evidence_revision,retry_of=retry_of,
                        idempotency_key=request.headers.get('Idempotency-Key'))
                if created:
                    profile['_operation_id']=operation_id
                    queued_profiles.append(profile)
                else:
                    results.append({'judge_id':profile['judge_id'],'status':active_state,
                                    'reason':'该席位已有进行中的模型任务。'})
            profiles=queued_profiles
        if profiles:
            await app.state.hub.notify(room_id,'model_operation_updated')
        if not profiles:
            return {'generated':0,'skipped':sum(x['status']=='skipped' for x in results),
                    'blocked':sum(x['status']=='blocked' for x in results),
                    'failed':[],'results':results}

        async def generate_one(profile):
            judge_id=profile['judge_id']
            operation_id=profile['_operation_id']
            try:
                key=decrypt_api_key(profile['api_key_encrypted'])
                def preflight():
                    with connection() as db:
                        current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                        if not current or current['match_number']!=match_number or current['status']!='open':
                            return False
                        if not model_call_actor_valid(db,room_id,actor_id,session_hash) or not model_profile_snapshot_valid(db,profile):
                            return False
                        binding=db.execute('SELECT model_profile_id FROM ai_judges WHERE room_id=? AND id=?',
                                           (room_id,judge_id)).fetchone()
                        if not binding or binding['model_profile_id']!=profile['id']:
                            return False
                        states=db.execute('SELECT status FROM rounds WHERE room_id=?',(room_id,)).fetchall()
                        if not states or any(x['status']!='revealed' for x in states):
                            return False
                        if db.execute('SELECT 1 FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
                                      (room_id,judge_id)).fetchone():
                            return False
                        current_packet=anonymous_match_packet(db,room_id)
                        return hashlib.sha256(current_packet.encode('utf-8')).hexdigest()==evidence_revision
                result=await model_call(profile,[
                    {'role':'system','content':('你是多人图灵测试的独立裁判。你必须只根据匿名代号及参赛者回答，判断每个代号实际是人类还是 AI。'
                     '不能访问或猜测真实身份、模型来源、账号资料。对所有代号都必须给出判断。'
                     '严格返回 JSON 对象，不要使用 Markdown 代码块或附加说明。结构：'
                     '{"predictions":{"A":"Human"},"evidence":"判断依据","confidence":0,"weakness":"最不确定之处","possible_error":"可能误判原因"}。'
                     'predictions 的值只能是 Human 或 AI，confidence 必须为 0 到 100 的整数。')},
                    {'role':'user','content':packet}],key,preflight,
                    operation_id=operation_id)
                payload=parse_json_object(result['text'])
                verdict=VerdictData.model_validate(payload)
                if set(verdict.predictions)!=set(roster) or not all(v in ('Human','AI') for v in verdict.predictions.values()):
                    raise ModelAPIError('AI 裁判没有为所有匿名代号给出有效的 Human / AI 判断。')
                if not verdict.evidence.strip():
                    raise ModelAPIError('AI 裁判没有提供判断依据。')
                with connection(immediate=True) as db:
                    current=db.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                    if not current or current['match_number']!=match_number or current['status']!='open':
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间比赛场次或状态发生变化，旧判决未保存。')
                        return {'judge_id':judge_id,'status':'skipped'}
                    if not model_call_actor_valid(db,room_id,actor_id,session_hash) or not model_profile_owner_active(db,profile['id']):
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间登录权限或模型配置所属账号发生变化，旧判决未保存。')
                        return {'judge_id':judge_id,'status':'skipped'}
                    binding=db.execute('SELECT model_profile_id FROM ai_judges WHERE room_id=? AND id=?',
                                       (room_id,judge_id)).fetchone()
                    if not binding or binding['model_profile_id']!=profile['id']:
                        finish_model_operation_in_db(db,operation_id,'stale',error='生成期间 AI 裁判席绑定发生变化，旧判决未保存。')
                        return {'judge_id':judge_id,'status':'skipped'}
                    states=db.execute('SELECT status FROM rounds WHERE room_id=?',(room_id,)).fetchall()
                    if not states or any(x['status']!='revealed' for x in states):
                        finish_model_operation_in_db(db,operation_id,'stale',error='比赛资料在模型判断期间发生变化，已废弃旧判决，请重新生成。')
                        return {'judge_id':judge_id,'status':'skipped',
                                'reason':'比赛资料发生变化，旧判决已作废。'}
                    current_packet=anonymous_match_packet(db,room_id)
                    current_revision=hashlib.sha256(current_packet.encode('utf-8')).hexdigest()
                    if current_revision!=evidence_revision:
                        finish_model_operation_in_db(db,operation_id,'stale',error='比赛资料在模型判断期间发生变化，已废弃旧判决，请重新生成。')
                        return {'judge_id':judge_id,'status':'skipped',
                                'reason':'比赛资料发生变化，旧判决已作废。'}
                    if db.execute('SELECT 1 FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
                                  (room_id,judge_id)).fetchone():
                        finish_model_operation_in_db(db,operation_id,'stale',error='该 AI 裁判已经锁定判断，旧结果未重复保存。')
                        return {'judge_id':judge_id,'status':'skipped'}
                    locked=utcnow()
                    data=verdict.model_dump()
                    data['locked_at']=locked
                    db.execute('INSERT INTO ai_judge_verdicts(room_id,ai_judge_id,verdict_json,locked_at) VALUES(?,?,?,?)',
                               (room_id,judge_id,json.dumps(data,ensure_ascii=False),locked))
                    log_event(db,room_id,current['current_round'],'ai_judge_verdict_locked','AI 裁判','模型 API 已自动完成最终判断并锁定')
                    finish_model_operation_in_db(db,operation_id,'completed',result_applied=True)
                await app.state.hub.notify(room_id,'ai_judge_verdict_locked')
                return {'judge_id':judge_id,'status':'generated','usage':result['usage']}
            except ModelRequestLimitReached as exc:
                return {'judge_id':judge_id,'status':'blocked','error':str(exc)}
            except ModelCallCancelled:
                return {'judge_id':judge_id,'status':'skipped'}
            except ModelAPIError as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'judge_id':judge_id,'status':'failed','error':str(exc)}
            except Exception as exc:
                await finish_and_notify_model_operation(operation_id,'failed',error=exc)
                return {'judge_id':judge_id,'status':'failed','error':'AI 裁判返回格式无效或模型调用失败，请检查配置后重试。'}

        results=await asyncio.gather(*(generate_one(profile) for profile in profiles))
        return {'generated':sum(x['status']=='generated' for x in results),
                'skipped':sum(x['status']=='skipped' for x in results),
                'blocked':sum(x['status']=='blocked' for x in results),
                'failed':[x for x in results if x['status']=='failed'],'results':results}


    @app.post('/api/rooms/{room_id}/model-operations/{operation_id}/retry')
    async def retry_model_operation(room_id:int,operation_id:int,request:Request):
        with connection() as db:
            u=caller(request,db,mutate=True)
            room,_=require_room_manager(db,room_id,u)
            task=db.execute('SELECT * FROM model_operation_tasks WHERE id=? AND room_id=?',
                            (operation_id,room_id)).fetchone()
            if not task:
                raise HTTPException(404,'模型任务不存在或已随比赛记录清理。')
            if task['state'] not in ('failed','interrupted','blocked'):
                raise HTTPException(409,'只有失败、中断或额度阻止的任务可以重试。')
            if task['match_number']!=room['match_number'] or task['round_number']!=room['current_round']:
                raise HTTPException(409,'任务所属比赛场次或轮次已变化，不能重试旧任务。')
            if room['status']!='open' or room['verdict_locked_at']:
                raise HTTPException(409,'比赛已结束或判决已锁定，不能重试。')
            profile_row=db.execute('''SELECT mp.*,u.deleted_at AS owner_deleted_at
                FROM model_profiles mp JOIN users u ON u.id=mp.owner_user_id
                WHERE mp.id=? AND mp.owner_user_id=?''',
                (task['profile_id'],task['profile_owner_user_id'])).fetchone()
            if not profile_row or model_profile_credential_status(dict(profile_row))!='ok':
                raise HTTPException(409,'原模型配置已删除或不可用，请先修复配置并重新绑定。')
            profile=dict(profile_row)
            if model_profile_snapshot_hash(profile)!=task['profile_snapshot_hash']:
                raise HTTPException(409,'原模型配置已发生变化，请确认新配置后从当前阶段重新发起。')
            purpose=task['purpose']
            target_key=task['target_key']
            if purpose=='judge_question':
                rnd=current_round(db,room)
                if rnd['number']!=task['round_number'] or rnd['status']!='draft':
                    raise HTTPException(409,'当前轮次已开始或变化，不能重试出题。')
                if active_participant_count(db,room_id)<2:
                    raise HTTPException(409,'至少需要两名在场参赛者才能重新出题。')
                judge=db.execute('SELECT model_profile_id,label FROM ai_judges WHERE room_id=? AND id=?',
                                 (room_id,int(target_key))).fetchone()
                if not judge or judge['model_profile_id']!=task['profile_id']:
                    raise HTTPException(409,'AI 裁判席已删除或更换绑定，不能重试旧任务。')
                if db.execute('SELECT 1 FROM ai_judge_questions WHERE room_id=? AND round_number=? AND ai_judge_id=?',
                              (room_id,task['round_number'],int(target_key))).fetchone():
                    raise HTTPException(409,'该 AI 裁判已提交问题，无需重试。')
                roster=aliases(db,room_id)[2]
                history=anonymous_match_packet(db,room_id,task['round_number']-1)
                revision=hashlib.sha256(
                    (str(task['round_number'])+'|'+','.join(roster)+'|'+history).encode('utf-8')).hexdigest()
                if revision!=task['context_revision']:
                    raise HTTPException(409,'本次出题依据已变化，请从当前阶段重新发起。')
                target_judge_id=int(target_key)
                retry_function=generate_ai_judge_questions
            elif purpose=='ai_answer':
                rnd=current_round(db,room)
                if rnd['number']!=task['round_number'] or rnd['status']!='collecting':
                    raise HTTPException(409,'当前轮次已结束或变化，不能重试回答。')
                seat=db.execute('SELECT name,model_profile_id FROM ai_players WHERE room_id=? AND alias=?',
                                (room_id,target_key)).fetchone()
                if not seat or seat['model_profile_id']!=task['profile_id']:
                    raise HTTPException(409,'AI 参赛席已删除或更换绑定，不能重试旧任务。')
                if db.execute('SELECT 1 FROM answers WHERE round_id=? AND alias=?',
                              (rnd['id'],target_key)).fetchone():
                    raise HTTPException(409,'该 AI 参赛席已提交回答，无需重试。')
                history=anonymous_player_history(db,room_id,target_key,task['round_number']-1)
                revision=hashlib.sha256((rnd['question']+'\0'+history).encode('utf-8')).hexdigest()
                if revision!=task['context_revision']:
                    raise HTTPException(409,'回答所依据的问题或历史资料已变化，不能重试旧任务。')
                target_alias=target_key
                retry_function=generate_ai_answers
            elif purpose=='judge_verdict':
                rounds=db.execute('SELECT status FROM rounds WHERE room_id=? ORDER BY number',(room_id,)).fetchall()
                if not rounds or any(row['status']!='revealed' for row in rounds):
                    raise HTTPException(409,'所有轮次必须保持已揭晓状态，才能重试最终判断。')
                judge=db.execute('SELECT model_profile_id FROM ai_judges WHERE room_id=? AND id=?',
                                 (room_id,int(target_key))).fetchone()
                if not judge or judge['model_profile_id']!=task['profile_id']:
                    raise HTTPException(409,'AI 裁判席已删除或更换绑定，不能重试旧任务。')
                if db.execute('SELECT 1 FROM ai_judge_verdicts WHERE room_id=? AND ai_judge_id=?',
                              (room_id,int(target_key))).fetchone():
                    raise HTTPException(409,'该 AI 裁判已锁定判断，无需重试。')
                packet=anonymous_match_packet(db,room_id)
                revision=hashlib.sha256(packet.encode('utf-8')).hexdigest()
                if revision!=task['context_revision']:
                    raise HTTPException(409,'最终判断依据已变化，不能重试旧任务。')
                target_judge_id=int(target_key)
                retry_function=generate_ai_judge_verdicts
            else:
                raise HTTPException(409,'此类模型任务不支持重试。')
            if room['model_request_limit'] is not None and model_request_count(
                    db,room_id,room['match_number'])>=room['model_request_limit']:
                raise HTTPException(409,'本场模型请求次数仍已达到上限，请先提高上限。')
        request.state.model_operation_retry_of=operation_id
        if purpose=='judge_question':
            return await retry_function(room_id,request,target_judge_id=target_judge_id)
        if purpose=='ai_answer':
            return await retry_function(room_id,request,target_alias=target_alias)
        return await retry_function(room_id,request,target_judge_id=target_judge_id)

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
            clear_room_chat(db,room_id,room['match_number'])
            finished_match=room['match_number']
            log_event(db,room_id,room['current_round'],'identity_revealed','管理员','已揭晓真实身份与裁判得分')
        await app.state.hub.notify(room_id,'identity_revealed')
        await app.state.hub.clear_chat(room_id,finished_match)
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

