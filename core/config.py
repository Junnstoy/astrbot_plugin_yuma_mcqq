"""Validated, atomic configuration; the GUI and runtime share this source."""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .text import template_fields

MASK = "********"
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
DEFAULTS = {
    "schema_version": 3, "binding_verification_enabled": False,
    "points_name": "点数", "command_prefix": "",
    "enabled": True, "timezone": "Asia/Shanghai", "binding_cooldown_seconds": 604800,
    "servers": [], "routes": [], "commands": [],
    "media": {"enabled": False, "host": "127.0.0.1", "port": 8766,
              "public_base": "", "ttl_seconds": 86400, "max_bytes": 26214400,
              "max_cache_bytes": 536870912, "trusted_local_roots": [], "allowed_hosts": []},
    "monitor": {"enabled": True, "interval_seconds": 60, "retention_days": 7},
}
SERVER_DEFAULTS = {"name": "", "url": "ws://127.0.0.1:8080", "token": "", "enabled": True,
                   "display_name": "", "connection_mode": "forward", "reverse_host": "0.0.0.0",
                   "reverse_port": 8081, "reverse_path": "/queqiao",
                   "timeout": 10, "game_host": "127.0.0.1", "game_port": 25565,
                   "tps_command": "tps", "mspt_command": "mspt", "ping_command": "ping",
                   "component_version": "legacy"}
ROUTE_DEFAULTS = {"enabled": True, "qq_to_mc": True, "mc_to_qq": True, "admins": [],
                  "display_name": "",
                  "points_min": 10, "points_max": 10, "timezone": "Asia/Shanghai",
                  "signin_command": "signin click {player}", "templates": {},
                  "signin_prefixes": ["[手记人偶]"], "signin_rules": [],
                  "events": ["PlayerChatEvent", "PlayerJoinEvent", "PlayerQuitEvent", "PlayerDeathEvent", "PlayerAchievementEvent"],
                  "types": ["text", "face", "image", "link", "record", "video", "file", "forward", "reply", "at"],
                  "prefix": "[{group}] {sender}：", "max_forward_nodes": 30}
COMMAND_DEFAULTS = {"aliases": [], "description": "自定义服务器命令", "enabled": True,
                    "require_binding": True, "admin_only": True, "cooldown_seconds": 3,
                    "server_id": "", "parser": "rules", "cleanup_prefixes": [], "rules": [],
                    "parameters": [], "templates": {"SUCCESS": "{cleaned}", "UNKNOWN": "服务器返回了未识别的结果，请管理员检查命令规则。"}}


def _number(value, low, high, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or (integer and not isinstance(value, int)):
        raise ValueError(f"{name} 必须在 {low}–{high} 之间")


def _timezone(value):
    try:
        ZoneInfo(str(value))
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("无效的 IANA 时区") from exc


def _booleans(mapping, *keys):
    for key in keys:
        if not isinstance(mapping.get(key), bool):
            raise ValueError(f"{key} 必须为布尔值")


def validate(data: dict) -> dict:
    if not isinstance(data, dict):
        raise ValueError("配置必须是对象")
    data = copy.deepcopy(data)
    _booleans(data, "enabled", "binding_verification_enabled")
    if data.get("schema_version") != 3:
        raise ValueError("不支持的配置版本")
    if not isinstance(data["points_name"], str) or not data["points_name"].strip() or len(data["points_name"]) > 20 or any(ord(c) < 32 or c in "{}" for c in data["points_name"]):
        raise ValueError("点数名称须为 1–20 个字符，不能包含控制字符或花括号")
    data["points_name"] = data["points_name"].strip()
    if not isinstance(data["command_prefix"], str) or len(data["command_prefix"]) > 20 or any(c.isspace() or ord(c) < 32 for c in data["command_prefix"]):
        raise ValueError("插件唤醒前缀应不超过 20 字符，不能含空格或控制字符；留空沿用 AstrBot")
    _timezone(data["timezone"])
    for key in ("servers", "routes", "commands"):
        if not isinstance(data.get(key), list) or not all(isinstance(x, dict) for x in data[key]):
            raise ValueError(f"{key} 必须为对象数组")
    for key in ("media", "monitor"):
        if not isinstance(data.get(key), dict):
            raise ValueError(f"{key} 必须为对象")
    _number(data["binding_cooldown_seconds"], 0, 31536000, "换绑冷却")
    ids = set()
    names = set()
    for server in data["servers"]:
        for key, value in SERVER_DEFAULTS.items():
            server.setdefault(key, copy.deepcopy(value))
        _booleans(server, "enabled")
        sid = server.get("id", "")
        if not IDENTIFIER.fullmatch(str(sid)) or sid in ids:
            raise ValueError("服务器 ID 无效或重复")
        ids.add(sid)
        server["name"] = server["name"] or sid
        if not isinstance(server["display_name"], str) or len(server["display_name"]) > 100 or any(ord(c) < 32 for c in server["display_name"]):
            raise ValueError("服务器显示名称应为不超过100字符的单行文字")
        if server["name"] in names:
            raise ValueError("QueQiao server_name 不能重复")
        names.add(server["name"])
        url = urlsplit(server["url"])
        if server["connection_mode"] == "forward" and (url.scheme not in {"ws", "wss"} or not url.hostname or url.username or url.password):
            raise ValueError("QueQiao URL 必须是 ws/wss 地址，凭据请使用 token 字段")
        if any(c in server["token"] + server["name"] for c in "\r\n"):
            raise ValueError("连接字段不能包含换行")
        _number(server["timeout"], 1, 120, "请求超时")
        _number(server["game_port"], 1, 65535, "游戏端口", integer=True)
        if server["connection_mode"] not in {"forward", "reverse"}:
            raise ValueError("连接方式应为 forward / reverse")
        _number(server["reverse_port"], 1, 65535, "反向监听端口", integer=True)
        if not isinstance(server["reverse_host"], str) or not server["reverse_host"].strip() or any(c.isspace() for c in server["reverse_host"]):
            raise ValueError("反向监听地址不能为空或包含空白")
        if not isinstance(server["reverse_path"], str) or not re.fullmatch(r"/[A-Za-z0-9/_-]*", server["reverse_path"]):
            raise ValueError("反向连接路径应以 / 开头，只包含字母、数字、斜杠、下划线或横杠")
        if server["connection_mode"] == "reverse" and not server["token"].strip():
            raise ValueError("反向连接必须设置 Access Token")
        if server["component_version"] not in {"legacy", "modern"}:
            raise ValueError("聊天组件版本应为 legacy / modern")
    umos = set()
    scopes = {}
    for route in data["routes"]:
        for key, value in ROUTE_DEFAULTS.items():
            route.setdefault(key, copy.deepcopy(value))
        _booleans(route, "enabled", "qq_to_mc", "mc_to_qq")
        if not isinstance(route["display_name"], str) or len(route["display_name"]) > 100:
            raise ValueError("群显示名称不超过100字符")
        for key in ("events", "types"):
            if not isinstance(route[key], list) or not all(isinstance(v, str) for v in route[key]):
                raise ValueError(f"{key} 必须为字符串列表")
        if set(route["events"]) - set(ROUTE_DEFAULTS["events"]):
            raise ValueError("游戏事件类型无效")
        if set(route["types"]) - set(ROUTE_DEFAULTS["types"]):
            raise ValueError("转发消息类型无效")
        umo = route.get("umo", "")
        if not isinstance(umo, str) or umo.count(":") < 2 or len(umo) > 512 or umo in umos:
            raise ValueError("请填写 AstrBot /sid 给出的完整且不重复的 UMO")
        umos.add(umo)
        route.setdefault("scope", umo)
        if not route["scope"] or len(route["scope"]) > 512:
            raise ValueError("点数作用域不能为空")
        if route.get("server_id") not in ids:
            raise ValueError("会话引用了不存在的服务器")
        _timezone(route["timezone"])
        _number(route["points_min"], 0, 1000000, "签到最小点数", integer=True)
        _number(route["points_max"], route["points_min"], 1000000, "签到最大点数", integer=True)
        policy = (route["timezone"], route["points_min"], route["points_max"])
        if route["scope"] in scopes and scopes[route["scope"]] != policy:
            raise ValueError("共用点数作用域的路由必须使用相同时区和签到奖励范围")
        scopes[route["scope"]] = policy
        _number(route["max_forward_nodes"], 1, 100, "合并消息节点上限", integer=True)
        if not isinstance(route["admins"], list) or not all(isinstance(a, str) and a.isdigit() for a in route["admins"]):
            raise ValueError("会话管理员应为 QQ ID 字符串列表")
        fields = template_fields(route["signin_command"])
        if not fields <= {"player", "player_name", "user_id", "date"}:
            raise ValueError("签到命令包含未知占位符")
        for template in route["templates"].values():
            template_fields(template)
        if not isinstance(route["signin_rules"], list) or not isinstance(route["signin_prefixes"], list):
            raise ValueError("签到规则与前缀必须为数组")
        for rule in route["signin_rules"]:
            if not isinstance(rule, dict) or rule.get("status") not in {"SIGNED", "ALREADY_SIGNED", "FAILED"} or len(rule.get("pattern", "")) > 2048:
                raise ValueError("签到规则状态应为 SIGNED / ALREADY_SIGNED / FAILED")
            import regex
            try:
                compiled = regex.compile(rule["pattern"])
            except (regex.error, KeyError) as exc:
                raise ValueError("签到匹配表达式无效") from exc
            if rule["status"] in {"SIGNED", "ALREADY_SIGNED"} and "player" not in compiled.groupindex:
                raise ValueError("签到成功/重复规则必须包含命名捕获 player，用于核对目标账号")
    command_ids = set()
    triggers = set()
    for command in data["commands"]:
        for key, value in COMMAND_DEFAULTS.items():
            command.setdefault(key, copy.deepcopy(value))
        _booleans(command, "enabled", "require_binding", "admin_only")
        for key in ("aliases", "cleanup_prefixes", "rules", "parameters"):
            if not isinstance(command[key], list):
                raise ValueError(f"命令 {key} 必须为数组")
        cid, name = command.get("id", ""), command.get("name", "")
        if not IDENTIFIER.fullmatch(str(cid)) or cid in command_ids:
            raise ValueError("命令 ID 无效或重复")
        command_ids.add(cid)
        for trigger in [name] + command["aliases"]:
            if not isinstance(trigger, str) or not trigger.strip() or len(trigger) > 40 or trigger in triggers or any(c.isspace() for c in trigger):
                raise ValueError("触发名/别名不能为空、重复或包含空白")
            triggers.add(trigger)
        if command["server_id"] and command["server_id"] not in ids:
            raise ValueError("命令引用了不存在的服务器")
        _number(command["cooldown_seconds"], 0, 86400, "命令冷却")
        if command["parser"] not in {"rules", "text", "litesignin"}:
            raise ValueError("未知解析模式")
        template_fields(command.get("rcon", ""))
        if not command.get("rcon") or any(c in command["rcon"] for c in "\r\n"):
            raise ValueError("RCON 命令必须为单行")
        for rule in command["rules"]:
            if not rule.get("status") or len(rule.get("pattern", "")) > 2048:
                raise ValueError("结果规则需包含状态与不超过 2048 字符的表达式")
            import regex
            try:
                regex.compile(rule["pattern"])
            except regex.error as exc:
                raise ValueError(f"命令 {cid} 的匹配表达式无效：{exc}") from exc
        for template in command["templates"].values():
            template_fields(template)
        pnames = set()
        for param in command["parameters"]:
            if not isinstance(param, dict) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,63}", param.get("name", "")) or param["name"] in pnames or param["name"] in {"player", "player_name", "user_id", "date"}:
                raise ValueError("命令参数名无效、重复或占用保留字段")
            pnames.add(param["name"])
            if param.get("type", "word") not in {"word", "integer", "player", "enum"}:
                raise ValueError("参数类型应为 word / integer / player / enum")
            if param.get("type") == "integer":
                _number(param.get("min", -1000000000), -1000000000, 1000000000, "整数参数下限", integer=True)
                _number(param.get("max", 1000000000), param.get("min", -1000000000), 1000000000, "整数参数上限", integer=True)
            if param.get("type") == "enum" and (not isinstance(param.get("choices"), list) or not param["choices"] or not all(isinstance(v, str) for v in param["choices"])):
                raise ValueError("枚举参数必须包含非空字符串 choices 数组")
        if not template_fields(command["rcon"]) <= pnames | {"player", "player_name", "user_id", "date"}:
            raise ValueError("RCON 模板包含未定义的参数")
    for key, value in DEFAULTS["media"].items():
        data["media"].setdefault(key, copy.deepcopy(value))
    media = data["media"]
    _booleans(media, "enabled")
    _number(media["port"], 1, 65535, "媒体端口", integer=True)
    _number(media["ttl_seconds"], 60, 2592000, "媒体有效期")
    _number(media["max_bytes"], 1024, 1073741824, "媒体大小限制")
    _number(media["max_cache_bytes"], media["max_bytes"], 107374182400, "媒体缓存总上限")
    for key in ("trusted_local_roots", "allowed_hosts"):
        if not isinstance(media[key], list) or not all(isinstance(v, str) and v for v in media[key]):
            raise ValueError(f"{key} 必须为非空字符串数组")
    if media["public_base"]:
        u = urlsplit(media["public_base"])
        if u.scheme not in {"http", "https"} or not u.hostname or u.username:
            raise ValueError("媒体公开地址必须是 HTTP(S) 地址")
    for key, value in DEFAULTS["monitor"].items():
        data["monitor"].setdefault(key, value)
    _booleans(data["monitor"], "enabled")
    _number(data["monitor"]["interval_seconds"], 10, 86400, "采样间隔")
    _number(data["monitor"]["retention_days"], 1, 365, "历史保留天数")
    return data


class Settings:
    def __init__(self, path: str | Path, bootstrap: dict | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("schema_version", 1) < 2:
                for route in saved.get("routes", []):
                    if isinstance(route.get("types"), list) and "text" in route["types"] and "face" not in route["types"]:
                        route["types"].append("face")
                saved["schema_version"] = 2
            if saved.get("schema_version") == 2:
                # Migrate only the former shipped defaults, once. Custom query
                # commands and later explicit Spark choices remain untouched.
                for server in saved.get("servers", []):
                    for field, old, new in (("tps_command", "spark tps", "tps"),
                                            ("ping_command", "spark ping", "ping")):
                        if server.get(field) == old:
                            server[field] = new
                saved["schema_version"] = 3
            self.data.update(saved)
        elif bootstrap:
            self.data.update({k: v for k, v in bootstrap.items() if k in DEFAULTS})
        self.data = validate(self.data)

    def public(self) -> dict:
        result = copy.deepcopy(self.data)
        for server in result["servers"]:
            server["token"] = MASK if server["token"] else ""
        return result

    def save(self, patch: dict) -> dict:
        if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
            raise ValueError("包含未知配置字段")
        candidate = copy.deepcopy(self.data)
        candidate.update(copy.deepcopy(patch))
        old = {s["id"]: s for s in self.data["servers"]}
        for server in candidate["servers"]:
            if server.get("token") == MASK:
                if server.get("id") not in old:
                    raise ValueError("新服务器不能使用掩码作为 token")
                server["token"] = old[server["id"]]["token"]
        candidate = validate(candidate)
        fd, name = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(candidate, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.data = candidate
        return self.public()

    def server(self, server_id: str) -> dict:
        for server in self.data["servers"]:
            if server["id"] == server_id:
                return server
        raise ValueError("服务器不存在")

    def route(self, umo: str) -> dict | None:
        return next((r for r in self.data["routes"] if r["umo"] == umo), None)
