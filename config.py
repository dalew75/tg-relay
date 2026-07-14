from decouple import config

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