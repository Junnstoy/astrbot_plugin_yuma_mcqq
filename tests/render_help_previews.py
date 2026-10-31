"""Generate synthetic admin-view previews; never contacts QQ or Minecraft."""
import ast
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.help_renderer import build_help_document, help_catalog, render_help_image
from PIL import Image

tree = ast.parse((ROOT / 'adapters/astrbot_commands.py').read_text(encoding='utf-8'))
builtins = next(ast.literal_eval(node.value) for node in tree.body
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BUILTINS' for t in node.targets))
admins = {'points_add', 'points_sub', 'points_set', 'order_reconcile', 'rcon'}
names = ['在线玩家', '账户查询', '领地信息', '背包统计', '任务进度', '传送点列表', '今日活动', '商店库存', '资源查询', '服务器公告']
descriptions = ['查询当前服务器在线玩家及人数。', '查看绑定玩家的账户信息与游戏余额。', '查询玩家领地名称、面积及权限信息。',
                '统计背包中的资源数量，展示已使用容量。', '显示每日任务完成情况及尚未领取的奖励。', '列出可以使用的传送点与目的地说明。',
                '查看今天开放的活动、开始时间和参与条件。', '显示指定商品的剩余库存与兑换要求。',
                '按资源名称查询当前持有数量和可兑换项目。', '读取服务器公告和活动规则。']
target = ROOT / 'docs/screenshots'
target.mkdir(parents=True, exist_ok=True)
report = []
for count in (5, 15, 30, 50):
    effective = [{'handler_name': 'cmd_' + key, 'effective_command': name, 'original_command': name,
                  'description': description, 'aliases': [], 'enabled': True} for key, name, description in builtins]
    custom = {}
    for index in range(count):
        number = index + 1
        handler = f'cmd_preview_{number:02d}'
        description = descriptions[index % len(descriptions)]
        if number % 7 == 0:
            description += '\n长说明示例：请先完成游戏内绑定，命令结果会按当前群服路由返回；如活动尚未开放，会显示下次开放时间。'
        name = f'{names[index % len(names)]}{number:02d}'
        if number % 13 == 0:
            name += '_跨世界资源与在线状态联合查询'
        effective.append({'handler_name': handler, 'effective_command': name, 'description': description,
                          'aliases': [f'查询{number:02d}'] if number % 3 == 0 else [], 'enabled': True})
        custom[handler] = {'id': f'preview_{number:02d}', 'admin_only': number % 4 == 0,
                           'require_binding': number % 2 == 0, 'cooldown_seconds': 3, 'enabled': True}
    # Exercise true global ordering, including a custom command between builtins.
    preferences = {'entries': {'help': {'order': 0}, 'custom:preview_01': {'order': 15}}}
    catalog = help_catalog(effective, custom, preferences, {k: d for k, _, d in builtins}, admins)
    assert catalog[0]['action_id'] == 'help'
    assert catalog[2]['action_id'] == 'custom:preview_01'
    document = build_help_document(catalog, is_admin=True, command_hint=lambda name: '#' + name)
    image = render_help_image(document, ROOT / 'work/help-preview-single' / str(count))
    destination = target / f'help-preview-custom-{count}.png'
    shutil.copy2(image, destination)
    with Image.open(destination) as result:
        counts = json.loads(result.info['help_counts'])
        assert counts['commands'] == 23 + count, counts
        original = result.info['help_text']
        assert len([x for x in catalog if x['kind'] == 'custom']) == count
        for entry in catalog:
            assert '#' + entry['name'] in original
        columns = (23 + count + 14) // 15
        assert int(result.info['columns']) == columns
        cards = json.loads(result.info['card_layout'])
        per_column = []
        for column in range(columns):
            items = [card for card in cards if card['column'] == column]
            sequences = [number for card in items for number in card['sequences']]
            assert sequences == list(range(column * 15 + 1, min((column + 1) * 15, 23 + count) + 1))
            per_column.append(len(sequences))
        drawn = [entry for card in cards for entry in card['command_text']]
        assert [number for number, _ in drawn] == list(range(1, 24 + count))
        assert drawn[0][1][0] == '01  #mc帮助'
        assert drawn[2][1][0] == '03  #在线玩家01'
        report.append({'custom': count, 'total': counts['commands'], 'size': list(result.size),
                       'columns': columns, 'per_column': per_column,
                       'bytes': destination.stat().st_size, 'path': destination.relative_to(ROOT).as_posix()})
shutil.copy2(target / 'help-preview-custom-5.png', target / 'help-example.png')
(target / 'help-preview-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
