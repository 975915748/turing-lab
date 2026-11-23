
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT NOT NULL, pw_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts (
            identity TEXT PRIMARY KEY, failure_count INTEGER NOT NULL,
            first_failed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, code TEXT NOT NULL UNIQUE,
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
        
ALTER TABLE members ADD COLUMN removed_at TEXT;
ALTER TABLE answers ADD COLUMN edited_at TEXT;

