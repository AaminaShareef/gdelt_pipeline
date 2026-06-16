/**
 * SCDI Frontend — script.js
 *
 * Covers:
 *  - Client profile CRUD (GET/POST/PUT/DELETE /api/clients)
 *  - Stage 1: /api/clients/:id/queries  (POST)
 *  - Stage 2: /api/clients/:id/fetch    (POST with queries from Stage 1)
 *
 * State is held in memory only — no localStorage.
 * The server (SQLite via db.py) is the source of truth.
 */

"use strict";

// ── State ────────────────────────────────────────────────────────────
let activeClientId  = null;   // currently selected client
let generatedQueries = [];    // Stage 1 output, passed into Stage 2

// ── DOM refs ─────────────────────────────────────────────────────────
const clientList          = document.getElementById("client-list");
const btnNewProfile       = document.getElementById("btn-new-profile");
const btnSaveProfile      = document.getElementById("btn-save-profile");
const btnCancel           = document.getElementById("btn-cancel");
const clientNameInput     = document.getElementById("client-name");
const formTitle           = document.getElementById("form-title");
const pipelineClientLabel = document.getElementById("pipeline-client-label");
const btnRunQueries       = document.getElementById("btn-run-queries");
const btnRunFetch         = document.getElementById("btn-run-fetch");
const stage1Output        = document.getElementById("stage-1-output");
const stage2Output        = document.getElementById("stage-2-output");
const toastEl             = document.getElementById("toast");

// Header stage dots
const stageDots = document.querySelectorAll(".stage-dot");

// ── Section config ───────────────────────────────────────────────────
const SECTIONS = {
  suppliers: {
    fields: ["name", "supplies", "location"],
    tpl: "tpl-supplier",
  },
  materials: {
    fields: ["name", "sourced_from"],
    tpl: "tpl-material",
  },
  logistics_nodes: {
    fields: ["name", "type", "role", "location"],
    tpl: "tpl-logistics_nodes",
  },
  facilities: {
    fields: ["name", "location"],
    tpl: "tpl-facility",
  },
};

// ─────────────────────────────────────────────────────────────────────
// UTILITIES
// ─────────────────────────────────────────────────────────────────────

function toast(msg, type = "ok") {
  toastEl.textContent = msg;
  toastEl.className = `toast show ${type}`;
  clearTimeout(toastEl._t);
  toastEl._t = setTimeout(() => (toastEl.className = "toast"), 2800);
}

async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { "Content-Type": "application/json" },
  };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function setActiveStage(n) {
  stageDots.forEach((dot, i) => {
    dot.classList.remove("active", "done");
    if (i + 1 < n) dot.classList.add("done");
    if (i + 1 === n) dot.classList.add("active");
  });
}

function setRunning(card, running) {
  card.classList.toggle("stage-running", running);
}

// ─────────────────────────────────────────────────────────────────────
// FORM HELPERS
// ─────────────────────────────────────────────────────────────────────

const TPL_ID = {
  suppliers:       "tpl-supplier",
  materials:       "tpl-material",
  logistics_nodes: "tpl-logistics_nodes",
  facilities:      "tpl-facility",
};

function addRow(section) {
  const container = document.getElementById(`${section}-rows`);
  const tplId = TPL_ID[section] || `tpl-${section}`;
  const tpl = document.getElementById(tplId);
  if (!tpl || !container) { console.error("addRow: missing tpl/container for", section); return; }
  const clone = tpl.content.cloneNode(true);
  container.appendChild(clone);
  const row = container.lastElementChild;
  row.querySelector(".btn-remove").addEventListener("click", () => row.remove());
  const firstInput = row.querySelector("input");
  if (firstInput) firstInput.focus();
}

function clearForm() {
  clientNameInput.value = "";
  Object.keys(SECTIONS).forEach((section) => {
    document.getElementById(`${section}-rows`).innerHTML = "";
  });
}

function readProfile() {
  const profile = { client_name: clientNameInput.value.trim() };
  Object.entries(SECTIONS).forEach(([section, cfg]) => {
    const rows = document.querySelectorAll(`#${section}-rows .entity-row`);
    profile[section] = Array.from(rows).map((row) => {
      const obj = {};
      cfg.fields.forEach((f) => {
        obj[f] = (row.querySelector(`[data-field="${f}"]`)?.value || "").trim();
      });
      return obj;
    }).filter((obj) => obj.name);           // skip rows with no name
  });
  return profile;
}

function fillForm(profile) {
  clientNameInput.value = profile.client_name || "";
  Object.entries(SECTIONS).forEach(([section, cfg]) => {
    const container = document.getElementById(`${section}-rows`);
    container.innerHTML = "";
    const items = profile[section] || [];
    items.forEach((item) => {
      const tplId = TPL_ID[section] || `tpl-${section}`;
      const tpl = document.getElementById(tplId);
      const clone = tpl.content.cloneNode(true);
      container.appendChild(clone);
      const row = container.lastElementChild;
      cfg.fields.forEach((f) => {
        const inp = row.querySelector(`[data-field="${f}"]`);
        if (inp) inp.value = item[f] || "";
      });
      row.querySelector(".btn-remove").addEventListener("click", () => row.remove());
    });
  });
}

// ─────────────────────────────────────────────────────────────────────
// CLIENT LIST
// ─────────────────────────────────────────────────────────────────────

async function loadClientList() {
  try {
    const clients = await api("GET", "/api/clients");
    renderClientList(clients);
  } catch (e) {
    clientList.innerHTML = `<p class="empty-state" style="color:var(--severity-high)">${e.message}</p>`;
  }
}

function renderClientList(clients) {
  if (!clients.length) {
    clientList.innerHTML = '<p class="empty-state">No profiles yet. Create one →</p>';
    return;
  }
  clientList.innerHTML = "";
  clients.forEach((c) => {
    const el = document.createElement("div");
    el.className = "client-item" + (c.id === activeClientId ? " selected" : "");
    el.dataset.id = c.id;

    const dateStr = c.updated_at
      ? new Date(c.updated_at).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "2-digit" })
      : "";

    el.innerHTML = `
      <div class="client-item-name">${c.client_name}</div>
      <div class="client-item-date">${dateStr}</div>
      <div class="client-item-actions">
        <button class="btn-tiny edit-btn">Edit</button>
        <button class="btn-tiny danger delete-btn">Delete</button>
      </div>
    `;
    el.querySelector(".edit-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      loadClientForEdit(c.id);
    });
    el.querySelector(".delete-btn").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteClient(c.id, c.client_name);
    });
    el.addEventListener("click", () => selectClient(c.id, c.client_name));
    clientList.appendChild(el);
  });
}

async function selectClient(id, name) {
  activeClientId = id;
  pipelineClientLabel.textContent = name;
  btnRunQueries.disabled = false;

  // Reset Stage 2 until Stage 1 is re-run
  generatedQueries = [];
  btnRunFetch.disabled = true;
  stage1Output.innerHTML = "";
  stage2Output.innerHTML = "";
  setActiveStage(1);

  // Highlight in list
  document.querySelectorAll(".client-item").forEach((el) => {
    el.classList.toggle("selected", Number(el.dataset.id) === id);
  });
}

async function loadClientForEdit(id) {
  try {
    const record = await api("GET", `/api/clients/${id}`);
    fillForm(record.profile);
    activeClientId = id;
    formTitle.textContent = `Edit — ${record.client_name}`;
    btnCancel.style.display = "";
    selectClient(id, record.client_name);
  } catch (e) {
    toast(e.message, "err");
  }
}

async function deleteClient(id, name) {
  if (!confirm(`Delete profile for "${name}"?`)) return;
  try {
    await api("DELETE", `/api/clients/${id}`);
    toast(`Deleted "${name}"`, "ok");
    if (activeClientId === id) {
      activeClientId = null;
      pipelineClientLabel.textContent = "— no client selected —";
      btnRunQueries.disabled = true;
      btnRunFetch.disabled = true;
      stage1Output.innerHTML = "";
      stage2Output.innerHTML = "";
      clearForm();
      formTitle.textContent = "New Client Profile";
      btnCancel.style.display = "none";
    }
    loadClientList();
  } catch (e) {
    toast(e.message, "err");
  }
}

// ─────────────────────────────────────────────────────────────────────
// SAVE PROFILE
// ─────────────────────────────────────────────────────────────────────

async function saveProfile() {
  const profile = readProfile();
  if (!profile.client_name) { toast("Enter a client name first", "err"); return; }

  try {
    let record;
    if (activeClientId) {
      record = await api("PUT", `/api/clients/${activeClientId}`, profile);
      toast(`Saved "${record.client_name}"`, "ok");
    } else {
      record = await api("POST", "/api/clients", profile);
      toast(`Created "${record.client_name}"`, "ok");
    }
    activeClientId = record.id;
    formTitle.textContent = `Edit — ${record.client_name}`;
    btnCancel.style.display = "";
    pipelineClientLabel.textContent = record.client_name;
    btnRunQueries.disabled = false;
    loadClientList();
  } catch (e) {
    toast(e.message, "err");
  }
}

// ─────────────────────────────────────────────────────────────────────
// STAGE 1 — QUERY GENERATION
// ─────────────────────────────────────────────────────────────────────

async function runQueryGeneration() {
  if (!activeClientId) return;
  const card = document.getElementById("stage-1-card");
  setRunning(card, true);
  setActiveStage(1);
  stage1Output.innerHTML = `<p class="log-line info"><span class="spinner"></span>Generating queries via Llama 3.3 70B…</p>`;
  btnRunQueries.disabled = true;

  try {
    const data = await api("POST", `/api/clients/${activeClientId}/queries`);
    generatedQueries = data.queries || [];
    renderQueries(generatedQueries);
    setActiveStage(2);
    btnRunFetch.disabled = false;
    toast(`${generatedQueries.length} queries generated`, "ok");
  } catch (e) {
    stage1Output.innerHTML = `<p class="log-line err">✗ ${e.message}</p>`;
    toast(e.message, "err");
  } finally {
    setRunning(card, false);
    btnRunQueries.disabled = false;
  }
}

const ENTITY_COLORS = {
  supplier: "var(--supplier)",
  material: "var(--material)",
  logistics: "var(--logistics)",
  facility: "var(--facility)",
};

function renderQueries(queries) {
  stage1Output.innerHTML = "";
  queries.forEach((q) => {
    const color = ENTITY_COLORS[q.entity_type] || "var(--text-muted)";
    const chip = document.createElement("div");
    chip.className = "query-chip";
    chip.innerHTML = `
      <div class="query-chip-header">
        <span class="section-tag" style="background:${color}20;color:${color}">
          ${q.entity_type}
        </span>
        <span class="query-chip-entity">${q.entity_name}</span>
        <span class="log-line" style="margin-left:auto">${q.search_route}</span>
      </div>
      <div class="query-chip-kw">${q.bigquery_keywords || q.doc_api_query}</div>
    `;
    stage1Output.appendChild(chip);
  });
}

// ─────────────────────────────────────────────────────────────────────
// STAGE 2 — GDELT FETCH
// ─────────────────────────────────────────────────────────────────────

async function runGdeltFetch() {
  if (!activeClientId || !generatedQueries.length) return;
  const card = document.getElementById("stage-2-card");
  setRunning(card, true);
  setActiveStage(2);
  stage2Output.innerHTML = `<p class="log-line info"><span class="spinner"></span>Fetching from GDELT (BigQuery + DOC API)…</p>`;
  btnRunFetch.disabled = true;

  try {
    const data = await api("POST", `/api/clients/${activeClientId}/fetch`, {
      queries: generatedQueries,
    });
    renderFetchResults(data);
    setActiveStage(3);
    toast(`${data.article_count} articles fetched`, "ok");
  } catch (e) {
    stage2Output.innerHTML = `<p class="log-line err">✗ ${e.message}</p>`;
    toast(e.message, "err");
  } finally {
    setRunning(card, false);
    btnRunFetch.disabled = false;
  }
}

function renderFetchResults(data) {
  stage2Output.innerHTML = "";

  // Summary block
  const summary = document.createElement("div");
  summary.className = "fetch-summary";
  summary.innerHTML = `
    <div class="fetch-summary-num">${data.article_count}</div>
    <div class="fetch-summary-label">articles retrieved from GDELT</div>
  `;

  // Breakdown by entity type
  if (data.articles && data.articles.length) {
    const counts = {};
    data.articles.forEach((a) => {
      const types = Array.isArray(a.entity_type) ? a.entity_type : [a.entity_type];
      types.forEach((t) => { counts[t] = (counts[t] || 0) + 1; });
    });
    const breakdown = document.createElement("div");
    breakdown.className = "fetch-entity-breakdown";
    Object.entries(counts).forEach(([type, count]) => {
      const color = ENTITY_COLORS[type] || "var(--text-muted)";
      breakdown.innerHTML += `
        <div class="fetch-entity-row">
          <span style="color:${color}">${type}</span>
          <span>${count}</span>
        </div>
      `;
    });
    summary.appendChild(breakdown);
  }

  stage2Output.appendChild(summary);

  // Show save path if available
  if (data.saved_to) {
    const saved = document.createElement("p");
    saved.className = "log-line ok";
    saved.textContent = `✓ Saved → ${data.saved_to}`;
    stage2Output.appendChild(saved);
  }

  // Sample articles (first 5)
  if (data.articles && data.articles.length) {
    const label = document.createElement("p");
    label.className = "log-line";
    label.textContent = "Sample articles:";
    stage2Output.appendChild(label);

    data.articles.slice(0, 5).forEach((art) => {
      const chip = document.createElement("div");
      chip.className = "query-chip";
      const tone = typeof art.tone === "number" ? art.tone.toFixed(1) : "—";
      const toneColor = art.tone < -5 ? "var(--severity-high)"
                      : art.tone < -2 ? "var(--severity-medium)"
                      : "var(--text-muted)";
      const entities = Array.isArray(art.entity_name)
        ? art.entity_name.join(", ")
        : (art.entity_name || "");
      chip.innerHTML = `
        <div class="query-chip-header">
          <span class="query-chip-entity" style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            ${art.source || "Unknown source"}
          </span>
          <span class="log-line" style="margin-left:auto;color:${toneColor}">tone ${tone}</span>
        </div>
        <div class="query-chip-kw" style="font-size:10px;word-break:break-all">
          <a href="${art.url}" target="_blank" rel="noopener"
             style="color:var(--accent);text-decoration:none">
            ${art.url.length > 70 ? art.url.slice(0, 70) + "…" : art.url}
          </a>
        </div>
        ${entities ? `<div class="log-line" style="margin-top:4px">↳ ${entities}</div>` : ""}
      `;
      stage2Output.appendChild(chip);
    });

    if (data.article_count > 5) {
      const more = document.createElement("p");
      more.className = "log-line";
      more.textContent = `… and ${data.article_count - 5} more saved to disk`;
      stage2Output.appendChild(more);
    }
  }
}

// ─────────────────────────────────────────────────────────────────────
// EVENT BINDINGS
// ─────────────────────────────────────────────────────────────────────

btnNewProfile.addEventListener("click", () => {
  activeClientId = null;
  clearForm();
  formTitle.textContent = "New Client Profile";
  btnCancel.style.display = "none";
  pipelineClientLabel.textContent = "— no client selected —";
  btnRunQueries.disabled = true;
  btnRunFetch.disabled = true;
  stage1Output.innerHTML = "";
  stage2Output.innerHTML = "";
  generatedQueries = [];
  setActiveStage(1);
  clientNameInput.focus();
});

btnCancel.addEventListener("click", () => {
  if (!activeClientId) return;
  // Re-load the saved profile discarding edits
  loadClientForEdit(activeClientId);
  btnCancel.style.display = "none";
});

btnSaveProfile.addEventListener("click", saveProfile);

document.querySelectorAll(".btn-add").forEach((btn) => {
  btn.addEventListener("click", () => addRow(btn.dataset.section));
});

btnRunQueries.addEventListener("click", runQueryGeneration);
btnRunFetch.addEventListener("click", runGdeltFetch);

// Keyboard shortcut: Ctrl/Cmd+S to save
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    saveProfile();
  }
});

// ─────────────────────────────────────────────────────────────────────
// INIT
// ─────────────────────────────────────────────────────────────────────

loadClientList();