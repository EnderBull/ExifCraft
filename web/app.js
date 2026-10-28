/* ExifCraft · 前端主逻辑 */
'use strict';

const TOKEN = document.querySelector('meta[name="pm-token"]').content;
/* RAW 格式统一正则（佳能 CR3 / 富士 RAF / 哈苏 3FR / 飞思 IIQ 等）。所有 RAW 一律只读。 */
const RAW_RE = /\.(arw|dng|cr2|cr3|nef|orf|rw2|raf|pef|srw|raw|sr2|srf|rwl|3fr|iiq)$/i;
const CR3_RE = /\.cr3$/i;
const isRaw = (p) => RAW_RE.test(p);
const isCr3 = (p) => CR3_RE.test(p);
const PARAMS = new URLSearchParams(location.search);
const FRAMED = PARAMS.has('framed');
const CHROME = !FRAMED && PARAMS.has('chrome');

/* 平台差异：服务端渲染 index.html 时把 __PM_PLATFORM__ 换成 mac / win */
const PLATFORM = (document.querySelector('meta[name="pm-platform"]') || {}).content || 'win';
const IS_MAC = PLATFORM === 'mac';
const SEP = IS_MAC ? '/' : '\\';
const MOD = IS_MAC ? 'Cmd' : 'Ctrl';
const CONFIG_DIR_LABEL = IS_MAC
  ? '~/Library/Application Support/ExifCraft'
  : '%LOCALAPPDATA%\\ExifCraft';

/* ------------------------------------------------------------ utils */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
/* 取父目录。macOS 的 "/" 与 Windows 的 "D:\\" 都在这里收敛，避免上层自己拼分隔符。 */
function parentPath(p) {
  if (!p) return '';
  const s = String(p).replace(/[\\/]+$/, '');
  if (/^[A-Za-z]:$/.test(s)) return '';              // Windows 盘符根 / POSIX 根，没有更上层
  const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
  if (i < 0) return '';
  if (i === 0) return s[0];                          // POSIX 根目录
  const up = s.slice(0, i);
  return /^[A-Za-z]:$/.test(up) ? up + SEP : up;
}

const fmtSize = (n) => {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)} ${u[i]}`;
};
const fmtDate = (ts) => {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { 'X-PM-Token': TOKEN, 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}
const apiUrl = (p, extra = '') => `${p}${p.includes('?') ? '&' : '?'}t=${TOKEN}${extra}`;

/* ------------------------------------------------------------ toast */
function toast(msg, type = '', ms = 3200, action = null) {
  let root = $('#toasts');
  if (!root) { root = document.createElement('div'); root.id = 'toasts'; document.body.appendChild(root); }
  const d = document.createElement('div');
  d.className = `toast ${type}`;
  if (type === 'err' || type === 'ok') {
    d.innerHTML = type === 'err'
      ? '<svg class="snack-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 8v4.5M12 16h.01"/></svg>'
      : '<svg class="snack-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.6 2.6L16 9.6"/></svg>';
  }
  const span = document.createElement('span');
  span.textContent = msg;
  d.appendChild(span);
  if (action) {
    const btn = document.createElement('button');
    btn.className = 'snack-action';
    btn.textContent = action.label;
    btn.onclick = () => { d.remove(); action.fn(); };
    d.appendChild(btn);
  }
  root.appendChild(d);
  const kill = () => { d.classList.add('out'); setTimeout(() => d.remove(), 200); };
  if (ms > 0) setTimeout(kill, ms);
  return d;
}

/* ------------------------------------------------------------ state */
const S = {
  path: '',
  files: [],
  view: [],
  sel: new Set(),
  anchor: -1,
  thumb: 168,
  sort: 'name',
  recursive: false,
  dirs: [],
  query: '',
  cols: 4,
  rowH: 200,
  meta: null,
  metaPath: '',
  checked: new Set(),
  settings: {},
  history: [],
  hIdx: -1,
  viewer: { idx: -1, zoom: 1, ox: 0, oy: 0 },
  cacheBust: {},
  et: { available: false, version: '', path: '', source: '', readonly: true },
  etAvailable: false,
};

/* ------------------------------------------------------------ 窗口控制（无边框） */
const MIN_W = 1040;
const MIN_H = 640;
const wapi = () => (window.pywebview && window.pywebview.api) || null;

function setMaxState(maxed) {
  document.body.classList.toggle('maxed', !!maxed);
  const btn = $('#winMax');
  if (!btn) return;
  btn.querySelector('.ico-max').hidden = !!maxed;
  btn.querySelector('.ico-restore').hidden = !maxed;
  btn.title = maxed ? '还原' : '最大化';
}

function applyChrome() {
  // macOS 一律使用系统原生标题栏（交通灯由系统绘制），不启用自绘窗口控件
  if (IS_MAC) return;
  if (document.body.classList.contains('frameless')) return;
  document.body.classList.add('frameless');
  const c = $('#winctl');
  if (c) c.hidden = false;
}

let chromeWired = false;

/* 注意：window.pywebview.api 在桥接就绪前是空对象 {}，
   必须等方法真的挂上之后再置位 chromeWired，否则永远接不上。 */
function wireWindowApi() {
  // macOS 用系统原生标题栏：最小化/最大化/关闭/八向缩放全交给系统，前端一律不接管
  if (IS_MAC) return;
  const api = wapi();
  if (!api) return;
  const has = (n) => typeof api[n] === 'function';
  if (!has('get_bounds')) return;
  if (chromeWired) return;
  chromeWired = true;

  const call = (fn, ...a) => { try { return fn(...a); } catch (e) { /* ignore */ } };

  if (has('minimize')) $('#winMin').onclick = () => call(api.minimize.bind(api));
  if (has('close')) $('#winClose').onclick = () => call(api.close.bind(api));
  if (has('toggle_maximize')) {
    $('#winMax').onclick = async () => {
      const m = await call(api.toggle_maximize.bind(api));
      setMaxState(m);
    };
    // 双击顶栏最大化（顶栏同时是 pywebview 原生拖拽区）
    $('#toolbar').addEventListener('dblclick', async (e) => {
      if (e.target.closest('button, input, select, .search, .zoom-ctl, .mini-select, .winctl')) return;
      const m = await call(api.toggle_maximize.bind(api));
      setMaxState(m);
    });
  }

  if (!has('move_to') || !has('resize_to')) return;
  const useBounds = has('set_bounds');

  const bounds = async () => {
    let b = await api.get_bounds();
    if (typeof b === 'string') {
      try { b = JSON.parse(b); } catch (_) { return null; }
    }
    return b && typeof b === 'object' ? b : null;
  };

  let rs = null;
  $$('.rz').forEach((h) => {
    h.addEventListener('mousedown', async (e) => {
      if (e.button !== 0) return;
      let b;
      try { b = await bounds(); } catch (_) { return; }
      if (!b || b.maxed) return;
      rs = {
        dir: h.dataset.rz, sx: e.screenX, sy: e.screenY,
        x: b.x, y: b.y, w: window.innerWidth, h: window.innerHeight,
        dpr: window.devicePixelRatio || 1,
        raf: 0, nw: 0, nh: 0, nx: b.x, ny: b.y,
      };
      e.preventDefault();
      e.stopPropagation();
    });
  });

  window.addEventListener('mousemove', (e) => {
    if (!rs) return;
    const dx = e.screenX - rs.sx;
    const dy = e.screenY - rs.sy;
    let nw = rs.w, nh = rs.h, nx = rs.x, ny = rs.y;
    if (rs.dir.includes('e')) nw = Math.max(MIN_W, rs.w + dx);
    if (rs.dir.includes('s')) nh = Math.max(MIN_H, rs.h + dy);
    if (rs.dir.includes('w')) { nw = Math.max(MIN_W, rs.w - dx); nx = Math.round(rs.x + (rs.w - nw) * rs.dpr); }
    if (rs.dir.includes('n')) { nh = Math.max(MIN_H, rs.h - dy); ny = Math.round(rs.y + (rs.h - nh) * rs.dpr); }
    rs.nw = nw; rs.nh = nh; rs.nx = nx; rs.ny = ny;
    if (!rs.raf) {
      rs.raf = requestAnimationFrame(() => {
        rs.raf = 0;
        if (useBounds) {
          api.set_bounds(rs.nx, rs.ny, rs.nw, rs.nh);
        } else {
          if (rs.nx !== rs.x || rs.ny !== rs.y) api.move_to(rs.nx, rs.ny);
          api.resize_to(rs.nw, rs.nh);
        }
      });
    }
  });

  window.addEventListener('mouseup', () => { rs = null; });
}

/* ============================================================ exiftool */
async function refreshExiftool() {
  try {
    S.et = await api('/api/exiftool');
  } catch (e) {
    S.et = { available: false, version: '', path: '', source: '', readonly: true };
  }
  S.etAvailable = !!S.et.available;
  return S.et;
}

/* ============================================================ 分栏宽度
   备用宽度：左栏（目录）与右栏（信息）都可以拖动改宽度。
   宽度只写在 CSS 变量 --sw / --iw 上（style.css 的 #main 网格直接消费），
   拖动过程中不落盘，松手才写 /api/settings，避免每帧一次请求。 */
const COL_MIN = { sw: 180, iw: 240 };
const COL_MAX = { sw: 440, iw: 620 };
const COL_DEF = { sw: 248, iw: 320 };
const COL_KEY = { sw: 'sidebar_width', iw: 'inspector_width' };
/* 中间画布保底宽度：两栏加起来不能把网格挤没 */
const CENTER_MIN = 320;

/* 可用上限：窗口宽 - 另一栏 - 中间保底，且不超过各自的硬上限 */
function colMax(which, other) {
  const room = (window.innerWidth || 1440) - other - CENTER_MIN;
  return Math.max(COL_MIN[which], Math.min(COL_MAX[which], room));
}

function clampCol(which, v, other) {
  const raw = Math.round(Number(v) || COL_DEF[which]);
  return Math.max(COL_MIN[which], Math.min(colMax(which, other), raw));
}

function colWidths() {
  const s = S.settings || {};
  const iw = clampCol('iw', s.inspector_width || COL_DEF.iw, COL_MIN.sw);
  const sw = clampCol('sw', s.sidebar_width || COL_DEF.sw, iw);
  return { sw, iw };
}

/* 把两栏宽度写到根节点的 CSS 变量上，网格与把手位置一起跟着变 */
function applyColWidths(w) {
  const c = w || colWidths();
  S.colW = c;
  document.documentElement.style.setProperty('--sw', c.sw + 'px');
  document.documentElement.style.setProperty('--iw', c.iw + 'px');
}

function persistColWidths(w) {
  const body = {};
  body[COL_KEY.sw] = w.sw;
  body[COL_KEY.iw] = w.iw;
  api('/api/settings', { method: 'POST', body: JSON.stringify(body) }).catch(() => {});
}

function bindColumnResizers() {
  $$('.col-rz').forEach((h) => {
    const which = h.dataset.col === 'iw' ? 'iw' : 'sw';
    h.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const startX = e.clientX;
      const start = (S.colW || colWidths())[which];
      // 左栏向右拖 = 变宽；右栏向右拖 = 变窄
      const dir = which === 'sw' ? 1 : -1;
      let cur = start;
      h.classList.add('dragging');
      document.body.classList.add('col-resizing');

      const other = which === 'sw' ? 'iw' : 'sw';
      const onMove = (ev) => {
        const base = S.colW || colWidths();
        const next = clampCol(which, start + (ev.clientX - startX) * dir, base[other]);
        if (next === cur) return;
        cur = next;
        const w = Object.assign({}, base);
        w[which] = next;
        applyColWidths(w);
      };
      const onUp = () => {
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        h.classList.remove('dragging');
        document.body.classList.remove('col-resizing');
        if (cur !== start) {
          S.settings[COL_KEY[which]] = cur;
          persistColWidths(S.colW || colWidths());
        }
        renderGrid();
      };
      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
    });

    /* 双击把手恢复默认宽度 */
    h.addEventListener('dblclick', () => {
      const w = Object.assign({}, S.colW || colWidths());
      w[which] = COL_DEF[which];
      applyColWidths(w);
      S.settings[COL_KEY[which]] = COL_DEF[which];
      persistColWidths(w);
      renderGrid();
      toast(which === 'sw' ? '目录栏宽度已恢复默认' : '信息栏宽度已恢复默认');
    });
  });
}

/* 窗口变窄时把两栏一起收回到允许范围内，否则中间画布会被挤没 */
window.addEventListener('resize', () => {
  if (!S.colW) return;
  const c = colWidths();
  if (c.sw !== S.colW.sw || c.iw !== S.colW.iw) applyColWidths(c);
});

/* ============================================================ init */
async function init() {
  try {
    S.settings = await api('/api/settings');
  } catch (e) { S.settings = {}; }
  await refreshExiftool();

  document.documentElement.dataset.theme = S.settings.theme || 'dark';
  S.thumb = S.settings.thumb_size || 168;
  $('#zoom').value = S.thumb;
  syncZoomPct();
  S.sort = S.settings.sort || 'name';
  $('#sort').value = S.sort;
  S.recursive = !!S.settings.recursive;
  setRecursiveUi();

  applyColWidths();
  bindColumnResizers();

  bindToolbar();
  bindGrid();
  bindViewer();
  bindKeys();

  await loadRoots();
  if (S.settings.last_dir) navigate(S.settings.last_dir);
  renderInspector();

  if (CHROME) applyChrome();
  if (!FRAMED) {
    if (wapi()) applyChrome();
    wireWindowApi();
    window.addEventListener('pywebviewready', () => {
      if (FRAMED) return;
      applyChrome();
      wireWindowApi();
    });
    let tries = 0;
    const timer = setInterval(() => {
      if (chromeWired || ++tries > 80) { clearInterval(timer); return; }
      if (wapi()) applyChrome();
      wireWindowApi();
    }, 250);
  }
}

/* ------------------------------------------------------------ tree */
async function loadRoots() {
  const { roots } = await api('/api/roots');
  const tree = $('#tree');
  tree.innerHTML = '';
  for (const r of roots) {
    tree.appendChild(makeNode(r.name, r.path, 0, r.kind === 'quick' ? 'quick' : 'drive', null, false));
  }
}

function iconFor(kind) {
  if (kind === 'quick') return '<svg viewBox="0 0 24 24"><path d="M4 7a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/></svg>';
  if (kind === 'drive') return '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="6" rx="2"/><rect x="3" y="13" width="18" height="6" rx="2"/><path d="M7 8h.01M7 16h.01"/></svg>';
  return '<svg viewBox="0 0 24 24"><path d="M4 7a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/></svg>';
}

const TREE_INDENT = 8;   // 第一级左内边距
const TREE_STEP = 16;     // 每一层的缩进量：正好等于一个展开箭头的宽度，子级箭头与父级图标对齐

function makeNode(label, path, depth, kind, count, expanded) {
  const wrap = document.createElement('div');
  wrap.className = 'node-wrap';

  const node = document.createElement('div');
  node.className = 'node';
  node.dataset.path = path;
  node.dataset.depth = String(depth);
  node.style.paddingLeft = `${TREE_INDENT + depth * TREE_STEP}px`;
  node.innerHTML = `
    <span class="twist"><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></span>
    <span class="ico">${iconFor(kind)}</span>
    <span class="label">${esc(label)}</span>
    ${count != null ? `<span class="count">${count}</span>` : ''}`;

  const kids = document.createElement('div');
  kids.className = 'node-kids';
  kids.hidden = true;
  // 引导线对齐本节点的展开箭头中心，子级从这条线往右展开，层级关系一眼可见
  kids.style.setProperty('--guide', `${TREE_INDENT + depth * TREE_STEP + 8}px`);

  node.addEventListener('click', (e) => {
    if (e.target.closest('.twist')) { toggleNode(node, kids, path); return; }
    navigate(path);
    if (kids.hidden) toggleNode(node, kids, path);
  });

  wrap.appendChild(node);
  wrap.appendChild(kids);
  return wrap;
}

async function toggleNode(node, kids, path) {
  if (!kids.hidden) { kids.hidden = true; node.classList.remove('open'); return; }
  kids.hidden = false;
  node.classList.add('open');
  if (kids.dataset.loaded) return;
  kids.dataset.loaded = '1';
  const depth = (parseInt(node.dataset.depth, 10) || 0) + 1;
  const pad = `${TREE_INDENT + depth * TREE_STEP}px`;
  kids.innerHTML = `<div class="node node-hint" style="padding-left:${pad}">加载中…</div>`;
  try {
    const d = await api(`/api/list?path=${encodeURIComponent(path)}&sort=name`);
    kids.innerHTML = '';
    if (!d.dirs.length) {
      kids.innerHTML = `<div class="node node-hint small" style="padding-left:${pad}">无子文件夹</div>`;
      return;
    }
    for (const sub of d.dirs) {
      kids.appendChild(makeNode(sub.name, sub.path, depth, 'folder', sub.count, false));
    }
  } catch (e) {
    kids.innerHTML = `<div class="node node-hint err" style="padding-left:${pad}">${esc(e.message)}</div>`;
  }
}

function highlightTree(path) {
  $$('.node').forEach((n) => n.classList.toggle('active', n.dataset.path === path));
}

/* ------------------------------------------------------------ navigate */
async function navigate(path, push = true) {
  if (!path) return;
  try {
    const d = await api(`/api/list?path=${encodeURIComponent(path)}&sort=${S.sort}${S.recursive ? '&recursive=1' : ''}`);
    S.path = d.path;
    S.files = d.files;
    S.dirs = d.dirs || [];
    S.sel.clear();
    S.anchor = -1;
    S.meta = null;
    S.metaPath = '';
    applyFilter();
    renderCrumbs(d);
    highlightTree(d.path);
    $('#empty').hidden = d.files.length > 0;
    $('#gridScroll').scrollTop = 0;
    if (push) {
      S.history = S.history.slice(0, S.hIdx + 1);
      S.history.push(d.path);
      S.hIdx = S.history.length - 1;
    }
    updateNav();
    updateStatus();
    updateActionBar();
    renderInspector();
    api('/api/settings', { method: 'POST', body: JSON.stringify({ last_dir: d.path }) }).catch(() => {});
  } catch (e) {
    toast(e.message, 'err');
  }
}

function renderCrumbs(d) {
  const box = $('#crumbs');
  box.innerHTML = '';
  const parts = d.path.split(/[\\/]+/).filter(Boolean);
  let acc = IS_MAC ? '/' : '';
  const nodes = [];
  if (/^[A-Za-z]:$/.test(parts[0])) {
    acc = parts[0] + SEP;
    nodes.push({ name: parts[0], path: acc });
  }
  for (let i = 0; i < parts.length; i++) {
    if (/^[A-Za-z]:$/.test(parts[i])) continue;      // 盘符已单独入栈
    acc = acc.endsWith(SEP) ? acc + parts[i] : acc + SEP + parts[i];
    nodes.push({ name: parts[i], path: acc });
  }
  if (!nodes.length) { box.textContent = d.path; return; }
  const shown = nodes.length > 5
    ? [nodes[0], { name: '…', path: nodes[nodes.length - 4].path, gap: true }, ...nodes.slice(-3)]
    : nodes;
  shown.forEach((n, i) => {
    if (i) { const s = document.createElement('span'); s.className = 'crumb-sep'; s.textContent = '›'; box.appendChild(s); }
    const b = document.createElement('button');
    b.className = 'crumb' + (i === shown.length - 1 ? ' current' : '');
    b.textContent = n.name;
    b.title = n.path;
    b.onclick = () => navigate(n.path);
    box.appendChild(b);
  });
}

function updateNav() {
  $('#btnBack').disabled = S.hIdx <= 0;
  $('#btnFwd').disabled = S.hIdx >= S.history.length - 1;
  $('#btnUp').disabled = !S.path || !parentPath(S.path);
}

/* ============================================================ grid */
function applyFilter() {
  const q = S.query.trim().toLowerCase();
  const match = (n) => !q || n.toLowerCase().includes(q);
  const dirs = S.recursive ? [] : (S.dirs || []).filter((d) => match(d.name)).map((d) => ({
    name: d.name, path: d.path, kind: 'dir', count: d.count, isDir: true,
  }));
  const files = S.files.filter((f) => match(f.name));
  S.view = dirs.concat(files);
  S.photoCount = files.length;
  S.sel.forEach((p) => { if (!files.some((f) => f.path === p)) S.sel.delete(p); });
  renderGrid(true);
  updateStatus();
}

const PAD_X = 16;
const PAD_Y = 14;

function layout() {
  const scroll = $('#gridScroll');
  const avail = scroll.clientWidth - PAD_X * 2 - 10;
  const gap = 12;
  const cw = S.thumb;
  const cols = Math.max(1, Math.floor((avail + gap) / (cw + gap)));
  const totalW = cols * cw + (cols - 1) * gap;
  S.cols = cols;
  S.cellW = cw;
  S.gapX = gap;
  S.offX = PAD_X + Math.max(0, Math.round((avail - totalW) / 2));
  S.rowH = cw + 26 + gap;
  const rows = Math.ceil(S.view.length / cols);
  $('#gridSizer').style.height = `${Math.max(0, PAD_Y + rows * S.rowH + 96)}px`;
}

/* ---------------------------------------------------- 缩略图优先级调度
   浏览器的每主机并发连接数是 6。以前每张缩略图一进 DOM 就立刻发请求，
   于是"整屏 + 上下各一行"的请求会一起挤进队列按先进先出排队：
   快速滚到下面时，上面那些已经滚走的图还在占着连接，正在看的反而排在后面。
   现在改成自己控并发、按"离视口中心的距离"排优先级：
   视口内的先加载，视口外的等视口内的全部完成后再补。 */
const THUMB_MAX_INFLIGHT = 6;
const thumbQ = new Map();       // img -> { src, prio }   排队中
const thumbActive = new Map();  // img -> { done }        正在下载

function thumbWant(img, src, prio) {
  const st = img.dataset.thumbState;
  if (st === 'done') return;
  const q = thumbQ.get(img);
  if (q) { q.prio = prio; return; }
  if (st === 'loading') return;
  thumbQ.set(img, { src, prio });
  pumpThumbs();
}

/* 放弃一张图：出队；若正在下载，则解绑回调、清空 src 真正中止请求，
   否则节点被移除后 load/error 不再触发，并发槽会永久泄漏、整个网格再也加载不出图。 */
function thumbDrop(img) {
  if (!img) return;
  thumbQ.delete(img);
  const a = thumbActive.get(img);
  if (!a) return;
  img.removeEventListener('load', a.done);
  img.removeEventListener('error', a.done);
  img.removeAttribute('src');
  img.dataset.thumbState = '';
  thumbActive.delete(img);
  pumpThumbs();
}

/* 整批放弃（切换目录时整层清空，节点不会走 thumbDrop） */
function thumbAbortAll() {
  for (const img of Array.from(thumbActive.keys())) thumbDrop(img);
  thumbQ.clear();
}

function pumpThumbs() {
  while (thumbActive.size < THUMB_MAX_INFLIGHT && thumbQ.size) {
    let best = null, bestPrio = Infinity;
    for (const [img, v] of thumbQ) {
      if (v.prio < bestPrio) { bestPrio = v.prio; best = img; }
    }
    if (!best) return;
    if (!best.isConnected) { thumbQ.delete(best); continue; }   // 已经滚出渲染范围
    const src = thumbQ.get(best).src;
    thumbQ.delete(best);
    best.dataset.thumbState = 'loading';
    const done = () => {
      best.removeEventListener('load', done);
      best.removeEventListener('error', done);
      best.dataset.thumbState = 'done';
      thumbActive.delete(best);
      pumpThumbs();
    };
    best.addEventListener('load', done);
    best.addEventListener('error', done);
    thumbActive.set(best, { done });
    best.src = src;
  }
}

const rendered = new Map();

function renderGrid(reset = false) {
  const layer = $('#gridLayer');
  if (reset) { layer.innerHTML = ''; rendered.clear(); thumbAbortAll(); }
  layout();

  const scroll = $('#gridScroll');
  const top = Math.max(0, scroll.scrollTop - PAD_Y);
  const h = scroll.clientHeight;
  const startRow = Math.max(0, Math.floor(top / S.rowH) - 1);
  const endRow = Math.ceil((top + h) / S.rowH) + 1;
  const start = startRow * S.cols;
  const end = Math.min(S.view.length, endRow * S.cols);

  // 视口真正可见的行区间，用来区分"正在看的"和"上下缓冲行的"
  const visFirst = Math.floor(top / S.rowH);
  const visLast = Math.floor(Math.max(top, top + h - 1) / S.rowH);
  const visMid = (visFirst + visLast) / 2;
  const prioOf = (row) => {
    const inView = row >= visFirst && row <= visLast;
    return (inView ? 0 : 1000) + Math.abs(row - visMid);
  };

  for (const [i, el] of rendered) {
    if (i < start || i >= end) { thumbDrop(el._thumbImg); el.remove(); rendered.delete(i); }
  }

  for (let i = start; i < end; i++) {
    const f = S.view[i];
    const r = Math.floor(i / S.cols), c = i % S.cols;
    const x = S.offX + c * (S.cellW + S.gapX);
    const y = PAD_Y + r * S.rowH;
    const prio = prioOf(r);

    const existing = rendered.get(i);
    if (existing) {
      // 仅重排位置，不重建节点，避免图片重新淡入造成闪烁
      existing.style.width = `${S.cellW}px`;
      existing.style.transform = `translate3d(${x}px, ${y}px, 0)`;
      // 滚动后重新评估优先级：滚远了就往后排，滚回来了就提前
      if (existing._thumbImg) thumbWant(existing._thumbImg, existing._thumbSrc, prio);
      continue;
    }

    const el = document.createElement('div');
    el.className = 'cell';
    el.style.width = `${S.cellW}px`;
    el.style.transform = `translate3d(${x}px, ${y}px, 0)`;
    el.dataset.idx = i;
    el.dataset.path = f.path;

    if (f.isDir) {
      el.classList.add('dir');
      el.innerHTML = `
        <div class="thumb">
          <div class="folder-ico">
            <svg viewBox="0 0 24 24"><path d="M4 7a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/></svg>
          </div>
          ${f.count ? `<span class="badge count-badge">${f.count}</span>` : ''}
        </div>
        <div class="name" title="${esc(f.name)}">${esc(f.name)}</div>`;
      el.addEventListener('dblclick', () => navigate(f.path));
      el.addEventListener('mousedown', (e) => {
        if (e.button === 0 && !e.ctrlKey && !e.shiftKey) {
          S.sel.clear(); syncSelection(); updateActionBar();
        }
      });
      layer.appendChild(el);
      rendered.set(i, el);
      continue;
    }

    const bust = S.cacheBust[f.path] || '';
    const label = (S.recursive && f.rel && f.rel !== f.name) ? f.rel : f.name;
    el.innerHTML = `
      <div class="thumb">
        <div class="ph"><svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="3"/><circle cx="12" cy="12" r="3.4"/></svg></div>
        <img alt="">
        ${f.kind === 'raw' ? '<span class="badge raw">RAW</span>' : ''}
        <span class="check"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>
      </div>
      <div class="name" title="${esc(f.path)}">${esc(label)}</div>`;
    const img = el.querySelector('img');
    img.addEventListener('load', () => { img.classList.add('loaded'); el.querySelector('.ph')?.remove(); });
    img.addEventListener('error', () => { img.style.display = 'none'; });
    // 不直接给 src：交给调度器，按视口优先级决定谁先下载
    el._thumbImg = img;
    el._thumbSrc = apiUrl('/api/thumb', `&path=${encodeURIComponent(f.path)}${bust}`);
    el.addEventListener('mousedown', onCellDown);
    el.addEventListener('dblclick', () => openViewer(i));
    // 必须先入 DOM 再排队：调度器会丢弃尚未连接（isConnected === false）的节点
    layer.appendChild(el);
    rendered.set(i, el);
    thumbWant(img, el._thumbSrc, prio);
  }

  syncSelection();
}

function syncSelection() {
  for (const [i, el] of rendered) {
    const f = S.view[i];
    el.classList.toggle('sel', !!f && S.sel.has(f.path));
  }
  updateStatus();
}

function onCellDown(e) {
  if (e.button !== 0) return;
  const el = e.currentTarget;
  const i = +el.dataset.idx;
  const f = S.view[i];
  if (!f) return;
  e.preventDefault();
  $('#gridScroll').focus();

  if (e.shiftKey && S.anchor >= 0) {
    const [a, b] = [Math.min(S.anchor, i), Math.max(S.anchor, i)];
    for (let k = a; k <= b; k++) S.sel.add(S.view[k].path);
  } else if (e.ctrlKey || e.metaKey) {
    if (S.sel.has(f.path)) S.sel.delete(f.path); else S.sel.add(f.path);
    S.anchor = i;
  } else {
    S.sel.clear();
    S.sel.add(f.path);
    S.anchor = i;
  }
  syncSelection();
  updateActionBar();
  loadMetaForSelection();
}

function firstVisibleIndex() {
  const top = Math.max(0, $('#gridScroll').scrollTop - PAD_Y);
  if (!S.rowH || !S.cols) return 0;
  return Math.floor(top / S.rowH) * S.cols;
}

/* 滑杆已填充比例（M3 slider 的 active track），纯视觉，无控制流 */
function syncZoomPct() {
  const el = $('#zoom');
  if (!el) return;
  const min = +el.min || 0;
  const max = +el.max || 100;
  const pct = ((+el.value - min) / (max - min)) * 100;
  el.style.setProperty('--pct', pct.toFixed(1) + '%');
}

function setThumb(v) {
  const next = Math.max(120, Math.min(300, Math.round(v / 4) * 4));
  if (next === S.thumb) return;
  const idx = firstVisibleIndex();
  S.thumb = next;
  $('#zoom').value = next;
  syncZoomPct();
  renderGrid();
  const scroll = $('#gridScroll');
  const row = Math.floor(Math.max(0, idx) / S.cols);
  scroll.scrollTop = Math.max(0, PAD_Y + row * S.rowH);
  renderGrid();
  api('/api/settings', { method: 'POST', body: JSON.stringify({ thumb_size: next }) }).catch(() => {});
}

/* marquee */
let mq = null;
function bindGrid() {
  const scroll = $('#gridScroll');
  scroll.addEventListener('scroll', () => {
    requestAnimationFrame(() => renderGrid());
    clearTimeout(S._scrollIdle);
    S._scrollIdle = setTimeout(() => renderGrid(), 120);
  }, { passive: true });

  scroll.addEventListener('wheel', (e) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    setThumb(S.thumb + (e.deltaY > 0 ? -16 : 16));
  }, { passive: false });

  scroll.addEventListener('mousedown', (e) => {
    if (e.target.closest('.cell') || e.button !== 0) return;
    const rect = scroll.getBoundingClientRect();
    mq = { x0: e.clientX - rect.left + scroll.scrollLeft, y0: e.clientY - rect.top + scroll.scrollTop, add: e.ctrlKey };
    if (!mq.add) { S.sel.clear(); syncSelection(); }
  });

  window.addEventListener('mousemove', (e) => {
    if (!mq) return;
    const scroll = $('#gridScroll');
    const rect = scroll.getBoundingClientRect();
    const x1 = e.clientX - rect.left + scroll.scrollLeft;
    const y1 = e.clientY - rect.top + scroll.scrollTop;
    const box = $('#marquee');
    box.hidden = false;
    box.style.left = `${Math.min(mq.x0, x1)}px`;
    box.style.top = `${Math.min(mq.y0, y1) - scroll.scrollTop}px`;
    box.style.width = `${Math.abs(x1 - mq.x0)}px`;
    box.style.height = `${Math.abs(y1 - mq.y0)}px`;

    const rx0 = Math.min(mq.x0, x1), rx1 = Math.max(mq.x0, x1);
    const ry0 = Math.min(mq.y0, y1), ry1 = Math.max(mq.y0, y1);
    const sel = new Set(mq.add ? S.sel : []);
    for (const [i, el] of rendered) {
      const f = S.view[i];
      if (!f) continue;
      const r = Math.floor(i / S.cols), c = i % S.cols;
      const cx = S.offX + c * (S.cellW + S.gapX), cy = PAD_Y + r * S.rowH;
      if (cx + S.cellW > rx0 && cx < rx1 && cy + S.rowH > ry0 && cy < ry1) sel.add(f.path);
    }
    S.sel = sel;
    syncSelection();
  });

  window.addEventListener('mouseup', () => {
    if (!mq) return;
    mq = null;
    $('#marquee').hidden = true;
    updateActionBar();
    loadMetaForSelection();
  });

  new ResizeObserver(() => {
    clearTimeout(S._roTimer);
    S._roTimer = setTimeout(() => renderGrid(), 70);
  }).observe(scroll);
}

/* ============================================================ inspector */
/* 递归开关：视觉态 + 无障碍态一起更新 */
function setRecursiveUi() {
  const btn = $('#btnRecursive');
  if (!btn) return;
  btn.classList.toggle('on', S.recursive);
  btn.setAttribute('aria-checked', S.recursive ? 'true' : 'false');
}

function tabTo(name) {
  $$('.tab').forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('#tabInfo').hidden = name !== 'info';
  $('#tabEdit').hidden = name !== 'edit';
}

async function loadMetaForSelection() {
  const paths = Array.from(S.sel);
  if (paths.length === 1) {
    const p = paths[0];
    try {
      const m = await api(`/api/meta?path=${encodeURIComponent(p)}`);
      S.meta = m;
      S.metaPath = p;
    } catch (e) { S.meta = null; }
  } else {
    S.meta = null;
    S.metaPath = '';
  }
  renderInspector();
}

function renderInspector() {
  renderInfo();
  renderEdit();
}

function renderInfo() {
  const box = $('#tabInfo');
  const n = S.sel.size;

  if (!n) {
    box.innerHTML = `<div class="multi-note">未选择照片。<br>在左侧选择文件夹，点击缩略图查看详情；按住 ${MOD} / Shift 可多选。</div>`;
    return;
  }
  if (n > 1) {
    const raw = Array.from(S.sel).filter(isRaw).length;
    box.innerHTML = `
      <div class="multi-note">已选择 <b>${n}</b> 张照片${raw ? `，其中 <b>${raw}</b> 张为 RAW（只读）` : ''}。<br>
      切换到「编辑」标签可批量修改 EXIF；底部工具条可重命名或导出。</div>
      <div class="group-title">选中文件</div>
      ${Array.from(S.sel).slice(0, 40).map((p) => `<div class="kv"><span class="v small">${esc(p.split(/[\\/]/).pop())}</span></div>`).join('')}
      ${n > 40 ? `<div class="kv"><span class="v dim">… 还有 ${n - 40} 个</span></div>` : ''}`;
    return;
  }

  const m = S.meta;
  if (!m) { box.innerHTML = '<div class="multi-note">读取中…</div>'; return; }
  const f = m.file;

  // 少数格式（如飞思 IIQ）容器里最大的内嵌图只有 640×480，缩略图和大图都只能放大，
  // 这里直说一句，免得用户以为是程序写坏了。
  let previewNote = '';
  const pvArea = (f.preview_width || 0) * (f.preview_height || 0);
  if (f.kind === 'raw' && pvArea && pvArea < f.width * f.height * 0.5) {
    previewNote = `${f.preview_width} × ${f.preview_height}（文件里只有这么大）`;
  }

  const groups = [
    ['文件', [
      ['文件名', f.name],
      ['格式', f.kind === 'raw' ? 'RAW（只读）' : 'JPG'],
      ['尺寸', f.width ? `${f.width} × ${f.height}` : '—'],
      ...(previewNote ? [['内嵌预览', previewNote]] : []),
      ['大小', fmtSize(f.size)],
      ['修改时间', fmtDate(f.mtime)],
    ]],
  ];

  const byGroup = { shoot: [], author: [] };
  for (const fd of m.fields) {
    if (fd.group === 'shoot' || fd.group === 'author') byGroup[fd.group].push([fd.label, fd.value]);
  }
  groups.push(['拍摄', byGroup.shoot]);
  groups.push(['作者与版权', byGroup.author]);

  let html = '';
  for (const [title, rows] of groups) {
    html += `<div class="group-title">${title}</div>`;
    for (const [k, v] of rows) {
      html += `<div class="kv"><span class="k">${esc(k)}</span><span class="v${v === '—' ? ' dim' : ''}">${esc(v)}</span></div>`;
    }
  }

  html += `<div class="group-title">位置</div>`;
  if (m.gps.lat != null) {
    html += `<div class="kv"><span class="k">纬度</span><span class="v">${m.gps.lat.toFixed(6)}</span></div>
             <div class="kv"><span class="k">经度</span><span class="v">${m.gps.lng.toFixed(6)}</span></div>
             <div class="kv"><span class="k">海拔</span><span class="v">${m.gps.alt != null ? m.gps.alt + ' m' : '—'}</span></div>`;
  } else {
    html += `<div class="kv"><span class="v dim">无 GPS 信息</span></div>`;
  }

  html += `<div class="gps-actions top">
    <button class="btn sm" id="btnReveal">在文件夹中显示</button>
    <button class="btn sm" id="btnOpenViewer">大图查看</button>
  </div>`;

  box.innerHTML = html;
  $('#btnReveal').onclick = () => api('/api/reveal', { method: 'POST', body: JSON.stringify({ path: f.path }) });
  $('#btnOpenViewer').onclick = () => {
    const i = S.view.findIndex((x) => x.path === f.path);
    if (i >= 0) openViewer(i);
  };
}

/* ------------------------------------------------------------ edit form */
const EDIT_GROUPS = [
  {
    title: '拍摄', fields: [
      { k: 'DateTimeOriginal', label: '拍摄时间', type: 'datetime' },
      { k: 'Make', label: '相机厂商', type: 'text', ph: '例如 SONY' },
      { k: 'Model', label: '相机型号', type: 'text', ph: '例如 ILCE-7M4' },
      { k: 'LensModel', label: '镜头', type: 'text', ph: '例如 FE 24-70mm F2.8 GM II' },
    ],
  },
  {
    title: '拍摄参数', fields: [
      { k: 'FNumber', label: '光圈 (f/)', type: 'number', step: '0.1', ph: '例如 2.8' },
      { k: 'ExposureTime', label: '快门 (秒)', type: 'number', step: '0.0001', ph: '例如 0.004' },
      { k: 'ISO', label: 'ISO', type: 'number', step: '1', ph: '例如 400' },
      { k: 'FocalLength', label: '焦距 (mm)', type: 'number', step: '1', ph: '例如 50' },
      { k: 'FocalLength35', label: '等效焦距', type: 'number', step: '1', ph: '例如 75' },
      { k: 'ExposureBias', label: '曝光补偿', type: 'number', step: '0.1', ph: '例如 -0.3' },
      { k: 'WhiteBalance', label: '白平衡', type: 'select', opts: [['', '—'], ['0', '自动'], ['1', '手动']] },
      {
        k: 'ExposureProgram', label: '曝光程序', type: 'select', opts: [['', '—'], ['0', '未定义'], ['1', '手动'], ['2', '程序自动'],
          ['3', '光圈优先'], ['4', '快门优先'], ['5', '创意程序'], ['6', '动作程序'], ['7', '人像'], ['8', '风景']]
      },
      {
        k: 'MeteringMode', label: '测光模式', type: 'select', opts: [['', '—'], ['0', '未知'], ['1', '平均测光'], ['2', '中央重点'],
          ['3', '点测光'], ['4', '多分区'], ['5', '评价测光'], ['6', '局部测光']]
      },
    ],
  },
  {
    title: '作者与版权', fields: [
      { k: 'Artist', label: '作者', type: 'text', ph: '你的名字' },
      { k: 'Copyright', label: '版权', type: 'text', ph: '© 2026 你的名字' },
      { k: 'ImageDescription', label: '说明', type: 'textarea', ph: '拍摄说明 / 关键词' },
    ],
  },
];

function renderEdit() {
  const box = $('#tabEdit');
  const n = S.sel.size;
  if (!n) {
    box.innerHTML = '<div class="multi-note">先选择照片，再勾选要修改的字段。</div>';
    return;
  }
  const rawCount = Array.from(S.sel).filter(isRaw).length;
  const jpgCount = n - rawCount;
  const single = n === 1 && S.meta && S.metaPath === Array.from(S.sel)[0];
  const edit = single ? S.meta.edit : {};

  let html = '';
  if (rawCount) {
    html += `<div class="multi-note">已选 ${n} 张，其中 <b>${rawCount}</b> 张是只读 RAW，写入时会自动跳过。</div>`;
  }
  if (!jpgCount) {
    html += '<div class="multi-note">选中的都是 RAW 文件，<b>只读</b>，仅可查看 EXIF。修改请选择 JPG。</div>';
  }

  for (const g of EDIT_GROUPS) {
    html += `<div class="group-title">${g.title}</div>`;
    for (const f of g.fields) {
      const on = S.checked.has(f.k);
      const val = single ? (edit[f.k] ?? '') : '';
      let input = '';
      if (f.type === 'select') {
        input = `<select data-k="${f.k}">${f.opts.map(([v, t]) =>
          `<option value="${v}" ${String(val) === v ? 'selected' : ''}>${t}</option>`).join('')}</select>`;
      } else if (f.type === 'textarea') {
        input = `<textarea data-k="${f.k}" placeholder="${f.ph || ''}">${esc(val)}</textarea>`;
      } else if (f.type === 'datetime') {
        const dt = val ? String(val).replace(' ', 'T') : '';
        input = `<input type="datetime-local" step="1" data-k="${f.k}" value="${esc(dt)}">`;
      } else {
        input = `<input type="${f.type}" data-k="${f.k}" step="${f.step || ''}" placeholder="${f.ph || ''}" value="${val === '' || val == null ? '' : esc(val)}">`;
      }
      html += `<div class="field ${on ? 'on' : ''}" data-field="${f.k}">
        <span class="cb"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>
        <div class="body"><label>${f.label}</label>${input}</div></div>`;
    }
  }

  html += `<div class="group-title">位置（GPS）</div>
    <div class="field ${S.checked.has('GPS') ? 'on' : ''}" data-field="GPS">
      <span class="cb"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>
      <div class="body">
        <label>经纬度</label>
        <div class="gps-row">
          <input type="number" step="0.000001" data-gps="lat" placeholder="纬度" value="${single && S.meta.gps.lat != null ? S.meta.gps.lat : ''}">
          <input type="number" step="0.000001" data-gps="lng" placeholder="经度" value="${single && S.meta.gps.lng != null ? S.meta.gps.lng : ''}">
        </div>
        <div class="gps-actions">
          <button class="btn sm" id="btnMapPick">地图选点</button>
          <button class="btn sm ghost" id="btnGpsClear">清除</button>
        </div>
      </div>
    </div>
    <div class="apply-bar">
      <button class="btn primary wide" id="btnApply">应用到 ${n} 张</button>
    </div>`;

  box.innerHTML = html;

  $$('.field', box).forEach((el) => {
    el.querySelector('.cb').onclick = () => {
      const k = el.dataset.field;
      if (S.checked.has(k)) S.checked.delete(k); else S.checked.add(k);
      el.classList.toggle('on', S.checked.has(k));
    };
  });

  $('#btnGpsClear').onclick = () => {
    box.querySelector('[data-gps="lat"]').value = '';
    box.querySelector('[data-gps="lng"]').value = '';
  };

  $('#btnMapPick').onclick = () => openMapPicker((lat, lng) => {
    box.querySelector('[data-gps="lat"]').value = lat.toFixed(6);
    box.querySelector('[data-gps="lng"]').value = lng.toFixed(6);
    S.checked.add('GPS');
    box.querySelector('[data-field="GPS"]').classList.add('on');
  });

  $('#btnApply').onclick = applyEdits;
}

async function applyEdits() {
  const box = $('#tabEdit');
  const paths = Array.from(S.sel);
  const changes = {};

  for (const k of S.checked) {
    if (k === 'GPS') continue;
    const el = box.querySelector(`[data-k="${k}"]`);
    if (!el) continue;
    let v = el.value;
    if (v === '' || v == null) continue;
    const numFields = ['FNumber', 'ExposureTime', 'ISO', 'FocalLength', 'FocalLength35', 'ExposureBias',
      'WhiteBalance', 'ExposureProgram', 'MeteringMode'];
    changes[k] = numFields.includes(k) ? Number(v) : v;
  }

  if (S.checked.has('GPS')) {
    const lat = parseFloat(box.querySelector('[data-gps="lat"]').value);
    const lng = parseFloat(box.querySelector('[data-gps="lng"]').value);
    if (Number.isNaN(lat) || Number.isNaN(lng)) {
      toast('请填写有效的经纬度，或点击「地图选点」', 'err');
      return;
    }
    if (Math.abs(lat) > 90 || Math.abs(lng) > 180) { toast('经纬度超出范围', 'err'); return; }
    changes.GPSLat = lat;
    changes.GPSLng = lng;
  }

  if (!Object.keys(changes).length) { toast('请先勾选要修改的字段', 'err'); return; }

  const btn = $('#btnApply');
  btn.disabled = true;
  btn.textContent = '写入中…';
  try {
    const r = await api('/api/write', { method: 'POST', body: JSON.stringify({ paths, changes }) });
    if (r.ok) toast(`已写入 ${r.ok} 个文件${r.failed.length ? `，${r.failed.length} 个失败` : ''}`, 'ok');
    if (r.failed.length) {
      console.warn(r.failed);
      toast(`失败：${r.failed[0].error}`, 'err', 5000);
    }
    for (const p of paths) S.cacheBust[p] = `&v=${Date.now()}`;
    S.checked.clear();
    renderGrid(true);
    await loadMetaForSelection();
    tabTo('info');
  } catch (e) {
    toast(e.message, 'err', 5000);
  } finally {
    btn.disabled = false;
    btn.textContent = `应用到 ${paths.length} 张`;
  }
}

/* ============================================================ action bar */
function updateActionBar() {
  const bar = $('#actionbar');
  const n = S.sel.size;
  bar.hidden = n === 0;
  bar.classList.toggle('show', n > 0);
  $('#abCount').textContent = `已选 ${n} 张`;
  updateStatus();
}

function updateStatus() {
  const q = S.query.trim().toLowerCase();
  const dirs = S.recursive ? 0 : (S.dirs || []).filter((d) => !q || d.name.toLowerCase().includes(q)).length;
  const n = S.photoCount ?? S.files.length;
  $('#stCount').textContent = S.path
    ? `${n} 张照片${S.recursive ? '（含子目录）' : ''}${dirs ? ` · ${dirs} 个文件夹` : ''}`
    : '未打开文件夹';
  $('#stSel').textContent = S.sel.size ? `已选 ${S.sel.size} 张` : '未选择';
  $('#stPath').textContent = S.path || '';
  $('#stPath').title = S.path || '';
}

/* ============================================================ toolbar */
function bindToolbar() {
  // 顶栏同时是窗口拖拽区：控件上的 mousedown 不冒泡到 body，避免拖窗口抢走交互
  $('#toolbar').addEventListener('mousedown', (e) => {
    if (e.target.closest('button, input, select, .search, .zoom-ctl, .mini-select, .winctl')) {
      e.stopPropagation();
    }
  });

  $('#btnBack').onclick = () => { if (S.hIdx > 0) { S.hIdx--; navigate(S.history[S.hIdx], false); } };
  $('#btnFwd').onclick = () => { if (S.hIdx < S.history.length - 1) { S.hIdx++; navigate(S.history[S.hIdx], false); } };
  $('#btnUp').onclick = () => {
    if (!S.path) return;
    const parent = parentPath(S.path);
    navigate(parent || S.path);
  };

  $('#search').addEventListener('input', debounce((e) => {
    S.query = e.target.value;
    applyFilter();
  }, 160));

  $('#sort').onchange = (e) => {
    S.sort = e.target.value;
    api('/api/settings', { method: 'POST', body: JSON.stringify({ sort: S.sort }) }).catch(() => {});
    navigate(S.path, false);
  };

  $('#zoom').oninput = (e) => setThumb(+e.target.value);

  $('#btnSettings').onclick = openSettings;
  $('#btnAddRoot').onclick = () => openDirPicker(null, (p) => navigate(p));

  $('#btnRecursive').onclick = async () => {
    S.recursive = !S.recursive;
    setRecursiveUi();
    api('/api/settings', { method: 'POST', body: JSON.stringify({ recursive: S.recursive }) }).catch(() => {});
    if (S.path) {
      const btn = $('#btnRecursive');
      btn.disabled = true;
      await navigate(S.path, false);
      btn.disabled = false;
      if (S.recursive) toast(`已包含子文件夹，共 ${S.photoCount ?? 0} 张照片`, '', 2400);
    }
  };

  $$('.tab').forEach((t) => { t.onclick = () => tabTo(t.dataset.tab); });

  $$('#actionbar .btn').forEach((b) => {
    b.onclick = () => {
      const act = b.dataset.act;
      if (act === 'edit') { tabTo('edit'); }
      else if (act === 'rename') openRename();
      else if (act === 'export') openExport();
      else if (act === 'clear') { S.sel.clear(); syncSelection(); updateActionBar(); loadMetaForSelection(); }
    };
  });
}

function bindKeys() {
  document.addEventListener('keydown', (e) => {
    const inField = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === '/' && !inField) { e.preventDefault(); $('#search').focus(); return; }
    if (e.key === 'Escape') {
      if (!$('#viewer').hidden) { closeViewer(); return; }
      if ($('#modalRoot').children.length) { closeModal(); return; }
      if (inField) { document.activeElement.blur(); return; }
      if (S.sel.size) { S.sel.clear(); syncSelection(); updateActionBar(); loadMetaForSelection(); }
      return;
    }
    if (inField) return;
    if ((e.ctrlKey || e.metaKey) && (e.key === '=' || e.key === '+')) { e.preventDefault(); setThumb(S.thumb + 16); return; }
    if ((e.ctrlKey || e.metaKey) && e.key === '-') { e.preventDefault(); setThumb(S.thumb - 16); return; }
    if ((e.ctrlKey || e.metaKey) && e.key === '0') { e.preventDefault(); setThumb(168); return; }
    if (e.altKey && e.key === 'ArrowLeft') { $('#btnBack').click(); return; }
    if (e.altKey && e.key === 'ArrowRight') { $('#btnFwd').click(); return; }
    if (e.altKey && e.key === 'ArrowUp') { $('#btnUp').click(); return; }
    if (!$('#viewer').hidden) return;

    const cols = S.cols;
    let idx = S.anchor >= 0 ? S.anchor : -1;
    if (['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
      e.preventDefault();
      if (idx < 0) idx = 0;
      else if (e.key === 'ArrowRight') idx = Math.min(S.view.length - 1, idx + 1);
      else if (e.key === 'ArrowLeft') idx = Math.max(0, idx - 1);
      else if (e.key === 'ArrowDown') idx = Math.min(S.view.length - 1, idx + cols);
      else idx = Math.max(0, idx - cols);
      const f = S.view[idx];
      if (!f) return;
      S.anchor = idx;
      if (e.shiftKey) S.sel.add(f.path);
      else { S.sel.clear(); S.sel.add(f.path); }
      scrollToIndex(idx);
      syncSelection(); updateActionBar(); loadMetaForSelection();
      return;
    }
    if (e.key === 'Enter') {
      if (idx >= 0) {
        const item = S.view[idx];
        if (item?.isDir) navigate(item.path); else openViewer(idx);
      }
      return;
    }
    if (e.key === ' ' && idx >= 0) {
      e.preventDefault();
      const p = S.view[idx].path;
      if (S.sel.has(p)) S.sel.delete(p); else S.sel.add(p);
      syncSelection(); updateActionBar(); loadMetaForSelection();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
      e.preventDefault();
      S.sel = new Set(S.view.map((f) => f.path));
      syncSelection(); updateActionBar(); loadMetaForSelection();
    }
  });
}

function scrollToIndex(i) {
  const scroll = $('#gridScroll');
  const r = Math.floor(i / S.cols);
  const y = r * S.rowH;
  if (y < scroll.scrollTop || y + S.rowH > scroll.scrollTop + scroll.clientHeight) {
    scroll.scrollTop = Math.max(0, y - scroll.clientHeight / 2);
    renderGrid();
  }
}

/* ============================================================ modal */
function modal({ title, body, foot, size = '', onClose }) {
  const root = $('#modalRoot');
  const scrim = document.createElement('div');
  scrim.className = 'scrim';
  scrim.innerHTML = `<div class="modal ${size}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
      <div class="modal-head"><h2>${esc(title)}</h2><div class="tool-spacer"></div>
        <button class="icon-btn" data-close aria-label="关闭" title="关闭"><svg viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
      </div>
      <div class="modal-body"></div>
      <div class="modal-foot"></div>
    </div>`;
  scrim.querySelector('.modal-body').innerHTML = body;
  const footEl = scrim.querySelector('.modal-foot');
  if (foot) footEl.innerHTML = foot; else footEl.remove();
  scrim.querySelector('[data-close]').onclick = () => closeModal();
  scrim.addEventListener('mousedown', (e) => { if (e.target === scrim) closeModal(); });
  root.appendChild(scrim);
  scrim._onClose = onClose;
  return scrim;
}

function closeModal() {
  const root = $('#modalRoot');
  const last = root.lastElementChild;
  if (!last) return;
  last._onClose?.();
  last.remove();
}

/* ============================================================ rename */
function openRename() {
  const paths = Array.from(S.sel);
  if (!paths.length) return;
  const m = modal({
    title: `批量重命名 · ${paths.length} 个文件`,
    size: 'wide',
    body: `
      <div class="form-row">
        <label>命名模板</label>
        <div class="token-box" id="rnTokens"></div>
        <div class="hint">点下方变量插入到末尾；点变量上的 × 可单独删除它。变量之间的普通文字可以直接编辑。</div>
      </div>
      <div class="form-row">
        <label>可用变量</label>
        <div class="var-groups" id="rnChips"></div>
      </div>
      <div class="form-grid mb">
        <div class="form-row flush"><label>起始序号</label><input type="number" id="rnStart" value="1" min="0" step="1"></div>
        <div class="form-row flush"><label>预览</label><div class="hint" id="rnSummary">—</div></div>
      </div>
      <div class="table-box">
        <table class="preview"><thead><tr><th style="width:46%">原文件名</th><th style="width:46%">新文件名</th><th style="width:8%">状态</th></tr></thead>
        <tbody id="rnBody"><tr><td colspan="3" class="muted">加载中…</td></tr></tbody></table>
      </div>`,
    foot: `<span class="hint">重命名会保留原文件内容，仅修改文件名</span>
           <div class="tool-spacer"></div>
           <button class="btn ghost" data-close>取消</button>
           <button class="btn primary" id="rnApply" disabled>应用</button>`,
  });

  let items = [];

  const VAR_GROUPS = [
    { title: '常用', items: [['{name}', '原文件名'], ['{date}', '拍摄日期'], ['{time}', '拍摄时间'], ['{seq}', '序号'], ['{seq:3}', '序号补零']] },
    { title: '时间', items: [['{yyyy}', '年'], ['{mm}', '月'], ['{dd}', '日'], ['{HH}', '时'], ['{MM}', '分'], ['{SS}', '秒']] },
    { title: '拍摄信息', items: [['{camera}', '相机型号'], ['{lens}', '镜头'], ['{iso}', 'ISO'], ['{focal}', '焦距'], ['{fnum}', '光圈'], ['{shutter}', '快门']] },
    { title: '其他', items: [['{ext}', '扩展名']] },
  ];

  const VAR_DESC = {};
  VAR_GROUPS.forEach((g) => g.items.forEach(([v, d]) => { VAR_DESC[v] = d; }));

  function parseTemplate(s) {
    const out = [];
    const re = /\{[a-zA-Z]+(?::\d+)?\}/g;
    let last = 0;
    let m;
    while ((m = re.exec(s))) {
      if (m.index > last) out.push({ t: 'x', v: s.slice(last, m.index) });
      out.push({ t: 'v', v: m[0] });
      last = m.index + m[0].length;
    }
    if (last < s.length) out.push({ t: 'x', v: s.slice(last) });
    if (!out.length || out[out.length - 1].t !== 'x') out.push({ t: 'x', v: '' });
    return out;
  }

  const tokensStr = () => parts.map((p) => p.v).join('');

  let parts = parseTemplate('{date}_{time}_{seq:3}');

  function renderTokens(focusEnd) {
    const box = $('#rnTokens');
    box.innerHTML = '';
    parts.forEach((p, i) => {
      if (p.t === 'v') {
        const el = document.createElement('span');
        el.className = 'tok';
        el.title = VAR_DESC[p.v] || '';
        const lbl = document.createElement('span');
        lbl.textContent = p.v;
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'tok-x';
        x.textContent = '×';
        x.title = '删除这个变量';
        x.onclick = (ev) => {
          ev.stopPropagation();
          parts.splice(i, 1);
          normalizeParts();
          renderTokens(false);
        };
        el.append(lbl, x);
        box.appendChild(el);
      } else {
        const inp = document.createElement('input');
        inp.className = 'tok-text';
        inp.value = p.v;
        inp.spellcheck = false;
        inp.placeholder = i === parts.length - 1 ? '输入文字…' : '';
        inp.style.width = `${Math.max(1, p.v.length)}ch`;
        inp.oninput = () => {
          parts[i].v = inp.value;
          inp.style.width = `${Math.max(1, inp.value.length)}ch`;
          refresh();
        };
        inp.onkeydown = (e) => { if (e.key === 'Enter') e.preventDefault(); };
        box.appendChild(inp);
      }
    });
    if (focusEnd) {
      const last = box.lastElementChild;
      if (last && last.tagName === 'INPUT') last.focus();
    }
    refresh();
  }

  function normalizeParts() {
    const out = [];
    for (const p of parts) {
      if (p.t === 'x' && p.v === '') continue;
      const prev = out[out.length - 1];
      if (p.t === 'x' && prev && prev.t === 'x') prev.v += p.v;
      else out.push({ t: p.t, v: p.v });
    }
    if (!out.length || out[out.length - 1].t !== 'x') out.push({ t: 'x', v: '' });
    parts = out;
  }

  function addVar(v) {
    normalizeParts();
    const last = parts[parts.length - 1];
    if (last.t === 'x' && last.v === '') parts.pop();
    parts.push({ t: 'v', v });
    parts.push({ t: 'x', v: '' });
    renderTokens(true);
  }

  const chips = $('#rnChips');
  chips.innerHTML = VAR_GROUPS.map((g) => `
    <div class="var-group">
      <span class="vg-title">${g.title}</span>
      <div class="var-chips">
        ${g.items.map(([v, d]) => `<button type="button" class="chip" data-v="${esc(v)}">
          <code>${esc(v)}</code><span>${esc(d)}</span></button>`).join('')}
      </div>
    </div>`).join('');

  chips.addEventListener('click', (e) => {
    const b = e.target.closest('.chip');
    if (b) addVar(b.dataset.v);
  });

  $('#rnTokens').addEventListener('mousedown', (e) => {
    if (e.target.id !== 'rnTokens') return;
    e.preventDefault();
    const inputs = $$('#rnTokens .tok-text');
    if (inputs.length) inputs[inputs.length - 1].focus();
  });

  const refresh = debounce(async () => {
    const t = tokensStr() || '{name}';
    const start = +$('#rnStart').value || 1;
    try {
      const r = await api('/api/rename/preview', {
        method: 'POST',
        body: JSON.stringify({ paths, template: t, start }),
      });
      items = r.items;
      const bad = items.filter((i) => i.conflict).length;
      const changed = items.filter((i) => i.changed).length;
      $('#rnBody').innerHTML = items.map((i) => `
        <tr class="${i.conflict ? 'conflict' : ''}">
          <td class="mono">${esc(i.oldName)}</td>
          <td class="mono new">${esc(i.newName)}</td>
          <td>${i.conflict ? esc(i.conflict) : (i.changed ? '改名' : '不变')}</td>
        </tr>`).join('');
      $('#rnSummary').innerHTML = bad
        ? `<span class="hint warn">${bad} 个冲突，请调整模板</span>`
        : `${changed} 个将被改名，${items.length - changed} 个不变`;
      $('#rnApply').disabled = bad > 0 || changed === 0;
    } catch (e) {
      $('#rnBody').innerHTML = `<tr><td colspan="3" class="err">${esc(e.message)}</td></tr>`;
    }
  }, 280);

  $('#rnStart').addEventListener('input', refresh);
  renderTokens(false);

  $('#rnApply').onclick = async () => {
    const btn = $('#rnApply');
    btn.disabled = true;
    btn.textContent = '处理中…';
    try {
      const job = await api('/api/rename/apply', { method: 'POST', body: JSON.stringify({ items }) });
      closeModal();
      const n = job.result?.renamed ?? 0;
      toast(`已重命名 ${n} 个文件`, 'ok', 8000, n ? {
        label: '撤销',
        fn: async () => {
          const r = await api('/api/rename/undo', { method: 'POST', body: '{}' }).catch((e) => { toast(e.message, 'err'); return null; });
          if (!r) return;
          toast(`已撤销 ${r.ok} 个重命名`, 'ok');
          await navigate(S.path, false);
        },
      } : null);
      await navigate(S.path, false);
    } catch (e) {
      toast(e.message, 'err', 5000);
      btn.disabled = false;
      btn.textContent = '应用';
    }
  };
}

/* ============================================================ job */
function runJob(jobId, title, onDone) {
  modal({
    title,
    body: `<div class="progress"><div id="jobBar" style="width:0"></div></div>
           <div class="hint mt" id="jobMsg">准备中…</div>`,
    foot: `<div class="tool-spacer"></div><button class="btn ghost" id="jobCancel">取消</button>`,
  });

  const bar = $('#jobBar');
  const msg = $('#jobMsg');
  let timer = null;
  let stopping = false;

  const poll = async () => {
    try {
      const j = await api('/api/job?id=' + encodeURIComponent(jobId));
      const pct = j.total ? Math.round((j.done / j.total) * 100) : 100;
      bar.style.width = `${pct}%`;
      msg.textContent = `${j.done} / ${j.total} · ${pct}%`;
      if (j.status !== 'running') {
        clearInterval(timer);
        if (!stopping) closeModal();
        onDone(j);
      }
    } catch (e) {
      clearInterval(timer);
      if (!stopping) { closeModal(); toast(e.message, 'err'); }
    }
  };

  $('#jobCancel').onclick = async () => {
    stopping = true;
    clearInterval(timer);
    await api('/api/job/cancel', { method: 'POST', body: JSON.stringify({ id: jobId }) }).catch(() => {});
    closeModal();
    toast('已取消', '', 2000);
  };

  timer = setInterval(poll, 260);
  poll();
}

/* ============================================================ export */
function openExport() {
  const paths = Array.from(S.sel).filter((p) => !isRaw(p));
  if (!paths.length) { toast('请选择 JPG 照片（RAW 不支持导出）', 'err'); return; }

  const m = modal({
    title: `缩放导出 · ${paths.length} 张 JPG`,
    body: `
      <div class="form-row">
        <label>输出目录</label>
        <div class="inline-row grow">
          <input type="text" id="exDir" placeholder="点击右侧按钮选择文件夹" readonly>
          <button class="btn" id="exPick">选择…</button>
        </div>
      </div>
      <div class="form-grid">
        <div class="form-row"><label>长边像素</label><input type="number" id="exEdge" value="3840" min="320" step="10">
          <div class="hint">短边按比例缩放；原图小于该值时不放大</div></div>
        <div class="form-row"><label>JPEG 质量</label><input type="number" id="exQuality" value="85" min="40" max="100" step="1">
          <div class="hint">建议 80–92</div></div>
      </div>
      <div class="group-title">保留内容</div>
      <div class="keep-grid">
        <label class="keep-item on"><span class="cb2"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>
          <input type="checkbox" id="keepParams" checked>
          <span>拍摄参数<span class="sub">机型 / 光圈 / 快门 / ISO / 焦段</span></span></label>
        <label class="keep-item on"><span class="cb2"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>
          <input type="checkbox" id="keepAuthor" checked>
          <span>作者与版权<span class="sub">作者 / 版权 / 说明</span></span></label>
        <label class="keep-item on"><span class="cb2"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>
          <input type="checkbox" id="keepGps" checked>
          <span>GPS 位置<span class="sub">经纬度 / 海拔</span></span></label>
        <label class="keep-item on"><span class="cb2"><svg viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5"/></svg></span>
          <input type="checkbox" id="keepIcc" checked>
          <span>ICC 色彩配置<span class="sub">关闭后广色域照片可能偏色</span></span></label>
      </div>`,
    foot: `<div class="tool-spacer"></div>
           <button class="btn ghost" data-close>取消</button>
           <button class="btn primary" id="exGo" disabled>开始导出</button>`,
  });

  let outDir = '';
  $$('.keep-item').forEach((item) => {
    const cb = item.querySelector('input');
    cb.addEventListener('change', () => item.classList.toggle('on', cb.checked));
  });

  $('#exPick').onclick = () => openDirPicker(S.path, (p) => {
    outDir = p;
    $('#exDir').value = p;
    $('#exGo').disabled = false;
  });

  $('#exGo').onclick = async () => {
    if (!outDir) return;
    const payload = {
      paths, outDir,
      longEdge: +$('#exEdge').value || 3840,
      quality: +$('#exQuality').value || 85,
      keep: {
        params: $('#keepParams').checked,
        author: $('#keepAuthor').checked,
        gps: $('#keepGps').checked,
        icc: $('#keepIcc').checked,
      },
    };
    closeModal();
    const job = await api('/api/export', {
      method: 'POST',
      body: JSON.stringify(payload),
    }).catch((e) => { toast(e.message, 'err', 5000); return null; });
    if (!job) return;

    runJob(job.id, '正在导出', (j) => {
      if (j.status === 'cancelled') { toast('已取消导出', '', 2500); return; }
      toast(`已导出 ${j.result?.written ?? 0} 张到 ${outDir}`, 'ok', 4200);
      if (j.errors?.length) toast(`${j.errors.length} 张失败：${j.errors[0].error}`, 'err', 5000);
    });
  };
}

/* ============================================================ dir picker */
function openDirPicker(startPath, cb) {
  let cur = startPath || '';

  const m = modal({
    title: '选择文件夹',
    size: 'wide',
    body: `<div class="dir-path" id="dpPath">—</div>
           <div class="inline-row mb">
             <button class="btn sm" id="dpUp">上一级</button>
             <button class="btn sm" id="dpHome">回到根目录</button>
           </div>
           <div class="dir-list" id="dpList"></div>`,
    foot: `<div class="tool-spacer"></div>
           <button class="btn ghost" data-close>取消</button>
           <button class="btn primary" id="dpOk">选择此文件夹</button>`,
  });

  async function go(p) {
    try {
      const d = p ? await api(`/api/list?path=${encodeURIComponent(p)}&sort=name`) : await api('/api/roots');
      cur = p || '';
      $('#dpPath').textContent = cur || '根目录（请选择一个磁盘或位置）';
      const rows = [];
      if (p && d.parent) rows.push(`<div class="dir-row" data-p="${esc(d.parent)}"><svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg><span class="grow">.. 上一级</span></div>`);
      const list = p ? d.dirs : d.roots.map((r) => ({ name: r.name, path: r.path }));
      for (const sub of list) {
        rows.push(`<div class="dir-row" data-p="${esc(sub.path)}">
          <svg viewBox="0 0 24 24"><path d="M4 7a2 2 0 0 1 2-2h3l2 2h7a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/></svg>
          <span class="grow">${esc(sub.name)}</span></div>`);
      }
      if (!rows.length) rows.push('<div class="dir-row muted">没有子文件夹</div>');
      $('#dpList').innerHTML = rows.join('');
      $$('#dpList .dir-row[data-p]').forEach((r) => { r.onclick = () => go(r.dataset.p); });
      $('#dpOk').disabled = !cur;
    } catch (e) { toast(e.message, 'err'); }
  }

  $('#dpUp').onclick = () => {
    if (!cur) return;
    go(parentPath(cur));
  };
  $('#dpHome').onclick = () => go('');
  $('#dpOk').onclick = () => { if (cur) { closeModal(); cb(cur); } };

  go(startPath || '');
}

/* ============================================================ settings */
function openSettings() {
  const s = S.settings;
  const m = modal({
    title: '设置',
    size: 'wide',
    body: `
      <div class="tabs tabs-bar">
        <button class="tab active" data-stab="general">通用</button>
        <button class="tab" data-stab="exiftool">ExifTool</button>
        <button class="tab" data-stab="map">地图</button>
        <button class="tab" data-stab="about">关于</button>
      </div>
      <div id="stGeneral">
        <div class="group-title">外观</div>
        <div class="form-row">
          <label>主题</label>
          <div class="seg" id="segTheme">
            <button data-theme="dark" class="${document.documentElement.dataset.theme === 'dark' ? 'active' : ''}">深色</button>
            <button data-theme="light" class="${document.documentElement.dataset.theme === 'light' ? 'active' : ''}">浅色</button>
          </div>
        </div>
        ${IS_MAC ? '' : `<div class="group-title">窗口</div>
        <div class="form-row">
          <label class="check-row">
            <input type="checkbox" id="stNativeFrame" ${s.native_frame ? 'checked' : ''}>
            使用 Windows 系统标题栏
          </label>
          <div class="hint">默认使用自绘标题栏（无边框窗口）。如果窗口显示异常、无法拖动或缩放，勾选此项并重启程序即可恢复系统标题栏。</div>
        </div>`}
        <div class="group-title">缩略图缓存</div>
        <div class="form-row">
          <div class="hint">缩略图会缓存在本机，加快二次打开速度。删除后首次浏览会重新生成。</div>
          <div class="inline-row mt">
            <button class="btn" id="btnClearCache">清理缓存</button>
          </div>
        </div>
        <div class="group-title">EXIF 备份</div>
        <div class="hint tight">每次写入前都会自动备份原始 EXIF，可随时回滚。</div>
        <div id="bkList" class="dir-list short"></div>
      </div>
      <div id="stExifTool" hidden>
        <div class="group-title">当前状态</div>
        <div class="kv"><span class="k">是否已调用</span><span class="v" id="stEtState">${S.etAvailable ? '已调用' : '未调用'}</span></div>
        <div class="kv"><span class="k">版本</span><span class="v small" id="stEtVer">${S.etAvailable ? esc(S.et.version || '未知') : '—'}</span></div>
        <div class="kv"><span class="k">来源</span><span class="v small" id="stEtSrc">${S.etAvailable ? esc(S.et.source || '自动查找') : '—'}</span></div>
        <div class="kv"><span class="k">路径</span><span class="v small" id="stEtFile">${S.etAvailable ? esc(S.et.path || '') : '—'}</span></div>

        <div class="group-title">这个功能做什么</div>
        <div class="hint">ExifTool 在本程序里<b>只用于读取</b>，不参与写入。它的作用是让 CR3（佳能 RAW）的 EXIF 读得更完整：内置解析器已经能读出拍摄时间、光圈、快门、ISO、焦距、镜头、作者、版权、GPS 等常用字段；接上 ExifTool 后还能补上机身序列号、镜头序列号、子秒时间、时区偏移、色彩空间等更细的标签。</div>
        <div class="hint tight"><b>所有 RAW 文件（含 CR3、ARW、NEF、DNG 等）都只读</b>，本程序不会修改它们的任何内容。要改 EXIF 请用 JPG。<b>程序不内置 ExifTool</b>（它会让安装体积多出约 27 MB），需要你自己放一份。</div>

        <div class="group-title">配置方式</div>
        <div class="form-row">
          <label>ExifTool 路径</label>
          <div class="inline-row grow">
            <input type="text" id="stEtPath" value="${esc(s.exiftool_path || '')}" placeholder="留空则自动在程序目录里查找" spellcheck="false">
            <button class="btn" id="btnEtPick">选择…</button>
            <button class="btn ghost" id="btnEtTest">检测</button>
          </div>
          <div class="hint" id="stEtStatus">${etStatusText()}</div>
        </div>
        <div class="hint tight">留空时会自动在两处找：① 程序目录下的 <code>exiftool.exe</code>；② 程序目录下的 <code>exiftool-版本号\</code> 文件夹（官方压缩包解压后的原样结构，不用改名）。也可以用上面的「选择…」直接指定。<b>程序不会去系统 PATH 里找</b>，所以机器上别的软件带的 ExifTool 不会被误用。</div>
        <div class="inline-row">
          <button class="btn ghost" id="btnEtTut">查看配置教程</button>
        </div>
        <div id="etTutBox" class="tut-box" hidden></div>
      </div>
      <div id="stMap" hidden>
        <div class="form-row">
          <label>高德地图 Key</label>
          <input type="text" id="stKey" value="${esc(s.amap_key || '')}" placeholder="32 位字符" spellcheck="false">
        </div>
        <div class="form-row">
          <label>安全密钥（securityJsCode）</label>
          <input type="text" id="stSecret" value="${esc(s.amap_secret || '')}" placeholder="32 位字符" spellcheck="false">
        </div>
        <div class="inline-row mb">
          <button class="btn" id="btnTestMap">测试连接</button>
          <button class="btn ghost" id="btnTut">查看获取教程</button>
        </div>
        <div class="map-status" id="mapStatus">未配置时仍可使用全部 EXIF 功能，仅地图选点不可用。</div>
        <div id="tutBox" class="tut-box" hidden></div>
      </div>
      <div id="stAbout" hidden>
        <div class="group-title">ExifCraft</div>
        <div class="kv"><span class="k">版本</span><span class="v">2.2.0</span></div>
        <div class="kv"><span class="k">支持</span><span class="v">JPG 读写 EXIF · RAW（含 CR3）只读</span></div>
        <div class="kv"><span class="k">ExifTool</span><span class="v small">${S.etAvailable ? '已调用 v' + esc(S.et.version || '') + '（仅用于读取 CR3）' : '未调用（可选）'}</span></div>
        <div class="kv"><span class="k">配置</span><span class="v small">${esc(CONFIG_DIR_LABEL)}</span></div>
        <div class="group-title">快捷键</div>
        <div class="kv"><span class="k">方向键</span><span class="v">移动选择</span></div>
        <div class="kv"><span class="k">Shift + 方向键</span><span class="v">扩展选择</span></div>
        <div class="kv"><span class="k">Enter</span><span class="v">大图查看</span></div>
        <div class="kv"><span class="k">Space</span><span class="v">切换选中</span></div>
        <div class="kv"><span class="k">${MOD} + A</span><span class="v">全选</span></div>
        <div class="kv"><span class="k">/</span><span class="v">搜索文件名</span></div>
        <div class="kv"><span class="k">Esc</span><span class="v">取消选择 / 关闭</span></div>
        <div class="kv"><span class="k">Alt + ← → ↑</span><span class="v">后退 / 前进 / 上一级</span></div>
        <div class="kv"><span class="k">I</span><span class="v">大图中显示 EXIF</span></div>
      </div>`,
    foot: `<div class="tool-spacer"></div>
           <button class="btn ghost" data-close>关闭</button>
           <button class="btn primary" id="btnSaveSettings">保存</button>`,
  });

  $$('[data-stab]').forEach((t) => {
    t.onclick = () => {
      $$('[data-stab]').forEach((x) => x.classList.toggle('active', x === t));
      $('#stGeneral').hidden = t.dataset.stab !== 'general';
      $('#stExifTool').hidden = t.dataset.stab !== 'exiftool';
      $('#stMap').hidden = t.dataset.stab !== 'map';
      $('#stAbout').hidden = t.dataset.stab !== 'about';
    };
  });

  $$('#segTheme button').forEach((b) => {
    b.onclick = () => {
      $$('#segTheme button').forEach((x) => x.classList.toggle('active', x === b));
      document.documentElement.dataset.theme = b.dataset.theme;
      api('/api/settings', { method: 'POST', body: JSON.stringify({ theme: b.dataset.theme }) }).catch(() => {});
    };
  });

  $('#btnClearCache').onclick = async () => {
    const r = await api('/api/cache/clear', { method: 'POST', body: '{}' });
    toast(`已清理 ${r.removed} 个缓存文件，释放 ${fmtSize(r.bytes)}`, 'ok');
  };

  loadBackups();

  const syncEtStatus = () => {
    const el = $('#stEtStatus');
    if (el) el.textContent = etStatusText();
    syncEtFields();
  };

  $('#btnEtPick').onclick = async () => {
    const api2 = wapi();
    if (!api2 || !api2.pick_file) { toast('当前环境无法弹出文件对话框，请手动填写路径', 'err'); return; }
    try {
      const p = await api2.pick_file();
      if (p) { $('#stEtPath').value = p; toast('已选择，记得点保存', 'ok'); }
    } catch (e) { toast(e.message || String(e), 'err'); }
  };

  $('#btnEtTest').onclick = async () => {
    const p = $('#stEtPath').value.trim();
    if (p) {
      await api('/api/settings', { method: 'POST', body: JSON.stringify({ exiftool_path: p }) }).catch(() => {});
      S.settings.exiftool_path = p;
    }
    await refreshExiftool();
    syncEtStatus();
    toast(S.etAvailable
      ? `已调用 ExifTool ${S.et.version || ''}（${S.et.source || '自动查找'}）`
      : '没有找到 ExifTool：CR3 仍可查看常用 EXIF，只是细节标签少一些', S.etAvailable ? 'ok' : '');
  };

  $('#btnEtTut').onclick = () => {
    const box = $('#etTutBox');
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    if (!box.dataset.loaded) {
      box.appendChild($('#tplExifToolTut').content.cloneNode(true));
      box.dataset.loaded = '1';
      $$('[data-external]', box).forEach((a2) => {
        a2.onclick = (e) => {
          e.preventDefault();
          api('/api/open-external', { method: 'POST', body: JSON.stringify({ url: a2.href }) });
        };
      });
    }
  };

  $('#btnTut').onclick = () => {
    const box = $('#tutBox');
    if (!box.hidden) { box.hidden = true; return; }
    box.hidden = false;
    if (!box.dataset.loaded) {
      box.appendChild($('#tplTutorial').content.cloneNode(true));
      box.dataset.loaded = '1';
      $$('[data-external]', box).forEach((a) => {
        a.onclick = (e) => { e.preventDefault(); api('/api/open-external', { method: 'POST', body: JSON.stringify({ url: a.href }) }); };
      });
    }
  };

  $('#btnTestMap').onclick = async () => {
    const key = $('#stKey').value.trim();
    const secret = $('#stSecret').value.trim();
    const st = $('#mapStatus');
    if (!key || !secret) { st.innerHTML = '<span class="hint warn">Key 与安全密钥都要填写</span>'; return; }
    st.textContent = '正在测试…';
    try {
      await loadAmap(key, secret);
      st.innerHTML = '<span class="hint ok">连接成功，地图可以正常加载</span>';
    } catch (e) {
      st.innerHTML = `<span class="hint warn">连接失败：${esc(e.message)}</span>`;
    }
  };

  $('#btnSaveSettings').onclick = async () => {
    S.settings = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({
        amap_key: $('#stKey').value.trim(),
        amap_secret: $('#stSecret').value.trim(),
        theme: document.documentElement.dataset.theme,
        native_frame: !!($('#stNativeFrame') && $('#stNativeFrame').checked),
        exiftool_path: $('#stEtPath').value.trim(),
      }),
    });
    await refreshExiftool();
    closeModal();
    toast('设置已保存', 'ok');
  };
}

function etStatusText() {
  if (S.etAvailable) {
    const v = S.et.version ? `v${S.et.version}` : '已就绪';
    const src = S.et.source ? `（${S.et.source}）` : '';
    return `已调用 ExifTool ${v}${src}，CR3 的 EXIF 会读得更完整`;
  }
  return '未调用 ExifTool。CR3 仍可正常查看常用 EXIF（内置解析器），只是细节标签少一些。';
}

/* 刷新 ExifTool 页上那几个只读展示行 */
function syncEtFields() {
  const set = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
  set('stEtState', S.etAvailable ? '已调用' : '未调用');
  set('stEtVer', S.etAvailable ? (S.et.version || '未知') : '—');
  set('stEtSrc', S.etAvailable ? (S.et.source || '自动查找') : '—');
  set('stEtFile', S.etAvailable ? (S.et.path || '') : '—');
}

async function loadBackups() {
  const box = $('#bkList');
  if (!box) return;
  try {
    const { items } = await api('/api/backups');
    if (!items.length) { box.innerHTML = '<div class="dir-row muted">暂无备份</div>'; return; }
    box.innerHTML = items.map((b) => `
      <div class="dir-row">
        <svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/></svg>
        <span class="grow">${new Date(b.time * 1000).toLocaleString('zh-CN')} · ${b.count} 个文件</span>
        <button class="btn sm" data-bk="${esc(b.id)}">回滚</button>
      </div>`).join('');
    $$('[data-bk]', box).forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm('确定把该批文件恢复到写入前的 EXIF 状态？')) return;
        const r = await api('/api/restore', { method: 'POST', body: JSON.stringify({ id: btn.dataset.bk }) });
        toast(`已恢复 ${r.ok} 个文件${r.failed.length ? `，${r.failed.length} 个失败` : ''}`, r.failed.length ? 'err' : 'ok');
        for (const p of S.sel) S.cacheBust[p] = `&v=${Date.now()}`;
        renderGrid(true);
      };
    });
  } catch (e) { box.innerHTML = `<div class="dir-row err">${esc(e.message)}</div>`; }
}

/* ============================================================ amap */
let amapLoading = null;
function loadAmap(key, secret) {
  if (amapLoading) return amapLoading;
  amapLoading = new Promise((resolve, reject) => {
    if (window.AMap) { resolve(window.AMap); return; }
    window._AMapSecurityConfig = { securityJsCode: secret };
    const s = document.createElement('script');
    s.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(key)}&plugin=AMap.PlaceSearch,AMap.AutoComplete,AMap.Geocoder`;
    s.onerror = () => { amapLoading = null; reject(new Error('无法加载高德地图脚本，请检查网络')); };
    s.onload = () => {
      if (window.AMap) resolve(window.AMap);
      else { amapLoading = null; reject(new Error('高德地图初始化失败，请检查 Key 与安全密钥')); }
    };
    document.head.appendChild(s);
    setTimeout(() => { if (!window.AMap) { amapLoading = null; reject(new Error('加载超时，请检查 Key / 白名单 / 网络')); } }, 12000);
  });
  return amapLoading;
}

/* 坐标转换（与后端一致） */
const X_PI = Math.PI * 3000 / 180;
function outOfChina(lng, lat) { return !(lng > 73.66 && lng < 135.05 && lat > 3.86 && lat < 53.55); }
function transformLat(x, y) {
  let r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * Math.sqrt(Math.abs(x));
  r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
  r += (20 * Math.sin(y * Math.PI) + 40 * Math.sin(y / 3 * Math.PI)) * 2 / 3;
  r += (160 * Math.sin(y / 12 * Math.PI) + 320 * Math.sin(y * Math.PI / 30)) * 2 / 3;
  return r;
}
function transformLng(x, y) {
  let r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * Math.sqrt(Math.abs(x));
  r += (20 * Math.sin(6 * x * Math.PI) + 20 * Math.sin(2 * x * Math.PI)) * 2 / 3;
  r += (20 * Math.sin(x * Math.PI) + 40 * Math.sin(x / 3 * Math.PI)) * 2 / 3;
  r += (150 * Math.sin(x / 12 * Math.PI) + 300 * Math.sin(x / 30 * Math.PI)) * 2 / 3;
  return r;
}
function wgs2gcj(lng, lat) {
  if (outOfChina(lng, lat)) return [lng, lat];
  let dLat = transformLat(lng - 105, lat - 35);
  let dLng = transformLng(lng - 105, lat - 35);
  const rad = lat / 180 * Math.PI;
  let magic = Math.sin(rad);
  magic = 1 - 0.00669342162296594323 * magic * magic;
  const sq = Math.sqrt(magic);
  dLat = (dLat * 180) / ((6378245 * (1 - 0.00669342162296594323)) / (magic * sq) * Math.PI);
  dLng = (dLng * 180) / (6378245 / sq * Math.cos(rad) * Math.PI);
  return [lng + dLng, lat + dLat];
}
function gcj2wgs(lng, lat) {
  if (outOfChina(lng, lat)) return [lng, lat];
  let wl = lng, wa = lat;
  for (let i = 0; i < 6; i++) {
    const [gl, ga] = wgs2gcj(wl, wa);
    wl += lng - gl;
    wa += lat - ga;
  }
  return [wl, wa];
}

async function openMapPicker(onPick) {
  const key = (S.settings.amap_key || '').trim();
  const secret = (S.settings.amap_secret || '').trim();
  if (!key || !secret) {
    toast('请先在「设置 → 地图」中填写高德 Key 与安全密钥', 'err', 4200);
    openSettings();
    return;
  }

  const m = modal({
    title: '地图选点',
    size: 'xwide',
    body: `<div class="map-wrap">
             <div id="amapBox"></div>
             <div class="map-search">
               <input type="text" id="mapSearch" placeholder="搜索地点，例如：上海外滩" spellcheck="false">
               <button class="btn primary" id="mapGo">搜索</button>
             </div>
             <div class="map-tip">点击地图任意位置放置标记</div>
           </div>
           <div class="map-coord">WGS-84：<b id="mcLat">—</b><b id="mcLng"></b>
             <span class="hint">（写入 EXIF 的坐标）</span></div>
           <div class="map-status" id="mapMsg">在地图上点击选择位置，或搜索地点后拖动标记。</div>`,
    foot: `<div class="tool-spacer"></div>
           <button class="btn ghost" data-close>取消</button>
           <button class="btn primary" id="mapOk" disabled>使用此位置</button>`,
  });

  let picked = null;
  try {
    const AMap = await loadAmap(key, secret);
    const map = new AMap.Map('amapBox', { zoom: 11, center: [116.397428, 39.90923], resizeEnable: true });
    const marker = new AMap.Marker({ anchor: 'bottom-center', draggable: true });
    map.add(marker);

    const setPoint = (gcjLng, gcjLat) => {
      const [wlng, wlat] = gcj2wgs(gcjLng, gcjLat);
      picked = { lat: wlat, lng: wlng };
      $('#mcLat').textContent = wlat.toFixed(6);
      $('#mcLng').textContent = ', ' + wlng.toFixed(6);
      $('#mapOk').disabled = false;
      marker.setPosition([gcjLng, gcjLat]);
      map.setCenter([gcjLng, gcjLat]);
    };

    map.on('click', (e) => setPoint(e.lnglat.getLng(), e.lnglat.getLat()));
    marker.on('dragend', (e) => setPoint(e.lnglat.getLng(), e.lnglat.getLat()));

    const doSearch = () => {
      const q = $('#mapSearch').value.trim();
      if (!q) return;
      const ps = new AMap.PlaceSearch({ pageSize: 1, extensions: 'all' });
      ps.search(q, (status, result) => {
        if (status === 'complete' && result.poiList && result.poiList.pois.length) {
          const p = result.poiList.pois[0];
          setPoint(p.location.getLng(), p.location.getLat());
          $('#mapMsg').textContent = `已定位：${p.name}${p.address ? ' · ' + p.address : ''}`;
        } else {
          $('#mapMsg').innerHTML = '<span class="hint warn">没有找到该地点，换个关键词试试</span>';
        }
      });
    };
    $('#mapGo').onclick = doSearch;
    $('#mapSearch').addEventListener('keydown', (e) => { if (e.key === 'Enter') doSearch(); });

    const cur = S.meta?.gps;
    if (cur && cur.lat != null) {
      const [gl, ga] = wgs2gcj(cur.lng, cur.lat);
      map.setZoomAndCenter(15, [gl, ga]);
      marker.setPosition([gl, ga]);
      picked = { lat: cur.lat, lng: cur.lng };
      $('#mcLat').textContent = cur.lat.toFixed(6);
      $('#mcLng').textContent = ', ' + cur.lng.toFixed(6);
      $('#mapOk').disabled = false;
      $('#mapMsg').textContent = '已定位到这张照片现有的坐标，可直接拖动标记微调。';
    }
  } catch (e) {
    $('#mapMsg').innerHTML = `<span class="hint warn">${esc(e.message)}</span><br>
      <span class="hint big">可在「设置 → 地图」查看获取 Key 的详细教程；也可以直接手动输入经纬度。</span>`;
  }

  $('#mapOk').onclick = () => {
    if (!picked) return;
    closeModal();
    onPick(picked.lat, picked.lng);
  };
}

/* ============================================================ viewer */
let vwLoading = false;

function openViewer(idx) {
  const item = S.view[idx];
  if (!item || item.isDir) return;
  const list = S.view.filter((x) => !x.isDir);
  const pos = list.findIndex((x) => x.path === item.path);
  if (pos < 0) return;
  S.viewer.list = list;
  S.viewer.idx = pos;
  S.viewer.zoom = 1;
  S.viewer.ox = 0; S.viewer.oy = 0;
  $('#viewer').hidden = false;
  loadViewerImage();
}

function loadViewerImage() {
  const f = S.viewer.list[S.viewer.idx];
  if (!f) return;
  const img = $('#vwImg');
  const loading = $('#vwLoading');
  img.classList.remove('loaded');
  loading.style.display = '';
  loading.textContent = '载入中…';
  vwLoading = true;
  $('#vwName').textContent = f.name;
  $('#vwIndex').textContent = `${S.viewer.idx + 1} / ${S.viewer.list.length}`;
  applyTransform();

  img.onload = () => {
    vwLoading = false;
    img.classList.add('loaded');
    loading.style.display = 'none';
  };
  img.onerror = () => {
    vwLoading = false;
    loading.textContent = '无法显示该图片';
  };
  img.src = apiUrl('/api/preview', `&path=${encodeURIComponent(f.path)}${S.cacheBust[f.path] || ''}`);

  if (!$('#vwExifPanel').hidden) renderViewerExif(f.path);
}

function applyTransform() {
  const img = $('#vwImg');
  const { zoom, ox, oy } = S.viewer;
  img.style.transform = `translate(${ox}px, ${oy}px) scale(${zoom})`;
}

function renderViewerExif(path) {
  const panel = $('#vwExifPanel');
  panel.innerHTML = '<div class="kv"><span class="v">读取中…</span></div>';
  api(`/api/meta?path=${encodeURIComponent(path)}`).then((m) => {
    const rows = [['文件名', m.file.name], ['尺寸', `${m.file.width}×${m.file.height}`], ['大小', fmtSize(m.file.size)]];
    for (const f of m.fields) {
      if (f.value && f.value !== '—') rows.push([f.label, f.value]);
    }
    if (m.gps.lat != null) rows.push(['坐标', `${m.gps.lat.toFixed(5)}, ${m.gps.lng.toFixed(5)}`]);
    panel.innerHTML = rows.map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`).join('');
  }).catch(() => { panel.innerHTML = '<div class="kv"><span class="v">读取失败</span></div>'; });
}

function closeViewer() {
  $('#viewer').hidden = true;
  $('#vwImg').src = '';
}

function bindViewer() {
  $('#vwClose').onclick = closeViewer;
  $('#vwPrev').onclick = () => stepViewer(-1);
  $('#vwNext').onclick = () => stepViewer(1);

  $('#vwZoomIn').onclick = () => { S.viewer.zoom = Math.min(8, S.viewer.zoom * 1.25); applyTransform(); };
  $('#vwZoomOut').onclick = () => { S.viewer.zoom = Math.max(0.2, S.viewer.zoom / 1.25); applyTransform(); };

  $('#vwExif').onclick = () => {
    const p = $('#vwExifPanel');
    p.hidden = !p.hidden;
    if (!p.hidden) renderViewerExif(S.viewer.list[S.viewer.idx].path);
  };

  const stage = $('#vwStage');
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    const z = S.viewer.zoom * (e.deltaY < 0 ? 1.12 : 1 / 1.12);
    S.viewer.zoom = Math.min(8, Math.max(0.2, z));
    applyTransform();
  }, { passive: false });

  let drag = null;
  stage.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, y: e.clientY, ox: S.viewer.ox, oy: S.viewer.oy };
    stage.classList.add('grabbing');
  });
  window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    S.viewer.ox = drag.ox + (e.clientX - drag.x);
    S.viewer.oy = drag.oy + (e.clientY - drag.y);
    applyTransform();
  });
  window.addEventListener('mouseup', () => {
    if (!drag) return;
    drag = null;
    stage.classList.remove('grabbing');
  });

  stage.addEventListener('dblclick', () => {
    S.viewer.zoom = S.viewer.zoom > 1 ? 1 : 2;
    S.viewer.ox = 0; S.viewer.oy = 0;
    applyTransform();
  });

  document.addEventListener('keydown', (e) => {
    if ($('#viewer').hidden) return;
    if (e.key === 'ArrowRight') { e.preventDefault(); stepViewer(1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); stepViewer(-1); }
    else if (e.key === 'i' || e.key === 'I') { $('#vwExif').click(); }
    else if (e.key === '+' || e.key === '=') { $('#vwZoomIn').click(); }
    else if (e.key === '-') { $('#vwZoomOut').click(); }
  });
}

function stepViewer(d) {
  const next = S.viewer.idx + d;
  if (next < 0 || next >= S.viewer.list.length) return;
  S.viewer.idx = next;
  S.viewer.zoom = 1;
  S.viewer.ox = 0; S.viewer.oy = 0;
  loadViewerImage();
}

/* ============================================================ M3 交互增强（P4）
   以下三块都是独立的附加层：不改动任何既有控制流，只是往 DOM 上叠视觉反馈。 */

/* M3 ripple —— 给声明式元素叠加按压涟漪（网格 cell 与窗口按钮不做，见方案 6.1） */
(() => {
  const SEL = '.btn,.icon-btn,.tab,.crumb,.node,.chip,.tok-x,.keep-item,.seg button,.dir-row,.snack-action';
  document.addEventListener('pointerdown', (e) => {
    const el = e.target.closest(SEL);
    if (!el) return;
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const r = el.getBoundingClientRect();
    const d = Math.max(r.width, r.height) * 2;
    const s = document.createElement('span');
    s.className = 'md-ripple';
    s.style.width = d + 'px';
    s.style.height = d + 'px';
    s.style.left = (e.clientX - r.left - d / 2) + 'px';
    s.style.top = (e.clientY - r.top - d / 2) + 'px';
    el.appendChild(s);
    s.addEventListener('animationend', () => s.remove());
  }, { passive: true });
})();

/* M3 tooltip —— 原生 title 保留作无障碍兜底，悬停时惰性迁移成 aria-label + data-tip 浮层
   （惰性迁移而不是启动时全量扫：目录树/弹窗的节点是动态生成的，且部分 title 在插入之后才赋值） */
(() => {
  let tip = null;
  let timer = 0;
  const toTip = (el) => {
    const t = el.getAttribute('title');
    if (t) { el.setAttribute('aria-label', t); el.setAttribute('data-tip', t); el.removeAttribute('title'); }
    return el.getAttribute('data-tip');
  };
  function show(el) {
    const text = el.getAttribute('data-tip');
    if (!text) return;
    if (!tip) { tip = document.createElement('div'); tip.id = 'mdTip'; document.body.appendChild(tip); }
    tip.textContent = text;
    tip.classList.add('show');
    const r = el.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    let left = r.left + r.width / 2 - tr.width / 2;
    left = Math.max(8, Math.min(left, innerWidth - tr.width - 8));
    let top = r.bottom + 8;
    if (top + tr.height > innerHeight - 8) top = r.top - tr.height - 8;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  const hide = () => { clearTimeout(timer); if (tip) tip.classList.remove('show'); };
  const pick = (e) => {
    const el = e.target.closest && e.target.closest('[data-tip],[title]');
    if (!el) return null;
    return toTip(el) ? el : null;
  };
  document.addEventListener('pointerover', (e) => {
    const el = pick(e);
    if (!el) { hide(); return; }
    clearTimeout(timer);
    timer = setTimeout(() => show(el), 420);
  }, { passive: true });
  document.addEventListener('pointerout', (e) => {
    const el = e.target.closest && e.target.closest('[data-tip]');
    if (el && !el.contains(e.relatedTarget)) hide();
  }, { passive: true });
  document.addEventListener('focusin', (e) => { const el = pick(e); if (el) show(el); });
  document.addEventListener('focusout', hide);
  document.addEventListener('pointerdown', hide, { passive: true });
  addEventListener('scroll', hide, { passive: true, capture: true });
  addEventListener('blur', hide);
})();

/* M3 top app bar —— 内容滚动后顶栏升起阴影与容器色 */
(() => {
  const bar = $('#toolbar');
  const sc = $('#gridScroll');
  if (!bar || !sc) return;
  let raf = 0;
  sc.addEventListener('scroll', () => {
    if (raf) return;
    raf = requestAnimationFrame(() => {
      raf = 0;
      bar.classList.toggle('elevated', sc.scrollTop > 2);
    });
  }, { passive: true });
})();

/* ============================================================ go */
init();
