"""SQLite storage with transactional, session-scoped daily awards."""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import DEFAULT_TEMPLATES, award_points, date_multiplier, in_window, local_now, render_commands, validate_day, validate_settings

MAX_BALANCE = 2**63 - 1
LEGACY_USAGE = "请按本会话的唤醒头和插件前缀使用：签到、积分、查询 [玩家]、修改 玩家 数量、增加 玩家 数量、减少 玩家 数量、排行榜、商店列表、商店 购买 编号、兑换、补领。"
V3_USAGE = "签到、点数及签到排行榜仅需 AstrBot 唤醒头；其余命令需唤醒头和本会话插件前缀。可用命令：{command_list}"
LEGACY_TEMPLATES = {
    "no_permission": "你没有本会话的积分管理权限。",
    "usage": LEGACY_USAGE,
    "purchase_pending": "是否消耗 {price}{currency_name}，来购买 {product_id} {product_name}？请在 {minutes} 分钟内回复“兑换”。",
    "purchase_failed": "订单 {order_id} 的私聊发送失败，已扣款且奖励已保留。请在原会话回复“兑换”，或使用唤醒头和插件前缀发送“补领”，不会重复扣款。",
}

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
                CREATE TABLE IF NOT EXISTS checkin_resets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_id INTEGER NOT NULL UNIQUE, session TEXT NOT NULL,
                    user_id TEXT NOT NULL, record TEXT NOT NULL,
                    actor TEXT NOT NULL, reset_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_checkin_resets_session ON checkin_resets(session, original_id DESC);
                CREATE TABLE IF NOT EXISTS bindings (
                    account_key TEXT PRIMARY KEY, player TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS balance_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                    user_id TEXT NOT NULL, operation TEXT NOT NULL, old_total INTEGER NOT NULL,
                    delta INTEGER NOT NULL, total INTEGER NOT NULL, actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                    user_id TEXT NOT NULL, product_id TEXT NOT NULL, product_name TEXT NOT NULL,
                    price INTEGER NOT NULL, currency_name TEXT NOT NULL,
                    minutes INTEGER NOT NULL, status TEXT NOT NULL, product_snapshot TEXT NOT NULL,
                    reward TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
                    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(session, user_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_purchases_limit ON purchases(session, user_id, product_id, status);
                CREATE TABLE IF NOT EXISTS used_codes (
                    session TEXT NOT NULL, product_id TEXT NOT NULL, code TEXT NOT NULL,
                    order_id INTEGER NOT NULL,
                    PRIMARY KEY(session, product_id, code)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            columns = {row["name"] for row in db.execute("PRAGMA table_info(checkins)")}
            if "credited" not in columns:
                # Version 1 awarded every stored check-in immediately. Keep those assets
                # and mark them credited so old pending tasks can never award twice.
                db.execute("ALTER TABLE checkins ADD COLUMN credited INTEGER NOT NULL DEFAULT 1")
            if version < 3:
                for row in db.execute("SELECT session,settings FROM session_settings").fetchall():
                    settings = json.loads(row["settings"])
                    if settings.get("currency_name", "积分") == "积分":
                        settings["currency_name"] = "点数"
                    templates = settings.get("templates", {})
                    for key, old_default in LEGACY_TEMPLATES.items():
                        if templates.get(key) == old_default:
                            templates[key] = DEFAULT_TEMPLATES[key]
                    settings = self._clean_code_pool(db, row["session"], validate_settings(settings))
                    db.execute("UPDATE session_settings SET settings=? WHERE session=?",
                               (json.dumps(settings, ensure_ascii=False), row["session"]))
                db.execute("PRAGMA user_version=3")
            if version < 4:
                db.execute("PRAGMA user_version=4")
            if version < 5:
                for row in db.execute("SELECT session,settings FROM session_settings").fetchall():
                    settings = json.loads(row["settings"])
                    settings.setdefault("checkin_prefix", "")
                    templates = settings.get("templates", {})
                    if templates.get("usage") == V3_USAGE:
                        templates["usage"] = DEFAULT_TEMPLATES["usage"]
                    db.execute("UPDATE session_settings SET settings=? WHERE session=?",
                               (json.dumps(settings, ensure_ascii=False), row["session"]))
                db.execute("PRAGMA user_version=5")
            db.commit()

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
        return Store._clean_code_pool(db, session, validate_settings(json.loads(row[0]) if row else {}))

    @staticmethod
    def _clean_code_pool(db, session: str, settings: dict) -> dict:
        used = {}
        for row in db.execute("SELECT product_id,code FROM used_codes WHERE session=?", (session,)):
            used.setdefault(row["product_id"], set()).add(row["code"])
        for product in settings["products"]:
            allocated = used.get(product["id"], set())
            product["codes"] = [code for code in product["codes"] if code not in allocated]
        return settings

    @staticmethod
    def _balance(db, session: str, user_id: str) -> int:
        row = db.execute("SELECT total FROM balances WHERE session=? AND user_id=?", (session, user_id)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _record(row) -> dict:
        record = dict(row)
        record["commands"] = json.loads(record["commands"])
        record["credited"] = bool(record["credited"])
        return record

    @staticmethod
    def _set_balance(db, session: str, user_id: str, total: int) -> None:
        if not 0 <= total <= MAX_BALANCE:
            raise ValueError("余额不足或超出 SQLite 整数上限")
        db.execute("INSERT INTO balances(session, user_id, total) VALUES (?, ?, ?) "
                   "ON CONFLICT(session, user_id) DO UPDATE SET total=excluded.total", (session, user_id, total))

    def _audit(self, db, session, user_id, operation, old, total, actor):
        db.execute("INSERT INTO balance_audit(session,user_id,operation,old_total,delta,total,actor,created_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (session, user_id, operation, old, total-old, total, actor, self._stamp()))

    def get_settings(self, session: str) -> dict:
        with self._connection() as db:
            return self._settings(db, session)

    def save_settings(self, session: str, settings: dict) -> dict:
        settings = validate_settings(settings)
        if not isinstance(session, str) or not session.strip():
            raise ValueError("会话 ID 不能为空")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self._clean_code_pool(db, session, settings)
            db.execute("INSERT INTO session_settings(session, settings) VALUES (?, ?) "
                       "ON CONFLICT(session) DO UPDATE SET settings=excluded.settings",
                       (session, json.dumps(settings, ensure_ascii=False)))
            if not settings["enabled"] or not settings["mc_enabled"]:
                self._cancel_mc(db, session, "会话或 MC 联动已停用，未完成入账已取消；已发往 MC 的指令仍需自行核实")
            db.commit()
            return settings

    def _cancel_mc(self, db, session: str, reason: str) -> None:
        db.execute("UPDATE checkins SET mc_state='skipped',mc_detail=?,updated_at=? "
                   "WHERE session=? AND mc_state IN ('pending','sending')", (reason, self._stamp(), session))

    def sessions(self) -> list[dict]:
        with self._connection() as db:
            return [{"session": row["session"], "settings": self._clean_code_pool(db, row["session"], validate_settings(json.loads(row["settings"])))}
                    for row in db.execute("SELECT session, settings FROM session_settings ORDER BY session")]

    def balance(self, session: str, user_id: str) -> int:
        with self._connection() as db:
            return self._balance(db, session, str(user_id))

    def checkin(self, session: str, user_id: str, player: str = "", now: datetime | None = None) -> dict:
        return self._checkin(session, user_id, player, now=now)

    def makeup_checkin(self, session: str, user_id: str, player: str, day: str, actor: str) -> dict:
        validate_day(day)
        return self._checkin(session, user_id, player, makeup_day=day, actor=actor)

    def _checkin(self, session: str, user_id: str, player: str = "", now: datetime | None = None,
                 makeup_day: str | None = None, actor: str = "") -> dict:
        user_id = str(user_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self._settings(db, session)
            current = local_now(settings, now)
            target_date = validate_day(makeup_day) if makeup_day is not None else current.date()
            if target_date > current.date():
                raise ValueError("补签日期不能晚于本会话时区的今天")
            day = target_date.isoformat()
            total = self._balance(db, session, user_id)
            common = {"total": total, "day": day}
            if not settings["enabled"]:
                return {"status": "disabled", **common}
            if makeup_day is not None:
                if not settings["mc_enabled"]:
                    raise ValueError("补签需要开启 MC 联动；未执行游戏补签，也未增加点数")
                if not isinstance(player, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player):
                    return {"status": "unbound", **common}
            row = db.execute("SELECT * FROM checkins WHERE session=? AND user_id=? AND day=?",
                             (session, user_id, day)).fetchone()
            if row:
                return {**self._record(row), "status": "duplicate", "total": total}
            if makeup_day is None and not in_window(settings, current):
                return {"status": "outside_window", **common}
            if settings["mc_enabled"] and settings["require_binding"] and not player:
                return {"status": "unbound", **common}
            base = secrets.randbelow(settings["max_points"] - settings["min_points"] + 1) + settings["min_points"]
            multiplier = date_multiplier(settings, target_date)
            points = award_points(base, multiplier)
            credited = not settings["mc_enabled"]
            if credited:
                total += points
                if total > MAX_BALANCE:
                    raise ValueError("签到后余额超出 SQLite 整数上限，未入账")
            command_settings = settings
            if makeup_day is not None and settings["mc_enabled"]:
                if not settings["makeup_commands"]:
                    raise ValueError("本会话尚未配置 MC 补签指令")
                command_settings = {**settings, "commands": settings["makeup_commands"]}
            commands = render_commands(command_settings, player, points, user_id, date=day) if settings["mc_enabled"] and player else []
            state = "pending" if commands else "skipped"
            stamp = self._stamp()
            cursor = db.execute("""INSERT INTO checkins
                (session, user_id, player, day, points, total, base_points, multiplier,
                 mc_state, commands, server_name, created_at, updated_at, credited)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session, user_id, player, day, points, total, base, str(multiplier), state,
                 json.dumps(commands, ensure_ascii=False), settings["server_name"], stamp, stamp, int(credited)))
            if makeup_day is not None:
                self._audit(db, session, user_id, "makeup_requested", common["total"], common["total"], str(actor))
            if credited:
                self._set_balance(db, session, user_id, total)
                self._audit(db, session, user_id, "checkin", total-points, total, str(cursor.lastrowid))
            record = self._record(db.execute("SELECT * FROM checkins WHERE id=?", (cursor.lastrowid,)).fetchone())
            db.commit()
            return {"status": "success", **record}

    def history(self, session: str, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        with self._connection() as db:
            records = [self._record(row) for row in db.execute(
                "SELECT * FROM checkins WHERE session=? ORDER BY id DESC LIMIT ?", (session, limit))]
            for row in db.execute("SELECT * FROM checkin_resets WHERE session=? ORDER BY original_id DESC LIMIT ?", (session, limit)):
                records.append({**json.loads(row["record"]), "reset_at": row["reset_at"], "actor": row["actor"]})
            return sorted(records, key=lambda record: record["id"], reverse=True)[:limit]

    def reset_checkin(self, session: str, user_id: str, actor: str, now: datetime | None = None) -> dict:
        user_id = str(user_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self._settings(db, session)
            day = local_now(settings, now).date().isoformat()
            total = self._balance(db, session, user_id)
            row = db.execute("SELECT * FROM checkins WHERE session=? AND user_id=? AND day=?", (session, user_id, day)).fetchone()
            if row is None:
                return dict(day=day, total=total, removed=False)
            if row["mc_state"] == "sending":
                raise ValueError("该玩家的 MC 签到正在执行，无法重置，请等待执行结束后核实结果")
            stamp = self._stamp()
            db.execute("INSERT INTO checkin_resets(original_id,session,user_id,record,actor,reset_at) VALUES(?,?,?,?,?,?)",
                       (row["id"], session, user_id, json.dumps(self._record(row), ensure_ascii=False), str(actor), stamp))
            db.execute("DELETE FROM checkins WHERE id=?", (row["id"],))
            self._audit(db, session, user_id, "checkin_reset", total, total, str(actor))
            db.commit()
            return dict(day=day, total=total, removed=True, id=row["id"])

    def get_checkin(self, checkin_id: int) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM checkins WHERE id=?", (checkin_id,)).fetchone()
            return self._record(row) if row else None

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
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM checkins WHERE id=? AND mc_state='sending'", (checkin_id,)).fetchone()
            if not row:
                return
            total, credited = self._balance(db, row["session"], row["user_id"]), row["credited"]
            if state == "sent" and not credited:
                settings = self._settings(db, row["session"])
                if not settings["enabled"] or not settings["mc_enabled"]:
                    state, detail = "skipped", "MC 返回时会话已停用或删除，未入账"
                elif total + row["points"] > MAX_BALANCE:
                    state, detail = "failed", "MC 已成功，但余额超出整数上限，未入账，请管理员处理"
                else:
                    old = total
                    total += row["points"]
                    self._set_balance(db, row["session"], row["user_id"], total)
                    self._audit(db, row["session"], row["user_id"], "checkin_mc", old, total, str(checkin_id))
                    credited = 1
            db.execute("UPDATE checkins SET mc_state=?, mc_detail=?, updated_at=?, total=?, credited=? WHERE id=?",
                       (state, str(detail)[:10000], self._stamp(), total, credited, checkin_id))
            db.commit()

    def recover_inflight(self) -> int:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute("UPDATE checkins SET mc_state='unknown', mc_detail=?, updated_at=? WHERE mc_state='sending'",
                                ("进程在 MC 发送期间重启，执行结果未知，不自动重试", self._stamp()))
            db.execute("UPDATE purchases SET status='delivery_failed',detail=?,updated_at=? WHERE status='sending'",
                       ("发送进程中断，可在原会话回复“兑换”以重发同一奖励，不重复扣款", self._stamp()))
            db.commit()
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

    def lookup_users(self, platform: str, player: str) -> list[str]:
        prefix = f"{platform}:User:"
        with self._connection() as db:
            return [row["account_key"][len(prefix):] for row in db.execute(
                "SELECT account_key FROM bindings WHERE player=? COLLATE NOCASE ORDER BY account_key", (player,))
                if row["account_key"].startswith(prefix)]

    def delete_session(self, session: str) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM session_settings WHERE session=?", (session,))
            db.execute("UPDATE purchases SET status='cancelled',detail=?,updated_at=? WHERE session=? AND status='pending'",
                       ("会话配置已删除，订单取消，未扣款", self._stamp(), session))
            # Also finalize sending so deleting then recreating the same session
            # cannot authorize credit from a previously cancelled operation.
            db.execute("UPDATE checkins SET mc_state='skipped',mc_detail=?,updated_at=? "
                       "WHERE session=? AND mc_state IN ('pending','sending')",
                       ("会话配置已删除，未完成入账已取消；已发往 MC 的指令仍需自行核实", self._stamp(), session))
            db.commit()

    def toggle_session(self, session: str, enabled: bool) -> None:
        if not isinstance(enabled, bool):
            raise ValueError("启用状态必须为布尔值")
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT settings FROM session_settings WHERE session=?", (session,)).fetchone()
            if not row:
                raise ValueError("会话配置不存在")
            settings = validate_settings({**json.loads(row[0]), "enabled": enabled})
            db.execute("UPDATE session_settings SET settings=? WHERE session=?", (json.dumps(settings, ensure_ascii=False), session))
            if not enabled:
                self._cancel_mc(db, session, "会话已停用，未完成入账已取消；已发往 MC 的指令仍需自行核实")
            db.commit()

    def adjust_balance(self, session: str, user_id: str, operation: str, amount: int, actor: str) -> dict:
        if operation not in {"set", "add", "subtract"}:
            raise ValueError("操作必须为 set、add 或 subtract")
        if type(amount) is not int or not 0 <= amount <= MAX_BALANCE:
            raise ValueError("调整金额必须为不超过 SQLite 整数上限的非负整数")
        user_id = str(user_id)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._balance(db, session, user_id)
            total = amount if operation == "set" else old + amount if operation == "add" else old - amount
            self._set_balance(db, session, user_id, total)
            self._audit(db, session, user_id, operation, old, total, str(actor))
            db.commit()
            return dict(total=total, old=old, delta=total-old, user_id=user_id, target_id=user_id)

    def leaderboard(self, session: str, limit: int = 10) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT user_id,total FROM balances WHERE session=? "
                                                   "ORDER BY total DESC,user_id LIMIT ?", (session, max(1, min(int(limit), 1000))))]

    def audit(self, session: str, limit: int = 100) -> list[dict]:
        with self._connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM balance_audit WHERE session=? ORDER BY id DESC LIMIT ?",
                                                  (session, max(1, min(int(limit), 1000))))]

    @staticmethod
    def _utc(now: datetime | None = None) -> datetime:
        now = now or datetime.now(timezone.utc)
        return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)

    @staticmethod
    def _available_codes(db, session, product) -> list[str]:
        used = {row[0] for row in db.execute("SELECT code FROM used_codes WHERE session=? AND product_id=?",
                                            (session, product["id"]))}
        return [code for code in product["codes"] if code not in used]

    def product_catalog(self, session: str) -> list[dict]:
        with self._connection() as db:
            settings = self._settings(db, session)
            return [dict(id=product["id"], name=product["name"], price=product["price"],
                         enabled=product["enabled"], reward_type=product["reward_type"], purchase_limit=product["purchase_limit"],
                         stock=len(self._available_codes(db, session, product)) if product["reward_type"] == "codes" else None)
                    for product in settings["products"] if product["enabled"]]

    @staticmethod
    def _check_purchase_limit(db, session: str, user_id: str, product: dict) -> None:
        limit = product["purchase_limit"]
        if not limit:
            return
        count = db.execute("SELECT COUNT(*) FROM purchases WHERE session=? AND user_id=? AND product_id=? "
                           "AND status IN ('paid','sending','delivery_failed','delivered')",
                           (session, user_id, product["id"])).fetchone()[0]
        if count >= limit:
            raise ValueError(f"该商品每人累计限购 {limit} 次，你已达到限购次数")

    @staticmethod
    def _same_purchase_terms(product: dict, snapshot: dict) -> bool:
        return all(product.get(key) == snapshot.get(key)
                   for key in ("id", "name", "price", "reward_type", "reward_text"))

    def _order(self, db, row, *, private=True) -> dict:
        result = dict(row)
        result.pop("product_snapshot", None)
        if not private:
            if result.get("reward"):
                result["detail"] = result["detail"].replace(result["reward"], "[奖励内容已隐藏]")
            result.pop("reward", None)
        result["order_id"] = result["id"]
        result["total"] = self._balance(db, result["session"], result["user_id"])
        return result

    def create_purchase(self, session: str, user_id: str, product_id: str, now: datetime | None = None) -> dict:
        user_id, product_id = str(user_id), str(product_id)
        current = self._utc(now)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = self._settings(db, session)
            if not settings["enabled"]:
                raise ValueError("本会话已停用")
            unresolved = db.execute("SELECT id FROM purchases WHERE session=? AND user_id=? "
                                    "AND status IN ('paid','delivery_failed','sending') LIMIT 1", (session, user_id)).fetchone()
            if unresolved:
                raise ValueError("你有已付款但尚未完成发货的订单，请先使用本会话当前的确认或补领指令重发原奖励")
            product = next((item for item in settings["products"] if item["id"] == product_id and item["enabled"]), None)
            if product is None:
                raise ValueError("商品不存在或已下架")
            self._check_purchase_limit(db, session, user_id, product)
            if self._balance(db, session, user_id) < product["price"]:
                raise ValueError(f"余额不足，购买需要 {product['price']} {settings['currency_name']}")
            if product["reward_type"] == "codes" and not self._available_codes(db, session, product):
                raise ValueError("商品库存不足")
            db.execute("UPDATE purchases SET status='cancelled',detail=?,updated_at=? "
                       "WHERE session=? AND user_id=? AND status='pending'",
                       ("已被新的购买请求替换，未扣款", current.isoformat(), session, user_id))
            expires = current + timedelta(minutes=settings["confirmation_minutes"])
            cursor = db.execute("INSERT INTO purchases(session,user_id,product_id,product_name,price,currency_name,minutes,"
                                "status,product_snapshot,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)",
                                (session, user_id, product_id, product["name"], product["price"], settings["currency_name"],
                                 settings["confirmation_minutes"], json.dumps(product, ensure_ascii=False), expires.isoformat(),
                                 current.isoformat(), current.isoformat()))
            result = self._order(db, db.execute("SELECT * FROM purchases WHERE id=?", (cursor.lastrowid,)).fetchone())
            db.commit()
            return result

    def active_purchase(self, session: str, user_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT * FROM purchases WHERE session=? AND user_id=? "
                             "AND status IN ('pending','paid','delivery_failed','sending') ORDER BY id DESC LIMIT 1",
                             (session, str(user_id))).fetchone()
            return self._order(db, row) if row else None

    def confirm_purchase(self, session: str, user_id: str, now: datetime | None = None) -> dict:
        user_id = str(user_id)
        current = self._utc(now)
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM purchases WHERE session=? AND user_id=? ORDER BY id DESC LIMIT 1", (session, user_id)).fetchone()
            if not row:
                raise ValueError("没有待确认的购买请求，请先选择商品")
            if row["status"] in {"paid", "delivery_failed", "sending", "delivered"}:
                return self._order(db, row)
            if row["status"] != "pending":
                raise ValueError("购买请求已过期或取消，请重新选择商品")
            if current >= datetime.fromisoformat(row["expires_at"]):
                db.execute("UPDATE purchases SET status='expired',updated_at=? WHERE id=?", (current.isoformat(), row["id"]))
                db.commit()
                raise ValueError("购买确认时间已过期，请重新选择商品")
            settings = self._settings(db, session)
            if not settings["enabled"]:
                raise ValueError("本会话已停用，无法确认新购买")
            product = next((item for item in settings["products"] if item["id"] == row["product_id"] and item["enabled"]), None)
            if product is None:
                raise ValueError("商品不存在或已下架")
            if not self._same_purchase_terms(product, json.loads(row["product_snapshot"])):
                raise ValueError("商品配置已修改，请重新选择商品后确认")
            self._check_purchase_limit(db, session, user_id, product)
            old = self._balance(db, session, user_id)
            if old < row["price"]:
                raise ValueError("余额不足，未扣款")
            if product["reward_type"] == "codes":
                available = self._available_codes(db, session, product)
                if not available:
                    raise ValueError("商品库存不足，未扣款")
                reward = available[0]
                db.execute("INSERT INTO used_codes(session,product_id,code,order_id) VALUES(?,?,?,?)",
                           (session, product["id"], reward, row["id"]))
                settings = self._clean_code_pool(db, session, settings)
                db.execute("UPDATE session_settings SET settings=? WHERE session=?",
                           (json.dumps(settings, ensure_ascii=False), session))
            else:
                reward = product["reward_text"]
            total = old - row["price"]
            self._set_balance(db, session, user_id, total)
            self._audit(db, session, user_id, "purchase", old, total, str(row["id"]))
            db.execute("UPDATE purchases SET status='paid',reward=?,updated_at=? WHERE id=?", (reward, current.isoformat(), row["id"]))
            result = self._order(db, db.execute("SELECT * FROM purchases WHERE id=?", (row["id"],)).fetchone())
            db.commit()
            return result

    def claim_delivery(self, order_id: int) -> bool:
        with self._connection() as db:
            return db.execute("UPDATE purchases SET status='sending',updated_at=? "
                              "WHERE id=? AND status IN ('paid','delivery_failed')", (self._stamp(), order_id)).rowcount == 1

    def finish_delivery(self, order_id: int, success: bool, detail: str = "") -> None:
        if not isinstance(success, bool):
            raise ValueError("发送结果必须为布尔值")
        with self._connection() as db:
            db.execute("UPDATE purchases SET status=?,detail=?,updated_at=? WHERE id=? AND status='sending'",
                       ("delivered" if success else "delivery_failed", str(detail)[:10000], self._stamp(), order_id))

    def orders(self, session: str, limit: int = 100) -> list[dict]:
        with self._connection() as db:
            return [self._order(db, row, private=False) for row in db.execute(
                "SELECT * FROM purchases WHERE session=? ORDER BY id DESC LIMIT ?", (session, max(1, min(int(limit), 1000))))]
