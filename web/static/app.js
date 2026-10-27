/* TradingAgents Desk — frontend logic (vanilla JS + fetch SSE). */
'use strict';

const $ = (id) => document.getElementById(id);
const META = { providers: [], models: {}, prefs: {}, env: {}, announcements: [] };
const TEAMS = {
  'Analyst Team': ['Market Analyst', 'Sentiment Analyst', 'News Analyst', 'Fundamentals Analyst'],
  'Research Team': ['Bull Researcher', 'Bear Researcher', 'Research Manager'],
  'Trading Team': ['Trader'],
  'Risk Management': ['Aggressive Analyst', 'Neutral Analyst', 'Conservative Analyst'],
  'Portfolio Management': ['Portfolio Manager'],
};
const TEAM_NUMERAL = ['I', 'II', 'III', 'IV', 'V'];
const AGENT_STATE = { pending: 'wait', in_progress: 'live', completed: 'done', error: 'err' };
const ANALYST_LABEL = { market: 'Market', social: 'Sentiment', news: 'News', fundamentals: 'Fundamentals' };
let elapsedTimer = null;
let elapsedBase = 0;
let lastMd = null, lastHtml = null;

/* ── stoppable runs: one active run at a time, AbortController-backed ── */
let runState = null; // {kind: 'analysis'|'backtest', ctrl: AbortController}
const RUN_LABELS = { 'btn-run': 'Run analysis', 'btn-bt': 'Run backtest' };
function armStopButton(id) {
  const b = $(id);
  b.disabled = false;
  b.dataset.orig = b.innerHTML;
  b.innerHTML = '■ Stop';
  b.classList.add('is-stop');
}
function disarmStopButton(id) {
  const b = $(id);
  if (b.dataset.orig != null) b.innerHTML = b.dataset.orig;
  else b.innerHTML = RUN_LABELS[id] || b.innerHTML;
  b.classList.remove('is-stop');
  b.disabled = false;
}
function startRun(kind, id) {
  if (runState) {
    if (runState.kind === kind) runState.ctrl.abort(); // this button is now Stop
    else alert(`A ${runState.kind} run is active — stop it first.`);
    return null;
  }
  const ctrl = new AbortController();
  runState = { kind, ctrl };
  armStopButton(id);
  return ctrl;
}
function endRun(id) {
  runState = null;
  disarmStopButton(id);
}
const isAbort = (e) => e && (e.name === 'AbortError' || e.code === 20);

/* ── tabs ── */
const TABS = ['analysis', 'backtest', 'history'];
function showTab(which) {
  for (const t of TABS) {
    const on = t === which;
    $('view-' + t).classList.toggle('hidden', !on);
    $('tab-' + t).classList.toggle('is-active', on);
  }
  if (which === 'history') loadHistory();
  if (which === 'backtest') refreshEngineNote();
  // the chart cannot measure itself while its panel is hidden; redraw on return
  if (which === 'analysis' && window._lastBars) scheduleChart();
  if (location.hash !== '#' + which) history.replaceState(null, '', '#' + which);
}
$('tab-analysis').onclick = () => showTab('analysis');
$('tab-backtest').onclick = () => showTab('backtest');
$('tab-history').onclick = () => showTab('history');
if (TABS.includes(location.hash.slice(1))) showTab(location.hash.slice(1));

/* ── run dot ── */
function setRun(on, text) {
  $('run-dot-t').textContent = text || (on ? 'Running' : 'Idle');
  $('run-dot-i').className = 'sq ' + (on ? 'is-live' : '');
}

/* ── setup form ── */
async function loadMeta() {
  const res = await fetch('/api/meta');
  Object.assign(META, await res.json());

  $('in-date').value = META.today;
  $('in-date').max = META.today;
  $('edition').textContent = 'Desk · ' + META.today;
  // The placeholder markup has no date, so the first paint asked for "today".
  // Now that the real default is set, re-anchor the chart to it.
  refreshChart();

  const langSel = $('in-lang');
  const prefLang = META.prefs.output_language || 'English';
  for (const l of META.languages) {
    const o = document.createElement('option');
    o.value = o.textContent = l;
    if (l === prefLang) o.selected = true;
    langSel.appendChild(o);
  }

  const analystBox = $('in-analysts');
  const remembered = META.prefs.analysts || ['market', 'social', 'news', 'fundamentals'];
  for (const key of META.analysts) {
    const label = document.createElement('label');
    label.className = 'opt';
    label.dataset.key = key;
    label.innerHTML = `<input type="checkbox" ${remembered.includes(key) ? 'checked' : ''}><span>${ANALYST_LABEL[key] || key}</span>`;
    analystBox.appendChild(label);
  }

  const depthBox = $('in-depth');
  const prefDepth = META.prefs.research_depth ?? 1;
  META.depths.forEach((d, i) => {
    const label = document.createElement('label');
    label.className = 'opt';
    label.innerHTML = `<input type="radio" name="depth" value="${d.value}" ${(d.value === prefDepth || (i === 0 && ![1, 3, 5].includes(prefDepth))) ? 'checked' : ''}><span>${d.label}</span>`;
    depthBox.appendChild(label);
  });

  const provSel = $('in-provider');
  const prefProv = (META.prefs.llm_provider || META.env.provider || 'openai').split('-cn')[0];
  for (const p of META.providers) {
    const o = document.createElement('option');
    o.value = p.key; o.textContent = `${p.label} · ${p.key}`;
    if (p.key === prefProv) o.selected = true;
    provSel.appendChild(o);
  }
  refreshProvider();

  $('announce-pill').classList.toggle('hidden', !META.announcements.length);
  if (META.announcements.length) {
    $('announce-pill').title = META.announcements.join('\n\n').replace(/<[^>]*>/g, '');
    $('announce-pill').onclick = () => alert(META.announcements.join('\n\n').replace(/<[^>]*>/g, ''));
  }

  // backtest defaults
  const d = new Date();
  $('bt-end').value = META.today;
  $('bt-start').value = new Date(d - 30 * 864e5).toISOString().slice(0, 10);
  const btBox = $('bt-analysts');
  for (const key of META.analysts) {
    const label = document.createElement('label');
    label.className = 'opt';
    label.innerHTML = `<input type="checkbox" checked><span>${ANALYST_LABEL[key] || key}</span>`;
    label.dataset.key = key;
    btBox.appendChild(label);
  }
  refreshEngineNote();
}

function currentProvider() { return $('in-provider').value; }
function providerMeta(key) { return META.providers.find((p) => p.key === key) || {}; }

function refreshProvider() {
  const prov = currentProvider();
  const regionWrap = $('region-wrap');
  const regionSel = $('in-region');
  regionSel.innerHTML = '';
  let effProv = prov;
  if (['qwen', 'minimax', 'glm'].includes(prov)) {
    const opts = prov === 'qwen'
      ? [['qwen', 'International — dashscope-intl (DASHSCOPE_API_KEY)'], ['qwen-cn', 'China — dashscope (DASHSCOPE_CN_API_KEY)']]
      : prov === 'minimax'
        ? [['minimax', 'Global — api.minimax.io (MINIMAX_API_KEY)'], ['minimax-cn', 'China — api.minimaxi.com (MINIMAX_CN_API_KEY)']]
        : [['glm', 'Z.AI — api.z.ai (ZHIPU_API_KEY)'], ['glm-cn', 'BigModel — open.bigmodel.cn (ZHIPU_CN_API_KEY)']];
    $('region-label').textContent = `Region · ${prov.toUpperCase()}`;
    for (const [v, t] of opts) {
      const o = document.createElement('option');
      o.value = v; o.textContent = t;
      regionSel.appendChild(o);
    }
    regionWrap.classList.remove('hidden');
    effProv = regionSel.value;
  } else {
    regionWrap.classList.add('hidden');
  }
  // backend url default
  const pm = providerMeta(effProv) && providerMeta(effProv).default_url;
  const remembered = META.prefs.llm_provider === effProv ? META.prefs.backend_url : null;
  if (!document.activeElement || document.activeElement.id !== 'in-url') {
    if (!($('in-url').value || '').trim()) $('in-url').value = remembered || pm || '';
  }
  refreshModels(effProv);
  refreshEffort(effProv);
  refreshKeyWarn(effortProviderKey());
}
function effortProviderKey() {
  const prov = currentProvider();
  if ($('region-wrap').classList.contains('hidden')) return prov;
  return $('in-region').value || prov;
}

function fillModels(selId, customId, effProv, mode, prefKey) {
  const sel = $(selId);
  const custom = $(customId);
  sel.innerHTML = '';
  let opts = ((META.models[effProv] || {})[mode]) || [];
  if (!opts.some((m) => m.value === 'custom')) {
    opts = opts.concat([{ label: 'Custom model ID', value: 'custom' }]);
  }
  const pref = META.prefs[prefKey];
  opts.forEach((m) => {
    const o = document.createElement('option');
    o.value = m.value; o.textContent = m.label;
    if (m.value === pref) o.selected = true;
    sel.appendChild(o);
  });
  // A local endpoint's own model id is not in the catalog: keep it as a custom
  // entry instead of silently reverting to another model. Only for the provider
  // it was saved with, so switching providers still starts from that catalog.
  const rememberedHere = META.prefs.llm_provider === effProv;
  if (pref && rememberedHere && !opts.some((m) => m.value === pref)) {
    custom.value = pref;
    sel.value = 'custom';
  } else if (pref && opts.some((m) => m.value === pref)) {
    sel.value = pref;
  } else {
    sel.selectedIndex = 0;
  }
  custom.classList.toggle('hidden', sel.value !== 'custom');
}
function refreshModels(effProv) {
  fillModels('in-quick', 'in-quick-custom', effProv, 'quick', 'quick_think_llm');
  fillModels('in-deep', 'in-deep-custom', effProv, 'deep', 'deep_think_llm');
}
function refreshEffort(effProv) {
  const wrap = $('effort-wrap'), sel = $('in-effort');
  sel.innerHTML = '';
  const base = effProv.split('-cn')[0];
  const conf = { google: ['high', 'minimal'], openai: ['medium', 'high', 'low'], anthropic: ['high', 'medium', 'low'] }[base];
  if (!conf) { wrap.classList.add('hidden'); return; }
  wrap.classList.remove('hidden');
  $('effort-label').textContent = `Reasoning · ${effProv.toUpperCase()}`;
  for (const v of conf) {
    const o = document.createElement('option');
    o.value = o.textContent = v;
    sel.appendChild(o);
  }
}
function refreshKeyWarn(effProv) {
  const el = $('key-warn');
  const noKey = ['ollama', 'openai_compatible'];
  if (noKey.includes(effProv.split('-')[0]) && effProv !== 'openai_compatible') { el.classList.add('hidden'); return; }
  el.textContent = `Needs its API key in the environment/.env before running on ${effProv}.`;
  el.classList.remove('hidden');
}
$('in-provider').onchange = refreshProvider;
$('in-region').onchange = () => { const k = effortProviderKey(); refreshModels(k); refreshEffort(k); refreshKeyWarn(k); };
$('in-quick').onchange = () => $('in-quick-custom').classList.toggle('hidden', $('in-quick').value !== 'custom');
$('in-deep').onchange = () => $('in-deep-custom').classList.toggle('hidden', $('in-deep').value !== 'custom');
function refreshAssetHint() {
  const v = ($('in-ticker').value || '').trim().toUpperCase();
  const crypto = /-(USD|USDT|USDC|BTC|ETH)$/.test(v);
  $('asset-hint').textContent = crypto ? 'Detected: crypto — fundamentals unavailable' : '';
  document.querySelectorAll('#in-analysts label').forEach((l) => {
    if (l.dataset.key === 'fundamentals') l.style.display = crypto ? 'none' : '';
  });
}

/* ticker autocomplete */
let symTimer = null, symItems = [], symActive = -1;
function hideTickerDrop() { $('ticker-drop').classList.add('hidden'); symActive = -1; }
function pickSymbol(sym) {
  $('in-ticker').value = sym;
  hideTickerDrop();
  refreshAssetHint();
  loadChart(sym, ($('in-date') || {}).value || '');
}
async function suggestTickers() {
  const q = ($('in-ticker').value || '').trim();
  const drop = $('ticker-drop');
  if (q.length < 1) { hideTickerDrop(); return; }
  try {
    const res = await fetch('/api/symbols?q=' + encodeURIComponent(q));
    const { results } = await res.json();
    if (($('in-ticker').value || '').trim() !== q) return; // stale response
    symItems = results || [];
    symActive = symItems.length ? 0 : -1;
    if (!symItems.length) { hideTickerDrop(); return; }
    drop.innerHTML = '';
    symItems.forEach((r, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'sym-item' + (i === 0 ? ' is-active' : '');
      b.innerHTML = `<span class="sym-code">${esc(r.symbol)}</span><span class="sym-name">${esc(r.name)}</span>`;
      b.onmousedown = (e) => { e.preventDefault(); pickSymbol(r.symbol); };
      b.onmousemove = () => highlightSym(i);
      drop.appendChild(b);
    });
    drop.classList.remove('hidden');
  } catch (e) { hideTickerDrop(); }
}
function highlightSym(i) {
  symActive = i;
  [...$('ticker-drop').children].forEach((c, k) => c.classList.toggle('is-active', k === i));
}
$('in-ticker').addEventListener('input', () => {
  refreshAssetHint();
  clearTimeout(symTimer);
  symTimer = setTimeout(suggestTickers, 150);
});
$('in-ticker').addEventListener('keydown', (e) => {
  const open = !$('ticker-drop').classList.contains('hidden');
  if (e.key === 'ArrowDown' && open) { e.preventDefault(); highlightSym((symActive + 1) % symItems.length); }
  else if (e.key === 'ArrowUp' && open) { e.preventDefault(); highlightSym((symActive - 1 + symItems.length) % symItems.length); }
  else if (e.key === 'Enter' && open && symActive >= 0) { e.preventDefault(); pickSymbol(symItems[symActive].symbol); }
  else if (e.key === 'Escape') hideTickerDrop();
});
$('in-ticker').addEventListener('blur', () => setTimeout(hideTickerDrop, 120));

/* ── live rendering ── */
function renderPipeline(agents) {
  const box = $('pipeline');
  box.innerHTML = '';
  Object.entries(TEAMS).forEach(([team, members], ti) => {
    const active = members.filter((m) => agents && m in agents);
    if (!active.length) return;
    const row = document.createElement('div');
    row.className = 'pipe-row';
    row.innerHTML = `<div class="pipe-team"><b>${TEAM_NUMERAL[ti]}</b>${team}</div>`;
    const list = document.createElement('div');
    list.className = 'pipe-agents';
    for (const m of active) {
      const st = agents[m] || 'pending';
      const el = document.createElement('span');
      el.className = 'agent';
      el.dataset.status = st;
      el.innerHTML = `<i></i>${esc(m)}<span class="as">${AGENT_STATE[st] || esc(st)}</span>`;
      list.appendChild(el);
    }
    row.appendChild(list);
    box.appendChild(row);
  });
}
/* show the pipeline structure at rest, before the first snapshot arrives */
renderPipeline(Object.fromEntries(Object.values(TEAMS).flat().map((m) => [m, 'pending'])));

function renderStats(snap, note) {
  $('st-agents').textContent = snap ? `${Object.values(snap.agents).filter((s) => s === 'completed').length}/${Object.keys(snap.agents).length}` : '–';
  $('st-llm').textContent = snap ? snap.llm_calls : '–';
  $('st-tools').textContent = snap ? snap.tool_calls : '–';
  $('st-tokens').textContent = snap ? `${snap.tokens_in}↑ ${snap.tokens_out}↓` : '–';
  $('st-reports').textContent = snap ? `${snap.completed_reports}/${snap.total_reports}` : '–';
  if (note) $('st-note').textContent = note;
}

function startElapsed() {
  stopElapsed();
  const t0 = Date.now();
  elapsedTimer = setInterval(() => {
    const s = Math.floor((Date.now() - t0 + elapsedBase * 1000) / 1000);
    $('st-elapsed').textContent = `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }, 500);
}
function stopElapsed() { if (elapsedTimer) clearInterval(elapsedTimer); elapsedTimer = null; }

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
const FEED_KIND = {
  Tool: 'k-tool', Data: 'k-data', System: 'k-system', Route: 'k-route',
  Error: 'k-error', Message: 'k-message',
};
function pushFeed(kind, ts, title, body) {
  const feed = $('feed');
  const empty = feed.querySelector('.feed-empty');
  if (empty) empty.remove();
  const div = document.createElement('div');
  div.className = 'feed-item';
  div.innerHTML = `<span class="feed-time">${esc(ts || '')}</span>`
    + `<span class="feed-kind ${FEED_KIND[kind] || 'k-message'}">${esc(kind)}</span>`
    + `<span class="feed-body">${esc(title)}</span>`
    + (body ? `<p class="feed-text">${esc(String(body).slice(0, 400))}</p>` : '');
  feed.prepend(div);
  while (feed.children.length > 80) feed.lastChild.remove();
}

const REPORT_TITLES = {
  market_report: 'Market Analysis', sentiment_report: 'Social Sentiment', news_report: 'News Analysis',
  fundamentals_report: 'Fundamentals Analysis',
  bull_history: 'Bull Researcher', bear_history: 'Bear Researcher',
  investment_plan: 'Research Team Decision',
  trader_investment_plan: 'Trading Team Plan',
  aggressive_history: 'Aggressive Analyst', conservative_history: 'Conservative Analyst',
  neutral_history: 'Neutral Analyst', final_trade_decision: 'Portfolio Management Decision',
};
const latestHtml = {};
function pushReport(section, title, html) {
  latestHtml[section] = `<h3>${esc(title)}</h3>` + html;
  const order = Object.keys(REPORT_TITLES).filter((k) => latestHtml[k]);
  $('current-report').innerHTML = order.map((k) => latestHtml[k]).join('');
}

/* ── SSE reader ── */
async function readSSE(response, onEvent) {
  const reader = response.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      for (const line of chunk.split('\n')) {
        if (line.startsWith('data:')) {
          try { onEvent(JSON.parse(line.slice(5).trim())); } catch (e) { /* partial */ }
        }
      }
    }
  }
}

/* ── run analysis ── */
function selectedAnalysts() {
  return [...document.querySelectorAll('#in-analysts label')]
    .filter((l) => l.style.display !== 'none' && l.querySelector('input').checked)
    .map((l) => l.dataset.key);
}

/* The Run setup engine: provider, endpoint, models, depth, reasoning effort.
   Both tabs send it — a sweep runs the same graph as an analysis. */
function engineFields() {
  const effProv = effortProviderKey();
  const qv = $('in-quick').value === 'custom' ? $('in-quick-custom').value.trim() : $('in-quick').value;
  const dv = $('in-deep').value === 'custom' ? $('in-deep-custom').value.trim() : $('in-deep').value;
  const depth = parseInt(document.querySelector('input[name="depth"]:checked')?.value, 10);
  return {
    llm_provider: effProv,
    backend_url: $('in-url').value.trim() || null,
    quick_think_llm: qv || null,
    deep_think_llm: dv || null,
    research_depth: Number.isFinite(depth) ? depth : null,
    google_thinking_level: effProv === 'google' ? ($('in-effort').value || null) : null,
    openai_reasoning_effort: effProv === 'openai' ? ($('in-effort').value || null) : null,
    anthropic_effort: effProv === 'anthropic' ? ($('in-effort').value || null) : null,
  };
}

function engineNote() {
  const f = engineFields();
  const depth = f.research_depth;
  return `${f.llm_provider} · ${f.deep_think_llm || 'default model'}`
    + (depth ? ` · ${depth} round${depth === 1 ? '' : 's'}` : '');
}

function refreshEngineNote() {
  const el = $('bt-engine');
  if (el) el.textContent = `Engine from Run setup: ${engineNote()}`;
}

$('btn-run').onclick = async () => {
  if (runState) { startRun('analysis', 'btn-run'); return; } // Stop button behavior
  const analysts = selectedAnalysts();
  if (!analysts.length) { alert('Select at least one analyst.'); return; }
  let portfolio = null;
  const ptxt = $('in-portfolio').value.trim();
  if (ptxt) {
    try { portfolio = JSON.parse(ptxt); }
    catch (e) { alert('Portfolio JSON is not usable: ' + e.message); return; }
  }
  const engine = engineFields();
  if (!engine.quick_think_llm || !engine.deep_think_llm) { alert('Pick both thinking models (or enter custom IDs).'); return; }
  const body = {
    ticker: $('in-ticker').value.trim() || 'SPY',
    analysis_date: $('in-date').value,
    output_language: $('in-lang').value,
    analysts,
    ...engine,
    checkpoint: $('in-checkpoint').checked,
    save_report: $('in-save').checked,
    make_html: $('in-html').checked,
    portfolio,
  };

  const ctrl = startRun('analysis', 'btn-run');
  if (!ctrl) return;
  setRun(true);
  elapsedBase = 0;
  startElapsed();
  $('final-wrap').classList.add('hidden');
  $('feed').innerHTML = '<p class="feed-empty">Listening…</p>';
  $('current-report').innerHTML = '<p style="color:#64708a;font-style:italic">Waiting for the first analyst report…</p>';
  for (const k of Object.keys(latestHtml)) delete latestHtml[k];
  lastMd = lastHtml = null;

  try {
    const res = await fetch('/api/analyze', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    await readSSE(res, (ev) => {
      if (ev.snapshot) {
        renderPipeline(ev.snapshot.agents);
        renderStats(ev.snapshot, ev.note);
        if (ev.snapshot.elapsed) elapsedBase = ev.snapshot.elapsed;
      }
      if (ev.type === 'status' && ev.note) $('st-note').textContent = ev.note;
      else if (ev.type === 'message') pushFeed(ev.kind, ev.timestamp, '', ev.content);
      else if (ev.type === 'tool') pushFeed('Tool', ev.timestamp, `${ev.name}(${Object.entries(ev.args || {}).map(([k, v]) => `${k}=${v}`).join(', ')})`, '');
      else if (ev.type === 'report') pushReport(ev.section, ev.title, ev.html);
      else if (ev.type === 'warning') pushFeed('System', '', ev.message, '');
      else if (ev.type === 'route') pushFeed('Route', ev.timestamp, ev.text, '');
      else if (ev.type === 'done') finishRun(ev);
      else if (ev.type === 'error') {
        pushFeed('Error', '', ev.message, '');
        $('st-note').textContent = 'error';
      }
    });
  } catch (e) {
    if (isAbort(e)) {
      pushFeed('System', '', 'Stopped by user — partial results above are discarded.', '');
      $('st-note').textContent = 'stopped';
    } else {
      pushFeed('Error', '', 'Connection failed: ' + e.message, '');
    }
  } finally {
    endRun('btn-run');
    setRun(false);
    stopElapsed();
  }
};

const GROUP_TITLE = {
  market_report: 'Market Analyst', sentiment_report: 'Sentiment Analyst', news_report: 'News Analyst',
  fundamentals_report: 'Fundamentals Analyst',
  bull_history: 'Bull Researcher', bear_history: 'Bear Researcher', investment_plan: 'Research Manager',
  trader_investment_plan: 'Trader',
  aggressive_history: 'Aggressive Analyst', conservative_history: 'Conservative Analyst',
  neutral_history: 'Neutral Analyst', final_trade_decision: 'Portfolio Manager',
};

/* right-hand stamp on the rating hero: instrument + session, from the save path */
function heroMeta(dir) {
  const parts = String(dir || '').split(/[\\/]/).filter(Boolean);
  if (parts.length < 2) return '';
  const date = parts[parts.length - 1];
  const ticker = parts[parts.length - 2];
  return `<div class="rating-meta">
      <span class="rating-kicker">Instrument</span><b>${esc(ticker)}</b>
      <span class="rating-kicker">Session</span><b>${esc(date)}</b>
    </div>`;
}

function finishRun(ev) {
  $('st-note').textContent = 'complete';
  const hero = $('rating-hero');
  const up = ['Buy', 'Overweight'].includes(ev.rating);
  const down = ['Sell', 'Underweight'].includes(ev.rating);
  hero.className = 'rating-hero ' + (ev.is_review
    ? 'is-review'
    : up ? 'is-up' : down ? 'is-down' : 'is-flat');
  hero.innerHTML = ev.is_review
    ? `<div class="rating-fig">
         <span class="rating-kicker">Review</span>
         <span class="rating-word">Unrated</span>
       </div>
       <p class="rating-review"><b>No rating could be read from the final decision</b>
         Recorded for review rather than as a position. ${esc(ev.wall_time || '')}</p>`
    : `<div class="rating-fig">
         <span class="rating-kicker">Final rating</span>
         <span class="rating-word">${esc(ev.rating || '?')}</span>
         <p class="rating-note">${esc(ev.wall_time || '')}</p>
       </div>
       ${gaugeSVG(ev.rating)}${heroMeta(ev.save_dir)}`;

  const byKey = {};
  for (const s of ev.sections || []) byKey[s.key] = s;
  const wrap = $('final-report');
  wrap.innerHTML = '';
  const shown = new Set();
  const groups = [
    ['I. Analyst Team Reports', ['market_report', 'sentiment_report', 'news_report', 'fundamentals_report']],
    ['II. Research Team Decision', ['bull_history', 'bear_history', 'investment_plan']],
    ['III. Trading Team Plan', ['trader_investment_plan']],
    ['IV. Risk Management Team Decision', ['aggressive_history', 'conservative_history', 'neutral_history']],
    ['V. Portfolio Manager Decision', ['final_trade_decision']],
  ];
  for (const [heading, keys] of groups) {
    const parts = keys.map((k) => byKey[k]).filter(Boolean);
    if (!parts.length) continue;
    const card = document.createElement('section');
    card.className = 'final-card';
    card.innerHTML = `<h3 class="final-head">${heading}</h3>`;
    for (const p of parts) {
      shown.add(p.key);
      const d = document.createElement('details');
      d.className = 'final-det';
      if (p.key === 'final_trade_decision') d.open = true;
      d.innerHTML = `<summary>${esc(GROUP_TITLE[p.key] || p.title)}</summary><div class="rich">${p.html}</div>`;
      card.appendChild(d);
    }
    wrap.appendChild(card);
  }
  for (const s of ev.sections || []) {
    if (shown.has(s.key)) continue;
    const d = document.createElement('section');
    d.className = 'final-card rich';
    d.innerHTML = `<h3 class="final-head">${esc(s.title)}</h3><div class="rich">${s.html}</div>`;
    wrap.appendChild(d);
  }

  lastMd = ev.complete_md; lastHtml = ev.complete_html;
  $('dl-md').style.display = ev.complete_md ? '' : 'none';
  $('dl-html').style.display = ev.complete_html ? '' : 'none';
  $('save-path').textContent = ev.save_dir || '';
  $('final-wrap').classList.remove('hidden');
  $('final-wrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function download(name, text, mime) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: mime || 'text/plain' }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}
$('dl-md').onclick = () => lastMd && download('complete_report.md', lastMd, 'text/markdown');
$('dl-html').onclick = () => lastHtml && download('complete_report.html', lastHtml, 'text/html');

/* ── backtest ── */
$('btn-bt').onclick = async () => {
  if (runState) { startRun('backtest', 'btn-bt'); return; } // Stop button behavior
  const analysts = [...document.querySelectorAll('#bt-analysts label')]
    .filter((l) => l.querySelector('input').checked).map((l) => l.dataset.key);
  const body = {
    tickers: $('bt-tickers').value, start: $('bt-start').value, end: $('bt-end').value,
    every: parseInt($('bt-every').value, 10) || 7, asset_type: $('bt-asset').value,
    analysts: analysts.length ? analysts : ['market', 'social', 'news', 'fundamentals'],
    run_id: $('bt-runid').value.trim() || null,
    ...engineFields(),
  };
  refreshEngineNote();
  const ctrl = startRun('backtest', 'btn-bt');
  if (!ctrl) return;
  $('bt-issues').innerHTML = '';
  $('bt-summary').textContent = 'Running… (press Stop to cancel)';
  $('bt-status').textContent = `starting · ${engineNote()}`;
  try {
    const res = await fetch('/api/backtest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      signal: ctrl.signal,
    });
    await readSSE(res, (ev) => {
      if (ev.type === 'hello') {
        $('bt-status').textContent = `queued · ${ev.note}`;
      } else if (ev.type === 'progress') {
        const pct = ev.total ? Math.round((100 * ev.done) / ev.total) : 0;
        $('bt-bar').style.width = pct + '%';
        $('bt-status').textContent = `[${ev.done}/${ev.total}] ${ev.ticker} ${ev.date}`;
      } else if (ev.type === 'warning') {
        const p = document.createElement('p');
        p.className = 'bt-issue';
        p.textContent = ev.message;
        $('bt-issues').prepend(p);
      } else if (ev.type === 'route') {
        const p = document.createElement('p');
        p.className = 'bt-issue route';
        p.textContent = `→ ${ev.ticker ? '[' + ev.ticker + '] ' : ''}${ev.text}`;
        $('bt-issues').prepend(p);
        while ($('bt-issues').children.length > 60) $('bt-issues').lastChild.remove();
      } else if (ev.type === 'done') {
        $('bt-bar').style.width = '100%';
        $('bt-status').textContent = `Ran ${ev.cells_run} cells, skipped ${ev.skipped} · ${ev.note || engineNote()} · log ${ev.log_path} · run-id ${ev.run_id}`;
        $('bt-summary').textContent = ev.summary;
        for (const f of ev.failures || []) {
          const p = document.createElement('p');
          p.className = 'bt-issue';
          p.textContent = `failed: ${f.ticker} ${f.date}: ${f.reason}`;
          $('bt-issues').appendChild(p);
        }
        for (const f of ev.settlement_failures || []) {
          const p = document.createElement('p');
          p.className = 'bt-issue';
          p.textContent = `unsettled: ${f.ticker}: ${f.reason}`;
          $('bt-issues').appendChild(p);
        }
      } else if (ev.type === 'error') {
        $('bt-summary').textContent = 'Error: ' + ev.message;
      }
    });
  } catch (e) {
    if (isAbort(e)) {
      $('bt-summary').textContent = 'Stopped by user — resume the sweep later with its run-id.';
      $('bt-status').textContent = 'stopped';
    } else {
      $('bt-summary').textContent = 'Connection failed: ' + e.message;
    }
  } finally {
    endRun('btn-bt');
  }
};

loadMeta().catch((e) => { $('st-note').textContent = 'meta failed'; console.error(e); });

/* ── archive ── */
let histReports = [];
let histCurrent = null;
let histCurrentMd = null, histCurrentHtml = null;

const RATING_TONE = {
  Buy: 'b-up', Overweight: 'b-up', Hold: '', Underweight: 'b-down', Sell: 'b-down',
};

async function loadHistory() {
  const box = $('hist-list');
  box.innerHTML = '<p class="feed-empty" style="padding:2px">Loading…</p>';
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    histReports = data.reports || [];
    $('hist-root').textContent = data.root || '';
    renderHistList();
  } catch (e) {
    box.innerHTML = `<p class="bt-issue">Failed: ${esc(e.message)}</p>`;
  }
}

function renderHistList() {
  const box = $('hist-list');
  const f = ($('hist-filter').value || '').trim().toUpperCase();
  const items = histReports.filter((r) => !f || (r.ticker || '').toUpperCase().includes(f));
  box.innerHTML = items.length ? '' : '<p class="feed-empty" style="padding:2px">No saved reports yet — run an analysis with “Save report” on.</p>';
  for (const r of items) {
    const b = document.createElement('button');
    const active = histCurrent === r.name;
    b.className = 'hist-item' + (active ? ' is-active' : '');
    const badge = r.rating
      ? `<span class="badge ${RATING_TONE[r.rating] || ''}">${esc(r.rating)}</span>` : '';
    b.innerHTML = `<div class="hist-top">
        <span class="hist-sym">${esc(r.ticker)}</span>${badge}
        <span class="hist-date">${esc(r.modified)}</span></div>
      <div class="hist-sub">${esc(r.analysis_date || '')} · ${esc(r.name)}</div>`;
    b.onclick = () => viewReport(r.name);
    box.appendChild(b);
  }
  box.classList.toggle('is-scroll', box.scrollHeight > box.clientHeight + 4);
}

async function viewReport(name) {
  histCurrent = name;
  renderHistList();
  $('hist-title').textContent = name;
  $('hist-frame').removeAttribute('srcdoc');
  $('hist-empty').classList.add('hidden');
  try {
    const res = await fetch('/api/history/' + encodeURIComponent(name));
    const data = await res.json();
    if (data.error) {
      $('hist-title').textContent = 'Error: ' + data.error;
      $('hist-empty').classList.remove('hidden');
      return;
    }
    histCurrentMd = data.md; histCurrentHtml = data.html;
    if (data.html) $('hist-frame').srcdoc = data.html;
    else $('hist-frame').srcdoc = '<body style="font-family:Georgia,serif;padding:32px"><pre>' + esc(data.md) + '</pre></body>';
    $('hist-dl-md').classList.remove('hidden');
    $('hist-dl-html').classList.toggle('hidden', !data.html);
    $('hist-del').classList.remove('hidden');
  } catch (e) {
    $('hist-title').textContent = 'Failed: ' + e.message;
  }
}

$('btn-refresh-hist').onclick = loadHistory;
$('hist-filter').oninput = renderHistList;
$('hist-dl-md').onclick = () => histCurrentMd && download(histCurrent + '.md', histCurrentMd, 'text/markdown');
$('hist-dl-html').onclick = () => histCurrentHtml && download(histCurrent + '.html', histCurrentHtml, 'text/html');
$('hist-del').onclick = async () => {
  if (!histCurrent || !confirm(`Delete saved report "${histCurrent}"?`)) return;
  const res = await fetch('/api/history/' + encodeURIComponent(histCurrent), { method: 'DELETE' });
  const data = await res.json();
  if (data.deleted) {
    histCurrent = null;
    $('hist-frame').removeAttribute('srcdoc');
    $('hist-title').textContent = 'Select a report';
    $('hist-empty').classList.remove('hidden');
    for (const id of ['hist-dl-md', 'hist-dl-html', 'hist-del']) $(id).classList.add('hidden');
    loadHistory();
  } else {
    alert('Delete failed: ' + (data.error || 'unknown'));
  }
};

/* ══ market visuals: gauge, tape, candles ══ */

function gaugeSVG(rating) {
  const order = ['Sell', 'Underweight', 'Hold', 'Overweight', 'Buy'];
  const colors = ['#fb7185', '#fb923c', '#94a3b8', '#4ade80', '#34d399'];
  const idx = order.indexOf(rating);
  const cx = 100, cy = 96, r = 78;
  const arc = (a0, a1) => {
    const p = (a) => `${(cx + r * Math.cos(Math.PI - a * Math.PI)).toFixed(1)},${(cy - r * Math.sin(a * Math.PI)).toFixed(1)}`;
    return `M ${p(a0)} A ${r} ${r} 0 0 1 ${p(a1)}`;
  };
  let segs = '';
  order.forEach((_, i) => {
    const dim = idx < 0 || i > idx;
    segs += `<path d="${arc(i / 5 + 0.012, (i + 1) / 5 - 0.012)}" fill="none" stroke="${colors[i]}" stroke-width="13" stroke-linecap="butt" opacity="${dim ? 0.2 : 1}"/>`;
  });
  const a = idx < 0 ? 0.5 : idx / 4;
  const nx = cx - 62 * Math.cos(a * Math.PI), ny = cy - 62 * Math.sin(a * Math.PI);
  return `<svg width="210" height="112" viewBox="0 0 200 108" role="img" aria-label="rating gauge">
    ${segs}
    <line x1="${cx}" y1="${cy}" x2="${nx.toFixed(1)}" y2="${ny.toFixed(1)}" stroke="#ecf2fb" stroke-width="2"/>
    <circle cx="${cx}" cy="${cy}" r="4.5" fill="#ecf2fb"/>
    <text x="10" y="107" fill="#8b98ad" font-size="8" letter-spacing="1.5" font-family="'IBM Plex Mono',monospace">SELL</text>
    <text x="171" y="107" fill="#8b98ad" font-size="8" letter-spacing="1.5" font-family="'IBM Plex Mono',monospace">BUY</text>
  </svg>`;
}

/* ticker tape */
async function loadTape() {
  try {
    const res = await fetch('/api/quotes');
    const { quotes } = await res.json();
    if (!quotes || !quotes.length) {
      $('tape').innerHTML = '<span class="tape-item" style="border:0;color:#64708a">Quotes unavailable</span>';
      return;
    }
    const item = (q) => {
      const up = q.chg >= 0;
      return `<span class="tape-item"><span class="tape-sym">${esc(q.symbol)}</span>`
        + `<span class="tape-px">${q.price.toLocaleString()}</span>`
        + `<span class="${up ? 'up' : 'down'}">${up ? '▲' : '▼'} ${up ? '+' : ''}${q.chg.toFixed(2)}%</span></span>`;
    };
    const half = quotes.map(item).join('');
    $('tape').innerHTML = half + half; // duplicated for a seamless loop
  } catch (e) {
    $('tape').innerHTML = '<span class="tape-item" style="border:0;color:#64708a">Quotes unavailable</span>';
  }
}
loadTape();
setInterval(loadTape, 75000);

/* candlestick chart (canvas, no dependencies) */
function ma(values, n) {
  return values.map((_, i) => {
    if (i < n - 1) return null;
    let s = 0;
    for (let k = i - n + 1; k <= i; k++) s += values[k];
    return s / n;
  });
}

const CH = {
  ink: '#ecf2fb', muted: '#8b98ad', faint: '#64708a',
  grid: 'rgba(255,255,255,.075)', soft: 'rgba(255,255,255,.035)',
  up: '#34d399', down: '#fb7185',
  ma20: '#60a5fa', ma50: '#fbbf24', ema21: '#a78bfa', rsi: '#67e8f9',
  volUp: 'rgba(52,211,153,.20)', volDown: 'rgba(251,113,133,.20)',
};

/* The fetched series plus the window the user is looking at. i0/i1 are bar
   indices and stay fractional while zooming/panning so the view glides instead
   of snapping a bar at a time. */
const CHART = {
  ticker: '', bars: [], i0: 0, i1: 0, hover: -1, chip: '6M', free: false,
  ma20: true, ma50: true, ema21: false, vol: true, rsi: false, log: false,
  drag: null, tuned: false,
};
const RANGE_DAYS = { '1M': 30, '3M': 92, '6M': 195, '1Y': 370 };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function ema(values, n) {
  const k = 2 / (n + 1);
  const out = new Array(values.length).fill(null);
  let prev = null;
  for (let i = n - 1; i < values.length; i++) {
    if (prev === null) {
      let s = 0;
      for (let j = i - n + 1; j <= i; j++) s += values[j];
      prev = s / n;
    } else {
      prev = values[i] * k + prev * (1 - k);
    }
    out[i] = prev;
  }
  return out;
}

function rsi14(values, n = 14) {
  const out = new Array(values.length).fill(null);
  if (values.length <= n) return out;
  let gain = 0, loss = 0;
  for (let i = 1; i <= n; i++) {
    const d = values[i] - values[i - 1];
    gain += Math.max(d, 0); loss += Math.max(-d, 0);
  }
  gain /= n; loss /= n;
  out[n] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  for (let i = n + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    gain = (gain * (n - 1) + Math.max(d, 0)) / n;
    loss = (loss * (n - 1) + Math.max(-d, 0)) / n;
    out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  }
  return out;
}

function dayOf(t) { return new Date(t + 'T00:00:00'); }
function fmtDay(t) {
  const d = dayOf(t);
  return `${MONTHS[d.getMonth()]} ${String(d.getDate()).padStart(2, '0')}`;
}
function fmtVol(v) {
  if (!v) return '—';
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return Math.round(v / 1e3) + 'K';
  return String(v);
}

function niceStep(span, target) {
  const raw = span / Math.max(1, target);
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const norm = raw / mag;
  // nearest candidate in ratio space: 27 -> 25, not 50
  const mult = [1, 2, 2.5, 5, 10].reduce(
    (best, m) => (Math.abs(Math.log(m / norm)) < Math.abs(Math.log(best / norm)) ? m : best), 1);
  return mult * mag;
}

function pill(ctx, x, y, w, h, r) {
  ctx.beginPath();
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function priceTag(ctx, x, y, text, bg) {
  ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
  const w = ctx.measureText(text).width + 10;
  ctx.fillStyle = bg;
  pill(ctx, x, y - 8, w, 16, 4);
  ctx.fill();
  ctx.fillStyle = '#05070d';
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText(text, x + 5, y + 0.5);
}

function rangeBounds(key) {
  const bars = CHART.bars, last = bars.length - 1;
  if (!bars.length) return [0, 0];
  if (key === 'YTD') {
    const year = bars[last].t.slice(0, 4);
    const i = bars.findIndex((b) => b.t.slice(0, 4) === year);
    return [Math.max(0, i < 0 ? 0 : i), last];
  }
  const days = RANGE_DAYS[key] || 195;
  const cutoff = new Date(dayOf(bars[last].t).getTime() - days * 864e5);
  let i = bars.findIndex((b) => dayOf(b.t) >= cutoff);
  if (i < 0) i = 0;
  return [Math.max(0, Math.min(i, Math.max(0, last - 4))), last];
}

function markRangeChips() {
  document.querySelectorAll('#chart-ranges .seg-btn').forEach((b) => {
    const on = !CHART.free && b.dataset.range === CHART.chip;
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function applyRange(key) {
  CHART.chip = key;
  CHART.free = false;
  const [a, b] = rangeBounds(key);
  CHART.i0 = a; CHART.i1 = b;
  CHART.hover = -1;
  markRangeChips();
  renderChart();
}

function updateOhlc() {
  const el = $('chart-ohlc');
  if (!el) return;
  const b = CHART.bars[CHART.hover];
  if (!b) { el.textContent = ''; return; }
  const chg = ((b.c - b.o) / b.o) * 100;
  el.textContent = `${b.t}  O ${b.o.toFixed(2)}  H ${b.h.toFixed(2)}  L ${b.l.toFixed(2)}  `
    + `C ${b.c.toFixed(2)}  (${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%)  Vol ${fmtVol(b.v)}`;
}

function renderChart() {
  const cv = $('chart'), bars = CHART.bars;
  const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
  const W = Math.max(240, Math.round(cv.clientWidth || cv.parentElement.clientWidth || 640));
  const H = W < 560 ? 268 : 300;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr);
    cv.height = Math.round(H * dpr);
  }
  cv.style.height = H + 'px';
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!bars.length) return;

  const PADL = 6, PADR = 62, PADT = 10, PADB = 24;
  const plotW = W - PADL - PADR;
  const i0 = Math.max(0, Math.round(CHART.i0));
  const i1 = Math.min(bars.length - 1, Math.round(CHART.i1));
  const n = i1 - i0 + 1;
  if (n < 1) return;

  const totalH = H - PADT - PADB;
  const volH = CHART.vol ? Math.round(totalH * 0.15) : 0;
  const rsiH = CHART.rsi ? Math.round(totalH * 0.2) : 0;
  const priceH = totalH - volH - rsiH - (CHART.vol ? 8 : 0) - (CHART.rsi ? 8 : 0);
  let cursor = PADT + priceH;
  let volTop = 0, rsiTop = 0;
  if (CHART.vol) { cursor += 8; volTop = cursor; cursor += volH; }
  if (CHART.rsi) { cursor += 8; rsiTop = cursor; }

  const closes = bars.map((b) => b.c);
  const overlays = [];
  if (CHART.ma20) overlays.push([ma(closes, 20), CH.ma20]);
  if (CHART.ma50) overlays.push([ma(closes, 50), CH.ma50]);
  if (CHART.ema21) overlays.push([ema(closes, 21), CH.ema21]);

  let lo = Infinity, hi = -Infinity, vmax = 0;
  for (let i = i0; i <= i1; i++) {
    const b = bars[i];
    lo = Math.min(lo, b.l); hi = Math.max(hi, b.h);
    vmax = Math.max(vmax, b.v || 0);
  }
  for (const [vals] of overlays) {
    for (let i = i0; i <= i1; i++) {
      const v = vals[i];
      if (v == null) continue;
      lo = Math.min(lo, v); hi = Math.max(hi, v);
    }
  }
  if (!isFinite(lo) || !isFinite(hi)) return;

  const log = CHART.log && lo > 0;
  const tr = (p) => (log ? Math.log(p) : p);
  const rawSpan = (tr(hi) - tr(lo)) || 1;
  const tLo = tr(lo) - rawSpan * 0.06;
  const tHi = tr(hi) + rawSpan * 0.06;
  const Y = (p) => PADT + (1 - (tr(p) - tLo) / (tHi - tLo)) * priceH;
  const step = plotW / n;
  const X = (i) => PADL + (i - i0 + 0.5) * step;
  const bw = Math.max(1, Math.min(10, step * 0.62));
  const tagDp = hi < 10000 ? 2 : 0;
  const tagY = (y) => Math.max(PADT + 8, Math.min(PADT + priceH - 8, y));
  const lastBar = bars[bars.length - 1];
  const lastTagY = i1 === bars.length - 1 ? tagY(Y(lastBar.c)) : null;
  const hoverBar = CHART.hover >= i0 && CHART.hover <= i1 ? bars[CHART.hover] : null;
  const hoverTagY = hoverBar ? tagY(Y(hoverBar.c)) : null;

  ctx.lineWidth = 1;
  ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';

  // price grid + right-hand scale
  const pStep = niceStep(tHi - tLo, 5);
  const dp = pStep >= 1 ? 0 : pStep >= 0.05 ? 2 : 3;
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  for (let v = Math.ceil(tLo / pStep) * pStep; v <= tHi + 1e-9; v += pStep) {
    const price = log ? Math.exp(v) : v;
    if (price <= 0) continue;
    const y = Math.round(Y(price)) + 0.5;
    ctx.strokeStyle = CH.grid;
    ctx.beginPath(); ctx.moveTo(PADL, y); ctx.lineTo(PADL + plotW, y); ctx.stroke();
    // a tag sitting on this line already prints the price; do not double-print it
    const clash = (lastTagY != null && Math.abs(y - lastTagY) < 11)
      || (hoverTagY != null && Math.abs(y - hoverTagY) < 11);
    if (clash) continue;
    ctx.fillStyle = CH.muted;
    ctx.fillText(price.toFixed(dp), PADL + plotW + 7, y);
  }
  ctx.strokeStyle = CH.soft;
  ctx.beginPath();
  ctx.moveTo(PADL + plotW + 0.5, PADT);
  ctx.lineTo(PADL + plotW + 0.5, PADT + totalH);
  ctx.stroke();

  // date axis: sessions when the window is short, month starts when it is long
  const marks = [];
  if (n <= 46) {
    const every = Math.max(1, Math.ceil(n / 5));
    for (let i = i0; i <= i1; i += every) marks.push([i, fmtDay(bars[i].t)]);
  } else {
    let prev = '';
    for (let i = i0; i <= i1; i++) {
      const m = bars[i].t.slice(0, 7);
      if (m !== prev) {
        prev = m;
        marks.push([i, `${MONTHS[Number(m.slice(5, 7)) - 1]} ${m.slice(2, 4)}`]);
      }
    }
    if (marks.length > 8) {
      const stride = Math.ceil(marks.length / 8);
      const kept = marks.filter((_, k) => k % stride === 0);
      marks.length = 0; marks.push(...kept);
    }
  }
  // thin to what the width can hold, then skip any label that would still collide
  const maxLabels = Math.max(3, Math.floor(plotW / 86));
  if (marks.length > maxLabels) {
    const stride = Math.ceil(marks.length / maxLabels);
    const kept = marks.filter((_, k) => k % stride === 0);
    marks.length = 0; marks.push(...kept);
  }
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  let lastRight = -Infinity;
  for (const [idx, label] of marks) {
    const x = Math.round(X(idx)) + 0.5;
    ctx.strokeStyle = CH.soft;
    ctx.beginPath(); ctx.moveTo(x, PADT); ctx.lineTo(x, PADT + totalH); ctx.stroke();
    const tw = ctx.measureText(label).width;
    const lx = Math.min(PADL + plotW - tw / 2 - 2, Math.max(PADL + tw / 2 + 2, x));
    if (lx - tw / 2 < lastRight + 8) continue;
    ctx.fillStyle = CH.faint;
    ctx.fillText(label, lx, PADT + totalH + 8);
    lastRight = lx + tw / 2;
  }

  // volume pane
  if (CHART.vol) {
    for (let i = i0; i <= i1; i++) {
      const b = bars[i];
      const vh = vmax ? (b.v / vmax) * volH : 0;
      ctx.fillStyle = b.c >= b.o ? CH.volUp : CH.volDown;
      ctx.fillRect(X(i) - bw / 2, volTop + volH - vh, bw, Math.max(0.5, vh));
    }
    ctx.strokeStyle = CH.soft;
    ctx.beginPath();
    ctx.moveTo(PADL, volTop + volH - 0.5);
    ctx.lineTo(PADL + plotW, volTop + volH - 0.5);
    ctx.stroke();
    ctx.fillStyle = CH.faint;
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.font = '9px "JetBrains Mono", ui-monospace, monospace';
    const volLabel = 'VOL · ' + fmtVol(vmax);
    ctx.fillStyle = 'rgba(5,7,13,.62)';
    pill(ctx, PADL, volTop + 1, ctx.measureText(volLabel).width + 6, 13, 3);
    ctx.fill();
    ctx.fillStyle = CH.faint;
    ctx.fillText(volLabel, PADL + 3, volTop + 2);
    ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
  }

  // RSI pane
  if (CHART.rsi) {
    const series = rsi14(closes);
    const RY = (v) => rsiTop + (1 - v / 100) * rsiH;
    ctx.fillStyle = 'rgba(103,232,249,.055)';
    ctx.fillRect(PADL, RY(70), plotW, RY(30) - RY(70));
    ctx.save();
    ctx.setLineDash([3, 3]); ctx.strokeStyle = CH.soft;
    for (const lvl of [30, 70]) {
      ctx.beginPath(); ctx.moveTo(PADL, RY(lvl)); ctx.lineTo(PADL + plotW, RY(lvl)); ctx.stroke();
    }
    ctx.restore();
    ctx.save();
    ctx.beginPath(); ctx.rect(PADL, rsiTop, plotW, rsiH); ctx.clip();
    ctx.strokeStyle = CH.rsi; ctx.lineWidth = 1.3;
    ctx.beginPath();
    let started = false;
    for (let i = i0; i <= i1; i++) {
      const v = series[i];
      if (v == null) { started = false; continue; }
      if (!started) { ctx.moveTo(X(i), RY(v)); started = true; } else ctx.lineTo(X(i), RY(v));
    }
    ctx.stroke();
    ctx.restore();
    ctx.lineWidth = 1;
    let lastV = null;
    for (let i = i1; i >= i0 && lastV == null; i--) lastV = series[i];
    ctx.fillStyle = CH.rsi;
    ctx.textAlign = 'left'; ctx.textBaseline = 'top';
    ctx.font = '9px "JetBrains Mono", ui-monospace, monospace';
    const rsiLabel = 'RSI 14 · ' + (lastV == null ? '—' : lastV.toFixed(1));
    ctx.fillStyle = 'rgba(5,7,13,.62)';
    pill(ctx, PADL, rsiTop + 1, ctx.measureText(rsiLabel).width + 6, 13, 3);
    ctx.fill();
    ctx.fillStyle = CH.rsi;
    ctx.fillText(rsiLabel, PADL + 3, rsiTop + 2);
    ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
  }

  // candles
  for (let i = i0; i <= i1; i++) {
    const b = bars[i], up = b.c >= b.o;
    const x = Math.round(X(i)) + 0.5;
    ctx.strokeStyle = ctx.fillStyle = up ? CH.up : CH.down;
    ctx.beginPath(); ctx.moveTo(x, Y(b.h)); ctx.lineTo(x, Y(b.l)); ctx.stroke();
    const yo = Y(b.o), yc = Y(b.c);
    ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1.2, Math.abs(yc - yo)));
  }

  // overlays, clipped to the price pane so a long MA cannot bleed into the volume
  if (overlays.length) {
    ctx.save();
    ctx.beginPath(); ctx.rect(PADL, PADT, plotW, priceH); ctx.clip();
    for (const [vals, col] of overlays) {
      ctx.strokeStyle = col; ctx.lineWidth = 1.4;
      ctx.beginPath();
      let started = false;
      for (let i = i0; i <= i1; i++) {
        const v = vals[i];
        if (v == null) { started = false; continue; }
        if (!started) { ctx.moveTo(X(i), Y(v)); started = true; } else ctx.lineTo(X(i), Y(v));
      }
      ctx.stroke();
    }
    ctx.restore();
    ctx.lineWidth = 1;
  }

  // last close: dashed line + price tag, only when the newest bar is in view
  if (lastTagY != null) {
    ctx.save();
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = lastBar.c >= lastBar.o ? CH.up : CH.down;
    ctx.beginPath(); ctx.moveTo(PADL, lastTagY); ctx.lineTo(PADL + plotW, lastTagY); ctx.stroke();
    ctx.restore();
    priceTag(ctx, PADL + plotW + 2, lastTagY, lastBar.c.toFixed(tagDp),
      lastBar.c >= lastBar.o ? CH.up : CH.down);
  }

  // crosshair: bar under the pointer, its close on the price scale, its date below
  const hv = CHART.hover;
  if (hoverBar) {
    const b = hoverBar;
    const x = Math.round(X(hv)) + 0.5;
    const y = Y(b.c);
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(236,242,251,.34)';
    ctx.beginPath(); ctx.moveTo(x, PADT); ctx.lineTo(x, PADT + totalH); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(PADL, y); ctx.lineTo(PADL + plotW, y); ctx.stroke();
    ctx.restore();
    ctx.strokeStyle = 'rgba(236,242,251,.5)';
    const top = Math.min(Y(b.h), Y(b.l)), bot = Math.max(Y(b.h), Y(b.l));
    ctx.strokeRect(Math.round(x - bw / 2) - 1.5, top - 1.5, bw + 3, Math.max(3, bot - top + 3));
    priceTag(ctx, PADL + plotW + 2, hoverTagY, b.c.toFixed(tagDp), 'rgba(236,242,251,.94)');
    const label = fmtDay(b.t);
    ctx.font = '10px "JetBrains Mono", ui-monospace, monospace';
    const tw = ctx.measureText(label).width + 12;
    const tx = Math.max(PADL, Math.min(PADL + plotW - tw, x - tw / 2));
    ctx.fillStyle = 'rgba(236,242,251,.94)';
    pill(ctx, tx, PADT + totalH + 4, tw, 16, 4);
    ctx.fill();
    ctx.fillStyle = '#05070d';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(label, tx + tw / 2, PADT + totalH + 12.5);
  }

  // header readout follows the visible window, not just the newest bar
  const firstBar = bars[i0], lastVis = bars[i1];
  const chg = ((lastVis.c - firstBar.c) / firstBar.c) * 100;
  const spanDays = Math.round((dayOf(lastVis.t) - dayOf(firstBar.t)) / 864e5);
  const suffix = spanDays >= 330 ? '1Y'
    : spanDays >= 45 ? `${Math.round(spanDays / 30.44)}M` : `${Math.max(1, spanDays)}D`;
  $('chart-sym').textContent = CHART.ticker;
  $('chart-last').textContent = lastVis.c.toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const chgEl = $('chart-chg');
  chgEl.textContent = `${chg >= 0 ? '▲ +' : '▼ '}${chg.toFixed(2)}% / ${suffix}`;
  chgEl.className = 'chart-chg ' + (chg >= 0 ? 'up' : 'down');
  $('chart-range').textContent = `${firstBar.t} → ${lastVis.t}`;
  cv.setAttribute('aria-label',
    `${CHART.ticker}: ${n} daily candles from ${firstBar.t} to ${lastVis.t}. `
    + 'Arrow keys inspect a session, plus and minus zoom, double-click resets.');
  updateOhlc();
  $('chart-empty').style.display = 'none';
}

/* Coalesce redraws to one per frame. The timer is a fallback for windows where
   rAF is throttled (occluded tab, backgrounded preview pane); without it a
   queued frame that never fires would leave the chart stale forever. */
let chartQueued = false;
function flushChart() {
  if (!chartQueued) return;
  chartQueued = false;
  renderChart();
}
function scheduleChart() {
  if (chartQueued) return;
  chartQueued = true;
  requestAnimationFrame(flushChart);
  setTimeout(flushChart, 100);
}

function drawCandles(ticker, bars) {
  CHART.ticker = ticker;
  CHART.bars = bars;
  CHART.hover = -1;
  CHART.drag = null;
  window._lastTicker = ticker;
  window._lastBars = bars;
  // A phone cannot show half a year of sessions legibly; start it on a quarter
  // instead. Only the first draw, so an explicit 6M pick is never overridden.
  if (!CHART.tuned) {
    CHART.tuned = true;
    if (($('chart').clientWidth || 0) < 480 && CHART.chip === '6M') CHART.chip = '3M';
  }
  applyRange(CHART.chip);
}

/* ── interactions: hover, wheel zoom, drag pan, keyboard ── */
function chartGeom(ev) {
  const r = $('chart').getBoundingClientRect();
  const PADL = 6, PADR = 62;
  return { x: ev.clientX - r.left, y: ev.clientY - r.top, plotW: Math.max(1, r.width - PADL - PADR), left: PADL };
}

function barAt(x, g) {
  const i0 = Math.round(CHART.i0), i1 = Math.round(CHART.i1);
  const n = Math.max(1, i1 - i0 + 1);
  const idx = i0 + Math.round(((x - g.left) / g.plotW) * n - 0.5);
  return Math.max(0, Math.min(CHART.bars.length - 1, idx));
}

function zoomBy(factor) {
  if (CHART.bars.length < 2) return;
  const last = CHART.bars.length - 1;
  const n = CHART.i1 - CHART.i0 + 1;
  const mid = (CHART.i0 + CHART.i1) / 2;
  const newN = Math.min(last + 1, Math.max(8, n * factor));
  let a = mid - newN / 2 + 0.5, b = a + newN - 1;
  if (a < 0) { b -= a; a = 0; }
  if (b > last) { a -= (b - last); b = last; }
  CHART.i0 = Math.max(0, a); CHART.i1 = Math.min(last, b);
  CHART.free = true;
  markRangeChips();
  scheduleChart();
}

const chartCanvas = $('chart');

chartCanvas.addEventListener('pointermove', (ev) => {
  if (!CHART.bars.length) return;
  const g = chartGeom(ev);
  if (CHART.drag) {
    if (Math.abs(ev.clientX - CHART.drag.x) > 3) CHART.drag.moved = true;
    const dx = (ev.clientX - CHART.drag.x) / g.plotW * CHART.drag.n;
    let a = CHART.drag.i0 - dx, b = CHART.drag.i1 - dx;
    const last = CHART.bars.length - 1, min = 7;
    if (a < 0) { b -= a; a = 0; }
    if (b > last) { a -= (b - last); b = last; }
    if (b - a < min) { b = Math.min(last, a + min); a = Math.max(0, b - min); }
    CHART.i0 = a; CHART.i1 = b;
    CHART.free = true;
    scheduleChart();
    return;
  }
  const idx = barAt(g.x, g);
  if (idx !== CHART.hover) { CHART.hover = idx; scheduleChart(); }
});

chartCanvas.addEventListener('pointerleave', () => {
  if (CHART.drag) return;
  CHART.hover = -1;
  scheduleChart();
});

chartCanvas.addEventListener('pointerdown', (ev) => {
  if (!CHART.bars.length || ev.button !== 0) return;
  try { chartCanvas.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic or already lost */ }
  CHART.drag = { x: ev.clientX, i0: CHART.i0, i1: CHART.i1, n: Math.max(1, CHART.i1 - CHART.i0 + 1), moved: false };
  chartCanvas.classList.add('is-drag');
});

function endDrag(ev, tap) {
  if (!CHART.drag) return;
  const wasTap = !CHART.drag.moved;
  CHART.drag = null;
  chartCanvas.classList.remove('is-drag');
  markRangeChips();
  // a tap (no drag) is a request to inspect that session, which is the only
  // way to read a bar on touch, where there is no hover
  if (tap && wasTap && ev && ev.clientX != null) {
    const g = chartGeom(ev);
    CHART.hover = barAt(g.x, g);
    scheduleChart();
  }
}
chartCanvas.addEventListener('pointerup', (ev) => endDrag(ev, true));
chartCanvas.addEventListener('pointercancel', (ev) => endDrag(ev, false));

chartCanvas.addEventListener('wheel', (ev) => {
  if (!CHART.bars.length) return;
  ev.preventDefault();
  const g = chartGeom(ev);
  const last = CHART.bars.length - 1;
  const n = CHART.i1 - CHART.i0 + 1;
  const frac = Math.min(1, Math.max(0, (g.x - g.left) / g.plotW));
  const pivot = CHART.i0 + frac * n;
  const newN = Math.min(last + 1, Math.max(8, n * Math.exp(ev.deltaY * 0.0016)));
  let a = pivot - frac * newN, b = a + newN - 1;
  if (a < 0) { b -= a; a = 0; }
  if (b > last) { a -= (b - last); b = last; }
  CHART.i0 = Math.max(0, a); CHART.i1 = Math.min(last, b);
  CHART.free = true;
  markRangeChips();
  scheduleChart();
}, { passive: false });

chartCanvas.addEventListener('dblclick', () => applyRange(CHART.chip));

chartCanvas.addEventListener('keydown', (ev) => {
  if (!CHART.bars.length) return;
  const i0 = Math.round(CHART.i0), i1 = Math.round(CHART.i1);
  if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') {
    ev.preventDefault();
    const base = CHART.hover < 0 ? i1 : CHART.hover;
    const step = ev.shiftKey ? 10 : 1;
    CHART.hover = Math.max(i0, Math.min(i1, base + (ev.key === 'ArrowLeft' ? -step : step)));
    scheduleChart();
  } else if (ev.key === '+' || ev.key === '=') {
    ev.preventDefault(); zoomBy(0.8);
  } else if (ev.key === '-' || ev.key === '_') {
    ev.preventDefault(); zoomBy(1.25);
  } else if (ev.key === 'Home' || ev.key === '0') {
    ev.preventDefault(); applyRange(CHART.chip);
  }
});

document.querySelectorAll('#chart-ranges .seg-btn').forEach((b) => {
  b.onclick = () => applyRange(b.dataset.range);
});
document.querySelectorAll('#chart-inds .seg-btn').forEach((b) => {
  b.onclick = () => {
    const key = b.dataset.ind;
    CHART[key] = !CHART[key];
    b.classList.toggle('is-on', CHART[key]);
    b.setAttribute('aria-pressed', CHART[key] ? 'true' : 'false');
    renderChart();
  };
});

/* Requests can land out of order (change the date twice quickly, or the
   initial load resolving after a pick), so only the newest one may draw. */
let chartSeq = 0;
async function loadChart(ticker, date) {
  const seq = ++chartSeq;
  $('chart-empty').style.display = 'flex';
  $('chart-empty').textContent = `Loading ${ticker} chart…`;
  try {
    const res = await fetch(`/api/candles?ticker=${encodeURIComponent(ticker)}&days=400`
      + `&date=${encodeURIComponent(date || '')}`);
    const data = await res.json();
    if (seq !== chartSeq) return;
    if (data.error || !data.bars || !data.bars.length) {
      $('chart-empty').textContent = data.error || 'No chart data';
      $('chart-empty').style.display = 'flex';
      return;
    }
    drawCandles(data.ticker, data.bars);
  } catch (e) {
    if (seq !== chartSeq) return;
    $('chart-empty').textContent = 'Chart unavailable';
    $('chart-empty').style.display = 'flex';
  }
}
let chartResize = 0;
window.addEventListener('resize', () => {
  clearTimeout(chartResize);
  chartResize = setTimeout(() => {
    // a hidden panel has no width to measure; showTab redraws on return
    if (CHART.bars.length && $('chart').clientWidth > 0) renderChart();
  }, 120);
});

/* The chart is anchored to the setup form: the ticker picks the instrument and
   the analysis date picks the window's end. Re-fetch whenever either settles
   (``change`` fires on blur/Enter, not per keystroke), so the graph never shows
   a different instrument or date than the run about to be launched. */
function refreshChart() {
  const ticker = ($('in-ticker').value || '').trim() || 'SPY';
  loadChart(ticker, ($('in-date') || {}).value || '');
}
$('in-ticker').addEventListener('change', refreshChart);
$('in-date').addEventListener('change', refreshChart);
refreshChart();
const _btnRun = $('btn-run');
if (_btnRun) _btnRun.addEventListener('click', refreshChart);

/* ── vendor health (on-demand only — never auto-polled, quota-safe) ── */
async function loadHealth(showPanel) {
  const pill = $('health-pill');
  if (!pill) return;
  pill.textContent = 'Checking…';
  try {
    const res = await fetch('/api/health');
    const data = await res.json();
    const vs = data.vendors || [];
    const ok = vs.filter((v) => v.status === 'ok').length;
    pill.textContent = `Sources ${ok}/${vs.length}`;
    pill.title = vs.map((v) => `${v.name}: ${v.status}${v.detail ? ' — ' + v.detail : ''}`).join('\n') + '\nClick to re-check';
    if (showPanel) showHealthPanel(vs, data.checked_at);
  } catch (e) { pill.textContent = 'Sources ?'; pill.title = 'Health check failed: ' + e.message; }
}
function showHealthPanel(vs, at) {
  let p = $('health-panel');
  if (!p) {
    p = document.createElement('div');
    p.id = 'health-panel';
    document.body.appendChild(p);
  }
  p.className = 'health-panel';
  p.innerHTML = `<div class="health-head">Vendor health · ${esc(at || '')}</div>`
    + vs.map((v) => {
      const tone = v.status === 'ok' ? 'is-done' : v.status === 'down' ? 'is-err is-live' : '';
      return `<div class="health-row">
        <span class="sq ${tone}"></span>
        <span class="health-name">${esc(v.name)}</span>
        <span class="health-state" title="${esc(v.detail || '')}">${esc(v.status)}</span>
      </div>`;
    }).join('')
    + `<div class="health-foot">On demand · cached 2 min · AV never probed</div>`;
  p.classList.remove('hidden');
  clearTimeout(p._t);
  p._t = setTimeout(() => p.classList.add('hidden'), 9000);
}
if ($('health-pill')) {
  $('health-pill').onclick = () => loadHealth(true);
  loadHealth(false); // one quiet check on page load; the panel opens on demand
}
document.addEventListener('click', (e) => {
  const panel = $('health-panel');
  const pill = $('health-pill');
  if (!panel || panel.classList.contains('hidden')) return;
  if (panel.contains(e.target) || (pill && pill.contains(e.target))) return;
  panel.classList.add('hidden');
});
