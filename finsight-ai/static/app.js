'use strict';
// FinSight front end: vanilla JS, no build step. Every dynamic string goes through textContent (el()).

const $ = id => document.getElementById(id);
const inr = new Intl.NumberFormat('en-IN', {style: 'currency', currency: 'INR', maximumFractionDigits: 2});
const money = s => s == null ? '–' : inr.format(Number(s));
const fmtNative = (v, cur = 'INR') => v == null ? '–' : new Intl.NumberFormat(cur === 'INR' ? 'en-IN' : 'en-US', {style: 'currency', currency: cur || 'INR', maximumFractionDigits: 2}).format(Number(v));
const pct = (v, signed = true) => v == null ? '–' : `${signed && Number(v) > 0 ? '+' : ''}${Number(v).toFixed(2)}%`;
const signCls = v => v == null ? '' : Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : '';
const inrShort = v => { const n = Math.abs(v); return n >= 1e5 ? '₹' + (v / 1e5).toFixed(n >= 1e6 ? 0 : 1) + 'L' : n >= 1e3 ? '₹' + Math.round(v / 1e3) + 'k' : '₹' + v; };
const ago = iso => { const s = (Date.now() - new Date(iso)) / 1000; return s < 60 ? 'just now' : s < 3600 ? Math.round(s / 60) + 'm ago' : s < 86400 ? Math.round(s / 3600) + 'h ago' : new Date(iso).toLocaleDateString('en-IN', {day: 'numeric', month: 'short'}); };
let CATEGORIES = [];
let firebaseAuth = null;

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e[k] = v; else e.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return e;
}
function table(headers, rows) {
  return el('table', {}, el('tr', {}, headers.map(h => el('th', {class: h.num ? 'num' : null}, h.label ?? h))), rows);
}
function badge(status) {
  const map = {reconciled: ['b-good', '✓ Reconciled'], imported: ['b-good', '✓ Imported'], mismatch: ['b-bad', '✕ Mismatch'],
    needs_review: ['b-warn', '! Needs review'], unverifiable: ['b-neutral', '? Unverifiable'], unsupported_layout: ['b-bad', '✕ Unsupported'],
    duplicate: ['b-neutral', 'Duplicate'], failed: ['b-bad', '✕ Failed'], sent: ['b-good', 'Emailed'], skipped: ['b-neutral', 'Not emailed'], pending: ['b-warn', 'Email queued']};
  const [cls, label] = map[status] || ['b-neutral', status];
  return el('span', {class: 'badge ' + cls}, label);
}
// Toasts: stacked, typed (success | error | info | warn), auto-dismiss with a progress bar, closable.
function toast(msg, type = 'info', {title = null, timeout = type === 'error' ? 6000 : 3800} = {}) {
  const icons = {success: '✓', error: '!', info: 'i', warn: '!'};
  const close = () => { item.classList.add('out'); setTimeout(() => item.remove(), 200); };
  const item = el('div', {class: `toast-item t-${type}`, role: type === 'error' ? 'alert' : 'status'},
    el('span', {class: 'toast-icon', 'aria-hidden': 'true'}, icons[type] || 'i'),
    el('div', {class: 'toast-body'}, title ? el('b', {}, title) : null, msg),
    el('button', {class: 'toast-close', 'aria-label': 'Dismiss', onclick: close}, '×'),
    el('span', {class: 'toast-bar', style: `animation-duration:${timeout}ms`}));
  $('toasts').append(item);
  while ($('toasts').children.length > 4) $('toasts').firstElementChild.remove();
  setTimeout(close, timeout);
}
const toastError = e => toast(e.message || String(e), 'error', {title: 'Something went wrong'});

// Skeleton placeholders shown while data loads.
const skelLine = (w = '100%') => el('span', {class: 'skel skel-line', style: `width:${w}`});
function skeleton(id, kind = 'rows', n = 4) {
  const box = $(id); if (!box) return;
  if (kind === 'value') return box.replaceChildren(el('span', {class: 'skel skel-value'}));
  if (kind === 'chart') return box.replaceChildren(el('span', {class: 'skel skel-chart'}));
  if (kind === 'text') return box.replaceChildren(skelLine('90%'), skelLine('70%'));
  box.replaceChildren(...Array.from({length: n}, () => el('span', {class: 'skel skel-row'})));
}
const skeletonAll = (ids, kind, n) => ids.forEach(id => skeleton(id, kind, n));

async function api(url, opts = {}) {
  const headers = new Headers(opts.headers || {});
  if (firebaseAuth && firebaseAuth.currentUser) headers.set('Authorization', 'Bearer ' + await firebaseAuth.currentUser.getIdToken());
  const r = await fetch(url, {...opts, headers});
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && firebaseAuth) showSignin('Your session expired. Please sign in again.');
  if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || data));
  return data;
}
const postJSON = (url, body, method = 'POST') => api(url, {method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

// ---------- tabs (hash routing keeps the tab on refresh) ----------
const loaded = new Set();
const loaders = {overview: loadOverview, investments: loadInvestments, markets: loadMarkets, transactions: loadTransactions,
  documents: loadImports, settings: loadSettings};
function showTab(name) {
  if (name === 'assistant') { openChat(); name = 'overview'; }  // old links to the Assistant tab
  if (name === 'help') { openHelp(); name = 'overview'; history.replaceState(null, '', '#overview'); }  // link: /#help
  if (!loaders[name]) name = 'overview';
  document.querySelectorAll('.tab').forEach(t => t.setAttribute('aria-selected', t.dataset.tab === name));
  document.querySelectorAll('[data-panel]').forEach(p => p.hidden = p.dataset.panel !== name);
  if (!loaded.has(name)) { loaded.add(name); loaders[name]().catch(toastError); }
}
document.querySelectorAll('.tab').forEach(t => t.onclick = () => { location.hash = t.dataset.tab; });
window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));
function reloadAll() { loaded.clear(); showTab(location.hash.slice(1) || 'overview'); refreshNotifications(); }

// ---------- shared date range (Overview + Transactions) ----------
const range = {start: '', end: ''};
function rangeParams() { const p = new URLSearchParams(); if (range.start) p.set('start', range.start); if (range.end) p.set('end', range.end); return p; }
document.querySelectorAll('.range-form').forEach(f => {
  f.onsubmit = e => { e.preventDefault(); range.start = f.start.value; range.end = f.end.value; syncRange(); loaded.delete('overview'); loaded.delete('transactions'); showTab(location.hash.slice(1) || 'overview'); };
  f.querySelector('.range-clear').onclick = () => { range.start = range.end = ''; syncRange(); loaded.delete('overview'); loaded.delete('transactions'); showTab(location.hash.slice(1) || 'overview'); };
});
function syncRange() { document.querySelectorAll('.range-form').forEach(f => { f.start.value = range.start; f.end.value = range.end; }); }

// ---------- OVERVIEW ----------
async function loadOverview() {
  skeletonAll(['k-income', 'k-spend', 'k-rate', 'k-port'], 'value'); skeleton('monthly', 'chart');
  skeleton('categories', 'rows', 5); skeleton('recurring', 'rows', 3);
  const [s, latest] = await Promise.all([api('/api/analytics/summary?' + rangeParams()), api('/api/reviews/latest')]);
  $('k-income').textContent = money(s.income);
  $('k-spend').textContent = money(s.spending);
  $('k-rate').textContent = s.savings_rate_pct != null ? s.savings_rate_pct + '%' : '–';
  $('k-invest').textContent = s.investment_rate_pct != null ? `${s.investment_rate_pct}% of income invested` : 'No income data';
  const m = s.meta;
  $('ov-coverage').textContent = m.transactions_considered ? `Bank data ${m.data_from} → ${m.data_through}` + (m.needs_review_count ? ` · ${m.needs_review_count} transactions to review` : '') : 'No bank data yet — upload a statement in Documents';
  renderReviewCard(latest);
  await Promise.all([loadMonthly(), loadCategories(), loadRecurring(), loadPortfolioKpi()]);
}
async function loadPortfolioKpi() {
  try {
    const lp = await api('/api/investments/live');
    $('k-port').textContent = money(lp.total_market_value);
    $('k-port-hint').textContent = lp.day_change_value != null ? `${Number(lp.day_change_value) >= 0 ? '+' : ''}${money(lp.day_change_value)} today` : 'No holdings yet';
  } catch { $('k-port').textContent = '–'; $('k-port-hint').textContent = 'Market data unavailable'; }
}
function renderReviewCard(r) {
  if (!r || !r.report) { $('rv-open').hidden = true; return; }
  $('rv-headline').textContent = r.report.headline;
  $('rv-summary').textContent = r.report.summary;
  $('rv-when').textContent = 'Generated ' + ago(r.created_at);
  $('rv-open').hidden = false;
  $('rv-open').onclick = () => openReview(r);
}
async function runReview(btn) {
  btn.disabled = true; const label = btn.textContent; btn.textContent = 'Analysing your portfolio…';
  try {
    const r = await api('/api/reviews/run', {method: 'POST'});
    if (r.status !== 'ready') throw Error('Review failed: ' + (r.error || 'unknown error'));
    renderReviewCard(r); openReview(r); refreshNotifications();
    const e = r.email || {};
    toast(e.sent ? 'Your portfolio review was emailed to you.' : 'Your portfolio review is ready.' + (e.skipped ? ' Email is not set up, so it was not sent.' : ''), 'success', {title: 'Review ready'});
  } catch (e) { toastError(e); }
  finally { btn.disabled = false; btn.textContent = label; }
}
$('rv-run').onclick = e => runReview(e.currentTarget);

async function loadMonthly() {
  const {months} = await api('/api/analytics/monthly?' + rangeParams());
  const host = $('monthly'); host.replaceChildren();
  if (!months.length) { host.append(el('div', {class: 'empty'}, 'No monthly data.')); $('monthly-table').replaceChildren(); return; }
  const W = 640, H = 240, L = 48, B = 24, T = 8, NS = 'http://www.w3.org/2000/svg';
  const max = Math.max(...months.flatMap(m => [Number(m.income), Number(m.spending)]), 1);
  const raw = max / 4, mag = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 2.5, 5, 10].map(f => f * mag).find(s => s >= raw), top = Math.ceil(max / step) * step;
  const y = v => T + (H - T - B) * (1 - v / top);
  const band = (W - L) / months.length, barW = Math.max(4, Math.min(22, (band - 10) / 2));
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Monthly income and spending');
  const add = (tag, a, text) => { const n = document.createElementNS(NS, tag); for (const k in a) n.setAttribute(k, a[k]); if (text != null) n.textContent = text; svg.append(n); return n; };
  for (let v = 0; v <= top; v += step) { add('line', {x1: L, x2: W, y1: y(v), y2: y(v), class: v === 0 ? 'baseline' : 'gridline'}); add('text', {x: L - 6, y: y(v) + 4, 'text-anchor': 'end'}, inrShort(v)); }
  const tip = el('div', {class: 'tooltip', hidden: true});
  const bar = (x, v) => { const y0 = y(0), y1 = y(v), r = Math.min(4, (y0 - y1) / 2, barW / 2); return v <= 0 ? '' : `M${x},${y0}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + barW - r}Q${x + barW},${y1} ${x + barW},${y1 + r}V${y0}Z`; };
  months.forEach((m, i) => {
    const cx = L + band * i + band / 2;
    add('text', {x: cx, y: H - 6, 'text-anchor': 'middle'}, new Date(m.month + '-01').toLocaleString('en-IN', {month: 'short'}));
    const hit = add('rect', {x: L + band * i, y: T, width: band, height: H - T - B, class: 'hit', tabindex: 0, 'aria-label': `${m.month}: income ${money(m.income)}, spending ${money(m.spending)}`});
    add('path', {d: bar(cx - barW - 1, Number(m.income)), fill: 'var(--series-1)'});
    add('path', {d: bar(cx + 1, Number(m.spending)), fill: 'var(--series-2)'});
    const show = () => {
      tip.replaceChildren(el('div', {class: 't-title'}, m.month), ...[['Income', m.income, '--series-1'], ['Spending', m.spending, '--series-2'], ['Invested', m.net_invested, null]].map(([n, v, c]) =>
        el('div', {class: 't-row'}, el('span', {class: 'key', style: c ? `background:var(${c})` : 'background:transparent'}), el('b', {}, money(v)), el('span', {class: 'sub'}, n))));
      tip.hidden = false;
      const box = host.getBoundingClientRect(), sc = box.width / W;
      tip.style.left = Math.min(Math.max(cx * sc - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth) + 'px';
      tip.style.top = (y(Math.max(Number(m.income), Number(m.spending))) * sc - tip.offsetHeight - 8) + 'px';
    };
    hit.addEventListener('pointerenter', show); hit.addEventListener('focus', show);
    hit.addEventListener('pointerleave', () => tip.hidden = true); hit.addEventListener('blur', () => tip.hidden = true);
  });
  host.append(svg, tip);
  $('monthly-table').replaceChildren(table(['Month', {label: 'Income', num: 1}, {label: 'Spending', num: 1}, {label: 'Invested', num: 1}, {label: 'Net cash flow', num: 1}],
    months.map(m => el('tr', {}, el('td', {}, m.month), [m.income, m.spending, m.net_invested, m.net_cash_flow].map(v => el('td', {class: 'num'}, money(v)))))));
}
async function loadCategories() {
  const c = await api('/api/analytics/categories?' + rangeParams());
  const max = Math.max(...c.categories.map(x => Number(x.amount)), 1);
  $('categories').replaceChildren(...(c.categories.length ? c.categories.map(x => el('div', {class: 'cat-row', title: `${x.count} transactions`},
    el('span', {}, x.category), el('div', {class: 'cat-track'}, el('div', {class: 'cat-fill', style: `width:${(Number(x.amount) / max * 100).toFixed(1)}%`})),
    el('span', {class: 'num'}, `${money(x.amount)} · ${x.share_pct}%`))) : [el('div', {class: 'empty'}, 'No spending data.')]));
  const ex = c.excluded_transfers;
  $('cat-note').textContent = c.categories.length ? `Not counted as spending: ${money(ex.investment_transfers_out)} to investments, ${money(ex.internal_transfers_out)} own-account transfers.` : '';
}
async function loadRecurring() {
  const r = await api('/api/analytics/recurring');
  $('recurring').replaceChildren(r.recurring.length
    ? table(['Merchant', 'Category', {label: 'Typical', num: 1}, {label: 'Times', num: 1}, 'Last paid'], r.recurring.map(x => el('tr', {},
        el('td', {}, x.merchant), el('td', {}, x.category), el('td', {class: 'num'}, money(x.typical_amount)), el('td', {class: 'num'}, x.occurrences), el('td', {}, x.last_date))))
    : el('div', {class: 'empty'}, 'None detected.'));
}

// ---------- INVESTMENTS ----------
async function loadInvestments() {
  await Promise.all([loadLive(), loadStatements(), loadPerformance()]);
}
async function loadPerformance() {
  skeleton('perf', 'rows', 2);
  let p;
  try { p = await api('/api/investments/performance'); } catch (e) { $('perf').replaceChildren(el('div', {class: 'empty'}, e.message)); return; }
  if (!p.accounts.length) return;
  $('perf').replaceChildren(...p.accounts.map(a => el('div', {},
    el('div', {class: 'sub'}, `${a.platform} ${a.account} · P&L report ${a.report_period.start} → ${a.report_period.end}`),
    el('div', {class: 'stat-row'}, [
      ['XIRR (annualised)', a.xirr_pct != null ? pct(a.xirr_pct) : '–', signCls(a.xirr_pct)],
      ['Realised P&L', money(a.realised_pnl), signCls(a.realised_pnl)],
      ['Charges paid', money(a.charges_total), ''],
      ['Open lots value now', money(a.current_value_open_lots), '']]
      .map(([l, v, c]) => el('div', {class: 'stat'}, el('div', {class: 'l'}, l), el('div', {class: 'v ' + c}, v)))),
    a.xirr_unavailable_reason ? el('div', {class: 'sub', style: 'margin-top:8px'}, 'XIRR: ' + a.xirr_unavailable_reason)
      : el('div', {class: 'sub', style: 'margin-top:8px'}, `Since ${a.xirr_since}. ${a.note}`))));
}
async function loadLive() {
  skeleton('lp-table', 'rows', 6); skeletonAll(['lp-total', 'lp-day', 'lp-pnl', 'lp-conc'], 'value');
  let lp;
  try { lp = await api('/api/investments/live'); }
  catch (e) { $('lp-table').replaceChildren(el('div', {class: 'empty'}, 'Market data unavailable: ' + e.message)); return; }
  $('lp-source').textContent = lp.source;
  $('lp-total').textContent = money(lp.total_market_value);
  const valued = lp.positions.find(p => p.price_time);
  $('lp-time').textContent = valued ? 'Price time ' + new Date(valued.price_time).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'short'}) : '';
  const day = $('lp-day'); day.textContent = lp.day_change_value != null ? (Number(lp.day_change_value) >= 0 ? '+' : '') + money(lp.day_change_value) : '–'; day.className = 'value ' + signCls(lp.day_change_value);
  const pnl = $('lp-pnl'); pnl.textContent = lp.unrealized_pnl != null ? money(lp.unrealized_pnl) : '–'; pnl.className = 'value ' + signCls(lp.unrealized_pnl);
  $('lp-cov').textContent = lp.cost_basis_coverage;
  $('lp-conc').textContent = lp.concentration.largest_weight_pct != null ? lp.concentration.largest_weight_pct + '%' : '–';
  $('lp-npos').textContent = `${lp.concentration.positions} valued position(s)`;
  $('lp-table').replaceChildren(lp.positions.length ? table(['Holding', {label: 'Qty', num: 1}, {label: 'Price', num: 1}, {label: 'Today', num: 1}, {label: 'Value', num: 1}, 'Weight', {label: 'P&L', num: 1}, 'Trend'],
    lp.positions.map(p => el('tr', {},
      el('td', {}, el('b', {}, p.symbol || '—'), p.currency && p.currency !== 'INR' ? el('span', {class: 'badge b-info', style: 'margin-left:6px'}, p.currency) : null, el('div', {class: 'sub'}, p.name)),
      el('td', {class: 'num'}, Number(p.quantity).toLocaleString('en-IN')),
      el('td', {class: 'num'}, fmtNative(p.price, p.currency)),
      el('td', {class: 'num ' + signCls(p.day_change_pct)}, pct(p.day_change_pct)),
      el('td', {class: 'num'}, money(p.market_value), p.currency && p.currency !== 'INR' ? el('div', {class: 'sub'}, fmtNative(p.market_value_native, p.currency)) : null),
      el('td', {}, p.weight_pct != null ? el('div', {class: 'weight'}, el('div', {class: 'bar'}, el('i', {style: `width:${p.weight_pct}%`})), el('span', {class: 'sub'}, p.weight_pct + '%')) : '–'),
      el('td', {class: 'num ' + signCls(p.unrealized_pnl)}, p.unrealized_pnl != null ? `${money(p.unrealized_pnl)} (${pct(p.unrealized_pnl_pct)})` : el('span', {class: 'muted'}, 'add cost')),
      el('td', {}, p.stats && p.stats.trend ? badge({uptrend: 'Uptrend', downtrend: 'Downtrend', mixed: 'Mixed'}[p.stats.trend]) : '–'))))
    : el('div', {class: 'empty'}, 'No holdings yet. Import a holdings statement in Documents or add one below.'));
  const fx = Object.entries(lp.fx_rates || {}).filter(([, v]) => v).map(([c, v]) => `1 ${c} = ₹${v}`);
  $('lp-warnings').replaceChildren(...(fx.length ? [el('li', {}, `Converted at live rates: ${fx.join(', ')}. INR P&L on foreign stocks uses today's rate.`)] : []), ...lp.warnings.map(w => el('li', {}, w)));
}
$('lp-refresh').onclick = async () => { await loadLive(); toast('Prices refreshed from the market feed.', 'success'); };

async function loadStatements() {
  skeletonAll(['inv-holdings', 'inv-flows', 'inv-mf'], 'rows', 3);
  const [h, f, m] = await Promise.all([api('/api/investments/holdings'), api('/api/investments/cash-flows'), api('/api/investments/fund-orders')]);
  $('inv-holdings').replaceChildren(h.securities.length
    ? table(['Security', 'Platform', {label: 'Qty', num: 1}, {label: 'Reported value', num: 1}, 'As of'], h.securities.flatMap(s => s.holdings.map(x => el('tr', {},
        el('td', {}, el('b', {}, s.name), el('div', {class: 'sub'}, s.isin || '')), el('td', {}, `${x.platform} ${x.account}`),
        el('td', {class: 'num'}, Number(x.quantity).toLocaleString('en-IN')), el('td', {class: 'num'}, money(x.reported_value)),
        el('td', {}, x.as_of, x.snapshot_reconciliation !== 'reconciled' ? el('div', {}, badge('unverifiable')) : null)))))
    : el('div', {class: 'empty'}, 'No holdings statements imported.'));
  $('inv-warnings').replaceChildren(...h.warnings.map(w => el('li', {}, w)));
  $('inv-flows').replaceChildren(f.accounts.length
    ? table(['Account', {label: 'In', num: 1}, {label: 'Out', num: 1}, {label: 'Bought', num: 1}, {label: 'Sold', num: 1}, {label: 'Balance', num: 1}],
        f.accounts.map(a => el('tr', {}, el('td', {}, `${a.platform} ${a.account}`), [a.deposits, a.withdrawals, a.buy_settlements, a.sell_settlements, a.closing_balance].map(v => el('td', {class: 'num'}, money(v))))))
    : el('div', {class: 'empty'}, 'No broker fund ledgers imported.'));
  $('inv-mf').replaceChildren(m.schemes.length
    ? table(['Scheme', {label: 'Invested', num: 1}, {label: 'Redeemed', num: 1}, {label: 'Units', num: 1}], m.schemes.map(s => el('tr', {},
        el('td', {class: 'wrap'}, s.scheme), el('td', {class: 'num'}, money(s.invested)), el('td', {class: 'num'}, money(s.redeemed)), el('td', {class: 'num'}, s.net_units))))
    : el('div', {class: 'empty'}, 'No mutual fund orders imported.'));
}

$('quote-form').onsubmit = async e => {
  e.preventDefault();
  const card = $('quote-card'); card.hidden = false; card.replaceChildren(el('div', {class: 'sub'}, 'Looking up…'));
  try {
    const q = await api('/api/market/quote?q=' + encodeURIComponent(e.target.q.value.trim()));
    const r = q.return_pct || {};
    card.replaceChildren(
      el('div', {class: 'card-head'}, el('div', {}, el('h2', {}, `${q.name || q.symbol} `, el('span', {class: 'sub'}, q.symbol)),
        el('div', {class: 'sub'}, q.price_time ? 'As of ' + new Date(q.price_time).toLocaleString('en-IN') : '')),
        el('div', {style: 'text-align:right'}, el('div', {style: 'font-size:1.5rem;font-weight:700'}, money(q.price)), el('div', {class: signCls(q.day_change_pct)}, pct(q.day_change_pct) + ' today'))),
      el('div', {class: 'stat-row'}, [['1 week', pct(r['1w'])], ['1 month', pct(r['1m'])], ['3 months', pct(r['3m'])], ['1 year', pct(r['1y'])],
        ['50-day avg', money(q.sma50)], ['200-day avg', money(q.sma200)], ['Volatility (yr)', q.volatility_annual_pct != null ? q.volatility_annual_pct + '%' : '–'], ['52-week range', `${money(q.low_52w)} – ${money(q.high_52w)}`]]
        .map(([l, v]) => el('div', {class: 'stat'}, el('div', {class: 'l'}, l), el('div', {class: 'v'}, v)))),
      el('div', {class: 'sub', style: 'margin-top:10px'}, q.source));
  } catch (err) { card.replaceChildren(el('div', {class: 'sub'}, err.message)); }
};

$('holding').onsubmit = async e => {
  e.preventDefault();
  const d = Object.fromEntries(new FormData(e.target)); d.quantity = Number(d.quantity); d.avg_cost = Number(d.avg_cost);
  try { await postJSON('/api/holdings', d); $('holding-status').textContent = ''; toast(`${d.symbol.toUpperCase()} saved. P&L now uses your average cost.`, 'success', {title: 'Holding saved'}); e.target.reset(); loadLive(); }
  catch (err) { $('holding-status').textContent = err.message; }
};

// ---------- MARKETS ----------
const crore = v => v == null ? '–' : `${Number(v) > 0 ? '+' : ''}₹${Math.abs(Number(v)).toLocaleString('en-IN', {maximumFractionDigits: 0})} cr`.replace('+₹', '+₹').replace(/^₹/, Number(v) < 0 ? '−₹' : '₹');
const flowState = {view: 'daily', days: 90};
function niceStep(span) { const raw = span / 4, mag = 10 ** Math.floor(Math.log10(raw || 1)); return [1, 2, 2.5, 5, 10].map(f => f * mag).find(s => s >= raw); }
function svgBase(host, W, H, label) {
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', label);
  const add = (tag, a, text) => { const n = document.createElementNS(NS, tag); for (const k in a) n.setAttribute(k, a[k]); if (text != null) n.textContent = text; svg.append(n); return n; };
  const tip = el('div', {class: 'tooltip', hidden: true});
  host.replaceChildren(svg, tip);
  const place = (x, yTop) => { const box = host.getBoundingClientRect(), sc = box.width / W; tip.hidden = false;
    tip.style.left = Math.min(Math.max(x * sc - tip.offsetWidth / 2, 0), box.width - tip.offsetWidth) + 'px';
    tip.style.top = Math.max(yTop * sc - tip.offsetHeight - 8, 0) + 'px'; };
  return {svg, add, tip, place};
}

// Two series (FII, DII) as grouped bars around a zero baseline: one axis, colour follows the entity.
function divergingBars(host, items) {
  if (!items.length) { host.replaceChildren(el('div', {class: 'empty'}, 'No flow data yet.')); return; }
  const W = 760, H = 260, L = 58, B = 26, T = 10;
  const vals = items.flatMap(i => [i.fii, i.dii]).filter(v => v != null).map(Number);
  let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
  const step = niceStep(hi - lo); lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step || step;
  const y = v => T + (H - T - B) * (hi - v) / (hi - lo);
  const {add, tip, place} = svgBase(host, W, H, 'FII and DII net flows');
  for (let v = lo; v <= hi + 1e-9; v += step) {
    add('line', {x1: L, x2: W, y1: y(v), y2: y(v), class: Math.abs(v) < 1e-9 ? 'zero' : 'gridline'});
    add('text', {x: L - 6, y: y(v) + 4, 'text-anchor': 'end'}, (v / 1000).toFixed(Math.abs(step) < 1000 ? 1 : 0) + 'k');
  }
  const band = (W - L) / items.length, barW = Math.max(1.5, Math.min(16, (band - 3) / 2));
  const bar = (x, v) => { if (v == null || Number(v) === 0) return ''; const y0 = y(0), y1 = y(Number(v)), r = Math.min(3, Math.abs(y0 - y1) / 2, barW / 2), up = y1 < y0;
    return up ? `M${x},${y0}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + barW - r}Q${x + barW},${y1} ${x + barW},${y1 + r}V${y0}Z`
              : `M${x},${y0}V${y1 - r}Q${x},${y1} ${x + r},${y1}H${x + barW - r}Q${x + barW},${y1} ${x + barW},${y1 - r}V${y0}Z`; };
  const every = Math.ceil(items.length / 8);
  items.forEach((it, i) => {
    const cx = L + band * i + band / 2;
    if (i % every === 0) add('text', {x: cx, y: H - 8, 'text-anchor': 'middle'}, it.short);
    const hit = add('rect', {x: L + band * i, y: T, width: band, height: H - T - B, class: 'hit', tabindex: 0, 'aria-label': `${it.label}: FII ${crore(it.fii)}, DII ${crore(it.dii)}`});
    add('path', {d: bar(cx - barW - 0.5, it.fii), fill: 'var(--series-1)'});
    add('path', {d: bar(cx + 0.5, it.dii), fill: 'var(--series-2)'});
    const show = () => { tip.replaceChildren(el('div', {class: 't-title'}, it.label),
      ...[['FII / FPI', it.fii, '--series-1'], ['DII', it.dii, '--series-2']].map(([n, v, c]) => el('div', {class: 't-row'}, el('span', {class: 'key', style: `background:var(${c})`}), el('b', {}, crore(v)), el('span', {class: 'sub'}, n))),
      it.extra ? el('div', {class: 'sub', style: 'margin-top:4px'}, it.extra) : null);
      place(cx, Math.min(y(Number(it.fii || 0)), y(Number(it.dii || 0)), y(0))); };
    hit.addEventListener('pointerenter', show); hit.addEventListener('focus', show);
    hit.addEventListener('pointerleave', () => tip.hidden = true); hit.addEventListener('blur', () => tip.hidden = true);
  });
}

// Single-series area/line with a crosshair that snaps to the nearest date.
function lineChart(host, points, label) {
  if (points.length < 2) { host.replaceChildren(el('div', {class: 'empty'}, 'Not enough data.')); return; }
  const W = 760, H = 260, L = 58, B = 26, T = 10;
  const vals = points.map(p => p.close);
  let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.08 || 1; lo -= pad; hi += pad;
  const step = niceStep(hi - lo); lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
  const x = i => L + (W - L) * i / (points.length - 1), y = v => T + (H - T - B) * (hi - v) / (hi - lo);
  const {svg, add, tip, place} = svgBase(host, W, H, label);
  for (let v = lo; v <= hi + 1e-9; v += step) { add('line', {x1: L, x2: W, y1: y(v), y2: y(v), class: 'gridline'}); add('text', {x: L - 6, y: y(v) + 4, 'text-anchor': 'end'}, Math.round(v).toLocaleString('en-IN')); }
  const every = Math.ceil(points.length / 6);
  points.forEach((p, i) => { if (i % every === 0) add('text', {x: x(i), y: H - 8, 'text-anchor': 'middle'}, new Date(p.date).toLocaleDateString('en-IN', points.length > 260 ? {month: 'short', year: '2-digit'} : {day: 'numeric', month: 'short'})); });
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.close).toFixed(1)}`).join('');
  add('path', {d: `${d}L${x(points.length - 1)},${y(lo)}L${x(0)},${y(lo)}Z`, class: 'area'});
  add('path', {d, class: 'line'});
  const cross = add('line', {y1: T, y2: H - B, class: 'crosshair', visibility: 'hidden'});
  const dot = add('circle', {r: 4, fill: 'var(--series-1)', stroke: 'var(--surface)', 'stroke-width': 2, visibility: 'hidden'});
  const hit = add('rect', {x: L, y: T, width: W - L, height: H - T - B, class: 'hit', tabindex: 0, 'aria-label': `${label}: ${points.length} points`});
  const showAt = i => { const p = points[i]; cross.setAttribute('x1', x(i)); cross.setAttribute('x2', x(i)); cross.setAttribute('visibility', 'visible');
    dot.setAttribute('cx', x(i)); dot.setAttribute('cy', y(p.close)); dot.setAttribute('visibility', 'visible');
    const chg = (p.close / points[0].close - 1) * 100;
    tip.replaceChildren(el('div', {class: 't-title'}, new Date(p.date).toLocaleDateString('en-IN', {dateStyle: 'medium'})),
      el('div', {class: 't-row'}, el('span', {class: 'key', style: 'background:var(--series-1)'}), el('b', {}, p.close.toLocaleString('en-IN')), el('span', {class: 'sub ' + signCls(chg)}, pct(chg) + ' in range')));
    place(x(i), y(p.close)); };
  hit.addEventListener('pointermove', e => { const r = svg.getBoundingClientRect(), px = (e.clientX - r.left) / r.width * W; showAt(Math.max(0, Math.min(points.length - 1, Math.round((px - L) / (W - L) * (points.length - 1))))); });
  hit.addEventListener('focus', () => showAt(points.length - 1));
  const hide = () => { tip.hidden = true; cross.setAttribute('visibility', 'hidden'); dot.setAttribute('visibility', 'hidden'); };
  hit.addEventListener('pointerleave', hide); hit.addEventListener('blur', hide);
}

async function loadMarkets() { await Promise.all([loadFlows(), loadNifty()]); }
async function loadFlows() {
  skeleton('flows-chart', 'chart'); skeletonAll(['mk-fii', 'mk-dii'], 'value');
  const f = await api(`/api/markets/fii-dii?days=${flowState.days}`);
  const l = f.latest;
  for (const [id, v] of [['mk-fii', l && l.fii_net], ['mk-dii', l && l.dii_net]]) { $(id).textContent = crore(v); $(id).className = 'value ' + signCls(v); }
  $('mk-date').textContent = l ? `${new Date(l.date).toLocaleDateString('en-IN', {dateStyle: 'medium'})} · INR crore` : 'No data yet';
  const items = flowState.view === 'daily'
    ? f.daily.map(d => ({label: new Date(d.date).toLocaleDateString('en-IN', {dateStyle: 'medium'}), short: new Date(d.date).toLocaleDateString('en-IN', {day: 'numeric', month: 'short'}), fii: d.fii_net, dii: d.dii_net,
        extra: d.fii_buy != null ? `FII buy ${crore(d.fii_buy).replace('+', '')} / sell ${crore(d.fii_sell).replace('+', '')}` : null}))
    : f.monthly.map(m => ({label: new Date(m.month + '-01').toLocaleDateString('en-IN', {month: 'long', year: 'numeric'}), short: new Date(m.month + '-01').toLocaleDateString('en-IN', {month: 'short', year: '2-digit'}), fii: m.fii_net, dii: m.dii_net, extra: `${m.days} trading day(s) stored`}));
  divergingBars($('flows-chart'), items);
  $('flows-table').replaceChildren(table([flowState.view === 'daily' ? 'Date' : 'Month', {label: 'FII / FPI net', num: 1}, {label: 'DII net', num: 1}],
    [...items].reverse().map(i => el('tr', {}, el('td', {}, i.label), el('td', {class: 'num ' + signCls(i.fii)}, crore(i.fii)), el('td', {class: 'num ' + signCls(i.dii)}, crore(i.dii))))));
  $('flows-note').textContent = f.days_stored ? `${f.days_stored} trading day(s) stored since ${f.collected_since}. ${f.note}` : f.note;
  if (f.fetch && f.fetch.reason && f.fetch.reason !== 'fetched recently') toast(f.fetch.reason, 'warn', {title: 'FII/DII refresh'});
}
async function loadNifty(rangeKey = document.querySelector('#nifty-range [aria-pressed=true]').dataset.v) {
  skeleton('nifty-chart', 'chart'); skeletonAll(['mk-nifty', 'mk-nifty-1y'], 'value');
  const n = await api(`/api/markets/index?range=${rangeKey}`), s = n.stats, r = s.return_pct || {};
  lineChart($('nifty-chart'), n.points, 'NIFTY 50 closing levels');
  $('mk-nifty').textContent = Number(s.price).toLocaleString('en-IN', {maximumFractionDigits: 2});
  $('mk-nifty-day').textContent = `${pct(s.day_change_pct)} today`; $('mk-nifty-day').className = 'hint ' + signCls(s.day_change_pct);
  $('mk-nifty-1y').textContent = pct(r['1y']); $('mk-nifty-1y').className = 'value ' + signCls(r['1y']);
  $('mk-nifty-trend').textContent = s.trend ? `Trend: ${s.trend} (vs 50/200-day averages)` : '';
  $('nifty-source').textContent = `${s.source}${s.price_time ? ' · ' + new Date(s.price_time).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'short'}) : ''}`;
  $('nifty-stats').replaceChildren(...[['1 month', pct(r['1m'])], ['3 months', pct(r['3m'])], ['Volatility (yr)', s.volatility_annual_pct != null ? s.volatility_annual_pct + '%' : '–'], ['52-week range', `${Math.round(s.low_52w).toLocaleString('en-IN')} – ${Math.round(s.high_52w).toLocaleString('en-IN')}`]]
    .map(([l, v]) => el('div', {class: 'stat'}, el('div', {class: 'l'}, l), el('div', {class: 'v'}, v))));
}
function segmented(id, onChange) {
  $(id).querySelectorAll('button').forEach(b => b.onclick = () => {
    $(id).querySelectorAll('button').forEach(x => x.setAttribute('aria-pressed', x === b)); onChange(b.dataset.v);
  });
}
segmented('flow-view', v => { flowState.view = v; $('flow-days').hidden = v === 'monthly'; loadFlows().catch(toastError); });
segmented('nifty-range', v => loadNifty(v).catch(toastError));
$('flow-days').onchange = e => { flowState.days = Number(e.target.value); loadFlows().catch(toastError); };
$('flows-csv').onchange = async e => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  try {
    const r = await api('/api/markets/fii-dii/import', {method: 'POST', body: fd});
    toast(`${r.rows_read} rows read, ${r.imported} saved` + (r.errors.length ? `, ${r.errors.length} skipped` : '') + '.', r.errors.length ? 'warn' : 'success', {title: 'FII/DII history imported'});
    loadFlows();
  } catch (err) { toastError(err); } finally { e.target.value = ''; }
};

// ---------- TRANSACTIONS ----------
async function loadTransactions() {
  if (!$('transactions').querySelector('table')) skeleton('transactions', 'rows', 8);
  const p = rangeParams(); p.set('limit', '200');
  if ($('tx-search').value.trim()) p.set('search', $('tx-search').value.trim());
  if ($('tx-review').checked) p.set('needs_review', 'true');
  const r = await api('/api/transactions?' + p);
  $('tx-meta').textContent = r.total_matching ? `${r.total_matching} transactions · out ${money(r.sum_debits)} · in ${money(r.sum_credits)}` : '';
  if (!r.transactions.length) { $('transactions').replaceChildren(el('div', {class: 'empty'}, 'No transactions.')); return; }
  $('transactions').replaceChildren(table(['Date', 'Description', 'Category', {label: 'Amount', num: 1}, {label: 'Balance', num: 1}, 'Source'], r.transactions.map(t => {
    const sel = el('select', {'aria-label': 'Category for ' + t.description}, CATEGORIES.map(c => el('option', {value: c, selected: c === t.category}, c)));
    sel.onchange = async () => {
      sel.disabled = true;
      try { const res = await postJSON('/api/transactions/' + t.id, {category: sel.value, apply_to_merchant: $('tx-merchant').checked}, 'PATCH'); toast(`${res.updated} transaction(s) now in ${sel.value}` + ($('tx-merchant').checked ? ` — future ${res.merchant} transactions too` : ''), 'success', {title: 'Category updated'}); loaded.delete('overview'); }
      catch (e) { toastError(e); } finally { sel.disabled = false; }
    };
    return el('tr', {}, el('td', {}, t.date), el('td', {class: 'wrap'}, t.description),
      el('td', {}, sel, t.needs_review ? el('span', {class: 'badge b-warn', style: 'margin-left:6px'}, 'review') : null),
      el('td', {class: 'num ' + (t.type === 'credit' ? 'pos' : '')}, (t.type === 'credit' ? '+' : '−') + money(t.amount)),
      el('td', {class: 'num'}, money(t.balance_after)), el('td', {class: 'sub'}, 'p.' + t.source.page));
  })));
}
let searchTimer;
$('tx-search').oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => loadTransactions().catch(toastError), 250); };
$('tx-review').onchange = () => loadTransactions().catch(toastError);

// ---------- DOCUMENTS ----------
function importBox(head, sub, checks, warnings) {
  const issues = el('ul', {}, (checks || []).filter(c => c.status === 'fail').map(c => el('li', {}, `${c.name}: ${c.detail}`)), (warnings || []).map(w => el('li', {}, `${w.code}: ${w.message}`)));
  return el('div', {class: 'import'}, el('div', {class: 'toolbar'}, head), el('div', {class: 'sub', style: 'margin-top:4px'}, sub), issues.children.length ? issues : null);
}
async function loadImports() {
  skeleton('imports', 'rows', 3);
  const [bank, inv] = await Promise.all([api('/api/statements'), api('/api/investments/imports')]);
  const kindLabel = {holdings_snapshot: 'Holdings', funds_ledger: 'Fund ledger', mf_orders: 'MF orders', trade_report: 'P&L report', unsupported: 'Investment file'};
  const items = [
    ...bank.map(s => importBox([badge(s.reconciliation_status), s.status !== 'imported' ? badge(s.status) : null, el('b', {}, `Bank · ${s.institution || 'Unknown'} ${s.account || ''}`)],
      `${s.period_start || '?'} → ${s.period_end || '?'} · ${s.transactions_found} found · ${s.transactions_inserted} new · ${s.duplicates_skipped} duplicates`, s.checks, s.warnings)),
    ...inv.map(s => importBox([badge(s.reconciliation_status), s.status !== 'imported' ? badge(s.status) : null, el('b', {}, `${kindLabel[s.kind] || s.kind} · ${s.platform || 'Unknown'} ${s.account || ''}`)],
      `${s.as_of ? 'as of ' + s.as_of : s.period_start ? s.period_start + ' → ' + s.period_end : ''} · ${s.rows_found} rows · ${s.rows_inserted} new`, s.checks, s.warnings))];
  $('imports').replaceChildren(...(items.length ? items : [el('div', {class: 'empty'}, 'Nothing imported yet.')]));
}
// Shared by the Documents tab and onboarding: upload one file and describe the outcome.
async function uploadFile(file, category, {platform = 'auto', localOnly = false} = {}) {
  const fd = new FormData();
  fd.append('file', file); fd.append('category', category); fd.append('platform', platform);
  if (localOnly) fd.append('local_only', 'true');
  return api('/api/upload', {method: 'POST', body: fd});
}
const KIND = {holdings_snapshot: 'Holdings', funds_ledger: 'Fund ledger', mf_orders: 'MF orders', trade_report: 'P&L report'};
function describeUpload(d) {
  const s = d.structured;
  const where = d.indexed_in_rag === false ? 'kept local' : d.chunks ? `indexed for chat` : 'already indexed';
  if (!s) return {ok: true, status: 'imported', text: `Saved · ${where}`};
  if (s.status === 'failed' || s.status === 'unsupported_layout') return {ok: false, status: s.status, text: 'Layout not recognised for structured import' + (d.indexed_in_rag ? ' (still searchable in chat)' : '')};
  if (s.kind) return {ok: true, status: s.reconciliation_status, text: `${KIND[s.kind] || s.kind}${s.platform ? ' · ' + s.platform : ''} · ${s.rows_found} rows (${s.rows_inserted} new) · ${where}`};
  return {ok: true, status: s.reconciliation_status, text: `Bank · ${s.institution || ''} ${s.account || ''} · ${s.transactions_found} transactions (${s.transactions_inserted} new) · ${where}`};
}
function fileRow(name) {
  const detail = el('div', {class: 'detail'}, 'Uploading…'), state = el('span', {class: 'badge b-neutral'}, '…');
  const row = el('div', {class: 'ob-file'}, el('div', {style: 'min-width:0'}, el('div', {class: 'name'}, name), detail), state);
  return {row, done(d) { const r = describeUpload(d); detail.textContent = (d.duplicate ? 'Already uploaded · ' : '') + r.text; state.replaceWith(badge(r.status)); return r; },
    fail(msg) { detail.textContent = msg; state.replaceWith(badge('failed')); }};
}

$('u-cat').onchange = () => { $('u-platform-wrap').hidden = $('u-cat').value !== 'investment'; };
$('upload').onsubmit = async e => {
  e.preventDefault();
  const f = e.target, btn = f.querySelector('button'), files = [...f.file.files];
  btn.disabled = true; $('upload-status').replaceChildren();
  for (const file of files) {
    const r = fileRow(file.name); $('upload-status').append(r.row);
    try { r.done(await uploadFile(file, f.category.value, {platform: f.platform.value, localOnly: f.local_only.checked})); }
    catch (err) { r.fail(err.message); }
  }
  f.reset(); $('u-platform-wrap').hidden = true; btn.disabled = false;
  const ok = $('upload-status').querySelectorAll('.badge:not(.b-bad)').length, bad = files.length - ok;
  toast(bad ? `${ok} imported, ${bad} need attention — see details below.` : `${ok} file(s) imported. Your dashboard is updated.`, bad ? 'warn' : 'success', {title: 'Upload finished'});
  loaded.clear(); loaded.add('documents'); loadImports();
};

// ---------- FLOATING ASSISTANT ----------
const chatHistory = [];  // recent turns, sent with each question so follow-ups have context
const newConversationId = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
let conversationId = newConversationId();  // groups this conversation's turns into one LangSmith thread
function openChat() { $('chat-widget').hidden = false; $('chat-launcher').setAttribute('aria-expanded', 'true'); setTimeout(() => $('question').focus(), 50); }
function closeChat() { $('chat-widget').hidden = true; $('chat-launcher').setAttribute('aria-expanded', 'false'); $('chat-launcher').focus(); }
$('chat-launcher').onclick = () => $('chat-widget').hidden ? openChat() : closeChat();
$('cw-close').onclick = closeChat;
$('cw-expand').onclick = () => { const w = $('chat-widget'); w.classList.toggle('expanded'); $('cw-expand').textContent = w.classList.contains('expanded') ? '⤡' : '⤢'; };
$('cw-clear').onclick = () => { chatHistory.length = 0; conversationId = newConversationId(); $('messages').replaceChildren(el('div', {class: 'msg bot'}, 'New conversation. What would you like to know?')); $('suggestions').hidden = false; };
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('chat-widget').hidden && !document.querySelector('dialog[open]')) closeChat(); });
function bubble(text, me = false, extra = '') { const b = el('div', {class: 'msg ' + (me ? 'me' : 'bot') + extra}, text); $('messages').append(b); $('messages').scrollTop = $('messages').scrollHeight; return b; }
async function ask(q) {
  openChat(); $('suggestions').hidden = true; bubble(q, true);
  const result = bubble('Thinking…', false, ' thinking');
  const mode = $('mode').value;
  try {
    const d = await postJSON('/api/chat', {question: q, mode, history: mode === 'agent' ? chatHistory.slice(-12) : [], conversation_id: conversationId});
    const answer = typeof d.answer === 'string' ? d.answer : JSON.stringify(d.answer);
    result.classList.remove('thinking'); result.textContent = answer;
    if (d.tools_used && d.tools_used.length) result.append(el('div', {class: 'tools'}, 'Tools: ' + [...new Set(d.tools_used)].join(', ')));
    if (d.trace) result.append(feedbackRow(d.trace));
    chatHistory.push({role: 'user', content: q.slice(0, 4000)}, {role: 'assistant', content: answer.slice(0, 4000)});
  } catch (err) { result.classList.remove('thinking'); result.textContent = 'Error: ' + err.message; }
  $('messages').scrollTop = $('messages').scrollHeight;
}
// 👍/👎 under each answer -> LangSmith feedback on that exact run (👎 asks what went wrong, optional).
function feedbackRow(trace) {
  const send = (score, comment = null) => postJSON('/api/feedback', {run_id: trace.run_id, trace_id: trace.trace_id, score, comment});
  const note = el('span', {class: 'fb-note'});
  const up = el('button', {type: 'button', 'aria-label': 'Helpful answer', 'aria-pressed': 'false', title: 'Helpful'}, '👍');
  const down = el('button', {type: 'button', 'aria-label': 'Not helpful', 'aria-pressed': 'false', title: 'Not helpful'}, '👎');
  const row = el('div', {class: 'fb'}, up, down, note);
  const done = (btn, text) => { btn.setAttribute('aria-pressed', 'true'); up.disabled = down.disabled = true; note.textContent = text; };
  up.onclick = async () => { try { await send(1); done(up, 'Thanks!'); } catch (e) { toastError(e); } };
  down.onclick = () => {
    if (row.nextElementSibling && row.nextElementSibling.classList.contains('fb-comment')) return;
    const input = el('input', {placeholder: 'What was wrong? (optional)', maxlength: 1000, 'aria-label': 'What was wrong'});
    const form = el('form', {class: 'fb-comment'}, input, el('button', {class: 'btn sm'}, 'Send'));
    form.onsubmit = async e => {
      e.preventDefault();
      try { await send(0, input.value.trim() || null); form.remove(); done(down, 'Thanks, this helps us improve.'); } catch (err) { toastError(err); }
    };
    row.after(form); input.focus();
  };
  return row;
}
$('chat').onsubmit = e => { e.preventDefault(); const q = $('question').value.trim(); if (!q) return; $('question').value = ''; ask(q); };
$('question').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('chat').requestSubmit(); } };
document.querySelectorAll('#suggestions .chip').forEach(c => c.onclick = () => ask(c.textContent));

// ---------- FIRST-LOGIN ONBOARDING ----------
const OB_STEPS = [['Welcome to FinSight', 'Get started'], ['Add your bank statements', 'Continue'], ['Add your investment reports', 'Finish'], ["You're all set", 'Go to dashboard']];
let obStep = 1, obUploaded = 0, obUploading = 0;
function obRender() {
  document.querySelectorAll('#onboard [data-step]').forEach(s => s.hidden = Number(s.dataset.step) !== obStep);
  $('ob-title').textContent = OB_STEPS[obStep - 1][0];
  $('ob-next').textContent = OB_STEPS[obStep - 1][1];
  $('ob-step-label').textContent = obStep < 4 ? `Step ${obStep} of 3` : 'Done';
  $('ob-bar').style.width = `${(obStep - 1) / 3 * 100}%`;
  $('ob-back').hidden = obStep === 1 || obStep === 4;
  $('ob-skip').hidden = obStep === 4;
  $('ob-next').disabled = obUploading > 0;
  if (obStep === 4) {
    $('ob-summary').textContent = obUploaded ? `${obUploaded} document(s) processed. Here's what was imported:` : 'No documents added yet — you can upload them any time from the Documents tab.';
    $('ob-files-all').replaceChildren(...[...document.querySelectorAll('#ob-files-bank .ob-file, #ob-files-investment .ob-file')].map(n => n.cloneNode(true)));
  }
}
async function obUpload(files, category) {
  const list = $('ob-files-' + category);
  for (const file of files) {
    const r = fileRow(file.name); list.append(r.row); obUploading++; obRender();
    try { r.done(await uploadFile(file, category, {platform: category === 'investment' ? $('ob-platform').value : 'auto', localOnly: $('ob-local').checked})); obUploaded++; }
    catch (err) { r.fail(err.message); }
    finally { obUploading--; obRender(); }
  }
}
document.querySelectorAll('.ob-drop').forEach(zone => {
  const input = zone.querySelector('input');
  zone.onclick = () => input.click();
  input.onchange = () => { obUpload([...input.files], zone.dataset.category); input.value = ''; };
  zone.ondragover = e => { e.preventDefault(); zone.classList.add('over'); };
  zone.ondragleave = () => zone.classList.remove('over');
  zone.ondrop = e => {
    e.preventDefault(); zone.classList.remove('over');
    const ok = zone.dataset.accept.split(',');
    const files = [...e.dataTransfer.files].filter(f => ok.some(ext => f.name.toLowerCase().endsWith(ext)));
    if (files.length < e.dataTransfer.files.length) toast('Only PDF, .xlsx and .csv files are accepted.', 'warn', {title: 'Some files were skipped'});
    obUpload(files, zone.dataset.category);
  };
});
async function obFinish() {
  try { await api('/api/onboarding/complete', {method: 'POST'}); } catch {}
  $('onboard').close(); reloadAll();
  if (obUploaded) toast(`${obUploaded} document(s) imported. Explore Investments or ask the assistant.`, 'success', {title: 'Setup complete'});
}
$('ob-next').onclick = () => { if (obStep < 4) { obStep++; obRender(); } else obFinish(); };
$('ob-back').onclick = () => { obStep = Math.max(1, obStep - 1); obRender(); };
$('ob-skip').onclick = obFinish;
$('onboard').addEventListener('cancel', e => { e.preventDefault(); });  // Esc must not silently dismiss setup
function openOnboarding() {
  obStep = 1; obUploaded = 0; $('ob-files-bank').replaceChildren(); $('ob-files-investment').replaceChildren();
  obRender(); $('onboard').showModal();
}
$('st-onboard').onclick = openOnboarding;

// ---------- SETTINGS ----------
async function loadSettings() {
  skeleton('st-system', 'text');
  const s = await api('/api/settings');
  const f = $('settings-form');
  for (const k of ['email_enabled', 'weekly_review_enabled', 'price_alerts_enabled']) f[k].checked = s[k];
  f.price_move_threshold_pct.value = s.price_move_threshold_pct;
  $('st-weekly-when').textContent = s.schedule.weekly_review;
  $('st-email').textContent = s.email_recipient ? `To ${s.email_recipient}` + (s.smtp_configured ? '' : ' — SMTP not configured yet') : (s.email_unavailable_reason || 'No email address');
  $('st-system').replaceChildren(
    el('div', {}, 'Scheduler: ', s.scheduler_enabled ? badge('imported') : el('span', {class: 'badge b-warn'}, 'Off — set ENABLE_SCHEDULER=true')),
    el('div', {style: 'margin-top:6px'}, 'Email (SMTP): ', s.smtp_configured ? badge('imported') : el('span', {class: 'badge b-warn'}, 'Not configured')),
    el('div', {style: 'margin-top:6px'}, `Market data: ${s.market_data_source} · price checks ${s.schedule.price_check}`));
}
$('settings-form').onsubmit = async e => {
  e.preventDefault(); const f = e.target;
  try {
    await postJSON('/api/settings', {email_enabled: f.email_enabled.checked, weekly_review_enabled: f.weekly_review_enabled.checked,
      price_alerts_enabled: f.price_alerts_enabled.checked, price_move_threshold_pct: Number(f.price_move_threshold_pct.value)}, 'PUT');
    $('st-status').textContent = ''; toast('Your notification preferences are saved.', 'success', {title: 'Settings saved'});
  } catch (err) { toastError(err); }
};
$('st-run').onclick = e => runReview(e.currentTarget);

// ---------- NOTIFICATIONS ----------
async function refreshNotifications() {
  let n;
  try { n = await api('/api/notifications'); } catch { return; }
  $('bell-count').hidden = !n.unread; $('bell-count').textContent = n.unread > 9 ? '9+' : n.unread;
  $('notif-list').replaceChildren(...(n.items.length ? n.items.map(i => el('button', {class: 'notif' + (i.read ? '' : ' unread'), onclick: () => openNotification(i)},
    el('div', {class: 't'}, i.kind === 'portfolio_review' ? '📊' : '📈', i.title),
    el('div', {class: 'b'}, i.body),
    el('div', {class: 'when'}, ago(i.created_at), i.email_status ? ' · ' : '', i.email_status ? badge(i.email_status) : null)))
    : [el('div', {class: 'empty'}, 'No notifications yet.')]));
}
$('bell').onclick = e => { e.stopPropagation(); $('notif-panel').hidden = !$('notif-panel').hidden; };
document.addEventListener('click', e => { if (!$('notif-panel').contains(e.target)) $('notif-panel').hidden = true; });
$('read-all').onclick = async () => { await api('/api/notifications/read-all', {method: 'POST'}); refreshNotifications(); toast('All notifications marked as read.', 'info'); };
async function openNotification(n) {
  $('notif-panel').hidden = true;
  if (!n.read) api(`/api/notifications/${n.id}/read`, {method: 'POST'}).then(refreshNotifications);
  if (n.kind === 'portfolio_review' && n.payload.review_id) { try { openReview(await api('/api/reviews/' + n.payload.review_id)); } catch (e) { toastError(e); } return; }
  openModal(el('div', {}, el('div', {class: 'sub'}, ago(n.created_at)), el('h2', {style: 'margin-top:4px'}, n.title)),
    el('div', {}, el('p', {}, n.body), n.email_note ? el('p', {class: 'sub'}, 'Email: ' + n.email_note) : null,
      el('button', {class: 'btn ghost', onclick: () => { closeModal(); location.hash = 'investments'; }}, 'Open investments')));
}

// ---------- MODAL + REVIEW REPORT ----------
function openModal(title, body) { $('modal-title').replaceChildren(title); $('modal-body').replaceChildren(body); $('modal').showModal(); $('modal-body').scrollTop = 0; }
function closeModal() { $('modal').close(); }
$('modal-close').onclick = closeModal;
$('modal').addEventListener('click', e => { if (e.target === $('modal')) closeModal(); });

const STANCE = {hold: ['Hold', 'b-neutral'], watch_closely: ['Watch closely', 'b-warn'], consider_adding: ['Consider adding', 'b-good'],
  consider_trimming: ['Consider trimming', 'b-bad'], review_thesis: ['Review your thesis', 'b-info']};
const PRIORITY = {high: 'b-bad', medium: 'b-warn', low: 'b-neutral'};
function list(items) { return items && items.length ? el('ul', {class: 'sub'}, items.map(i => el('li', {style: 'margin:4px 0'}, i))) : el('p', {class: 'muted'}, 'None.'); }
function openReview(r) {
  const rep = r.report, pf = r.portfolio || {}, conc = pf.concentration || {};
  const title = el('div', {}, el('div', {class: 'sub'}, `Portfolio review · ${new Date(r.created_at).toLocaleString('en-IN', {dateStyle: 'medium', timeStyle: 'short'})}`),
    el('h2', {style: 'font-size:1.2rem;margin-top:4px'}, rep.headline));
  const body = el('div', {},
    el('section', {}, el('p', {style: 'font-size:.98rem;color:var(--text)'}, rep.summary),
      el('div', {class: 'stat-row'}, [['Market value', money(pf.total_market_value)], ['Today', pf.day_change_value != null ? money(pf.day_change_value) : '–'],
        ['Unrealized P&L', pf.unrealized_pnl != null ? money(pf.unrealized_pnl) : 'No cost basis'], ['Largest position', conc.largest_weight_pct != null ? conc.largest_weight_pct + '%' : '–']]
        .map(([l, v]) => el('div', {class: 'stat'}, el('div', {class: 'l'}, l), el('div', {class: 'v'}, v))))),
    el('section', {}, el('h3', {}, 'Market context'), el('p', {}, rep.market_context)),
    el('section', {}, el('h3', {}, 'Your holdings'), rep.holdings.map(h => el('div', {class: 'hcard'},
      el('div', {class: 'top'}, el('div', {}, el('b', {}, h.symbol), ' ', el('span', {class: 'sub'}, h.name)), el('span', {class: 'badge ' + (STANCE[h.stance] || ['', 'b-neutral'])[1]}, (STANCE[h.stance] || [h.stance])[0])),
      el('p', {}, h.reasoning),
      el('div', {class: 'two'}, el('div', {class: 'case bull'}, el('b', {}, 'Bull case'), h.bull_case), el('div', {class: 'case bear'}, el('b', {}, 'Bear case'), h.bear_case)),
      h.levels_to_watch.length ? el('div', {class: 'chips', style: 'margin-top:10px'}, h.levels_to_watch.map(l => el('span', {class: 'badge b-neutral'}, l))) : null,
      h.risk_flags.length ? el('div', {class: 'chips', style: 'margin-top:6px'}, h.risk_flags.map(l => el('span', {class: 'badge b-warn'}, '⚠ ' + l))) : null))),
    el('section', {}, el('h3', {}, 'Portfolio assessment'), el('p', {}, rep.portfolio_assessment)),
    el('section', {}, el('h3', {}, 'Things to consider'), rep.actions.map(a => el('div', {class: 'action'},
      el('span', {class: 'badge ' + (PRIORITY[a.priority] || 'b-neutral')}, a.priority),
      el('div', {}, el('b', {}, a.action), el('div', {class: 'sub', style: 'margin-top:2px'}, a.rationale), el('div', {class: 'sub', style: 'margin-top:2px'}, 'Risk: ' + a.risk))))),
    el('section', {}, el('h3', {}, 'Scenarios (not predictions)'), el('div', {class: 'scen'}, ['bull', 'base', 'bear'].map(name => {
      const s = rep.scenarios.find(x => x.name === name); if (!s) return null;
      return el('div', {class: 's'}, el('span', {class: 'badge ' + {bull: 'b-good', base: 'b-neutral', bear: 'b-bad'}[name]}, name[0].toUpperCase() + name.slice(1)),
        el('p', {}, s.description), el('div', {class: 'sub'}, el('b', {}, 'Triggers: '), s.triggers.join('; ')), el('div', {class: 'sub', style: 'margin-top:4px'}, el('b', {}, 'Impact: '), s.portfolio_impact));
    }))),
    el('section', {class: 'grid cols-2'}, el('div', {}, el('h3', {}, 'What to watch'), list(rep.what_to_watch)), el('div', {}, el('h3', {}, 'Data limitations'), list(rep.data_limitations))),
    el('div', {class: 'disclaimer'}, r.disclaimer, el('br'), 'Market data: ', r.source));
  openModal(title, body);
}

// ---------- THEME (system | light | dark; theme.js applies the saved choice before first paint) ----------
const THEME_KEY = 'finsight-theme';
const darkQuery = matchMedia('(prefers-color-scheme: dark)');
const SUN = '<svg class="theme-icon" data-for="dark" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
const MOON = '<svg class="theme-icon" data-for="light" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9z"/></svg>';
function themePref() { try { const t = localStorage.getItem(THEME_KEY); return t === 'light' || t === 'dark' ? t : 'system'; } catch { return 'system'; } }
const effectiveTheme = () => document.documentElement.dataset.theme || (darkQuery.matches ? 'dark' : 'light');
function syncThemeUi() {
  const now = effectiveTheme(), next = now === 'dark' ? 'light' : 'dark';
  document.querySelectorAll('.theme-toggle').forEach(b => {
    b.querySelectorAll('.theme-icon').forEach(i => i.classList.toggle('on', i.dataset.for === now));  // sun in dark, moon in light
    b.setAttribute('aria-label', `Switch to ${next} mode`); b.title = `Switch to ${next} mode`;
  });
  const pref = themePref();
  $('theme-seg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', b.dataset.v === pref));
}
function setTheme(pref) {
  if (pref === 'system') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = pref;
  try { pref === 'system' ? localStorage.removeItem(THEME_KEY) : localStorage.setItem(THEME_KEY, pref); } catch {}
  syncThemeUi();
}
document.querySelectorAll('.theme-toggle').forEach(b => {
  b.innerHTML = SUN + MOON;  // static markup, no user data
  b.onclick = () => { const next = effectiveTheme() === 'dark' ? 'light' : 'dark'; setTheme(next); toast(`${next === 'dark' ? 'Dark' : 'Light'} mode on. Change it any time in Settings.`, 'info'); };
});
segmented('theme-seg', v => setTheme(v));
darkQuery.addEventListener('change', syncThemeUi);  // OS switched while following the system
syncThemeUi();

// ---------- HELP & RESOURCES ----------
function openHelp() {
  const body = $('help-tpl').content.cloneNode(true).firstElementChild;
  body.querySelectorAll('[data-jump]').forEach(b => b.onclick = () => body.querySelector('#' + b.dataset.jump).scrollIntoView({behavior: 'smooth', block: 'start'}));
  body.querySelectorAll('[data-go]').forEach(b => b.onclick = () => { closeModal(); location.hash = b.dataset.go; });
  body.querySelectorAll('[data-ask]').forEach(b => b.onclick = () => { closeModal(); $('mode').value = 'agent'; ask(b.textContent); });
  body.querySelector('[data-action=setup]').onclick = () => { closeModal(); openOnboarding(); };
  body.querySelector('[data-action=chat]').onclick = () => { closeModal(); openChat(); };
  openModal(el('div', {}, el('div', {class: 'sub'}, 'FinSight AI'), el('h2', {style: 'font-size:1.2rem;margin-top:2px'}, 'Help & resources')), body);
}
$('help-btn').onclick = openHelp;
$('st-help').onclick = openHelp;
document.addEventListener('keydown', e => {
  const typing = e.target.closest('input, textarea, select, [contenteditable]');
  if (e.key === '?' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey && !$('app').hidden && !document.querySelector('dialog[open]')) { e.preventDefault(); openHelp(); }
});

// ---------- AUTH + BOOT ----------
function showSignin(message = '') {
  $('app').hidden = true; $('signin').hidden = false; $('signin-status').textContent = message;
  $('chat-launcher').hidden = true; $('chat-widget').hidden = true; chatHistory.length = 0;  // never leak chat across sign-outs
}
let notifTimer;
async function startApp(email, me = {}) {
  $('signin').hidden = true; $('app').hidden = false;
  $('flows-csv').closest('label').hidden = !me.is_admin;  // FII/DII history is shared by all users: admins only
  $('who').textContent = email || 'Dev mode';
  $('st-account').textContent = email ? `Signed in as ${email}` : 'Dev mode: single local user, no sign-in.';
  $('chat-launcher').hidden = false;
  try { CATEGORIES = await api('/api/categories'); } catch {}
  showTab(location.hash.slice(1) || 'overview');
  refreshNotifications(); clearInterval(notifTimer); notifTimer = setInterval(refreshNotifications, 60000);
  try { if (!(await api('/api/onboarding')).completed) openOnboarding(); } catch {}
}
const FIREBASE_SDK = 'https://www.gstatic.com/firebasejs/10.12.2/';
(async () => {
  let cfg = {enabled: false};
  try { cfg = await (await fetch('/api/auth/config')).json(); } catch {}
  if (!cfg.enabled) return startApp(null, await api('/api/me').catch(() => ({})));
  if (location.hostname === '127.0.0.1') { location.replace(location.href.replace('127.0.0.1', 'localhost')); return; }
  try {
    const {initializeApp} = await import(FIREBASE_SDK + 'firebase-app.js');
    const {getAuth, onAuthStateChanged, GoogleAuthProvider, signInWithPopup, signOut} = await import(FIREBASE_SDK + 'firebase-auth.js');
    firebaseAuth = getAuth(initializeApp(cfg.firebase));
    $('signin-btn').onclick = async () => { $('signin-status').textContent = ''; try { await signInWithPopup(firebaseAuth, new GoogleAuthProvider()); } catch (e) { $('signin-status').textContent = 'Sign-in failed: ' + (e.code || e.message); } };
    $('signout').hidden = false; $('signout').onclick = () => signOut(firebaseAuth);
    let rejection = '';
    onAuthStateChanged(firebaseAuth, async user => {
      if (!user) { clearInterval(notifTimer); showSignin(rejection); rejection = ''; return; }
      try { startApp(user.email, await api('/api/me')); }
      catch (e) { rejection = e.message; await signOut(firebaseAuth); }
    });
  } catch (e) { showSignin('Could not load Firebase: ' + e.message); }
})();
