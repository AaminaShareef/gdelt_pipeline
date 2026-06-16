// ============================================================================
// script.js
// Builds the client profile from user input (replacing the old hardcoded
// CLIENT_PROFILE dict), submits it to the Flask API, and renders the
// dashboard from whatever the pipeline run returns.
// ============================================================================

const state = {
  profiles: [],
  activeClientId: null,
};

// ---------------------------------------------------------------------------
// DYNAMIC ROW BUILDERS
// ---------------------------------------------------------------------------
function addRow(containerId, fields, removable = true) {
  const container = document.getElementById(containerId);
  const row = document.createElement("div");
  row.className = "entity-row" + (fields.length === 1 ? " single-col" : "");

  fields.forEach((f) => {
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = f.placeholder;
    input.dataset.field = f.name;
    row.appendChild(input);
  });

  if (removable) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-remove";
    btn.textContent = "✕";
    btn.addEventListener("click", () => row.remove());
    row.appendChild(btn);
  }

  container.appendChild(row);
  return row;
}

function readRows(containerId, fieldNames) {
  const container = document.getElementById(containerId);
  const rows = Array.from(container.querySelectorAll(".entity-row"));
  return rows
    .map((row) => {
      const entry = {};
      fieldNames.forEach((name) => {
        const input = row.querySelector(`[data-field="${name}"]`);
        entry[name] = input ? input.value.trim() : "";
      });
      return entry;
    })
    .filter((entry) => Object.values(entry).some((v) => v !== ""));
}

document.querySelectorAll("[data-add]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const type = btn.dataset.add;
    if (type === "supplier") {
      addRow("suppliers-list", [
        { name: "name", placeholder: "Supplier name" },
        { name: "provides", placeholder: "What they provide" },
        { name: "location", placeholder: "Location" },
      ]);
    } else if (type === "material") {
      addRow("materials-list", [
        { name: "commodity", placeholder: "Commodity" },
        { name: "origin_regions", placeholder: "Origin region(s), comma-separated" },
      ]);
    } else if (type === "port") {
      addRow("ports-list", [{ name: "port", placeholder: "Port name" }]);
    } else if (type === "carrier") {
      addRow("carriers-list", [{ name: "carrier", placeholder: "Carrier name" }]);
    } else if (type === "facility") {
      addRow("facilities-list", [
        { name: "location", placeholder: "Facility location" },
        { name: "type", placeholder: "Facility type (e.g. plant, hub)" },
      ]);
    }
  });
});

// Start each section with one empty row so the form isn't blank.
addRow("suppliers-list", [
  { name: "name", placeholder: "Supplier name" },
  { name: "provides", placeholder: "What they provide" },
  { name: "location", placeholder: "Location" },
]);
addRow("materials-list", [
  { name: "commodity", placeholder: "Commodity" },
  { name: "origin_regions", placeholder: "Origin region(s), comma-separated" },
]);
addRow("ports-list", [{ name: "port", placeholder: "Port name" }]);
addRow("carriers-list", [{ name: "carrier", placeholder: "Carrier name" }]);
addRow("facilities-list", [
  { name: "location", placeholder: "Facility location" },
  { name: "type", placeholder: "Facility type (e.g. plant, hub)" },
]);

// ---------------------------------------------------------------------------
// BUILD PROFILE FROM FORM (this is the hardcoded-profile replacement)
// ---------------------------------------------------------------------------
function buildProfileFromForm() {
  const clientId = document.getElementById("client_id").value.trim();

  const suppliers = readRows("suppliers-list", ["name", "provides", "location"]);

  const materials = readRows("materials-list", ["commodity", "origin_regions"]).map((m) => ({
    commodity: m.commodity,
    origin_regions: m.origin_regions
      ? m.origin_regions.split(",").map((s) => s.trim()).filter(Boolean)
      : [],
  }));

  const ports = readRows("ports-list", ["port"]).map((p) => p.port);
  const carriers = readRows("carriers-list", ["carrier"]).map((c) => c.carrier);

  const facilities = readRows("facilities-list", ["location", "type"]);

  return {
    client_id: clientId,
    tier1_suppliers: suppliers,
    raw_materials: materials,
    logistics: { ports, carriers },
    own_facilities: facilities,
  };
}

// ---------------------------------------------------------------------------
// PROFILE SUBMIT
// ---------------------------------------------------------------------------
const profileForm = document.getElementById("profile-form");
const profileStatus = document.getElementById("profile-status");

profileForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const profile = buildProfileFromForm();

  profileStatus.textContent = "Saving…";
  profileStatus.className = "status-msg";

  try {
    const res = await fetch("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    });
    const data = await res.json();

    if (!res.ok) {
      profileStatus.textContent = data.error || "Failed to save profile.";
      profileStatus.className = "status-msg error";
      return;
    }

    profileStatus.textContent = `Saved client "${data.client_id}".`;
    profileStatus.className = "status-msg ok";
    await loadProfiles();
  } catch (err) {
    profileStatus.textContent = "Network error while saving profile.";
    profileStatus.className = "status-msg error";
  }
});

// ---------------------------------------------------------------------------
// LOAD / LIST SAVED PROFILES
// ---------------------------------------------------------------------------
async function loadProfiles() {
  const res = await fetch("/api/profiles");
  const profiles = await res.json();
  state.profiles = profiles;

  const listEl = document.getElementById("saved-profiles-list");
  const selectEl = document.getElementById("active-client-select");

  listEl.innerHTML = "";
  selectEl.innerHTML = '<option value="">Select a client…</option>';

  profiles.forEach((p) => {
    const item = document.createElement("div");
    item.className = "saved-item";
    item.innerHTML = `<span>${p.client_id}</span>`;
    const delBtn = document.createElement("button");
    delBtn.textContent = "Remove";
    delBtn.addEventListener("click", async () => {
      await fetch(`/api/profiles/${p.client_id}`, { method: "DELETE" });
      await loadProfiles();
    });
    item.appendChild(delBtn);
    listEl.appendChild(item);

    const opt = document.createElement("option");
    opt.value = p.client_id;
    opt.textContent = p.client_id;
    selectEl.appendChild(opt);
  });
}

document.getElementById("active-client-select").addEventListener("change", (e) => {
  state.activeClientId = e.target.value || null;
  document.getElementById("run-pipeline-btn").disabled = !state.activeClientId;
});

// ---------------------------------------------------------------------------
// RUN PIPELINE
// ---------------------------------------------------------------------------
const runBtn = document.getElementById("run-pipeline-btn");
const runLog = document.getElementById("run-log");

runBtn.addEventListener("click", async () => {
  if (!state.activeClientId) return;

  runBtn.disabled = true;
  runBtn.textContent = "Running…";
  runLog.className = "run-log visible";
  runLog.textContent = `Starting pipeline for ${state.activeClientId}…\n`;

  document.getElementById("dashboard-empty").hidden = true;
  document.getElementById("dashboard-content").hidden = true;

  try {
    const res = await fetch(`/api/run/${state.activeClientId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    const data = await res.json();

    if (!res.ok) {
      runLog.textContent += `\nERROR: ${data.error || "Pipeline failed."}`;
      if (data.detail) runLog.textContent += `\n${data.detail}`;
      return;
    }

    if (data.log) runLog.textContent += data.log.join("\n");
    renderDashboard(data);
  } catch (err) {
    runLog.textContent += `\nNetwork error: ${err.message}`;
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = "Run analysis";
  }
});

// ---------------------------------------------------------------------------
// DASHBOARD RENDERING
// ---------------------------------------------------------------------------
function renderDashboard(run) {
  document.getElementById("dashboard-content").hidden = false;

  // --- summary cards (top-level pipeline numbers) ---
  const summaryRow = document.getElementById("summary-row");
  summaryRow.innerHTML = "";
  const briefingCount = (run.briefings || []).length;
  const disruptionCount = (run.deep_score || {}).n_disruptions || 0;
  const cards = [
    { label: "Disruption briefs", value: briefingCount },
    { label: "Confirmed disruptions", value: disruptionCount },
    { label: "Articles scraped", value: run.n_scraped_ok || 0 },
    { label: "Queries run", value: run.n_queries || 0 },
  ];
  cards.forEach((c) => {
    const card = document.createElement("div");
    card.className = "summary-card";
    card.innerHTML = `<div class="value">${c.value}</div><div class="label">${c.label}</div>`;
    summaryRow.appendChild(card);
  });

  // --- pipeline funnel stats bar ---
  const statsEl = document.getElementById("pipeline-stats");
  statsEl.innerHTML = "";
  const pf = run.prefilter || {};
  const ds = run.deep_score || {};
  const dd = run.dedup || {};
  const steps = [
    { num: run.n_rows || 0,       label: "GDELT rows" },
    { num: run.n_scraped_ok || 0, label: "scraped" },
    { num: pf.n_passed || 0,      label: "pre-filter passed" },
    { num: ds.n_disruptions || 0, label: "deep-scored disruptions" },
    { num: dd.n_kept || 0,        label: "after dedup" },
    { num: briefingCount,          label: "briefings" },
  ];
  steps.forEach((s, i) => {
    const step = document.createElement("span");
    step.className = "stat-step";
    step.innerHTML = `<span class="stat-num">${s.num}</span> ${s.label}`;
    statsEl.appendChild(step);
    if (i < steps.length - 1) {
      const arr = document.createElement("span");
      arr.className = "stat-arrow";
      arr.textContent = "›";
      statsEl.appendChild(arr);
    }
  });

  // --- briefings ---
  const briefingsList = document.getElementById("briefings-list");
  briefingsList.innerHTML = "";
  const briefings = run.briefings || [];
  if (briefings.length === 0) {
    briefingsList.innerHTML = '<div class="briefing-empty">No disruptions detected for this client in the selected time window.</div>';
  } else {
    briefings.forEach((b) => {
      const sev = b.severity || "LOW";
      const card = document.createElement("div");
      card.className = `briefing-card sev-${sev}`;

      const sourceLinks = (b.sources || [])
        .map((s) => `<a href="${s.url}" target="_blank" rel="noopener noreferrer">${s.domain} (${s.gkg_date})</a>`)
        .join(" ");

      card.innerHTML = `
        <div class="briefing-top">
          <span class="sev-badge sev-${sev}">${sev}</span>
          <span class="briefing-headline">${b.headline || ""}</span>
        </div>
        <div class="briefing-body">${b.brief || ""}</div>
        ${sourceLinks ? `<div class="briefing-sources">Sources: ${sourceLinks}</div>` : ""}
      `;
      briefingsList.appendChild(card);
    });
  }

  // --- entity grid: status driven by deep scoring, not raw scrape count ---
  const entityGrid = document.getElementById("entity-grid");
  entityGrid.innerHTML = "";

  // Build a map: anchor → highest severity seen across scored disruptions
  const anchorSeverity = {};  // anchor → "HIGH"|"MEDIUM"|"LOW"|null
  const anchorDisruptionCount = {};
  (run.scored_articles || []).forEach((a) => {
    const anchor = a.anchor;
    const ext = a.extraction || {};
    if (!anchorSeverity[anchor]) anchorSeverity[anchor] = null;
    if (ext.is_disruption) {
      anchorDisruptionCount[anchor] = (anchorDisruptionCount[anchor] || 0) + 1;
      const sev = ext.severity;
      const order = { HIGH: 3, MEDIUM: 2, LOW: 1 };
      if (!anchorSeverity[anchor] || (order[sev] || 0) > (order[anchorSeverity[anchor]] || 0)) {
        anchorSeverity[anchor] = sev;
      }
    }
  });

  // Collect all anchors from raw results (includes those with 0 disruptions)
  const byAnchor = {};
  (run.results || []).forEach((r) => {
    if (!byAnchor[r.anchor]) byAnchor[r.anchor] = { type: r.query_type, total: 0, scraped: 0 };
    byAnchor[r.anchor].total++;
    if (r.scrape_ok) byAnchor[r.anchor].scraped++;
  });

  const allAnchors = Object.keys(byAnchor);
  if (allAnchors.length === 0) {
    entityGrid.innerHTML = '<div class="empty-state">No matching articles found for this profile in the selected window.</div>';
  }

  allAnchors.forEach((anchor) => {
    const info = byAnchor[anchor];
    const topSev = anchorSeverity[anchor];  // null if no disruptions scored
    const dCount = anchorDisruptionCount[anchor] || 0;

    let statusClass, badgeClass, badgeLabel;
    if (topSev === "HIGH") {
      statusClass = "status-high"; badgeClass = "status-high"; badgeLabel = "HIGH";
    } else if (topSev === "MEDIUM") {
      statusClass = "status-medium"; badgeClass = "status-medium"; badgeLabel = "MEDIUM";
    } else if (topSev === "LOW") {
      statusClass = "status-medium"; badgeClass = "status-medium"; badgeLabel = "LOW";
    } else {
      statusClass = "status-clear"; badgeClass = "status-clear"; badgeLabel = "Clear";
    }

    const card = document.createElement("div");
    card.className = `entity-card ${statusClass}`;
    card.innerHTML = `
      <div class="entity-name">${anchor}</div>
      <div class="entity-meta">${info.type} · ${dCount} disruption(s) · ${info.scraped} article(s)</div>
      <span class="badge ${badgeClass}">${badgeLabel}</span>
    `;
    entityGrid.appendChild(card);
  });

  // --- article list: show scored articles with extraction data ---
  const articleList = document.getElementById("article-list");
  articleList.innerHTML = "";

  const articlesToShow = run.scored_articles && run.scored_articles.length > 0
    ? run.scored_articles
    : (run.results || []).filter((r) => r.scrape_ok);

  articlesToShow.forEach((r) => {
    const ext = r.extraction || {};
    const isDisruption = ext.is_disruption;
    const sev = ext.severity || "";

    const card = document.createElement("div");
    card.className = "article-card" + (isDisruption ? " is-disruption" : "");

    let extractionHtml = "";
    if (ext.event_type && ext.event_type !== "NONE") {
      const sevBadge = sev && sev !== "NONE"
        ? `<span class="sev-badge sev-${sev}" style="font-size:10px;padding:2px 5px">${sev}</span>`
        : "";
      extractionHtml = `
        <div class="extraction">
          <div class="ext-field"><span class="ext-label">Type</span><span class="ext-val">${ext.event_type}</span></div>
          ${sev && sev !== "NONE" ? `<div class="ext-field">${sevBadge}</div>` : ""}
          ${ext.event_summary ? `<div class="ext-field" style="flex-basis:100%"><span class="ext-label">Summary&nbsp;</span><span class="ext-val">${ext.event_summary}</span></div>` : ""}
        </div>
      `;
    }

    card.innerHTML = `
      <div class="article-top">
        <span>${r.domain} · ${r.anchor} (${r.query_type})</span>
        <span>${r.gkg_date}</span>
      </div>
      <a href="${r.url}" target="_blank" rel="noopener noreferrer">${r.url}</a>
      <div class="snippet">${(r.snippet || "").slice(0, 240)}…</div>
      ${extractionHtml}
    `;
    articleList.appendChild(card);
  });

  if (articlesToShow.length === 0) {
    articleList.innerHTML = '<div class="empty-state">No articles were successfully scraped this run.</div>';
  }
}

// ---------------------------------------------------------------------------
// INIT
// ---------------------------------------------------------------------------
loadProfiles();