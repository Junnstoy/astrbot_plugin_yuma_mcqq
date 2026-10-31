"""Bounded, read-only dashboard pages; no business actions or raw rewards."""
from __future__ import annotations

from contextlib import contextmanager


PAGE_SIZES = frozenset({10, 20, 50, 100})
# Only constant identifiers enter SQL; request values are always parameters.
TABLES = {
    "bindings": ("bindings", "*", "updated DESC,qq_id ASC", False),
    "balances": ("accounts", "*", "total DESC,scope ASC,qq_id ASC", True),
    "ledger": ("ledger", "*", "created DESC,id DESC", True),
    "signins": ("signins", "*", "day DESC,created DESC,scope ASC,qq_id ASC,operation_id ASC", True),
    "runs": ("runs", "id,action,server_id,status,created_at,updated_at", "created_at DESC,id ASC", False),
    "audit": ("audit", "id,actor,action,detail,created_at", "id DESC", False),
}


@contextmanager
def _snapshot(connection):
    """Keep count and rows consistent without committing a caller's transaction."""
    started = not connection.in_transaction
    if started:
        connection.execute("BEGIN")
    try:
        yield connection
    finally:
        if started:
            connection.rollback()


def read_page(store, ops, payload):
    """Return {rows,total,page,page_size,pages} for a known dashboard table.

    Scope applies to balances, ledger and signins before COUNT/LIMIT. Bindings
    are globally unique; runs/audit have no scope column and stay global. The
    operations projection deliberately excludes raw RCON replies and UMO.
    """
    if not isinstance(payload, dict):
        raise ValueError("分页请求必须为对象")
    name = payload.get("table")
    if not isinstance(name, str) or name not in TABLES:
        raise ValueError("不支持的分页数据表")
    page, page_size = payload.get("page", 1), payload.get("page_size", 20)
    if isinstance(page, bool) or not isinstance(page, int):
        raise ValueError("页码必须是整数")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size not in PAGE_SIZES:
        raise ValueError("每页数量只能为 10、20、50 或 100")
    scope = payload.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise ValueError("作用域必须为字符串")
    table, columns, ordering, scoped = TABLES[name]
    where, params = (" WHERE scope=?", (scope,)) if scoped and scope else ("", ())

    def query(connection):
        with _snapshot(connection):
            total = connection.execute(f"SELECT COUNT(*) FROM {table}{where}", params).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            current = min(max(1, page), pages)
            cursor = connection.execute(f"SELECT {columns} FROM {table}{where} ORDER BY {ordering} LIMIT ? OFFSET ?",
                                        (*params, page_size, (current - 1) * page_size))
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchall()]
            if name == "bindings":
                for row in rows:
                    row["verified"] = bool(row["verified"])
                    row["cooldown_until"] = max(store._cooldown(connection, identity) for identity in store._binding_identities(row))
            elif name == "balances":
                for row in rows:
                    row["available"] = row["total"] - row["frozen"]
            return dict(rows=rows, total=total, page=current, page_size=page_size, pages=pages)

    if name in {"runs", "audit"}:
        return query(ops)
    with store.connection() as connection:
        return query(connection)
