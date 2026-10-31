"""Transactional storage for Yuma MCQQ. No AstrBot dependency.

Only short local transactions are performed here; network operations never run
while a database transaction is open. All financial writes have business keys.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


MAX_POINTS = 2**63 - 1
ACTIVE_ORDER_STATES = ("reserved", "sending", "unknown")


class DomainError(ValueError):
    """An expected, safe-to-display business rejection."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def integer(value, minimum=0, maximum=MAX_POINTS):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise DomainError("数量必须是有效范围内的整数")
    return value


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bindings(
                qq_id TEXT PRIMARY KEY,mc_uuid TEXT UNIQUE NOT NULL,player TEXT NOT NULL,
                server_id TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS binding_cooldowns(
                identity TEXT PRIMARY KEY,until_time REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS binding_history(
                id INTEGER PRIMARY KEY,qq_id TEXT NOT NULL,old_binding TEXT,new_binding TEXT,
                actor TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS binding_challenges(
                digest TEXT PRIMARY KEY,qq_id TEXT UNIQUE NOT NULL,player TEXT NOT NULL,
                server_id TEXT NOT NULL,umo TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS accounts(
                scope TEXT NOT NULL,qq_id TEXT NOT NULL,total INTEGER NOT NULL DEFAULT 0,
                frozen INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(scope,qq_id),CHECK(total>=0),CHECK(frozen>=0),CHECK(frozen<=total));
            CREATE TABLE IF NOT EXISTS ledger(
                id INTEGER PRIMARY KEY,operation_id TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,
                qq_id TEXT NOT NULL,delta INTEGER NOT NULL,total_after INTEGER NOT NULL,
                reason TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS signin_operations(
                id TEXT PRIMARY KEY,scope TEXT NOT NULL,qq_id TEXT NOT NULL,day TEXT NOT NULL,
                points INTEGER NOT NULL,binding_json TEXT NOT NULL,umo TEXT NOT NULL,
                state TEXT NOT NULL,game_status TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',
                result_json TEXT,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS signins(
                scope TEXT NOT NULL,qq_id TEXT NOT NULL,day TEXT NOT NULL,points INTEGER NOT NULL,
                operation_id TEXT UNIQUE NOT NULL,game_status TEXT NOT NULL,created REAL NOT NULL,
                PRIMARY KEY(scope,qq_id,day));
            CREATE TABLE IF NOT EXISTS products(
                scope TEXT NOT NULL,id TEXT NOT NULL,definition TEXT NOT NULL,version INTEGER NOT NULL,
                stock INTEGER NOT NULL,enabled INTEGER NOT NULL,PRIMARY KEY(scope,id));
            CREATE TABLE IF NOT EXISTS product_codes(
                scope TEXT NOT NULL,product_id TEXT NOT NULL,digest TEXT NOT NULL,code TEXT NOT NULL,
                order_id TEXT,used INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(scope,product_id,digest));
            CREATE TABLE IF NOT EXISTS orders(
                id TEXT PRIMARY KEY,operation_id TEXT UNIQUE NOT NULL,scope TEXT NOT NULL,
                qq_id TEXT NOT NULL,product_id TEXT NOT NULL,snapshot TEXT NOT NULL,
                binding_json TEXT NOT NULL,umo TEXT NOT NULL,status TEXT NOT NULL,
                reward TEXT NOT NULL DEFAULT '',detail TEXT NOT NULL DEFAULT '',
                expires REAL NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS orders_user ON orders(scope,qq_id,created DESC);
            CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY,action TEXT NOT NULL,actor TEXT NOT NULL,
                detail TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS migration_batches(
                digest TEXT PRIMARY KEY,summary TEXT NOT NULL,actor TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_records(
                batch TEXT NOT NULL,table_name TEXT NOT NULL,source_id TEXT NOT NULL,record TEXT NOT NULL,
                PRIMARY KEY(batch,table_name,source_id));
            """)
            db.execute("INSERT OR IGNORE INTO schema_meta VALUES('version','1')")

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def close(self):
        # Connections are scoped to individual operations.
        pass

    @staticmethod
    def _audit(db, action, actor, detail):
        db.execute("INSERT INTO audit_log(action,actor,detail,created) VALUES(?,?,?,?)",
                   (action, str(actor), encode(detail), time.time()))

    def audit(self, action, actor="system", detail=None):
        with self.connection(True) as db:
            self._audit(db, action, actor, detail or {})

    @staticmethod
    def _account(db, scope, qq_id):
        db.execute("INSERT OR IGNORE INTO accounts(scope,qq_id) VALUES(?,?)", (scope, str(qq_id)))
        return dict(db.execute("SELECT * FROM accounts WHERE scope=? AND qq_id=?", (scope, str(qq_id))).fetchone())

    def balance(self, scope, qq_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM accounts WHERE scope=? AND qq_id=?", (scope, str(qq_id))).fetchone()
            value = dict(row) if row else dict(scope=scope, qq_id=str(qq_id), total=0, frozen=0)
            value["available"] = value["total"] - value["frozen"]
            return value

    def rank(self, scope, limit=20):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT qq_id,total,frozen FROM accounts WHERE scope=? ORDER BY total DESC,qq_id LIMIT ?",
                                                (scope, max(1, min(int(limit), 100))))]

    def adjust_points(self, scope, qq_id, mode, amount, actor, operation_id, reason=""):
        integer(amount)
        if mode not in {"add", "sub", "set"}:
            raise DomainError("未知点数调整方式")
        with self.connection(True) as db:
            old = db.execute("SELECT * FROM ledger WHERE operation_id=?", (operation_id,)).fetchone()
            if old:
                if old["scope"] != scope or old["qq_id"] != str(qq_id):
                    raise DomainError("操作标识冲突")
                return dict(total=old["total_after"], delta=old["delta"], duplicate=True)
            account = self._account(db, scope, qq_id)
            total = amount if mode == "set" else account["total"] + (amount if mode == "add" else -amount)
            if not account["frozen"] <= total <= MAX_POINTS:
                raise DomainError("可用点数不足，或新余额小于冻结点数")
            delta = total - account["total"]
            db.execute("UPDATE accounts SET total=? WHERE scope=? AND qq_id=?", (total, scope, str(qq_id)))
            db.execute("INSERT INTO ledger(operation_id,scope,qq_id,delta,total_after,reason,actor,created) VALUES(?,?,?,?,?,?,?,?)",
                       (operation_id, scope, str(qq_id), delta, total, "admin_" + mode, str(actor), time.time()))
            self._audit(db, "points_adjust", actor, {"scope": scope, "qq_id": str(qq_id), "mode": mode,
                                                   "amount": amount, "reason": str(reason)[:2000], "operation_id": operation_id})
            return dict(total=total, delta=delta, duplicate=False)

    def binding(self, qq_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM bindings WHERE qq_id=?", (str(qq_id),)).fetchone()
            if not row:
                return None
            value = dict(row)
            value["cooldown_until"] = max(self._cooldown(db, "qq:" + str(qq_id)), self._cooldown(db, "mc:" + value["mc_uuid"]))
            return value

    def resolve_user(self, identifier):
        with self.connection() as db:
            rows = db.execute("SELECT qq_id FROM bindings WHERE lower(player)=lower(?)", (identifier,)).fetchall()
            if len(rows) == 1:
                return rows[0][0]
        if str(identifier).isdigit():
            return str(identifier)
        raise DomainError("无法唯一定位用户，请使用 QQ ID")

    @staticmethod
    def _cooldown(db, identity):
        row = db.execute("SELECT until_time FROM binding_cooldowns WHERE identity=?", (identity,)).fetchone()
        return float(row[0]) if row else 0.0

    @classmethod
    def _check_cooldowns(cls, db, identities, now):
        remaining = max((cls._cooldown(db, i) - now for i in identities), default=0)
        if remaining > 0:
            raise DomainError(f"绑定变更冷却中，还需等待 {int(remaining) + 1} 秒")

    def begin_binding(self, qq_id, player, server_id, umo, *, rebind=False, ttl=300, now=None):
        now = time.time() if now is None else now
        with self.connection(True) as db:
            old = db.execute("SELECT * FROM bindings WHERE qq_id=?", (str(qq_id),)).fetchone()
            if old and old["player"].casefold() == player.casefold() and old["server_id"] == server_id:
                return dict(already=True, binding=dict(old))
            if old and not rebind:
                raise DomainError("已经绑定其他游戏账号，请使用修改绑定")
            identities = ["qq:" + str(qq_id)] + (["mc:" + old["mc_uuid"]] if old else [])
            self._check_cooldowns(db, identities, now)
            code = secrets.token_hex(5).upper()
            digest = hashlib.sha256(code.encode()).hexdigest()
            db.execute("DELETE FROM binding_challenges WHERE qq_id=? OR expires<?", (str(qq_id), now))
            db.execute("INSERT INTO binding_challenges VALUES(?,?,?,?,?,?)", (digest, str(qq_id), player, server_id, umo, now + ttl))
            return dict(code=code, expires=now + ttl, already=False)

    def complete_binding(self, code, mc_uuid, player, server_id, cooldown_seconds, *, now=None):
        now = time.time() if now is None else now
        canonical_uuid = str(uuid.UUID(str(mc_uuid)))
        digest = hashlib.sha256(code.upper().encode()).hexdigest()
        with self.connection(True) as db:
            challenge = db.execute("SELECT * FROM binding_challenges WHERE digest=?", (digest,)).fetchone()
            if not challenge or challenge["expires"] < now:
                raise DomainError("绑定验证码无效或已过期")
            if challenge["server_id"] != server_id or challenge["player"].casefold() != player.casefold():
                raise DomainError("验证码与当前玩家或服务器不匹配")
            qq_id = challenge["qq_id"]
            old = db.execute("SELECT * FROM bindings WHERE qq_id=?", (qq_id,)).fetchone()
            owner = db.execute("SELECT qq_id FROM bindings WHERE mc_uuid=?", (canonical_uuid,)).fetchone()
            if owner and owner[0] != qq_id:
                raise DomainError("该游戏账号已被其他 QQ 绑定")
            identities = {"qq:" + qq_id, "mc:" + canonical_uuid}
            if old:
                identities.add("mc:" + old["mc_uuid"])
            self._check_cooldowns(db, identities, now)
            # Unbinding removes the live row, not its identity history. A later
            # bind must not reuse an old version that a pending order captured.
            history_count = db.execute("SELECT COUNT(*) FROM binding_history WHERE qq_id=?", (qq_id,)).fetchone()[0]
            version = max(int(old["version"]) if old else 0, history_count) + 1
            value = dict(qq_id=qq_id, mc_uuid=canonical_uuid, player=player, server_id=server_id, version=version, updated=now)
            db.execute("INSERT INTO bindings VALUES(?,?,?,?,?,?) ON CONFLICT(qq_id) DO UPDATE SET mc_uuid=excluded.mc_uuid,player=excluded.player,server_id=excluded.server_id,version=excluded.version,updated=excluded.updated",
                       (qq_id, canonical_uuid, player, server_id, version, now))
            for identity in identities:
                db.execute("INSERT INTO binding_cooldowns VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET until_time=MAX(until_time,excluded.until_time)",
                           (identity, now + max(0, int(cooldown_seconds))))
            db.execute("INSERT INTO binding_history(qq_id,old_binding,new_binding,actor,created) VALUES(?,?,?,?,?)",
                       (qq_id, encode(dict(old)) if old else None, encode(value), qq_id, now))
            db.execute("DELETE FROM binding_challenges WHERE digest=?", (digest,))
            return dict(binding=value, umo=challenge["umo"])

    def unbind(self, qq_id, cooldown_seconds, *, actor=None, force=False, now=None):
        now = time.time() if now is None else now
        with self.connection(True) as db:
            old = db.execute("SELECT * FROM bindings WHERE qq_id=?", (str(qq_id),)).fetchone()
            if not old:
                raise DomainError("尚未绑定游戏账号")
            identities = ("qq:" + str(qq_id), "mc:" + old["mc_uuid"])
            if not force:
                self._check_cooldowns(db, identities, now)
            db.execute("DELETE FROM bindings WHERE qq_id=?", (str(qq_id),))
            db.execute("DELETE FROM binding_challenges WHERE qq_id=?", (str(qq_id),))
            for identity in identities:
                db.execute("INSERT INTO binding_cooldowns VALUES(?,?) ON CONFLICT(identity) DO UPDATE SET until_time=MAX(until_time,excluded.until_time)",
                           (identity, now + max(0, int(cooldown_seconds))))
            db.execute("INSERT INTO binding_history(qq_id,old_binding,new_binding,actor,created) VALUES(?,?,?,?,?)",
                       (str(qq_id), encode(dict(old)), None, str(actor or qq_id), now))
            return dict(old)

    def begin_signin(self, operation_id, scope, qq_id, day, points, binding, umo):
        integer(points)
        now = time.time()
        with self.connection(True) as db:
            db.execute("INSERT OR IGNORE INTO signin_operations(id,scope,qq_id,day,points,binding_json,umo,state,created,updated) VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                       (operation_id, scope, str(qq_id), day, points, encode(binding), umo, now, now))
        return self.signin_operation(operation_id)

    def signin_operation(self, operation_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM signin_operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                return None
            value = dict(row)
            value["binding"] = json.loads(value.pop("binding_json"))
            result_json = value.pop("result_json")
            value["result"] = json.loads(result_json) if result_json else None
            return value

    def set_signin_state(self, operation_id, state, game_status="", detail="", fields=None):
        with self.connection(True) as db:
            db.execute("UPDATE signin_operations SET state=?,game_status=?,detail=?,updated=? WHERE id=? AND state!='completed'",
                       (state, game_status, str(detail)[:12000], time.time(), operation_id))
            if fields is not None:
                db.execute("UPDATE signin_operations SET result_json=? WHERE id=? AND state!='completed'", (encode(fields), operation_id))

    def credit_signin(self, operation_id):
        with self.connection(True) as db:
            op = db.execute("SELECT * FROM signin_operations WHERE id=?", (operation_id,)).fetchone()
            if not op:
                raise DomainError("签到操作不存在")
            if op["state"] == "completed":
                return json.loads(op["result_json"])
            if op["state"] != "game_confirmed" or op["game_status"] not in ("SIGNED", "ALREADY_SIGNED"):
                raise DomainError("游戏签到尚未确认，不能增加点数")
            existing = db.execute("SELECT * FROM signins WHERE scope=? AND qq_id=? AND day=?", (op["scope"], op["qq_id"], op["day"])).fetchone()
            account = self._account(db, op["scope"], op["qq_id"])
            points = 0 if existing else op["points"]
            total = account["total"] + points
            if total > MAX_POINTS:
                raise DomainError("点数余额超过上限")
            if not existing:
                db.execute("INSERT INTO signins VALUES(?,?,?,?,?,?,?)", (op["scope"], op["qq_id"], op["day"], points, operation_id, op["game_status"], time.time()))
                db.execute("UPDATE accounts SET total=? WHERE scope=? AND qq_id=?", (total, op["scope"], op["qq_id"]))
                db.execute("INSERT INTO ledger(operation_id,scope,qq_id,delta,total_after,reason,actor,created) VALUES(?,?,?,?,?,'signin',?,?)",
                           ("signin:" + operation_id, op["scope"], op["qq_id"], points, total, op["qq_id"], time.time()))
            result = dict(group_status="ALREADY_SIGNED" if existing else "SIGNED", points_delta=points, balance=total, game_status=op["game_status"], day=op["day"])
            confirmed_fields = json.loads(op["result_json"]) if op["result_json"] else {}
            result["game_continuous_days"] = confirmed_fields.get("game_continuous_days", "")
            db.execute("UPDATE signin_operations SET state='completed',result_json=?,updated=? WHERE id=?", (encode(result), time.time(), operation_id))
            return result

    def save_product(self, scope, product, actor):
        definition = dict(product)
        product_id = str(definition.get("id", "")).strip()
        if not product_id or len(product_id) > 80:
            raise DomainError("商品编号不能为空且不能超过 80 个字符")
        kind = definition.get("kind", "text")
        if kind not in ("text", "code", "rcon"):
            raise DomainError("商品类型必须为 text、code 或 rcon")
        if not isinstance(definition.get("enabled", True), bool):
            raise DomainError("商品启用状态必须为布尔值")
        definition = {"id": product_id, "name": str(definition.get("name", "")).strip(),
                      "price": integer(definition.get("price", 0)), "kind": kind,
                      "reward": str(definition.get("reward", "")), "command": str(definition.get("command", "")),
                      "rules": definition.get("rules", []), "enabled": bool(definition.get("enabled", True)),
                      "limit_per_user": integer(definition.get("limit_per_user", 0), maximum=10**9)}
        if not definition["name"] or len(definition["name"]) > 200:
            raise DomainError("商品名称不能为空且不能超过 200 个字符")
        if kind == "text" and not definition["reward"]:
            raise DomainError("文本商品必须填写交付内容")
        if len(definition["reward"]) > 16384:
            raise DomainError("文本奖励不能超过 16 KiB 字符")
        if kind == "rcon" and (not definition["command"] or not definition["rules"]):
            raise DomainError("游戏商品必须配置一条命令和结果识别规则")
        stock = integer(product.get("stock", -1), minimum=-1, maximum=10**9)
        with self.connection(True) as db:
            old = db.execute("SELECT version,definition FROM products WHERE scope=? AND id=?", (scope, product_id)).fetchone()
            version = (old["version"] + (json.loads(old["definition"]) != definition)) if old else 1
            db.execute("INSERT INTO products VALUES(?,?,?,?,?,?) ON CONFLICT(scope,id) DO UPDATE SET definition=excluded.definition,version=excluded.version,stock=excluded.stock,enabled=excluded.enabled",
                       (scope, product_id, encode(definition), version, stock, int(definition["enabled"])))
            self._audit(db, "product_save", actor, {"scope": scope, "product_id": product_id, "version": version})
        return self.get_product(scope, product_id)

    @staticmethod
    def _product(db, scope, product_id, private=False):
        row = db.execute("SELECT * FROM products WHERE scope=? AND id=?", (scope, str(product_id))).fetchone()
        if not row:
            raise DomainError("商品不存在")
        value = json.loads(row["definition"])
        value.update(scope=scope, version=row["version"], stock=row["stock"], enabled=bool(row["enabled"]))
        if value["kind"] == "code":
            value["stock"] = db.execute("SELECT count(*) FROM product_codes WHERE scope=? AND product_id=? AND used=0", (scope, str(product_id))).fetchone()[0]
        if not private:
            for key in ("reward", "command", "rules"):
                value.pop(key, None)
        return value

    def get_product(self, scope, product_id, private=False):
        with self.connection() as db:
            return self._product(db, scope, product_id, private)

    def list_products(self, scope=None, private=False):
        with self.connection() as db:
            rows = db.execute("SELECT scope,id FROM products" + (" WHERE scope=?" if scope is not None else "") + " ORDER BY scope,id", (scope,) if scope is not None else ()).fetchall()
            return [self._product(db, r["scope"], r["id"], private) for r in rows]

    def delete_product(self, scope, product_id, actor):
        # Keep a tombstone so allocated codes and outstanding orders remain valid.
        with self.connection(True) as db:
            changed = db.execute("UPDATE products SET enabled=0,version=version+1 WHERE scope=? AND id=?", (scope, str(product_id))).rowcount
            self._audit(db, "product_disable", actor, {"scope": scope, "product_id": product_id})
            return bool(changed)

    def add_codes(self, scope, product_id, codes, actor):
        if not isinstance(codes, list) or len(codes) > 10000:
            raise DomainError("兑换码必须为列表，单次最多 10000 条")
        with self.connection(True) as db:
            if self._product(db, scope, product_id)["kind"] != "code":
                raise DomainError("该商品不是兑换码商品")
            count = 0
            for code in codes:
                if not isinstance(code, str) or not code.strip() or len(code) > 4000:
                    raise DomainError("兑换码不能为空或超过长度限制")
                code = code.strip()
                count += db.execute("INSERT OR IGNORE INTO product_codes(scope,product_id,digest,code) VALUES(?,?,?,?)",
                                    (scope, str(product_id), hashlib.sha256(code.encode()).hexdigest(), code)).rowcount
            self._audit(db, "product_codes", actor, {"scope": scope, "product_id": product_id, "added": count})
            return count

    @staticmethod
    def _order(row, private=False):
        if not row:
            return None
        value = dict(row)
        value["snapshot"] = json.loads(value["snapshot"])
        value["binding"] = json.loads(value.pop("binding_json"))
        if not private:
            value.pop("reward", None)
            for field in ("reward", "command", "rules"):
                value["snapshot"].pop(field, None)
        return value

    def order(self, order_id, private=False):
        with self.connection() as db:
            return self._order(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone(), private)

    def orders(self, scope=None, qq_id=None, limit=100):
        filters, values = [], []
        if scope is not None:
            filters.append("scope=?")
            values.append(scope)
        if qq_id is not None:
            filters.append("qq_id=?")
            values.append(str(qq_id))
        values.append(max(1, min(int(limit), 1000)))
        with self.connection() as db:
            return [self._order(r) for r in db.execute("SELECT * FROM orders" + (" WHERE " + " AND ".join(filters) if filters else "") + " ORDER BY created DESC LIMIT ?", values)]

    def create_order(self, scope, qq_id, product_id, operation_id, umo, binding=None, ttl=180):
        with self.connection(True) as db:
            previous = db.execute("SELECT * FROM orders WHERE operation_id=?", (operation_id,)).fetchone()
            if previous:
                return self._order(previous)
            active = db.execute("SELECT id FROM orders WHERE scope=? AND qq_id=? AND status IN ('reserved','sending','unknown','paid_pending','paid_sending') LIMIT 1", (scope, str(qq_id))).fetchone()
            if active:
                raise DomainError(f"请先处理未完成订单 {active[0]}")
            product = self._product(db, scope, product_id, True)
            if not product["enabled"]:
                raise DomainError("商品已下架")
            if product["stock"] == 0:
                raise DomainError("商品库存不足")
            if product["kind"] == "rcon" and not binding:
                raise DomainError("游戏商品需要先绑定 MC 账号")
            order_id, now = secrets.token_hex(8), time.time()
            db.execute("INSERT INTO orders(id,operation_id,scope,qq_id,product_id,snapshot,binding_json,umo,status,expires,created,updated) VALUES(?,?,?,?,?,?,?,?,'awaiting_confirm',?,?,?)",
                       (order_id, operation_id, scope, str(qq_id), str(product_id), encode(product), encode(binding or {}), umo, now + ttl, now, now))
            return self._order(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

    def reserve_order(self, order_id, scope, qq_id):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM orders WHERE id=? AND scope=? AND qq_id=?", (order_id, scope, str(qq_id))).fetchone()
            if not row:
                raise DomainError("订单不存在或不属于当前用户和会话")
            if row["status"] in ("reserved", "sending", "unknown", "fulfilled", "paid_pending", "paid_sending"):
                return self._order(row, True)
            if row["status"] != "awaiting_confirm" or row["expires"] < time.time():
                raise DomainError("订单已取消或确认期限已过，请重新选择商品")
            snapshot = json.loads(row["snapshot"])
            current = self._product(db, scope, row["product_id"], True)
            if not current["enabled"] or current["version"] != snapshot["version"]:
                raise DomainError("商品已变更，请重新选择商品确认")
            count = db.execute("SELECT count(*) FROM orders WHERE scope=? AND qq_id=? AND product_id=? AND status IN ('reserved','sending','unknown','fulfilled')",
                               (scope, str(qq_id), row["product_id"])).fetchone()[0]
            if current["limit_per_user"] and count >= current["limit_per_user"]:
                raise DomainError("已达到该商品的限购数量")
            if current["stock"] == 0:
                raise DomainError("商品库存不足")
            account = self._account(db, scope, qq_id)
            if account["total"] - account["frozen"] < current["price"]:
                raise DomainError("可用点数不足")
            reward = current["reward"]
            if current["kind"] == "code":
                code = db.execute("SELECT digest,code FROM product_codes WHERE scope=? AND product_id=? AND used=0 ORDER BY rowid LIMIT 1", (scope, row["product_id"])).fetchone()
                if not code:
                    raise DomainError("兑换码库存不足")
                reward = code["code"]
                db.execute("UPDATE product_codes SET used=1,order_id=? WHERE scope=? AND product_id=? AND digest=?", (order_id, scope, row["product_id"], code["digest"]))
            elif current["stock"] >= 0:
                db.execute("UPDATE products SET stock=stock-1 WHERE scope=? AND id=?", (scope, row["product_id"]))
            db.execute("UPDATE accounts SET frozen=frozen+? WHERE scope=? AND qq_id=?", (current["price"], scope, str(qq_id)))
            db.execute("UPDATE orders SET status='reserved',reward=?,updated=? WHERE id=?", (reward, time.time(), order_id))
            return self._order(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone(), True)

    def claim_delivery(self, order_id, *, reclaim=False):
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError("订单不存在")
            allowed = ["reserved", "paid_pending"]
            if reclaim and json.loads(row["snapshot"])["kind"] in ("text", "code"):
                allowed.extend(["unknown", "fulfilled"])
            if row["status"] not in allowed:
                return None
            # A fulfilled reward may be re-sent, but never transitions out of paid state.
            if row["status"] != "fulfilled":
                state = "paid_sending" if row["status"] == "paid_pending" else "sending"
                db.execute("UPDATE orders SET status=?,updated=? WHERE id=?", (state, time.time(), order_id))
            return self._order(row, True)

    def finish_order(self, order_id, outcome, detail="", actor="system"):
        if outcome not in ("fulfilled", "cancelled", "unknown"):
            raise DomainError("未知订单处理结果")
        with self.connection(True) as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                raise DomainError("订单不存在")
            if row["status"] in ("fulfilled", "cancelled"):
                if outcome != row["status"] and outcome != "unknown":
                    raise DomainError("订单已终结，不能重复或反向结算")
                return self._order(row)
            if row["status"] in ("paid_pending", "paid_sending"):
                if outcome == "cancelled":
                    raise DomainError("迁移的已付款订单不能自动退款，请核对原账本后单独调整点数")
                state = "fulfilled" if outcome == "fulfilled" else "paid_pending"
                db.execute("UPDATE orders SET status=?,detail=?,updated=? WHERE id=?", (state, str(detail)[:2000], time.time(), order_id))
                self._audit(db, "legacy_delivery_" + state, actor, {"order_id": order_id})
                return self._order(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())
            if row["status"] not in ACTIVE_ORDER_STATES:
                raise DomainError("订单尚未冻结点数")
            product = json.loads(row["snapshot"])
            price = product["price"]
            if outcome in ("fulfilled", "cancelled"):
                account = self._account(db, row["scope"], row["qq_id"])
                if account["frozen"] < price:
                    raise DomainError("订单冻结点数异常，请人工核对")
                total = account["total"] - (price if outcome == "fulfilled" else 0)
                db.execute("UPDATE accounts SET total=?,frozen=frozen-? WHERE scope=? AND qq_id=?", (total, price, row["scope"], row["qq_id"]))
                if outcome == "fulfilled":
                    db.execute("INSERT INTO ledger(operation_id,scope,qq_id,delta,total_after,reason,actor,created) VALUES(?,?,?,?,?,'shop',?,?)",
                               ("order:" + order_id, row["scope"], row["qq_id"], -price, total, str(actor), time.time()))
                elif product["kind"] != "code" and product["stock"] >= 0:
                    db.execute("UPDATE products SET stock=stock+1 WHERE scope=? AND id=? AND stock>=0", (row["scope"], row["product_id"]))
            db.execute("UPDATE orders SET status=?,detail=?,updated=? WHERE id=?", (outcome, str(detail)[:2000], time.time(), order_id))
            self._audit(db, "order_" + outcome, actor, {"order_id": order_id})
            return self._order(db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone())

    def recover(self):
        with self.connection(True) as db:
            db.execute("UPDATE signin_operations SET state='unknown',detail='进程中断，游戏执行结果待核对',updated=? WHERE state='sending'", (time.time(),))
            db.execute("UPDATE orders SET status='unknown',detail='进程中断，交付结果待核对',updated=? WHERE status='sending'", (time.time(),))
            db.execute("UPDATE orders SET status='paid_pending',detail='迁移已付款订单发送中断，可补领同一份奖励',updated=? WHERE status='paid_sending'", (time.time(),))
            confirmed = [r[0] for r in db.execute("SELECT id FROM signin_operations WHERE state='game_confirmed'")]
        results = []
        for operation_id in confirmed:
            results.append(self.credit_signin(operation_id))
        return results

    def snapshot(self, scope=None):
        clause = " WHERE scope=?" if scope is not None else ""
        params = (scope,) if scope is not None else ()
        with self.connection() as db:
            bindings = [dict(r) for r in db.execute("SELECT * FROM bindings ORDER BY qq_id LIMIT 1000")]
            for binding in bindings:
                binding["cooldown_until"] = max(self._cooldown(db, "qq:" + binding["qq_id"]), self._cooldown(db, "mc:" + binding["mc_uuid"]))
            balances = [dict(r) for r in db.execute("SELECT * FROM accounts" + clause + " ORDER BY total DESC LIMIT 1000", params)]
            ledger = [dict(r) for r in db.execute("SELECT * FROM ledger" + clause + " ORDER BY id DESC LIMIT 200", params)]
            signins = [dict(r) for r in db.execute("SELECT scope,qq_id,day,points,game_status,created FROM signins" + clause + " ORDER BY created DESC LIMIT 200", params)]
            operations = [dict(r) for r in db.execute("SELECT id,scope,qq_id,day,points,state,game_status,detail,created,updated FROM signin_operations" + clause + " ORDER BY created DESC LIMIT 200", params)]
        return dict(bindings=bindings, balances=balances, ledger=ledger, signins=signins, signin_operations=operations,
                    products=self.list_products(scope), orders=self.orders(scope))

    def _read_legacy(self, path):
        """Read only a user-selected offline SQLite copy; never discover live data."""
        source = Path(path).expanduser().resolve(strict=True)
        if source == self.path.resolve() or not source.is_file():
            raise DomainError("请选择旧插件数据库副本，不能导入当前数据库")
        if source.stat().st_size > 256 * 1024 * 1024:
            raise DomainError("数据库超过 256 MiB，请先离线分批迁移")
        permitted = ("session_settings", "balances", "checkins", "checkin_resets", "bindings", "balance_audit", "purchases", "used_codes")
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as legacy:
            legacy.row_factory = sqlite3.Row
            tables = {r[0] for r in legacy.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"balances", "checkins"}.issubset(tables):
                raise DomainError("不是受支持的旧群签到点数数据库")
            data = {}
            for table in permitted:
                if table not in tables:
                    continue
                rows = [dict(row) for row in legacy.execute(f'SELECT * FROM "{table}" LIMIT 100001')]
                if len(rows) > 100000:
                    raise DomainError("单张旧表超过 100000 行，请使用离线分批迁移")
                data[table] = rows
        digest = hashlib.sha256(encode(data).encode()).hexdigest()
        return data, digest

    @staticmethod
    def _legacy_summary(data, digest):
        totals = {}
        for row in data.get("balances", []):
            scope = str(row["session"])
            totals[scope] = totals.get(scope, 0) + integer(row["total"])
        return dict(digest=digest, counts={key: len(value) for key, value in data.items()}, balance_totals=totals,
                    warnings=["仅迁移数据，不启用旧插件配置或旧指令。", "只有玩家名的旧绑定不导入，需要重新验证 UUID。",
                              "旧流水与完整记录归档保留，余额以迁移期初值入账。", "旧未付款订单取消；已付款未交付奖励保留，可补领同一份奖励且不再次扣款。"])

    def preview_legacy(self, path):
        data, digest = self._read_legacy(path)
        summary = self._legacy_summary(data, digest)
        with self.connection() as db:
            summary["already_imported"] = bool(db.execute("SELECT 1 FROM migration_batches WHERE digest=?", (digest,)).fetchone())
        return summary

    @staticmethod
    def _legacy_product(raw):
        return dict(id=str(raw.get("id", "")), name=str(raw.get("name", "旧商品")),
                    price=integer(raw.get("price", 0)), kind="code" if raw.get("reward_type") == "codes" else "text",
                    reward=str(raw.get("reward_text", "")), command="", rules=[], enabled=False,
                    limit_per_user=integer(raw.get("purchase_limit", 0)), stock=-1, version=1)

    def import_legacy(self, path, expected_digest, actor, scope_map=None):
        data, digest = self._read_legacy(path)
        if digest != expected_digest:
            raise DomainError("源数据库已变化，请重新预览后导入")
        scope_map = scope_map or {}
        if not isinstance(scope_map, dict):
            raise DomainError("作用域映射必须为对象")
        def scope_for(value):
            return str(scope_map.get(str(value), str(value)))
        summary = self._legacy_summary(data, digest)
        now = time.time()
        prefix = "legacy:" + digest[:16] + ":"
        with self.connection(True) as db:
            existing = db.execute("SELECT summary FROM migration_batches WHERE digest=?", (digest,)).fetchone()
            if existing:
                return dict(json.loads(existing[0]), already_imported=True)
            # Never silently merge balances into a destination with existing activity.
            for row in data.get("balances", []):
                scope, user = scope_for(row["session"]), str(row["user_id"])
                if db.execute("SELECT 1 FROM accounts WHERE scope=? AND qq_id=?", (scope, user)).fetchone():
                    raise DomainError("目标作用域已有对应账户，请使用空目标或明确的作用域映射")
                total = integer(row["total"])
                db.execute("INSERT INTO accounts VALUES(?,?,?,0)", (scope, user, total))
                db.execute("INSERT INTO ledger(operation_id,scope,qq_id,delta,total_after,reason,actor,created) VALUES(?,?,?,?,?,'migration_opening',?,?)",
                           (prefix + "balance:" + scope + ":" + user, scope, user, total, total, str(actor), now))
            # Preserve source records verbatim in a restricted archive, never in public state.
            for table, rows in data.items():
                for index, row in enumerate(rows):
                    db.execute("INSERT INTO legacy_records VALUES(?,?,?,?)", (digest, table, str(row.get("id", index)), encode(row)))
            for row in data.get("checkins", []):
                if not bool(row.get("credited", 1)):
                    continue
                scope, user = scope_for(row["session"]), str(row["user_id"])
                if db.execute("SELECT 1 FROM signins WHERE scope=? AND qq_id=? AND day=?", (scope, user, row["day"])).fetchone():
                    raise DomainError("签到记录与目标数据冲突")
                db.execute("INSERT INTO signins VALUES(?,?,?,?,?,?,?)", (scope, user, str(row["day"]), integer(row["points"]), prefix + "signin:" + str(row["id"]), "LEGACY_CREDITED", now))
            for row in data.get("session_settings", []):
                scope = scope_for(row["session"])
                settings = json.loads(row["settings"])
                for raw in settings.get("products", []):
                    product = self._legacy_product(raw)
                    product_id = product["id"]
                    if not product_id:
                        raise DomainError("旧商品缺少编号")
                    if db.execute("SELECT 1 FROM products WHERE scope=? AND id=?", (scope, product_id)).fetchone():
                        raise DomainError("旧商品与目标商品编号冲突")
                    definition = {k: v for k, v in product.items() if k not in ("stock", "version")}
                    db.execute("INSERT INTO products VALUES(?,?,?,1,-1,0)", (scope, product_id, encode(definition)))
                    for code in raw.get("codes", []):
                        code = str(code)
                        db.execute("INSERT OR IGNORE INTO product_codes(scope,product_id,digest,code) VALUES(?,?,?,?)", (scope, product_id, hashlib.sha256(code.encode()).hexdigest(), code))
            for row in data.get("used_codes", []):
                scope, product_id, code = scope_for(row["session"]), str(row["product_id"]), str(row["code"])
                db.execute("INSERT INTO product_codes VALUES(?,?,?,?,?,1) ON CONFLICT(scope,product_id,digest) DO UPDATE SET used=1,order_id=excluded.order_id",
                           (scope, product_id, hashlib.sha256(code.encode()).hexdigest(), code, prefix + "order:" + str(row["order_id"])))
            for row in data.get("purchases", []):
                raw = json.loads(row["product_snapshot"])
                product = self._legacy_product(raw)
                product.update(id=str(row["product_id"]), name=str(row["product_name"]), price=integer(row["price"]))
                old_status = row["status"]
                state = "fulfilled" if old_status == "delivered" else "paid_pending" if old_status in ("paid", "sending", "delivery_failed") else "cancelled"
                order_id = prefix + "order:" + str(row["id"])
                db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (order_id, order_id, scope_for(row["session"]), str(row["user_id"]), str(row["product_id"]), encode(product), "{}", str(row["session"]), state,
                            str(row.get("reward", "")), "从旧插件导入；旧未付款订单已取消，旧已付款订单不会再次扣款", 0, now, now))
            db.execute("INSERT INTO migration_batches VALUES(?,?,?,?)", (digest, encode(summary), str(actor), now))
            self._audit(db, "legacy_import", actor, {"digest": digest, "counts": summary["counts"]})
        return dict(summary, already_imported=False)
