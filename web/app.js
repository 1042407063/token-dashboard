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
  charts.proj = echarts.init($('chartProj'), null, { renderer: 'canvas' });
  charts.trend = echarts.init($('chartTrend'), null, { renderer: 'canvas' });
  charts.heat = echarts.init($('chartHeat'), null, { renderer: 'canvas' });
  const ro = new ResizeObserver(debounce(() => {
    Object.values(charts).forEach(c => c && c.resize());
    syncRankMode();               // 卡片变高/变矮时切换单行/两行排版
  }, 150));
  document.querySelectorAll('.chart').forEach(el => ro.observe(el));
  const rk = $('sessRank');
  if (rk) ro.observe(rk);          // 排行是 HTML，不归 ECharts 管，自己盯高度
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
    grid: { left: 48, right: 12, top: 14, bottom: NARROW() ? 28 : 22 },
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
      axisLabel: { color: muted, fontSize: NARROW() ? 9 : 10, interval: NARROW() ? 5 : 2 },
    },
    yAxis: {
      type: 'value',
      splitLine: { lineStyle: { color: grid } },
      axisLabel: { color: muted, fontSize: 10, formatter: (x) => fmtShort(x) },
    },
    series: [
      {
        id: 'hour',
        name: label, type: 'bar', barWidth: '56%', animationDurationUpdate: 260,
        data: hours.map((val, i) => ({
          value: val,
          itemStyle: {
            color: i === nowHour ? accent : hexA(accent, .38),
            borderRadius: [3, 3, 0, 0],
          },
        })),
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
/* 柱子右侧永远两段：token 在前、积分在后。没有积分记录写 —，不写 0 */
function barLabel(it, value) {
  const cr = (it.credit_reqs || 0) > 0 ? fmtCredit(it.credit) : '—';
  return `{v|${fmtShort(value)}}{c|· ${cr}}`;
}
const BAR_RICH = (muted, faint) => ({
  v: { color: muted, fontSize: 10.5, fontFamily: 'inherit' },
  c: { color: faint, fontSize: 10, fontFamily: 'inherit', padding: [0, 0, 0, 3] },
});

function renderProj() {
  const items = (state.p.by_project || []).slice(0, 5);
  const accent = v('--accent'), muted = v('--muted'), text = v('--text');
  const faint = v('--faint');
  const rows = items.slice().reverse();               // 第一名放最上面
  const max = Math.max(...rows.map(x => x.tokens), 1);
  if (!dirty('proj', [state.range, items, NARROW()])) return;

  charts.proj.setOption({
    animation: ANIM,
    animationDurationUpdate: ANIM ? 260 : 0,
    // 不用 containLabel：它会在宽度紧张时把绘图区算得很小，这里直接给固定边距
    grid: NARROW() ? { left: 84, right: 96, top: 8, bottom: 8 }
          : { left: 112, right: 118, top: 10, bottom: 10 },
    tooltip: {
      trigger: 'item',
      backgroundColor: v('--panel'), borderColor: v('--border'),
      textStyle: { color: text, fontSize: 12 },
      formatter: (p) => {
        const it = rows[p.dataIndex] || {};
        return `${p.name}<br>${fmtFull(p.value)} tokens` +
          `<br><span style="opacity:.65">积分 ${fmtCredit(it.credit_reqs ? it.credit : null)}` +
          (it.turns ? ` · ${covText(it.credit_reqs, it.turns)}` : '') + `</span>`;
      },
    },
    xAxis: { type: 'value', show: false, max: max * 1.02 },
    yAxis: {
      type: 'category',
      data: rows.map(x => x.label || x.name),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: text, fontSize: NARROW() ? 10 : 11, width: NARROW() ? 72 : 96, overflow: 'truncate' },
    },
    series: [{
      id: 'proj',
      type: 'bar',
      data: rows.map(x => x.tokens),
      barMaxWidth: 20,
      barCategoryGap: '38%',
      itemStyle: { color: hexA(accent, .7), borderRadius: [0, 3, 3, 0] },
      label: {
        show: true, position: 'right',
        formatter: (p) => barLabel(rows[p.dataIndex] || {}, p.value),
        rich: BAR_RICH(muted, faint),
      },
    }],
  }, { notMerge: false, lazyUpdate: true });

  const lbl = RANGE_LABEL[state.range] || '今日';
  $('projCap').textContent = '项目消耗 TOP 5';
  $('projHint').textContent = items.length
    ? `${lbl} · 右为 token · 积分`
    : `${lbl}暂无数据`;
}

/* ---------------- 会话消耗 TOP 5 ----------------
   这一块刻意不用 ECharts：类目标签的基线锚定让"两行标签"和柱子对不齐，
   行距还会忽宽忽窄（实测同一面板里 33px / 21px 混着来）。改成行布局后
   每一行的高度、名称省略、模型名换行都是确定的。 */
function renderSessList() {
  const list = (state.p && state.p.top_sessions) || [];
  const box = $('sessRank');
  if (!box) return;
  if (!dirty('sess', [state.range, list])) return;

  const lbl = RANGE_LABEL[state.range] || '今日';
  $('sessHint').textContent = list.length
    ? `${lbl} · 右为 token · 积分`
    : `${lbl}暂无数据`;

  if (!list.length) {
    setHTML(box, '<div class="rk-empty">该区间没有会话记录</div>');
    box._rows = 0;
    syncRankMode();
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
    const cr = (x.credit_reqs || 0) > 0 ? fmtCredit(x.credit) : '—';
    setText(el.querySelector('.rk-no'), String(i + 1));
    setText(el.querySelector('.rk-name'), x.title || x.project || '未命名会话');
    setText(el.querySelector('.rk-model'), x.model || '未知模型');
    const pct = Math.max(1.5, (x.tokens || 0) / max * 100);
    const fill = el.querySelector('.rk-track i');
    if (fill._w !== pct) { fill._w = pct; fill.style.width = pct.toFixed(2) + '%'; }
    setText(el.querySelector('.rk-val'), `${fmtShort(x.tokens)} · ${cr}`);

    const dur = Math.max(0, ((x.last_ts || 0) - (x.first_ts || 0)) / 1000);
    const tip = `${x.title || '未命名会话'}\n${x.model || '未知模型'}\n` +
      `${fmtFull(x.tokens)} tokens · 积分 ${cr}\n` +
      `${x.turns || 0} 轮 · ${fmtDurShort(dur)} · ${covText(x.credit_reqs, x.turns)}\n` +
      `历史累计 ${fmtFull(x.total_tokens || 0)} tokens · 积分 ` +
      `${fmtCredit(x.total_credit || null)}`;
    if (el.title !== tip) el.title = tip;
    el.classList.toggle('top', i === 0);
  });

  syncRankMode();
}

/* 卡片高度决定行内排版：矮到放不下两行就切单行，绝不互相压 */
function syncRankMode() {
  const box = $('sessRank');
  if (!box) return;
  const n = Math.max(1, box.children.length);
  const perRow = box.clientHeight / n;
  const compact = perRow < 32;
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

  const ma = vals.map((_, i) => {
    const seg = vals.slice(Math.max(0, i - 6), i + 1);
    return Math.round(seg.reduce((a, b) => a + b, 0) / seg.length);
  });

  charts.trend.setOption({
    animation: ANIM,
    grid: { left: 44, right: 8, top: 8, bottom: 18 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: v('--panel'), borderColor: v('--border'), textStyle: { color: text, fontSize: 12 },
      formatter: (ps) => {
        const i = ps[0].dataIndex;
        return `<b>${s[i].date}</b><br>${fmtFull(vals[i])} tokens` +
          `<br><span style="opacity:.65">积分 ${fmtCredit(s[i].credit_reqs ? s[i].credit : null)}` +
          ` · ${s[i].turns} 轮 · 7日均 ${fmtShort(ma[i])}</span>`;
      },
    },
    xAxis: {
      type: 'category', data: dates, boundaryGap: false,
      axisLine: { lineStyle: { color: grid } }, axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 10,
                   interval: Math.max(0, Math.floor(s.length / (NARROW() ? 4 : 8))) },
    },
    yAxis: {
      type: 'value', splitLine: { lineStyle: { color: grid } },
      axisLabel: { color: muted, fontSize: 10, formatter: (x) => fmtShort(x) },
    },
    series: [{
      name: '每日', type: 'line', data: vals, smooth: .28, symbol: 'none',
      lineStyle: { color: accent, width: 2 },
      areaStyle: {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: hexA(accent, .3) }, { offset: 1, color: hexA(accent, .01) },
        ]),
      },
    }, {
      name: '7日均', type: 'line', data: ma, smooth: .3, symbol: 'none',
      lineStyle: { color: v('--accent-2'), width: 1.4, type: 'dashed', opacity: .9 },
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
  if (!uid) return '未归属';
  // 显示名由后端统一给（config.ACCOUNT_ALIASES / 账号名模板，且已做唯一化），
  // 切号时间线也跟着用；库里暂时查不到的账号退回带短码的名字，别再共用同一个名字
  const a = state.accounts || {};
  const cur = a.current || {};
  if (cur.user_id === uid && cur.short) return cur.short;
  const hit = (a.items || []).find(x => x.user_id === uid);
  return (hit && hit.short) || '账号 ' + uid.slice(0, 8);
}

function renderAcct(force) {
  const a = state.accounts || {};
  const cur = a.current || {};
  const items = a.items || [];

  const chip = $('acctChip');
  if (chip) {
    setText(chip, cur.short ? '账号 ' + cur.short
      : (a.available ? '账号 —' : '账号不可用'));
    const tip = cur.user_id ? '当前账号 ' + cur.user_id : '未识别到当前账号';
    if (chip.title !== tip) chip.title = tip;
    chip.classList.toggle('off', !cur.short);
  }

  const seg = $('segAcct');
  if (!seg) return;
  const hasNone = (a.unattributed_sessions || 0) > 0;
  // short 也要进 key：改了昵称后按钮文字得跟着刷新，不能只看 user_id
  const key = JSON.stringify([items.map(x => [x.user_id, x.short]), hasNone, state.acct]);
  if (!force && seg._k === key) return;
  seg._k = key;

  const btns = [`<button data-v="all"${state.acct ? '' : ' class="on"'}>全部</button>`]
    .concat(items.map(x =>
      `<button data-v="${escapeHtml(x.user_id)}"${state.acct === x.user_id ? ' class="on"' : ''}>`
      + `${escapeHtml(x.short)}</button>`));
  if (hasNone) {
    btns.push(`<button data-v="${ACCT_NONE}"${state.acct === ACCT_NONE ? ' class="on"' : ''}>未归属</button>`);
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
  document.addEventListener('click', (e) => {
    if (!pop || !pop.classList.contains('on')) return;
    if (pop.contains(e.target) || (logBtn && logBtn.contains(e.target))) return;
    pop.classList.remove('on');
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

  tickClock();
  setInterval(tickClock, 1000);
  if (STATIC) setLive('off', '静态快照');

  // 首屏：注入的数据完整就立刻画，省掉一次请求；不完整（比如 ?range=7d）就拉一次
  const bootOk = BOOT && BOOT.v && BOOT.period && BOOT.summary
    && state.range === 'today' && !state.acct;
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
