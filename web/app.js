/* WorkBuddy Token 面板前端
   数据流：SSE 只推"数据版本号变了"（60 字节），真正的数据走一次 /api/bundle；
   每个渲染函数入口做数据指纹比对，没变就整块跳过 —— 这是"整页实时"不卡不闪的关键。 */

const $ = (id) => document.getElementById(id);
const v = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ?static=1：静态快照模式，关动画、不连 SSE —— 用于离线查看与截图核对
const STATIC = new URLSearchParams(location.search).has('static');
const ANIM = !STATIC;
// 手机断点：与 style.css 的 720px 保持一致
const NARROW = () => window.innerWidth <= 720;
const heatWeeks = () => (NARROW() ? 26 : 52);

const state = {
  summary: null, today: null, session: null,
  series: [], heat: [], by: 'model', days: 30,
  range: 'today', p: null,   // p：按区间聚合的 token + 积分
  v: 0,                      // 当前页面数据对应的版本号
  heatScale: null,           // 热力图色阶（带迟滞，避免整图闪烁）
  acct: null,                // null=全部账号 / '__none__' / user_id
  accounts: null,            // bundle 里的账号段（当前账号 + 列表）
  lastAcct: undefined,       // 切号提示用；undefined 表示还没初始化过
};

let BOOT = null;
try {
  const node = $('boot-data');
  if (node && node.textContent.trim() && node.textContent.trim() !== '__BOOT__') {
    BOOT = JSON.parse(node.textContent);
  }
} catch (e) { console.warn('首屏数据解析失败', e); }

let charts = {};
let pollTimer = null, fails = 0, es = null;
let pendingV = 0;            // 隐藏期间收到的版本：只记不拉，切回来再补
let hiddenAt = 0, bgTimer = 0, esClosed = false;

/* ---------------- 数字格式 ---------------- */
function shortParts(n) {
  n = Math.max(0, Math.round(n || 0));
  if (n >= 1e8) return [(n / 1e8).toFixed(2), '亿'];
  if (n >= 1e4) return [(n / 1e4).toFixed(1), '万'];
  return [n.toLocaleString('zh-CN'), ''];
}
function fmtShort(n) { const p = shortParts(n); return p[0] + p[1]; }
/* 坐标轴刻度专用：去掉 ".0" 尾巴，让 0 / 5万 / 10万 排在一起时位数一致，
   原来 5.0万、10.0万 混着 0 和 5万，右对齐后小数点对不齐，看着很脏 */
function fmtAxis(n) {
  const [num, unit] = shortParts(n);
  if (!unit) return num;
  return num.replace(/\.0+$/, '').replace(/(\.\d)0$/, '$1') + unit;
}
function fmtFull(n) { return Math.round(n || 0).toLocaleString('zh-CN'); }
function fmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
  return Math.floor(s / 3600) + ' 小时 ' + Math.floor((s % 3600) / 60) + ' 分';
}
/* KPI 卡片空间有限，用紧凑写法，避免被省略号吃掉 */
function fmtDurShort(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return s + ' 秒';
  if (s < 3600) return Math.floor(s / 60) + ' 分';
  return Math.floor(s / 3600) + ' 时 ' + Math.floor((s % 3600) / 60) + ' 分';
}
/* 把秒数说成人话：多久之前 */
function agoText(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + ' 分钟前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
  return Math.floor(s / 86400) + ' 天前';
}

function hexA(hex, a) {
  hex = (hex || '#2563eb').replace('#', '');
  if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
  const n = parseInt(hex, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

/* ---------------- 统一的图形语言 ----------------
   三张柱状图原先各写各的：小时图是半透明纯色、项目图是 .7 透明、趋势图是
   渐变，圆角 3 和 3 混着用，粗细 56%/20/34 也不一致，摆在一屏里像三个
   不同的产品。这里收成一套：同一套渐变、同一档圆角、同一套强调色。 */
const BAR_R = 4;                      // 柱顶圆角，竖直柱用
/* 注意：这里收的是 hex，透明度在函数内部转。
   之前写成 barFillH(hexA(accent, .78))，等于对已经变成 rgba() 的字符串
   再跑一次 hexA，parseInt 拿到 NaN，渐变终点是 rgba(NaN,...)，ECharts 直接
   当成无效色渲染成脏灰 —— 项目条右端发黑就是这么来的。 */
function barFillV(hex, a0 = 1, a1 = .55) {   // 竖直柱：上浓下淡
  return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
    { offset: 0, color: hexA(hex, a0) }, { offset: 1, color: hexA(hex, a1) },
  ]);
}
function barFillH(hex, a0 = 1, a1 = .55) {   // 水平柱：左浓右淡
  return new echarts.graphic.LinearGradient(0, 0, 1, 0, [
    { offset: 0, color: hexA(hex, a0) }, { offset: 1, color: hexA(hex, a1) },
  ]);
}
/* 常规柱用弱化色，当前/第一名用主色 —— 强弱只体现在透明度上，不换色相 */
const barDim = (hex) => barFillV(hex, .5, .3);
const barHot = (hex) => barFillV(hex, 1, .62);

/* ---------------- 数据指纹 ----------------
   放在"数据 → 视图"的边界，而不是 setOption 里：
   这样既能挡住"推来了但内容一样"，也能挡住"用户点按钮强制重画"。 */
const seen = new Map();
function dirty(key, data) {
  const s = JSON.stringify(data);
  if (seen.get(key) === s) return false;
  seen.set(key, s);
  return true;
}
function resetDirty() { seen.clear(); }

/* 文本/HTML 写入前先比一次，避免每轮重建 DOM 导致选中丢失、tooltip 关闭重开 */
function setText(el, text) {
  if (!el) return;
  text = String(text);
  if (el._t === text) return;
  el._t = text;
  el.textContent = text;
}
function setHTML(el, html) {
  if (!el) return;
  if (el._h === html) return;
  el._h = html;
  el.innerHTML = html;
}

/* ---------------- 数字补间 ---------------- */
function tween(el, to) {
  if (!el) return;
  to = Math.max(0, to || 0);
  if (el._v === undefined) {          // 首帧直接落值，否则会一直停在占位符
    el._v = to;
    el.innerHTML = renderNum(to);
    return;
  }
  if (el._v === to && !el._raf) return;   // 没变且没有动画在跑：一次 DOM 都不碰
  const from = el._v;
  if (!ANIM || matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el._v = to;
    el.innerHTML = renderNum(to);
    return;
  }
  const t0 = performance.now(), ms = 420;
  cancelAnimationFrame(el._raf);
  const step = (t) => {
    const k = Math.min(1, (t - t0) / ms);
    const e = 1 - Math.pow(1 - k, 3);
    // 每帧回写"屏幕上正在显示的值"，这样 200ms 后再来一次更新也能从当前位置续上，
    // 不会像原来那样从上一个目标值重新起步（周期一短就会跳）
    el._v = from + (to - from) * e;
    el.innerHTML = renderNum(el._v);
    el._raf = k < 1 ? requestAnimationFrame(step) : 0;
  };
  el._raf = requestAnimationFrame(step);
}
function renderNum(n) {
  const [num, unit] = shortParts(n);
  return unit ? `${num}<small>${unit}</small>` : num;
}

/* ---------------- KPI ---------------- */
const RANGE_LABEL = {
  today: '今天', yesterday: '昨天', before: '前天', '7d': '近 7 天', '30d': '近 30 天',
};

/* 积分量级差很多：单日十几分，30 天八千多，按量级选精度 */
function fmtCredit(n) {
  // null / undefined = 这条维度没有积分记录，和"积分是 0"完全不是一件事，
  // 早先统一显示 0.00，结果每个免费模型的会话都像在烧积分，越看越不对
  if (n === null || n === undefined || n === '' || Number.isNaN(Number(n))) return '—';
  n = Number(n) || 0;
  if (n >= 1000) return Math.round(n).toLocaleString('zh-CN');
  if (n >= 10) return n.toFixed(1);
  return n.toFixed(2);
}

/* 有积分记录的轮数 / 总轮数。分母是轮数，不是事件数 */
function covText(covered, total) {
  covered = covered || 0;
  total = total || 0;
  if (!total) return '';
  if (!covered) return '无积分记录';
  return covered >= total ? '全轮有记录' : `${covered}/${total} 轮有记录`;
}

/* 一行两段的统一写法：「积分 3.46 · 1/2 轮有记录」 */
function creditLine(item, turns) {
  const t = turns == null ? item.turns : turns;
  const cov = covText(item.credit_reqs, t);
  return `积分 <b>${fmtCredit((item.credit_reqs || 0) > 0 ? item.credit : null)}</b>` +
    (cov ? `<span class="dim"> · ${cov}</span>` : '');
}

function renderKpis() {
  const s = state.summary, ss = state.session, p = state.p, t = state.today;
  if (!s || !p) return;
  // 指纹只取这张卡真正读到的字段，别把整个 state 串进去
  if (!dirty('kpi', [state.range, p, ss, t && t.now_hour,
                     { c: s.credit, to: s.totals, a: s.all_time, i: s.indexing }])) {
    return;
  }
  const label = RANGE_LABEL[state.range] || '区间';

  // ① 会话卡：卡的"主角"随区间切换
  //    今天   → 当前正在跑的会话，大数 = 今日消耗
  //    历史区间 → 该区间里最贵的那条会话，大数 = 它在区间内的消耗
  //    第三行统一给"这条会话的历史累计"作为次要信息
  const isToday = state.range === 'today';
  let card = null;
  if (isToday && ss && ss.session_id) {
    card = {
      label: '活跃会话',
      name: ss.title || ss.project || '当前会话',
      model: ss.model || '',
      primary: ss.tokens_today || 0,
      credit: ss.credit_today || 0,
      credit_reqs: ss.credit_reqs_today || 0,
      turns: ss.turns_today || 0,
      totalTokens: ss.tokens || 0,
      totalCredit: ss.credit_total || 0,
      totalCreditReqs: ss.credit_reqs_total || 0,
      totalTurns: ss.turns || 0,
      live: true,
    };
  } else if ((p.top_sessions || []).length) {
    const t0 = p.top_sessions[0];
    card = {
      label: '区间最贵会话',
      name: t0.title || '未命名会话',
      model: t0.model || '',
      primary: t0.tokens || 0,
      credit: t0.credit || 0,
      credit_reqs: t0.credit_reqs || 0,
      turns: t0.turns || 0,
      totalTokens: t0.total_tokens || 0,
      totalCredit: t0.total_credit || 0,
      totalCreditReqs: 0,
      totalTurns: 0,
      live: false,
    };
  }

  if (card) {
    tween($('kSession'), card.primary);
    $('sessCap').textContent = card.label;
    $('sessName').textContent = card.name;
    // 模型名放到第二行：标题行已经够挤了，塞个 chip 会把会话名压成省略号
    $('kSessionSub').innerHTML =
      `${isToday ? '今日' : '区间'}${creditLine(card, card.turns)}<br>` +
      `<span class="dim">${card.model ? escapeHtml(card.model) + ' · ' : ''}累计 ` +
      `${fmtShort(card.totalTokens)} · 积分 ` +
      `${fmtCredit(card.totalCreditReqs ? card.totalCredit : (card.totalCredit || null))}` +
      (card.totalTurns ? ` · 共 ${card.totalTurns} 轮` : '') + `</span>`;
  } else {
    $('sessCap').textContent = '活跃会话';
    $('sessName').textContent = '';
    $('kSessionSub').innerHTML = '<span class="dim">该区间没有会话记录</span>';
  }

  // 状态灯单独抽出去：它会随真实时间自己变（空闲 N 分 → 已结束），
  // 不能只在数据更新时算一次，否则会一直停在"活跃"
  state.cardLive = !!(card && card.live);
  state.hasCard = !!card;
  if (!state.sessAt) state.sessAt = Date.now();
  updateStatus();

  // ② 所选区间累计
  tween($('kRange'), p.tokens);
  $('rangeCap').textContent = label + '累计';
  $('rangeTag').textContent = p.days > 1 ? `${p.days} 天` : '';
  $('kRangeSub').innerHTML =
    creditLine(p) + `<br>` +
    `<span class="dim">${(p.start || '').slice(5)} ~ ${(p.end || '').slice(5)} · ${p.turns} 轮</span>`;

  // ③ 多日看日均；单日看轮均（一天算"日均"等于累计，没信息量）
  //    分母刻意用"有积分记录的天/轮"：没记录的轮次不是花了 0 分，是不知道
  const days = Math.max(1, p.days || 1);
  const turns = Math.max(1, p.turns || 0);
  const crDays = p.credit_days || 0;
  const crReqs = p.credit_reqs || 0;
  if (days > 1) {
    tween($('kAvg'), p.tokens / days);
    $('avgCap').textContent = '区间日均';
    $('kAvgSub').innerHTML =
      `日均积分 <b>${fmtCredit(crDays ? p.credit / crDays : null)}</b><br>` +
      `<span class="dim">${days} 天 · ${crDays} 天有积分记录 · 缓存 ${fmtShort(p.cache_read)}</span>`;
  } else {
    tween($('kAvg'), p.tokens / turns);
    $('avgCap').textContent = '轮均消耗';
    $('kAvgSub').innerHTML =
      `轮均积分 <b>${fmtCredit(crReqs ? p.credit / crReqs : null)}</b><br>` +
      `<span class="dim">${p.turns} 轮 · ${crReqs} 轮有积分记录 · 缓存 ${fmtShort(p.cache_read)}</span>`;
  }

  // ④ 缓存命中率（跟随区间）
  $('kCache').innerHTML = (p.cache_hit_rate || 0).toFixed(1) + '<small>%</small>';
  $('cacheTag').textContent = label;

  const c = s.credit || {};
  let creditTxt = '积分 —';
  if (c.available) {
    // 覆盖率的分母是"请求轮数"，不是积分行数 —— 前者才是用户能感知的缺口
    creditTxt = c.mode === 'jsonl_partial'
      ? `积分 ${fmtShort(c.jsonl_total)}（仅团队调用）`
      : `全历史积分 ${fmtCredit(c.total)}` +
        (c.coverage != null ? ` · 覆盖 ${c.coverage}%（${c.reqs_covered}/${c.reqs} 轮）` : '');
  }
  $('kCacheSub').innerHTML =
    `缓存 ${fmtShort(p.cache_read)} / 输入 ${fmtShort(p.input)}<br><span class="dim">${creditTxt}</span>`;
  // 覆盖率的成因放在悬停里：一个字都不多占，但想深究时能立刻看到
  const kc = $('kCacheSub');
  if (kc) {
    kc.title = c.available && c.sessions_without_credit
      ? `${c.sessions_without_credit} 个会话在 workbuddy.db 里没有用量记录，`
        + `这些轮次拿不到积分数据（不是 0 分，是不知道）`
      : '';
  }

  renderIndexInfo();
  const cr0 = s.credit || {};
  $('foot').textContent =
    `累计 ${fmtFull(s.all_time.tokens)} tokens · 积分 ${fmtCredit(cr0.available ? cr0.total : null)}` +
    (cr0.available && cr0.coverage != null ? `（覆盖 ${cr0.coverage}%）` : '') +
    ` · ${fmtFull(s.all_time.turns)} 轮 · ${s.all_time.days} 天 · 纯本地处理，不上传`;

  // 迷你柱：跟随区间；只有"今天"才高亮当前小时
  drawMini(p.hours, state.range === 'today' && t ? t.now_hour : null);
  // 最近一分钟走势曲线已取消：画布始终隐藏，空间留给文字
  const sp = $('spark');
  if (sp) sp.style.display = 'none';
}

function renderIndexInfo() {
  const s = state.summary;
  if (!s) return;
  const idx = s.indexing || {};
  setText($('indexInfo'), idx.active
    ? `索引中 ${idx.pct}%`
    : `索引 ${(s.totals || {}).files || 0} 文件 · ${idx.last_cost_ms || 0}ms`);
}

function updateStatus() {
  const st = $('sessTag');
  if (!st) return;
  const ss = state.session;
  let cls = 'off', txt = '';
  if (state.cardLive) {
    // idle_s 是"拉数据那一刻"的值，之后要用本地流逝时间补上，
    // 否则没有新数据时空闲分钟数会一直冻在那里
    const drift = state.sessAt ? (Date.now() - state.sessAt) / 1000 : 0;
    const idle = ss && ss.idle_s != null ? ss.idle_s + drift : null;
    if (ss && ss.delta_60s > 0 && drift < 90) { cls = 'on'; txt = '正在消耗'; }
    else if (idle == null || idle < 90) { cls = 'on'; txt = '活跃'; }
    else if (idle < 3600) { cls = 'idle'; txt = '空闲 ' + Math.max(1, Math.round(idle / 60)) + ' 分'; }
    else { cls = 'off'; txt = '已结束 · ' + agoText(idle); }
  } else if (state.hasCard) {
    txt = '历史会话';
  }
  const full = 'status' + (cls ? ' ' + cls : '');
  if (st._c !== full) { st._c = full; st.className = full; }
  setText(st, txt);
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/* ---------------- 迷你图 ---------------- */
function drawMini(hours, nowHour) {
  const box = $('todayMini');
  if (!box) return;
  const from = Math.max(0, (nowHour == null ? 23 : nowHour) - 11);
  const seg = (hours || []).slice(from, Math.max(from + 1, (nowHour == null ? 24 : nowHour) + 1));
  const max = Math.max(...seg, 1);
  const key = seg.join(',') + '|' + (nowHour == null ? '' : nowHour);
  if (box._k === key) return;
  box._k = key;
  // 节点数不变时只改高度：重建 <i> 会让 CSS transition 失效，看起来是"瞬跳"
  if (box.children.length !== seg.length) {
    box.innerHTML = seg.map(() => '<i></i>').join('');
  }
  seg.forEach((val, i) => {
    const el = box.children[i];
    if (!el) return;
    const pct = Math.max(3, val / max * 100);
    if (el._h !== pct) { el._h = pct; el.style.height = pct + '%'; }
    const cls = i === seg.length - 1 ? 'hot' : '';
    if (el._c !== cls) { el._c = cls; el.className = cls; }
  });
}

/* ---------------- 图表 ---------------- */
function initCharts() {
  charts.hour = echarts.init($('chartHour'), null, { renderer: 'canvas' });
  charts.pie = echarts.init($('chartPie'), null, { renderer: 'canvas' });
  charts.trend = echarts.init($('chartTrend'), null, { renderer: 'canvas' });
  charts.heat = echarts.init($('chartHeat'), null, { renderer: 'canvas' });
  const ro = new ResizeObserver(debounce(() => {
    Object.values(charts).forEach(c => c && c.resize());
    // 两张排行都是 HTML，不归 ECharts 管，自己盯高度切单行/两行
    syncRankMode($('projRank'));
    syncRankMode($('sessRank'));
  }, 150));
  document.querySelectorAll('.chart').forEach(el => ro.observe(el));
  ['projRank', 'sessRank'].forEach(id => {
    const el = $(id);
    if (el) ro.observe(el);
  });
}

function renderHour() {
  const p = state.p;
  if (!p) return;
  const accent = v('--accent'), muted = v('--muted'), grid = v('--grid'), text = v('--text');
  const label = RANGE_LABEL[state.range] || '区间';
  const single = (p.days || 1) === 1;
  const hours = p.hours || [];
  const nowHour = state.range === 'today' && state.today ? state.today.now_hour : null;
  const peak = hours.length ? hours.indexOf(Math.max(...hours)) : null;
  if (!dirty('hour', [state.range, p.days, hours, nowHour, NARROW()])) return;

  charts.hour.setOption({
    animation: ANIM,
    animationDurationUpdate: ANIM ? 260 : 0,
    grid: { left: 46, right: 10, top: 16, bottom: NARROW() ? 26 : 20 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: v('--panel'), borderColor: v('--border'), textStyle: { color: text, fontSize: 12 },
      formatter: (ps) => {
        const h = ps[0].axisValue;
        const i = ps[0].dataIndex;
        const cur = ps.find(x => x.seriesName === label);
        const cr = (p.credit_hours || [])[i] || 0;
        return `<b>${h}</b><br>${label} ${fmtFull(cur ? cur.value : 0)} tokens` +
          `<br><span style="opacity:.65">积分 ${fmtCredit(cr || null)}</span>`;
      },
    },
    xAxis: {
      type: 'category',
      data: Array.from({ length: 24 }, (_, i) => i + '时'),
      axisLine: { lineStyle: { color: grid } },
      axisTick: { show: false },
      // 每 3 小时一个刻度，但"当前小时"和"峰值小时"必须单独标出来：
      // 否则柱子悬在 9时 和 12时 中间，读者得自己数格子才知道是哪一刻
      axisLabel: {
        color: muted, fontSize: NARROW() ? 9 : 10,
        interval: (idx) => idx % (NARROW() ? 6 : 3) === 0 || idx === nowHour || idx === peak,
        // 峰值那一格加粗并染色，和"峰值 11 时"的文案对上
        formatter: (val, idx) => (idx === peak && peak != null ? `{p|${val}}` : val),
        rich: { p: { color: accent, fontWeight: 700, fontSize: NARROW() ? 9 : 10 } },
      },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: grid } },
      axisLabel: { color: muted, fontSize: 10, formatter: (x) => fmtAxis(x) },
    },
    series: [
      {
        id: 'hour',
        name: label, type: 'bar',
        // 24 根柱子塞进 940px，固定 barWidth 会撑爆，用百分比 + 上限
        barWidth: '62%', barMaxWidth: 26, barCategoryGap: '30%',
        animationDurationUpdate: 260,
        data: hours.map((val, i) => ({
          value: val,
          itemStyle: {
            // 当前小时用主色实心，其余用同一色相的弱化渐变，视觉上是一族
            color: i === nowHour ? barHot(accent) : barDim(accent),
            borderRadius: [BAR_R, BAR_R, 0, 0],
          },
        })),
        emphasis: { itemStyle: { color: barHot(accent) } },
      },
    ],
    // 用 merge 而不是 notMerge：后者每次都会重建组件，入场动画会重播，
    // 悬停中的 tooltip 也会被销毁重建（表现出来就是一直闪）
  }, { notMerge: false, lazyUpdate: true });

  $('hourCap').textContent = single ? `${label}按小时` : `${label}各小时合计`;
  $('hourHint').innerHTML = peak != null && hours[peak]
    ? `峰值 <b>${peak} 时</b> · ${fmtShort(hours[peak])} · 积分 ` +
      `${fmtCredit((p.credit_hours || [])[peak] || null)}`
    : '';
}

function renderPie() {
  const items = (state.by === 'project' ? state.p.by_project
    : state.by === 'account' ? state.p.by_account
      : state.p.by_model) || [];
  const accent = v('--accent'), muted = v('--muted'), text = v('--text');
  const narrow = NARROW();
  if (!dirty('pie', [state.by, state.range, items, narrow])) return;
  // 维度切换时图例项数会变，普通 merge 会把旧图例名留在原地
  const byChanged = state._pieBy !== state.by;
  state._pieBy = state.by;

  const top = items.slice(0, 6).map(x => ({
    name: x.label || x.name, value: x.tokens,
    credit: x.credit || 0, credit_reqs: x.credit_reqs || 0, turns: x.turns || 0,
  }));
  if (items.length > 6) {
    const rest = items.slice(6);
    const sum = (k) => rest.reduce((a, b) => a + (b[k] || 0), 0);
    top.push({ name: '其他', value: sum('tokens'), credit: sum('credit'),
               credit_reqs: sum('credit_reqs'), turns: sum('turns') });
  }
  const total = top.reduce((a, b) => a + b.value, 0) || 1;
  const sumCredit = items.reduce((a, b) => a + (b.credit || 0), 0);
  const sumCrq = items.reduce((a, b) => a + (b.credit_reqs || 0), 0);
  const sumTurns = items.reduce((a, b) => a + (b.turns || 0), 0);
  const palette = [accent, v('--accent-2'), '#7c6cf0', '#e0774f', '#3ea88f', '#c9a227', muted];

  charts.pie.setOption({
    animation: ANIM,
    color: palette,
    title: {
      text: fmtShort(total),
      // 环心就是区间总量，积分跟着写在一起，不用再去别的卡片找。
      // 必须分两行：环内宽度有限，单行会被圆环盖掉后半截
      subtext: (({ today: '今日', yesterday: '昨日', before: '前日',
                   '7d': '7 天', '30d': '30 天' })[state.range] || '今日') +
               `\n积分 ${fmtCredit(sumCrq ? sumCredit : null)}`,
      left: narrow ? '50%' : '34%',
      top: narrow ? '28%' : '32%',
      textAlign: 'center',
      textStyle: { color: text, fontSize: narrow ? 15 : 17, fontWeight: 700, fontFamily: 'inherit' },
      subtextStyle: { color: muted, fontSize: narrow ? 9.5 : 10.5,
                      lineHeight: narrow ? 12 : 14, fontFamily: 'inherit' },
    },
    tooltip: {
      trigger: 'item',
      backgroundColor: v('--panel'), borderColor: v('--border'), textStyle: { color: text, fontSize: 12 },
      formatter: (p) => {
        const it = top[p.dataIndex] || {};
        return `${p.name}<br>${fmtFull(p.value)} tokens · ${(p.value / total * 100).toFixed(1)}%` +
          `<br><span style="opacity:.65">积分 ${fmtCredit(it.credit_reqs ? it.credit : null)}` +
          (it.turns ? ` · ${covText(it.credit_reqs, it.turns)}` : '') + `</span>`;
      },
    },
    legend: narrow ? {
      // 窄屏干脆不画图例：横排会挤掉环，竖排会压到标题，信息改由副标题承载
      show: false,
    } : {
      type: 'scroll', orient: 'vertical', right: 4, top: 'middle',
      itemWidth: 8, itemHeight: 8, itemGap: 7,
      textStyle: { color: muted, fontSize: 11 },
      pageIconColor: muted, pageTextStyle: { color: muted, fontSize: 10 },
    },
    series: [{
      id: 'pie',
      type: 'pie',
      radius: narrow ? ['38%', '60%'] : ['52%', '76%'],
      center: narrow ? ['50%', '38%'] : ['34%', '50%'],
      avoidLabelOverlap: true, minAngle: 3,
      itemStyle: { borderColor: v('--panel'), borderWidth: 2 },
      label: { show: false },
      emphasis: { scale: true, scaleSize: 4 },
      data: top,
      animationDurationUpdate: 320,
    }],
  }, { notMerge: false, lazyUpdate: true,
       replaceMerge: byChanged ? ['series', 'legend', 'title'] : undefined });

  const capBase = ({ model: '模型占比', project: '项目占比', account: '账号占比' })[state.by]
    + ' · ' + (RANGE_LABEL[state.range] || '今日');
  $('pieCap').innerHTML = narrow && top[0]
    ? `${capBase}<span class="hint">${escapeHtml(top[0].name)}</span>`
    : `${capBase}<span class="hint">${escapeHtml(covText(sumCrq, sumTurns))}</span>`;
}

/* ---------------- 项目 TOP5 / 会话 TOP5 ---------------- */

/* ---------------- 排行列表（项目 / 会话共用） ----------------
   这一块刻意不用 ECharts：类目标签的基线锚定让"两行标签"和柱子对不齐，
   行距还会忽宽忽窄（实测同一面板里 33px / 21px 混着来）。改成行布局后
   每一行的高度、名称省略、副标题都是确定的。

   项目排行和会话排行用的是同一套结构：序号圆牌 + 名称 + 右侧副标签 +
   条形 + 数值。两栏并排时行高、条形粗细、颜色、省略规则全部一致，
   只是喂进来的字段不同（项目给占比，会话给模型名）。 */
function rankRows(list) {
  // 统一成 {name, sub, tokens, credit, creditReqs, turns, tip}
  return list.map((x) => ({
    name: x.title || x.label || x.name || x.project || '未命名',
    sub: x.model || (x.share != null ? x.share.toFixed(1) + '%' : ''),
    tokens: x.tokens || 0,
    credit: x.credit,
    creditReqs: x.credit_reqs || 0,
    turns: x.turns || 0,
    tip: x._tip || '',
  }));
}

function renderRankList(box, list, opts) {
  if (!box) return;
  if (!list.length) {
    setHTML(box, `<div class="rk-empty">${opts.empty}</div>`);
    box._rows = 0;
    syncRankMode(box);
    return;
  }
  const max = Math.max(...list.map(x => x.tokens), 1);

  // 行数变了才重建 DOM；否则只改内容，这样条形宽度能有过渡动画
  if (box._rows !== list.length) {
    setHTML(box, list.map(() => (
      '<div class="rk"><div class="rk-top"><span class="rk-no"></span>' +
      '<span class="rk-name"></span><span class="rk-model"></span></div>' +
      '<div class="rk-bar"><span class="rk-track"><i></i></span>' +
      '<span class="rk-val"></span></div></div>')).join(''));
    box._rows = list.length;
  }

  list.forEach((x, i) => {
    const el = box.children[i];
    if (!el) return;
    const cr = x.creditReqs > 0 ? fmtCredit(x.credit) : '—';
    setText(el.querySelector('.rk-no'), String(i + 1));
    setText(el.querySelector('.rk-name'), x.name);
    setText(el.querySelector('.rk-model'), x.sub || '');
    const pct = Math.max(1.5, (x.tokens || 0) / max * 100);
    const fill = el.querySelector('.rk-track i');
    if (fill._w !== pct) { fill._w = pct; fill.style.width = pct.toFixed(2) + '%'; }
    setText(el.querySelector('.rk-val'), `${fmtShort(x.tokens)} · ${cr}`);
    if (x.tip && el.title !== x.tip) el.title = x.tip;
    el.classList.toggle('top', i === 0);
  });

  syncRankMode(box);
}

/* ---------------- 项目消耗 TOP 5 ---------------- */
function renderProj() {
  const items = (state.p && state.p.by_project) || [];
  const box = $('projRank');
  if (!box) return;
  if (!dirty('proj', [state.range, items])) return;

  const lbl = RANGE_LABEL[state.range] || '今日';
  const list = items.slice(0, 5);
  $('projCap').textContent = '项目消耗 TOP 5';
  $('projHint').textContent = list.length
    ? `${lbl} · 显示 ${list.length}/${items.length} 条 · 右为 token · 积分`
    : `${lbl}暂无数据`;

  const rows = rankRows(list).map((r, i) => {
    const src = list[i];
    r.tip = `${r.name}\n${fmtFull(r.tokens)} tokens · 积分 ${fmtCredit(r.creditReqs ? r.credit : null)}\n` +
      `${r.turns} 轮 · ${covText(r.creditReqs, r.turns)}` +
      (src.share != null ? `\n占比 ${src.share.toFixed(1)}%` : '');
    return r;
  });
  renderRankList(box, rows, { empty: '该区间没有项目记录' });
}

/* ---------------- 会话消耗 TOP 5 ---------------- */
function renderSessList() {
  const items = (state.p && state.p.top_sessions) || [];
  const box = $('sessRank');
  if (!box) return;
  if (!dirty('sess', [state.range, items])) return;

  const lbl = RANGE_LABEL[state.range] || '今日';
  const list = items.slice(0, 5);
  $('sessHint').textContent = list.length
    ? `${lbl} · 显示 ${list.length}/${items.length} 条 · 右为 token · 积分`
    : `${lbl}暂无数据`;

  const rows = rankRows(list).map((r, i) => {
    const src = list[i];
    const dur = Math.max(0, ((src.last_ts || 0) - (src.first_ts || 0)) / 1000);
    r.tip = `${r.name}\n${r.sub || '未知模型'}\n` +
      `${fmtFull(r.tokens)} tokens · 积分 ${fmtCredit(r.creditReqs ? r.credit : null)}\n` +
      `${r.turns} 轮 · ${fmtDurShort(dur)} · ${covText(r.creditReqs, r.turns)}\n` +
      `历史累计 ${fmtFull(src.total_tokens || 0)} tokens · 积分 ` +
      `${fmtCredit(src.total_credit || null)}`;
    return r;
  });
  renderRankList(box, rows, { empty: '该区间没有会话记录' });
}

/* 卡片高度决定行内排版：矮到放不下两行就切单行，绝不互相压 */
function syncRankMode(box) {
  box = box || $('sessRank');
  if (!box) return;
  // 滚动模式下始终用紧凑单行，避免内容被挤压
  const n = Math.max(1, box.children.length);
  const perRow = box.clientHeight / n;
  const compact = perRow < 40;  // 阈值从 32 提高到 40，给滚动模式更多空间
  if (box._compact !== compact) {
    box._compact = compact;
    box.classList.toggle('compact', compact);
  }
}


function renderTrend() {
  const s = state.series || [];
  const accent = v('--accent'), muted = v('--muted'), grid = v('--grid'), text = v('--text');
  if (!dirty('trend', [state.days, s, NARROW()])) return;
  const dates = s.map(x => x.date.slice(5));
  const vals = s.map(x => x.tokens);

  // 7 天窗口用 3 日均，30 天用 7 日均：窗口越长噪声越大，均线要跟着拉长
  const win = state.days > 7 ? 7 : 3;
  const ma = vals.map((_, i) => {
    const seg = vals.slice(Math.max(0, i - win + 1), i + 1);
    return Math.round(seg.reduce((a, b) => a + b, 0) / seg.length);
  });

  charts.trend.setOption({
    animation: ANIM,
    grid: { left: 46, right: 10, top: 34, bottom: 20 },
    // 柱+线混排，没有图例谁也认不出那条虚线是什么。放在右上角，和
    // "近 30 天累计" 的文案同一行区域，不额外占高度
    legend: {
      show: true, right: 0, top: 0, itemWidth: 10, itemHeight: 6, itemGap: 12,
      icon: 'roundRect',
      textStyle: { color: muted, fontSize: 10 },
      data: [{ name: '每日', icon: 'roundRect' }, { name: `${win}日均`, icon: 'line' }],
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow', shadowStyle: { color: hexA(accent, .07) } },
      backgroundColor: v('--panel'), borderColor: v('--border'), textStyle: { color: text, fontSize: 12 },
      formatter: (ps) => {
        const i = ps[0].dataIndex;
        return `<b>${s[i].date}</b><br>${fmtFull(vals[i])} tokens` +
          `<br><span style="opacity:.65">积分 ${fmtCredit(s[i].credit_reqs ? s[i].credit : null)}` +
          ` · ${s[i].turns} 轮 · ${win}日均 ${fmtShort(ma[i])}</span>`;
      },
    },
    xAxis: {
      type: 'category', data: dates,
      axisLine: { lineStyle: { color: grid } }, axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 10, hideOverlap: true,
                   interval: state.days > 7
                     ? Math.max(0, Math.floor(s.length / (NARROW() ? 4 : 8)))
                     : 0 },
    },
    yAxis: {
      type: 'value', splitLine: { lineStyle: { color: grid } },
      axisLabel: { color: muted, fontSize: 10, formatter: (x) => fmtAxis(x) },
    },
    series: [{
      name: '每日', type: 'bar', data: vals, z: 1,
      barMaxWidth: NARROW() ? 18 : 28, barCategoryGap: '38%',
      itemStyle: {
        borderRadius: [BAR_R, BAR_R, 0, 0],
        color: barFillV(accent, .92, .58),
      },
      emphasis: { itemStyle: { color: barFillV(accent) } },
    }, {
      name: `${win}日均`, type: 'line', data: ma, smooth: .3, symbol: 'none', z: 2,
      lineStyle: { color: v('--accent-2'), width: 1.6, type: 'dashed', opacity: .95 },
      // 均线下面垫一层同色微透明填充，柱和线的层次一眼能分开
      areaStyle: { color: hexA(v('--accent-2'), .06) },
    }],
  }, { notMerge: false, lazyUpdate: true });

  const sum = vals.reduce((a, b) => a + b, 0);
  const sumCr = s.reduce((a, b) => a + (b.credit || 0), 0);
  const sumCrq = s.reduce((a, b) => a + (b.credit_reqs || 0), 0);
  $('trendHint').innerHTML = `近 <b>${state.days}</b> 天累计 <b>${fmtShort(sum)}</b>` +
    ` · 积分 <b>${fmtCredit(sumCrq ? sumCr : null)}</b>`;
}

function renderHeat() {
  const days = state.heat || [];
  const accent = v('--accent'), muted = v('--muted'), text = v('--text');
  const track = v('--track'), panel = v('--panel');
  if (!dirty('heat', [days, heatWeeks(), accent])) return;

  // 窗口锚在"数据里最后一天"，不用客户端当前时间：
  // 否则跨零点会整体左移一格，而且每台机器时区不同会错开
  let last = '';
  for (const d of days) if (d.date > last) last = d.date;
  if (!last) last = fmtDate(new Date());
  const end = parseDay(last);
  const start = new Date(end.getTime() - (heatWeeks() * 7 - 1) * 86400000);

  const vals = days.map(d => d.tokens).sort((a, b) => a - b);
  const p90 = (vals.length ? vals[Math.floor(vals.length * .92)] : 0) || 1;

  // 色阶加迟滞：分位数天天在动，不加的话每次刷新整张图都会深浅跳变，
  // 而且同一个格子的颜色两次看到的不一样，横向对比就失效了
  const sc = state.heatScale || (state.heatScale = { max: 0, stops: null, key: '' });
  if (!sc.max || p90 > sc.max * 1.12 || p90 < sc.max * 0.88) sc.max = p90;
  if (!sc.stops || sc.key !== accent) {
    sc.key = accent;
    sc.stops = [hexA(accent, .12), hexA(accent, .42), hexA(accent, .72), accent];
  }

  const win = fmtDate(start) + '~' + fmtDate(end);
  const winChanged = state._heatWin !== win;
  state._heatWin = win;

  const crMap = {};
  for (const d of days) crMap[d.date] = d.credit;

  charts.heat.setOption({
    animation: ANIM,
    tooltip: {
      backgroundColor: panel, borderColor: v('--border'), textStyle: { color: text, fontSize: 12 },
      formatter: (p) => `${p.value[0]}<br>${fmtFull(p.value[1])} tokens` +
        `<br><span style="opacity:.65">积分 ${fmtCredit(crMap[p.value[0]] || null)}</span>`,
    },
    // 图例会压住右下角的格子，这里关掉，靠 title 里的色深说明 + tooltip 表达
    visualMap: {
      show: false, min: 0, max: sc.max,
      inRange: { color: sc.stops },
    },
    calendar: {
      range: [fmtDate(start), fmtDate(end)],
      left: 30, right: 6, top: 16, bottom: 2,
      cellSize: ['auto', 'auto'],
      splitLine: { show: false },
      itemStyle: { color: track, borderWidth: 2, borderColor: panel, borderRadius: 2 },
      yearLabel: { show: false },
      // 并排后宽度减半，七个标签会叠在一起，只标一三五
      dayLabel: {
        firstDay: 1, color: muted, fontSize: 9, margin: 2,
        nameMap: { 0: '', 1: '一', 2: '', 3: '三', 4: '', 5: '五', 6: '' },
      },
      monthLabel: { nameMap: 'ZH', color: muted, fontSize: 9, margin: 2 },
    },
    series: [{
      id: 'heat',
      type: 'heatmap', coordinateSystem: 'calendar',
      data: days.map(d => [d.date, d.tokens]),
      itemStyle: { borderRadius: 2, borderWidth: 2, borderColor: panel },
      emphasis: { itemStyle: { borderColor: accent } },
    }],
  }, { notMerge: false, lazyUpdate: true,
       replaceMerge: winChanged ? ['calendar', 'series'] : undefined });
}

function parseDay(s) {
  const [y, m, d] = String(s || '').split('-').map(Number);
  return new Date(y || 2000, (m || 1) - 1, d || 1);
}

function fmtDate(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function relayout() {
  // 只在布局真的可能变的时候调（boot / 断点切换 / 主题切换）。
  // 数据刷新不需要 —— 每次 resize 都会重建 canvas 位图并全量重绘，白白吃 CPU。
  const fix = () => Object.values(charts).forEach(c => c && c.resize());
  requestAnimationFrame(fix);
  clearTimeout(relayout._t);
  relayout._t = setTimeout(fix, 120);
}


/* ---------------- 账号 ----------------
   归属是"事后解释"：events 先落库，accounts 由采集线程慢慢追平。
   追不上的会话显示成"未归属"，不猜、不并进任何账号。 */
const ACCT_NONE = '__none__';

function acctName(uid) {
  // 全站唯一数据源：后端 labels 里含活跃账号、历史账号与「未归属」。
  // 改名后后端重算并 bump，前端拉一次全量，所有位置一起换字。
  const a = state.accounts || {};
  const labels = a.labels || {};
  const key = uid || ACCT_NONE;
  if (labels[key]) return labels[key];
  const cur = a.current || {};
  if (cur.user_id === uid && cur.short) return cur.short;
  const hit = (a.items || []).find(x => x.user_id === uid);
  return (hit && hit.short) || labels[ACCT_NONE] || '未归属';
}

function renderAcct(force) {
  const a = state.accounts || {};
  const cur = a.current || {};
  const items = a.items || [];

  const chip = $('acctChip');
  if (chip) {
    // short 已经是完整显示名（默认带「账号」前缀或用户昵称），不要再拼一次
    setText(chip, cur.short || (a.available ? '账号 —' : '账号不可用'));
    const tip = cur.user_id ? '当前账号 ' + cur.user_id : '未识别到当前账号';
    if (chip.title !== tip) chip.title = tip;
    chip.classList.toggle('off', !cur.short);
  }

  const seg = $('segAcct');
  if (!seg) return;
  const hasNone = (a.unattributed_sessions || 0) > 0;
  // labels 进 key：任何账号改名（含未归属、历史账号）都要重画按钮
  const key = JSON.stringify([items.map(x => [x.user_id, x.short]),
    a.labels || {}, hasNone, state.acct, cur.short]);
  if (!force && seg._k === key) return;
  seg._k = key;

  const btns = [`<button data-v="all"${state.acct ? '' : ' class="on"'}>全部</button>`]
    .concat(items.map(x =>
      `<button data-v="${escapeHtml(x.user_id)}"${state.acct === x.user_id ? ' class="on"' : ''}>`
      + `${escapeHtml(x.short)}</button>`));
  if (hasNone) {
    btns.push(`<button data-v="${ACCT_NONE}"${state.acct === ACCT_NONE ? ' class="on"' : ''}>`
      + `${escapeHtml(acctName(ACCT_NONE))}</button>`);
  }
  setHTML(seg, btns.join(''));
  seg.style.display = items.length ? '' : 'none';
}

function maybeAcctToast() {
  const cur = ((state.accounts || {}).current || {}).user_id || '';
  if (state.lastAcct === undefined) { state.lastAcct = cur; return; }   // 首屏不提示
  if (cur === state.lastAcct) return;
  const from = acctName(state.lastAcct), to = acctName(cur);
  state.lastAcct = cur;
  const el = $('acctToast');
  if (!el) return;
  el.textContent = `账号已切换：${from} → ${to}`;
  el.classList.add('on');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('on'), 6000);
}

async function toggleAcctLog() {
  const pop = $('acctLogPop');
  if (!pop) return;
  if (pop.classList.contains('on')) { pop.classList.remove('on'); return; }
  setHTML(pop, '<div class="row total"><span class="dim">加载中…</span></div>');
  pop.classList.add('on');
  try {
    const d = await getJSON('/api/accounts');
    const sw = d.switches || [];
    const au = d.audit || {};
    const rows = sw.map(s => {
      const t = new Date(s.ts);
      const p = (n) => String(n).padStart(2, '0');
      const when = `${t.getMonth() + 1}/${t.getDate()} ${p(t.getHours())}:${p(t.getMinutes())}`;
      return `<div class="row"><span>${escapeHtml(acctName(s.from_user))} → `
        + `${escapeHtml(acctName(s.to_user))}</span><span class="dim">${when}</span></div>`;
    });
    const rate = au.tokens ? (100 * au.tokens_attributed / au.tokens) : 0;
    setHTML(pop, (rows.length ? rows.join('')
        : '<div class="row total"><span class="dim">还没有切换记录</span></div>')
      + `<div class="row total"><span class="dim">归属覆盖 ${rate.toFixed(1)}%`
      + ` · 未知会话 ${au.sessions_unknown || 0}</span></div>`);
    setText($('acctLogBtn'), '⇄ ' + sw.length);
  } catch (e) {
    setHTML(pop, '<div class="row total"><span class="dim">读取失败</span></div>');
  }
}


/* ---------------- 账号改名 ---------------- */
function renameRows() {
  const a = state.accounts || {};
  const labels = a.labels || {};
  const rows = (a.items || []).map(x => ({
    uid: x.user_id, label: labels[x.user_id] || x.short,
  }));
  rows.push({ uid: ACCT_NONE, label: labels[ACCT_NONE] || '未归属' });
  return rows;
}

function renderRenamePop() {
  const pop = $('acctRenamePop');
  if (!pop) return;
  const aliases = (state.accounts || {}).aliases || {};
  const lines = renameRows().map(r =>
    `<div class="row rename-row"><span class="dim" title="${escapeHtml(r.uid)}">`
    + `${escapeHtml(r.label)}</span>`
    + `<input type="text" data-uid="${escapeHtml(r.uid)}" maxlength="32" `
    + `value="${escapeHtml(aliases[r.uid] || '')}" placeholder="${escapeHtml(r.label)}">`
    + `</div>`).join('');
  setHTML(pop, '<div class="row total"><span>自定义账号名称（留空恢复默认）</span></div>'
    + lines
    + '<div class="row total rename-actions"><span class="rename-msg" id="renameMsg"></span>'
    + '<button class="btn-mini" id="renameSave">保存</button></div>');
  pop.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); saveRename(); }
    });
  });
  const btn = $('renameSave');
  if (btn) btn.addEventListener('click', saveRename);
}

async function saveRename() {
  const pop = $('acctRenamePop');
  const msg = $('renameMsg');
  if (!pop) return;
  const names = {};
  pop.querySelectorAll('input[data-uid]').forEach(inp => {
    names[inp.dataset.uid] = inp.value.trim();
  });
  if (msg) setText(msg, '保存中…');
  try {
    const r = await fetch('/api/accounts/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ names }),
    });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || ('HTTP ' + r.status));
    if (d.accounts) state.accounts = d.accounts;

    // 清掉所有渲染指纹 + 强制重拉整页：顶栏、筛选、饼图、会话卡、时间线一起换字
    resetDirty();
    renderAcct(true);
    await sync(true);

    // 弹层里显示的是"实际生效的名字"（重名时后端会补短码），就地更新不重建 DOM
    const labels = (state.accounts && state.accounts.labels) || {};
    pop.querySelectorAll('.rename-row').forEach(row => {
      const inp = row.querySelector('input');
      if (!inp) return;
      const live = labels[inp.dataset.uid] || inp.placeholder;
      const tag = row.querySelector('.dim');
      if (tag) setText(tag, live);
      inp.placeholder = live;
    });
    if (msg) setText(msg, '已保存');

    const logPop = $('acctLogPop');
    if (logPop && logPop.classList.contains('on')) {
      logPop.classList.remove('on');
      toggleAcctLog();
    }
  } catch (e) {
    if (msg) setText(msg, '保存失败：' + e.message);
  }
}

function toggleRenamePop() {
  const pop = $('acctRenamePop');
  if (!pop) return;
  if (pop.classList.contains('on')) { pop.classList.remove('on'); return; }
  const logPop = $('acctLogPop');
  if (logPop) logPop.classList.remove('on');
  renderRenamePop();
  pop.classList.add('on');
  const first = pop.querySelector('input');
  if (first) first.focus();
}

function bindAcct() {
  const seg = $('segAcct');
  if (seg) {
    seg.addEventListener('click', async (e) => {
      const btn = e.target.closest('button');
      if (!btn || btn.classList.contains('on')) return;
      [...seg.children].forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      state.acct = btn.dataset.v === 'all' ? null : btn.dataset.v;
      await sync(true);        // 版本号没变，普通 sync 会被 v 相等挡掉
    });
  }
  const logBtn = $('acctLogBtn');
  const pop = $('acctLogPop');
  if (logBtn) {
    logBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleAcctLog(); });
  }
  const renameBtn = $('acctRenameBtn');
  const renamePop = $('acctRenamePop');
  if (renameBtn) {
    renameBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleRenamePop(); });
  }
  document.addEventListener('click', (e) => {
    for (const [p, trigger] of [[pop, logBtn], [renamePop, renameBtn]]) {
      if (!p || !p.classList.contains('on')) continue;
      if (p.contains(e.target) || (trigger && trigger.contains(e.target))) continue;
      p.classList.remove('on');
    }
  });
}

function renderAll(force) {
  if (force) resetDirty();     // 主题切换会换颜色，指纹必须作废
  renderAcct(force);
  renderKpis();
  renderHour();
  renderPie();
  renderProj();
  renderSessList();
  renderTrend();
  renderHeat();
}

/* ---------------- 数据 ---------------- */
async function getJSON(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error(url + ' ' + r.status);
  return r.json();
}

/* 整页数据：一次请求拿全，避免"趋势是 v5、热力图是 v4"这种撕裂 */
function bundleUrl() {
  return `/api/bundle?range=${encodeURIComponent(state.range)}` +
    `&days=${state.days}&weeks=${heatWeeks()}` +
    (state.acct ? `&acct=${encodeURIComponent(state.acct)}` : '');
}

let syncing = false, syncAgain = false;
async function sync(force) {
  if (syncing) { syncAgain = true; return; }     // 同一时刻只允许一个请求在飞
  syncing = true;
  try {
    const b = await getJSON(bundleUrl());
    if (!force && b.v === state.v) return;       // 版本没变，一个 DOM 都不碰
    applyBundle(b);
  } catch (e) {
    console.error('数据加载失败', e);
    setLive('down', '加载失败');
  } finally {
    syncing = false;
    if (syncAgain) { syncAgain = false; sync(true); }
  }
}

function applyBundle(b) {
  state.v = b.v;
  document.documentElement.dataset.v = b.v;   // 排障用：页面当前数据版本
  state.summary = b.summary;
  state.today = b.today;
  state.session = b.session;
  state.p = b.period;            // 关键：5 个渲染函数都读它，原来只有首屏喂过一次
  state.series = b.series || [];
  state.heat = b.heat || [];
  if (b.accounts) state.accounts = b.accounts;
  // 服务端会把非法 acct 白名单化成 null，这里跟随，避免前后端各说各话
  if ('acct' in b && b.acct !== state.acct) state.acct = b.acct;
  state.sessAt = Date.now();     // 状态灯的"空闲 N 分"从这一刻开始算流逝
  renderAll();
  maybeAcctToast();
}

function setLive(kind, text) {
  const el = $('live');
  el.className = 'live' + (kind === 'poll' ? ' warn' : kind === 'down' ? ' down' : '');
  el.textContent = text;
}

function connect() {
  try { es && es.close(); } catch (e) {}
  esClosed = false;
  es = new EventSource('/api/stream');

  // SSE 只告诉我们"版本变了"。数据另走一次 /api/bundle —— 因为趋势天数、
  // 热力图周数是本机参数，服务端不知道，广播完整快照必然会漏掉那几张图
  es.addEventListener('v', (ev) => {
    fails = 0;
    setLive('live', '实时');
    stopPolling();
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }

    if (d.i && state.summary && state.summary.indexing) {
      // 首次全量索引期间没有新事件，进度只能从这里更新
      const idx = state.summary.indexing;
      const a = !!d.i.a, pct = d.i.p == null ? idx.pct : d.i.p;
      if (idx.active !== a || idx.pct !== pct) {
        idx.active = a; idx.pct = pct;
        renderIndexInfo();
      }
    }

    if (d.v === state.v) return;
    if (document.hidden) { pendingV = d.v; return; }   // 隐藏期间只记不拉
    sync();
  });

  es.onopen = () => {
    fails = 0;
    setLive('live', '实时');
    if (pendingV !== state.v) { pendingV = state.v; sync(true); }   // 断线期间可能漏了
  };
  es.onerror = () => {
    try { es.close(); } catch (e) {}
    setLive('down', '重连中…');
    if (++fails >= 3) { startPolling(); }
    else setTimeout(connect, 800 * fails);
  };
}

function startPolling() {
  if (pollTimer) return;
  setLive('poll', '轮询 2s');
  const tick = async () => {
    if (document.hidden) return;
    try {
      await sync(true);
      setLive('poll', '轮询 2s');
    } catch (e) { setLive('down', '连接断开'); }
  };
  tick();
  pollTimer = setInterval(tick, 2000);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

/* ---------------- 交互 ---------------- */
function bindSeg(id, key, onChange) {
  $(id).addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn || btn.classList.contains('on')) return;
    [...e.currentTarget.children].forEach(b => b.classList.remove('on'));
    btn.classList.add('on');
    onChange(btn.dataset.v);
  });
}

function bindTheme() {
  $('themeBtn').addEventListener('click', () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem('tp-theme', root.dataset.theme); } catch (e) {}
    setTimeout(() => { renderAll(true); relayout(); }, 60);
  });
}

function tickClock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  $('clock').textContent = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  updateStatus();          // 状态灯的"空闲 N 分 / 已结束 X 前"每秒自己往前走
}

/* 切到后台就停止拉数据；断得太早会频繁重连，所以给 60 秒宽限。
   60 秒后断开 SSE，服务端 clients=0 才会把索引器降到 5 秒一拍 —— 这是待机省电的开关。 */
function bindVisibility() {
  document.addEventListener('visibilitychange', () => {
    if (STATIC) return;
    if (document.hidden) {
      hiddenAt = Date.now();
      clearTimeout(bgTimer);
      bgTimer = setTimeout(() => {
        try { es && es.close(); } catch (e) {}
        es = null; esClosed = true;
      }, 60000);
      return;
    }
    clearTimeout(bgTimer);
    const gone = esClosed || (hiddenAt && Date.now() - hiddenAt > 60000);
    hiddenAt = 0; esClosed = false;
    if (gone) { connect(); sync(true).then(() => relayout()); }
    else if (pendingV !== state.v) sync(true);
    updateStatus();
  });

  window.addEventListener('pagehide', () => {
    try { es && es.close(); } catch (e) {}
  });
}

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------------- 启动 ---------------- */
async function boot() {
  initCharts();
  bindTheme();
  bindAcct();
  bindSeg('segBreak', 'by', (val) => {
    state.by = val;
    renderPie();          // 数据已在 state.p 里，不需要再请求
  });
  bindSeg('segDays', 'days', async (val) => {
    state.days = +val;
    await sync(true);     // 服务端按 (series, days) 分键，只有趋势会重算
  });
  bindSeg('segRange', 'range', async (val) => {
    state.range = val;
    await sync(true);
  });

  // 支持 ?range=7d 直接打开指定区间（方便分享与截图核对）
  const preset = new URLSearchParams(location.search).get('range');
  if (preset && RANGE_LABEL[preset]) {
    state.range = preset;
    [...$('segRange').children].forEach(b => b.classList.toggle('on', b.dataset.v === preset));
  }

  // 支持 ?acct=<user_id> 直接看某个账号；非法值由服务端白名单化回"全部"
  const pacct = new URLSearchParams(location.search).get('acct');
  if (pacct) state.acct = pacct;

  // 支持 ?days=7 / 30 直接打开指定趋势窗（方便分享与截图核对）
  const pdays = +new URLSearchParams(location.search).get('days');
  if (pdays === 7 || pdays === 30) {
    state.days = pdays;
    [...$('segDays').children].forEach(b => b.classList.toggle('on', +b.dataset.v === pdays));
  }

  tickClock();
  setInterval(tickClock, 1000);
  if (STATIC) setLive('off', '静态快照');

  // 首屏：注入的数据完整就立刻画，省掉一次请求；不完整（比如 ?range=7d）就拉一次
  const bootOk = BOOT && BOOT.v && BOOT.period && BOOT.summary
    && state.range === 'today' && !state.acct && state.days === 30;
  if (bootOk) {
    applyBundle({
      v: BOOT.v, acct: null, accounts: BOOT.accounts,
      summary: BOOT.summary, today: BOOT.today, session: BOOT.session,
      period: BOOT.period, series: BOOT.series || [], heat: BOOT.heat || [],
    });
    relayout();
  } else {
    try {
      await sync(true);
      relayout();
    } catch (e) {
      console.error(e);
      setLive('down', '加载失败');
    }
  }

  // 跨断点时重排：饼图布局、坐标轴间隔、热力图周数都要跟着变
  let wasNarrow = NARROW();
  window.addEventListener('resize', debounce(() => {
    const n = NARROW();
    if (n === wasNarrow) return;
    wasNarrow = n;
    renderAll(true);
    relayout();
    sync(true);                    // 热力图周数变了，重新取一次
  }, 250));

  bindVisibility();
  if (!STATIC) connect();
}

document.addEventListener('DOMContentLoaded', boot);
