"use strict";

const PAGE_SIZE = 100;
const $ = (id) => document.getElementById(id);
const state = {
  status: null,
  tab: "explorer",
  folder: "",
  trail: [],
  sort: "size",
  order: "desc",
  browseOffset: 0,
  fileOffset: 0,
  pollTimer: null,
  browseRequest: 0,
  filesRequest: 0,
  searchTimer: null,
  trashing: false,
  resultGeneration: 0,
  breakdownRequest: 0,
  chartView: "donut",
};

async function api(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { cache: "no-store", ...options });
  } catch {
    throw new Error("Cannot connect to Folder Analyzer. Check that the local server is running.");
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(`The server returned an unreadable response (${response.status}).`);
  }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status}).`);
  return data;
}

function post(url, body) {
  return api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

function showError(error) {
  const notice = $("notice");
  notice.textContent = error instanceof Error ? error.message : String(error);
  notice.hidden = false;
}

function clearError() {
  $("notice").hidden = true;
  $("notice").textContent = "";
}

function formatSize(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes.toLocaleString()} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)) - 1, units.length - 1);
  const amount = bytes / 1024 ** (exponent + 1);
  return `${amount.toLocaleString(undefined, { maximumFractionDigits: amount >= 10 ? 1 : 2 })} ${units[exponent]}`;
}

function formatCount(value) { return (Number(value) || 0).toLocaleString(); }

function formatDuration(seconds) {
  const n = Number(seconds) || 0;
  if (n < 60) return `${n.toFixed(n < 10 ? 1 : 0)}s`;
  const mins = Math.floor(n / 60);
  return `${mins}m ${Math.floor(n % 60)}s`;
}

function formatDate(value) {
  if (value === null || value === undefined || value === "") return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

function nameOf(path) {
  const parts = String(path || "").replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || path || "Root";
}

function node(tag, className, value) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (value !== undefined) el.textContent = String(value);
  return el;
}

function setStatus(data) {
  const previous = state.status;
  state.status = data;
  const scanning = data.state === "scanning";
  $("progress").hidden = !scanning;
  $("scan-button").disabled = scanning || state.trashing;
  $("browse-button").disabled = scanning || state.trashing;
  $("folder-path").disabled = scanning || state.trashing;
  $("cancel-button").disabled = state.trashing;
  $("progress-label").textContent = `Scanning · ${formatCount(data.files)} files, ${formatCount(data.folders)} folders`;
  $("current-path").textContent = data.current_path || data.root || "Reading folder…";

  if (scanning) {
    if (previous?.state !== "scanning" || previous?.root !== data.root) state.resultGeneration++;
    $("welcome").hidden = true;
    $("results").hidden = false;
    $("results-subtitle").textContent = data.root || "Scan in progress";
    $("export-link").hidden = true;
    document.querySelector(".workspace").hidden = true;
    updateStats(data);
    if (!state.pollTimer) state.pollTimer = window.setTimeout(pollStatus, 650);
    return;
  }
  window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  if (data.state === "idle" || !data.root) {
    $("results").hidden = true;
    $("welcome").hidden = false;
    return;
  }
  $("results").hidden = false;
  $("welcome").hidden = true;
  document.querySelector(".workspace").hidden = false;
  updateStats(data);
  const qualifier = data.state === "cancelled" ? "Partial scan · " : data.state === "error" ? "Scan stopped · " : "";
  $("results-subtitle").textContent = qualifier + data.root;
  $("export-link").hidden = data.state !== "complete";
  if (data.state === "error" && data.error) showError(new Error(data.error));
  if (data.state === "cancelled" && previous?.state === "scanning") showError(new Error("Scan cancelled. Showing results collected so far."));
  if (!state.folder || previous?.root !== data.root || previous?.state === "scanning") {
    state.folder = data.root;
    state.trail = [data.root];
    state.browseOffset = 0;
    state.fileOffset = 0;
    loadVisibleTab();
  }
}

function updateStats(data) {
  $("stat-size").textContent = formatSize(data.total_bytes);
  $("stat-files").textContent = formatCount(data.files);
  $("stat-folders").textContent = formatCount(data.folders);
  $("stat-skipped").textContent = formatCount(data.skipped);
  $("stat-elapsed").textContent = formatDuration(data.elapsed);
}

async function pollStatus() {
  state.pollTimer = null;
  try {
    setStatus(await api("/api/status"));
  } catch (error) {
    showError(error);
    state.pollTimer = window.setTimeout(pollStatus, 2000);
  }
}

async function startScan(event) {
  event.preventDefault();
  if (state.trashing) return;
  const path = $("folder-path").value.trim();
  if (!path) return;
  clearError();
  try {
    await post("/api/scan", { path });
    setStatus({ state: "scanning", root: path, files: 0, folders: 0, total_bytes: 0, skipped: 0, elapsed: 0 });
    await pollStatus();
  } catch (error) { showError(error); }
}

async function browseFolder() {
  if (state.trashing) return;
  clearError();
  $("browse-button").disabled = true;
  try {
    const data = await post("/api/pick-folder", {});
    if (data.path) $("folder-path").value = data.path;
  } catch (error) { showError(error); }
  finally { $("browse-button").disabled = state.trashing || state.status?.state === "scanning"; }
}

async function cancelScan() {
  if (state.trashing) return;
  $("cancel-button").disabled = true;
  try { await post("/api/cancel", {}); await pollStatus(); }
  catch (error) { showError(error); }
  finally { $("cancel-button").disabled = state.trashing; }
}

function setTrashBusy(busy) {
  state.trashing = busy;
  const scanning = state.status?.state === "scanning";
  $("scan-button").disabled = busy || scanning;
  $("browse-button").disabled = busy || scanning;
  $("folder-path").disabled = busy || scanning;
  $("cancel-button").disabled = busy;
  for (const button of document.querySelectorAll(".trash-button")) button.disabled = busy;
}

async function trashFolder(path) {
  if (state.trashing || state.status?.state === "scanning" || path === state.status?.root) return;
  const confirmed = await confirmTrash(path);
  if (!confirmed) return;
  clearError();
  setTrashBusy(true);
  state.resultGeneration++;
  try {
    const result = await post("/api/trash", { path });
    state.browseRequest++;
    state.filesRequest++;
    state.folder = state.status.root;
    state.trail = [state.status.root];
    state.browseOffset = 0;
    state.fileOffset = 0;
    if (result.scan) setStatus(result.scan);
    await pollStatus();
  } catch (error) { showError(error); }
  finally { setTrashBusy(false); }
}

function confirmTrash(path) {
  const dialog = $("trash-dialog");
  $("trash-path").textContent = path;
  dialog.returnValue = "";
  dialog.showModal();
  $("trash-cancel").focus();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

function selectTab(tab, focus = false) {
  state.tab = tab;
  for (const button of document.querySelectorAll("[data-tab]")) {
    const active = button.dataset.tab === tab;
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
    $("panel-" + button.dataset.tab).hidden = !active;
    if (active && focus) button.focus();
  }
  loadVisibleTab();
}

function loadVisibleTab() {
  if (!state.status || !["complete", "cancelled", "error"].includes(state.status.state)) return;
  if (state.tab === "explorer") { loadBrowse(); loadBreakdown(); }
  if (state.tab === "files") loadFiles();
  if (state.tab === "types") loadTypes();
  if (state.tab === "errors") loadErrors();
}

function navigate(path) {
  const index = state.trail.indexOf(path);
  state.trail = index >= 0 ? state.trail.slice(0, index + 1) : [...state.trail, path];
  state.folder = path;
  state.browseOffset = 0;
  loadBrowse();
  loadBreakdown();
}

function renderBreadcrumbs(data) {
  const container = $("breadcrumbs");
  container.replaceChildren();
  state.trail.forEach((path, index) => {
    if (index) container.append(node("span", "crumb-separator", "/"));
    const crumb = node("button", "crumb" + (path === state.folder ? " current" : ""), index === 0 ? path : nameOf(path));
    crumb.type = "button";
    crumb.title = path;
    if (path === state.folder) crumb.setAttribute("aria-current", "location");
    else crumb.addEventListener("click", () => navigate(path));
    container.append(crumb);
  });
}

function renderMap(items) {
  const container = $("folder-map");
  container.replaceChildren();
  const visible = items.filter((item) => Number(item.size) > 0).slice(0, 4);
  if (!visible.length) return;
  const max = Math.max(...visible.map((item) => Number(item.size)));
  visible.forEach((item) => {
    const isFolder = item.kind === "folder";
    const share = max ? Math.max(3, Number(item.size) / max * 100) : 0;
    const card = node("button", "map-item");
    card.type = "button";
    card.disabled = !isFolder;
    card.title = isFolder ? `Open ${item.path}` : item.path;
    card.append(node("span", "map-name", item.name), node("span", "map-size", formatSize(item.size)), node("span", "map-share", isFolder ? "OPEN FOLDER →" : "FILE"));
    const fill = node("span", "map-fill");
    fill.style.width = `${share}%`;
    card.append(fill);
    if (isFolder) card.addEventListener("click", () => navigate(item.path));
    container.append(card);
  });
}

const CHART_COLORS = ["#50dbbe", "#85b7ef", "#f2b979", "#bba8ef", "#ec8fa1", "#88d1d3", "#bbd88d", "#e5a4dd", "#7f9cf0", "#d7cb87", "#70b5a2", "#da9b77", "#7893a1"];

function setChartView(view) {
  state.chartView = view;
  $("donut-view").hidden = view !== "donut";
  $("folder-map").hidden = view !== "bars";
  for (const name of ["donut", "bars"]) {
    const button = $("view-" + name);
    button.classList.toggle("active", name === view);
    button.setAttribute("aria-pressed", String(name === view));
  }
}

function renderBreakdown(data) {
  const items = data.items || [];
  const total = Math.max(0, Number(data.total_bytes) || 0);
  const hasChart = total > 0 && items.some((item) => Number(item.size) > 0);
  $("donut-empty").hidden = hasChart;
  if (!hasChart) $("donut-empty").textContent = "No file data to chart in this folder.";
  $("viz-heading").hidden = !hasChart;
  $("donut-total").textContent = formatSize(total);
  const svg = $("donut-svg");
  svg.querySelectorAll("circle").forEach((circle) => circle.remove());
  const legend = $("donut-legend");
  legend.replaceChildren();
  renderMap(items.filter((item) => item.kind !== "other"));
  if (!hasChart) {
    $("donut-view").hidden = true;
    $("folder-map").hidden = true;
    return;
  }
  const radius = 82;
  const circumference = 2 * Math.PI * radius;
  let used = 0;
  items.forEach((item, index) => {
    const size = Math.max(0, Number(item.size) || 0);
    if (size === 0) return;
    const color = CHART_COLORS[index % CHART_COLORS.length];
    const percent = size / total * 100;
    const label = `${item.name}: ${formatSize(size)} (${percent.toFixed(1)}%)`;
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("class", "donut-segment" + (item.kind === "folder" ? " interactive" : ""));
    circle.setAttribute("cx", "120");
    circle.setAttribute("cy", "120");
    circle.setAttribute("r", String(radius));
    circle.setAttribute("fill", "none");
    circle.setAttribute("stroke", color);
    circle.setAttribute("stroke-width", "38");
    const length = circumference * size / total;
    circle.setAttribute("stroke-dasharray", `${Math.max(0, length - 1)} ${circumference - Math.max(0, length - 1)}`);
    circle.setAttribute("stroke-dashoffset", String(-used));
    used += length;
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = label;
    circle.append(title);
    if (item.kind === "folder") circle.addEventListener("click", () => navigate(item.path));
    svg.append(circle);

    const button = node("button", "legend-row" + (item.kind === "folder" ? " interactive" : ""));
    button.type = "button";
    button.disabled = item.kind !== "folder";
    button.title = item.kind === "folder" ? `Open ${item.path}` : label;
    button.setAttribute("aria-label", item.kind === "folder" ? `Open ${item.name}, ${formatSize(size)}, ${percent.toFixed(1)} percent` : label);
    const swatch = node("span", "legend-swatch");
    swatch.style.backgroundColor = color;
    swatch.setAttribute("aria-hidden", "true");
    button.append(swatch, node("span", "legend-name", item.name), node("span", "legend-value", `${formatSize(size)} · ${percent.toFixed(1)}%`));
    if (item.kind === "folder") button.addEventListener("click", () => navigate(item.path));
    legend.append(button);
  });
  setChartView(state.chartView);
}

async function loadBreakdown() {
  const request = ++state.breakdownRequest;
  const generation = state.resultGeneration;
  const path = state.folder;
  $("donut-empty").hidden = false;
  $("donut-empty").textContent = "Loading folder breakdown…";
  $("donut-view").hidden = true;
  $("folder-map").hidden = true;
  try {
    const data = await api(`/api/breakdown?${new URLSearchParams({ path })}`);
    if (request !== state.breakdownRequest || generation !== state.resultGeneration || path !== state.folder) return;
    renderBreakdown(data);
  } catch (error) {
    if (request === state.breakdownRequest && generation === state.resultGeneration) {
      $("donut-empty").textContent = "Folder breakdown could not be loaded.";
      showError(error);
    }
  }
}

function addNameCell(row, item, clickable, showPath = false) {
  const cell = node("td");
  const content = node("span", "name-content");
  const icon = node("span", "item-icon" + (item.kind === "file" ? " file" : ""), item.kind === "folder" ? "▰" : "≡");
  icon.setAttribute("aria-hidden", "true");
  const wrapper = node("span", "name-wrapper");
  if (clickable) {
    const button = node("button", "folder-link", item.name);
    button.type = "button";
    button.title = item.path;
    button.addEventListener("click", () => navigate(item.path));
    wrapper.append(button);
  } else wrapper.append(node("span", "item-name", item.name));
  if (showPath) wrapper.append(node("span", "item-path", item.path));
  content.append(icon, wrapper);
  cell.append(content);
  row.append(cell);
}

function numericCell(row, value, optional = false) { row.append(node("td", "numeric" + (optional ? " optional-col" : ""), value)); }

function renderPagination(prefix, offset, total) {
  const control = $(prefix + "-pagination");
  control.hidden = total <= PAGE_SIZE;
  $(prefix + "-range").textContent = total ? `${formatCount(offset + 1)}–${formatCount(Math.min(offset + PAGE_SIZE, total))} of ${formatCount(total)}` : "";
  $(prefix + "-prev").disabled = offset === 0;
  $(prefix + "-next").disabled = offset + PAGE_SIZE >= total;
}

async function loadBrowse() {
  const request = ++state.browseRequest;
  const generation = state.resultGeneration;
  const params = new URLSearchParams({ path: state.folder, sort: state.sort, order: state.order, offset: String(state.browseOffset), limit: String(PAGE_SIZE) });
  try {
    const data = await api(`/api/browse?${params}`);
    if (request !== state.browseRequest || generation !== state.resultGeneration) return;
    renderBreadcrumbs(data);
    const body = $("browse-body");
    body.replaceChildren();
    data.items.forEach((item) => {
      const row = node("tr");
      addNameCell(row, item, item.kind === "folder");
      numericCell(row, formatSize(item.size));
      numericCell(row, item.kind === "folder" ? formatCount(item.files) : "—", true);
      numericCell(row, item.kind === "folder" ? formatCount(item.folders) : "—", true);
      numericCell(row, formatDate(item.modified), true);
      const action = node("td", "action-cell");
      if (item.kind === "folder" && item.path !== state.status.root) {
        const button = node("button", "trash-button", "Recycle Bin");
        button.type = "button";
        button.title = `Move ${item.path} to Recycle Bin`;
        button.setAttribute("aria-label", `Move ${item.path} to Recycle Bin`);
        button.disabled = state.trashing;
        button.addEventListener("click", () => trashFolder(item.path));
        action.append(button);
      }
      row.append(action);
      body.append(row);
    });
    $("browse-empty").hidden = data.total !== 0;
    renderPagination("browse", state.browseOffset, data.total);
  } catch (error) { if (request === state.browseRequest && generation === state.resultGeneration) showError(error); }
}

async function loadFiles() {
  const request = ++state.filesRequest;
  const generation = state.resultGeneration;
  const params = new URLSearchParams({ query: $("file-search").value.trim(), extension: $("extension-filter").value.trim(), offset: String(state.fileOffset), limit: String(PAGE_SIZE) });
  try {
    const data = await api(`/api/files?${params}`);
    if (request !== state.filesRequest || generation !== state.resultGeneration) return;
    const body = $("files-body");
    body.replaceChildren();
    data.items.forEach((item) => {
      const row = node("tr");
      addNameCell(row, item, false, true);
      numericCell(row, formatSize(item.size));
      numericCell(row, formatDate(item.modified), true);
      body.append(row);
    });
    $("files-empty").hidden = data.total !== 0;
    renderPagination("files", state.fileOffset, data.total);
  } catch (error) { if (request === state.filesRequest && generation === state.resultGeneration) showError(error); }
}

async function loadTypes() {
  const generation = state.resultGeneration;
  try {
    const data = await api("/api/types");
    if (generation !== state.resultGeneration) return;
    const list = $("types-list");
    list.replaceChildren();
    const max = Math.max(1, ...data.items.map((item) => Number(item.size)));
    data.items.forEach((item) => {
      const extension = item.extension || "[none]";
      const button = node("button", "type-row");
      button.type = "button";
      button.title = `Show ${extension} files`;
      button.append(node("span", "type-name", item.extension || "No extension"));
      const track = node("span", "type-track");
      const fill = node("span", "type-fill");
      fill.style.width = `${Math.max(2, Number(item.size) / max * 100)}%`;
      track.append(fill);
      button.append(track, node("span", "type-size", formatSize(item.size)), node("span", "type-count", `${formatCount(item.count)} ${Number(item.count) === 1 ? "file" : "files"}`));
      button.addEventListener("click", () => {
        $("file-search").value = "";
        $("extension-filter").value = extension;
        state.fileOffset = 0;
        selectTab("files", true);
      });
      list.append(button);
    });
    $("types-empty").hidden = data.items.length !== 0;
  } catch (error) { if (generation === state.resultGeneration) showError(error); }
}

async function loadErrors() {
  const generation = state.resultGeneration;
  try {
    const data = await api("/api/errors");
    if (generation !== state.resultGeneration) return;
    const body = $("errors-body");
    body.replaceChildren();
    data.items.forEach((item) => {
      const row = node("tr");
      row.append(node("td", "error-path", item.path), node("td", "error-reason", item.error));
      body.append(row);
    });
    $("errors-empty").hidden = data.total !== 0;
  } catch (error) { if (generation === state.resultGeneration) showError(error); }
}

function changeSort(sort) {
  state.order = state.sort === sort && state.order === "desc" ? "asc" : "desc";
  state.sort = sort;
  state.browseOffset = 0;
  for (const value of ["size", "name"]) {
    const button = $("sort-" + value);
    const active = value === sort;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    $("sort-" + value + "-arrow").textContent = active ? (state.order === "desc" ? "↓" : "↑") : "";
  }
  loadBrowse();
}

function initEvents() {
  $("scan-form").addEventListener("submit", startScan);
  $("browse-button").addEventListener("click", browseFolder);
  $("cancel-button").addEventListener("click", cancelScan);
  for (const button of document.querySelectorAll("[data-tab]")) {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll("[data-tab]")];
      const index = tabs.indexOf(button);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      selectTab(tabs[next].dataset.tab, true);
    });
  }
  $("sort-size").addEventListener("click", () => changeSort("size"));
  $("sort-name").addEventListener("click", () => changeSort("name"));
  $("view-donut").addEventListener("click", () => setChartView("donut"));
  $("view-bars").addEventListener("click", () => setChartView("bars"));
  $("browse-prev").addEventListener("click", () => { state.browseOffset = Math.max(0, state.browseOffset - PAGE_SIZE); loadBrowse(); });
  $("browse-next").addEventListener("click", () => { state.browseOffset += PAGE_SIZE; loadBrowse(); });
  $("files-prev").addEventListener("click", () => { state.fileOffset = Math.max(0, state.fileOffset - PAGE_SIZE); loadFiles(); });
  $("files-next").addEventListener("click", () => { state.fileOffset += PAGE_SIZE; loadFiles(); });
  for (const input of [$("file-search"), $("extension-filter")]) input.addEventListener("input", () => {
    window.clearTimeout(state.searchTimer);
    state.fileOffset = 0;
    state.searchTimer = window.setTimeout(loadFiles, 250);
  });
  $("clear-filters").addEventListener("click", () => {
    $("file-search").value = "";
    $("extension-filter").value = "";
    state.fileOffset = 0;
    loadFiles();
  });
}

async function init() {
  initEvents();
  try {
    const [config, status] = await Promise.all([api("/api/config"), api("/api/status")]);
    $("folder-path").value = status.root || config.default_path || "";
    setStatus(status);
  } catch (error) { showError(error); }
}

init();
