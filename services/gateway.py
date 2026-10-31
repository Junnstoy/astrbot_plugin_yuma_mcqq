"""QueQiao V2 transport. Reconnection never replays requests."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote_plus, unquote_plus

import aiohttp
from aiohttp import web

log = logging.getLogger(__name__)


class GatewayError(RuntimeError):
    """The remote API explicitly rejected a request."""


class GatewayUnavailable(GatewayError):
    """The command was not sent because no connection was available."""


class OutcomeUnknown(GatewayError):
    """A request may have executed; do not automatically retry mutations."""


@dataclass
class _Connection:
    config: dict
    ws: aiohttp.ClientWebSocketResponse | web.WebSocketResponse | None = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    task: asyncio.Task | None = None
    worker: asyncio.Task | None = None
    connected_at: float | None = None
    last_event_at: float | None = None
    last_error: str = ""
    reconnects: int = 0
    dropped_events: int = 0
    generation: str = field(default_factory=lambda: uuid.uuid4().hex)
    attach_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Gateway:
    def __init__(self, settings, on_event):
        self.settings = settings
        self.on_event = on_event
        self._connections: dict[str, _Connection] = {}
        self._session: aiohttp.ClientSession | None = None
        self._closing = True
        self._listeners: dict[tuple[str, int], tuple[web.AppRunner, web.TCPSite]] = {}

    async def start(self) -> None:
        if not self._closing:
            return
        self._closing = False
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        reverse_groups: dict[tuple[str, int], list[_Connection]] = {}
        try:
            for server in self.settings.data.get("servers", []):
                if not server.get("enabled", True):
                    continue
                connection = _Connection(dict(server))
                self._connections[str(server["id"])] = connection
                mode = server.get("connection_mode", "forward")
                if mode == "reverse":
                    if not server.get("token"):
                        raise ValueError("反向 WebSocket 必须配置非空 access token")
                    key = (str(server.get("reverse_host", "0.0.0.0")), int(server.get("reverse_port", 8081)))
                    reverse_groups.setdefault(key, []).append(connection)
                elif mode == "forward":
                    connection.task = asyncio.create_task(self._connect(connection), name=f"yuma-ws-{server['id']}")
                else:
                    raise ValueError("连接方向必须是 forward 或 reverse")
                connection.worker = asyncio.create_task(self._events(connection), name=f"yuma-events-{server['id']}")
            for key, connections in reverse_groups.items():
                application = web.Application(client_max_size=2 * 1024 * 1024)

                async def accept(request, targets=tuple(connections)):
                    return await self._accept_reverse(request, targets)

                application.router.add_get("/{path:.*}", accept)
                runner = web.AppRunner(application, access_log=None, shutdown_timeout=5)
                await runner.setup()
                site = web.TCPSite(runner, *key)
                self._listeners[key] = (runner, site)
                await site.start()
        except BaseException:
            await self.close()
            raise

    async def reload(self) -> None:
        await self.close()
        await self.start()

    async def close(self) -> None:
        self._closing = True
        for _, site in self._listeners.values():
            await site.stop()
        tasks = []
        for connection in self._connections.values():
            for task in (connection.task, connection.worker):
                if task and task is not asyncio.current_task():
                    task.cancel()
                    tasks.append(task)
            self._reject_pending(connection, "连接停止，已发送请求的结果需核对")
            if connection.ws is not None:
                await connection.ws.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connections.clear()
        for runner, _ in self._listeners.values():
            await runner.cleanup()
        self._listeners.clear()
        if self._session:
            await self._session.close()
            self._session = None

    async def _accept_reverse(self, request: web.Request, connections: tuple[_Connection, ...]):
        if self._closing:
            raise web.HTTPServiceUnavailable(text="Gateway stopping")
        matching_path = [c for c in connections if c.config.get("reverse_path", "/queqiao") == request.path]
        if not matching_path:
            raise web.HTTPNotFound(text="Unknown endpoint")
        if len(request.headers.getall("x-self-name", [])) != 1:
            raise web.HTTPUnauthorized(text="Missing server identity")
        name = unquote_plus(request.headers["x-self-name"])
        connection = next((c for c in matching_path if c.config["name"] == name), None)
        if connection is None:
            raise web.HTTPUnauthorized(text="Invalid server identity")
        if request.headers.get("x-client-origin", "").lower() in {"astrbot", "astrbot_plugin_yuma_mcqq"}:
            raise web.HTTPForbidden(text="Bot loopback is not allowed")
        authorizations = request.headers.getall("Authorization", [])
        expected = "Bearer " + str(connection.config["token"])
        if len(authorizations) != 1 or not hmac.compare_digest(authorizations[0].encode(), expected.encode()):
            raise web.HTTPUnauthorized(text="Invalid authorization")
        async with connection.attach_lock:
            if connection.ws is not None and not connection.ws.closed:
                raise web.HTTPConflict(text="Server is already connected")
            ws = web.WebSocketResponse(heartbeat=20, max_msg_size=2 * 1024 * 1024)
            await ws.prepare(request)
            if self._closing:
                await ws.close(code=1001, message=b"Gateway stopping")
                return ws
            if connection.connected_at is not None:
                connection.reconnects += 1
            self._attach(connection, ws)
            connection.task = asyncio.current_task()
        try:
            await self._read(connection, ws)
        finally:
            self._detach(connection, ws)
            if connection.task is asyncio.current_task():
                connection.task = None
            await ws.close()
        return ws

    @staticmethod
    def _attach(connection: _Connection, ws) -> None:
        connection.ws = ws
        connection.generation = uuid.uuid4().hex
        connection.connected_at = time.time()
        connection.last_error = ""

    def _detach(self, connection: _Connection, ws) -> None:
        if connection.ws is ws:
            connection.ws = None
            self._reject_pending(connection, "连接中断，服务器可能已执行该请求，请核对结果")

    async def _read(self, connection: _Connection, ws) -> None:
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(message.data)
                except (ValueError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                server_name = payload.get("server_name")
                if server_name and server_name != connection.config["name"]:
                    continue
                if payload.get("post_type") == "response" or ("echo" in payload and "api" in payload):
                    future = connection.pending.get(str(payload.get("echo", "")))
                    if future is not None and not future.done():
                        future.set_result(payload)
                else:
                    # Event callbacks can request RCON; they must not block this reader.
                    connection.last_event_at = time.time()
                    try:
                        connection.queue.put_nowait(payload)
                    except asyncio.QueueFull:
                        connection.dropped_events += 1
            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                break

    @staticmethod
    def _reject_pending(connection: _Connection, message: str) -> None:
        for future in list(connection.pending.values()):
            if not future.done():
                future.set_exception(OutcomeUnknown(message))
        connection.pending.clear()

    async def _connect(self, connection: _Connection) -> None:
        config = connection.config
        backoff = 1.0
        while not self._closing:
            ws = None
            try:
                headers = {"x-self-name": quote_plus(str(config["name"])), "x-client-origin": "astrbot_plugin_yuma_mcqq"}
                if config.get("token"):
                    headers["Authorization"] = "Bearer " + str(config["token"])
                assert self._session is not None
                async with self._session.ws_connect(config["url"], headers=headers, heartbeat=20, max_msg_size=2 * 1024 * 1024) as ws:
                    self._attach(connection, ws)
                    backoff = 1.0
                    await self._read(connection, ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                connection.last_error = type(exc).__name__
                log.warning("[YumaMCQQ][%s] connection unavailable: %s", config["id"], type(exc).__name__)
            finally:
                self._detach(connection, ws)
            if not self._closing:
                connection.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(60.0, backoff * 2)

    async def _events(self, connection: _Connection) -> None:
        while not self._closing:
            payload = await connection.queue.get()
            try:
                await self.on_event(str(connection.config["id"]), payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("[YumaMCQQ][%s] event failed: %s", connection.config["id"], type(exc).__name__)
            finally:
                connection.queue.task_done()

    async def request(self, server_id: str, api: str, data: dict | None = None, timeout: float | None = None) -> dict:
        connection = self._connections.get(str(server_id))
        if self._closing or not connection or connection.ws is None or connection.ws.closed:
            raise GatewayUnavailable("游戏服务器未连接，指令未发送")
        if len(connection.pending) >= 128:
            raise GatewayUnavailable("服务器请求过多，请稍后再试；本次指令未发送")
        echo = f"{connection.generation}:{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        connection.pending[echo] = future
        packet = {"api": api, "data": data or {}, "echo": echo}
        sent = False
        try:
            sent = True  # A failed send can still have reached the peer.
            await connection.ws.send_json(packet)
            response = await asyncio.wait_for(asyncio.shield(future), timeout=float(timeout or connection.config.get("timeout", 12)))
            if response.get("api") != api:
                raise OutcomeUnknown("回包接口与请求不一致，结果待核对")
            code = str(response.get("code", ""))
            status = str(response.get("status", "")).upper()
            if not code or not status:
                raise OutcomeUnknown("回包缺少状态字段，执行结果待核对")
            if code != "200" or status != "SUCCESS":
                if api == "send_rcon_command":
                    if code == "503" and status == "FAILED":
                        raise GatewayUnavailable("QueQiao 的 RCON 未启用或未连接，命令未执行")
                    # QueQiao maps a socket/read failure after dispatch to an API error.
                    # Older releases may also use 400 for these failures; an error packet
                    # alone is therefore not evidence that a reward was never delivered.
                    raise OutcomeUnknown("QueQiao 报告 RCON 异常，命令可能已执行，请核对结果")
                raise GatewayError(str(response.get("message") or "服务器拒绝了请求")[:500])
            return response
        except asyncio.TimeoutError as exc:
            raise OutcomeUnknown("服务器响应超时，可能已经执行，请核对结果") from exc
        except (GatewayError, asyncio.CancelledError):
            raise
        except Exception as exc:
            if sent:
                raise OutcomeUnknown("请求发送后连接异常，执行结果待核对") from exc
            raise GatewayUnavailable("无法发送请求") from exc
        finally:
            connection.pending.pop(echo, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Retrieve a simultaneously delivered disconnect exception.
                future.exception()

    async def rcon(self, server_id: str, command: str) -> str:
        command = command.strip().lstrip("/")
        if not command or any(char in command for char in ("\n", "\r", "\x00")):
            raise ValueError("每次只能执行一条非空 RCON 指令")
        if len(command.encode("utf-8")) > 1446:
            raise ValueError("RCON 指令超过 1446 字节")
        response = await self.request(server_id, "send_rcon_command", {"command": command})
        result = response.get("data")
        if not isinstance(result, str):
            raise OutcomeUnknown("RCON 成功回包未包含文本结果，请核对执行结果")
        return result

    async def broadcast(self, server_id: str, components) -> dict:
        return await self.request(server_id, "broadcast", {"message": components})

    def status(self) -> list[dict]:
        return [{"id": server_id, "name": connection.config.get("name", server_id),
                 "connection_mode": connection.config.get("connection_mode", "forward"),
                 "reverse_host": connection.config.get("reverse_host", "0.0.0.0") if connection.config.get("connection_mode") == "reverse" else None,
                 "reverse_port": connection.config.get("reverse_port", 8081) if connection.config.get("connection_mode") == "reverse" else None,
                 "reverse_path": connection.config.get("reverse_path", "/queqiao") if connection.config.get("connection_mode") == "reverse" else None,
                 "connected": bool(connection.ws is not None and not connection.ws.closed),
                 "connected_at": connection.connected_at, "last_event_at": connection.last_event_at,
                 "last_error": connection.last_error, "pending": len(connection.pending),
                 "reconnects": connection.reconnects, "dropped_events": connection.dropped_events}
                for server_id, connection in self._connections.items()]
