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
  });
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

  scanSummary.innerHTML = `
    <span class="count">${data.count}</span>
    <span>document${data.count !== 1 ? "s" : ""} found</span>
    ${typeStr ? `<span class="types">${typeStr}</span>` : ""}
    ${extractedInList ? `<span class="types">${extractedInList} already extracted · ${newCount} new</span>` : ""}
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
  } catch (_) { /* no graph yet — nothing to show */ }
}
checkExistingGraph();

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
    return;
  }

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
  loadSuggestions();
}

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

  // Meta pills — chunks + communities are clickable if details are available
  const meta = document.createElement("div");
  meta.className = "meta";

  const staticPills = [
    `${data.query_type?.toUpperCase()} query`,
    `${data.entities_found} entities`,
  ];
  staticPills.forEach((label) => {
    const pill = document.createElement("span");
    pill.className = "meta-pill";
    pill.textContent = label;
    meta.appendChild(pill);
  });

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
        ? `<a class="file-view-link" href="${escHtml(c.doc_url)}" target="_blank" rel="noopener" title="Open the source RFP from Azure Blob">Open RFP ↗</a>`
        : "";
      item.innerHTML = `
        <div class="drawer-item-label">Chunk ${i + 1}</div>
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
