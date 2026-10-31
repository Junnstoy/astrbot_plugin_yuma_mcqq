const $ = (selector, parent = document) => parent.querySelector(selector);
const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
// getRandomValues also works when a LAN AstrBot panel uses plain HTTP.
const randomId = () => [...crypto.getRandomValues(new Uint8Array(16))].map(b => b.toString(16).padStart(2, '0')).join('');
const pages = [
  ['overview', '总览', '01', '连接游戏与群聊，让每个动作都有记录。'],
  ['routes', '连接与群服', '02', '设置 QueQiao 连接与原始 UMO 路由。'],
  ['commands', '自定义命令', '03', '独立动作、解析规则与回复模板。触发名称在管理行为中修改。'],
  ['bindings', '绑定与点数', '04', '双向唯一绑定、变更冷却与完整点数流水。'],
  ['shop', '点数商店', '05', '商品、库存和兑换订单；结果未知时保留现场。'],
  ['monitor', '性能监控', '06', '持续采样 TPS、MSPT 与延迟，缺失样本保留为空。'],
  ['diagnostics', '诊断与迁移', '07', '回包调试、操作记录与显式数据迁移。'],
];
let state = null;
let current = 'overview';
let selectedScope = '';
let selectedServer = '';
let metrics = [];
let migrationPreview = null;

function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}
function button(text, fn, cls = 'secondary small') {
  const node = element('button', text, cls);
  node.type = 'button';
  node.addEventListener('click', () => Promise.resolve().then(fn).catch(showError));
  return node;
}
function showError(error) { $('#banner').hidden = false; $('#banner').textContent = String(error.message || error); }
function unwrap(response) {
  if (response?.status === 'error' || response?.success === false) throw new Error(response.message || response.error || '请求失败');
  if (response && 'data' in response && (response.status === 'ok' || response.status === 'success' || response.success === true || typeof response.code === 'number')) return response.data;
  return response;
}
async function apiGet(path) { return unwrap(await bridge.apiGet(path)); }
async function action(name, payload = {}) { return unwrap(await bridge.apiPost('action', { action: name, payload })); }
async function refresh() {
  state = await apiGet('state');
  $('#banner').hidden = true;
  const connected = (state.connections || []).filter(x => x.connected || x.status === 'connected').length;
  $('#connection').textContent = `${connected} / ${state.settings.servers.length} 已连接`;
  $('#connection').classList.toggle('off', connected === 0);
  render();
}
function output(title, value) {
  $('#output-title').textContent = title;
  $('#output-text').textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  $('#output').showModal();
}
async function saveSettings(patch) {
  await bridge.apiPost('settings', { settings: patch, revision: state.revision }).then(unwrap);
  await refresh();
}
function panel(title, description, tools = []) {
  const section = element('div', null, 'panel');
  const head = element('div', null, 'panel-head');
  const labels = element('div'); labels.append(element('h2', title));
  if (description) labels.append(element('p', description));
  head.append(labels);
  const actions = element('div', null, 'actions'); tools.forEach(x => actions.append(x)); head.append(actions);
  section.append(head); $('#content').append(section); return section;
}
function table(parent, columns, rows, actions) {
  if (!rows?.length) { parent.append(element('div', '还没有记录', 'empty')); return; }
  const wrap = element('div', null, 'table-wrap'); const t = element('table');
  const tr = element('tr'); columns.forEach(c => tr.append(element('th', c[0]))); if (actions) tr.append(element('th', '操作'));
  const thead = element('thead'); thead.append(tr); t.append(thead); const body = element('tbody');
  rows.forEach(row => {
    const line = element('tr');
    columns.forEach(([, key]) => {
      let value = typeof key === 'function' ? key(row) : row[key];
      if (typeof value === 'object' && value !== null) value = JSON.stringify(value);
      line.append(element('td', value ?? '—'));
    });
    if (actions) { const td = element('td'); const div = element('div', null, 'actions'); actions(row).forEach(x => div.append(x)); td.append(div); line.append(td); }
    body.append(line);
  }); t.append(body); wrap.append(t); parent.append(wrap);
}
function field(label, key, type = 'text', options = {}) { return { label, key, type, ...options }; }
function edit(title, specs, initial, onSave, saveLabel = '保存') {
  $('#dialog-title').textContent = title; $('#fields').replaceChildren(); $('#dialog-error').textContent = ''; $('#save').textContent = saveLabel;
  const controls = [];
  specs.forEach(spec => {
    const label = element('label', null, `field ${spec.wide || spec.type === 'json' || spec.type === 'textarea' ? 'wide' : ''} ${spec.type === 'checkbox' ? 'check' : ''}`);
    label.append(element('span', spec.label));
    const control = element(spec.type === 'select' ? 'select' : ['json', 'textarea'].includes(spec.type) ? 'textarea' : 'input');
    control.setAttribute('aria-label', spec.label);
    if (control.tagName === 'INPUT') control.type = spec.type;
    if (spec.type === 'select') (spec.options || []).forEach(v => { const option = element('option', Array.isArray(v) ? v[1] : v); option.value = Array.isArray(v) ? v[0] : v; control.append(option); });
    const value = initial[spec.key] ?? spec.default ?? '';
    if (spec.type === 'checkbox') control.checked = Boolean(value);
    else control.value = spec.type === 'json' ? JSON.stringify(value || (spec.object ? {} : []), null, 2) : String(value);
    if (spec.readonly) control.readOnly = true;
    if (spec.required) control.required = true;
    if (spec.min !== undefined) control.min = spec.min;
    if (spec.max !== undefined) control.max = spec.max;
    if (spec.placeholder) control.placeholder = spec.placeholder;
    label.append(control); if (spec.hint) label.append(element('small', spec.hint));
    $('#fields').append(label); controls.push([spec, control]);
  });
  $('#edit-form').onsubmit = async event => {
    event.preventDefault(); $('#dialog-error').textContent = ''; $('#save').disabled = true;
    try {
      const value = structuredClone(initial);
      controls.forEach(([spec, control]) => {
        value[spec.key] = spec.type === 'checkbox' ? control.checked : spec.type === 'number' ? Number(control.value) : spec.type === 'json' ? JSON.parse(control.value || (spec.object ? '{}' : '[]')) : control.value;
      });
      await onSave(value); $('#editor').close();
    } catch (error) { $('#dialog-error').textContent = String(error.message || error); }
    finally { $('#save').disabled = false; }
  }; $('#editor').showModal();
}
const nowText = value => value ? new Date(Number(value) * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
function scopeSelect(parent, fn = render) {
  const options = [...new Set(state.settings.routes.map(x => x.scope || x.umo))];
  if (!options.includes(selectedScope)) selectedScope = options[0] || '';
  const select = element('select', null, 'scope-select');
  options.forEach(value => { const option = element('option', value); option.value = value; select.append(option); }); select.value = selectedScope;
  select.onchange = () => { selectedScope = select.value; fn(); }; parent.append(select);
}
function serverSelect(parent, fn) {
  if (!state.settings.servers.some(x => x.id === selectedServer)) selectedServer = state.settings.servers[0]?.id || '';
  const select = element('select', null, 'scope-select'); state.settings.servers.forEach(s => { const o = element('option', s.name || s.id); o.value = s.id; select.append(o); }); select.value = selectedServer;
  select.onchange = () => { selectedServer = select.value; fn?.(); }; parent.append(select);
}
function filtered(key) { return (state.domain?.[key] || []).filter(x => !selectedScope || x.scope === selectedScope); }
function render() {
  const page = pages.find(x => x[0] === current); $('#title').textContent = page[1]; $('#subtitle').textContent = page[3];
  $('#nav').querySelectorAll('button').forEach(n => n.classList.toggle('active', n.dataset.page === current));
  $('#content').replaceChildren(); if (!state) return;
  ({ overview, routes, commands, bindings, shop, monitor, diagnostics })[current]();
}
function overview() {
  const cards = element('div', null, 'cards');
  [['服务器', state.settings.servers.length, 'QueQiao 连接'], ['群服路由', state.settings.routes.length, '按原始 UMO 定位'], ['已绑定账号', state.domain?.bindings?.length || 0, 'QQ ↔ MC 双向唯一'], ['待核对订单', (state.domain?.orders || []).filter(x => x.status === 'unknown').length, '结果未知不自动重发']].forEach(([label, value, hint]) => { const c = element('div', null, 'card'); c.append(element('div', label, 'label'), element('div', value, 'number'), element('div', hint, 'hint')); cards.append(c); }); $('#content').append(cards);
  const p = panel('连接状态', '所有后台请求都有独立的服务器与响应关联。', [button('全局设置', globalSettings)]);
  table(p, [['服务器', x => x.name || x.server_id || x.id], ['状态', x => x.connected ? '已连接' : x.status || '未连接'], ['最近错误', x => x.last_error || '—']], state.connections);
  const f = panel('消息处理顺序', '明确指令优先处理，AI 消息交回 AstrBot，普通消息最后转发。'); const flow = element('div', null, 'flow');
  ['指令与权限', 'AI 唤醒 / 接管', '普通消息转发'].forEach((s, i) => { if (i) flow.append(element('i', '→')); flow.append(element('span', s)); }); f.append(flow);
  f.append(element('div', '首次使用：添加服务器 → 添加 UMO 路由 → 在游戏完成绑定验证 → 使用“签到”。商店兑换记录与点数流水独立保存。', 'note'));
}
function globalSettings() {
  edit('全局与媒体设置', [field('插件启用', 'enabled', 'checkbox'), field('换绑冷却（秒）', 'binding_cooldown_seconds', 'number', { min: 0 }), field('时区', 'timezone'), field('媒体服务配置', 'media', 'json', { object: true, hint: 'public_base 必须可被玩家浏览器访问；trusted_local_roots 仅填允许公开的媒体目录。' }), field('采样配置', 'monitor', 'json', { object: true, hint: 'interval_seconds ≥ 10；采样与群推送分开，当前按指令返回图片。' })], state.settings, async v => saveSettings({ enabled: v.enabled, binding_cooldown_seconds: v.binding_cooldown_seconds, timezone: v.timezone, media: v.media, monitor: v.monitor }));
}
function serverEditor(existing) {
  const value = existing || { id: randomId().slice(0,8), name: '', url: 'ws://127.0.0.1:8080', token: '', enabled: true, timeout: 10, game_host: '127.0.0.1', game_port: 25565, tps_command: 'spark tps', mspt_command: 'mspt', ping_command: 'spark ping', component_version: 'legacy' };
  edit(existing ? '编辑服务器' : '添加服务器', [field('服务器 ID', 'id', 'text', { readonly: !!existing, required: true }), field('QueQiao server_name', 'name', 'text', { required: true }), field('WebSocket URL', 'url', 'text', { wide: true, required: true }), field('Access Token', 'token', 'password', { hint: '已有 token 用掩码显示，保持掩码即可保留。' }), field('请求超时（秒）', 'timeout', 'number'), field('游戏主机', 'game_host'), field('游戏端口', 'game_port', 'number'), field('TPS 指令', 'tps_command'), field('MSPT 指令', 'mspt_command'), field('玩家延迟指令', 'ping_command'), field('文本组件版本', 'component_version', 'select', { options: [['legacy','传统 clickEvent'],['modern','新版 click_event']] }), field('启用连接', 'enabled', 'checkbox')], value, async v => { const list = state.settings.servers.filter(x => x.id !== v.id); list.push(v); await saveSettings({ servers: list }); });
}
function routeEditor(existing) {
  const value = existing || { umo: '', scope: '', server_id: state.settings.servers[0]?.id || '', enabled: true, qq_to_mc: true, mc_to_qq: true, admins: [], timezone: 'Asia/Shanghai', points_min: 10, points_max: 10, signin_command: 'signin click {player}', signin_prefixes: ['[手记人偶]'], templates: {}, events: ['PlayerChatEvent','PlayerJoinEvent','PlayerQuitEvent','PlayerDeathEvent','PlayerAchievementEvent'], types: ['text','image','link','record','video','file','forward','reply','at'] };
  edit(existing ? '编辑群服路由' : '添加群服路由', [field('原始 UMO', 'umo', 'text', { required: true, wide: true, readonly: !!existing, hint: '在目标会话使用 AstrBot /sid 获取，不要用裸群号。' }), field('点数作用域', 'scope', 'text', { hint: '留空使用 UMO；仅在明确需要共享点数时使用相同作用域。' }), field('目标服务器', 'server_id', 'select', { options: state.settings.servers.map(x=>[x.id,x.name]) }), field('启用', 'enabled', 'checkbox'), field('QQ → MC', 'qq_to_mc', 'checkbox'), field('MC → QQ', 'mc_to_qq', 'checkbox'), field('时区', 'timezone'), field('签到点数下限', 'points_min', 'number', { min: 0 }), field('签到点数上限', 'points_max', 'number', { min: 0 }), field('游戏签到命令', 'signin_command', 'text', { wide: true }), field('会话管理员 QQ ID', 'admins', 'json'), field('签到回包前缀', 'signin_prefixes', 'json'), field('签到结果规则', 'signin_rules', 'json', { hint: '留空使用内置 LiteSignIn 样本；自定义状态为 SIGNED / ALREADY_SIGNED / FAILED，命名捕获 player 必须匹配账号。' }), field('QQ 转发前缀', 'prefix', 'text', { default: '[{group}] {sender}：' }), field('合并消息节点上限', 'max_forward_nodes', 'number', { default: 30, min: 1, max: 100 }), field('回复模板覆盖', 'templates', 'json', { object: true }), field('转发消息类型', 'types', 'json'), field('游戏事件开关', 'events', 'json')], value, async v => { v.scope ||= v.umo; const list = state.settings.routes.filter(x => x.umo !== v.umo); list.push(v); await saveSettings({ routes: list }); });
}
function routes() {
  const p = panel('服务器连接', 'name 必须与 QueQiao 的 server_name 一致。', [button('全局 / 媒体设置',globalSettings), button('添加服务器',()=>serverEditor(), '')]);
  table(p,[['名称','name'],['ID','id'],['地址','url'],['启用',x=>x.enabled?'是':'否']],state.settings.servers,s=>[button('编辑',()=>serverEditor(s)),button('删除',async()=>{if(confirm('删除该服务器？请先移除引用它的路由和命令。'))await saveSettings({servers:state.settings.servers.filter(x=>x.id!==s.id)});},'danger small')]);
  const q = panel('会话与路由', '积分账户按作用域隔离，消息始终按 UMO 路由。', [button('添加路由',()=>routeEditor(), '')]);
  table(q,[['UMO','umo'],['服务器','server_id'],['点数作用域','scope'],['签到奖励',x=>`${x.points_min}–${x.points_max}`]],state.settings.routes,r=>[button('编辑',()=>routeEditor(r)),button('删除',async()=>{if(confirm('仅删除路由配置，保留历史数据。继续？'))await saveSettings({routes:state.settings.routes.filter(x=>x.umo!==r.umo)});},'danger small')]);
}
function commandEditor(existing) {
  const value = existing || { id:randomId().slice(0,8),name:'',aliases:[],description:'',enabled:true,require_binding:true,admin_only:true,cooldown_seconds:3,server_id:'',rcon:'',parser:'rules',cleanup_prefixes:[],parameters:[],rules:[],templates:{SUCCESS:'{cleaned}',UNKNOWN:'服务器结果未识别，请管理员检查。'} };
  edit(existing?'编辑命令动作':'新增命令动作',[field('稳定动作 ID','id','text',{readonly:!!existing,required:true}),field('初始触发名','name','text',{required:true,hint:'已注册后的实际触发名以 AstrBot 管理行为为准。'}),field('说明','description','text',{wide:true}),field('初始别名','aliases','json'),field('目标服务器','server_id','select',{options:[['','跟随群服路由'],...state.settings.servers.map(x=>[x.id,x.name])]}),field('调用冷却（秒）','cooldown_seconds','number'),field('动作启用','enabled','checkbox'),field('需要绑定','require_binding','checkbox'),field('仅管理员','admin_only','checkbox'),field('RCON 命令模板','rcon','text',{wide:true,required:true,hint:'可用 {player}、{user_id} 以及下方定义的参数。一条动作执行一条控制台命令。'}),field('参数定义','parameters','json',{hint:'例：[{"name":"amount","type":"integer","min":1,"max":64}]'}),field('解析方式','parser','select',{options:[['rules','完整正则匹配'],['text','纯文本查询'],['litesignin','LiteSignIn 签到回包']]}),field('清洗前缀','cleanup_prefixes','json'),field('结果规则','rules','json',{hint:'例：[{"status":"SUCCESS","pattern":"成功给玩家 (?P<player>\\w+) ..."}]'}),field('按状态选择回复模板','templates','json',{object:true})],value,async v=>{const list=state.settings.commands.filter(x=>x.id!==v.id);list.push(v);await saveSettings({commands:list});});
}
function commands() {
  const p=panel('自定义命令','命令使用独立处理器；稳定 ID 不随触发词改名。',[button('新增命令',()=>commandEditor(),'')]);
  table(p,[['动作 ID','id'],['初始触发名','name'],['RCON','rcon'],['解析','parser'],['权限',x=>x.admin_only?'管理员':'成员']],state.settings.commands,c=>[button('编辑',()=>commandEditor(c)),button('回包测试',()=>preview(c)),button('删除',async()=>{if(confirm('删除此命令动作及其注册入口？'))await saveSettings({commands:state.settings.commands.filter(x=>x.id!==c.id)});},'danger small')]);
  const q=panel('管理行为生效值','在 AstrBot → 插件 → 管理行为中修改名称、别名和入口启停。');
  const effective=state.effective_commands || []; table(q,[['动作',x=>x.action_id||x.id],['实际触发名',x=>x.name||x.command],['别名',x=>x.aliases||[]],['入口启用',x=>x.enabled===false?'否':'是']],Array.isArray(effective)?effective:Object.values(effective));
}
function preview(command={parser:'litesignin',rules:[],templates:{SIGNED:'游戏签到成功，玩家 {player}，连续 {days} 天。',ALREADY_SIGNED:'{player} 今日已签到。'}}) {
  edit('回包解析预览',[field('目标玩家','player','text',{default:'rincho'}),field('原始 RCON 回包','raw','textarea',{wide:true,default:'Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e1 §a天。'}),field('补充模板变量','values','json',{object:true})],{player:'rincho',raw:'Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e1 §a天。',values:{}},async v=>output('清洗 / 匹配 / 模板预览',await action('preview',{...v,command})),'运行预览');
}
function bindings() {
  const p=panel('账号绑定','游戏内使用一次性验证码验证控制权。解绑保留冷却和历史。');
  table(p,[['QQ','qq_id'],['玩家','player'],['UUID','mc_uuid'],['服务器','server_id'],['变更时间',x=>nowText(x.updated)]],state.domain?.bindings,b=>[button('管理员解绑',async()=>{if(confirm(`解绑 ${b.qq_id} ↔ ${b.player}？冷却仍会保留。`)){await action('binding_unbind',{user_id:b.qq_id});await refresh();}},'danger small')]);
  const q=panel('点数账户','余额和冻结额分开显示。管理员调整也写入流水。',[button('调整点数',()=>edit('管理员调整点数',[field('QQ ID','user_id','text',{required:true}),field('操作','mode','select',{options:[['add','增加'],['sub','扣除'],['set','设置余额']]}),field('数量','amount','number',{min:0,required:true}),field('原因','reason','text',{required:true,wide:true})],{user_id:'',mode:'add',amount:0,reason:'',operation_id:randomId()},async v=>{output('调整结果',await action('points_adjust',{...v,scope:selectedScope}));await refresh();}),'')]);
  scopeSelect(q);table(q,[['QQ','qq_id'],['总点数',x=>x.total??x.balance],['冻结','frozen'],['可用',x=>(x.total??x.balance??0)-(x.frozen||0)]],filtered('balances'));
  const l=panel('最近流水','点数变更均保存来源与操作人。');table(l,[['QQ','qq_id'],['变动','delta'],['变更后','total_after'],['原因','reason'],['操作人','actor'],['时间',x=>nowText(x.created)]],filtered('ledger').slice(0,100));
  const s=panel('签到记录','游戏成功或今日已签后，才结算群签到。');table(s,[['QQ','qq_id'],['日期','day'],['点数','points'],['游戏状态','game_status']],filtered('signins').slice(0,100));
}
async function productEditor(existing) {
  let value={id:randomId().slice(0,8),name:'',price:10,kind:'text',reward:'',command:'',rules:[],enabled:true,stock:-1,limit_per_user:0};
  if(existing){const detail=await action('product_get',{scope:selectedScope,product_id:existing.id});value={...value,...(detail.product||detail)};}
  edit(existing?'编辑商品':'新增商品',[field('商品 ID','id','text',{readonly:!!existing,required:true}),field('名称','name','text',{required:true}),field('价格（点数）','price','number',{min:0,required:true}),field('交付方式','kind','select',{options:[['text','私聊固定文本'],['code','私聊独立兑换码'],['rcon','游戏命令交付']]}),field('库存（-1 不限）','stock','number',{min:-1}),field('每人累计限购（0 不限）','limit_per_user','number',{min:0}),field('上架','enabled','checkbox'),field('私聊奖励文本','reward','textarea',{hint:'仅通过私聊发送，不出现在商品列表中。'}),field('游戏交付命令','command','text',{wide:true,hint:'RCON 商品使用。可用 {player}、{user_id}、{order_id} 等交付字段。'}),field('游戏交付结果规则','rules','json',{hint:'必须能明确确认成功；超时或无法识别进入待核对。'})],value,async v=>{await action('product_save',{scope:selectedScope,product:v});await refresh();});
}
function shop() {
  const p=panel('商品目录','群点数只在本商店兑换，独立兑换码分配后不会重新入池。',[button('新增商品',()=>{if(!selectedScope)throw Error('请先配置群服路由');return productEditor();},'')]);scopeSelect(p);
  table(p,[['ID','id'],['名称','name'],['价格','price'],['类型','kind'],['库存',x=>x.stock===-1?'不限':x.stock],['状态',x=>x.enabled?'上架':'下架']],filtered('products'),r=>[button('编辑',()=>productEditor(r)),...(r.kind==='code'?[button('添加兑换码',()=>edit('导入兑换码',[field('兑换码（每行一个）','codes','textarea',{required:true})],{codes:''},async v=>{await action('product_codes',{scope:selectedScope,product_id:r.id,codes:v.codes.split(/\r?\n/).map(x=>x.trim()).filter(Boolean)});await refresh();}))]:[]),button('下架 / 删除',async()=>{if(confirm('删除商品定义？历史订单和已用兑换码会保留。')){await action('product_delete',{scope:selectedScope,product_id:r.id});await refresh();}},'danger small')]);
  const q=panel('兑换订单','只有确认交付成功才完成结算；未知结果由管理员核对。');
  table(q,[['订单','id'],['QQ','qq_id'],['商品','product_id'],['状态','status'],['时间',x=>nowText(x.created)]],filtered('orders').slice(0,100),r=>['unknown','reserved'].includes(r.status)?[button('核对',()=>edit('核对交付结果',[field('结论','outcome','select',{options:[['success','已经交付：完成扣款'],['failed','确认未交付：取消并释放冻结']]}),field('核对说明','reason','textarea',{required:true})],{outcome:'success',reason:''},async v=>{await action('order_resolve',{...v,scope:selectedScope,order_id:r.id});await refresh();},'提交核对'))]:[]);
}
function metricValue(sample,key){ const value=sample[key]??sample.metrics?.[key];return typeof value==='number'&&Number.isFinite(value)?value:null; }
function drawChart(parent, samples, key, label, unit) {
  const box=element('div',null,'panel');const head=element('div',null,'metric-head');const valid=samples.map(x=>metricValue(x,key)).filter(x=>x!==null);head.append(element('span',label),element('strong',valid.length?`${valid.at(-1).toFixed(1)} ${unit}`:'暂无数据'));box.append(head);
  const ns='http://www.w3.org/2000/svg';const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 600 200');svg.classList.add('chart');svg.setAttribute('role','img');svg.setAttribute('aria-label',label+'历史曲线');
  const max=Math.max(key==='tps'?20:1,...valid)*1.1;let path='';let active=false;
  const start=Number(samples[0]?.timestamp||0),end=Number(samples.at(-1)?.timestamp||start),interval=state.settings.monitor.interval_seconds||60;let previous=null;
  samples.forEach(s=>{const v=metricValue(s,key),time=Number(s.timestamp);if(v===null){active=false;return;}if(previous!==null&&time-previous>interval*2.5)active=false;const x=20+((time-start)/Math.max(1,end-start))*560,y=175-(v/max)*145;path+=`${active?'L':'M'}${x.toFixed(2)},${y.toFixed(2)} `;active=true;previous=time;});
  [30,100,175].forEach(y=>{const l=document.createElementNS(ns,'line');['x1','x2','y1','y2'].forEach((a,i)=>l.setAttribute(a,String([20,580,y,y][i])));l.setAttribute('stroke','#2b3b36');svg.append(l);});
  const line=document.createElementNS(ns,'path');line.setAttribute('d',path);line.setAttribute('fill','none');line.setAttribute('stroke','#a4dfbc');line.setAttribute('stroke-width','2');svg.append(line);box.append(svg);if(samples.length)box.append(element('small',`${nowText(start)} → ${nowText(end)} · 纵轴 0–${max.toFixed(1)} ${unit}`));parent.append(box);
}
async function loadMetrics(){const target=selectedServer;if(!target)return;const r=await action('metrics',{server_id:target,hours:24});if(target!==selectedServer)return;metrics=r.samples||[];if(current==='monitor')render();}
function monitor(){
  const p=panel('性能历史','最近 24 小时；图中断线表示样本缺失。',[button('刷新图表',loadMetrics),button('立即采样',async()=>{await action('sample',{server_id:selectedServer});await loadMetrics();}),button('采样设置',globalSettings)]);serverSelect(p,()=>loadMetrics().catch(showError));
  if(!selectedServer){p.append(element('div','请先添加服务器','empty'));return;}
  const grid=element('div',null,'grid-two');$('#content').append(grid);drawChart(grid,metrics,'tps','TPS','');drawChart(grid,metrics,'mspt','MSPT','ms');drawChart(grid,metrics,'player_ping_ms','玩家延迟','ms');drawChart(grid,metrics,'slp_rtt_ms','机器人 → 服务端 RTT','ms');
  const note=element('div','采样按配置间隔在后台进行。QQ群使用“服务器状态 / tps / mspt / ping”获取历史图片。玩家 ping 与机器人探测链路分别记录，未知值不填 0。','note');$('#content').append(note);
}
function diagnostics(){
  const p=panel('回包与连接诊断','解析预览不会执行游戏命令。',[button('LiteSignIn 样本预览',()=>preview()),button('执行 RCON',()=>edit('实际执行 RCON',[field('目标服务器','server_id','select',{options:state.settings.servers.map(x=>[x.id,x.name])}),field('控制台命令','command','text',{required:true,wide:true,hint:'这会实际执行服务器命令，请核对内容。'})],{server_id:state.settings.servers[0]?.id||'',command:''},async v=>output('RCON 返回',await action('rcon',v)),'执行命令'))]);
  table(p,[['动作','action'],['服务器','server_id'],['状态','status'],['时间',x=>nowText(x.created_at)]],state.runs||[]);
  const m=panel('旧数据迁移','仅在明确选择文件后读取；先预览，再确认导入。不会自动寻找生产数据库。',[button('预览迁移',()=>edit('预览旧点数数据库',[field('旧 SQLite 文件绝对路径','path','text',{required:true,wide:true,hint:'先停用旧插件并制作一致性备份，再选择备份文件。旧名称绑定需要重新验证 UUID。'})],{path:''},async v=>{const result=await action('import_preview',v);migrationPreview={path:v.path,digest:result.digest};output('迁移预览',result);},'只读预览')),button('导入备份',()=>edit('导入旧点数数据',[field('已预览的 SQLite 备份路径','path','text',{required:true,wide:true}),field('旧 session → 新 scope 映射','scope_map','json',{object:true,hint:'未填写的旧作用域保留原值；请按迁移说明填写。'}),field('我已预览并备份当前数据','confirmed','checkbox')],{path:migrationPreview?.path||'',confirmed:false,scope_map:{}},async v=>{if(!v.confirmed)throw Error('请先预览并备份后勾选确认');if(!migrationPreview?.digest||migrationPreview.path!==v.path)throw Error('请先预览同一个备份文件');output('迁移结果',await action('import_apply',{...v,digest:migrationPreview.digest}));migrationPreview=null;await refresh();},'确认导入'),'danger small')]);m.append(element('div','迁移保留余额、历史与已用码；不自动沿用旧命令、旧插件联动或旧玩法。重复批次应被拒绝或安全跳过。','note'));
  const mediaPanel=panel('媒体服务状态','可点击资源链接由独立媒体服务提供。');table(mediaPanel,[['项目','name'],['状态','value']],Object.entries(state.media||{}).map(([name,value])=>({name,value})));
  const a=panel('管理操作记录','不记录 Token、兑换码或奖励正文。');table(a,[['操作者','actor'],['操作','action'],['说明','detail'],['时间',x=>nowText(x.created_at)]],state.audit||[]);
}
pages.forEach(([id,label,number])=>{const b=button('',()=>{current=id;render();if(id==='monitor')return loadMetrics();},'');b.dataset.page=id;b.append(element('span',number),document.createTextNode(label));$('#nav').append(b);});
$('#refresh').onclick=()=>refresh().catch(showError);$('#dialog-close').onclick=$('#cancel').onclick=()=>$('#editor').close();$('#output-close').onclick=()=>$('#output').close();
try{if(!bridge)throw new Error('请从 AstrBot 插件详情页打开此管理界面。');await bridge.ready();await refresh();}catch(error){showError(error);$('#connection').textContent='未连接';$('#content').replaceChildren(element('div','无法读取插件状态，请检查插件是否已启用。','empty'));}
