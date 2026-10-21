import os
from pathlib import Path

from decouple import config


def _load_nats_credentials():
    """Fill NATS_USER / NATS_PASS from the shared credentials file (unified-messaging's
    NATS_CREDENTIALS_FILE, default ~/.config/nats/credentials.env). Real env vars win, and decouple
    reads os.environ before .env. Values are never logged."""
    path = Path(os.path.expanduser(os.environ.get('NATS_CREDENTIALS_FILE', '').strip() or '~/.config/nats/credentials.env'))
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key and not key.startswith('#'):
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


_load_nats_credentials()

API_ID = config('API_ID')
API_HASH = config('API_HASH')
FORWARD = config('FORWARD', cast=bool, default=True)
RELAY_MAP = config('RELAY_MAP')
SESSION_NAME = config('SESSION_NAME', default='session')
NATS_SERVER = config('NATS_SERVER')
NATS_SUBJECT = config('NATS_SUBJECT', default='telegram-messages')
NATS_USER = config('NATS_USER', default='')
NATS_PASS = config('NATS_PASS', default='')
ALLOWLIST_PATH = config('ALLOWLIST_PATH', default='data/allowlist.json')
MGMT_GROUP = config('MGMT_GROUP', default='')
MGMT_NOTIFY = config('MGMT_NOTIFY', cast=bool, default=False)
RELAY_OUTGOING = config('RELAY_OUTGOING', cast=bool, default=False)
SOURCES_SUBJECT = config('SOURCES_SUBJECT', default='tg-relay.sources')
SOURCES_EVENT_SUBJECT = config('SOURCES_EVENT_SUBJECT', default='tg-relay.sources.event')
