"""Permission-aware help models and a single readable help image."""
from __future__ import annotations

import hashlib
import json
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
DEFAULT_ORDER["help"] = 0


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
    return HelpDocument("MCQQ 指令速查", [
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


MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_HEIGHT = 20_000


def _wrap(text: str, font, width: int) -> list[str]:
    lines = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4).split("\n"):
        current = ""
        for char in paragraph:
            if current and font.getlength(current + char) > width:
                lines.append(current)
                current = ""
                if len(lines) * (font.size + 10) > MAX_IMAGE_HEIGHT:
                    raise RuntimeError("帮助内容超过单张图片尺寸上限，将使用完整文字回复")
            current += char
        lines.append(current)
    return lines


@dataclass
class _Card:
    title: str
    count: int
    height: int
    text: list[tuple[str, str, int, int]]
    badges: list[tuple[int, int, int, int, str]]
    panels: list[tuple[int, int]]
    heading_height: int
    label: str
    sequences: list[int]
    command_text: list[tuple[int, list[str]]]


def _card_layout(section: HelpSection, width: int, fonts: dict, label: str | None = None,
                 sequences: list[int] | None = None) -> _Card:
    """Lay out the entire card before allocating any image pixels."""
    pad, gap = 26, 6
    inner = width - pad * 2
    text, badges, panels = [], [], []
    command_text = []
    sequences = sequences or list(range(1, len(section.commands) + 1))
    label = label or section.title
    y = 20
    for line in _wrap(label, fonts["section"], inner - 78):
        text.append(("section", line, pad, y))
        y += fonts["section"].size + 8
    heading_height = y + 12
    y = heading_height + 22
    for sequence, command in zip(sequences, section.commands):
        text_start = len(text)
        panel_top = y - 11
        for kind, value in (("command", command.name),
                            ("muted", "别名：" + "、".join(command.aliases) if command.aliases else ""),
                            ("body", command.description)):
            if not value:
                continue
            for line in _wrap(value, fonts[kind], inner):
                text.append((kind, line, pad, y))
                y += fonts[kind].size + 6
            y += 3
        x, row_height = pad, 0
        for note in (part for part in command.notes.split(" · ") if part):
            lines = _wrap(note, fonts["badge"], inner - 22)
            badge_width = min(inner, int(max(fonts["badge"].getlength(line) for line in lines)) + 23)
            badge_height = len(lines) * 22 + 6
            if x > pad and x + badge_width > width - pad:
                y += row_height + gap
                x, row_height = pad, 0
            badges.append((x, y, x + badge_width, y + badge_height, _badge_style(note)))
            for line_index, line in enumerate(lines):
                text.append(("badge", line, x + 11, y + 2 + line_index * 22))
            x += badge_width + gap
            row_height = max(row_height, badge_height)
        y += row_height + 10
        panels.append((panel_top, y))
        y += 19
        command_text.append((sequence, [value for _, value, _, _ in text[text_start:]]))
        if y > MAX_IMAGE_HEIGHT:
            raise RuntimeError("帮助内容超过单张图片尺寸上限，将使用完整文字回复")
    if not section.commands:
        text.append(("muted", "当前分类没有可用指令。", pad, y))
        y += 34
    return _Card(section.title, len(section.commands), y - 6, text, badges, panels,
                 heading_height, label, sequences, command_text)


def _region_outline(rectangles: list[tuple[int, int, int, int]]) -> list[tuple[int, int]]:
    """Outline contiguous column rectangles joined only through their overlap."""
    points = [(rectangles[0][0], rectangles[0][1])]
    for index, (left, top, right, bottom) in enumerate(rectangles):
        points.append((right, top))
        if index + 1 < len(rectangles):
            next_left, next_top, _, _ = rectangles[index + 1]
            bridge_top = max(top, next_top)
            points.extend(((right, bridge_top), (next_left, bridge_top), (next_left, next_top)))
    points.append((rectangles[-1][2], rectangles[-1][3]))
    for index in range(len(rectangles) - 1, -1, -1):
        left, top, right, bottom = rectangles[index]
        points.append((left, bottom))
        if index:
            _, _, previous_right, previous_bottom = rectangles[index - 1]
            bridge_bottom = min(bottom, previous_bottom)
            points.extend(((left, bridge_bottom), (previous_right, bridge_bottom), (previous_right, previous_bottom)))
    clean = []
    for point in points:
        if not clean or point != clean[-1]:
            clean.append(point)
    return clean


def _flow_layout(document: HelpDocument, columns: int, card_width: int, fonts: dict,
                 margin: int, gutter: int, top: int):
    """Read down each column in global order, with no more than 15 commands."""
    entries = [(section.title, command) for section in document.sections for command in section.commands]
    digits = max(2, len(str(len(entries))))
    cards, regions, seen_categories, bottom = [], [], set(), top
    for column in range(columns):
        first, last = column * 15, min((column + 1) * 15, len(entries))
        cursor, y = first, top
        while cursor < last:
            title = entries[cursor][0]
            end = cursor + 1
            while end < last and entries[end][0] == title:
                end += 1
            commands, sequences = [], list(range(cursor + 1, end + 1))
            for sequence, (_, command) in zip(sequences, entries[cursor:end]):
                commands.append(HelpCommand(f"{sequence:0{digits}d}  {command.name}", command.aliases,
                                            command.description, command.notes))
            label = title + " · 续" if title in seen_categories else title
            seen_categories.add(title)
            card = _card_layout(HelpSection(title, commands), card_width, fonts, label, sequences)
            x = margin + column * (card_width + gutter)
            rectangle = (x, y, x + card_width, y + card.height)
            cards.append((card, x, y, len(regions)))
            regions.append({"category": title, "layout": "single_column", "column": column,
                            "sequences": sequences, "polygon": _region_outline([rectangle]),
                            "rectangles": [rectangle]})
            y += card.height + gutter
            cursor = end
        bottom = max(bottom, y)
    if bottom > MAX_IMAGE_HEIGHT:
        raise RuntimeError("帮助内容超过单张图片尺寸上限，将使用完整文字回复")
    return cards, regions, bottom


def _badge_style(note: str) -> str:
    if "管理员" in note:
        return "admin"
    if note.startswith("权限"):
        return "member"
    if "绑定" in note:
        return "binding"
    if "冷却" in note:
        return "cooldown"
    return "default"


def render_help_image(document: HelpDocument, directory: str | Path, *, width: int | None = None,
                      font_path: str | Path | None = None) -> str:
    """Render one full-size dark card image; over-budget content raises for text fallback."""
    from PIL import Image, ImageDraw, ImageFont, PngImagePlugin
    command_count = sum(len(section.commands) for section in document.sections)
    columns = max(1, (command_count + 14) // 15)
    if width is None:
        width = 60 + 720 * columns
    if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 16384:
        raise ValueError("帮助图片宽度必须是 1–16384 之间的整数")
    if width < 96 + 640 * columns + 28 * (columns - 1):
        raise RuntimeError("指定图片宽度不足以容纳每列最多 15 条指令，将使用完整文字回复")
    font_path = Path(font_path) if font_path else _font_path()
    fonts = {kind: ImageFont.truetype(str(font_path), size) for kind, size in
             (("title", 44), ("section", 26), ("command", 24), ("body", 20), ("muted", 18), ("badge", 16))}
    margin, gutter = 48, 28
    usable = width - margin * 2
    card_width = (usable - gutter * (columns - 1)) // columns
    category_count = len({section.title for section in document.sections})
    statistics = f"{command_count} 条指令  /  {category_count} 个分类"
    heading = "MCQQ 指令速查" if document.title == "MCQQ 指令帮助" else document.title
    statistic_width = int(fonts["muted"].getlength(statistics)) + 32
    statistic_x = width - margin - 24 - statistic_width
    header_text = [("badge", "MINECRAFT  ×  QQ", margin + 28, 41)]
    y = 72
    for line in _wrap(heading, fonts["title"], usable - statistic_width - 84):
        header_text.append(("title", line, margin + 28, y))
        y += 58
    header_text.append(("muted", statistics, statistic_x + 16, 91))
    banner_bottom = y + 27
    y = banner_bottom + 28
    cards, regions, y = _flow_layout(document, columns, card_width, fonts, margin, gutter, y)
    if not document.sections:
        header_text.append(("body", "当前没有可用的插件指令。", margin, y + 24))
        y += 100
    footer_top = y + 8
    footer_text = [("section", "使用说明", margin + 24, footer_top + 18)]
    y = footer_top + 64
    for notice in document.notices:
        for line in _wrap(notice, fonts["muted"], usable - 48):
            footer_text.append(("muted", line, margin + 24, y))
            y += 26
        y += 3
    y += 13
    height = max(420, y + 40)
    if height > MAX_IMAGE_HEIGHT or width * height > MAX_IMAGE_PIXELS:
        raise RuntimeError("帮助内容超过单张图片尺寸上限，将使用完整文字回复")
    full_text = document.as_text()
    digest = hashlib.sha256((str(width) + ":ordered-columns-15-v2:" + full_text).encode()).hexdigest()[:24]
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"help-{digest}.png"
    fd, temp_path = tempfile.mkstemp(prefix=".help-", suffix=".png", dir=directory)
    os.close(fd)
    colors = {"title": "#f1f7ff", "section": "#f1f7ff", "command": "#eef7ff",
              "body": "#c4d2e4", "muted": "#95aac4", "badge": "#c5d9ee"}
    accents = ("#58d5db", "#ba9cfb", "#77c7ff", "#f4bc7e", "#91dba4", "#f29cc8")
    category_colors = {"签到与绑定": "#82deb1", "点数与商店": "#bc9af5", "服务器查询": "#63d8e9",
                       "管理员操作": "#f39390", "自定义命令": "#eec476", "帮助": "#88b7ff"}
    badge_colors = {"member": ("#173d36", "#8cdfb8"), "admin": ("#472a35", "#f4a4ae"),
                    "binding": ("#403923", "#eed38b"), "cooldown": ("#193c47", "#8bd5e8"),
                    "default": ("#1b324b", "#c5d9ee")}
    rendered_text = []
    try:
        with Image.new("RGB", (width, height), "#0b1422") as image:
            draw = ImageDraw.Draw(image)
            for grid_x in range(0, width, 36):
                draw.line((grid_x, 0, grid_x, height), fill="#111f31")
            for grid_y in range(0, height, 36):
                draw.line((0, grid_y, width, grid_y), fill="#111f31")
            draw.rectangle((0, 0, width, 5), fill="#58d5db")
            draw.rounded_rectangle((margin, 20, width - margin, banner_bottom), radius=20, fill="#14253c", outline="#2d435e")
            draw.rounded_rectangle((statistic_x, 84, statistic_x + statistic_width, 128), radius=12, fill="#213c53", outline="#395c75")

            def paint(rows, offset_x=0, offset_y=0, accent=None):
                for kind, value, text_x, text_y in rows:
                    color = accent if kind == "command" and accent else colors[kind]
                    if kind == "badge":
                        color = badge_colors[_badge_style(value)][1]
                    draw.text((text_x + offset_x, text_y + offset_y), value, font=fonts[kind], fill=color)
                    rendered_text.append(value)

            paint(header_text)
            header_lines = rendered_text[:]
            for index, region in enumerate(regions):
                accent = category_colors.get(region["category"], accents[index % len(accents)])
                polygon = region["polygon"]
                draw.polygon(polygon, fill="#112035")
                draw.line(polygon + [polygon[0]], fill=accent, width=2)
            for card, x, top, index in cards:
                accent = category_colors.get(card.title, accents[index % len(accents)])
                draw.rectangle((x + 2, top + 2, x + card_width - 2, top + card.heading_height), fill="#172941")
                draw.rounded_rectangle((x + 13, top + 22, x + 17, top + card.heading_height - 14), radius=2, fill=accent)
                count_label = f"{card.count} 项"
                draw.text((x + card_width - 26 - fonts["badge"].getlength(count_label), top + 28), count_label, font=fonts["badge"], fill=accent)
                for panel_top, panel_bottom in card.panels:
                    draw.rounded_rectangle((x + 14, top + panel_top, x + card_width - 14, top + panel_bottom), radius=10, fill="#0c192a", outline="#21344a")
                for left, upper, right, lower, style in card.badges:
                    draw.rounded_rectangle((x + left, top + upper, x + right, top + lower), radius=6, fill=badge_colors[style][0])
                paint(card.text, x, top, accent)
            draw.rounded_rectangle((margin, footer_top, width - margin, height - 34), radius=16, fill="#102034", outline="#293d56")
            footer_start = len(rendered_text)
            paint(footer_text)
            command_lines = sorted((item for card, _, _, _ in cards for item in card.command_text), key=lambda item: item[0])
            rendered_text = (header_lines + [value for card, _, _, _ in cards for kind, value, _, _ in card.text if kind == "section"]
                             + [line for _, lines in command_lines for line in lines] + rendered_text[footer_start:])
            metadata = PngImagePlugin.PngInfo()
            metadata.add_itxt("help_text", full_text)
            metadata.add_itxt("rendered_text", "\n".join(rendered_text))
            metadata.add_itxt("help_counts", json.dumps({"commands": command_count, "categories": category_count}))
            metadata.add_itxt("columns", str(columns))
            metadata.add_itxt("card_layout", json.dumps([
                {"title": card.label, "category": card.title, "command_count": card.count,
                 "sequences": card.sequences, "command_text": card.command_text,
                 "column": (x - margin) // (card_width + gutter),
                 "x": x, "y": top, "width": card_width, "height": card.height}
                for card, x, top, _ in cards], ensure_ascii=False))
            metadata.add_itxt("region_layout", json.dumps(regions, ensure_ascii=False))
            image.save(temp_path, format="PNG", pnginfo=metadata)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    # Only this renderer's old files in its own plugin-data subdirectory expire.
    cutoff = time.time() - 86400
    for old_path in directory.glob("help-*.png"):
        try:
            if old_path.stat().st_mtime < cutoff:
                old_path.unlink()
        except OSError:
            pass
    return str(path)
