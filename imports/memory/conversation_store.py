"""Persistent conversation store backed by SQLite."""
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class ConversationStore:
    def __init__(self, path: str | Path = None):
        self.path = Path(path or Path(__file__).resolve().parents[2] / 'data' / 'state' / 'conversations.db')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    ts REAL NOT NULL,
                    meta TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_user
                ON messages(user_id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS summaries (
                    user_id INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                )
            """)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection with WAL mode for better concurrent access."""
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def append_message(self, user_id: int, role: str, text: str, meta: Dict[str, Any] | None = None) -> int:
        """Append a message to the user's conversation history and return its ID."""
        ts = time.time()
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO messages (user_id, role, text, ts, meta) VALUES (?, ?, ?, ?, ?)",
                (int(user_id), role, text, ts, meta_json),
            )
            inserted_id = cursor.lastrowid
            conn.commit()
            return inserted_id

    def delete_since_id(self, user_id: int, start_id: int) -> None:
        """Remove all messages for a user starting from a specific ID (inclusive)."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM messages WHERE user_id = ? AND id >= ?",
                (int(user_id), int(start_id))
            )
            conn.commit()

    def get_history(self, user_id: int) -> List[Dict[str, Any]]:
        """Get the full conversation history for a user, ordered by insertion order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT role, text, ts, meta FROM messages WHERE user_id = ? ORDER BY id ASC",
                (int(user_id),),
            ).fetchall()
        result = []
        for row in rows:
            entry: Dict[str, Any] = {
                'role': row['role'],
                'text': row['text'],
                'ts': row['ts'],
            }
            if row['meta']:
                try:
                    entry['meta'] = json.loads(row['meta'])
                except Exception:
                    entry['meta'] = row['meta']
            result.append(entry)
        return result

    def get_history_count(self, user_id: int) -> int:
        """Get the number of messages in a user's conversation history."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM messages WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        return row['cnt'] if row else 0

    def set_history(self, user_id: int, messages: List[Dict[str, Any]]) -> None:
        """Replace the user's entire conversation history (used after summarization)."""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE user_id = ?", (int(user_id),))
            for msg in messages:
                ts = msg.get('ts', time.time())
                meta = msg.get('meta')
                meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
                conn.execute(
                    "INSERT INTO messages (user_id, role, text, ts, meta) VALUES (?, ?, ?, ?, ?)",
                    (int(user_id), msg.get('role', 'user'), msg.get('text', ''), ts, meta_json),
                )
            conn.commit()

    def clear_history(self, user_id: int) -> None:
        """Delete all messages for a user."""
        with self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE user_id = ?", (int(user_id),))
            conn.commit()

    def set_summary(self, user_id: int, summary: str) -> None:
        """Set or update the conversation summary for a user."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO summaries (user_id, summary, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET summary = excluded.summary, updated_at = excluded.updated_at""",
                (int(user_id), summary, now),
            )
            conn.commit()

    def get_summary(self, user_id: int) -> str:
        """Get the conversation summary for a user."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT summary FROM summaries WHERE user_id = ?",
                (int(user_id),),
            ).fetchone()
        return row['summary'] if row else ''
