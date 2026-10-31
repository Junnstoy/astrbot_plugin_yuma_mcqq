"""Bounded media cache with expiring, read-only capability URLs."""
from __future__ import annotations

import asyncio
import base64
import binascii
import html
import ipaddress
import json
import mimetypes
import secrets
import socket
import sqlite3
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
from aiohttp import web


class MediaUnavailable(RuntimeError):
    pass


class MediaRejected(ValueError):
    pass


class _PublicResolver(aiohttp.abc.AbstractResolver):
    """Validate every DNS answer at connection time (not only before DNS lookup)."""
    async def resolve(self, host, port=0, family=socket.AF_INET):
        addresses = await asyncio.get_running_loop().getaddrinfo(host, port, family=family, type=socket.SOCK_STREAM)
        result = []
        for fam, _, proto, _, address in addresses:
            ip = address[0]
            if not ipaddress.ip_address(ip).is_global:
                raise MediaRejected("媒体地址解析到非公网地址")
            result.append({"hostname": host, "host": ip, "port": port, "family": fam, "proto": proto, "flags": socket.AI_NUMERICHOST})
        return result

    async def close(self):
        return None


class MediaService:
    def __init__(self, data_dir, settings):
        self.settings = settings
        self.directory = Path(data_dir) / "media"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._db = None
        self._open()
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.last_error = ""

    def _open(self):
        if self._db is None:
            self._db = sqlite3.connect(self.directory / "index.sqlite3")
            self._db.row_factory = sqlite3.Row
            self._db.execute("CREATE TABLE IF NOT EXISTS assets(token TEXT PRIMARY KEY, filename TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, expires REAL NOT NULL)")
            self._db.commit()

    @property
    def config(self) -> dict:
        return self.settings.data.get("media", {})

    @property
    def base(self) -> str:
        return str(self.config.get("public_base", "")).rstrip("/")

    async def start(self) -> None:
        self._open()
        if self._runner:
            return
        if not self.config.get("enabled", False):
            return
        if not self.base:
            self.last_error = "未配置玩家可访问的媒体 public_base"
            return
        parsed = urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            self.last_error = "媒体 public_base 必须是无凭据的 HTTP(S) 基础地址"
            return
        self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=_PublicResolver()), timeout=aiohttp.ClientTimeout(total=30))
        application = web.Application(client_max_size=1024)
        application.router.add_get("/m/{token}", self._serve)
        application.router.add_get("/v/{token}", self._view)
        self._runner = web.AppRunner(application, access_log=None)
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, str(self.config.get("host", "127.0.0.1")), int(self.config.get("port", 8766))).start()
        except Exception:
            await self._runner.cleanup()
            self._runner = None
            await self._session.close()
            self._session = None
            self.last_error = "媒体服务启动失败，请检查监听地址与端口"
            return
        self.last_error = ""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="yuma-media-expiry")

    async def _stop_http(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._session:
            await self._session.close()
            self._session = None

    async def reload(self) -> None:
        await self._stop_http()
        await self.start()

    async def close(self) -> None:
        await self._stop_http()
        if self._db is not None:
            self._db.close()
            self._db = None

    def status(self) -> dict:
        if self._db is None:
            return {"running": False, "last_error": self.last_error, "assets": 0, "bytes": 0}
        count, size = self._db.execute("SELECT count(*),coalesce(sum(size),0) FROM assets").fetchone()
        return {"running": self._runner is not None, "last_error": self.last_error, "assets": count, "bytes": size}

    def _ready(self) -> None:
        if not self.config.get("enabled", False) or not self._runner or not self.base:
            raise MediaUnavailable(self.last_error or "媒体链接服务未开启")

    def _check_url(self, source: str) -> str:
        if any(ord(char) < 32 for char in source):
            raise MediaRejected("媒体地址包含控制字符")
        parsed = urlsplit(source)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise MediaRejected("只允许无凭据的 HTTP(S) 媒体地址")
        host = parsed.hostname.lower().rstrip(".")
        allowed = {str(value).lower().rstrip(".") for value in self.config.get("allowed_hosts", [])}
        if allowed and host not in allowed:
            raise MediaRejected("媒体域名不在允许列表中")
        if host in ("localhost", "localhost.localdomain") or host.endswith((".localhost", ".local", ".internal", ".lan")):
            raise MediaRejected("不允许本地网络媒体地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise MediaRejected("不允许非公网媒体地址")
        return source

    def public_source_url(self, source: str) -> str:
        """A QQ-provided public URL can be clicked without running a proxy/cache.

        This performs no HTTP request or file read. Proxy downloads still validate
        every DNS result and redirect separately through _PublicResolver.
        """
        source = str(source).strip()
        if source.startswith("//"):
            source = "https:" + source
        elif source.lower().startswith("www."):
            source = "https://" + source
        return self._check_url(source)

    @staticmethod
    def _mime(content: bytes, suggested: str, name: str = "") -> str:
        # NapCat and QQ sometimes label image/video bodies as octet-stream.
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            return "image/webp"
        if content.startswith(b"RIFF") and content[8:12] == b"WAVE":
            return "audio/wav"
        if content.startswith(b"OggS"):
            return "audio/ogg"
        if content.startswith(b"ID3") or (len(content) > 1 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0):
            return "audio/mpeg"
        if len(content) > 12 and content[4:8] == b"ftyp":
            return "audio/mp4" if content[8:12] in (b"M4A ", b"M4B ") else "video/mp4"
        return suggested or mimetypes.guess_type(name)[0] or "application/octet-stream"

    async def _download(self, source: str) -> tuple[bytes, str]:
        if not self._session:
            raise MediaUnavailable("媒体服务未启动")
        limit = int(self.config.get("max_bytes", 20 * 1024 * 1024))
        current = source
        for _ in range(4):
            self._check_url(current)
            async with self._session.get(current, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location", "")
                    if not location:
                        raise MediaRejected("媒体重定向无目标")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                if response.content_length is not None and response.content_length > limit:
                    raise MediaRejected("媒体超过大小限制")
                content = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    content.extend(chunk)
                    if len(content) > limit:
                        raise MediaRejected("媒体超过大小限制")
                return bytes(content), response.headers.get("Content-Type", "").split(";")[0]
        raise MediaRejected("媒体重定向次数过多")

    def _local_path(self, source: str) -> Path:
        if source.startswith(("\\\\", "//")):
            raise MediaRejected("不允许网络共享文件")
        if source.startswith("file://"):
            parsed = urlsplit(source)
            if parsed.netloc and parsed.netloc not in ("localhost",):
                raise MediaRejected("不允许网络共享文件")
            value = unquote(parsed.path)
            if len(value) > 3 and value[0] == "/" and value[2] == ":":
                value = value[1:]
            path = Path(value).resolve(strict=True)
        else:
            path = Path(source).resolve(strict=True)
        roots = [Path(value).resolve() for value in self.config.get("trusted_local_roots", [])]
        if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
            raise MediaRejected("文件不在管理员配置的可信媒体目录中")
        if path.stat().st_size > int(self.config.get("max_bytes", 20 * 1024 * 1024)):
            raise MediaRejected("文件超过大小限制")
        return path

    async def publish(self, source: str, kind: str, name: str = "") -> str:
        """Explicitly cache a source; callers wanting a direct link use public_source_url."""
        self._ready()
        if kind not in ("image", "record", "audio", "video", "file"):
            raise MediaRejected("不支持的媒体类型")
        if source.startswith("base64://"):
            encoded = source[9:]
            maximum = int(self.config.get("max_bytes", 20 * 1024 * 1024))
            if len(encoded) > ((maximum + 2) // 3) * 4:
                raise MediaRejected("媒体超过大小限制")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise MediaRejected("无效的媒体 Base64") from exc
            mime = ""
        elif source.startswith(("http://", "https://")):
            content, mime = await self._download(source)
            name = name or Path(unquote(urlsplit(source).path)).name
        else:
            path = self._local_path(source)
            content = await asyncio.to_thread(path.read_bytes)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            name = name or path.name
        if len(content) > int(self.config.get("max_bytes", 20 * 1024 * 1024)):
            raise MediaRejected("文件超过大小限制")
        mime = self._mime(content, mime, name)
        token = await self._store(content, kind, name or kind, mime)
        endpoint = "v" if kind in ("record", "audio", "video") else "m"
        return f"{self.base}/{endpoint}/{token}"

    async def publish_record(self, nodes: list) -> str:
        self._ready()
        content = []
        for node in nodes[:100]:
            author = html.escape(str(node.get("author", "未知发送者")))
            text = html.escape(str(node.get("text", ""))[:4000])
            links = []
            for item in node.get("links", [])[:20]:
                url = str(item.get("url", ""))
                if urlsplit(url).scheme in ("http", "https"):
                    links.append(f'<a rel="noreferrer noopener" href="{html.escape(url, quote=True)}">{html.escape(str(item.get("label", "附件")))}</a>')
            content.append(f"<article><strong>{author}</strong><p>{text}</p>{' '.join(links)}</article>")
        body = self._page("合并消息", "".join(content))
        token = await self._store(body.encode("utf-8"), "record_page", "合并消息.html", "text/html; charset=utf-8")
        return f"{self.base}/v/{token}"

    async def _store(self, content: bytes, kind: str, name: str, mime: str) -> str:
        async with self._lock:
            self.cleanup()
            maximum = int(self.config.get("max_cache_bytes", 512 * 1024 * 1024))
            total = self._db.execute("SELECT coalesce(sum(size),0) FROM assets").fetchone()[0]
            if total + len(content) > maximum:
                raise MediaRejected("媒体缓存已满，请等待过期清理或调整容量")
            token = secrets.token_urlsafe(32)
            path = self.directory / token
            await asyncio.to_thread(path.write_bytes, content)
            expires = time.time() + int(self.config.get("ttl_seconds", 86400))
            safe_name = Path(name.replace("\\", "/")).name[:180].replace("\r", "").replace("\n", "")
            self._db.execute("INSERT INTO assets VALUES(?,?,?,?,?,?,?)", (token, token, safe_name, kind, mime, len(content), expires))
            self._db.commit()
            return token

    def cleanup(self) -> None:
        rows = self._db.execute("SELECT token,filename FROM assets WHERE expires<=?", (time.time(),)).fetchall()
        for row in rows:
            path = self.directory / row["filename"]
            if path.parent == self.directory:
                path.unlink(missing_ok=True)
        self._db.execute("DELETE FROM assets WHERE expires<=?", (time.time(),))
        self._db.commit()

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self.cleanup()

    def _asset(self, token: str):
        if len(token) > 80:
            raise web.HTTPNotFound()
        row = self._db.execute("SELECT * FROM assets WHERE token=? AND expires>?", (token, time.time())).fetchone()
        if not row or not (self.directory / row["filename"]).is_file():
            raise web.HTTPNotFound(text="链接已过期或不存在")
        return row

    @staticmethod
    def _headers() -> dict:
        return {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'; frame-ancestors 'none'"}

    async def _serve(self, request: web.Request):
        row = self._asset(request.match_info["token"])
        if row["kind"] == "record_page":
            raise web.HTTPNotFound()
        headers = self._headers()
        safe_inline = {"image/png", "image/jpeg", "image/gif", "image/webp", "audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav", "audio/ogg", "video/mp4", "video/webm"}
        mime = row["mime"] if row["mime"] in safe_inline else "application/octet-stream"
        headers["Content-Type"] = mime
        if row["kind"] == "file" or mime == "application/octet-stream":
            from urllib.parse import quote
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(row["name"])
        return web.FileResponse(self.directory / row["filename"], headers=headers)

    async def _view(self, request: web.Request):
        row = self._asset(request.match_info["token"])
        if row["kind"] == "record_page":
            body = (self.directory / row["filename"]).read_text("utf-8")
        else:
            tag = "video" if row["kind"] == "video" else "audio"
            url = "../m/" + row["token"]
            body = self._page(row["name"], f'<{tag} controls preload="metadata" src="{url}"></{tag}><p><a href="{url}">下载文件</a></p>')
        return web.Response(text=body, content_type="text/html", headers=self._headers())

    @staticmethod
    def _page(title: str, content: str) -> str:
        return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(title) + '</title><style>body{font:16px system-ui;max-width:850px;margin:40px auto;padding:0 20px;color:#233047;background:#f5f7fb}article{background:white;border-radius:12px;padding:18px;margin:14px 0}p{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#315ccb}video,audio{max-width:100%;width:100%}</style><h1>' + html.escape(title) + "</h1>" + content + "</html>"
