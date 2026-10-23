/* Native AstrBot plugin view. All data crosses the authenticated bridge. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  const defaults = { enabled: false, timezone: 'Asia/Shanghai', min_points: 10, max_points: 30, windows: [{ start: '00:00', end: '23:59' }], date_rules: [], mc_enabled: false, server_name: '', require_binding: true, commands: [], makeup_commands: ['signin click {date} {player}'], leaderboard_command: 'signin leaderboard', currency_name: '点数', checkin_prefix: '', command_prefix: '', admin_ids: [], confirmation_minutes: 3, mc_success_keywords: [], mc_failure_keywords: [], products: [], templates: {} };
  const templateNames = { checkin_success: '签到成功', checkin_pending: '签到处理中', checkin_duplicate: '重复签到', checkin_disabled: '签到未启用', checkin_outside_window: '不在签到时间', checkin_unbound: '尚未绑定', checkin_mc_failed: '游戏签到失败', checkin_mc_unknown: '游戏结果未知', balance: '查询余额', shop_list: '商店列表', shop_item: '单件商品介绍', purchase_pending: '等待兑换确认', purchase_delivered: '奖励已发放', purchase_private: '私聊奖励内容', purchase_failed: '兑换发放失败', ranking: '排行榜', ranking_item: '单条排名', admin_reset: '管理员重置签到', makeup_success: '补签成功', makeup_duplicate: '补签日期已有签到', admin_adjust: '管理员调整余额', admin_query: '管理员查询余额', no_permission: '权限不足', usage: '指令使用说明' };
  const templateFields = ['user_id', 'player', 'points', 'total', 'currency_name', 'day', 'mc_state', 'minutes', 'product_name', 'product_id', 'price', 'order_id', 'reward', 'old', 'delta', 'target_id', 'items', 'stock', 'rank', 'command_list', 'confirmation_command'];
  let state = { sessions: [], servers: [], session: '', binding_count: 0 };
  let settings = structuredClone(defaults);
  let dirty = false;
  let busy = false;
  let ready = false;
  let sequence = 0;
  let page = 'overview';
  let finishConfirmation = null;
  let refreshingStock = false;
  let stockRevision = 0;
  const lines = value => value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);

  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function notify(message, kind = 'info') {
    $('notice').textContent = message;
    $('notice').className = `notice ${kind}`;
    $('notice').hidden = !message;
  }
  function lock(value) {
    if (value) stockRevision++;
    busy = value;
    $('settings-fields').disabled = value || !state.session || !ready;
    $('save').disabled = value || !state.session || !ready;
    $('refresh').disabled = value || !ready;
    $('session-select').disabled = value || !ready;
    $('new-session').disabled = value || !ready;
    $('add-session').disabled = value || !ready;
    $('discard').disabled = value || !dirty;
    $('back-overview').disabled = value || !ready;
    $('sessions-list').querySelectorAll('button,input').forEach(control => { control.disabled = value || !ready; });
  }
  function showPage(name) {
    page = name;
    $('overview-page').hidden = name !== 'overview';
    $('editor-page').hidden = name !== 'editor';
    window.scrollTo({ top: 0, behavior: 'instant' });
  }
  function confirmAction(title, description, accept = '确认') {
    $('confirm-title').textContent = title;
    $('confirm-description').textContent = description;
    $('confirm-accept').textContent = accept;
    $('confirm-overlay').hidden = false;
    const previousFocus = document.activeElement;
    $('confirm-cancel').focus();
    return new Promise(resolve => { finishConfirmation = value => { $('confirm-overlay').hidden = true; finishConfirmation = null; previousFocus?.focus(); resolve(value); }; });
  }
  async function mayLeave() {
    return !dirty || await confirmAction('有尚未保存的修改', '离开后将放弃当前会话未保存的修改。是否继续？', '放弃修改并继续');
  }
  function markDirty() {
    dirty = true;
    $('save-state').textContent = '有尚未保存的修改';
    $('save-state').className = 'save-state dirty';
    $('discard').disabled = busy;
    preview();
  }
  function markSaved(message = '设置已加载') {
    dirty = false;
    $('save-state').textContent = message;
    $('save-state').className = 'save-state';
    $('discard').disabled = true;
    $('save-caption').textContent = state.session || '设置仅应用于当前会话';
  }
  function result(data) {
    if (!data || data.ok !== true) throw new Error(data?.error || data?.message || '服务端未返回有效结果');
    return data;
  }
  function rowInput(label, type, value, options = {}) {
    const wrapper = el('label', label, 'repeat-field');
    const input = el('input');
    input.type = type;
    input.value = value ?? '';
    input.required = true;
    Object.assign(input, options);
    wrapper.append(input);
    return wrapper;
  }
  function updateWindowButtons() {
    const rows = $('windows-list').children;
    for (const row of rows) row.querySelector('button').disabled = rows.length === 1;
    $('windows-empty').hidden = rows.length > 0;
  }
  function addWindow(value = { start: '09:00', end: '22:00' }) {
    const row = el('div', undefined, 'repeat-row window-row');
    row.append(rowInput('开始时间', 'time', value.start), el('span', '—', 'row-separator'), rowInput('结束时间', 'time', value.end));
    const remove = el('button', '×', 'remove-row');
    remove.type = 'button'; remove.title = '删除此时段'; remove.setAttribute('aria-label', '删除此时段');
    remove.addEventListener('click', () => { row.remove(); updateWindowButtons(); markDirty(); });
    row.append(remove); $('windows-list').append(row); updateWindowButtons();
  }
  function addDate(value = { date: '', multiplier: 2 }) {
    const row = el('div', undefined, 'repeat-row date-row');
    row.append(rowInput('日期', 'text', value.date, { placeholder: '2026-10-01 或 10-01', maxLength: 10, pattern: '(?:[0-9]{4}-)?[0-9]{2}-[0-9]{2}' }), rowInput('奖励倍率', 'number', value.multiplier, { min: '0', max: '1000', step: 'any' }));
    const remove = el('button', '×', 'remove-row');
    remove.type = 'button'; remove.title = '删除此日期'; remove.setAttribute('aria-label', '删除此日期');
    remove.addEventListener('click', () => { row.remove(); $('dates-empty').hidden = $('dates-list').children.length > 0; markDirty(); });
    row.append(remove); $('dates-list').append(row); $('dates-empty').hidden = true;
  }
  function addProduct(value = {}) {
    const used = new Set([...$('products-list').querySelectorAll('[data-field="id"]')].map(input => input.value));
    let number = 1; while (used.has(String(number))) number++;
    const product = { id: String(number), name: '', price: 100, purchase_limit: 0, enabled: true, reward_type: 'text', reward_text: '', codes: [], ...value };
    const row = el('section', undefined, 'product-card');
    const heading = el('div', undefined, 'card-heading');
    heading.append(el('h3', '商品设置'));
    const controls = el('div', undefined, 'product-controls');
    const enabledLabel = el('label', undefined, 'check-label'); const enabled = el('input'); enabled.type = 'checkbox'; enabled.checked = product.enabled; enabled.dataset.field = 'enabled'; enabledLabel.append(enabled, el('span', '上架'));
    const remove = el('button', '删除商品', 'button danger small'); remove.type = 'button'; remove.addEventListener('click', () => { row.remove(); $('products-empty').hidden = !!$('products-list').children.length; markDirty(); });
    controls.append(enabledLabel, remove); heading.append(controls); row.append(heading);
    const fields = el('div', undefined, 'product-fields');
    for (const [key, label, type] of [['id', '商品序号', 'text'], ['name', '商品名称', 'text'], ['price', '兑换价格', 'number'], ['purchase_limit', '每人累计限购（0 不限）', 'number']]) {
      const field = rowInput(label, type, product[key], type === 'number' ? { min: '0', max: String(Number.MAX_SAFE_INTEGER), step: '1' } : {});
      field.querySelector('input').dataset.field = key; fields.append(field);
    }
    row.append(fields);
    row.append(el('p', '限购按本会话、用户及商品累计计算。已付款但待补发的订单也占额度；未扣款的取消、过期订单不计入。', 'hint'));
    const typeLabel = el('label', '奖励类型', 'field'); const select = el('select'); select.dataset.field = 'reward_type';
    for (const [type, text] of [['text', '固定文本奖励'], ['codes', '独立兑换码']]) { const option = el('option', text); option.value = type; select.append(option); }
    select.value = product.reward_type; typeLabel.append(select); row.append(typeLabel);
    const textLabel = el('label', '奖励文本（仅后台可见）', 'field'); const text = el('textarea'); text.dataset.field = 'reward_text'; text.value = product.reward_text; text.rows = 3; textLabel.append(text);
    const codesLabel = el('label', '剩余可售兑换码（每行一个，仅后台可见）', 'field'); const codes = el('textarea'); codes.dataset.field = 'codes'; codes.value = product.codes.join('\n'); codes.rows = 4; codes.spellcheck = false; codesLabel.append(codes);
    const stockCount = el('span', `后台剩余 ${product.codes.length} 条`, 'stock-count'); stockCount.dataset.stockCount = ''; codesLabel.append(stockCount);
    row.append(textLabel, codesLabel, el('p', '付款分配后，兑换码会从此码池移除；私聊失败仍为原订单保留，不重新出售。重新保存旧表单也不会复活已分配的码。', 'hint'));
    const updateType = () => { textLabel.hidden = select.value !== 'text'; codesLabel.hidden = select.value !== 'codes'; };
    select.addEventListener('change', updateType); updateType(); $('products-list').append(row); $('products-empty').hidden = true;
  }
  function renderTemplates(values) {
    $('templates-list').replaceChildren();
    const fields = Array.isArray(state.template_fields) ? state.template_fields : templateFields;
    $('template-fields').textContent = `可用占位符：${fields.map(field => String(field).startsWith('{') ? field : `{${field}}`).join(' ')}。不同场景仅填写该场景可用的字段。`;
    for (const [key, value] of Object.entries(values || {})) {
      const label = el('label', undefined, 'template-field'); label.append(el('span', templateNames[key] || key), el('small', key));
      const input = el('textarea'); input.rows = 3; input.value = value; input.dataset.key = key; input.spellcheck = false; label.append(input); $('templates-list').append(label);
    }
  }
  function collect() {
    return { ...settings,
      enabled: $('enabled').checked, timezone: $('timezone').value.trim(),
      min_points: Number($('min-points').value), max_points: Number($('max-points').value),
      windows: [...$('windows-list').children].map(row => { const inputs = row.querySelectorAll('input'); return { start: inputs[0].value, end: inputs[1].value }; }),
      date_rules: [...$('dates-list').children].map(row => { const inputs = row.querySelectorAll('input'); return { date: inputs[0].value.trim(), multiplier: Number(inputs[1].value) }; }),
      mc_enabled: $('mc-enabled').checked, server_name: $('server-name').value,
      require_binding: $('require-binding').checked,
      commands: lines($('commands').value), makeup_commands: lines($('makeup-commands').value), leaderboard_command: $('leaderboard-command').value.trim(),
      currency_name: $('currency-name').value.trim(), checkin_prefix: $('checkin-prefix').value.trim(), command_prefix: $('command-prefix').value.trim(),
      admin_ids: lines($('admin-ids').value), confirmation_minutes: Number($('confirmation-minutes').value),
      mc_success_keywords: lines($('mc-success-keywords').value), mc_failure_keywords: lines($('mc-failure-keywords').value),
      products: [...$('products-list').children].map(row => ({ id: row.querySelector('[data-field="id"]').value.trim(), name: row.querySelector('[data-field="name"]').value.trim(), price: Number(row.querySelector('[data-field="price"]').value), purchase_limit: Number(row.querySelector('[data-field="purchase_limit"]').value), enabled: row.querySelector('[data-field="enabled"]').checked, reward_type: row.querySelector('[data-field="reward_type"]').value, reward_text: row.querySelector('[data-field="reward_text"]').value, codes: lines(row.querySelector('[data-field="codes"]').value) })),
      templates: Object.fromEntries([...$('templates-list').querySelectorAll('textarea')].map(input => [input.dataset.key, input.value]))
    };
  }
  function validate(data) {
    if (data.max_points < data.min_points) throw new Error('最高点数不能小于最低点数。');
    if (/\s/.test(data.checkin_prefix) || /\s/.test(data.command_prefix)) throw new Error('签到指令前缀和商店及点数管理前缀均不能包含空格或其他空白字符。');
    try { new Intl.DateTimeFormat('zh-CN', { timeZone: data.timezone }); } catch { throw new Error('时区无效，请填写 Asia/Shanghai 等 IANA 时区名称。'); }
    const seen = new Set();
    for (const rule of data.date_rules) {
      if (!Number.isFinite(rule.multiplier) || rule.multiplier <= 0 || rule.multiplier > 1000) throw new Error('奖励倍率必须大于 0 且不超过 1000。');
      const full = rule.date.length === 5 ? `2000-${rule.date}` : rule.date;
      const parsed = new Date(`${full}T12:00:00Z`);
      if (!/^\d{4}-\d{2}-\d{2}$/.test(full) || !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== full) throw new Error(`日期无效：${rule.date || '未填写'}。`);
      if (seen.has(rule.date)) throw new Error(`日期 ${rule.date} 已重复，请每个日期仅保留一条倍率规则。`);
      seen.add(rule.date);
    }
    if (data.mc_enabled && (!data.server_name || !data.commands.length)) throw new Error('开启 MC 同步时，请选择目标服务器并填写签到指令。');
    const products = new Set();
    for (const product of data.products) {
      if (!Number.isSafeInteger(product.price) || product.price < 0) throw new Error('商品价格必须为 0 至 9007199254740991 的整数。');
      if (!Number.isSafeInteger(product.purchase_limit) || product.purchase_limit < 0) throw new Error('每人累计限购必须为 0 至 9007199254740991 的整数，0 表示不限。');
      if (products.has(product.id)) throw new Error(`商品序号重复：${product.id}。`);
      products.add(product.id);
    }
  }
  function preview() {
    const data = collect();
    const checkinPrefix = data.checkin_prefix ? `${data.checkin_prefix} ` : '';
    const managementPrefix = data.command_prefix ? `${data.command_prefix} ` : '';
    $('prefix-preview').textContent = `以 # 为 AstrBot 唤醒头：#${checkinPrefix}签到、#${checkinPrefix}点数、#${managementPrefix}商店列表。非空前缀必须填写，两个前缀分别生效。`;
    $('stat-points').textContent = `${data.min_points}–${data.max_points}`;
    $('stat-currency').textContent = `${data.currency_name || '点数'} / 次`;
    $('stat-dates').textContent = data.date_rules.length;
    $('stat-status').textContent = data.enabled ? '签到已启用' : '签到已关闭';
    $('stat-status').className = `badge ${data.enabled ? 'good' : 'neutral'}`;
    $('preview-summary').textContent = data.enabled ? `每天签到一次，基础奖励 ${data.min_points}–${data.max_points} ${data.currency_name || '点数'}。特别日期使用对应倍率。` : '此会话已关闭签到；启用并保存后，群成员即可参加。';
    $('preview-time').textContent = `${data.timezone || '未设置时区'} · ${data.windows.map(w => `${w.start || '…'}—${w.end || '…'}`).join(' / ')}`;
    $('preview-mc').textContent = data.mc_enabled ? `同步至 ${data.server_name || '尚未选择的服务器'}，发送 ${data.commands.length} 条指令。仅命中成功关键词才入账，失败优先；未知结果不加分。` : 'MC 同步已关闭，直接记录群内签到与点数。';
  }
  function renderForm(data) {
    settings = { ...structuredClone(defaults), ...(data || {}) };
    $('stock-status').textContent = '每 12 秒同步库存与订单；编辑中的表单不会被覆盖。';
    $('stock-status').className = 'hint stock-status';
    $('enabled').checked = settings.enabled;
    $('min-points').value = settings.min_points;
    $('max-points').value = settings.max_points;
    $('timezone').value = settings.timezone;
    $('mc-enabled').checked = settings.mc_enabled;
    $('require-binding').checked = settings.require_binding;
    $('commands').value = settings.commands.join('\n');
    $('makeup-commands').value = settings.makeup_commands.join('\n');
    $('leaderboard-command').value = settings.leaderboard_command;
    $('currency-name').value = settings.currency_name;
    $('checkin-prefix').value = settings.checkin_prefix;
    $('command-prefix').value = settings.command_prefix;
    $('admin-ids').value = settings.admin_ids.join('\n');
    $('confirmation-minutes').value = settings.confirmation_minutes;
    $('mc-success-keywords').value = settings.mc_success_keywords.join('\n');
    $('mc-failure-keywords').value = settings.mc_failure_keywords.join('\n');
    $('products-list').replaceChildren();
    settings.products.forEach(addProduct);
    $('products-empty').hidden = settings.products.length > 0;
    renderTemplates(settings.templates);
    $('windows-list').replaceChildren();
    for (const window of settings.windows) addWindow(window);
    $('dates-list').replaceChildren();
    for (const date of settings.date_rules) addDate(date);
    $('dates-empty').hidden = settings.date_rules.length > 0;
    const servers = new Map((state.servers || []).map(server => [server.name, server]));
    if (settings.server_name && !servers.has(settings.server_name)) servers.set(settings.server_name, { name: settings.server_name, connected: false });
    $('server-name').replaceChildren(el('option', '请选择服务器'));
    $('server-name').firstElementChild.value = '';
    for (const server of servers.values()) {
      const option = el('option', `${server.name} · ${server.connected ? '已连接' : '未连接'}`);
      option.value = server.name; $('server-name').append(option);
    }
    $('server-name').value = settings.server_name;
    preview();
  }
  function renderSessions() {
    const sessions = new Map((state.sessions || []).map(item => [item.session, item]));
    if (state.session && !sessions.has(state.session)) sessions.set(state.session, { session: state.session });
    $('session-select').replaceChildren();
    if (!sessions.size) { const option = el('option', '暂无会话，请点击「新增会话」'); option.value = ''; $('session-select').append(option); }
    for (const item of sessions.values()) { const option = el('option', item.session); option.value = item.session; $('session-select').append(option); }
    $('session-select').value = state.session || '';
    renderOverview();
  }
  function renderOverview() {
    $('sessions-list').replaceChildren();
    $('sessions-empty').hidden = !!state.sessions.length;
    for (const item of state.sessions) {
      const config = { ...defaults, ...item.settings };
      const card = el('section', undefined, 'card session-overview'); card.dataset.session = item.session;
      const heading = el('div', undefined, 'card-heading');
      const title = el('div'); title.append(el('p', 'GROUP SESSION', 'eyebrow'), el('h2', item.session));
      const label = el('label', undefined, 'switch-label');
      const toggle = el('input'); toggle.type = 'checkbox'; toggle.checked = config.enabled; toggle.setAttribute('aria-label', `${item.session} 启用签到`);
      label.append(toggle, el('span', undefined, 'switch-track'));
      heading.append(title, label); card.append(heading);
      card.append(el('p', `${config.currency_name} · ${config.min_points}–${config.max_points} / 次`, 'session-summary'));
      const status = el('p', undefined, 'session-meta'); status.append(el('span', config.enabled ? '签到已开启' : '签到已关闭', `badge ${config.enabled ? 'good' : 'neutral'}`), el('span', config.mc_enabled ? 'MC 联动' : '群签到')); card.append(status);
      const actions = el('div', undefined, 'session-actions');
      const edit = el('button', '进入配置', 'button primary'); edit.type = 'button'; edit.dataset.action = 'edit';
      edit.addEventListener('click', async () => { notify(''); if (await load(item.session)) showPage('editor'); });
      const remove = el('button', '删除会话', 'button danger'); remove.type = 'button'; remove.dataset.action = 'delete';
      remove.addEventListener('click', () => deleteSession(item.session));
      toggle.addEventListener('change', async () => {
        const enabled = toggle.checked; lock(true);
        try { const response = result(await bridge.apiPost('sessions/toggle', { session: item.session, enabled })); item.settings = { ...config, ...(response.settings || {}), enabled }; renderOverview(); notify(enabled ? '此会话已开启签到。' : '此会话已关闭签到。', 'success'); }
        catch (error) { toggle.checked = config.enabled; notify(`切换失败：${error.message || error}`, 'error'); }
        finally { lock(false); }
      });
      actions.append(edit, remove); card.append(actions); $('sessions-list').append(card);
    }
  }
  async function deleteSession(session) {
    if (!await confirmAction('删除此会话的配置？', `${session}\n只删除会话配置，保留点数、签到和兑换历史，并停用待执行任务。重新添加相同会话可继续查看历史。`, '确认删除配置')) return;
    lock(true);
    try {
      result(await bridge.apiPost('sessions/delete', { session }));
      state.sessions = state.sessions.filter(item => item.session !== session);
      if (state.session === session) state.session = '';
      renderSessions(); notify('会话配置已删除，点数与历史记录已保留。', 'success');
    } catch (error) { notify(`删除失败，会话仍保留：${error.message || error}`, 'error'); }
    finally { lock(false); }
  }
  function renderBindings() {
    $('stat-bindings').textContent = state.binding_count ?? 0;
    $('binding-note').textContent = `已同步 ${state.binding_count ?? 0} 位玩家的绑定`;
  }
  function renderHistory(history = []) {
    $('history-body').replaceChildren();
    $('history-count').textContent = `${history.length} 条`;
    $('history-empty').hidden = history.length > 0;
    const labels = { disabled: ['未启用', 'neutral'], skipped: ['已跳过', 'neutral'], not_bound: ['未绑定', 'neutral'], pending: ['等待发送', 'warn'], sending: ['发送中', 'warn'], sent: ['已发送', 'good'], success: ['已发送', 'good'], unknown: ['结果未确认', 'warn'], failed: ['发送失败', 'bad'] };
    for (const record of history) {
      const row = el('tr');
      const user = el('td'); user.append(el('div', record.player || record.user_id, 'user-primary'));
      if (record.player) user.append(el('div', record.user_id, 'user-secondary'));
      const status = labels[record.mc_state] || [record.mc_state || '无同步记录', 'neutral'];
      const statusCell = el('td'); statusCell.append(el('span', status[0], `badge ${status[1]}`));
      if (record.reset_at) { row.classList.add('archived-record'); user.append(el('div', '管理员已重置 · 归档记录', 'archive-label')); statusCell.append(el('div', '原 MC 状态', 'user-secondary')); }
      const points = el('td'); points.append(el('div', record.credited ? `+${record.points ?? 0}` : `预计 ${record.points ?? 0}`, record.credited ? 'points-cell' : 'pending-points'), el('div', record.reset_at ? (record.credited ? '归档时已入账（未扣回）' : '归档时未入账') : (record.credited ? '已入账' : '未入账'), 'user-secondary'));
      row.append(user, el('td', record.day || '—'), points, el('td', String(record.total ?? '—')), statusCell, el('td', record.mc_detail || '—', 'detail-cell'));
      $('history-body').append(row);
    }
  }
  function renderActivity() {
    $('leaderboard-body').replaceChildren(); $('orders-body').replaceChildren();
    const leaderboard = state.leaderboard || []; const orders = state.orders || [];
    $('leaderboard-empty').hidden = !!leaderboard.length; $('orders-empty').hidden = !!orders.length;
    leaderboard.forEach((item, index) => { const row = el('tr'); row.append(el('td', String(index + 1)), el('td', item.user_id), el('td', `${item.total} ${settings.currency_name}`)); $('leaderboard-body').append(row); });
    const orderLabels = { pending: '待确认', confirmed: '已确认', paid: '已付款待发放', sending: '发货中', delivery_failed: '待补领', delivered: '已发放', expired: '已过期', cancelled: '已取消', failed: '失败' };
    for (const order of orders) {
      const row = el('tr'); const account = el('td'); account.append(el('div', String(order.id), 'user-primary'), el('div', order.user_id, 'user-secondary'));
      row.append(account, el('td', order.product_name), el('td', `${order.price} ${order.currency_name || settings.currency_name}`), el('td', orderLabels[order.status] || order.status), el('td', order.detail || '—', 'detail-cell')); $('orders-body').append(row);
    }
  }
  async function refreshStock() {
    if (!ready || busy || refreshingStock || page !== 'editor' || !state.session || finishConfirmation || document.hidden) return;
    const session = state.session;
    const version = sequence;
    const revision = stockRevision;
    refreshingStock = true;
    try {
      const data = result(await bridge.apiGet('state', { session }));
      if (busy || page !== 'editor' || state.session !== session || version !== sequence || revision !== stockRevision) return;
      const remote = new Map((data.settings?.products || []).map(product => [product.id, product]));
      const focused = $('settings-form').contains(document.activeElement) && document.activeElement.matches('input,textarea,select');
      let deferred = false;
      for (const row of $('products-list').children) {
        const id = row.querySelector('[data-field="id"]').value.trim();
        const product = remote.get(id);
        if (!product || product.reward_type !== 'codes') continue;
        const codes = row.querySelector('[data-field="codes"]');
        row.querySelector('[data-stock-count]').textContent = `后台剩余 ${product.codes.length} 条`;
        const previous = settings.products.find(item => item.id === id);
        const stockChanged = !previous || JSON.stringify(previous.codes) !== JSON.stringify(product.codes);
        if (stockChanged && (dirty || focused)) { deferred = true; continue; }
        if (!dirty && !focused) {
          codes.value = product.codes.join('\n');
          if (previous) previous.codes = [...product.codes];
        }
      }
      state.orders = data.orders || []; state.leaderboard = data.leaderboard || [];
      renderActivity();
      $('stock-status').textContent = deferred ? '后台库存已变化。正在编辑的内容已保留；下次保存时会自动剔除已分配的兑换码。' : `库存与订单已同步 · ${new Date().toLocaleTimeString('zh-CN')}；编辑中的表单不会被覆盖。`;
      $('stock-status').className = `hint stock-status${deferred ? ' stock-changed' : ''}`;
    } catch (error) {
      if (state.session === session && version === sequence && revision === stockRevision && page === 'editor') $('stock-status').textContent = '库存同步暂时失败，将自动重试；保存时仍会核对已分配的兑换码。';
    } finally { refreshingStock = false; }
  }
  async function load(session = '') {
    const request = ++sequence;
    lock(true);
    try {
      const data = result(await bridge.apiGet('state', session ? { session } : {}));
      if (request !== sequence) return;
      state = { ...data, session: data.session || session || '' };
      renderSessions(); renderBindings(); renderForm(data.settings); renderHistory(data.history); renderActivity();
      markSaved(state.session ? '设置已加载' : '添加一个会话，开始配置');
      return true;
    } catch (error) { $('session-select').value = state.session; notify(`加载失败：${error.message || error}`, 'error'); return false; }
    finally { if (request === sequence) lock(false); }
  }
  $('settings-form').addEventListener('input', markDirty);
  $('settings-form').addEventListener('change', markDirty);
  $('add-window').addEventListener('click', () => { addWindow(); markDirty(); });
  $('add-date').addEventListener('click', () => { addDate(); markDirty(); });
  $('add-product').addEventListener('click', () => { addProduct(); markDirty(); });
  $('new-session').addEventListener('click', () => { $('new-session-panel').hidden = false; $('session-input').focus(); });
  $('cancel-session').addEventListener('click', () => { $('new-session-panel').hidden = true; });
  $('session-select').addEventListener('change', async () => {
    const selected = $('session-select').value; $('session-select').value = state.session;
    if (!await mayLeave()) return;
    notify(''); await load(selected);
  });
  $('add-session').addEventListener('click', async () => {
    const session = $('session-input').value.trim();
    if (!/^[^:\s]+:GroupMessage:[^:\s]+$/.test(session)) { notify('请输入有效的群会话标识：平台ID:GroupMessage:群号。', 'error'); return; }
    $('new-session-panel').hidden = true;
    const existing = state.sessions.some(item => item.session === session);
    if (!await load(session)) return;
    showPage('editor');
    if (!existing) { markDirty(); notify('新会话尚未保存。完成设置后点击「保存当前会话」。'); }
  });
  $('back-overview').addEventListener('click', async () => { if (!await mayLeave()) return; notify(''); if (await load()) showPage('overview'); });
  $('refresh').addEventListener('click', async () => { if (page === 'editor' && !await mayLeave()) return; notify(''); await load(page === 'editor' ? state.session : ''); });
  $('discard').addEventListener('click', async () => { if (!await mayLeave()) return; notify(''); await load(state.session); });
  $('confirm-cancel').addEventListener('click', () => finishConfirmation?.(false));
  $('confirm-accept').addEventListener('click', () => finishConfirmation?.(true));
  document.addEventListener('keydown', event => {
    if (!finishConfirmation) return;
    if (event.key === 'Escape') { event.preventDefault(); finishConfirmation(false); }
    if (event.key === 'Tab') { event.preventDefault(); (document.activeElement === $('confirm-cancel') ? $('confirm-accept') : $('confirm-cancel')).focus(); }
  });
  document.querySelectorAll('[data-token]').forEach(button => button.addEventListener('click', () => {
    const field = $('commands'); const start = field.selectionStart; const end = field.selectionEnd;
    field.setRangeText(button.dataset.token, start, end, 'end'); field.focus(); markDirty();
  }));
  $('sync-bindings').addEventListener('click', async () => {
    lock(true);
    try { const data = result(await bridge.apiPost('bindings/sync', {})); state.binding_count = data.count; renderBindings(); notify(`已同步 ${data.count} 位玩家的绑定信息。`, 'success'); }
    catch (error) { notify(`绑定同步失败：${error.message || error}`, 'error'); }
    finally { lock(false); }
  });
  $('settings-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !state.session || !$('settings-form').reportValidity()) return;
    const data = collect();
    try { validate(data); } catch (error) { notify(error.message, 'error'); return; }
    lock(true); $('save').textContent = '正在保存…';
    try {
      const saved = result(await bridge.apiPost('settings', { session: state.session, settings: data }));
      renderForm(saved.settings || data);
      const index = state.sessions.findIndex(item => item.session === state.session);
      const item = { session: state.session, settings };
      if (index >= 0) state.sessions[index] = item; else state.sessions.push(item);
      renderSessions(); markSaved('已保存，当前会话立即生效'); notify('签到规则已保存。', 'success');
    } catch (error) { notify(`保存失败，修改仍保留：${error.message || error}`, 'error'); }
    finally { $('save').textContent = '保存当前会话'; lock(false); }
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  async function init() {
    if (!bridge || typeof bridge.ready !== 'function') { notify('无法连接 AstrBot。请从 AstrBot 的插件页面打开此面板。', 'error'); $('save-state').textContent = '未连接 AstrBot'; return; }
    lock(true);
    try { await bridge.ready(); ready = true; await load(); window.setInterval(refreshStock, 12000); }
    catch (error) { notify(`AstrBot 连接失败：${error.message || error}`, 'error'); $('save-state').textContent = '连接失败'; lock(false); }
  }
  init();
})();
