"""Periodic performance sampling, missing-data persistence and PNG charts."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import struct
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..core.text import clean_text

NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?)"
log = logging.getLogger(__name__)


def _metric_lines(raw: str) -> list[str]:
    # Spark prefixes every reply; some RCON senders concatenate replies without LF.
    cleaned = re.sub(r"\[(?:⚡|spark)\]\s*", "\n", clean_text(raw), flags=re.I)
    return [line.strip().replace("*", "") for line in cleaned.splitlines() if line.strip()]


def parse_tps(raw: str) -> dict:
    lines = _metric_lines(raw)
    for index, line in enumerate(lines):
        if "tps" not in line.lower():
            continue
        tail = line.split(":", 1)[-1] if ":" in line else ""
        values = [float(value) for value in re.findall(NUMBER, tail)]
        if len(values) < 3 and index + 1 < len(lines):
            candidate = lines[index + 1]
            if re.fullmatch(r"[\d.,\s]+", candidate):
                values = [float(value) for value in re.findall(NUMBER, candidate)]
        if len(values) in (3, 5) and all(0 <= value <= 100 for value in values):
            windows = ["1m", "5m", "15m"] if len(values) == 3 else ["5s", "10s", "1m", "5m", "15m"]
            return {"value": values[0], "window": windows[0], "windows": dict(zip(windows, values))}
    return {"value": None, "window": None, "windows": {}}


def parse_mspt(raw: str) -> dict:
    lines = _metric_lines(raw)
    groups = []
    for line in lines:
        for group in re.findall(rf"({NUMBER})\s*/\s*({NUMBER})\s*/\s*({NUMBER})(?:\s*/\s*({NUMBER}))?", line):
            groups.append([float(value) for value in group if value])
    if groups:
        spark = len(groups[0]) == 4
        windows = ["10s", "1m"] if spark else ["5s", "10s", "1m"]
        labels = ["min", "median", "p95", "max"] if spark else ["mean", "min", "max"]
        parsed = {window: dict(zip(labels, value)) for window, value in zip(windows, groups)}
        stat = "median" if spark else "mean"
        return {"value": parsed[windows[0]][stat], "window": windows[0], "statistic": stat, "windows": parsed}
    for line in lines:
        match = re.search(rf"(?:mean|average|avg|平均)\s*[:=：]?\s*({NUMBER})\s*ms", line, re.I)
        if match:
            return {"value": float(match[1]), "window": "reported", "statistic": "mean", "windows": {}}
    return {"value": None, "window": None, "statistic": None, "windows": {}}


def parse_ping(raw: str) -> dict:
    lines = _metric_lines(raw)
    if any(re.search(r"no (?:players|player)|没有.*玩家|无在线玩家", line, re.I) for line in lines):
        return {"value": None, "statistic": "no_players"}
    groups = re.findall(rf"({NUMBER})\s*/\s*({NUMBER})\s*/\s*({NUMBER})\s*/\s*({NUMBER})", "\n".join(lines))
    if groups:
        windows = {name: dict(zip(("min", "median", "p95", "max"), map(float, group))) for name, group in zip(("now", "15m"), groups)}
        return {"value": windows["now"]["median"], "statistic": "online_players_median", "window": "now", "windows": windows, **windows["now"]}
    for line in lines:
        match = re.search(rf"(?:average|mean|avg|平均)\D{{0,10}}({NUMBER})\s*ms", line, re.I)
        if match:
            return {"value": float(match[1]), "statistic": "online_players_mean"}
        match = re.search(rf"(?:ping|延迟).*?({NUMBER})\s*ms", line, re.I)
        if match:
            return {"value": float(match[1]), "statistic": "reported_player_ping"}
        match = re.search(rf"has\s+({NUMBER})\s+ms\s+ping", line, re.I)
        if match:
            return {"value": float(match[1]), "statistic": "reported_player_ping"}
    return {"value": None, "statistic": None}


def _varint(number: int) -> bytes:
    number &= 0xFFFFFFFF
    output = bytearray()
    while True:
        byte = number & 0x7F
        number >>= 7
        output.append(byte | (0x80 if number else 0))
        if not number:
            return bytes(output)


async def _read_varint(reader: asyncio.StreamReader) -> int:
    result = 0
    for index in range(5):
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 0x7F) << (7 * index)
        if not byte & 0x80:
            return result
    raise ValueError("invalid Minecraft VarInt")


async def slp_ping(host: str, port: int, timeout: float = 3) -> float:
    async def operation():
        reader, writer = await asyncio.open_connection(host, port)
        try:
            address = host.encode("utf-8")
            if len(address) > 255:
                raise ValueError("host too long")
            handshake = b"\x00" + _varint(-1) + _varint(len(address)) + address + struct.pack(">H", port) + b"\x01"
            writer.write(_varint(len(handshake)) + handshake + b"\x01\x00")
            await writer.drain()
            length = await _read_varint(reader)
            if not 1 <= length <= 1024 * 1024:
                raise ValueError("invalid status response size")
            status = await reader.readexactly(length)
            if status[0] != 0:
                raise ValueError("invalid status packet")
            nonce = struct.pack(">q", time.time_ns() // 1000000)
            started = time.perf_counter()
            writer.write(b"\x09\x01" + nonce)
            await writer.drain()
            length = await _read_varint(reader)
            if length != 9 or await reader.readexactly(length) != b"\x01" + nonce:
                raise ValueError("invalid pong")
            return (time.perf_counter() - started) * 1000
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
    return await asyncio.wait_for(operation(), timeout)


class Monitor:
    def __init__(self, store, settings, gateway, data_dir):
        self.settings, self.gateway = settings, gateway
        self.directory = Path(data_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._db = None
        self._open()
        self._task: asyncio.Task | None = None
        self._locks: dict[str, asyncio.Lock] = {}
        self._chart_lock = asyncio.Lock()
        self._last: dict[str, dict] = {}

    def _open(self):
        if self._db is None:
            self._db = sqlite3.connect(self.directory / "metrics.sqlite3")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("CREATE TABLE IF NOT EXISTS samples(id INTEGER PRIMARY KEY,server_id TEXT NOT NULL,timestamp REAL NOT NULL,payload TEXT NOT NULL)")
            self._db.execute("CREATE INDEX IF NOT EXISTS sample_window ON samples(server_id,timestamp)")
            self._db.commit()

    async def start(self):
        self._open()
        if self._task or not self.settings.data.get("monitor", {}).get("enabled", True):
            return
        self._task = asyncio.create_task(self._loop(), name="yuma-performance-sampler")

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._db is not None:
            self._db.close()
            self._db = None

    async def reload(self):
        await self.close()
        await self.start()

    def server_references(self, server_id):
        self._open()
        return self._db.execute("SELECT COUNT(*) FROM samples WHERE server_id=?", (server_id,)).fetchone()[0]

    def rename_server(self, old_id, new_id, operation_id):
        """Called only after sampling and foreground rendering have drained."""
        self._open()
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS server_renames(id TEXT PRIMARY KEY,old_id TEXT,new_id TEXT,count INTEGER)")
            previous = self._db.execute("SELECT old_id,new_id,count FROM server_renames WHERE id=?", (operation_id,)).fetchone()
            if previous:
                if tuple(previous[:2]) != (old_id, new_id):
                    raise ValueError("性能历史重命名操作不匹配")
                count = previous[2]
            else:
                rows = self._db.execute("SELECT id,payload FROM samples WHERE server_id=?", (old_id,)).fetchall()
                for row in rows:
                    payload = json.loads(row[1])
                    payload["server_id"] = new_id
                    self._db.execute("UPDATE samples SET server_id=?,payload=? WHERE id=?", (new_id, json.dumps(payload, ensure_ascii=False), row[0]))
                count = len(rows)
                self._db.execute("INSERT INTO server_renames VALUES(?,?,?,?)", (operation_id, old_id, new_id, count))
        self._last.clear()
        self._locks.pop(old_id, None)
        return count

    async def _loop(self):
        # Let WebSocket connections establish before the first automatic sample.
        await asyncio.sleep(2)
        while True:
            started = time.monotonic()
            if self.settings.data.get("enabled", True):
                for server in self.settings.data.get("servers", []):
                    if server.get("enabled", True):
                        try:
                            await self.sample(str(server["id"]))
                        except Exception as exc:
                            log.warning("[YumaMCQQ] sampler unavailable for %s: %s", server["id"], type(exc).__name__)
            interval = float(self.settings.data.get("monitor", {}).get("interval_seconds", 60))
            await asyncio.sleep(max(1, interval - (time.monotonic() - started)))

    async def sample(self, server_id: str) -> dict:
        self._open()
        lock = self._locks.setdefault(server_id, asyncio.Lock())
        if lock.locked():
            return {"server_id": server_id, "timestamp": time.time(), "status": "skipped", "error": "sample_in_progress"}
        async with lock:
            server = self.settings.server(server_id)
            result = {"server_id": server_id, "timestamp": time.time(), "tps": None, "mspt": None, "player_ping_ms": None, "slp_rtt_ms": None, "values": {}, "errors": []}
            for key, command_key, parser, default in (
                ("tps", "tps_command", parse_tps, "tps"),
                ("mspt", "mspt_command", parse_mspt, "mspt"),
                ("player_ping_ms", "ping_command", parse_ping, "ping"),
            ):
                command = str(server.get(command_key, default)).strip()
                if not command:
                    result["errors"].append(key + ":disabled")
                    continue
                try:
                    raw = await self.gateway.rcon(server_id, command)
                    parsed = parser(raw)
                    value = parsed["value"]
                    if value is not None and (not math.isfinite(value) or value < 0):
                        value = None
                    result[key] = value
                    result["values"][key] = parsed
                    if value is None:
                        result["errors"].append(key + ":unavailable")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result["errors"].append(key + ":" + type(exc).__name__)
            if server.get("game_host"):
                try:
                    result["slp_rtt_ms"] = await slp_ping(str(server["game_host"]), int(server.get("game_port", 25565)))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result["errors"].append("slp_rtt_ms:" + type(exc).__name__)
            valid = sum(result[key] is not None for key in ("tps", "mspt", "player_ping_ms", "slp_rtt_ms"))
            result["status"] = "ok" if not result["errors"] else ("partial" if valid else "missing")
            result["error"] = "; ".join(result["errors"])
            self._db.execute("INSERT INTO samples(server_id,timestamp,payload) VALUES(?,?,?)", (server_id, result["timestamp"], json.dumps(result, ensure_ascii=False)))
            retention = int(self.settings.data.get("monitor", {}).get("retention_days", 7)) * 86400
            self._db.execute("DELETE FROM samples WHERE timestamp<?", (time.time() - retention,))
            self._db.commit()
            self._last[server_id] = result
            return result

    def history(self, server_id: str, hours: float = 24) -> list[dict]:
        self._open()
        hours = min(24 * 365, max(1 / 60, float(hours)))
        rows = self._db.execute("SELECT payload FROM samples WHERE server_id=? AND timestamp>=? ORDER BY timestamp DESC LIMIT 50000", (server_id, time.time() - hours * 3600)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def status(self) -> dict:
        self._open()
        result = dict(self._last)
        if not result:
            for server in self.settings.data.get("servers", []):
                row = self._db.execute("SELECT payload FROM samples WHERE server_id=? ORDER BY timestamp DESC LIMIT 1", (server["id"],)).fetchone()
                if row:
                    result[server["id"]] = json.loads(row[0])
        return {"running": bool(self._task and not self._task.done()), "interval_seconds": self.settings.data.get("monitor", {}).get("interval_seconds", 60), "latest": result}

    async def render(self, server_id: str, hours: float = 24) -> str:
        rows = self.history(server_id, hours)
        path = self.directory / "charts"
        path.mkdir(exist_ok=True)
        filename = path / f"metrics-{server_id}.png"
        async with self._chart_lock:
            await asyncio.to_thread(self._render_chart, server_id, rows, filename)
        return str(filename)

    def _render_chart(self, server_id: str, rows: list[dict], filename: Path):
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from matplotlib.dates import DateFormatter
        figure = Figure(figsize=(11, 8), dpi=130, facecolor="#f4f7fc")
        FigureCanvasAgg(figure)
        zone = ZoneInfo(self.settings.data.get("timezone", "Asia/Shanghai"))
        times = [datetime.fromtimestamp(row["timestamp"], zone) for row in rows]
        panels = [("tps", "TPS (reported rolling window)", "ticks/s", "#4264d8"),
                  ("mspt", "Tick duration (MSPT / 50)", "ticks (1 tick = 50 ms)", "#a04bb4"),
                  ("player_ping_ms", "Player ping (server-reported)", "ms", "#d37a20"),
                  ("slp_rtt_ms", "Bot to server SLP RTT", "ms", "#168b7e")]
        for index, (key, title, unit, color) in enumerate(panels, 1):
            ax = figure.add_subplot(2, 2, index)
            ax.set_title(title, fontsize=10, loc="left")
            ax.set_ylabel(unit, fontsize=9)
            ax.set_facecolor("white")
            ax.grid(alpha=0.15)
            scale = 50 if key == "mspt" else 1
            values = [row[key] / scale if row.get(key) is not None else float("nan") for row in rows]
            if rows:
                ax.plot(times, values, color=color, linewidth=1.6, marker="." if len(rows) < 20 else None)
                ax.xaxis.set_major_formatter(DateFormatter("%m-%d\n%H:%M", tz=zone))
            if not any(row.get(key) is not None for row in rows):
                ax.text(0.5, 0.5, "No valid samples", ha="center", va="center", transform=ax.transAxes, color="#667788")
            ax.tick_params(labelsize=8)
            ax.set_ylim(bottom=0)
        figure.suptitle(f"Yuma MCQQ | {server_id} | {self.settings.data.get('timezone', 'Asia/Shanghai')}", fontsize=13, x=0.06, ha="left")
        figure.text(0.06, 0.025, "MSPT chart: 1 tick = 50 ms. Gaps indicate missing data. Player ping and bot RTT use different paths.", fontsize=8, color="#627086")
        figure.subplots_adjust(top=0.90, bottom=0.12, left=0.08, right=0.97, hspace=0.42, wspace=0.28)
        figure.savefig(filename, facecolor=figure.get_facecolor())
