#!/usr/bin/env python3
"""Create beta keys and add them to data/state/auth.json

Usage examples:
  python scripts/create_beta_keys.py --count 5 --duration-days 30 --max-uses 10 --label "beta-june"
  python scripts/create_beta_keys.py --count 1 --expires-at 2026-07-01T00:00:00Z --type infinity
"""
from __future__ import annotations
import argparse
import time
import secrets
import json
from pathlib import Path
from datetime import datetime, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_PATH = PROJECT_ROOT / "data" / "state" / "auth.json"


def parse_time(s: str) -> int:
    # Accept ISO8601-ish strings or integer timestamps
    try:
        return int(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            raise argparse.ArgumentTypeError("Invalid time format. Use unix timestamp or ISO datetime")


def make_key(length: int = 24) -> str:
    # url-safe token
    return secrets.token_urlsafe(length)[:length]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--duration-days", type=int, default=30, help="If set, will compute expires_at as now + duration")
    p.add_argument("--expires-at", type=parse_time, help="Unix ts or ISO datetime for explicit expiry")
    p.add_argument("--max-uses", type=int, default=0, help="0 = unlimited uses")
    p.add_argument("--label", type=str, default="", help="Optional label for the keys")
    p.add_argument("--type", type=str, choices=["user", "infinity"], default="user")
    args = p.parse_args()

    now = int(time.time())
    if args.type == 'infinity':
        # Infinity keys must not expire and are unlimited by definition
        expires = 0
        args.max_uses = 0
        if args.expires_at or args.duration_days:
            print("Warning: 'infinity' keys cannot have expiry or limited uses; ignoring --expires-at/--duration-days/--max-uses")
    else:
        if args.expires_at:
            expires = int(args.expires_at)
        else:
            expires = now + int(args.duration_days) * 86400 if args.duration_days else 0

    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    if AUTH_PATH.exists():
        data = json.loads(AUTH_PATH.read_text(encoding="utf-8")) or {}
    else:
        data = {}

    keys = data.setdefault('keys', {})

    created = []
    for _ in range(max(1, args.count)):
        k = make_key(32)
        # collision check
        while k in keys:
            k = make_key(32)
        meta = {
            'type': args.type,
            'created_at': now,
            'expires_at': expires,
            'max_uses': int(args.max_uses) if args.max_uses is not None else 0,
            'uses': 0,
            'label': args.label or ''
        }
        keys[k] = meta
        created.append((k, meta))

    with open(AUTH_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    for k, m in created:
        exp = m['expires_at']
        exp_str = datetime.fromtimestamp(exp, tz=timezone.utc).isoformat() if exp else 'never'
        print(f"KEY: {k}\n  type: {m['type']}  expires_at: {exp_str}  max_uses: {m['max_uses']}  label: {m['label']}\n")


if __name__ == '__main__':
    main()
