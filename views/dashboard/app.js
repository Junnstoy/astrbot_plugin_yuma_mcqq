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
const messageTypes = [['text','文字'],['face','QQ 表情'],['image','图片'],['link','链接 / 分享卡片'],['record','语音'],['video','视频'],['file','文件'],['forward','合并消息'],['reply','引用消息'],['at','@ 提及']];
const gameEvents = [['PlayerChatEvent','玩家聊天'],['PlayerJoinEvent','玩家加入'],['PlayerQuitEvent','玩家离开'],['PlayerDeathEvent','玩家死亡'],['PlayerAchievementEvent','玩家成就']];
const pointsName = () => state?.settings?.points_name || '点数';
const currencyLabel = text => String(text).replaceAll('点数', pointsName());
const serverLabel = server => server.display_name || server.name || server.id;
const migrationStatus = status => ({ready:'待导入',duplicate:'重复合并',existing:'已存在',blocked:'需要处理',imported:'已处理',ignored:'未选择',excluded:'未选择'}[status]||status);
const mcColors={black:'#000000',dark_blue:'#0000aa',dark_green:'#00aa00',dark_aqua:'#00aaaa',dark_red:'#aa0000',dark_purple:'#aa00aa',gold:'#ffaa00',gray:'#aaaaaa',dark_gray:'#555555',blue:'#5555ff',green:'#55ff55',aqua:'#55ffff',red:'#ff5555',light_purple:'#ff55ff',yellow:'#ffff55',white:'#ffffff'};
const tagColors=[['link','链接','#5555ff'],['image','图片','#5555ff'],['record','语音','#5555ff'],['video','视频','#5555ff'],['file','文件','#5555ff'],['forward','合并消息','#5555ff'],['reply','引用','#aaaaaa'],['face','表情','#55ff55'],['at','@提及','#ffff55']];

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
  const labels = element('div'); labels.append(element('h2', currencyLabel(title)));
  if (description) labels.append(element('p', currencyLabel(description)));
  head.append(labels);
  const actions = element('div', null, 'actions'); tools.forEach(x => actions.append(x)); head.append(actions);
  section.append(head); $('#content').append(section); return section;
}
function table(parent, columns, rows, actions) {
  if (!rows?.length) { parent.append(element('div', '还没有记录', 'empty')); return; }
  const wrap = element('div', null, 'table-wrap'); const t = element('table');
  const tr = element('tr'); columns.forEach(c => tr.append(element('th', currencyLabel(c[0])))); if (actions) tr.append(element('th', '操作'));
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
  let advanced = null;
  specs.forEach(spec => {
    let parent = $('#fields');
    if (spec.advanced) {
      if (!advanced) {
        const detail = element('details', null, 'advanced wide');
        detail.append(element('summary', '高级解析与格式（已有默认值，通常无需修改）'));
        advanced = element('div', null, 'form-grid'); detail.append(advanced); parent.append(detail);
      }
      parent = advanced;
    }
    if (spec.type === 'section') {
      const section = element('div', null, 'form-section wide'); section.append(element('h3', spec.label));
      if(spec.hint)section.append(element('p', spec.hint, 'muted'));parent.append(section);return;
    }
    if (spec.type === 'checks') {
      const set = element('fieldset', null, 'checkbox-set wide'); set.append(element('legend',spec.label));
      if(spec.hint)set.append(element('p',spec.hint,'muted'));
      const grid=element('div',null,'checkbox-grid');const selected=new Set(initial[spec.key]??spec.default??[]);
      const items=(spec.options||[]).map(([key,text])=>{
        const label=element('label',null,'toggle-item');const input=element('input');input.type='checkbox';input.value=key;input.checked=selected.has(key);input.setAttribute('aria-label',text);
        label.append(input,element('span',text));grid.append(label);return input;
      });set.append(grid);parent.append(set);controls.push([spec,items,set]);return;
    }
    const label = element('label', null, `field ${spec.wide || spec.type === 'json' || spec.type === 'textarea' ? 'wide' : ''} ${spec.type === 'checkbox' ? 'check' : ''}`);
    label.append(element('span', spec.label));
    const control = element(spec.type === 'select' ? 'select' : ['json', 'textarea', 'lines'].includes(spec.type) ? 'textarea' : 'input');
    control.setAttribute('aria-label', spec.label);
    if (control.tagName === 'INPUT') control.type = spec.type;
    if (spec.type === 'select') (spec.options || []).forEach(v => { const option = element('option', Array.isArray(v) ? v[1] : v); option.value = Array.isArray(v) ? v[0] : v; control.append(option); });
    const value = initial[spec.key] ?? spec.default ?? '';
    if (spec.type === 'checkbox') control.checked = Boolean(value);
    else control.value = spec.type === 'json' ? JSON.stringify(value || (spec.object ? {} : []), null, 2) : spec.type === 'lines' ? (Array.isArray(value)?value.join('\n'):String(value)) : String(value);
    if (spec.readonly) control.readOnly = true;
    if (spec.required) control.required = true;
    if (spec.min !== undefined) control.min = spec.min;
    if (spec.max !== undefined) control.max = spec.max;
    if (spec.placeholder) control.placeholder = spec.placeholder;
    label.append(control); if (spec.hint) label.append(element('small', spec.hint));
    parent.append(label); controls.push([spec, control, label]);
  });
  const read = (spec, control) => spec.type==='checks' ? control.filter(x=>x.checked).map(x=>x.value) : spec.type==='checkbox' ? control.checked : spec.type==='number' ? Number(control.value) : spec.type==='json' ? JSON.parse(control.value||(spec.object?'{}':'[]')) : spec.type==='lines' ? control.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean) : control.value;
  const visibility=()=>{
    const values={};controls.forEach(([spec,control])=>{if(!['checks','json'].includes(spec.type))values[spec.key]=read(spec,control);});
    controls.forEach(([spec,control,label])=>{if(spec.showWhen){label.hidden=!spec.showWhen(values);control.required=!!spec.required&&!label.hidden;}});
  };
  controls.forEach(([,control])=>{if(!Array.isArray(control))control.addEventListener('change',visibility);});visibility();
  $('#edit-form').onsubmit = async event => {
    event.preventDefault(); $('#dialog-error').textContent = ''; $('#save').disabled = true;
    try {
      const value = structuredClone(initial);
      controls.forEach(([spec, control]) => {
        value[spec.key] = read(spec,control);
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
  const select = element('select', null, 'scope-select'); state.settings.servers.forEach(s => { const o = element('option', serverLabel(s)); o.value = s.id; select.append(o); }); select.value = selectedServer;
  select.onchange = () => { selectedServer = select.value; fn?.(); }; parent.append(select);
}
function filtered(key) { return (state.domain?.[key] || []).filter(x => !selectedScope || x.scope === selectedScope); }
function render() {
  const page = pages.find(x => x[0] === current); $('#title').textContent = currencyLabel(page[1]); $('#subtitle').textContent = currencyLabel(page[3]);
  $('#nav').querySelectorAll('button').forEach(n => {n.classList.toggle('active', n.dataset.page === current);n.lastChild.textContent=currencyLabel(pages.find(p=>p[0]===n.dataset.page)[1]);});
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
  f.append(element('div', '首次使用：添加服务器 → 添加 UMO 路由 → 绑定玩家名 → 使用“签到”。仅开启验证码时需要游戏验证。商店兑换记录与点数流水独立保存。', 'note'));
}
function globalSettings() {
  const config=state.settings,media=config.media,monitor=config.monitor;
  const value={...config,monitor_enabled:monitor.enabled!==false,interval_seconds:monitor.interval_seconds||60,retention_days:monitor.retention_days||7,
    media_enabled:!!media.enabled,media_host:media.host||'127.0.0.1',media_port:media.port||8766,public_base:media.public_base||'',ttl_seconds:media.ttl_seconds||86400,
    trusted_local_roots:media.trusted_local_roots||[],allowed_hosts:media.allowed_hosts||[]};
  edit('全局设置',[
    field('插件与指令','section_basic','section'),field('插件启用','enabled','checkbox'),
    field('插件唤醒前缀（可选）','command_prefix','text',{hint:'留空沿用 AstrBot 的唤醒方式。填 mc 后可直接发送 mc签到 或 mc 签到；只影响本插件。',default:''}),
    field('点数显示名称','points_name','text',{default:'点数',required:true,hint:'例如金币、星币。回复模板使用 {points_name}，指令名称仍在管理行为中修改。'}),
    field('绑定时要求游戏验证码','binding_verification_enabled','checkbox',{hint:'默认关闭：直接按玩家名绑定。开启后，用户必须到游戏发送验证码。已有绑定和冷却保留。'}),
    field('换绑 / 解绑冷却（秒）','binding_cooldown_seconds','number',{min:0}),field('默认时区','timezone'),
    field('性能监控','section_monitor','section',{hint:'后台采样与聊天查询独立；tps / mspt / ping 返回实际 RCON 文本，性能图返回历史图片。'}),
    field('自动采样','monitor_enabled','checkbox'),field('采样间隔（秒）','interval_seconds','number',{min:10,max:86400}),field('历史保留（天）','retention_days','number',{min:1,max:365}),
    field('媒体链接服务','section_media','section',{hint:'QQ 自带的公开媒体链接可直接转发。开启缓存服务可提供限时资源链接及合并消息完整页面。'}),
    field('启用媒体缓存服务','media_enabled','checkbox'),field('玩家可访问的公开地址','public_base','text',{wide:true,placeholder:'https://media.example.com',showWhen:v=>v.media_enabled}),
    field('媒体监听地址','media_host','text',{showWhen:v=>v.media_enabled}),field('媒体监听端口','media_port','number',{showWhen:v=>v.media_enabled,min:1,max:65535}),
    field('链接有效期（秒）','ttl_seconds','number',{showWhen:v=>v.media_enabled,min:60}),
    field('允许读取的媒体目录（每行一个）','trusted_local_roots','lines',{wide:true,advanced:true,hint:'仅填写专用媒体缓存目录，不要填写整个机器人数据目录。'}),
    field('允许下载的域名（每行一个，可留空）','allowed_hosts','lines',{wide:true,advanced:true,hint:'留空允许公开网络资源；始终禁止下载私网和本地服务地址。'})
  ],value,async v=>saveSettings({enabled:v.enabled,command_prefix:v.command_prefix,points_name:v.points_name,binding_verification_enabled:v.binding_verification_enabled,
    binding_cooldown_seconds:v.binding_cooldown_seconds,timezone:v.timezone,
    monitor:{...monitor,enabled:v.monitor_enabled,interval_seconds:v.interval_seconds,retention_days:v.retention_days},
    media:{...media,enabled:v.media_enabled,host:v.media_host,port:v.media_port,public_base:v.public_base,ttl_seconds:v.ttl_seconds,trusted_local_roots:v.trusted_local_roots,allowed_hosts:v.allowed_hosts}}));
}
function serverEditor(existing) {
  const value={id:randomId().slice(0,8),name:'',display_name:'',connection_mode:'forward',url:'ws://127.0.0.1:8080',token:'',enabled:true,timeout:10,
    reverse_host:'0.0.0.0',reverse_port:8081,reverse_path:'/queqiao',game_host:'127.0.0.1',game_port:25565,tps_command:'tps',mspt_command:'mspt',ping_command:'ping',component_version:'legacy',...existing};
  edit(existing?'编辑服务器':'添加服务器',[
    field('服务器身份与显示','section_server','section'),field('服务器 ID','id','text',{readonly:!!existing,required:true}),
    field('QueQiao server_name','name','text',{required:true,hint:'连接鉴权标识，必须与 QueQiao 配置一致。'}),
    field('服务器显示名称','display_name','text',{hint:'MC → QQ 的 {server} 使用此名称；留空才使用 server_name。例如生存服、创造服。'}),
    field('启用连接','enabled','checkbox'),
    field('连接方式','connection_mode','select',{options:[['forward','正向：插件连接 QueQiao'],['reverse','反向：QueQiao 连接插件']],wide:true}),
    field('QueQiao WebSocket 地址','url','text',{required:true,wide:true,showWhen:v=>v.connection_mode==='forward',hint:'填写服务器上 QueQiao 的监听地址，例如 ws://游戏服务器IP:8080。'}),
    field('反向监听地址','reverse_host','text',{showWhen:v=>v.connection_mode==='reverse',required:true}),
    field('反向监听端口','reverse_port','number',{showWhen:v=>v.connection_mode==='reverse',min:1,max:65535,required:true}),
    field('反向 WebSocket 路径','reverse_path','text',{showWhen:v=>v.connection_mode==='reverse',required:true,hint:'QueQiao 客户端连接 ws://机器人可达地址:端口/queqiao；不同服可共享端口，用 server_name 区分。',wide:true}),
    field('Access Token','token','password',{hint:'反向连接必填。QueQiao 与此处使用相同 Token；已有掩码保持不变即可保留。'}),
    field('请求超时（秒）','timeout','number',{min:1,max:120}),
    field('RCON 查询与采样','section_metrics','section',{hint:'以下指令既用于聊天查询的原文回复，也用于后台性能采样。服务器状态只检查连接在线 / 离线。'}),
    field('TPS 指令','tps_command'),field('MSPT 指令','mspt_command'),field('玩家延迟指令','ping_command'),
    field('游戏主机（RTT 探测）','game_host'),field('游戏端口','game_port','number',{min:1,max:65535}),
    field('聊天组件格式','component_version','select',{options:[['legacy','传统 clickEvent / hoverEvent'],['modern','新版 click_event / hover_event']],advanced:true,hint:'按 Paper / QueQiao 的组件解析版本选择。链接无法点击或引用无法悬浮时核对此项。'})
  ],value,async v=>{const list=state.settings.servers.filter(x=>x.id!==v.id);list.push(v);await saveSettings({servers:list});});
}
function routeEditor(existing) {
  const base={umo:'',scope:'',display_name:'',server_id:state.settings.servers[0]?.id||'',enabled:true,qq_to_mc:true,mc_to_qq:true,admins:[],timezone:'Asia/Shanghai',points_min:10,points_max:10,
    signin_command:'signin click {player}',signin_prefixes:['[手记人偶]'],signin_rules:[],templates:{},events:gameEvents.map(x=>x[0]),types:messageTypes.map(x=>x[0]),prefix:'[{group}] {sender}{reply}：',max_forward_nodes:30,...existing};
  const value={...base,signin_success:base.templates.signin??'',signin_already:base.templates.signin_already??'',mc_to_qq_template:base.templates.mc_to_qq??'',reply_hover_template:base.templates.reply_hover??''};
  for(const [key,,fallback] of tagColors){const color=base.message_colors?.[key];value['color_'+key]=mcColors[color]||color||fallback;}
  edit(existing?'编辑群服路由':'添加群服路由',[
    field('群聊与服务器','section_route','section'),field('原始 UMO','umo','text',{required:true,wide:true,readonly:!!existing,hint:'在目标群使用 AstrBot /sid 获取完整会话标识。'}),
    field('QQ群显示名称','display_name','text',{hint:'QQ → MC 的 {group} 使用此名称，留空显示 QQ群。'}),
    field('目标服务器','server_id','select',{options:state.settings.servers.map(x=>[x.id,serverLabel(x)])}),
    field('启用此群路由','enabled','checkbox'),field('QQ → MC 转发','qq_to_mc','checkbox'),field('MC → QQ 转发','mc_to_qq','checkbox'),
    field('QQ → MC 消息类型','types','checks',{options:messageTypes,hint:'勾选的类型会转发；图片、视频等显示可点击标签，引用显示悬浮内容。'}),
    field('MC → QQ 游戏事件','events','checks',{options:gameEvents,hint:'分别开启玩家聊天、加入、离开、死亡和成就。QueQiao 也必须上报对应事件。'}),
    field('QQ → MC 显示前缀','prefix','text',{wide:true,hint:'变量：{group} 群显示名、{sender} 发言人、{reply} 带悬浮内容的引用标签、{user_id} QQ号。例：&e[{group}]&e{sender}{reply}&8:。无引用时 {reply} 为空；旧模板未填写时仍在正文显示引用。支持 &a、§a、&#RRGGBB 颜色。'}),
    field('消息标签颜色','section_colors','section',{hint:'点击色块选择链接、图片和引用等标签的颜色；点击链接和悬浮内容仍会保留。'}),
    ...tagColors.map(([key,label])=>field(label+'标签颜色','color_'+key,'color')),
    field('引用悬浮内容（留空用默认）','reply_hover_template','textarea',{wide:true,placeholder:'{sender}\n{message}\nDate: {time}',hint:'默认三行：原作者、原消息内容、原消息时间。{sender} 是被引用消息作者，{message} 为原文，{time} 按本路由时区显示 HH:MM:SS；没有原时间则显示未知。'}),
    field('MC → QQ 消息格式','mc_to_qq_template','textarea',{wide:true,placeholder:'[{server}] {message}',hint:'留空使用默认。{server} 是服务器显示名称，{player} 是玩家名，{message} 是已含玩家名的聊天或事件内容。'}),
    field('签到与账户','section_signin','section'),field('点数作用域','scope','text',{hint:'留空使用 UMO。填写相同值可共享余额；共享时区和签到奖励必须一致。'}),
    field('签到时区','timezone'),field('签到奖励下限','points_min','number',{min:0}),field('签到奖励上限','points_max','number',{min:0}),
    field('游戏签到命令','signin_command','text',{wide:true,hint:'先执行此命令，确认游戏已签到，再结算本群奖励。{player} 会替换成绑定的玩家名。'}),
    field('群管理员 QQ（每行一个）','admins','lines',{wide:true}),
    field('群签到成功时的完整回复（留空用默认）','signin_success','textarea',{wide:true,placeholder:'玩家: {player}\n{group_result}\n{game_result}',hint:'{group_result} 是群签到结果行，{game_result} 是本次游戏结果行，分别判断。还可用 {days} 连续天数、{points_delta} 奖励、{points_name} 货币名称、{balance} 余额、{rcon} 完整回包。'}),
    field('群内今日已签到时的完整回复（留空用默认）','signin_already','textarea',{wide:true,placeholder:'玩家: {player}\n{group_result}\n{game_result}',hint:'群内重复不等于游戏也重复；保留 {game_result} 才能显示本次游戏是否成功。'}),
    field('识别时忽略的回包前缀（每行一个）','signin_prefixes','lines',{wide:true,advanced:true,hint:'只影响规则识别。例如 [手记人偶] 会从匹配文本开头移除；QQ群中 {rcon} 仍保留它。颜色代码自动清除，无需填入。'}),
    field('自定义签到结果规则','signin_rules','json',{advanced:true,hint:'默认 [] 直接识别已提供的 LiteSignIn 回包。仅当服务端返回文字变化时修改。每项包含 status 和 pattern；SIGNED / ALREADY_SIGNED 必须用 (?P<player>...) 捕获玩家名。'}),
    field('合并消息展开上限','max_forward_nodes','number',{advanced:true,min:1,max:100})
  ],value,async v=>{v.scope||=v.umo;const templates={...v.templates};for(const [field,key] of [['signin_success','signin'],['signin_already','signin_already'],['mc_to_qq_template','mc_to_qq'],['reply_hover_template','reply_hover']]){if(v[field].trim())templates[key]=v[field];else delete templates[key];delete v[field];}v.templates=templates;v.message_colors={...v.message_colors};for(const [key] of tagColors){v.message_colors[key]=v['color_'+key];delete v['color_'+key];}const list=state.settings.routes.filter(x=>x.umo!==v.umo);list.push(v);await saveSettings({routes:list});});
}
function routes() {
  const p=panel('服务器连接','鉴权名称与对外显示名称分开设置。支持正向连接及反向监听。',[button('全局设置',globalSettings),button('添加服务器',()=>serverEditor(),'')]);
  const connections=new Map((state.connections||[]).map(x=>[x.id||x.server_id,x]));
  table(p,[['显示名称',serverLabel],['鉴权名称','name'],['方式',x=>x.connection_mode==='reverse'?'反向':'正向'],['连接地址',x=>x.connection_mode==='reverse'?`${x.reverse_host}:${x.reverse_port}${x.reverse_path}`:x.url],['状态',x=>!x.enabled?'已关闭':connections.get(x.id)?.connected?'在线':x.connection_mode==='reverse'?'等待连接':'离线']],state.settings.servers,s=>[button('编辑',()=>serverEditor(s)),button('删除',async()=>{if(confirm('删除服务器前请移除引用它的群路由和指令。继续？'))await saveSettings({servers:state.settings.servers.filter(x=>x.id!==s.id)});},'danger small')]);
  const q=panel('消息转发与群服路由','每个群分别设置转发方向、消息类型和游戏事件开关。',[button('添加路由',()=>routeEditor(),'')]);
  table(q,[['群显示名称',x=>x.display_name||'QQ群'],['UMO','umo'],['目标服务器',x=>serverLabel(state.settings.servers.find(s=>s.id===x.server_id)||{id:x.server_id})],['方向',x=>[x.qq_to_mc?'QQ → MC':'',x.mc_to_qq?'MC → QQ':''].filter(Boolean).join(' / ')||'已关闭'],['游戏事件',x=>gameEvents.filter(([key])=>x.events?.includes(key)).map(([,name])=>name).join('、')]],state.settings.routes,r=>[button('编辑与开关',()=>routeEditor(r)),button('删除',async()=>{if(confirm('仅移除路由，历史数据保留。继续？'))await saveSettings({routes:state.settings.routes.filter(x=>x.umo!==r.umo)});},'danger small')]);
}
function commandEditor(existing) {
  const value = existing || { id:randomId().slice(0,8),name:'',aliases:[],description:'',enabled:true,require_binding:true,admin_only:true,cooldown_seconds:3,server_id:'',rcon:'',parser:'text',cleanup_prefixes:[],parameters:[],rules:[],templates:{SUCCESS:'{cleaned}',UNKNOWN:'服务器结果未识别，请管理员检查。'} };
  edit(existing?'编辑命令动作':'新增命令动作',[field('稳定动作 ID','id','text',{readonly:!!existing,required:true}),field('初始触发名','name','text',{required:true,hint:'已注册后的实际触发名以 AstrBot 管理行为为准。'}),field('说明','description','text',{wide:true}),field('初始别名','aliases','json'),field('目标服务器','server_id','select',{options:[['','跟随群服路由'],...state.settings.servers.map(x=>[x.id,serverLabel(x)])]}),field('调用冷却（秒）','cooldown_seconds','number'),field('动作启用','enabled','checkbox'),field('需要绑定','require_binding','checkbox'),field('仅管理员','admin_only','checkbox'),field('RCON 命令模板','rcon','text',{wide:true,required:true,hint:'可用 {player}、{user_id} 以及下方定义的参数。一条动作执行一条控制台命令。'}),field('参数定义','parameters','json',{hint:'例：[{"name":"amount","type":"integer","min":1,"max":64}]'}),field('解析方式','parser','select',{options:[['rules','完整正则匹配'],['text','直接返回清洗后的 RCON 文本（默认）'],['litesignin','LiteSignIn 签到回包']]}),field('识别时忽略的前缀（每行一个）','cleanup_prefixes','lines',{advanced:true,wide:true,hint:'仅移除明确填写的回包开头文字。颜色代码会自动清除；{rcon} 保留完整文本。'}),field('结果规则','rules','json',{advanced:true,hint:'例：[{"status":"SUCCESS","pattern":"成功给玩家 (?P<player>\\w+) ..."}]'}),field('按状态选择回复模板','templates','json',{object:true,advanced:true,hint:'默认 SUCCESS 使用 {cleaned}；可用 {rcon} 表示仅去颜色和日志包装的完整回包，{points_name} 为货币名称。纯文本查询无需填写结果规则。'})],value,async v=>{const list=state.settings.commands.filter(x=>x.id!==v.id);list.push(v);await saveSettings({commands:list});});
}
function commands() {
  const p=panel('自定义命令','命令使用独立处理器；稳定 ID 不随触发词改名。',[button('新增命令',()=>commandEditor(),'')]);
  table(p,[['动作 ID','id'],['初始触发名','name'],['RCON','rcon'],['解析','parser'],['权限',x=>x.admin_only?'管理员':'成员']],state.settings.commands,c=>[button('编辑',()=>commandEditor(c)),button('模拟回复（不执行）',()=>preview(c)),button('删除',async()=>{if(confirm('删除此命令动作及其注册入口？'))await saveSettings({commands:state.settings.commands.filter(x=>x.id!==c.id)});},'danger small')]);
  const q=panel('管理行为生效值','在 AstrBot → 插件 → 管理行为中修改名称、别名和入口启停。');
  const effective=state.effective_commands || []; table(q,[['动作',x=>x.action_id||x.handler_name||x.id],['实际触发名',x=>x.effective_command||x.name||x.command],['别名',x=>x.effective_aliases||x.aliases||[]],['入口启用',x=>x.enabled===false?'否':'是']],Array.isArray(effective)?effective:Object.values(effective));
}
function signinPreview() {
  edit('群签到与游戏签到 · 模拟回复',[
    field('使用哪份路由模板','route_umo','select',{options:[['','内置默认模板'],...state.settings.routes.map(r=>[r.umo,r.display_name||r.umo])],wide:true}),
    field('期望玩家名','player','text',{required:true}),field('模拟：群内今日已经签到','group_signed','checkbox',{hint:'只影响本次预览，不查询真实签到记录。游戏结果由下面的回包识别。'}),
    field('示例本次奖励','points_delta','number',{min:0}),field('示例当前余额','balance','number',{min:0}),
    field('粘贴游戏签到原始回包','raw','textarea',{wide:true,required:true,hint:'成功和重复游戏签到回包，分别搭配群已签到勾选状态，即可验证四种结果；不执行 RCON、不增加点数。'})
  ],{route_umo:state.settings.routes[0]?.umo||'',player:'rincho',group_signed:false,points_delta:1,balance:1,
    raw:'Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e1 §a天。'},async v=>{
      const r=await action('preview_signin',v);
      output('签到模拟结果 · 未执行命令',`游戏识别：${r.status}\n群结果：${r.data?.group_status||'未结算'}\n\n提取字段\n${JSON.stringify(r.fields||{},null,2)}\n\n回复预览\n${r.preview||'（空）'}`);
  },'只模拟回复');
}
function preview(command) {
  const isSignin=command.parser==='litesignin';
  const raw=isSignin?'Rcon:§f[§3手记人偶§f] §a成功给玩家 §6rincho §a签到， 当前该玩家已连续签到 §e1 §a天。':'';
  edit(`模拟回复：${command.name||command.id||'当前指令'}`,[
    field('此测试只模拟文本处理，不执行游戏指令','section_preview','section',{hint:'粘贴这条指令实际收到的服务器回复，可查看清洗结果、匹配状态和最终显示文字。预览中的余额与奖励仅为示例。'}),
    field('对应游戏命令','command_text','text',{readonly:true,wide:true}),field('期望玩家名（可选）','player','text'),
    field('粘贴服务器原始回复','raw','textarea',{wide:true,required:true,hint:'可直接粘贴包含 § 颜色、Rcon: 前缀的原始内容。不要粘贴要执行的命令。'}),
    field('示例模板变量','values','json',{object:true,advanced:true,hint:'仅用于模拟显示，例如 points_delta、balance。不会读取或修改真实账户。'})
  ],{command_text:command.rcon||'',player:isSignin?'rincho':'',raw,values:{points_delta:10,balance:100,points_name:pointsName()}},async v=>{
    const r=await action('preview',{...v,command});
    const labels={SIGNED:'游戏签到成功',ALREADY_SIGNED:'游戏今日已签到',SUCCESS:'匹配成功 / 查询文本已收到',UNKNOWN:'未识别（请检查回包与规则）',FAILED:'匹配到失败状态'};
    output('模拟解析结果 · 未执行命令',`① 清洗后用于匹配的文本\n${r.cleaned||'（空）'}\n\n② 识别结果\n${labels[r.status]||r.status} [${r.status}]\n\n③ 提取到的字段\n${JSON.stringify(r.fields||{},null,2)}\n\n④ 回复预览\n${r.preview||'（空）'}`);
  },'只模拟回复');
}
function bindings() {
  const p=panel('账号绑定',state.settings.binding_verification_enabled?'验证码已开启：需到游戏验证。解绑保留冷却和历史。':'验证码已关闭：直接按玩家名绑定；未验证身份会明确标记。');
  table(p,[['QQ','qq_id'],['玩家','player'],['验证状态',x=>x.verified?'已验证':'未验证（玩家名）'],['真实 UUID',x=>x.mc_uuid||'—'],['服务器','server_id'],['变更时间',x=>nowText(x.updated)]],state.domain?.bindings,b=>[button('管理员解绑',async()=>{if(confirm(`解绑 ${b.qq_id} ↔ ${b.player}？本次管理员操作不受 CD 限制，普通用户后续操作仍遵守冷却。`)){const result=await action('binding_unbind',{user_id:b.qq_id});await refresh();output('管理员解绑结果',result.text||'解绑成功。');}},'danger small')]);
  const q=panel('点数账户','余额和冻结额分开显示。管理员调整也写入流水。',[button(currencyLabel('调整点数'),()=>edit(currencyLabel('管理员调整点数'),[field('QQ ID','user_id','text',{required:true}),field('操作','mode','select',{options:[['add','增加'],['sub','扣除'],['set','设置余额']]}),field('数量','amount','number',{min:0,required:true}),field('原因','reason','text',{required:true,wide:true})],{user_id:'',mode:'add',amount:0,reason:'',operation_id:randomId()},async v=>{output('调整结果',await action('points_adjust',{...v,scope:selectedScope}));await refresh();}),'')]);
  scopeSelect(q);table(q,[['QQ','qq_id'],['总点数',x=>x.total??x.balance],['冻结','frozen'],['可用',x=>(x.total??x.balance??0)-(x.frozen||0)]],filtered('balances'));
  const l=panel('最近流水','点数变更均保存来源与操作人。');table(l,[['QQ','qq_id'],['变动','delta'],['变更后','total_after'],['原因','reason'],['操作人','actor'],['时间',x=>nowText(x.created)]],filtered('ledger').slice(0,100));
  const s=panel('签到记录','游戏成功或今日已签后，才结算群签到。');table(s,[['QQ','qq_id'],['日期','day'],['点数','points'],['游戏状态','game_status']],filtered('signins').slice(0,100));
}
async function productEditor(existing) {
  let value={id:randomId().slice(0,8),name:'',price:10,kind:'text',reward:'',command:'',rules:[],enabled:true,stock:-1,limit_per_user:0};
  if(existing){const detail=await action('product_get',{scope:selectedScope,product_id:existing.id});value={...value,...(detail.product||detail)};}
  edit(existing?'编辑商品':'新增商品',[field('商品 ID','id','text',{readonly:!!existing,required:true}),field('名称','name','text',{required:true}),field(currencyLabel('价格（点数）'),'price','number',{min:0,required:true}),field('交付方式','kind','select',{options:[['text','私聊固定文本'],['code','私聊独立兑换码'],['rcon','游戏命令交付']]}),field('库存（-1 不限）','stock','number',{min:-1}),field('每人累计限购（0 不限）','limit_per_user','number',{min:0}),field('上架','enabled','checkbox'),field('私聊奖励文本','reward','textarea',{hint:'仅通过私聊发送，不出现在商品列表中。'}),field('游戏交付命令','command','text',{wide:true,hint:'RCON 商品使用。可用 {player}、{user_id}、{order_id} 等交付字段。'}),field('游戏交付结果规则','rules','json',{hint:'必须能明确确认成功；超时或无法识别进入待核对。'})],value,async v=>{await action('product_save',{scope:selectedScope,product:v});await refresh();});
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
  const note=element('div','采样按配置间隔在后台进行。QQ群的 tps / mspt / ping 返回实际 RCON 文本；“服务器状态”仅返回在线或离线；“性能图”获取历史图片。玩家 ping 与机器人探测链路分别记录，未知值不填 0。','note');$('#content').append(note);
}
function rememberMigration(parameters,result) {
  migrationPreview={parameters:structuredClone(parameters),digest:result.digest,plan_digest:result.plan_digest,result};
  $('#banner').hidden=true;
  render();
  const plan=result.binding_plan||{};
  output('迁移预览 · 只读核对',{digest:result.digest,already_imported:result.already_imported,counts:result.counts,
    balance_totals:result.balance_totals,warnings:result.warnings,binding_counts:plan.counts,
    can_apply:plan.can_apply,conflicts:plan.conflicts});
}
function migrationStart() {
  const previous=migrationPreview?.parameters;
  edit('预览旧点数数据库',[
    field('旧 SQLite 文件绝对路径','path','text',{required:true,wide:true,hint:'选择一致性备份；路径必须位于运行 AstrBot 的机器上。预览不会修改原数据库。'}),
    field('旧绑定的目标服务器','binding_server_id','select',{wide:true,options:[['','按映射与群路由推导'],...state.settings.servers.map(s=>[s.id,serverLabel(s)])],hint:'旧绑定通常只有QQ和玩家名，没有服务器。只有一台服时可直接选择；导入后显示为未验证的名称绑定。'}),
    field('旧 session → 新 scope 映射','scope_map','json',{object:true,advanced:true,hint:'影响余额、签到和商店；未填写的旧作用域保留原值。已经迁过的账本不会再次导入。'}),
    field('旧账号或平台 → 服务器 ID 映射','server_map','json',{object:true,advanced:true,hint:'多服务器时可按完整旧账号键、账号前缀或平台实例指定服务器，优先于上面的统一目标服。'})
  ],{path:previous?.path||'',binding_server_id:previous?.binding_server_id||(state.settings.servers.length===1?state.settings.servers[0].id:''),
    scope_map:previous?.scope_map||{},server_map:previous?.server_map||{}},async v=>{
      const parameters={...v,binding_choices:null};
      rememberMigration(parameters,await action('import_preview',parameters));
  },'只读预览');
}
function migrationChooseBindings() {
  if(!migrationPreview)throw Error('请先预览旧数据库');
  const previous=migrationPreview;
  const rows=previous.result.binding_plan?.rows||[];
  const chosen=previous.parameters.binding_choices??rows.map(r=>r.account_key);
  edit('选择要迁移的绑定',[
    field('绑定记录','binding_choices','checks',{options:rows.map(r=>[r.account_key,`${r.qq_id} → ${r.player} · ${r.server_id||'未指定服'} · ${r.account_key}${r.reason?' · '+r.reason:''}`]),hint:'冲突记录取消不需要的那一项，再核对。相同QQ、玩家和服务器的重复记录会自动合并。全部取消表示仅归档旧绑定，不建立可用绑定。'})
  ],{binding_choices:chosen},async v=>{
    if(migrationPreview!==previous)throw Error('迁移预览已更新，请重新选择');
    const parameters={...previous.parameters,binding_choices:v.binding_choices};
    rememberMigration(parameters,await action('import_preview',parameters));
  },'重新核对选择');
}
function migrationApply() {
  const previous=migrationPreview;
  if(!previous?.digest)throw Error('请先预览备份文件');
  if(!previous.result.binding_plan?.can_apply)throw Error('绑定计划存在冲突或缺少服务器，请先选择绑定并重新核对');
  const counts=previous.result.binding_plan.counts||{};
  edit('确认已核对的迁移计划',[
    field('已预览的 SQLite 备份路径','path','text',{readonly:true,wide:true}),
    field(`绑定：待导入 ${counts.ready||0}，已存在 ${counts.existing||0}，重复 ${counts.duplicates||0}`,'summary','section',{hint:previous.result.already_imported?'此批次账本已经迁过，本次仅处理尚未迁移的绑定。':'余额、历史和本次选定绑定将在一个事务中导入。'}),
    field('我已核对选择并备份当前数据','confirmed','checkbox')
  ],{path:previous.parameters.path,confirmed:false},async v=>{
    if(!v.confirmed)throw Error('请先核对计划并备份当前数据');
    if(migrationPreview!==previous)throw Error('迁移计划已更新，请重新确认');
    const result=await action('import_apply',{...previous.parameters,digest:previous.digest,plan_digest:previous.plan_digest,confirmed:true});
    migrationPreview=null;await refresh();output('迁移结果',result);
  },'确认导入');
}
function diagnostics(){
  const p=panel('回包与连接诊断','解析预览不会执行游戏命令。',[button('签到回包解析示例',signinPreview),button('执行 RCON',()=>edit('实际执行 RCON',[field('目标服务器','server_id','select',{options:state.settings.servers.map(x=>[x.id,serverLabel(x)])}),field('控制台命令','command','text',{required:true,wide:true,hint:'这会实际执行服务器命令，请核对内容。'})],{server_id:state.settings.servers[0]?.id||'',command:''},async v=>output('RCON 返回',await action('rcon',v)),'执行命令'))]);
  table(p,[['动作','action'],['服务器','server_id'],['状态','status'],['时间',x=>nowText(x.created_at)]],state.runs||[]);
  const m=panel('旧数据迁移','先预览绑定计划，处理冲突，再确认导入。已经迁过账本的批次可补迁绑定。',[button('预览迁移',migrationStart),button('选择绑定 / 重新核对',migrationChooseBindings),button('导入备份',migrationApply,'danger small')]);
  m.append(element('div','旧名称绑定可迁为未验证的名称身份，不伪造 UUID。不会重复迁入余额，也不会自动发送奖励或执行旧命令。','note'));
  if(migrationPreview?.result.binding_plan){
    const plan=migrationPreview.result.binding_plan;
    m.append(element('div',`当前计划：${plan.can_apply?'可导入':'需要处理冲突或服务器映射'}；待导入 ${plan.counts?.ready||0} 条。`,'note'));
    table(m,[['旧账号键','account_key'],['QQ','qq_id'],['MC名','player'],['服务器','server_id'],['状态',r=>migrationStatus(r.status)],['说明','reason']],plan.rows||[]);
  }
  const mediaPanel=panel('媒体服务状态','可点击资源链接由独立媒体服务提供。');table(mediaPanel,[['项目','name'],['状态','value']],Object.entries(state.media||{}).map(([name,value])=>({name,value})));
  const a=panel('管理操作记录','不记录 Token、兑换码或奖励正文。');table(a,[['操作者','actor'],['操作','action'],['说明','detail'],['时间',x=>nowText(x.created_at)]],state.audit||[]);
}
pages.forEach(([id,label,number])=>{const b=button('',()=>{current=id;render();if(id==='monitor')return loadMetrics();},'');b.dataset.page=id;b.append(element('span',number),document.createTextNode(label));$('#nav').append(b);});
$('#refresh').onclick=()=>refresh().catch(showError);$('#dialog-close').onclick=$('#cancel').onclick=()=>$('#editor').close();$('#output-close').onclick=()=>$('#output').close();
try{if(!bridge)throw new Error('请从 AstrBot 插件详情页打开此管理界面。');await bridge.ready();await refresh();}catch(error){showError(error);$('#connection').textContent='未连接';$('#content').replaceChildren(element('div','无法读取插件状态，请检查插件是否已启用。','empty'));}
