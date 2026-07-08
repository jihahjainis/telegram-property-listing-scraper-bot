from pathlib import Path
import os


ENV_FILE = Path(__file__).with_name('.env')


def load_env_file(path=ENV_FILE):
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith('#') or '=' not in line:
            continue

        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip()

        if value and value[0] in ('"', "'") and value[-1] == value[0]:
            value = value[1:-1]

        os.environ.setdefault(key, value)


def get_telegram_config():
    load_env_file()

    required_vars = ('TELEGRAM_API_ID', 'TELEGRAM_API_HASH')
    missing_vars = [name for name in required_vars if not os.environ.get(name)]

    if missing_vars:
        missing = ', '.join(missing_vars)
        raise RuntimeError(f'Missing required environment variable(s): {missing}')

    try:
        api_id = int(os.environ['TELEGRAM_API_ID'])
    except ValueError as error:
        raise RuntimeError('TELEGRAM_API_ID must be a number') from error

    api_hash = os.environ['TELEGRAM_API_HASH']
    session = os.environ.get('TELEGRAM_SESSION', str(Path.home() / 'property_session'))

    return api_id, api_hash, session


def get_bot_config():
    load_env_file()

    required_vars = ('TELEGRAM_BOT_TOKEN', 'TELEGRAM_ALLOWED_USERS')
    missing_vars = [name for name in required_vars if not os.environ.get(name)]

    if missing_vars:
        missing = ', '.join(missing_vars)
        raise RuntimeError(f'Missing required environment variable(s): {missing}')

    bot_token = os.environ['TELEGRAM_BOT_TOKEN']

    try:
        allowed_user_ids = {
            int(user_id.strip())
            for user_id in os.environ['TELEGRAM_ALLOWED_USERS'].split(',')
            if user_id.strip()
        }
    except ValueError as error:
        raise RuntimeError('TELEGRAM_ALLOWED_USERS must be a comma-separated list of numeric user IDs') from error

    if not allowed_user_ids:
        raise RuntimeError('TELEGRAM_ALLOWED_USERS must contain at least one numeric user ID')

    return bot_token, allowed_user_ids


def get_therooma_config():
    load_env_file()

    api_url = os.environ.get('THEROOMA_API_URL', '').strip()
    api_key = os.environ.get('THEROOMA_API_KEY', '').strip()

    return api_url, api_key
