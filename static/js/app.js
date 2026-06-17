/* =====================================================================
   SCDI — Supply Chain Disruption Intelligence — Frontend Logic
   Connects to Flask backend (app.py) via REST API
   ===================================================================== */

'use strict';

/* ---------- STATE ---------- */
const state = {
  mode: null,
  selectedClientId: null,
  profiles: [],
  runResult: null,
};

/* ---------- NAVIGATION ---------- */
function showPage(id) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  window.scrollTo(0, 0);
}

function goBack(page) {
  showPage(page);
}

function selectMode(mode) {
  state.mode = mode;
  if (mode === 'existing') {
    showPage('existing');
    loadProfiles();
  } else {
    showPage('new');
    ensureOneOfEach();
  }
}

/* ---------- TOAST ---------- */
let toastTimer;
function showToast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = 'toast visible ' + type;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = 'toast'; }, 4000);
}

/* ---------- API HELPERS ---------- */
async function apiFetch(path, opts = {}) {
  try {
    const res = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return { ok: true, data };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

/* ---------- EXISTING PROFILES PAGE ---------- */
async function loadProfiles() {
  const loading = document.getElementById('profiles-loading');
  const list    = document.getElementById('profiles-list');
  const empty   = document.getElementById('profiles-empty');
  const bar     = document.getElementById('selected-profile-bar');

  loading.style.display = 'flex';
  list.style.display    = 'none';
  empty.style.display   = 'none';
  bar.style.display     = 'none';
  state.selectedClientId = null;

  const result = await apiFetch('/api/profiles');
  loading.style.display = 'none';

  if (!result.ok) { showToast('Failed to load profiles: ' + result.error, 'error'); return; }

  state.profiles = result.data;

  if (!state.profiles.length) {
    empty.style.display = 'block';
    return;
  }

  list.style.display = 'grid';
  list.innerHTML = '';
  state.profiles.forEach(p => {
    const card = document.createElement('div');
    card.className = 'profile-card';
    card.innerHTML = `
      <div class="profile-card-id">${escHtml(p.client_id)}</div>
      <div class="profile-card-meta">Updated ${formatDate(p.updated_at)}</div>
    `;
    card.addEventListener('click', () => selectProfile(p.client_id, card));
    list.appendChild(card);
  });
}

function selectProfile(clientId, card) {
  document.querySelectorAll('.profile-card').forEach(c => c.classList.remove('selected'));
  card.classList.add('selected');
  state.selectedClientId = clientId;

  const bar = document.getElementById('selected-profile-bar');
  document.getElementById('selected-client-name').textContent = clientId;
  bar.style.display = 'flex';
}

async function runExistingProfile() {
  if (!state.selectedClientId) { showToast('Select a profile first.', 'error'); return; }
  const daysBack = parseInt(document.getElementById('ex-days-back').value) || 7;
  const articlesPerQuery = parseInt(document.getElementById('ex-articles-per-query').value) || 15;
  startPipeline(state.selectedClientId, { days_back: daysBack, articles_per_query: articlesPerQuery });
}

/* ---------- NEW PROFILE PAGE ---------- */
let supplierCount = 0, materialCount = 0, facilityCount = 0;

function ensureOneOfEach() {
  if (!document.getElementById('suppliers-list').children.length) addSupplier();
  if (!document.getElementById('materials-list').children.length) addMaterial();
  if (!document.getElementById('facilities-list').children.length) addFacility();
}

function addSupplier() {
  const id = ++supplierCount;
  const div = document.createElement('div');
  div.className = 'dynamic-item';
  div.id = 'sup-' + id;
  div.innerHTML = `
    <button class="remove-btn" onclick="removeItem('sup-${id}')">×</button>
    <div class="dynamic-item-inner">
      <div class="form-group" style="flex:1.2">
        <label class="form-label">Supplier Name</label>
        <input type="text" class="form-input sup-name" placeholder="e.g. KorTech Semiconductors" />
      </div>
      <div class="form-group" style="flex:1">
        <label class="form-label">Location</label>
        <input type="text" class="form-input sup-location" placeholder="e.g. Busan, South Korea" />
      </div>
      <div class="form-group" style="flex:0.8">
        <label class="form-label">Provides</label>
        <input type="text" class="form-input sup-provides" placeholder="e.g. MCUs" />
      </div>
    </div>
  `;
  document.getElementById('suppliers-list').appendChild(div);
}

function addMaterial() {
  const id = ++materialCount;
  const div = document.createElement('div');
  div.className = 'dynamic-item';
  div.id = 'mat-' + id;
  div.innerHTML = `
    <button class="remove-btn" onclick="removeItem('mat-${id}')">×</button>
    <div class="dynamic-item-inner">
      <div class="form-group" style="flex:1">
        <label class="form-label">Commodity</label>
        <input type="text" class="form-input mat-commodity" placeholder="e.g. Lithium carbonate" />
      </div>
      <div class="form-group" style="flex:1.5">
        <label class="form-label">Origin Regions (comma-separated)</label>
        <input type="text" class="form-input mat-origins" placeholder="e.g. Chile, Argentina" />
      </div>
    </div>
  `;
  document.getElementById('materials-list').appendChild(div);
}

function addFacility() {
  const id = ++facilityCount;
  const div = document.createElement('div');
  div.className = 'dynamic-item';
  div.id = 'fac-' + id;
  div.innerHTML = `
    <button class="remove-btn" onclick="removeItem('fac-${id}')">×</button>
    <div class="dynamic-item-inner">
      <div class="form-group" style="flex:1.5">
        <label class="form-label">Location</label>
        <input type="text" class="form-input fac-location" placeholder="e.g. Rotterdam, Netherlands" />
      </div>
      <div class="form-group" style="flex:1">
        <label class="form-label">Type</label>
        <input type="text" class="form-input fac-type" placeholder="e.g. Assembly Plant" />
      </div>
    </div>
  `;
  document.getElementById('facilities-list').appendChild(div);
}

function removeItem(id) {
  const el = document.getElementById(id);
  if (el) el.remove();
}

function buildProfileFromForm() {
  const clientId = document.getElementById('f-client-id').value.trim();
  if (!clientId) throw new Error('Client ID is required.');

  const suppliers = [];
  document.querySelectorAll('#suppliers-list .dynamic-item').forEach(el => {
    const name = el.querySelector('.sup-name').value.trim();
    const loc  = el.querySelector('.sup-location').value.trim();
    if (name && loc) suppliers.push({ name, location: loc, provides: el.querySelector('.sup-provides').value.trim() });
  });

  const materials = [];
  document.querySelectorAll('#materials-list .dynamic-item').forEach(el => {
    const commodity = el.querySelector('.mat-commodity').value.trim();
    const origins   = el.querySelector('.mat-origins').value.split(',').map(s => s.trim()).filter(Boolean);
    if (commodity) materials.push({ commodity, origin_regions: origins });
  });

  const portsRaw    = document.getElementById('f-ports').value.trim();
  const carriersRaw = document.getElementById('f-carriers').value.trim();
  const ports    = portsRaw    ? portsRaw.split('\n').map(s => s.trim()).filter(Boolean) : [];
  const carriers = carriersRaw ? carriersRaw.split('\n').map(s => s.trim()).filter(Boolean) : [];

  const facilities = [];
  document.querySelectorAll('#facilities-list .dynamic-item').forEach(el => {
    const loc  = el.querySelector('.fac-location').value.trim();
    const type = el.querySelector('.fac-type').value.trim();
    if (loc) facilities.push({ location: loc, type });
  });

  if (!suppliers.length && !materials.length && !ports.length && !carriers.length && !facilities.length) {
    throw new Error('Add at least one supply chain entity.');
  }

  return {
    client_id: clientId,
    tier1_suppliers: suppliers,
    raw_materials: materials,
    logistics: { ports, carriers },
    own_facilities: facilities,
  };
}

async function submitNewProfile() {
  let profile;
  try { profile = buildProfileFromForm(); }
  catch (e) { showToast(e.message, 'error'); return; }

  const saveResult = await apiFetch('/api/profiles', {
    method: 'POST',
    body: JSON.stringify(profile),
  });

  if (!saveResult.ok) {
    showToast('Profile save failed: ' + saveResult.error, 'error');
    return;
  }

  const daysBack         = parseInt(document.getElementById('f-days-back').value) || 7;
  const articlesPerQuery = parseInt(document.getElementById('f-articles-per-query').value) || 15;
  const maxTokens        = parseInt(document.getElementById('f-max-tokens').value) || 500;

  startPipeline(profile.client_id, { days_back: daysBack, articles_per_query: articlesPerQuery, max_tokens: maxTokens });
}

/* ---------- PIPELINE RUNNER ---------- */
function startPipeline(clientId, opts = {}) {
  state.selectedClientId = clientId;
  showPage('running');

  document.getElementById('running-client-label').textContent = clientId;
  document.getElementById('log-box').innerHTML = '';

  // Reset stage items
  document.querySelectorAll('.stage-item').forEach(el => {
    el.classList.remove('active', 'done');
    el.querySelector('.stage-status').textContent = '';
  });

  activateStage(1);

  apiFetch(`/api/run/${encodeURIComponent(clientId)}`, {
    method: 'POST',
    body: JSON.stringify(opts),
  }).then(result => {
    if (!result.ok) {
      showToast('Pipeline failed: ' + result.error, 'error');
      showPage(state.mode === 'existing' ? 'existing' : 'new');
      return;
    }
    state.runResult = result.data;
    renderLogLines(result.data.log || []);
    completeAllStages();
    setTimeout(() => renderDashboard(result.data), 800);
  });

  // Animate stages based on time (mock progress while request is in flight)
  simulateStageProgress();
}

function simulateStageProgress() {
  const delays = [0, 3000, 9000, 18000, 30000, 45000];
  delays.forEach((delay, i) => {
    setTimeout(() => {
      for (let s = 1; s <= i; s++) completeStage(s);
      if (i < 6) activateStage(i + 1);
    }, delay);
  });
}

function activateStage(n) {
  const el = document.querySelector(`.stage-item[data-stage="${n}"]`);
  if (!el) return;
  el.classList.add('active');
  el.querySelector('.stage-status').textContent = '…';
}

function completeStage(n) {
  const el = document.querySelector(`.stage-item[data-stage="${n}"]`);
  if (!el) return;
  el.classList.remove('active');
  el.classList.add('done');
  el.querySelector('.stage-status').textContent = '✓';
}

function completeAllStages() {
  for (let i = 1; i <= 6; i++) completeStage(i);
}

function renderLogLines(lines) {
  const box = document.getElementById('log-box');
  box.innerHTML = '';
  lines.forEach(line => {
    const p = document.createElement('p');
    p.textContent = line;
    box.appendChild(p);
  });
  box.scrollTop = box.scrollHeight;
}

/* ---------- DASHBOARD ---------- */
function renderDashboard(data) {
  showPage('dashboard');

  // Client badge
  document.getElementById('dash-client-badge').textContent = data.client_id || state.selectedClientId;

  // KPIs
  renderKPIs(data);

  // Briefings
  const briefings = data.briefings || [];
  document.getElementById('briefing-count').textContent = briefings.length + ' events detected';
  renderBriefings(briefings);

  // Pipeline summary
  renderPipelineSummary(data);

  // Articles table
  const scored = data.scored_articles || [];
  document.getElementById('article-count').textContent = scored.length + ' articles analysed';
  renderArticlesTable(scored);
}

function renderKPIs(data) {
  const pf   = data.prefilter || {};
  const ds   = data.deep_score || {};
  const dd   = data.dedup || {};
  const brf  = (data.briefings || []);

  const kpis = [
    { label: 'Articles Scraped',    val: data.n_scraped_ok ?? '—',     sub: 'successfully retrieved', color: 'indigo' },
    { label: 'Pre-filter Passed',   val: pf.n_passed ?? '—',           sub: `of ${pf.n_input ?? '?'} input`, color: 'cyan' },
    { label: 'Disruptions Found',   val: ds.n_disruptions ?? '—',      sub: 'confirmed by Nemotron', color: 'amber' },
    { label: 'Unique Events Kept',  val: dd.n_kept ?? '—',             sub: `${dd.n_dropped ?? 0} duplicates removed`, color: 'green' },
    { label: 'Event Clusters',      val: dd.n_clusters ?? brf.length ?? '—', sub: 'briefings generated', color: 'indigo' },
    { label: 'LLM Verifications',   val: dd.n_llm_verification_calls ?? '—', sub: 'dedup calls made', color: 'cyan' },
  ];

  const row = document.getElementById('kpi-row');
  row.innerHTML = '';
  kpis.forEach((k, i) => {
    const card = document.createElement('div');
    card.className = `kpi-card ${k.color}`;
    card.style.animationDelay = (i * 0.08) + 's';
    card.innerHTML = `
      <div class="kpi-label">${k.label}</div>
      <div class="kpi-val">${k.val}</div>
      <div class="kpi-sub">${k.sub}</div>
    `;
    row.appendChild(card);
  });
}

function renderBriefings(briefings) {
  const grid = document.getElementById('briefings-grid');
  grid.innerHTML = '';

  if (!briefings.length) {
    grid.innerHTML = '<div class="empty-state"><div class="empty-icon">◎</div><div class="empty-title">No disruptions detected</div><div class="empty-desc">The pipeline found no supply chain disruptions in the selected period.</div></div>';
    return;
  }

  briefings.forEach((b, i) => {
    const sev = b.severity || 'LOW';
    const card = document.createElement('div');
    card.className = `briefing-card sev-${sev}`;
    card.style.animationDelay = (i * 0.1) + 's';

    const sources = (b.sources || []).slice(0, 5).map(s =>
      `<a class="source-tag" href="${escHtml(s.url || '#')}" target="_blank" rel="noopener">${escHtml(s.domain || 'unknown')}</a>`
    ).join('');

    card.innerHTML = `
      <div class="briefing-sev-bar"></div>
      <div class="briefing-header">
        <div class="briefing-headline">${escHtml(b.headline || 'Untitled Event')}</div>
        <span class="sev-badge ${sev}">${sev}</span>
      </div>
      <div class="briefing-brief">${escHtml(b.brief || 'No briefing text available.')}</div>
      ${sources ? `
        <div class="briefing-sources-label">Sources</div>
        <div class="briefing-source-list">${sources}</div>
      ` : ''}
    `;
    grid.appendChild(card);
  });
}

function renderPipelineSummary(data) {
  const pf = data.prefilter || {};
  const ds = data.deep_score || {};
  const dd = data.dedup || {};

  const nRows       = data.n_rows || 0;
  const nScraped    = data.n_scraped_ok || 0;
  const nPassed     = pf.n_passed || 0;
  const nDisruption = ds.n_disruptions || 0;
  const nKept       = dd.n_kept || 0;

  const stages = [
    { label: 'BigQuery Rows',     val: nRows,       pct: 100,                                  sub: `${(data.bytes_scanned || 0) / 1e9 < 0.001 ? '<0.001' : ((data.bytes_scanned || 0) / 1e9).toFixed(3)} GB scanned` },
    { label: 'Scraped OK',        val: nScraped,    pct: nRows    ? (nScraped/nRows*100)    : 0, sub: `${data.n_filtered_domain || 0} domain-filtered` },
    { label: 'Pre-filter Passed', val: nPassed,     pct: nScraped ? (nPassed/nScraped*100)  : 0, sub: `${pf.n_dropped || 0} dropped` },
    { label: 'Deep Score Disrupt',val: nDisruption, pct: nPassed  ? (nDisruption/nPassed*100): 0, sub: `threshold ≥ 0.65` },
    { label: 'Dedup Kept',        val: nKept,       pct: nDisruption ? (nKept/nDisruption*100): 0, sub: `${dd.n_dropped || 0} duplicates removed` },
  ];

  const grid = document.getElementById('pipeline-summary-grid');
  grid.innerHTML = '';
  stages.forEach((s, i) => {
    const card = document.createElement('div');
    card.className = 'pipe-card';
    card.style.animationDelay = (i * 0.07) + 's';
    const pct = Math.min(100, Math.max(0, s.pct || 0));
    card.innerHTML = `
      <div class="pipe-card-label">${s.label}</div>
      <div class="pipe-card-val">${s.val}</div>
      <div class="pipe-funnel-row">
        <div class="funnel-bar"><div class="funnel-fill" style="width:${pct}%"></div></div>
        <span>${pct.toFixed(0)}%</span>
      </div>
      <div style="font-size:0.7rem;color:var(--text-muted);margin-top:0.2rem">${s.sub}</div>
    `;
    grid.appendChild(card);
  });
}

function renderArticlesTable(scored) {
  const tbody = document.getElementById('articles-tbody');
  tbody.innerHTML = '';

  if (!scored.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2rem;color:var(--text-muted)">No articles to display.</td></tr>';
    return;
  }

  scored.forEach(a => {
    const ext = a.extraction || {};
    const score = ext.relevance_score ?? 0;
    const isDisruption = ext.is_disruption;
    const sev = ext.severity || '';
    const sevColor = sev === 'HIGH' ? 'var(--crimson)' : sev === 'MEDIUM' ? 'var(--amber)' : sev === 'LOW' ? 'var(--green)' : 'var(--text-muted)';

    const scoreColor = score >= 0.65 ? 'var(--crimson)' : score >= 0.4 ? 'var(--amber)' : 'var(--green)';

    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="td-source">${escHtml(a.domain || '—')}</td>
      <td class="td-date">${escHtml(formatDateShort(a.gkg_date || ''))}</td>
      <td class="td-anchor" title="${escHtml(a.anchor || '')}">${escHtml(a.anchor || '—')}</td>
      <td class="td-event"><span style="color:var(--text-dim)">${escHtml(ext.event_type || '—')}</span></td>
      <td><span style="color:${sevColor};font-family:var(--mono);font-size:0.72rem">${sev || '—'}</span></td>
      <td>
        <div class="score-bar-wrap">
          <div class="score-bar-bg"><div class="score-bar-fill" style="width:${(score*100).toFixed(0)}%;background:${scoreColor}"></div></div>
          <span class="score-text" style="color:${scoreColor}">${score.toFixed(2)}</span>
        </div>
      </td>
      <td>${isDisruption ? '<span class="tag-yes">● yes</span>' : '<span class="tag-no">○ no</span>'}</td>
    `;
    tbody.appendChild(tr);
  });
}

/* ---------- UTILS ---------- */
function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium' }).format(new Date(iso));
  } catch { return iso; }
}

function formatDateShort(raw) {
  if (!raw) return '—';
  // GDELT date can be YYYYMMDDHHMMSS (14 chars) or ISO
  if (/^\d{14}$/.test(raw)) {
    return raw.substring(0, 4) + '-' + raw.substring(4, 6) + '-' + raw.substring(6, 8);
  }
  try {
    return new Intl.DateTimeFormat('en-GB', { dateStyle: 'short' }).format(new Date(raw));
  } catch { return raw; }
}

/* ---------- DEMO MODE ----------
   When the backend is not available (e.g. static file preview), populate
   the dashboard with realistic-looking sample data so the UI is fully
   explorable. The demo triggers automatically on network failure.
   -------------------------------------------------------------------- */
function loadDemoData() {
  showToast('Running in demo mode — backend not connected.', '');
  const demo = {
    client_id: 'demo-client',
    n_scraped_ok: 142,
    n_rows: 314,
    n_filtered_domain: 88,
    bytes_scanned: 4.2e9,
    prefilter: { n_input: 142, n_passed: 67, n_dropped: 75 },
    deep_score: { n_input: 67, n_disruptions: 18 },
    dedup: { n_input: 18, n_kept: 11, n_dropped: 7, n_clusters: 5, n_llm_verification_calls: 6 },
    briefings: [
      {
        severity: 'HIGH',
        headline: 'Penang Flash Floods Halt Semiconductor Fabs',
        brief: 'Severe flash flooding across Penang, Malaysia has forced multiple semiconductor fabrication facilities to suspend operations for at least 48 hours. Your Tier-1 supplier KorTech operates an assembly facility in Penang Industrial Estate directly in the affected zone. Logistics partners confirm road access to Bayan Lepas is blocked. Recovery timeline is uncertain; elevated risk of MCU supply shortfall within 2 weeks.',
        sources: [
          { domain: 'reuters.com', url: 'https://reuters.com', gkg_date: '20241214' },
          { domain: 'thestar.com.my', url: 'https://thestar.com.my', gkg_date: '20241214' },
          { domain: 'channelnewsasia.com', url: 'https://channelnewsasia.com', gkg_date: '20241215' },
        ],
      },
      {
        severity: 'HIGH',
        headline: 'Chilean Lithium Workers Strike Enters Day 5',
        brief: 'A strike at SQM\'s Atacama lithium operations entered its fifth day with no resolution in sight, according to trade union statements. Spot prices for lithium carbonate have risen 8% on the news. Your battery-grade lithium carbonate sourcing from Chile is exposed; the Atacama operation accounts for an estimated 34% of global supply in this grade. Contractual minimums may be at risk if the strike extends beyond two weeks.',
        sources: [
          { domain: 'reuters.com', url: 'https://reuters.com', gkg_date: '20241213' },
          { domain: 'mining.com', url: 'https://mining.com', gkg_date: '20241213' },
          { domain: 'miningweekly.com', url: 'https://miningweekly.com', gkg_date: '20241214' },
        ],
      },
      {
        severity: 'MEDIUM',
        headline: 'Port of Rotterdam Congestion: 3-Day Delay',
        brief: 'An influx of diverted vessels from the Suez Canal routing changes has created significant congestion at the Port of Rotterdam. Container dwell times are averaging 3.2 days above normal, with Maersk publishing a service advisory. Your Rotterdam transshipment lane is impacted; vessels carrying components from East Asia will experience corresponding delays. The port authority expects congestion to ease within 10 days.',
        sources: [
          { domain: 'freightwaves.com', url: 'https://freightwaves.com', gkg_date: '20241212' },
          { domain: 'lloydslist.com', url: 'https://lloydslist.com', gkg_date: '20241212' },
        ],
      },
      {
        severity: 'MEDIUM',
        headline: 'US-China Semiconductor Export Controls Expanded',
        brief: 'The US Department of Commerce announced an expansion of semiconductor export controls targeting advanced logic chips and EDA software. KorTech\'s US-origin manufacturing equipment licences may be subject to review under the updated Entity List criteria. Legal counsel should assess whether your existing supply agreements include force-majeure provisions covering regulatory action. No immediate supply interruption is indicated, but medium-term sourcing diversification is recommended.',
        sources: [
          { domain: 'wsj.com', url: 'https://wsj.com', gkg_date: '20241211' },
          { domain: 'ft.com', url: 'https://ft.com', gkg_date: '20241211' },
          { domain: 'bloomberg.com', url: 'https://bloomberg.com', gkg_date: '20241212' },
        ],
      },
      {
        severity: 'LOW',
        headline: 'Typhoon Gaemi Tracking Toward Taiwan Strait',
        brief: 'Typhoon Gaemi is forecast to pass through the Taiwan Strait within 72 hours. While current trajectory models suggest the storm will weaken before making landfall, shipping through the strait may experience 24–48 hour delays. Your carrier Evergreen operates several services through this lane; anticipate advisory notices. No facility exposure is indicated at this time.',
        sources: [
          { domain: 'reuters.com', url: 'https://reuters.com', gkg_date: '20241210' },
          { domain: 'japantimes.co.jp', url: 'https://japantimes.co.jp', gkg_date: '20241210' },
        ],
      },
    ],
    scored_articles: Array.from({ length: 20 }, (_, i) => ({
      domain: ['reuters.com','thestar.com.my','freightwaves.com','bloomberg.com','ft.com','wsj.com','nst.com.my','japantimes.co.jp'][i % 8],
      gkg_date: '2024121' + (i % 9),
      anchor: ['KorTech', 'Lithium carbonate / Chile', 'Port of Rotterdam', 'Maersk', 'Penang'][i % 5],
      extraction: {
        relevance_score: [0.91, 0.87, 0.76, 0.62, 0.44, 0.31, 0.78, 0.83, 0.55, 0.70, 0.88, 0.65, 0.29, 0.91, 0.77, 0.48, 0.82, 0.60, 0.38, 0.73][i],
        is_disruption: [true,true,true,false,false,false,true,true,false,true,true,true,false,true,true,false,true,false,false,true][i],
        event_type: ['NATURAL_DISASTER','LABOR_STRIKE','LOGISTICS_DELAY','NONE','NONE','NONE','TRADE_POLICY','LOGISTICS_DELAY','NONE','FACILITY_INCIDENT','NATURAL_DISASTER','LABOR_STRIKE','NONE','TRADE_POLICY','LOGISTICS_DELAY','NONE','GEOPOLITICAL','NONE','NONE','LOGISTICS_DELAY'][i],
        severity: ['HIGH','HIGH','MEDIUM','NONE','NONE','NONE','MEDIUM','MEDIUM','NONE','HIGH','HIGH','MEDIUM','NONE','MEDIUM','MEDIUM','NONE','LOW','NONE','NONE','LOW'][i],
      },
    })),
    log: [
      'Generated 12 queries for client \'demo-client\'',
      'Mode: FILTERED (trusted domains only)',
      'Trusted domains: 14 global, 4 country-level, 2 local, 19 trade press (39 total)',
      '[1/12] entity :: KorTech Semiconductors',
      '[4/12] geo :: Penang, Malaysia',
      '[8/12] commodity :: Lithium carbonate / Chile',
      '[12/12] logistics :: Port of Rotterdam',
      'Done. 314 rows returned, 230 attempted, 142 scraped successfully (4.200 GB scanned).',
      'Stage 3: pre-filtering 142 scraped articles…',
      'Pre-filter: 142/142 scored (67 passed so far)',
      'Stage 4: deep scoring 67 articles with Nemotron…',
      'Deep scoring: 67/67 analyzed (18 confirmed disruptions so far)',
      'Stage 5: deduplicating 18 confirmed disruptions…',
      'Deduplication: 18 disruption articles -> 5 clusters -> 11 unique articles kept (6 total LLM verification calls)',
      'Stage 6: generating briefings for 5 event clusters…',
      'Briefing 1/5 generated: Penang Flash Floods Halt Semiconductor Fabs',
      'Briefing 5/5 generated: Typhoon Gaemi Tracking Toward Taiwan Strait',
    ],
  };

  state.selectedClientId = 'demo-client';
  state.runResult = demo;
  renderLogLines(demo.log);
  completeAllStages();
  setTimeout(() => renderDashboard(demo), 600);
}

/* ---------- BOOT ---------- */
document.addEventListener('DOMContentLoaded', () => {
  // Test backend connectivity; if it fails, show demo on pipeline attempt
  window._backendAvailable = null;
  fetch('/api/profiles', { method: 'GET' })
    .then(r => { window._backendAvailable = r.ok; })
    .catch(() => { window._backendAvailable = false; });

  // Override startPipeline for demo mode
  const _origStartPipeline = startPipeline;
  window._realStartPipeline = _origStartPipeline;

  window.startPipeline = function(clientId, opts) {
    if (window._backendAvailable === false) {
      showPage('running');
      document.getElementById('running-client-label').textContent = clientId;
      document.getElementById('log-box').innerHTML = '';
      document.querySelectorAll('.stage-item').forEach(el => {
        el.classList.remove('active', 'done');
        el.querySelector('.stage-status').textContent = '';
      });
      activateStage(1);
      simulateStageProgress();
      setTimeout(loadDemoData, 4000);
    } else {
      _origStartPipeline(clientId, opts);
    }
  };
});