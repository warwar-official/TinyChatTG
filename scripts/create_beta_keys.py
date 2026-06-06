# TinyChat (c) 2026 WarWar <somethingstrenge@gmail.com>
# This file is part of TinyChat.
# TinyChat is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License v3.

"""Create beta keys and add them to data/state/auth.json

Usage examples:
  python scripts/create_beta_keys.py --count 5 --duration-days 30 --max-uses 10 --label "beta-june"
  python scripts/create_beta_keys.py --count 1 --type infinity --label "infinity-key"
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from imports.auth.store import AuthStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AUTH_PATH = PROJECT_ROOT / "data" / "state" / "auth.json"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--duration-days", type=int, default=30, help="If set, will compute expires_at as now + duration")
    p.add_argument("--max-uses", type=int, default=0, help="0 = unlimited uses")
    p.add_argument("--label", type=str, default="", help="Optional label for the keys")
    p.add_argument("--type", type=str, choices=["user", "infinity"], default="user")
    args = p.parse_args()

    auth_store = AuthStore(AUTH_PATH)
    
    if args.duration_days and args.type == 'infinity':
        print("Warning: duration_days is ignored when type is infinity.")
    if args.max_uses and args.type == 'infinity':
        print("Warning: max_uses is ignored when type is infinity.")

    for _ in range(max(1, args.count)):
        auth_store.generate_code(type=args.type, ttl=args.duration_days * 24 * 3600, max_uses=args.max_uses, label=args.label)
        if args.type == 'infinity':
            print(f"Created infinity key with label='{args.label}'")
        else:
            print(f"Created key with type={args.type}, duration_days={args.duration_days}, max_uses={args.max_uses}, label={args.label}")


if __name__ == '__main__':
    main()
