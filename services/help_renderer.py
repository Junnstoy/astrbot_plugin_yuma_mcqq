"""Permission-aware help models and paginated PNGs, independent of AstrBot."""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


GROUPS = (
    ("签到与绑定", ("signin", "binding", "binding_status", "rebind", "unbind")),
    ("点数与商店", ("balance", "rank", "shop", "purchase", "confirm", "reclaim", "orders")),
    ("服务器查询", ("status", "tps", "mspt", "ping", "performance")),
    ("管理员操作", ("points_add", "points_sub", "points_set", "order_reconcile", "rcon")),
    ("帮助", ("help",)),
)
ACTION_GROUP = {action: group for group, actions in GROUPS for action in actions}
DEFAULT_ORDER = {action: (index + 1) * 10 for index, action in enumerate(ACTION_GROUP)}


def help_catalog(effective: list[dict], custom_handlers: dict[str, dict], help_settings: dict,
                 builtin_descriptions: dict[str, str], admin_actions: set | frozenset) -> list[dict]:
    """Full editable catalog, including hidden, disabled and admin-only actions."""
    overrides = help_settings.get("entries", {})
    custom_order = {handler: 1000 + index * 10 for index, handler in enumerate(custom_handlers)}
    records = []
    for index, entry in enumerate(effective):
        handler = str(entry.get("handler_name", ""))
        custom = custom_handlers.get(handler)
        if custom is not None:
            action = "custom:" + str(custom["id"])
            kind, group = "custom", "自定义命令"
            default_order = custom_order[handler]
        else:
            action = handler.removeprefix("cmd_")
            kind, group = "builtin", ACTION_GROUP.get(action, "其他命令")
            default_order = DEFAULT_ORDER.get(action, 2000 + index * 10)
        name = str(entry.get("effective_command") or "").strip()
        if not action or not name:
            continue
        definition = custom or {}
        description = str(entry.get("description") or definition.get("description") or builtin_descriptions.get(action, "")).strip()
        original = str(entry.get("original_command") or "")
        if original:
            description = description.replace(original, name)
        override = overrides.get(action, {})
        aliases = list(dict.fromkeys(str(alias).strip() for alias in entry.get("aliases", []) if str(alias).strip() and str(alias).strip() != name))
        admin_only = (action in admin_actions or str(entry.get("permission", "")).lower() == "admin"
                      or (custom is not None and definition.get("admin_only", True)))
        records.append({
            "action_id": action, "name": name, "aliases": aliases,
            "description": str(override.get("description") or "").strip() or description,
            "default_description": description, "visible": override.get("visible", True),
            "order": override.get("order", default_order), "default_order": default_order,
            "kind": kind, "group": group,
            "enabled": bool(entry.get("enabled", True) and entry.get("plugin_activated", True) and definition.get("enabled", True)),
            "admin_only": bool(admin_only),
            "require_binding": bool(definition.get("require_binding", True)) if custom is not None else action == "signin",
            "cooldown_seconds": definition.get("cooldown_seconds", 0),
        })
    return sorted(records, key=lambda record: (record["order"], record["default_order"]))


@dataclass
class HelpCommand:
    name: str
    aliases: list[str]
    description: str
    notes: str


@dataclass
class HelpSection:
    title: str
    commands: list[HelpCommand] = field(default_factory=list)


@dataclass
class HelpDocument:
    title: str
    notices: list[str]
    sections: list[HelpSection]

    def as_text(self) -> str:
        lines = [self.title, *self.notices]
        for section in self.sections:
            lines.extend(("", "【" + section.title + "】"))
            for command in section.commands:
                lines.append(command.name)
                if command.aliases:
                    lines.append("别名：" + "、".join(command.aliases))
                if command.description:
                    lines.append(command.description)
                lines.append(command.notes)
        if not self.sections:
            lines.append("当前没有可用的插件指令。")
        return "\n".join(lines)


def build_help_document(catalog: list[dict], *, is_admin: bool, command_hint: Callable[[str], str]) -> HelpDocument:
    sections = []
    for entry in sorted(catalog, key=lambda record: (record["order"], record["default_order"])):
        if not entry["visible"] or not entry["enabled"] or (entry["admin_only"] and not is_admin):
            continue
        # Keep global numeric ordering, even when an override interleaves groups.
        if not sections or sections[-1].title != entry["group"]:
            sections.append(HelpSection(entry["group"]))
        notes = ["权限：管理员" if entry["admin_only"] else "权限：成员可用"]
        if entry["require_binding"]:
            notes.append("需要绑定 MC 账号")
        if entry.get("cooldown_seconds", 0):
            notes.append(f"冷却：{entry['cooldown_seconds']} 秒")
        sections[-1].commands.append(HelpCommand(
            command_hint(entry["name"]), [command_hint(alias) for alias in entry["aliases"]],
            entry["description"], " · ".join(notes),
        ))
    return HelpDocument("MCQQ 指令帮助", [
        "以当前管理行为的生效名称与别名为准；下方命令已带本会话唤醒前缀。",
        "当前权限：" + ("管理员" if is_admin else "普通成员") + "；已隐藏停用、帮助隐藏或无权使用的命令。",
    ], sections)


def _font_path() -> Path:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    for root in (Path("/usr/share/fonts"), Path("/usr/local/share/fonts")):
        if root.is_dir():
            for pattern in ("NotoSansCJK*Regular*", "NotoSansSC*Regular*", "SourceHanSansSC*Regular*", "wqy-*.ttc"):
                candidates.extend(root.rglob(pattern))
    from PIL import ImageFont
    for path in candidates:
        if path.is_file():
            try:
                ImageFont.truetype(str(path), 24)
                return path
            except OSError:
                continue
    raise RuntimeError("没有可用的中文字体，帮助将使用文字回复")


def _wrap(text: str, font, width: int) -> list[str]:
    lines = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n"):
        current = ""
        for char in paragraph:
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ""
            current += char
        lines.append(current)
    return lines


def _blocks(document: HelpDocument) -> list[list[tuple[str, str]]]:
    blocks = [[("muted", text) for text in document.notices]]
    for section in document.sections:
        for index, command in enumerate(section.commands):
            block = ([("section", section.title)] if index == 0 else []) + [("command", command.name)]
            if command.aliases:
                block.append(("body", "别名：" + "、".join(command.aliases)))
            if command.description:
                block.append(("body", command.description))
            block.extend((("muted", command.notes), ("gap", "")))
            blocks.append(block)
    if not document.sections:
        blocks.append([("body", "当前没有可用的插件指令。")])
    return blocks


def render_help_pages(document: HelpDocument, directory: str | Path, *, width: int = 1120,
                      page_height: int = 1760, font_path: str | Path | None = None) -> list[str]:
    """Render every line without truncation; callers handle font/render fallback."""
    from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
    if width < 500 or page_height < 500:
        raise ValueError("帮助图片尺寸过小")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    font_path = Path(font_path) if font_path else _font_path()
    fonts = {kind: ImageFont.truetype(str(font_path), size) for kind, size in
             (("title", 40), ("section", 28), ("command", 26), ("body", 23), ("muted", 20))}
    heights = {kind: font.size + 11 for kind, font in fonts.items()}
    heights["gap"] = 12
    margin, header, footer = 56, 112, 64
    capacity = page_height - header - footer
    pages, page, used = [], [], 0
    for block in _blocks(document):
        rows = [(kind, line) for kind, text in block
                for line in ([""] if kind == "gap" else _wrap(text, fonts[kind], width - 2 * margin))]
        size = sum(heights[kind] for kind, _ in rows)
        if page and size <= capacity and used + size > capacity:
            pages.append(page)
            page, used = [], 0
        for kind, line in rows:
            # Keep a section heading with at least its first command line.
            needed = heights[kind] + heights["command"] if kind == "section" else heights[kind]
            if page and used + needed > capacity:
                pages.append(page)
                page, used = [], 0
            page.append((kind, line))
            used += heights[kind]
    if page:
        pages.append(page)
    full_text = document.as_text()
    digest = hashlib.sha256(full_text.encode()).hexdigest()[:24]
    output = []
    colors = {"section": "#3156a5", "command": "#183864", "body": "#34445b", "muted": "#67758b"}
    for index, rows in enumerate(pages, 1):
        height = max(440, header + sum(heights[kind] for kind, _ in rows) + footer)
        path = directory / f"help-{digest}-{index}.png"
        fd, temp_path = tempfile.mkstemp(prefix=".help-", suffix=".png", dir=directory)
        os.close(fd)
        try:
            with Image.new("RGB", (width, height), "#f4f7fc") as image:
                draw = ImageDraw.Draw(image)
                draw.rectangle((0, 0, width, 8), fill="#4264d8")
                draw.text((margin, 30), document.title, font=fonts["title"], fill="#173052")
                y = header
                for kind, text in rows:
                    if kind == "section":
                        draw.rounded_rectangle((margin - 12, y - 2, width - margin + 12, y + heights[kind] - 3), radius=8, fill="#e4ecfb")
                    if kind != "gap":
                        draw.text((margin, y), text, font=fonts[kind], fill=colors[kind])
                    y += heights[kind]
                draw.line((margin, height - footer + 13, width - margin, height - footer + 13), fill="#d6dfed", width=1)
                label = f"第 {index} / {len(pages)} 页"
                draw.text((width - margin - fonts["muted"].getlength(label), height - 40), label, font=fonts["muted"], fill=colors["muted"])
                metadata = PngImagePlugin.PngInfo()
                metadata.add_itxt("page_text", "\n".join(text for _, text in rows))
                if index == 1:
                    metadata.add_itxt("help_text", full_text)
                image.save(temp_path, format="PNG", pnginfo=metadata)
            os.replace(temp_path, path)
            output.append(str(path))
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
    # Only this renderer's old files in its own plugin-data subdirectory expire.
    cutoff = time.time() - 86400
    for path in directory.glob("help-*.png"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass
    return output
