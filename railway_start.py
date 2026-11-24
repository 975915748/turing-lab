"""Start web server after validating persisted secrets and the first admin account."""
import os
import re
import sys
from pathlib import Path

from app import db_open, init_db, seed_admin
from model_api import ModelAPIError, decrypt_api_key, validate_credential_key


def validate_stored_model_credentials(path):
    """Inspect every saved model credential and return repairable, non-secret diagnostics."""
    with db_open(path) as db:
        rows = db.execute(
            "SELECT id, owner_user_id, legacy_room_id, ownership_issue, api_key_encrypted "
            "FROM model_profiles ORDER BY id"
        ).fetchall()
    if not rows:
        return []

    try:
        key_valid = validate_credential_key()
        key_status = None if key_valid else 'key_missing'
    except ModelAPIError:
        key_valid = False
        key_status = 'key_invalid'

    issues = []
    for row in rows:
        token = row['api_key_encrypted']
        if not token:
            status = 'empty_ciphertext'
        elif not key_valid:
            status = key_status
        else:
            try:
                decrypt_api_key(token)
                continue
            except ModelAPIError:
                status = 'cannot_decrypt'
        issues.append({
            'profile_id': row['id'],
            'room_id': row['legacy_room_id'],
            'owner_user_id': row['owner_user_id'],
            'ownership_issue': row['ownership_issue'],
            'status': status,
        })
    return issues


def main():
    path = Path(os.environ.get('DB_PATH', '/app/data/game.sqlite3'))
    init_db(path)
    credential_issues = validate_stored_model_credentials(path)
    if credential_issues:
        print(
            'MODEL CREDENTIAL RECOVERY REQUIRED: the website will start, but affected '
            'model API profiles cannot be used until their API Keys are repaired.',
            file=sys.stderr,
        )
        for issue in credential_issues:
            print(
                f"  profile_id={issue['profile_id']} room_id={issue['room_id']} "
                f"status={issue['status']}",
                file=sys.stderr,
            )
        print(
            'Use the personal center to replace affected provider API Keys. '
            'If MODEL_CREDENTIAL_KEY was lost, set a new valid Fernet key first; '
            'all previously encrypted API Keys must then be entered again.',
            file=sys.stderr,
        )
    with db_open(path) as db:
        exists = db.execute('SELECT 1 FROM users WHERE is_admin=1').fetchone() is not None
    if not exists:
        username = os.getenv('BOOTSTRAP_ADMIN_USERNAME', '').strip()
        display = os.getenv('BOOTSTRAP_ADMIN_DISPLAY', '').strip() or '主持人'
        password = os.getenv('BOOTSTRAP_ADMIN_PASSWORD', '')
        if not re.fullmatch(r'[A-Za-z0-9_]{3,30}', username) or len(display) > 40 or len(password) < 12:
            print('SECURE SETUP REQUIRED: define BOOTSTRAP_ADMIN_USERNAME (3-30 ASCII letters/digits/_), BOOTSTRAP_ADMIN_PASSWORD (12+ characters) and optionally BOOTSTRAP_ADMIN_DISPLAY using Railway Variables. Refusing to expose a website with no administrator.', file=sys.stderr)
            raise SystemExit(3)
        if not seed_admin(path, username, display, password):
            print('Admin already exists. Continuing.')
        else:
            print('Initial admin account created in persistent SQLite database.')
    port = os.environ.get('PORT', '8000')
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        print('Invalid PORT setting.', file=sys.stderr)
        raise SystemExit(4)
    os.execv(sys.executable, [sys.executable, '-m', 'uvicorn', 'app:app', '--host', '0.0.0.0', '--port', port, '--workers', '1', '--proxy-headers'])


if __name__ == '__main__':
    main()
