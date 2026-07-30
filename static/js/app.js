// ── Tab switching ────────────────────────────────────────────────
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    const panelId = "tab-" + tab.dataset.tab;
    document.getElementById(panelId).classList.add("active");
    // Trigger tab-specific init
    if (tab.dataset.tab === "community") initCommunityTab();
    if (tab.dataset.tab === "query") initQueryTab();
    if (tab.dataset.tab === "batch") initBatchTab();
  });
});

// ── Tab info popovers ("i" — what each tab does, mirrors the README) ─
document.querySelectorAll(".tab-info").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const popover = btn.nextElementSibling;
    const wasOpen = popover.classList.contains("open");
    document.querySelectorAll(".tab-info-popover.open").forEach((p) => p.classList.remove("open"));
    document.querySelectorAll(".tab-info[aria-expanded]").forEach((b) => b.removeAttribute("aria-expanded"));
    if (!wasOpen) {
      popover.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
    }
  });
});
document.querySelectorAll(".tab-info-popover").forEach((p) => {
  p.addEventListener("click", (e) => e.stopPropagation());   // reading it shouldn't close it
});
document.addEventListener("click", () => {
  document.querySelectorAll(".tab-info-popover.open").forEach((p) => p.classList.remove("open"));
  document.querySelectorAll(".tab-info[aria-expanded]").forEach((b) => b.removeAttribute("aria-expanded"));
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  document.querySelectorAll(".tab-info-popover.open").forEach((p) => p.classList.remove("open"));
  document.querySelectorAll(".tab-info[aria-expanded]").forEach((b) => b.removeAttribute("aria-expanded"));
});

// ── Element refs ─────────────────────────────────────────────────
const folderInput   = document.getElementById("folder-path");
const scanBtn       = document.getElementById("scan-btn");
const scanResult    = document.getElementById("scan-result");
const scanSummary   = document.getElementById("scan-summary");
const fileListEl    = document.getElementById("file-list");
const selectAllCb   = document.getElementById("select-all-files");
const fileCountEl   = document.getElementById("file-select-count");
const runBtn        = document.getElementById("run-btn");
const progressCard  = document.getElementById("progress-card");
const resultCard    = document.getElementById("result-card");
const stopBtn       = document.getElementById("stop-btn");
const logEl         = document.getElementById("log");
const fillEl        = document.getElementById("progress-fill");
const pctEl         = document.getElementById("progress-pct");
const stageEl       = document.getElementById("progress-stage");
const partialBadge  = document.getElementById("partial-badge");

let totalDocCount   = 0;
let currentJobId    = null;
let pollTimer       = null;
let blobHasFolders  = false;   // true when the container has virtual folders

// ── Blob container + folder selection ────────────────────────────
// The storage account can hold several containers (gps-proposals, rfp-docs…),
// each with virtual folders. User picks a container, then folders inside it.
function getSelectedContainer() {
  return document.getElementById("container-select")?.value || "";
}

async function loadBlobContainers() {
  if (!window.BLOB_MODE) return;
  const sel = document.getElementById("container-select");
  try {
    const res  = await fetch("/api/data-prep/containers");
    const data = await res.json();
    sel.innerHTML = "";
    (data.containers || []).forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c;
      opt.textContent = c;
      if (c === data.default) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", () => {
      // Container changed — reset folder + scan state
      document.getElementById("blob-folder-block").classList.add("hidden");
      scanResult.classList.add("hidden");
      blobHasFolders = false;
      loadBlobFolders();
    });
    await loadBlobFolders();
  } catch (_) {
    sel.innerHTML = '<option value="">(error listing containers)</option>';
  }
}

async function loadBlobFolders() {
  if (!window.BLOB_MODE) return;
  try {
    const params = new URLSearchParams();
    const cont = getSelectedContainer();
    if (cont) params.set("container", cont);
    const res  = await fetch("/api/data-prep/folders?" + params);
    const data = await res.json();
    const folders = data.folders || [];
    // Flat container (all files at root) — keep the one-click scan flow
    if (folders.length <= 1 && (!folders.length || folders[0].path === "")) return;

    blobHasFolders = true;
    const listEl = document.getElementById("folder-list");
    listEl.innerHTML = "";
    folders.forEach((f) => {
      const row = document.createElement("label");
      row.className = "file-item";

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.dataset.path = f.path;   // value attr can't hold "" reliably
      cb.addEventListener("change", updateFolderSelection);

      const name = document.createElement("span");
      name.className = "file-item-name";
      name.textContent = f.path === "" ? "(container root)" : f.path;

      const count = document.createElement("span");
      count.className = "file-badge";
      count.textContent = `${f.count} file${f.count !== 1 ? "s" : ""}`;

      row.appendChild(cb);
      row.appendChild(name);
      row.appendChild(count);
      listEl.appendChild(row);
    });

    updateFolderSelection();
    document.getElementById("blob-folder-block").classList.remove("hidden");
  } catch (_) { /* folder listing failed — whole-container scan still works */ }
}

function getSelectedFolders() {
  if (!blobHasFolders) return null;   // null = whole container
  return [...document.querySelectorAll("#folder-list input[type=checkbox]:checked")]
    .map((cb) => cb.dataset.path);
}

function updateFolderSelection() {
  const boxes = [...document.querySelectorAll("#folder-list input[type=checkbox]")];
  const checked = boxes.filter((b) => b.checked).length;
  document.getElementById("folder-select-count").textContent =
    `${checked} of ${boxes.length} folders`;
  const selectAll = document.getElementById("folder-select-all");
  selectAll.checked = checked === boxes.length && boxes.length > 0;
  selectAll.indeterminate = checked > 0 && checked < boxes.length;
  scanBtn.disabled = checked === 0;
}

document.getElementById("folder-select-all")?.addEventListener("change", (e) => {
  document.querySelectorAll("#folder-list input[type=checkbox]").forEach((cb) => {
    cb.checked = e.target.checked;
  });
  updateFolderSelection();
});

loadBlobContainers();

// ── Step 1: Scan folder / blob container ─────────────────────────
scanBtn.addEventListener("click", async () => {
  const folder = folderInput.value.trim();
  if (!window.BLOB_MODE && !folder) { folderInput.focus(); return; }

  const blobMode = window.BLOB_MODE;
  scanBtn.disabled = true;
  scanBtn.textContent = blobMode ? "Scanning container…" : "Scanning…";
  scanResult.classList.add("hidden");

  try {
    const params = new URLSearchParams(blobMode ? {} : { folder_path: folder });
    if (blobMode && getSelectedContainer()) params.set("container", getSelectedContainer());
    const selFolders = getSelectedFolders();
    if (selFolders) selFolders.forEach((f) => params.append("folders", f));
    const res = await fetch("/api/data-prep/scan?" + params);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderScanResult(data);
  } catch (err) {
    alert("Scan error: " + err.message);
  } finally {
    scanBtn.disabled = false;
    scanBtn.textContent = blobMode ? "Scan container" : "Scan folder";
  }
});

function renderScanResult(data) {
  totalDocCount = data.count;

  const typeStr = Object.entries(data.by_type || {})
    .map(([ext, n]) => `${ext.toUpperCase()} ×${n}`)
    .join(" · ");

  const extractedSet = new Set(data.extracted || []);
  const extractedInList = (data.files || []).filter((f) => extractedSet.has(f)).length;
  const newCount = data.count - extractedInList;

  // Guardrail: client RFPs are INPUT (questions), not answer-corpus content.
  // Ingesting one poisons retrieval — its text matches the very questions
  // users will ask. Soft warning only; the user decides.
  const rfpLike = (data.files || []).filter((f) => /(^|[^a-z])rfp([^a-z]|$)/i.test(f));

  scanSummary.innerHTML = `
    <span class="count">${data.count}</span>
    <span>document${data.count !== 1 ? "s" : ""} found</span>
    ${typeStr ? `<span class="types">${typeStr}</span>` : ""}
    ${extractedInList ? `<span class="types">${extractedInList} already extracted · ${newCount} new</span>` : ""}
    ${rfpLike.length ? `<span class="prereq-warn" title="${escHtml(rfpLike.slice(0, 5).join(", "))}">⚠ ${rfpLike.length} file name${rfpLike.length !== 1 ? "s" : ""} contain "RFP" — client RFPs are question documents and should NOT be ingested into the answer corpus (they will dominate retrieval for their own questions)</span>` : ""}
  `;

  // Build the selectable file list.
  // Default: new (un-extracted) files checked; already-extracted unchecked —
  // so an incremental run only processes what's new.
  fileListEl.innerHTML = "";
  (data.files || []).forEach((fname) => {
    const isExtracted = extractedSet.has(fname);
    const row = document.createElement("label");
    row.className = "file-item";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = fname;
    cb.checked = !isExtracted;
    cb.addEventListener("change", updateFileSelection);

    const name = document.createElement("span");
    name.className = "file-item-name";
    name.textContent = fname;
    name.title = fname;

    row.appendChild(cb);
    row.appendChild(name);

    if (isExtracted) {
      const badge = document.createElement("span");
      badge.className = "file-badge";
      badge.textContent = "extracted";
      row.appendChild(badge);
    }

    if (data.blob_mode || window.BLOB_MODE) {
      const link = document.createElement("a");
      link.className = "file-view-link";
      link.href = "/api/data-prep/doc-open?filename=" + encodeURIComponent(fname);
      link.target = "_blank";
      link.rel = "noopener";
      link.textContent = "view ↗";
      link.title = "Open this RFP from Azure Blob";
      link.addEventListener("click", (e) => e.stopPropagation());
      row.appendChild(link);
    }

    fileListEl.appendChild(row);
  });

  updateFileSelection();
  scanResult.classList.remove("hidden");
  runBtn.disabled = false;
}

function getSelectedFiles() {
  return [...fileListEl.querySelectorAll("input[type=checkbox]:checked")].map((cb) => cb.value);
}

function updateFileSelection() {
  const boxes = [...fileListEl.querySelectorAll("input[type=checkbox]")];
  const checked = boxes.filter((b) => b.checked).length;
  fileCountEl.textContent = `${checked} of ${boxes.length} selected`;
  selectAllCb.checked = checked === boxes.length && boxes.length > 0;
  selectAllCb.indeterminate = checked > 0 && checked < boxes.length;
  runBtn.disabled = checked === 0;
}

selectAllCb.addEventListener("change", () => {
  fileListEl.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.checked = selectAllCb.checked;
  });
  updateFileSelection();
});

// ── Step 2: Run ──────────────────────────────────────────────────
runBtn.addEventListener("click", async () => {
  const folder = folderInput.value.trim();
  if (!window.BLOB_MODE && !folder) return;

  const selectedFiles = getSelectedFiles();
  if (!selectedFiles.length) return;

  runBtn.disabled = true;
  runBtn.textContent = "Running…";
  stopBtn.disabled = false;
  resultCard.classList.add("hidden");
  partialBadge.classList.add("hidden");
  progressCard.classList.remove("hidden");
  logEl.textContent = "";
  setProgress(0, "Starting…");

  try {
    const res = await fetch("/api/data-prep/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        folder_path: folder,
        resolution: parseFloat(document.getElementById("resolution").value) || 1.0,
        skip_existing: document.getElementById("skip-existing").checked,
        selected_files: selectedFiles,
        force_reextract: document.getElementById("force-reextract").checked,
        folders: getSelectedFolders(),
        container: window.BLOB_MODE ? getSelectedContainer() : "",
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    currentJobId = data.job_id;
    pollStatus(currentJobId);
  } catch (err) {
    appendLog("ERROR: " + err.message);
    resetRunBtn();
  }
});

// ── Stop & Save ──────────────────────────────────────────────────
stopBtn.addEventListener("click", async () => {
  if (!currentJobId) return;
  stopBtn.disabled = true;
  stopBtn.textContent = "Stopping…";
  try {
    await fetch(`/api/data-prep/cancel/${currentJobId}`, { method: "POST" });
    appendLog("Stop requested — finishing in-flight docs then building partial graph…");
  } catch (err) {
    appendLog("Stop error: " + err.message);
  }
});

// ── Polling ──────────────────────────────────────────────────────
function pollStatus(jobId) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const res  = await fetch(`/api/data-prep/status/${jobId}`);
      const job  = await res.json();
      renderLogs(job.logs);
      setProgress(job.progress * 100, prettyStage(job.stage));

      if (job.status === "completed") {
        clearInterval(pollTimer);
        renderResult(job.result);
        resetRunBtn();
        stopBtn.disabled = true;
      } else if (job.status === "failed") {
        clearInterval(pollTimer);
        appendLog("FAILED: " + (job.error || "unknown error"));
        resetRunBtn();
        stopBtn.disabled = true;
      }
    } catch (err) {
      appendLog("Polling error: " + err.message);
    }
  }, 1200);
}

// ── Render helpers ───────────────────────────────────────────────
function renderLogs(logs) {
  if (!logs?.length) return;
  logEl.textContent = logs.map((l) => l.message).join("\n");
  logEl.scrollTop = logEl.scrollHeight;
}

function appendLog(msg) {
  logEl.textContent += (logEl.textContent ? "\n" : "") + msg;
  logEl.scrollTop = logEl.scrollHeight;
}

function setProgress(pct, stage) {
  const p = Math.max(0, Math.min(100, pct));
  fillEl.style.width = p + "%";
  pctEl.textContent = Math.round(p) + "%";
  if (stage) stageEl.textContent = stage;
}

function prettyStage(stage) {
  const ml = window.MODEL_LABEL || "LLM";
  return {
    start:            "Starting…",
    extract_text:     "Extracting text + chunks",
    extract_entities: `Extracting entities with ${ml}`,
    build_graph:      "Building graph + communities",
    generate_html:    "Generating visualisation",
    cancelling:       "Stopping — finishing in-flight docs…",
    done:             "Complete",
    error:            "Error",
  }[stage] || stage || "";
}

function renderResult(result) {
  if (!result) return;
  const stats = result.stats || {};
  document.getElementById("result-title").textContent = "Result";

  if (result.was_cancelled) {
    partialBadge.classList.remove("hidden");
  }

  const docsProcessed = (result.doc_paths || []).length;
  const grid = document.getElementById("stats-grid");
  const cells = [
    ["Documents", docsProcessed + (result.was_cancelled ? ` / ${totalDocCount}` : "")],
    ["Entities",       result.entities_count       ?? stats.entities      ?? "—"],
    ["Relationships",  result.relationships_count  ?? stats.relationships ?? "—"],
    ["Graph nodes",    stats.nodes       ?? "—"],
    ["Graph edges",    stats.edges       ?? "—"],
    ["Communities",    stats.communities ?? "—"],
  ];
  grid.innerHTML = cells
    .map(([label, num]) =>
      `<div class="stat-box"><div class="num">${num}</div><div class="label">${label}</div></div>`
    )
    .join("");

  const errs = result.per_doc_errors || [];
  document.getElementById("errors-block").innerHTML = errs.length
    ? `<div class="error-note">${errs.length} doc(s) had extraction errors:<br>` +
      errs.map((e) => `• ${e.filename}: ${e.error}`).join("<br>") + `</div>`
    : "";

  const graphBtn = document.getElementById("view-graph");
  graphBtn.style.display = stats.nodes ? "" : "none";
  const rebuildBtn2 = document.getElementById("rebuild-graph-btn");
  if (rebuildBtn2) rebuildBtn2.style.display = "";

  resultCard.classList.remove("hidden");
  if (stats.nodes) loadGraphScopes();
}

function resetRunBtn() {
  runBtn.disabled = false;
  runBtn.textContent = "Run data prep";
  const rebuildBtn = document.getElementById("rebuild-graph-btn");
  if (rebuildBtn) { rebuildBtn.disabled = false; rebuildBtn.textContent = "Rebuild graph"; }
}

// ── Existing graph on page load ──────────────────────────────
// The graph HTML endpoint works without running data prep — if a graph
// already exists on disk, surface it immediately.
async function checkExistingGraph() {
  try {
    const res = await fetch("/api/data-prep/graph-stats");
    const stats = await res.json();
    if (!stats.exists || !stats.nodes) return;

    document.getElementById("result-title").textContent = "Current knowledge graph";
    document.getElementById("stats-grid").innerHTML = [
      ["Entities",      stats.entities      ?? "—"],
      ["Relationships", stats.relationships ?? "—"],
      ["Graph nodes",   stats.nodes         ?? "—"],
      ["Graph edges",   stats.edges         ?? "—"],
      ["Communities",   stats.communities   ?? "—"],
    ].map(([label, num]) =>
      `<div class="stat-box"><div class="num">${num}</div><div class="label">${label}</div></div>`
    ).join("");

    document.getElementById("view-graph").style.display = "";
    resultCard.classList.remove("hidden");
    loadGraphScopes();
  } catch (_) { /* no graph yet — nothing to show */ }
}
checkExistingGraph();

// ── Graph-viz scope picker (#5) ──────────────────────────────
// The whole-corpus graph is ~22k nodes and freezes D3, so the viz is scoped
// and degree-capped. This picks which scope's sub-graph to open.
function updateGraphHref() {
  const v = document.getElementById("graph-scope")?.value || "";
  const link = document.getElementById("view-graph");
  link.href = v
    ? `/api/data-prep/graph-html?scope=${encodeURIComponent(v)}`
    : "/api/data-prep/graph-html";
}

async function loadGraphScopes() {
  const sel = document.getElementById("graph-scope");
  if (!sel) return;
  try {
    const data = await (await fetch("/api/data-prep/graph-scopes")).json();
    const hint = document.getElementById("graph-scope-hint");
    if (!data.scoping_available) {
      // No registry → only the whole-corpus (capped) view is possible.
      if (hint) hint.textContent =
        "Showing the top 600 most-connected entities. Sync blob metadata to focus by Platform / Service Function.";
      updateGraphHref();
      return;
    }
    // Reuse the grouped optgroup renderer from the Query tab.
    fillScopeSelect(sel, data.scopes || []);
    // Restore the "whole corpus" default label as the first option.
    if (sel.options[0]) sel.options[0].textContent = "Whole corpus (top 600)";
    updateGraphHref();
  } catch (_) { updateGraphHref(); }
}

document.getElementById("graph-scope")?.addEventListener("change", updateGraphHref);

// ── Rebuild graph (no LLM re-extraction) ─────────────────────
document.getElementById("rebuild-graph-btn")?.addEventListener("click", async () => {
  const rebuildBtn = document.getElementById("rebuild-graph-btn");
  rebuildBtn.disabled = true;
  rebuildBtn.textContent = "Rebuilding…";
  runBtn.disabled = true;
  stopBtn.disabled = true;
  resultCard.classList.add("hidden");
  partialBadge.classList.add("hidden");
  progressCard.classList.remove("hidden");
  logEl.textContent = "";
  setProgress(0, "Rebuilding graph…");

  try {
    const resolution = parseFloat(document.getElementById("resolution").value) || 1.0;
    const res = await fetch("/api/data-prep/build-graph", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ resolution }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    currentJobId = data.job_id;
    pollStatus(currentJobId);
  } catch (err) {
    appendLog("ERROR: " + err.message);
    resetRunBtn();
  }
});

// ═══════════════════════════════════════════════════════════════
// ── TAB 2: Community Summariser ──────────────────────────────
// ═══════════════════════════════════════════════════════════════

let commJobId       = null;
let commPollTimer   = null;
let commTotalCount  = 0;

async function initCommunityTab() {
  const statusEl  = document.getElementById("comm-prereq-status");
  const runCard   = document.getElementById("comm-run-card");
  statusEl.innerHTML = '<span class="muted">Checking prerequisites…</span>';
  runCard.classList.add("hidden");

  try {
    const res  = await fetch("/api/community/prerequisites");
    const data = await res.json();
    renderCommPrereqs(data);
  } catch (err) {
    statusEl.innerHTML = `<span class="prereq-err">Error: ${err.message}</span>`;
  }
}

function renderCommPrereqs(data) {
  const statusEl = document.getElementById("comm-prereq-status");
  const runCard  = document.getElementById("comm-run-card");

  if (!data.ready) {
    statusEl.innerHTML =
      '<span class="prereq-warn">⚠ Data Prep has not completed yet. ' +
      'Run Tab 1 first to build the knowledge graph.</span>';
    document.getElementById("scope-comm-card").classList.add("hidden");
    return;
  }

  loadScopePlan();   // per-scope communities (#4) — hidden if no registry

  const done = data.summaries_done;
  const total = data.community_count;
  statusEl.innerHTML = done === total
    ? `<span class="prereq-ok">✓ ${total} communities ready · ${done} summaries already written</span>`
    : `<span class="prereq-ok">✓ ${total} communities ready</span>` +
      (done ? ` <span class="muted">(${done} summaries already written)</span>` : "");

  commTotalCount = total;

  // Community meta bar
  const metaEl = document.getElementById("comm-meta");
  metaEl.innerHTML = `
    <div><div class="big">${total}</div><div class="label">Communities</div></div>
    <div><div class="big">${done}</div><div class="label">Already summarised</div></div>
    <div><div class="big">${total - done}</div><div class="label">To process</div></div>
  `;

  // Selectable community list — un-summarised ones pre-checked.
  // Tick a summarised one to regenerate (summaries are always overwritten).
  const listEl = document.getElementById("comm-list");
  listEl.innerHTML = "";
  (data.communities || []).forEach((c) => {
    const row = document.createElement("label");
    row.className = "file-item";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = c.id;
    cb.checked = !c.has_summary;
    cb.addEventListener("change", updateCommSelection);

    const name = document.createElement("span");
    name.className = "file-item-name";
    name.textContent = `Community ${c.id} — ${c.entity_count} entities`;
    name.title = (c.source_docs || []).join(", ");

    row.appendChild(cb);
    row.appendChild(name);

    if (c.has_summary) {
      const badge = document.createElement("span");
      badge.className = "file-badge";
      badge.textContent = "summarised";
      row.appendChild(badge);
    }

    listEl.appendChild(row);
  });

  updateCommSelection();
  runCard.classList.remove("hidden");
}

function getSelectedCommunities() {
  return [...document.querySelectorAll("#comm-list input[type=checkbox]:checked")]
    .map((cb) => cb.value);
}

function updateCommSelection() {
  const boxes = [...document.querySelectorAll("#comm-list input[type=checkbox]")];
  const checked = boxes.filter((b) => b.checked).length;
  document.getElementById("comm-select-count").textContent =
    `${checked} of ${boxes.length} selected`;
  const selectAll = document.getElementById("comm-select-all");
  selectAll.checked = checked === boxes.length && boxes.length > 0;
  selectAll.indeterminate = checked > 0 && checked < boxes.length;
  document.getElementById("comm-run-btn").disabled = checked === 0;
}

document.getElementById("comm-select-all").addEventListener("change", (e) => {
  document.querySelectorAll("#comm-list input[type=checkbox]").forEach((cb) => {
    cb.checked = e.target.checked;
  });
  updateCommSelection();
});

// ── Run ──────────────────────────────────────────────────────
document.getElementById("comm-run-btn").addEventListener("click", async () => {
  const selectedComms = getSelectedCommunities();
  if (!selectedComms.length) return;

  const commRunBtn = document.getElementById("comm-run-btn");
  commRunBtn.disabled = true;
  commRunBtn.textContent = "Running…";

  document.getElementById("comm-result-card").classList.add("hidden");
  document.getElementById("comm-partial-badge").classList.add("hidden");
  document.getElementById("comm-progress-card").classList.remove("hidden");
  document.getElementById("comm-log").textContent = "";
  document.getElementById("comm-stop-btn").disabled = false;
  setCommProgress(0, "Starting…");

  try {
    const res = await fetch("/api/community/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_communities: selectedComms }),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const data = await res.json();
    commJobId = data.job_id;
    pollCommStatus(commJobId);
  } catch (err) {
    appendCommLog("ERROR: " + err.message);
    resetCommBtn();
  }
});

// ── Stop ─────────────────────────────────────────────────────
document.getElementById("comm-stop-btn").addEventListener("click", async () => {
  if (!commJobId) return;
  const stopBtn = document.getElementById("comm-stop-btn");
  stopBtn.disabled = true;
  stopBtn.textContent = "Stopping…";
  try {
    await fetch(`/api/community/cancel/${commJobId}`, { method: "POST" });
    appendCommLog("Stop requested — finishing in-flight summaries…");
  } catch (err) {
    appendCommLog("Stop error: " + err.message);
  }
});

// ── Polling ──────────────────────────────────────────────────
function pollCommStatus(jobId) {
  clearInterval(commPollTimer);
  commPollTimer = setInterval(async () => {
    try {
      const res = await fetch(`/api/community/status/${jobId}`);
      const job = await res.json();
      renderCommLogs(job.logs);
      setCommProgress(job.progress * 100, prettyCommStage(job.stage));
      if (job.status === "completed") {
        clearInterval(commPollTimer);
        renderCommResult(job.result);
        resetCommBtn();
        document.getElementById("comm-stop-btn").disabled = true;
        initCommunityTab();   // refresh list so "summarised" badges update
      } else if (job.status === "failed") {
        clearInterval(commPollTimer);
        appendCommLog("FAILED: " + (job.error || "unknown error"));
        resetCommBtn();
        document.getElementById("comm-stop-btn").disabled = true;
      }
    } catch (err) {
      appendCommLog("Polling error: " + err.message);
    }
  }, 1500);
}

// ── Render helpers ───────────────────────────────────────────
function renderCommLogs(logs) {
  if (!logs?.length) return;
  const el = document.getElementById("comm-log");
  el.textContent = logs.map((l) => l.message).join("\n");
  el.scrollTop = el.scrollHeight;
}

function appendCommLog(msg) {
  const el = document.getElementById("comm-log");
  el.textContent += (el.textContent ? "\n" : "") + msg;
  el.scrollTop = el.scrollHeight;
}

function setCommProgress(pct, stage) {
  const p = Math.max(0, Math.min(100, pct));
  document.getElementById("comm-fill").style.width = p + "%";
  document.getElementById("comm-pct").textContent  = Math.round(p) + "%";
  if (stage) document.getElementById("comm-stage").textContent = stage;
}

function prettyCommStage(stage) {
  const ml = window.MODEL_LABEL || "LLM";
  return {
    start:      "Starting…",
    validate:   "Checking prerequisites",
    summarise:  `Summarising communities with ${ml}`,
    cancelling: "Stopping — finishing in-flight summaries…",
    done:       "Complete",
    error:      "Error",
  }[stage] || stage || "";
}

async function renderCommResult(result) {
  if (!result) return;

  if (result.was_cancelled) {
    document.getElementById("comm-partial-badge").classList.remove("hidden");
  }

  const ok    = (result.results || []).filter((r) => !r.error).length;
  const errs  = (result.errors || []).length;
  document.getElementById("comm-stats").innerHTML =
    `<div class="scan-summary" style="margin:0 0 12px">
      <span class="count">${ok}</span>
      <span>summaries written</span>
      ${errs ? `<span class="prereq-warn">${errs} error(s)</span>` : ""}
      ${result.was_cancelled ? `<span class="muted">of ${commTotalCount} total</span>` : ""}
    </div>`;

  // Load summaries list
  try {
    const res  = await fetch("/api/community/summaries");
    const data = await res.json();
    const listEl = document.getElementById("comm-summary-list");
    listEl.innerHTML = "";
    (data.summaries || []).forEach((s) => {
      const item = document.createElement("div");
      item.className = "summary-item";
      // Extract community number from filename e.g. community_03.md → 3
      const num = s.file.replace("community_", "").replace(".md", "").replace(/^0+/, "") || "0";
      item.innerHTML = `
        <div class="summary-header">
          <span class="summary-comm-id">Community ${num}</span>
          <span class="summary-title">${escHtml(s.title)}</span>
          <span class="summary-chevron">▶</span>
        </div>
        <pre class="summary-body">${escHtml(s.preview)}${s.preview.length >= 400 ? "\n…" : ""}</pre>
      `;
      item.querySelector(".summary-header").addEventListener("click", () => {
        item.classList.toggle("open");
      });
      listEl.appendChild(item);
    });
  } catch (_) {}

  document.getElementById("comm-result-card").classList.remove("hidden");
}

function resetCommBtn() {
  const btn = document.getElementById("comm-run-btn");
  btn.disabled = false;
  btn.textContent = "Run community summariser";
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// ── Per-scope communities (feedback item #4) ─────────────────────────────────
let scopeJobId = null;
let scopePollTimer = null;

// Cache of the last-loaded plan, keyed by scope name — lets the selection
// helpers below recompute the cost estimate without a re-fetch.
let _scopePlanByName = {};

async function loadScopePlan() {
  const card = document.getElementById("scope-comm-card");
  const statusEl = document.getElementById("scope-plan-status");
  const tableEl = document.getElementById("scope-plan-table");
  try {
    const data = await (await fetch("/api/community/scope-plan")).json();
    if (!data.scoping_available) {
      // No metadata registry → per-scope communities don't apply. Keep hidden.
      card.classList.add("hidden");
      return;
    }
    card.classList.remove("hidden");
    const built = data.scopes.filter((s) => s.built).length;
    statusEl.innerHTML =
      `<span class="prereq-ok">✓ ${data.total_scopes} scopes</span> ` +
      `<span class="muted">· ${built} built · ${data.total_scopes - built} pending</span>`;

    _scopePlanByName = {};
    data.scopes.forEach((s) => { _scopePlanByName[s.scope] = s; });

    const rows = data.scopes.map((s) => `
      <tr>
        <td><input type="checkbox" class="scope-select-cb"
              data-scope="${encodeURIComponent(s.scope)}" ${s.built ? "" : "checked"} /></td>
        <td>${escHtml(s.scope)}</td>
        <td><span class="scope-field-tag ${s.field}">${s.field === "platform" ? "Platform / Sub Service Line" : "Service Function"}</span></td>
        <td class="num">${s.doc_count ?? "—"}</td>
        <td class="num">${s.entity_count ?? "—"}</td>
        <td>${s.built
          ? `<span class="prereq-ok">✓ ${s.communities} comms · ${s.summaries} summaries</span>`
          : `<span class="muted">not built</span>`}</td>
      </tr>`).join("");
    tableEl.innerHTML =
      `<thead><tr><th></th><th>Scope</th><th>Field</th><th class="num">Docs</th>` +
      `<th class="num">Entities</th><th>Status</th></tr></thead><tbody>${rows}</tbody>`;

    updateScopeSelectState();
  } catch (err) {
    statusEl.innerHTML = `<span class="prereq-err">Error: ${escHtml(err.message)}</span>`;
  }
}

function getSelectedScopes() {
  return Array.from(document.querySelectorAll(".scope-select-cb:checked"))
    .map((cb) => decodeURIComponent(cb.dataset.scope));
}

// Recomputes the "Select all" checkbox tri-state, the count label, the build
// button's label/enabled state, and the cost estimate — all driven purely by
// which checkboxes are ticked, so rewriting a subset only ever costs for that
// subset (unticked = untouched by the next build).
function updateScopeSelectState() {
  const boxes = Array.from(document.querySelectorAll(".scope-select-cb"));
  const checkedBoxes = boxes.filter((cb) => cb.checked);
  const checked = checkedBoxes.length;

  const countEl = document.getElementById("scope-select-count");
  if (countEl) countEl.textContent = boxes.length ? `${checked} of ${boxes.length} selected` : "";

  const selectAll = document.getElementById("scope-select-all");
  if (selectAll) {
    selectAll.checked = boxes.length > 0 && checked === boxes.length;
    selectAll.indeterminate = checked > 0 && checked < boxes.length;
  }

  const btn = document.getElementById("scope-build-btn");
  if (btn) {
    btn.disabled = checked === 0;
    btn.textContent = checked === 0 || checked === boxes.length
      ? "Build all scopes"
      : `Rebuild ${checked} selected`;
  }

  const est = document.getElementById("scope-build-est");
  if (est) {
    if (!checked) {
      est.textContent = "Select at least one scope to build or rewrite.";
    } else {
      const names = new Set(checkedBoxes.map((cb) => decodeURIComponent(cb.dataset.scope)));
      let totalEnt = 0;
      names.forEach((n) => { totalEnt += _scopePlanByName[n]?.entity_count || 0; });
      est.textContent =
        `~${checked} scope(s), ${totalEnt} scoped entity instances — summaries call the model per community.`;
    }
  }
}

// Delegated listeners attached ONCE — the table's rows are replaced on every
// loadScopePlan() call, but the table element and the select-all checkbox
// themselves are not, so this never double-registers.
document.getElementById("scope-plan-table")?.addEventListener("change", (e) => {
  if (e.target.classList.contains("scope-select-cb")) updateScopeSelectState();
});
document.getElementById("scope-select-all")?.addEventListener("change", (e) => {
  document.querySelectorAll(".scope-select-cb").forEach((cb) => { cb.checked = e.target.checked; });
  updateScopeSelectState();
});

function setScopeProgress(pct, stage) {
  document.getElementById("scope-pct").textContent = `${Math.round(pct)}%`;
  document.getElementById("scope-fill").style.width = `${pct}%`;
  if (stage) document.getElementById("scope-stage").textContent = stage;
}

document.getElementById("scope-build-btn").addEventListener("click", async () => {
  const selected = getSelectedScopes();
  if (!selected.length) return;

  const btn = document.getElementById("scope-build-btn");
  const maxComm = parseInt(document.getElementById("scope-max-comm").value, 10);
  btn.disabled = true;
  btn.textContent = "Building…";

  document.getElementById("scope-progress").classList.remove("hidden");
  document.getElementById("scope-log").textContent = "";
  document.getElementById("scope-stop-btn").disabled = false;
  setScopeProgress(0, "Starting…");

  try {
    const body = { scopes: selected };
    if (Number.isFinite(maxComm) && maxComm > 0) body.max_communities_per_scope = maxComm;
    const res = await fetch("/api/community/build-scopes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `HTTP ${res.status}`);
    const data = await res.json();
    scopeJobId = data.job_id;
    pollScopeStatus(scopeJobId);
  } catch (err) {
    const el = document.getElementById("scope-log");
    el.textContent += "ERROR: " + err.message + "\n";
    resetScopeBtn();
  }
});

document.getElementById("scope-stop-btn").addEventListener("click", async () => {
  if (!scopeJobId) return;
  const stopBtn = document.getElementById("scope-stop-btn");
  stopBtn.disabled = true;
  stopBtn.textContent = "Stopping…";
  try {
    await fetch(`/api/community/cancel/${scopeJobId}`, { method: "POST" });
  } catch (_) {}
});

function pollScopeStatus(jobId) {
  clearInterval(scopePollTimer);
  scopePollTimer = setInterval(async () => {
    try {
      const job = await (await fetch(`/api/community/status/${jobId}`)).json();
      const el = document.getElementById("scope-log");
      if (job.logs?.length) {
        el.textContent = job.logs.map((l) => l.message).join("\n");
        el.scrollTop = el.scrollHeight;
      }
      setScopeProgress((job.progress || 0) * 100, job.stage || "");
      if (job.status === "completed") {
        clearInterval(scopePollTimer);
        resetScopeBtn();
        document.getElementById("scope-stop-btn").disabled = true;
        loadScopePlan();   // refresh built/pending counts
      } else if (job.status === "failed") {
        clearInterval(scopePollTimer);
        el.textContent += "\nFAILED: " + (job.error || "unknown error");
        resetScopeBtn();
        document.getElementById("scope-stop-btn").disabled = true;
      }
    } catch (err) {
      /* transient — keep polling */
    }
  }, 1500);
}

function resetScopeBtn() {
  const stopBtn = document.getElementById("scope-stop-btn");
  stopBtn.textContent = "Stop & Save";
  updateScopeSelectState();   // restores label/enabled-state from checkboxes
}

// ═══════════════════════════════════════════════════════════════
// ── TAB 3: Query Agent ───────────────────────────────────────
// ═══════════════════════════════════════════════════════════════

async function initQueryTab() {
  const statusEl  = document.getElementById("query-prereq-status");
  const chatWrap  = document.getElementById("query-chat-wrap");
  statusEl.innerHTML = '<span class="muted">Checking prerequisites…</span>';
  chatWrap.classList.add("hidden");

  try {
    const res  = await fetch("/api/query/prerequisites");
    const data = await res.json();
    renderQueryPrereqs(data);
  } catch (err) {
    statusEl.innerHTML = `<span class="prereq-err">Error: ${err.message}</span>`;
  }
}

function renderQueryPrereqs(data) {
  const statusEl = document.getElementById("query-prereq-status");
  const chatWrap = document.getElementById("query-chat-wrap");

  if (!data.ready) {
    statusEl.innerHTML =
      '<span class="prereq-warn">⚠ Graph not ready. Complete Data Prep (Tab 1) first.</span>';
    return;
  }

  const warnSummary = data.summaries_warning
    ? ' <span class="prereq-warn">· Community summaries missing — global queries may be weak</span>'
    : "";
  statusEl.innerHTML =
    `<span class="prereq-ok">✓ Graph ready — ${data.entities} entities · ` +
    `${data.communities} communities · ${data.summaries} summaries</span>${warnSummary}`;

  chatWrap.classList.remove("hidden");
  if (data.retrieval) setupRetrievalPanel(data.retrieval);
  loadPlatformScope();
  loadSuggestions();
}

// ── Platform scope (M1 service-function scoping) ─────────────
// Fills a scope <select> from /scopes, grouping Platform values and Service
// Function values under labelled optgroups. Returns true if any value existed.
function fillScopeSelect(sel, scopes) {
  const current = sel.value;
  sel.innerHTML = '<option value="">All documents</option>';
  const groups = [
    ["Platform / Sub Service Line", "platform"],
    ["Service Function", "service_function"],
  ];
  groups.forEach(([label, field]) => {
    const items = scopes.filter((s) => s.field === field);
    if (!items.length) return;
    const og = document.createElement("optgroup");
    og.label = label;
    items.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s.value;
      opt.textContent = `${s.value} (${s.count})`;
      og.appendChild(opt);
    });
    sel.appendChild(og);
  });
  if (current) sel.value = current;
}

// Populates the scope dropdown (Platform / Sub Service Line / Service Function)
// from the metadata registry and shows a live "N documents in scope" preview.
// Hidden entirely when no registry exists.
async function loadPlatformScope() {
  const row = document.getElementById("scope-row");
  if (!row) return;
  try {
    const data = await (await fetch("/api/query/scopes")).json();
    if (!data.scoping_available) { row.classList.add("hidden"); return; }
    fillScopeSelect(document.getElementById("scope-platform"), data.scopes || []);
    row.classList.remove("hidden");
    updateScopeCount();
  } catch (_) { row.classList.add("hidden"); }
}

function getSelectedPlatform() {
  const free = (document.getElementById("scope-freetext")?.value || "").trim();
  if (free) return free;
  return document.getElementById("scope-platform")?.value || "";
}

let _scopeCountTimer = null;
async function updateScopeCount() {
  const el = document.getElementById("scope-count");
  const p = getSelectedPlatform();
  if (!p) { el.textContent = ""; el.classList.remove("zero"); return; }
  el.textContent = "…";
  try {
    const data = await (await fetch("/api/query/scope-count?platform=" + encodeURIComponent(p))).json();
    const n = data.count;
    el.textContent = `${n} document${n === 1 ? "" : "s"} in scope`;
    el.classList.toggle("zero", n === 0);
  } catch (_) { el.textContent = ""; }
}

document.getElementById("scope-platform")?.addEventListener("change", () => {
  // picking a dropdown value clears free text
  const ft = document.getElementById("scope-freetext");
  if (ft) ft.value = "";
  updateScopeCount();
});
document.getElementById("scope-freetext")?.addEventListener("input", () => {
  clearTimeout(_scopeCountTimer);
  _scopeCountTimer = setTimeout(updateScopeCount, 350);   // debounce typing
});

// ── Retrieval settings panel ─────────────────────────────────
// Session-scoped overrides of the server's retrieval caps. Defaults + allowed
// ranges come from the server; values live in sessionStorage (die with the
// tab). The server clamps regardless — this is UX, not the guardrail.
const RS_LABELS = {
  top_chunks:               "Source chunks",
  top_communities:          "Communities",
  max_prompt_entities:      "Entities in context",
  max_prompt_relationships: "Relationships in context",
};
let rsDefaults = null;

function rsLoad() {
  try { return JSON.parse(sessionStorage.getItem("retrievalOverrides")) || {}; }
  catch (_) { return {}; }
}

function rsSave(overrides) {
  sessionStorage.setItem("retrievalOverrides", JSON.stringify(overrides));
  document.getElementById("rs-badge").classList.toggle(
    "hidden", !Object.keys(overrides).length);
}

function getRetrievalOverrides() {
  return rsLoad();   // {} when untouched → server uses .env defaults
}

function setupRetrievalPanel(retrieval) {
  rsDefaults = retrieval.defaults;
  const ranges = retrieval.ranges;
  const overrides = rsLoad();
  const rowsEl = document.getElementById("rs-rows");
  rowsEl.innerHTML = "";

  Object.keys(RS_LABELS).forEach((key) => {
    const [lo, hi] = ranges[key];
    const current = overrides[key] ?? rsDefaults[key];

    const row = document.createElement("div");
    row.className = "rs-row";

    const label = document.createElement("label");
    label.textContent = RS_LABELS[key];
    label.htmlFor = `rs-${key}`;

    const slider = document.createElement("input");
    slider.type = "range";
    slider.id = `rs-${key}`;
    slider.min = lo; slider.max = hi; slider.step = 1;
    slider.value = Math.min(hi, Math.max(lo, current));

    const val = document.createElement("span");
    val.className = "rs-val";
    val.textContent = slider.value;

    slider.addEventListener("input", () => {
      val.textContent = slider.value;
      const o = rsLoad();
      if (parseInt(slider.value, 10) === rsDefaults[key]) delete o[key];
      else o[key] = parseInt(slider.value, 10);
      rsSave(o);
    });

    row.appendChild(label);
    row.appendChild(slider);
    row.appendChild(val);
    rowsEl.appendChild(row);
  });

  document.getElementById("rs-badge").classList.toggle(
    "hidden", !Object.keys(overrides).length);
}

document.getElementById("rs-reset")?.addEventListener("click", () => {
  rsSave({});
  if (rsDefaults) {
    Object.keys(RS_LABELS).forEach((key) => {
      const slider = document.getElementById(`rs-${key}`);
      if (slider) {
        slider.value = rsDefaults[key];
        slider.nextElementSibling.textContent = rsDefaults[key];
      }
    });
  }
});

async function loadSuggestions() {
  try {
    const res  = await fetch("/api/query/suggestions");
    const data = await res.json();
    const el   = document.getElementById("query-chips");
    el.innerHTML = "";
    (data.suggestions || []).forEach((q) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = q;
      chip.addEventListener("click", () => submitQuery(q));
      el.appendChild(chip);
    });
  } catch (_) {}
}

// ── Input handlers ───────────────────────────────────────────
const queryInput   = document.getElementById("query-input");
const querySendBtn = document.getElementById("query-send-btn");

querySendBtn.addEventListener("click", () => {
  const q = queryInput.value.trim();
  if (q) submitQuery(q);
});

queryInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    const q = queryInput.value.trim();
    if (q) submitQuery(q);
  }
});

// ── Submit query ─────────────────────────────────────────────
async function submitQuery(question) {
  queryInput.value = "";
  querySendBtn.disabled = true;

  const messagesEl = document.getElementById("query-messages");

  // User bubble
  const pair = document.createElement("div");
  pair.className = "msg-pair";
  pair.innerHTML = `<div class="msg-user">${escHtml(question)}</div>`;

  // Thinking bubble
  const thinking = document.createElement("div");
  thinking.className = "msg-thinking";
  thinking.textContent = "Searching graph and synthesising answer…";
  pair.appendChild(thinking);
  messagesEl.appendChild(pair);
  messagesEl.scrollTop = messagesEl.scrollHeight;

  try {
    const res = await fetch("/api/query/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        query_type: document.getElementById("query-type").value,
        platform: getSelectedPlatform(),   // "" → all documents
        // session-scoped retrieval overrides; {} → server .env defaults
        ...getRetrievalOverrides(),
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    const data = await res.json();
    thinking.replaceWith(renderAgentMsg(data));
  } catch (err) {
    thinking.className = "msg-agent";
    thinking.style.color = "var(--red)";
    thinking.textContent = "Error: " + err.message;
  } finally {
    querySendBtn.disabled = false;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }
}

// ── Render agent answer ──────────────────────────────────────
function renderAgentMsg(data) {
  const el = document.createElement("div");
  el.className = "msg-agent";

  const html = simpleMarkdown(data.answer || "_(no answer — the LLM returned an empty response. Try a more specific question or increase MAX_QUERY_TOKENS in .env)_");
  el.innerHTML = html;

  // Title-matched documents — "this deck answers your question"
  if (data.matched_documents?.length) {
    const md = document.createElement("div");
    md.className = "matched-docs";
    md.innerHTML = "<span class='matched-docs-label'>📄 Matching documents:</span>";
    data.matched_documents.forEach((d) => {
      if (d.doc_url) {
        const a = document.createElement("a");
        a.className = "matched-doc-link";
        a.href = d.doc_url;
        a.target = "_blank";
        a.rel = "noopener";
        a.textContent = d.filename;
        a.title = "Open this document — its title matches your question";
        md.appendChild(a);
      } else {
        const s = document.createElement("span");
        s.className = "matched-doc-link";
        s.textContent = d.filename;
        md.appendChild(s);
      }
    });
    el.prepend(md);
  }

  // Planner rewrote the question (typo fixes etc.) — show what was searched
  if (data.rewritten_question) {
    const rw = document.createElement("div");
    rw.className = "rewritten-note muted";
    rw.textContent = "Interpreted as: " + data.rewritten_question;
    el.prepend(rw);
  }

  // Compound question (M3) — planner split it into up to 3 sub-questions,
  // each retrieved separately then merged into one synthesis (still 2 LLM
  // calls total). Show the user exactly how many parts and what they were.
  if (data.sub_questions?.length > 1) {
    const sq = document.createElement("div");
    sq.className = "subq-note";
    const items = data.sub_questions
      .map((q, i) => `<li><span class="subq-tag">Q${i + 1}</span>${escHtml(q)}</li>`)
      .join("");
    sq.innerHTML =
      `<div class="subq-head">Interpreted as ${data.sub_questions.length} questions:</div>` +
      `<ol>${items}</ol>`;
    el.prepend(sq);
  }

  // Dual-track breakdown (M2) — which two tracks were searched, with chunk counts
  if (data.tracks && data.tracks.dual) {
    const t = data.tracks;
    const tr = document.createElement("div");
    tr.className = "track-note";
    const a = `${escHtml(t.track_a.label)} (${t.track_a.chunks} chunk${t.track_a.chunks === 1 ? "" : "s"}${t.track_a.scope != null ? `, ${t.track_a.scope} docs` : ""})`;
    const b = `${escHtml(t.track_b.label)} (${t.track_b.chunks} chunk${t.track_b.chunks === 1 ? "" : "s"}${t.track_b.scope != null ? `, ${t.track_b.scope} docs` : ""})`;
    const both = t.both_chunks ? ` · ${t.both_chunks} in both` : "";
    tr.innerHTML = `<span class="track-pill track-a">${a}</span><span class="track-plus">+</span><span class="track-pill track-b">${b}</span>${both}`;
    el.prepend(tr);
  } else if (data.platform) {
    // Single-track scope note (no C&M, or user typed the SF value)
    const sc = document.createElement("div");
    sc.className = "rewritten-note muted";
    const n = data.scope_doc_count;
    sc.textContent = `Scoped to ${data.platform}` +
      (n != null ? ` (${n} document${n === 1 ? "" : "s"})` : "");
    el.prepend(sc);
  }

  // Meta pills — chunks + communities are clickable if details are available
  const meta = document.createElement("div");
  meta.className = "meta";

  const qpill = document.createElement("span");
  qpill.className = "meta-pill";
  qpill.textContent = `${data.query_type?.toUpperCase()} query`;
  meta.appendChild(qpill);

  // Entities — clickable to see WHICH entities (not just a count) and open a
  // focused interactive graph of exactly them.
  if (data.entities_found > 0) {
    const pill = document.createElement("span");
    const hasDetails = (data.entity_details || []).length > 0;
    pill.className = hasDetails ? "meta-pill clickable" : "meta-pill";
    pill.textContent = `${data.entities_found} entities`;
    if (hasDetails) {
      pill.title = "Click to see which entities — and view them as a graph";
      pill.addEventListener("click", () => openDrawer("entities", data.entity_details));
    }
    meta.appendChild(pill);
  }

  if (data.chunks_cited > 0) {
    const pill = document.createElement("span");
    pill.className = "meta-pill clickable";
    pill.textContent = `${data.chunks_cited} chunks`;
    pill.title = "Click to see source chunks";
    pill.addEventListener("click", () => openDrawer("chunks", data.chunk_details || []));
    meta.appendChild(pill);
  }

  if (data.communities_used > 0) {
    const pill = document.createElement("span");
    pill.className = "meta-pill clickable";
    pill.textContent = `${data.communities_used} communities`;
    pill.title = "Click to see community summaries";
    pill.addEventListener("click", () => openDrawer("communities", data.community_details || []));
    meta.appendChild(pill);
  }

  el.appendChild(meta);

  // Also-try chips
  if (data.also_try?.length) {
    const at = document.createElement("div");
    at.className = "also-try";
    at.innerHTML = "<span>Also try: </span>";
    data.also_try.forEach((q) => {
      const chip = document.createElement("span");
      chip.className = "also-chip";
      chip.textContent = q;
      chip.addEventListener("click", () => submitQuery(q));
      at.appendChild(chip);
    });
    el.appendChild(at);
  }

  return el;
}

// ── Context drawer ────────────────────────────────────────────
// Inject drawer + overlay once into the page
const _drawerOverlay = document.createElement("div");
_drawerOverlay.className = "drawer-overlay";
document.body.appendChild(_drawerOverlay);

const _drawer = document.createElement("div");
_drawer.className = "context-drawer";
_drawer.innerHTML = `
  <div class="drawer-header">
    <h3 id="drawer-title">Context</h3>
    <button class="drawer-close" id="drawer-close-btn">✕</button>
  </div>
  <div class="drawer-body" id="drawer-body"></div>`;
document.body.appendChild(_drawer);

document.getElementById("drawer-close-btn").addEventListener("click", closeDrawer);
_drawerOverlay.addEventListener("click", closeDrawer);

function openDrawer(type, items) {
  const title  = document.getElementById("drawer-title");
  const body   = document.getElementById("drawer-body");

  if (type === "entities") {
    title.textContent = `Answer Entities (${items.length})`;
    body.innerHTML = "";
    if (!items.length) {
      body.innerHTML = '<p class="muted">No entities were matched for this answer.</p>';
    } else {
      // "View as interactive graph" — focused graph of exactly these entities.
      const ids = items.map((e) => e.id).filter(Boolean).join(",");
      const graphBtn = document.createElement("a");
      graphBtn.className = "btn primary drawer-graph-btn";
      graphBtn.href = `/api/query/entity-graph?ids=${encodeURIComponent(ids)}`;
      graphBtn.target = "_blank";
      graphBtn.rel = "noopener";
      graphBtn.textContent = "Open interactive graph ↗";
      graphBtn.title = "Show these entities (highlighted) and their neighbours as a graph";
      body.appendChild(graphBtn);

      items.forEach((e) => {
        const item = document.createElement("div");
        item.className = "drawer-item";
        const docs = (e.source_docs || []).length
          ? `<div class="drawer-item-text">${e.source_docs.map(escHtml).join(", ")}</div>` : "";
        item.innerHTML = `
          <div class="drawer-item-title">${escHtml(e.name || e.id)}
            <span class="drawer-track">${escHtml(e.type || "unknown")}</span>
          </div>${docs}`;
        body.appendChild(item);
      });
    }
    _drawerOverlay.classList.add("open");
    _drawer.classList.add("open");
    return;
  }

  if (type === "chunks") {
    title.textContent = `Source Chunks (${items.length})`;
    body.innerHTML = "";
    if (!items.length) {
      body.innerHTML = '<p class="muted">No chunks available.</p>';
    }
    items.forEach((c, i) => {
      const item = document.createElement("div");
      item.className = "drawer-item";
      const docLink = c.doc_url
        ? `<a class="file-view-link" href="${escHtml(c.doc_url)}" target="_blank" rel="noopener" title="Open the source document">Open ↗</a>`
        : "";
      const trackTag = c.track
        ? `<span class="drawer-track">${escHtml(c.track)}</span>` : "";
      item.innerHTML = `
        <div class="drawer-item-label">Chunk ${i + 1} ${trackTag}</div>
        <div class="drawer-item-title">${escHtml(c.filename || "?")}
          ${c.page ? ` · p.${c.page}` : ""}
          ${c.section ? ` · ${escHtml(c.section)}` : ""}
          ${docLink}
        </div>
        <div class="drawer-item-text">${escHtml(c.text || "")}</div>`;
      body.appendChild(item);
    });
  } else {
    title.textContent = `Communities (${items.length})`;
    body.innerHTML = "";
    if (!items.length) {
      body.innerHTML = '<p class="muted">No communities matched this query.</p>';
    }
    items.forEach((c) => {
      const item = document.createElement("div");
      item.className = "drawer-item";
      const tags = (c.entities || []).map(e =>
        `<span class="drawer-tag">${escHtml(e)}</span>`).join("");
      item.innerHTML = `
        <div class="drawer-item-label">Community ${escHtml(String(c.id))}</div>
        ${tags ? `<div class="drawer-item-tags">${tags}</div>` : ""}
        <div class="drawer-item-text">${escHtml(c.summary || "")}</div>`;
      body.appendChild(item);
    });
  }

  _drawerOverlay.classList.add("open");
  _drawer.classList.add("open");
}

function closeDrawer() {
  _drawerOverlay.classList.remove("open");
  _drawer.classList.remove("open");
}

// ── Minimal markdown → HTML ──────────────────────────────────
function simpleMarkdown(text) {
  return text
    // Bold
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // Italic citations  *(source, p.N)*
    .replace(/\*\((.+?)\)\*/g, "<em>($1)</em>")
    // Inline code
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    // Table rows
    .replace(/^\|(.+)\|$/gm, (row) => {
      const cells = row.split("|").slice(1, -1);
      return "<tr>" + cells.map((c) => `<td>${c.trim()}</td>`).join("") + "</tr>";
    })
    // Wrap consecutive <tr> lines in <table>
    .replace(/((?:<tr>.*<\/tr>\n?)+)/g, "<table>$1</table>")
    // Header rows (separator lines like | --- |)
    .replace(/<tr><td>[-: ]+<\/td>.*?<\/tr>/g, "")
    // Bullet points
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/((?:<li>.*<\/li>\n?)+)/g, "<ul>$1</ul>")
    // Newlines → <br> (outside block elements)
    .replace(/\n/g, "<br>");
}

// ═══════════════════════════════════════════════════════════════
// ── TAB 4: Batch Q&A (CSV in, answers out) ───────────────────
// ═══════════════════════════════════════════════════════════════

let batchUploadId  = null;
let batchJobId     = null;
let batchPollTimer = null;
let batchSource    = "csv";     // "csv" | "rfp"

// ── Source toggle (Questions CSV ↔ RFP document) ─────────────
function applyBatchSource() {
  const isRfp = batchSource === "rfp";
  const fileInput = document.getElementById("batch-file");
  const btn = document.getElementById("batch-upload-btn");
  const hint = document.getElementById("batch-upload-hint");
  fileInput.value = "";
  fileInput.accept = isRfp ? ".pdf,.docx,.doc" : ".csv";
  btn.textContent = isRfp ? "Upload RFP" : "Upload CSV";
  hint.innerHTML = isRfp
    ? "Upload the client RFP (PDF or DOCX). The model extracts every bidder " +
      "question, then answers each one — the RFP itself is never added to the corpus."
    : "One question per row. The column named “Question” is used — otherwise the " +
      "first column. All your other columns are preserved in the download.";
  document.getElementById("batch-upload-result").classList.add("hidden");
}

document.getElementById("batch-source-toggle")?.addEventListener("click", (e) => {
  const seg = e.target.closest(".seg");
  if (!seg) return;
  batchSource = seg.dataset.source;
  document.querySelectorAll("#batch-source-toggle .seg").forEach((s) =>
    s.classList.toggle("active", s === seg));
  applyBatchSource();
});

async function initBatchTab() {
  const statusEl = document.getElementById("batch-prereq-status");
  const uploadCard = document.getElementById("batch-upload-card");
  statusEl.innerHTML = '<span class="muted">Checking prerequisites…</span>';
  uploadCard.classList.add("hidden");

  try {
    const data = await (await fetch("/api/query/prerequisites")).json();
    if (!data.ready) {
      statusEl.innerHTML =
        '<span class="prereq-warn">⚠ Graph not ready. Complete Data Prep (Tab 1) first.</span>';
      return;
    }
    const warn = data.summaries_warning
      ? ' <span class="prereq-warn">· Community summaries missing — global answers may be weak</span>'
      : "";
    statusEl.innerHTML =
      `<span class="prereq-ok">✓ Graph ready — ${data.entities} entities · ` +
      `${data.communities} communities · ${data.summaries} summaries</span>${warn}`;
    uploadCard.classList.remove("hidden");

    // Batch reuses the Query tab's session retrieval settings
    const o = getRetrievalOverrides();
    const note = document.getElementById("batch-settings-note");
    note.textContent = Object.keys(o).length
      ? "Using your custom retrieval settings from the Query tab: " +
        Object.entries(o).map(([k, v]) => `${k}=${v}`).join(", ")
      : "Using default retrieval settings (change them in the Query tab's gear panel).";

    // Platform scope for the whole batch (dual-track), same source as Query tab
    loadBatchPlatformScope();
  } catch (err) {
    statusEl.innerHTML = `<span class="prereq-err">Error: ${err.message}</span>`;
  }
}

async function loadBatchPlatformScope() {
  const row = document.getElementById("batch-scope-row");
  const note = document.getElementById("batch-scope-note");
  if (!row) return;
  try {
    const data = await (await fetch("/api/query/scopes")).json();
    if (!data.scoping_available) {
      // Make the absence explicit rather than silently omitting the control.
      row.classList.add("hidden");
      note?.classList.remove("hidden");
      return;
    }
    note?.classList.add("hidden");
    const sel = document.getElementById("batch-scope-platform");
    fillScopeSelect(sel, data.scopes || []);
    row.classList.remove("hidden");
    sel.onchange = async () => {
      const cnt = document.getElementById("batch-scope-count");
      if (!sel.value) { cnt.textContent = ""; return; }
      const d = await (await fetch("/api/query/scope-count?platform=" + encodeURIComponent(sel.value))).json();
      cnt.textContent = `${d.count} in scope + Clients and Markets`;
    };
  } catch (_) { row.classList.add("hidden"); }
}

// ── Upload ───────────────────────────────────────────────────
document.getElementById("batch-upload-btn")?.addEventListener("click", async () => {
  const input = document.getElementById("batch-file");
  const file = input.files?.[0];
  if (!file) { input.focus(); return; }

  const isRfp = batchSource === "rfp";
  const btn = document.getElementById("batch-upload-btn");
  btn.disabled = true;
  btn.textContent = isRfp ? "Extracting questions…" : "Uploading…";
  document.getElementById("batch-upload-result").classList.add("hidden");

  try {
    const fd = new FormData();
    fd.append("file", file);
    const url = isRfp ? "/api/batch/upload-rfp" : "/api/batch/upload";
    const res = await fetch(url, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    renderBatchUpload(data);
  } catch (err) {
    alert("Upload error: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = isRfp ? "Upload RFP" : "Upload CSV";
  }
});

function renderBatchUpload(data) {
  batchUploadId = data.upload_id;

  const srcNote = data.source === "rfp"
    ? `<span class="types">from ${escHtml(data.filename)}</span>`
    : `<span class="types">column: ${escHtml(data.question_column)}</span>`;
  document.getElementById("batch-summary").innerHTML = `
    <span class="count">${data.count}</span>
    <span>question${data.count !== 1 ? "s" : ""} ${data.source === "rfp" ? "extracted" : "found"}</span>
    ${srcNote}
    <span class="types">${data.llm_calls} LLM calls</span>
  `;

  const prev = document.getElementById("batch-preview");
  prev.innerHTML = "";
  data.preview.forEach((q, i) => {
    const row = document.createElement("div");
    row.className = "file-item";
    row.innerHTML = `<span class="file-item-name" title="${escHtml(q)}">${i + 1}. ${escHtml(q)}</span>`;
    prev.appendChild(row);
  });
  if (data.count > data.preview.length) {
    const more = document.createElement("div");
    more.className = "file-item";
    more.innerHTML = `<span class="file-item-name muted">… and ${data.count - data.preview.length} more</span>`;
    prev.appendChild(more);
  }

  document.getElementById("batch-upload-result").classList.remove("hidden");
}

// ── Run ──────────────────────────────────────────────────────
document.getElementById("batch-run-btn")?.addEventListener("click", async () => {
  if (!batchUploadId) return;
  const btn = document.getElementById("batch-run-btn");
  btn.disabled = true;
  btn.textContent = "Running…";

  document.getElementById("batch-result-card").classList.add("hidden");
  document.getElementById("batch-partial-badge").classList.add("hidden");
  document.getElementById("batch-progress-card").classList.remove("hidden");
  document.getElementById("batch-log").textContent = "";
  document.getElementById("batch-stop-btn").disabled = false;
  setBatchProgress(0, "Starting…");

  try {
    const res = await fetch("/api/batch/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_id: batchUploadId,
        query_type: document.getElementById("batch-query-type").value,
        platform: document.getElementById("batch-scope-platform")?.value || "",
        ...getRetrievalOverrides(),
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    batchJobId = data.job_id;
    pollBatchStatus(batchJobId);
  } catch (err) {
    appendBatchLog("ERROR: " + err.message);
    resetBatchBtn();
  }
});

document.getElementById("batch-stop-btn")?.addEventListener("click", async () => {
  if (!batchJobId) return;
  const btn = document.getElementById("batch-stop-btn");
  btn.disabled = true;
  btn.textContent = "Stopping…";
  try {
    await fetch(`/api/batch/cancel/${batchJobId}`, { method: "POST" });
    appendBatchLog("Stop requested — finishing in-flight questions, answers so far are kept…");
  } catch (err) {
    appendBatchLog("Stop error: " + err.message);
  }
});

function pollBatchStatus(jobId) {
  clearInterval(batchPollTimer);
  batchPollTimer = setInterval(async () => {
    try {
      const job = await (await fetch(`/api/batch/status/${jobId}`)).json();
      const el = document.getElementById("batch-log");
      if (job.logs?.length) {
        el.textContent = job.logs.map((l) => l.message).join("\n");
        el.scrollTop = el.scrollHeight;
      }
      setBatchProgress(job.progress * 100,
        job.stage === "answering" ? `Answering with ${window.MODEL_LABEL || "LLM"}` :
        job.stage === "done" ? "Complete" : job.stage || "Working");

      // Live/incremental: stream answers into the table as they complete.
      if (job.partial?.rows && job.status !== "completed") {
        renderBatchLive(job.partial, jobId);
      }

      if (job.status === "completed") {
        clearInterval(batchPollTimer);
        renderBatchResult(job.result, jobId);
        resetBatchBtn();
        document.getElementById("batch-stop-btn").disabled = true;
      } else if (job.status === "failed") {
        clearInterval(batchPollTimer);
        appendBatchLog("FAILED: " + (job.error || "unknown error"));
        resetBatchBtn();
        document.getElementById("batch-stop-btn").disabled = true;
      }
    } catch (err) {
      appendBatchLog("Polling error: " + err.message);
    }
  }, 1500);
}

// Shared table renderer — question · status · time · answer snippet.
// Pending rows (no Status yet) render greyed so the live view shows the full set.
function renderBatchTable(rows, qCol) {
  const table = document.getElementById("batch-table");
  table.innerHTML =
    "<thead><tr><th>#</th><th>Question</th><th>Status</th><th>Time</th><th>Answer</th></tr></thead>";
  const tbody = document.createElement("tbody");
  rows.forEach((r, i) => {
    const status = r.Status || "";
    const pending = !status;
    const cls = pending ? "batch-pending"
      : status.startsWith("NO CONTENT") || status.startsWith("GAP") ? "batch-warn"
      : status.startsWith("ERROR") ? "batch-err" : "batch-ok";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="num">${i + 1}</td>
      <td class="batch-q">${escHtml(r[qCol])}</td>
      <td><span class="${cls}">${escHtml(status || "pending…")}</span></td>
      <td class="num">${r["Time (s)"] ? escHtml(r["Time (s)"]) + "s" : "—"}</td>
      <td class="batch-a">${escHtml((r.Answer || "").slice(0, 220))}${(r.Answer || "").length > 220 ? "…" : ""}</td>`;
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
}

function renderBatchStats(cells) {
  document.getElementById("batch-stats").innerHTML = cells.map(([label, num]) =>
    `<div class="stat-box"><div class="num">${num}</div><div class="label">${label}</div></div>`
  ).join("");
}

// Live view during a run — fills in as answers complete; partial CSV downloadable.
function renderBatchLive(partial, jobId) {
  document.getElementById("batch-live-badge").classList.remove("hidden");
  const qCol = partial.question_column;
  const rows = (partial.rows || []).filter((r) => r[qCol]);
  renderBatchStats([
    ["Questions", partial.total ?? rows.length],
    ["Answered", partial.answered ?? 0],
    ["Completed", partial.done ?? 0],
    ["Remaining", Math.max(0, (partial.total ?? rows.length) - (partial.done ?? 0))],
  ]);
  document.getElementById("batch-download").href = `/api/batch/download/${jobId}`;
  renderBatchTable(rows, qCol);
  document.getElementById("batch-result-card").classList.remove("hidden");
}

function renderBatchResult(result, jobId) {
  if (!result) return;
  document.getElementById("batch-live-badge").classList.add("hidden");
  if (result.was_cancelled) {
    document.getElementById("batch-partial-badge").classList.remove("hidden");
  }

  renderBatchStats([
    ["Questions", result.total ?? "—"],
    ["Answered", result.answered ?? "—"],
    ["Needs content", result.no_content ?? 0],
    ["Errors", result.errors ?? 0],
  ]);

  document.getElementById("batch-download").href = `/api/batch/download/${jobId}`;

  const qCol = result.question_column;
  renderBatchTable((result.rows || []).filter((r) => r[qCol]), qCol);

  document.getElementById("batch-result-card").classList.remove("hidden");
}

function setBatchProgress(pct, stage) {
  const p = Math.max(0, Math.min(100, pct));
  document.getElementById("batch-fill").style.width = p + "%";
  document.getElementById("batch-pct").textContent = Math.round(p) + "%";
  if (stage) document.getElementById("batch-stage").textContent = stage;
}

function appendBatchLog(msg) {
  const el = document.getElementById("batch-log");
  el.textContent += (el.textContent ? "\n" : "") + msg;
  el.scrollTop = el.scrollHeight;
}

function resetBatchBtn() {
  const btn = document.getElementById("batch-run-btn");
  btn.disabled = false;
  btn.textContent = "Answer all questions";
  const stop = document.getElementById("batch-stop-btn");
  stop.textContent = "Stop & Save";
}
