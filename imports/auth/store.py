# TinyChat (c) 2026 WarWar <somethingstrenge@gmail.com>
# This file is part of TinyChat.
# TinyChat is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License v3.

import json
import time
import secrets
from pathlib import Path
from typing import Any, Dict
import threading
import fcntl
import os
from contextlib import contextmanager


class AuthStore:
    """Persistent auth and ban store saved as JSON under data/state/auth.json

    This store uses an in-process `threading.RLock` and a POSIX advisory lock
    (via `fcntl.flock`) on a companion `.lock` file to make read-modify-write
    sequences atomic across threads and processes. Writes are performed to a
    temporary file and fsynced before an atomic replace.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Protect in-process access
        self._thread_lock = threading.RLock()
        if not self.path.exists():
            self._save({})

    @contextmanager
    def _file_lock(self, exclusive: bool = True, timeout: float = 5.0):
        """Context manager for a POSIX advisory lock on a companion .lock file.

        Uses `fcntl.flock` on a lock file next to the target file. This will
        attempt a non-blocking lock and retry until timeout.
        """
        lock_path = self.path.with_name(self.path.name + '.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(lock_path, 'a+')
        start = time.time()
        flags = (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else (fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            while True:
                try:
                    fcntl.flock(fd.fileno(), flags)
                    break
                except BlockingIOError:
                    if timeout is not None and (time.time() - start) >= timeout:
                        fd.close()
                        raise TimeoutError(f"Timeout acquiring lock for {lock_path}")
                    time.sleep(0.05)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
        finally:
            fd.close()

    @contextmanager
    def _acquire_locks(self, exclusive: bool = True, timeout: float = 5.0):
        """Acquire both an in-process lock and a filesystem lock.

        Use this to make read-modify-write sequences atomic across threads and
        processes.
        """
        with self._thread_lock:
            with self._file_lock(exclusive=exclusive, timeout=timeout):
                yield

    def _read_raw(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        with open(self.path, 'r', encoding='utf-8') as f:
            return json.load(f) or {}

    def _write_raw(self, data: Dict[str, Any]):
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)

    def _load(self) -> Dict[str, Any]:
        # Acquire a shared lock while reading
        try:
            with self._acquire_locks(exclusive=False):
                return self._read_raw()
        except Exception:
            return {}

    def _save(self, data: Dict[str, Any]):
        # ensure parent dir exists (may have been removed externally)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use exclusive lock for the whole write operation
        with self._acquire_locks(exclusive=True):
            self._write_raw(data)

    def _get_user(self, user_id: int) -> Dict[str, Any]:
        with self._acquire_locks(exclusive=True):
            data = self._read_raw()
            users = data.setdefault('users', {})
            u = users.get(str(user_id))
            if not u:
                u = {
                    'authorized': False,
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
                self._write_raw(data)
            return u

    def _generate_random_code(self, length: int = 24) -> str:
        return secrets.token_urlsafe(length)[:length]

    def is_authorized(self, user_id: int) -> bool:
        u = self._get_user(user_id)
        now = time.time()
        # Persistent access wins: infinity or unexpired user access
        access_type = u.get('access_type')
        access_expires = u.get('access_expires', 0) or 0
        if access_type == 'infinity':
            return True
        if access_type == 'user' and access_expires and access_expires > now:
            return True
        # Fallback to ephemeral session authorization (generated codes)
        return bool(u.get('authorized', False))

    def add_start_attempt(self, user_id: int, window: int = 60, limit: int = 5, ban_seconds: int = 3600) -> None:
        now = time.time()
        with self._acquire_locks(exclusive=True):
            data = self._read_raw()
            users = data.setdefault('users', {})
            u = users.get(str(user_id))
            if not u:
                u = {
                    'authorized': False,
                    'code_failures': [],
                    'start_attempts': [],
                    'bans': {
                        'start_ban_until': 0,
                        'code_ban_until': 0,
                        'message_ban_until': 0
                    },
                    'message_timestamps': []
                }
            attempts = u.get('start_attempts') or []
            attempts = [t for t in attempts if now - t <= window]
            attempts.append(now)
            u['start_attempts'] = attempts
            if len(attempts) >= limit:
                u.setdefault('bans', {})['start_ban_until'] = now + ban_seconds
            users[str(user_id)] = u
            self._write_raw(data)

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
        with self._acquire_locks(exclusive=True):
            data = self._read_raw()
            users = data.setdefault('users', {})
            u = users.get(str(user_id))
            if not u:
                u = {
                    'authorized': False,
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
                self._write_raw(data)
                if not u.get('authorized'):
                    return {'banned': False, 'reason': None}

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
                users[str(user_id)] = u
                self._write_raw(data)
                return {'banned': True, 'reason': 'rate'}

            users[str(user_id)] = u
            self._write_raw(data)
            return {'banned': False, 'reason': None}

    def redeem_key(self, user_id: int, key: str, max_failures: int = 5, fail_window: int = 60) -> dict:
        """Attempt to redeem a key from the `keys` mapping.

        Returns a dict with at least `ok: bool`. On success returns `type` and `expires_at`.
        Possible failure reasons: 'not_found', 'expired', 'used_up'.
        """
        response = {'ok': False, 'reason': None}

        now = int(time.time())
        with self._acquire_locks(exclusive=True):
            data = self._read_raw()
            keys = data.setdefault('keys', {})
            kmeta = keys.get(key)
            users = data.setdefault('users', {})
            u = users.get(str(user_id))
            if not u:
                u = {
                    'authorized': False,
                    'code_failures': [],
                    'start_attempts': [],
                    'bans': {
                        'start_ban_until': 0,
                        'code_ban_until': 0,
                        'message_ban_until': 0
                    },
                    'message_timestamps': []
                }

            if not kmeta:
                response = {'ok': False, 'reason': 'not_found'}
            else:
                ktype = kmeta.get('type', 'user')
                expires_at = int(kmeta.get('expires_at', 0) or 0)
                max_uses = int(kmeta.get('max_uses', 0) or 0)
                uses = int(kmeta.get('uses', 0) or 0)

                # Non-infinity keys may expire
                if ktype != 'infinity' and expires_at and now > expires_at:
                    response = {'ok': False, 'reason': 'expired'}
                # Check usage limit
                elif max_uses > 0 and uses >= max_uses:
                    response = {'ok': False, 'reason': 'used_up'}
                else:
                    # Apply the key to the user
                    if ktype == 'infinity':
                        u['access_type'] = 'infinity'
                        u['access_expires'] = -1
                    else:
                        u['access_type'] = ktype
                        u['access_expires'] = expires_at
                    # consume key use, for infinity keys this will not remove them but will track usage
                    kmeta['uses'] = uses + 1
                    if max_uses > 0 and kmeta['uses'] >= max_uses:
                        # remove exhausted key
                        del keys[key]
                    else:
                        keys[key] = kmeta

                    u['authorized'] = True
                    u['code_failures'] = []

                    response = {'ok': True, 'type': ktype, 'expires_at': expires_at}

            if not response['ok']:
                # on any failure, record it for the user
                failures = u.get('code_failures') or []
                failures = [t for t in failures if now - t <= fail_window]
                failures.append(now)
                u['code_failures'] = failures
                # if too many failures within window, set code_ban
                if len(failures) >= max_failures:
                    u.setdefault('bans', {})['code_ban_until'] = now + fail_window

            users[str(user_id)] = u
            data['keys'] = keys
            self._write_raw(data)

            return response

    def generate_code(self, type: str = 'user', ttl: int = 3600, max_uses: int = 1, label: str = "") -> str:
        """Generate a new code and save it to the `keys` mapping with metadata."""
        code = self._generate_random_code()
        if type == 'infinity':
            expires_at = 0
            max_uses = 0
            label = label or "infinity"
        else:
            expires_at = int(time.time()) + ttl
            label = label or f"{type}_{expires_at}"
        with self._acquire_locks(exclusive=True):
            data = self._read_raw()
            keys = data.setdefault('keys', {})
            keys[code] = {
                'type': type,
                'expires_at': expires_at,
                'max_uses': max_uses,
                'uses': 0,
                'label': label
            }
            self._write_raw(data)
        return code