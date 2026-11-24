"""Start web server after validating persisted secrets and the first admin account."""
import os
import re
import sys
from pathlib import Path

from app import db_open, init_db, seed_admin
from model_api import ModelAPIError, decrypt_api_key, validate_credential_key


def validate_stored_model_credentials(path):
    """Fail closed if saved model credentials cannot be decrypted with this deployment key."""
    validate_credential_key()
    with db_open(path) as db:
        tokens = [
            row['api_key_encrypted']
            for row in db.execute(
                "SELECT api_key_encrypted FROM model_profiles WHERE api_key_encrypted<>''"
            )
        ]
    if not tokens:
        return
    validate_credential_key(required=True)
    for token in tokens:
        decrypt_api_key(token)


def main():
    path = Path(os.environ.get('DB_PATH', '/app/data/game.sqlite3'))
    init_db(path)
    try:
        validate_stored_model_credentials(path)
    except ModelAPIError as exc:
        print(
            f'MODEL CREDENTIAL CONFIGURATION ERROR: {exc} Refusing to start.',
            file=sys.stderr,
        )
        raise SystemExit(5) from None
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
