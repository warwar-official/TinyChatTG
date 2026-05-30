import json
import time
from pathlib import Path
from typing import Any, Dict


class AuthStore:
    """Persistent auth and ban store saved as JSON under data/state/auth.json"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save({})

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save(self, data: Dict[str, Any]):
        # ensure parent dir exists (may have been removed externally)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self.path)

    def _get_user(self, user_id: int) -> Dict[str, Any]:
        data = self._load()
        users = data.setdefault('users', {})
        u = users.get(str(user_id))
        if not u:
            u = {
                'authorized': False,
                'current_code': None,
                'code_expires': 0,
                'code_generated_at': 0,
                'code_failures': [],
                'start_attempts': [],
                'bans': {
                    'start_ban_until': 0,
                    'code_ban_until': 0,
                    'message_ban_until': 0
                },
                'message_timestamps': []
            }
            users[str(user_id)] = u
            self._save(data)
        return u

    def is_authorized(self, user_id: int) -> bool:
        u = self._get_user(user_id)
        return bool(u.get('authorized', False))

    def generate_code(self, user_id: int, ttl: int = 60) -> str:
        now = time.time()
        u = self._get_user(user_id)
        bans = u.get('bans', {}) or {}
        if now < bans.get('start_ban_until', 0):
            raise PermissionError('start_banned')
        if now < bans.get('code_ban_until', 0):
            raise PermissionError('code_banned')

        last_gen = u.get('code_generated_at', 0) or 0
        if now - last_gen < 60:
            raise PermissionError('code_rate_limited')

        import random
        code = f"{random.randint(100000, 999999)}"
        u['current_code'] = code
        u['code_expires'] = now + ttl
        u['code_generated_at'] = now
        # reset recent failures
        u['code_failures'] = []
        data = self._load()
        data.setdefault('users', {})[str(user_id)] = u
        self._save(data)
        return code

    def verify_code(self, user_id: int, code: str, max_failures: int = 5, fail_window: int = 60) -> bool:
        now = time.time()
        u = self._get_user(user_id)
        expected = u.get('current_code')
        expires = u.get('code_expires', 0)
        if expected and code and str(code).strip() == str(expected) and now <= expires:
            u['authorized'] = True
            u['current_code'] = None
            u['code_expires'] = 0
            u['code_failures'] = []
            data = self._load()
            data.setdefault('users', {})[str(user_id)] = u
            self._save(data)
            return True

        # failure
        failures = u.get('code_failures') or []
        failures = [t for t in failures if now - t <= fail_window]
        failures.append(now)
        u['code_failures'] = failures
        # if too many failures within window, set code_ban
        if len(failures) >= max_failures:
            u.setdefault('bans', {})['code_ban_until'] = now + fail_window

        data = self._load()
        data.setdefault('users', {})[str(user_id)] = u
        self._save(data)
        return False

    def add_start_attempt(self, user_id: int, window: int = 60, limit: int = 5, ban_seconds: int = 3600) -> None:
        now = time.time()
        u = self._get_user(user_id)
        attempts = u.get('start_attempts') or []
        attempts = [t for t in attempts if now - t <= window]
        attempts.append(now)
        u['start_attempts'] = attempts
        if len(attempts) >= limit:
            u.setdefault('bans', {})['start_ban_until'] = now + ban_seconds
        data = self._load()
        data.setdefault('users', {})[str(user_id)] = u
        self._save(data)

    def get_bans(self, user_id: int) -> Dict[str, float]:
        u = self._get_user(user_id)
        return u.get('bans', {}) or {}

    def is_start_banned(self, user_id: int) -> bool:
        now = time.time()
        bans = self.get_bans(user_id)
        return now < bans.get('start_ban_until', 0)

    def is_code_banned(self, user_id: int) -> bool:
        now = time.time()
        bans = self.get_bans(user_id)
        return now < bans.get('code_ban_until', 0)

    def record_message(self, user_id: int, per_minute_limit: int = 60, ban_seconds: int = 300) -> Dict[str, Any]:
        """Record a message timestamp and enforce rate limits. Returns {'banned': bool, 'reason': str or None}"""
        now = time.time()
        u = self._get_user(user_id)
        if not u.get('authorized'):
            return {'banned': False, 'reason': None}

        bans = u.get('bans', {}) or {}
        if now < bans.get('message_ban_until', 0):
            return {'banned': True, 'reason': 'already_banned'}

        mts = u.get('message_timestamps') or []
        mts = [t for t in mts if now - t <= 60]
        mts.append(now)
        u['message_timestamps'] = mts

        # check per-minute
        if len(mts) > per_minute_limit:
            u.setdefault('bans', {})['message_ban_until'] = now + ban_seconds
            data = self._load()
            data.setdefault('users', {})[str(user_id)] = u
            self._save(data)
            return {'banned': True, 'reason': 'rate'}

        data = self._load()
        data.setdefault('users', {})[str(user_id)] = u
        self._save(data)
        return {'banned': False, 'reason': None}

    def is_message_banned(self, user_id: int) -> bool:
        now = time.time()
        bans = self.get_bans(user_id)
        return now < bans.get('message_ban_until', 0)
