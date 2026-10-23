/* Native AstrBot plugin view. All data crosses the authenticated bridge. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
  const defaults = { enabled: false, timezone: 'Asia/Shanghai', min_points: 10, max_points: 30, windows: [{ start: '00:00', end: '23:59' }], date_rules: [], mc_enabled: false, server_name: '', require_binding: true, commands: [] };
  let state = { sessions: [], servers: [], session: '', binding_count: 0 };
  let settings = structuredClone(defaults);
  let dirty = false;
  let busy = false;
  let ready = false;
  let sequence = 0;

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
    busy = value;
    $('settings-fields').disabled = value || !state.session || !ready;
    $('save').disabled = value || !state.session || !ready;
    $('refresh').disabled = value || !ready;
    $('session-select').disabled = value || !ready;
    $('new-session').disabled = value || !ready;
    $('add-session').disabled = value || !ready;
    $('discard').disabled = value || !dirty;
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
  function collect() {
    return { ...settings,
      enabled: $('enabled').checked, timezone: $('timezone').value.trim(),
      min_points: Number($('min-points').value), max_points: Number($('max-points').value),
      windows: [...$('windows-list').children].map(row => { const inputs = row.querySelectorAll('input'); return { start: inputs[0].value, end: inputs[1].value }; }),
      date_rules: [...$('dates-list').children].map(row => { const inputs = row.querySelectorAll('input'); return { date: inputs[0].value.trim(), multiplier: Number(inputs[1].value) }; }),
      mc_enabled: $('mc-enabled').checked, server_name: $('server-name').value,
      require_binding: $('require-binding').checked,
      commands: $('commands').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
    };
  }
  function validate(data) {
    if (data.max_points < data.min_points) throw new Error('最高积分不能小于最低积分。');
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
  }
  function preview() {
    const data = collect();
    $('stat-points').textContent = `${data.min_points}–${data.max_points}`;
    $('stat-dates').textContent = data.date_rules.length;
    $('stat-status').textContent = data.enabled ? '签到已启用' : '签到已关闭';
    $('stat-status').className = `badge ${data.enabled ? 'good' : 'neutral'}`;
    $('preview-summary').textContent = data.enabled ? `每天签到一次，随机获得 ${data.min_points}–${data.max_points} 积分。特别日期使用对应倍率。` : '此会话已关闭签到；启用并保存后，群成员即可参加。';
    $('preview-time').textContent = `${data.timezone || '未设置时区'} · ${data.windows.map(w => `${w.start || '…'}—${w.end || '…'}`).join(' / ')}`;
    $('preview-mc').textContent = data.mc_enabled ? `同步至 ${data.server_name || '尚未选择的服务器'}，发送 ${data.commands.length} 条指令。${data.require_binding ? '签到前需要绑定游戏账号。' : '未绑定玩家仍可领取群积分。'}` : 'MC 同步已关闭，仅记录群内签到与积分。';
  }
  function renderForm(data) {
    settings = { ...structuredClone(defaults), ...(data || {}) };
    $('enabled').checked = settings.enabled;
    $('min-points').value = settings.min_points;
    $('max-points').value = settings.max_points;
    $('timezone').value = settings.timezone;
    $('mc-enabled').checked = settings.mc_enabled;
    $('require-binding').checked = settings.require_binding;
    $('commands').value = settings.commands.join('\n');
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
      row.append(user, el('td', record.day || '—'), el('td', `+${record.points ?? 0}`, 'points-cell'), el('td', String(record.total ?? '—')), statusCell, el('td', record.mc_detail || '—', 'detail-cell'));
      $('history-body').append(row);
    }
  }
  async function load(session = '') {
    const request = ++sequence;
    lock(true);
    try {
      const data = result(await bridge.apiGet('state', session ? { session } : {}));
      if (request !== sequence) return;
      state = { ...data, session: data.session || session || '' };
      renderSessions(); renderBindings(); renderForm(data.settings); renderHistory(data.history);
      markSaved(state.session ? '设置已加载' : '添加一个会话，开始配置');
    } catch (error) { $('session-select').value = state.session; notify(`加载失败：${error.message || error}`, 'error'); }
    finally { if (request === sequence) lock(false); }
  }
  $('settings-form').addEventListener('input', markDirty);
  $('settings-form').addEventListener('change', markDirty);
  $('add-window').addEventListener('click', () => { addWindow(); markDirty(); });
  $('add-date').addEventListener('click', () => { addDate(); markDirty(); });
  $('new-session').addEventListener('click', () => { $('new-session-panel').hidden = false; $('session-input').focus(); });
  $('cancel-session').addEventListener('click', () => { $('new-session-panel').hidden = true; });
  $('session-select').addEventListener('change', async () => {
    if (dirty) { $('session-select').value = state.session; notify('请先保存当前修改，或点击「放弃修改」，再切换会话。'); return; }
    notify(''); await load($('session-select').value);
  });
  $('add-session').addEventListener('click', () => {
    if (dirty) { notify('请先保存当前修改，或点击「放弃修改」，再新增会话。'); return; }
    const session = $('session-input').value.trim();
    if (!/^[^:\s]+:GroupMessage:[^:\s]+$/.test(session)) { notify('请输入有效的群会话标识：平台ID:GroupMessage:群号。', 'error'); return; }
    $('new-session-panel').hidden = true;
    if (state.sessions.some(item => item.session === session)) { load(session); return; }
    state.session = session; renderSessions(); renderForm(defaults); renderHistory(); markDirty(); lock(false);
    $('save-caption').textContent = session; notify('新会话尚未保存。完成设置后点击「保存当前会话」。');
  });
  $('refresh').addEventListener('click', async () => { if (dirty) { notify('当前有未保存的修改，请先保存或放弃修改再刷新。'); return; } notify(''); await load(state.session); });
  $('discard').addEventListener('click', async () => { notify(''); await load(state.session); });
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
    try { await bridge.ready(); ready = true; await load(); }
    catch (error) { notify(`AstrBot 连接失败：${error.message || error}`, 'error'); $('save-state').textContent = '连接失败'; lock(false); }
  }
  init();
})();
