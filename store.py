"""SQLite storage with transactional, session-scoped daily awards."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .core import award_points, date_multiplier, in_window, local_now, render_commands, validate_settings


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS session_settings (
                    session TEXT PRIMARY KEY, settings TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS balances (
                    session TEXT NOT NULL, user_id TEXT NOT NULL, total INTEGER NOT NULL,
                    PRIMARY KEY (session, user_id)
                );
                CREATE TABLE IF NOT EXISTS checkins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session TEXT NOT NULL, user_id TEXT NOT NULL, player TEXT NOT NULL,
                    day TEXT NOT NULL, points INTEGER NOT NULL, total INTEGER NOT NULL,
                    base_points INTEGER NOT NULL, multiplier TEXT NOT NULL,
                    mc_state TEXT NOT NULL, commands TEXT NOT NULL, server_name TEXT NOT NULL,
                    mc_detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (session, user_id, day)
                );
                CREATE INDEX IF NOT EXISTS idx_checkins_session_id ON checkins (session, id DESC);
                CREATE TABLE IF NOT EXISTS bindings (
                    account_key TEXT PRIMARY KEY, player TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            if db.in_transaction:
                db.rollback()
            db.close()

    @staticmethod
    def _stamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _settings(db, session: str) -> dict:
        row = db.execute("SELECT settings FROM session_settings WHERE session=?", (session,)).fetchone()
        return validate_settings(json.loads(row[0]) if row else {})

    @staticmethod
    def _balance(db, session: str, user_id: str) -> int:
        row = db.execute("SELECT total FROM balances WHERE session=? AND user_id=?", (session, user_id)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _record(row) -> dict:
        record = dict(row)
        record["commands"] = json.loads(record["commands"])
        return record

    def get_settings(self, session: str) -> dict:
        with self._connection() as db:
            return self._settings(db, session)

    def save_settings(self, session: str, settings: dict) -> None:
        settings = validate_settings(settings)
        if not isinstance(session, str) or not session.strip():
            raise ValueError("会话 ID 不能为空")
        with self._connection() as db:
            db.execute("INSERT INTO session_settings(session, settings) VALUES (?, ?) "
                       "ON CONFLICT(session) DO UPDATE SET settings=excluded.settings",
                       (session, json.dumps(settings, ensure_ascii=False)))

    def sessions(self) -> list[dict]:
        with self._connection() as db:
            return [{"session": row["session"], "settings": validate_settings(json.loads(row["settings"]))}
                    for row in db.execute("SELECT session, settings FROM session_settings ORDER BY session")]

    def balance(self, session: str, user_id: str) -> int:
        with self._connection() as db:
            return self._balance(db, session, str(user_id))

    def checkin(self, session: str, user_id: str, player: str = "", now: datetime | None = None) -> dict:
        user_id = str(user_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self._settings(db, session)
            current = local_now(settings, now)
            day = current.date().isoformat()
            total = self._balance(db, session, user_id)
            common = {"total": total, "day": day}
            if not settings["enabled"]:
                return {"status": "disabled", **common}
            row = db.execute("SELECT * FROM checkins WHERE session=? AND user_id=? AND day=?",
                             (session, user_id, day)).fetchone()
            if row:
                return {**self._record(row), "status": "duplicate", "total": total}
            if not in_window(settings, current):
                return {"status": "outside_window", **common}
            if settings["mc_enabled"] and settings["require_binding"] and not player:
                return {"status": "unbound", **common}
            base = secrets.randbelow(settings["max_points"] - settings["min_points"] + 1) + settings["min_points"]
            multiplier = date_multiplier(settings, current.date())
            points = award_points(base, multiplier)
            total += points
            commands = render_commands(settings, player, points, user_id) if settings["mc_enabled"] and player else []
            state = "pending" if commands else "skipped"
            stamp = self._stamp()
            cursor = db.execute("""INSERT INTO checkins
                (session, user_id, player, day, points, total, base_points, multiplier,
                 mc_state, commands, server_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session, user_id, player, day, points, total, base, str(multiplier), state,
                 json.dumps(commands, ensure_ascii=False), settings["server_name"], stamp, stamp))
            db.execute("INSERT INTO balances(session, user_id, total) VALUES (?, ?, ?) "
                       "ON CONFLICT(session, user_id) DO UPDATE SET total=excluded.total",
                       (session, user_id, total))
            record = self._record(db.execute("SELECT * FROM checkins WHERE id=?", (cursor.lastrowid,)).fetchone())
            db.commit()
            return {"status": "success", **record}

    def history(self, session: str, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._connection() as db:
            return [self._record(row) for row in db.execute(
                "SELECT * FROM checkins WHERE session=? ORDER BY id DESC LIMIT ?", (session, limit))]

    def claim_mc(self, checkin_id: int) -> bool:
        with self._connection() as db:
            result = db.execute("UPDATE checkins SET mc_state='sending', updated_at=? WHERE id=? AND mc_state='pending'",
                                (self._stamp(), checkin_id))
            return result.rowcount == 1

    def pending_mc(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._connection() as db:
            return [self._record(row) for row in db.execute(
                "SELECT * FROM checkins WHERE mc_state='pending' ORDER BY id LIMIT ?", (limit,))]

    def set_mc_state(self, checkin_id: int, state: str, detail: str = "") -> None:
        if state not in {"sent", "failed", "unknown", "skipped"}:
            raise ValueError("无效的 MC 执行结果状态")
        with self._connection() as db:
            # Only the caller who claimed the pending record may finalize it.
            db.execute("UPDATE checkins SET mc_state=?, mc_detail=?, updated_at=? WHERE id=? AND mc_state='sending'",
                       (state, str(detail)[:10000], self._stamp(), checkin_id))

    def recover_inflight(self) -> int:
        with self._connection() as db:
            result = db.execute("UPDATE checkins SET mc_state='unknown', mc_detail=?, updated_at=? WHERE mc_state='sending'",
                                ("进程在 MC 发送期间重启，执行结果未知，不自动重试", self._stamp()))
            return result.rowcount

    def sync_bindings(self, bindings: dict[str, str]) -> int:
        if not isinstance(bindings, dict) or any(not isinstance(key, str) or not isinstance(value, str)
                                                for key, value in bindings.items()):
            raise ValueError("绑定快照必须为账号到游戏名的字典")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM bindings")
            db.executemany("INSERT INTO bindings(account_key, player) VALUES (?, ?)", bindings.items())
            db.commit()
        return len(bindings)

    def get_binding(self, key: str) -> str:
        with self._connection() as db:
            row = db.execute("SELECT player FROM bindings WHERE account_key=?", (key,)).fetchone()
            return row[0] if row else ""
