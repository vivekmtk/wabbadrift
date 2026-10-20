"use strict";
// Wabbadrift UI: renders the local server's JSON reports. Everything read from the instances is
// HTML-escaped; the page re-renders from state on every change.

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const qs = (o) => new URLSearchParams(o).toString();
const LIMIT = 300; // rows per table section before "Show all"
const TABS = ["mods", "plugins", "root", "configs", "leaks", "fomod", "updates", "history"];
const TEXT_FILE = /\.(ini|toml|json|ya?ml|txt|xml|cfg|log|md|psc|bat|csv)$/i;

const MOD_STATUS = {
  only_baseline: ["Only in baseline", "c-frost"],
  different_version: ["Different version", "c-maybe"],
  changed_files: ["Changed files", "c-drop"],
  only_custom: ["Only in custom", "c-gold"],
  identical: ["Identical", "c-keep"],
};
const FILE_STATUS = {
  removed: ["Only in baseline", "c-frost"],
  added: ["Only in custom", "c-gold"],
  changed: ["Changed", "c-drop"],
  same: ["Same", "c-keep"],
};
const PLUGIN_KIND = {
  only_baseline: ["Only in baseline", "c-frost"],
  only_custom: ["Only in custom", "c-gold"],
  order_moved: ["Moved", "c-maybe"],
  enabled_changed: ["Enabled/disabled differs", "c-drop"],
  group_changed: ["Group changed", "c-muted"],
};
const EXPECTED = {
  generator_output: "Generator output", game_version: "Game version",
  machine_specific: "Machine-specific", intentional_addition: "Intentional addition",
};
const RELATION = {
  safe_to_pull: ["Safe to apply", "c-frost"],
  conflict: ["Conflict", "c-drop"],
  already_matches: ["Already matches", "c-keep"],
  not_in_custom: ["Not in custom", "c-muted"],
};
const CHANGE = {
  added: "Added", removed: "Removed", updated: "Updated", changed: "Changed", moved: "Moved",
  enabled: "Enabled", disabled: "Disabled", separator: "Separator",
};
const LEAK_KIND = {
  baseline_instance: ["Baseline instance", "c-drop"],
  baseline_game_root: ["Baseline game root", "c-drop"],
  wabbajack: ["Wabbajack folder", "c-maybe"],
  missing: ["Missing path", "c-maybe"],
};
const LEAK_WHERE = {
  executable: "Executable", custom_overwrite: "Custom overwrite", profile_ini: "Profile INI",
  mo2_settings: "ModOrganizer.ini", tool_config: "Tool config", config: "Config file",
};
const CONFIDENCE = { exact: "c-keep", likely: "c-frost", ambiguous: "c-maybe" };

const S = {
  pairs: [], pair: "", tab: "mods", q: "", expected: false,
  scan: null, busy: "", error: "",
  modStatus: "", modFlags: new Set(), plugFilter: "", rootStatus: "", relFilter: "",
  fomods: null, fomodError: "", fomodOnlyDiff: false, fomodOpen: new Set(),
  snapshots: null, wabbajackFile: "", snapError: "", snapBusy: false,
  old: "", neu: "", drift: null, driftBusy: false, driftError: "", driftOpen: new Set(),
  cfgOpen: new Set(), cfgDiffs: {},
  showAll: new Set(), drawer: null, opener: null,
  selCopy: new Set(), selReplace: new Set(), // nothing selected by default
  runs: null, runsError: "", undoBusy: "", undoError: null,
};

const store = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage unavailable */ } },
};

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Wabbadrift": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) {
    const err = new Error((data && data.error) || `${r.status} ${r.statusText}`);
    err.files = (data && data.files) || [];
    throw err;
  }
  return data;
}

// ------------------------------------------------------------------ small helpers
const st = (map, key) => {
  const [label, cls] = map[key] || [key || "—", "c-muted"];
  return `<span class="st ${cls}">${esc(label)}</span>`;
};
const exp = (e) => (e ? `<span class="vtag exp" title="Expected drift">${esc(EXPECTED[e] || e)}</span>` : "");
const tag = (text, cls = "", title = "") => `<span class="vtag ${cls}"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`;
const matchQ = (...fields) => !S.q || fields.some((f) => f && String(f).toLowerCase().includes(S.q));
const countBy = (list, fn) => list.reduce((acc, x) => { const k = fn(x); acc[k] = (acc[k] || 0) + 1; return acc; }, {});
const when = (t) => new Date(t * 1000).toLocaleString();
const val = (v) => (v === null || v === undefined ? `<span class="muted">—</span>` : esc(typeof v === "object" ? JSON.stringify(v) : v));
function size(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB"]; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n < 10 ? 1 : 0)} ${u[i]}`;
}
function working(text, sub = "") {
  return `<div class="working"><span class="spinner"></span><div>${esc(text)}${sub ? `<div class="hint">${esc(sub)}</div>` : ""}</div></div>`;
}
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}

/** A table in sections ({title, rows}); each section shows LIMIT rows until "Show all". */
function tbl(key, head, sections, rowFn, cols) {
  let body = "";
  for (const sec of sections) {
    if (!sec.rows.length) continue;
    if (sec.title) body += `<tr class="subhead"><td colspan="${cols}">${esc(sec.title)} <span class="muted">${sec.rows.length}</span></td></tr>`;
    const k = `${key}/${sec.title || ""}`;
    const shown = S.showAll.has(k) ? sec.rows : sec.rows.slice(0, LIMIT);
    body += shown.map(rowFn).join("");
    if (shown.length < sec.rows.length) {
      body += `<tr class="more-tr"><td colspan="${cols}"><button class="btn sm" data-act="more" data-key="${esc(k)}">Show all ${sec.rows.length}</button></td></tr>`;
    }
  }
  if (!body) return `<div class="empty">Nothing to show${S.q ? " for this search" : ""}.</div>`;
  return `<div class="table-wrap"><table><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

/** Segmented bar + legend; clicking filters state[key] to that status. */
function statusBar(title, map, counts, key) {
  const active = S[key];
  const keys = Object.keys(map);
  const segs = keys.filter((k) => counts[k]).map((k) => `<button class="oseg ${map[k][1]} ${active === k ? "on" : ""}" style="flex:${counts[k]}" title="${esc(map[k][0])}: ${counts[k]}" aria-label="${esc(map[k][0])}" data-act="filter" data-key="${key}" data-val="${k}"></button>`).join("");
  const leg = keys.map((k) => `<button class="${map[k][1]} ${active === k ? "on" : ""}" data-act="filter" data-key="${key}" data-val="${k}"><span class="sw"></span>${esc(map[k][0])} <b>${counts[k] || 0}</b></button>`).join("");
  const clear = active ? `<button data-act="filter" data-key="${key}" data-val="">Clear filter</button>` : "";
  return `<div class="overlap"><h3>${esc(title)}</h3><div class="obar">${segs}</div><div class="olegend">${leg}${clear}</div></div>`;
}

function keyTable(key, keys, labels = ["Baseline", "Custom"]) {
  const list = keys.filter((k) => matchQ(k.section, k.key));
  const sections = [{ rows: list.filter((k) => !k.expected) }];
  if (S.expected) sections.push({ title: "Expected drift", rows: list.filter((k) => k.expected) });
  const hidden = S.expected ? 0 : list.filter((k) => k.expected).length;
  const head = `<tr><th>Section</th><th>Key</th><th>${labels[0]}</th><th>${labels[1]}</th><th>Status</th></tr>`;
  const row = (k) => `<tr><td class="muted">${esc(k.section)}</td><td class="tname">${esc(k.key)}</td><td class="tval">${val(k.baseline)}</td><td class="tval">${val(k.custom)}</td><td>${st(FILE_STATUS, k.status)} ${exp(k.expected)}</td></tr>`;
  return tbl(key, head, sections, row, 5) + (hidden ? `<p class="hint">${hidden} expected ${hidden === 1 ? "difference" : "differences"} hidden.</p>` : "");
}

// ------------------------------------------------------------------ boot and data
async function init() {
  wire();
  const t = location.hash.slice(1);
  if (TABS.includes(t)) S.tab = t;
  S.expected = store.get("wabbadrift-expected") === "1";
  await loadPairs(store.get("wabbadrift-pair"));
  if (!S.pairs.length && !S.error) openPairForm("", null);
}

/** The configured comparisons; selects `want` if it exists, else the first. */
async function loadPairs(want) {
  try { S.pairs = (await api("/api/pairs")).pairs; S.error = ""; } catch (e) { S.error = e.message; }
  S.pair = S.pairs.includes(want) ? want : S.pairs[0] || "";
  store.set("wabbadrift-pair", S.pair);
  $("#pairSelect").innerHTML = S.pairs.map((p) => `<option ${p === S.pair ? "selected" : ""}>${esc(p)}</option>`).join("");
  $("#pairSelect").disabled = !S.pairs.length;
  S.scan = null; S.snapshots = null; S.drift = null; S.fomods = null; S.fomodError = "";
  S.runs = null; S.selCopy.clear(); S.selReplace.clear();
  if (S.pair) loadScan(false); else render();
}

async function loadScan(rescan) {
  S.busy = rescan ? "Rescanning both instances" : "Reading both instances";
  S.error = "";
  render();
  try {
    S.scan = rescan ? await api("/api/rescan", { pair: S.pair }) : await api(`/api/scan?${qs({ pair: S.pair })}`);
    S.fomods = null; S.fomodError = ""; S.drift = null; S.cfgDiffs = {};
  } catch (e) { S.error = e.message; }
  S.busy = "";
  render();
  if (S.scan) loadForTab();
}

function loadForTab() {
  if (S.tab === "fomod" && !S.fomods && !S.fomodError) loadFomods();
  if (S.tab === "updates" && !S.snapshots) loadSnapshots();
  if (S.tab === "history" && !S.runs) loadRuns();
}

async function loadRuns() {
  try { S.runs = (await api(`/api/runs?${qs({ pair: S.pair })}`)).runs; S.runsError = ""; } catch (e) { S.runsError = e.message; }
  render();
}

async function loadFomods() {
  try { S.fomods = (await api(`/api/fomods?${qs({ pair: S.pair })}`)).mods; } catch (e) { S.fomodError = e.message; }
  render();
}

async function loadSnapshots() {
  try {
    const r = await api(`/api/snapshots?${qs({ pair: S.pair })}`);
    S.snapshots = r.snapshots; S.wabbajackFile = r.wabbajack_file; S.snapError = "";
    const ids = S.snapshots.map((s) => s.id);
    if (!ids.includes(S.neu)) S.neu = ids[ids.length - 1] || "";
    if (!ids.includes(S.old)) S.old = ids[Math.max(0, ids.length - 2)] || "";
  } catch (e) { S.snapError = e.message; }
  render();
}

async function takeSnapshot(path) {
  S.snapBusy = true; S.snapError = ""; render();
  try {
    const info = await api("/api/snapshot", { pair: S.pair, path: path || "" });
    toast(`Snapshot ${info.name} ${info.version} ready`);
    S.snapshots = null; S.neu = info.id;
    await loadSnapshots();
  } catch (e) { S.snapError = e.message; }
  S.snapBusy = false; render();
}

async function compareSnapshots() {
  S.driftBusy = true; S.driftError = ""; S.drift = null; render();
  try { S.drift = await api(`/api/drift?${qs({ pair: S.pair, old: S.old, new: S.neu })}`); } catch (e) { S.driftError = e.message; }
  S.driftBusy = false; render();
}

// ------------------------------------------------------------------ rendering
function render() {
  document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === S.tab)));
  renderBars();
  if (S.drawer && S.drawer.kind === "info") renderPairInfo();
  const app = $("#app");
  if (!S.pairs.length) {
    app.innerHTML = `<div class="layout"><div class="empty"><span class="rune">ᚹ</span>${S.error ? `<p class="error">${esc(S.error)}</p>` : `<p>Nothing to compare yet.</p>
      <p class="hint">A comparison is your MO2 instance, a clean install of the same Wabbajack list (the baseline), and the list's .wabbajack file.</p>
      <button class="btn primary" data-act="newpair">Set up a comparison</button>`}</div></div>`;
    return;
  }
  if (S.busy) {
    app.innerHTML = `<div class="loading"><div><span class="spinner"></span> ${esc(S.busy.toUpperCase())}<small>Comparing every mod folder, both game folders and both profiles. The first scan of a comparison reads every file and can take a minute; later scans reuse what it learned.</small></div></div>`;
    return;
  }
  if (!S.scan) {
    app.innerHTML = `<div class="layout"><div class="empty error">${esc(S.error || "No scan yet.")}</div></div>`;
    return;
  }
  const views = { mods: viewMods, plugins: viewPlugins, root: viewRoot, configs: viewConfigs, leaks: viewLeaks, fomod: viewFomod, updates: viewUpdates, history: viewHistory };
  app.innerHTML = `<div class="layout">${views[S.tab]()}</div>`;
}

function renderBars() {
  const eb = $("#expectedBtn");
  eb.classList.toggle("on", S.expected);
  eb.setAttribute("aria-pressed", String(S.expected));
  eb.textContent = `${S.expected ? "Showing" : "Show"} expected drift`;
  $("#infoBtn").disabled = !S.pair;
  $("#rescanBtn").disabled = !S.pair || !!S.busy;
}

function shortWhen(t) {
  const d = new Date(t * 1000), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === now.toDateString()) return `today ${time}`;
  if (d.toDateString() === yesterday.toDateString()) return `yesterday ${time}`;
  return `${d.toLocaleDateString()} ${time}`;
}

// --- mods
function side(s) {
  if (!s) return `<span class="muted">—</span>`;
  const state = s.in_modlist ? (s.enabled ? "enabled" : "disabled") : "not in modlist";
  const ver = s.version || s.file;
  return `${ver ? esc(ver) : `<span class="muted">no version</span>`}<span class="sub">${state}${s.separator ? ` · ${esc(s.separator)}` : ""}</span>`;
}
function fileCounts(f) {
  const parts = [];
  if (f.changed) parts.push(`<span class="ch" title="Changed">~${f.changed}</span>`);
  if (f.removed) parts.push(`<span class="r" title="Only in baseline">−${f.removed}</span>`);
  if (f.added) parts.push(`<span class="a" title="Only in custom">+${f.added}</span>`);
  return parts.length ? `<span class="counts">${parts.join(" ")}</span>` : `<span class="muted">—</span>`;
}
function modTags(m) {
  let out = "";
  if (m.version_note) out += tag("version", "", m.version_note);
  if (m.moved) out += tag("order moved", "c-maybe");
  if (m.separator_changed) out += tag("separator", "c-maybe");
  if (m.enabled_changed) out += m.custom.enabled ? tag("enabled in yours", "c-drop", "Disabled in the baseline") : tag("disabled in yours", "c-drop", "Enabled in the baseline");
  out += exp(m.expected);
  if (!m.expected && m.expected_files) out += tag(`${m.expected_files} expected files`, "exp");
  return out;
}
// copy: a baseline mod custom doesn't have; replace: a matched mod whose files or version differ.
// The UI calls them Add and Reset.
const ACTION_WORD = { copy: "Add", replace: "Reset" };
const ACTION_HINT = {
  copy: "Add: copy this mod from the baseline into your instance",
  replace: "Reset: make your copy of this mod identical to the baseline's",
};
const canCopy = (m) => m.status === "only_baseline" && m.baseline.has_folder && m.baseline.in_modlist;
const canReplace = (m) => !!(m.custom && m.baseline && m.custom.has_folder && m.baseline.has_folder && m.status !== "identical");
function pickBox(m) {
  const kind = canCopy(m) ? "copy" : canReplace(m) ? "replace" : "";
  if (!kind) return "";
  const set = kind === "copy" ? S.selCopy : S.selReplace;
  const label = ACTION_HINT[kind];
  return `<label title="${label}"><input type="checkbox" data-act="pick" data-kind="${kind}" data-name="${esc(m.name)}" ${set.has(m.name) ? "checked" : ""} aria-label="${ACTION_WORD[kind]} ${esc(m.name)}"><span class="kind">${ACTION_WORD[kind]}</span></label>`;
}
function bulkBar(visible) {
  const copyable = visible.filter(canCopy).length, replaceable = visible.filter(canReplace).length;
  const n = S.selCopy.size + S.selReplace.size;
  if (!copyable && !replaceable && !n) return "";
  return `<div class="bulkbar">
    <span class="bulk-count">${n ? `<b>${S.selCopy.size}</b> to add · <b>${S.selReplace.size}</b> to reset` : "Tick mods in the Change column"}</span>
    ${copyable ? `<button class="btn sm" data-act="pickall" data-kind="copy" title="${ACTION_HINT.copy}">Tick all ${copyable} missing mods</button>` : ""}
    ${replaceable ? `<button class="btn sm" data-act="pickall" data-kind="replace" title="${ACTION_HINT.replace}">Tick all ${replaceable} differing mods</button>` : ""}
    ${n ? `<button class="btn sm ghost" data-act="pickclear">Clear</button>` : ""}
    <span class="spacer"></span>
    <button class="btn sm primary" data-act="preview" ${n ? "" : "disabled"}>${n ? `Review ${n} ${n === 1 ? "change" : "changes"}` : "Review changes"}</button></div>`;
}

const MOD_FLAGS = { moved: "Order moved", separator_changed: "Separator changed", enabled_changed: "Enabled/disabled differs" };

function viewMods() {
  const flagsOk = (m) => [...S.modFlags].every((f) => m[f]);
  const base = S.scan.mods.filter((m) => matchQ(m.name, m.custom && m.custom.name) && flagsOk(m));
  const real = base.filter((m) => !m.expected);
  // a flag toggle shows identical mods too, so the list matches the toggle's count
  const pick = (list) => list.filter((m) => (S.modStatus ? m.status === S.modStatus : S.modFlags.size || m.status !== "identical"));
  const sections = [{ rows: pick(real) }];
  if (S.expected) sections.push({ title: "Expected drift", rows: pick(base.filter((m) => m.expected)) });
  const hiddenExp = S.expected ? 0 : pick(base.filter((m) => m.expected)).length;
  const flagCounts = Object.fromEntries(Object.keys(MOD_FLAGS).map((f) => [f, real.filter((m) => m[f]).length]));
  const visible = sections.flatMap((s) => s.rows);
  S.visibleMods = visible; // "select all" picks what the filters show
  const head = `<tr><th class="pickcell" title="Add a missing mod, or reset yours to the baseline's version">Change</th><th>Mod</th><th>Status</th><th>Baseline</th><th>Custom</th><th>Files</th><th>Notes</th></tr>`;
  const row = (m) => `<tr class="click" tabindex="0" data-act="mod" data-name="${esc(m.name)}">
    <td class="pickcell">${pickBox(m)}</td>
    <td><div class="tname">${esc(m.name)}</div>${m.custom && m.baseline && m.custom.name !== m.baseline.name ? `<span class="sub">custom: ${esc(m.custom.name)}</span>` : ""}</td>
    <td>${st(MOD_STATUS, m.status)}${m.match && m.match !== "name" ? `<span class="sub">matched by ${esc(m.match === "nexus" ? "Nexus file" : "Nexus mod")}</span>` : ""}</td>
    <td>${side(m.baseline)}</td><td>${side(m.custom)}</td><td>${fileCounts(m.files)}</td><td>${modTags(m)}</td></tr>`;
  return `
    <div class="toolbar"><h2>Mods</h2><span class="meta">${S.scan.mods.length} mods across both instances${S.modStatus || S.modFlags.size ? "" : " · identical mods hidden"}</span></div>
    ${statusBar("Mod status", MOD_STATUS, countBy(real, (m) => m.status), "modStatus")}
    <div class="toolbar"><div class="toggles">${Object.entries(MOD_FLAGS).map(([f, label]) => `<button class="toggle ${S.modFlags.has(f) ? "on" : ""}" data-act="modflag" data-val="${f}">${label} <span class="n">${flagCounts[f]}</span></button>`).join("")}</div>
    <span class="spacer"></span><span class="meta">Open a mod to hash and compare its files.</span></div>
    ${bulkBar(visible)}
    ${tbl("mods", head, sections, row, 7)}
    ${hiddenExp ? `<p class="hint">${hiddenExp} mods with only expected drift are hidden.</p>` : ""}`;
}

// --- plugins
function groupChanged(p) {
  if (p.status !== "both" || !p.baseline_group) return false;
  const b = p.baseline_group.toLowerCase();
  return [p.custom_group, p.loot_group].some((g) => g && g.toLowerCase() !== b);
}
// null: not in plugins.txt, so the game loads it on its own (base game masters, Creation Club)
const enabledWord = (v) => (v === null ? "always loaded" : v ? "enabled" : "disabled");
function enabledCell(p) {
  if (p.status === "only_baseline") return `<span class="muted">${enabledWord(p.baseline_enabled)}</span>`;
  if (!p.enabled_changed) return `<span class="muted">${enabledWord(p.custom_enabled)}</span>`;
  return tag(`${enabledWord(p.custom_enabled)} in yours`, "c-drop", `${enabledWord(p.baseline_enabled)} in the baseline`);
}
/** Group names as "baseline → yours" when they differ; yours may come from MO2 or the LOOT userlist. */
function groupCell(p) {
  const yours = [p.custom_group, p.loot_group && `${p.loot_group} (LOOT)`].filter(Boolean);
  if (!p.group_changed) return esc(p.baseline_group || yours.join(", ")) || "—";
  return `${esc(p.baseline_group)}<span class="arrow">→</span>${esc(yours.join(", "))}`;
}
function viewPlugins() {
  const P = S.scan.plugins;
  const all = P.plugins.map((p) => ({ ...p, enabled_changed: p.status === "both" && p.baseline_enabled !== p.custom_enabled, group_changed: groupChanged(p) }));
  const has = (p, k) => (k.startsWith("only_") ? p.status === k : p[k]);
  const kinds = Object.keys(PLUGIN_KIND);
  const differs = all.filter((p) => kinds.some((k) => has(p, k)) && matchQ(p.name));
  const real = differs.filter((p) => !p.expected);
  const counts = Object.fromEntries(kinds.map((k) => [k, real.filter((p) => has(p, k)).length]));
  const pick = (list) => list.filter((p) => !S.plugFilter || has(p, S.plugFilter))
    .sort((a, b) => (a.custom_index ?? a.baseline_index ?? 1e9) - (b.custom_index ?? b.baseline_index ?? 1e9));
  const sections = [{ rows: pick(real) }];
  if (S.expected) sections.push({ title: "Expected drift", rows: pick(differs.filter((p) => p.expected)) });
  // the plugin each position in loadorder.txt holds, to name what a moved plugin loads after
  const bAt = {}, cAt = {};
  for (const p of P.plugins) {
    if (p.baseline_index !== null) bAt[p.baseline_index] = p.name;
    if (p.custom_index !== null) cAt[p.custom_index] = p.name;
  }
  const after = (at, i) => (i === 0 ? `<span class="muted">(first)</span>` : esc(at[i - 1] || "—"));
  const head = `<tr><th class="num">#</th><th>Plugin</th><th>Status</th><th>Enabled</th><th>Loads after <span class="muted">(baseline → yours)</span></th><th>Group <span class="muted">(baseline → yours)</span></th></tr>`;
  const row = (p) => `<tr><td class="num">${p.custom_index ?? ""}</td><td><div class="tname">${esc(p.name)}</div>${exp(p.expected)}</td>
      <td>${p.status === "both" ? "" : st(PLUGIN_KIND, p.status)}</td>
      <td class="nowrap">${enabledCell(p)}</td>
      <td>${p.order_moved ? `${after(bAt, p.baseline_index)}<span class="arrow">→</span>${after(cAt, p.custom_index)}` : ""}</td>
      <td class="muted">${groupCell(p)}</td></tr>`;
  let extra = "";
  if (P.locked_order.length) {
    extra += `<div class="group-head">Locked load order <span class="n">${P.locked_order.length}</span></div>
      <div class="table-wrap"><table><thead><tr><th>Plugin</th><th>Baseline</th><th>Custom</th></tr></thead><tbody>${P.locked_order.map(([n, b, c]) => `<tr><td class="tname">${esc(n)}</td><td>${val(b)}</td><td>${val(c)}</td></tr>`).join("")}</tbody></table></div>`;
  }
  if (P.loot_groups.length) {
    extra += `<div class="group-head">LOOT groups (custom userlist, read-only) <span class="n">${P.loot_groups.length}</span></div>
      <div class="table-wrap"><table><thead><tr><th>Group</th><th>Loads after</th></tr></thead><tbody>${P.loot_groups.map((g) => `<tr><td class="tname">${esc(g.name)}</td><td class="muted">${esc((g.after || []).join(", ")) || "—"}</td></tr>`).join("")}</tbody></table></div>`;
  }
  return `<div class="toolbar"><h2>Plugins</h2><span class="meta">${P.plugins.length} plugins · ${differs.length} differ · read-only</span></div>
    ${statusBar("Plugin differences", PLUGIN_KIND, counts, "plugFilter")}
    ${tbl("plugins", head, sections, row, 6)}${extra}`;
}

// --- game root
// where the .wabbajack gets each game-root file (gameroot.py _source): label, explanation
const ROOT_SOURCE = {
  "vanilla game file": ["Your game install", "Taken from the game as Steam or GOG installed it, e.g. SkyrimSE.exe or the base BSAs. A difference here usually means a different game version."],
  "mod archive": ["Mod download", "Extracted from a downloaded mod archive, e.g. the SKSE loader, ENB's d3d11.dll or Engine Fixes DLLs."],
  inline: ["Bundled in the .wabbajack", "A file the list author packed into the .wabbajack itself, usually a tweaked config such as enblocal.ini or SKSE.ini."],
  "built BSA": ["BSA built at install", "A BSA archive Wabbajack builds from loose files while installing the list."],
  install: ["Not in the list", "The .wabbajack doesn't deliver this file. It came from the game install, a game update, a tool or you, and is compared with the baseline's game folder directly."],
};
function rootHelp() {
  const rows = Object.values(ROOT_SOURCE).map(([label, hint]) => `<tr><td class="tname nowrap">${esc(label)}</td><td class="muted">${esc(hint)}</td></tr>`).join("");
  return `<div class="panel"><h3>Where the list gets it</h3><p class="hint">How the Wabbajack list puts each file into the baseline's game folder, read from the .wabbajack.</p>
    <div class="table-wrap"><table><tbody>${rows}</tbody></table></div></div>`;
}
function viewRoot() {
  const files = S.scan.game_root.filter((f) => matchQ(f.path, f.custom_root_mod, f.baseline_root_mod));
  const real = files.filter((f) => !f.expected);
  const pick = (list) => list.filter((f) => !S.rootStatus || f.status === S.rootStatus);
  const sections = [{ rows: pick(real) }];
  if (S.expected) sections.push({ title: "Expected drift", rows: pick(files.filter((f) => f.expected)) });
  const hidden = S.expected ? 0 : files.length - real.length;
  const head = `<tr><th>Path</th><th>Status</th><th>Where the list gets it <button class="th-info ${S.rootHelp ? "on" : ""}" data-act="rootHelp" aria-expanded="${!!S.rootHelp}" title="What these values mean">i</button></th><th class="num">Baseline</th><th class="num">Custom</th><th>Root Builder</th></tr>`;
  const row = (f) => `<tr><td class="tpath">${esc(f.path)} ${exp(f.expected)}</td><td>${st(FILE_STATUS, f.status)}${f.reason ? `<span class="sub">by ${esc(f.reason)}</span>` : ""}</td>
    <td class="muted" title="${esc((ROOT_SOURCE[f.source] || [])[1] || "")}">${esc((ROOT_SOURCE[f.source] || [f.source])[0])}</td><td class="num">${size(f.baseline_size)}</td><td class="num">${size(f.custom_size)}</td>
    <td class="muted">${[f.baseline_root_mod && `baseline: ${esc(f.baseline_root_mod)}`, f.custom_root_mod && `custom: ${esc(f.custom_root_mod)}`].filter(Boolean).join("<br>") || "—"}</td></tr>`;
  return `<div class="toolbar"><h2>Game root</h2><span class="meta">${S.scan.game_root.length} files differ${S.scan.game_versions_differ ? " · game versions differ, so extender plugin DLLs count as expected" : ""}</span></div>
    ${statusBar("File status", FILE_STATUS, countBy(real, (f) => f.status), "rootStatus")}
    ${S.rootHelp ? rootHelp() : ""}
    ${tbl("root", head, sections, row, 6)}
    ${hidden ? `<p class="hint">${hidden} expected files hidden (generator outputs, game-version files).</p>` : ""}`;
}

// --- configs
function viewConfigs() {
  const C = S.scan.configs;
  let html = `<div class="toolbar"><h2>Configs</h2><span class="meta">Profile INIs, ModOrganizer.ini, executables and config files inside mods. Instance paths are normalised before comparing.</span></div>`;
  const panel = (title, n, body, hint = "") => `<div class="panel"><h3>${esc(title)} <span class="muted">${n}</span></h3>${hint ? `<p class="hint">${hint}</p>` : ""}${body}</div>`;

  const inis = C.profile_inis.map((f) => (f.status === "changed"
    ? `<div class="group-head">${esc(f.name)} <span class="n">${f.keys.length} keys</span></div>${keyTable(`ini/${f.name}`, f.keys)}`
    : `<p>${esc(f.name)} ${st(FILE_STATUS, f.status)}</p>`)).join("");
  html += panel("Profile INIs", C.profile_inis.length, inis || `<p class="hint">Identical.</p>`);
  html += panel("ModOrganizer.ini", C.mo2_settings.length, C.mo2_settings.length ? keyTable("mo2", C.mo2_settings) : `<p class="hint">Identical.</p>`);

  const exes = C.executables.filter((e) => matchQ(e.title));
  html += panel("Executables", C.executables.length, exes.length ? `<div class="table-wrap"><table><thead><tr><th>Title</th><th>Status</th><th>Differences (baseline → custom)</th></tr></thead><tbody>${exes.map((e) => `<tr><td class="tname">${esc(e.title)}</td><td>${st(FILE_STATUS, e.status)}</td><td>${Object.entries(e.field_diffs).map(([k, [b, c]]) => `<div><span class="muted">${esc(k)}:</span> <span class="tval">${val(b)}</span><span class="arrow">→</span><span class="tval">${val(c)}</span></div>`).join("") || "—"}</td></tr>`).join("")}</tbody></table></div>` : `<p class="hint">Identical.</p>`);
  html += panel("Custom overwrites", C.custom_overwrites.length, C.custom_overwrites.length ? keyTable("ow", C.custom_overwrites) : `<p class="hint">Identical.</p>`);

  const mc = C.mod_configs.filter((c) => matchQ(c.mod, c.path));
  const real = mc.filter((c) => !c.expected);
  const sections = [{ rows: real }];
  if (S.expected) sections.push({ title: "Expected drift", rows: mc.filter((c) => c.expected) });
  const row = (c) => {
    const k = `${c.mod}\n${c.path}`;
    const open = S.cfgOpen.has(k);
    let r = `<tr class="click" tabindex="0" data-act="cfg" data-key="${esc(k)}"><td class="tname">${esc(c.mod)}</td><td class="tpath">${esc(c.path)}</td><td>${st(FILE_STATUS, c.status)} ${exp(c.expected)}</td><td class="muted">${open ? "▾" : "▸"} diff</td></tr>`;
    if (open) r += `<tr><td colspan="4">${diffHtml(S.cfgDiffs[k])}</td></tr>`;
    return r;
  };
  html += panel("Config files inside mods", C.mod_configs.length,
    tbl("modcfg", `<tr><th>Mod</th><th>File</th><th>Status</th><th></th></tr>`, sections, row, 4),
    "Changed or one-sided INI/JSON/YAML/TOML files in matched mods. Click one for its key or line diff (baseline → custom).");
  return html;
}

function diffHtml(d) {
  if (!d || d.busy) return working("Reading both files…");
  if (d.error) return `<p class="error">${esc(d.error)}</p>`;
  if (d.keys) {
    return d.keys.length ? keyTable(`diff/${d.path}`, d.keys) : `<p class="hint">Same keys and values (only formatting or comments differ).</p>`;
  }
  if (d.lines) {
    if (!d.lines.length) return `<p class="hint">Same text.</p>`;
    const cls = (l) => (l.startsWith("@@") ? "hunk" : l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : "");
    return `<div class="log">${d.lines.map((l) => `<span class="${cls(l)}">${esc(l)}</span>`).join("\n")}</div>`;
  }
  return `<p class="hint">Not a text file, or too large to diff.</p>`;
}

async function loadDiff(target, key, mod, path) {
  target[key] = { busy: true };
  render(); renderDrawer();
  try { target[key] = await api(`/api/filediff?${qs({ pair: S.pair, name: mod, path })}`); } catch (e) { target[key] = { error: e.message }; }
  render(); renderDrawer();
}

// --- leaks
function viewLeaks() {
  const L = S.scan.leaks.filter((l) => matchQ(l.value, l.path, l.location, l.key, l.section, l.tool));
  const head = `<tr><th>Points to</th><th>Found in</th><th>Where</th><th>Value</th><th>Suggested custom path</th></tr>`;
  const row = (l) => `<tr><td>${st(LEAK_KIND, l.kind)}${l.note ? `<span class="sub">${esc(l.note)}</span>` : ""}</td>
    <td>${esc(LEAK_WHERE[l.category] || l.category)}${l.tool ? `<span class="sub">${esc(l.tool)}</span>` : ""}</td>
    <td><span class="tpath">${esc(l.location)}</span><span class="sub">${esc([l.section, l.key].filter(Boolean).join(" · "))}</span></td>
    <td class="tval">${esc(l.value)}</td>
    <td class="tval">${l.suggestion ? `${esc(l.suggestion)}${l.suggestion_exists ? "" : `<span class="sub">doesn't exist yet</span>`}` : `<span class="muted">—</span>`}</td></tr>`;
  return `<div class="toolbar"><h2>Path leaks</h2><span class="meta">${S.scan.leaks.length} values in the custom instance point into the baseline, a Wabbajack folder, or nowhere · flagged only, never changed</span></div>
    ${statusBar("Where they point", LEAK_KIND, countBy(S.scan.leaks, (l) => l.kind), "leakKind")}
    ${tbl("leaks", head, [{ rows: L.filter((l) => !S.leakKind || l.kind === S.leakKind) }], row, 5)}`;
}

// --- FOMOD
const mark = (v) => (v === true ? "✓" : v === false ? "✗" : "?");
function choicesHtml(inf, key) {
  const shown = inf.choices.filter((c) => c.selected !== false);
  const rest = inf.choices.filter((c) => c.selected === false);
  const one = (c) => `<div class="choice ${c.selected === true ? "on" : c.selected === false ? "off" : "unk"}">
    <span class="mark" aria-label="${c.selected === true ? "picked" : c.selected === false ? "not picked" : "undecided"}">${mark(c.selected)}</span>
    <span class="path">${esc(c.step)} <i>›</i> ${esc(c.group)} <i>›</i> <b>${esc(c.option)}</b></span>
    ${tag(c.confidence, CONFIDENCE[c.confidence] || "")}
    ${c.evidence.length ? `<ul class="evidence">${c.evidence.slice(0, 4).map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}</div>`;
  const [got, total] = inf.coverage;
  return `<p class="hint">Explains ${got} of ${total} installed files from the archive.</p>
    <div class="choices">${shown.map(one).join("") || `<p class="hint">No option is picked.</p>`}</div>
    ${rest.length ? `<details data-key="${esc(key)}"><summary class="muted">Options not picked (${rest.length})</summary><div class="choices">${rest.map(one).join("")}</div></details>` : ""}`;
}
function fomodBody(r) {
  let h = "";
  if (r.notes && r.notes.length) h += r.notes.map((n) => `<p class="notes">${esc(n)}</p>`).join("");
  if (!r.baseline) return h || `<p class="hint">No FOMOD installer.</p>`;
  if (r.differences.length) {
    h += `<div class="diffbox"><b>Custom copy answers ${r.differences.length} ${r.differences.length === 1 ? "option" : "options"} differently</b>${r.differences.map((d) => `<div>${esc(d.step)} › ${esc(d.group)} › <b>${esc(d.option)}</b>: baseline ${mark(d.baseline)}<span class="arrow">→</span>custom ${mark(d.custom)}${d.custom_evidence.length ? `<span class="sub">${esc(d.custom_evidence[0])}</span>` : ""}</div>`).join("")}</div>`;
  }
  h += `<div class="sec-title">Baseline choices <span class="muted">${esc(r.archive)}</span></div>${choicesHtml(r.baseline, `${r.mod}/b`)}`;
  if (r.custom) h += `<div class="sec-title">Custom copy's choices <span class="muted">${esc(r.custom_archive || r.archive)}</span></div>${choicesHtml(r.custom, `${r.mod}/c`)}`;
  return h;
}
function viewFomod() {
  const head = `<div class="toolbar"><h2>FOMOD choices</h2><span class="meta">Reference only: worked out from installed files, installers are never run.</span></div>`;
  if (S.fomodError) return `${head}<div class="empty error">${esc(S.fomodError)}</div>`;
  if (!S.fomods) return head + working("Reading FOMOD installers…", "The first run lists every installer archive with 7-Zip and can take a few minutes; later runs use the cache.");
  const withDiff = S.fomods.filter((r) => r.differences.length).length;
  const conf = countBy(S.fomods.flatMap((r) => r.baseline.choices), (c) => c.confidence);
  const list = S.fomods.filter((r) => matchQ(r.mod, r.module, r.archive) && (!S.fomodOnlyDiff || r.differences.length))
    .sort((a, b) => b.differences.length - a.differences.length || a.mod.localeCompare(b.mod));
  const card = (r) => {
    const c = countBy(r.baseline.choices.filter((x) => x.selected !== false), (x) => x.confidence);
    return `<details class="fm" data-fm="${esc(r.mod)}" ${S.fomodOpen.has(r.mod) ? "open" : ""}><summary><b>${esc(r.mod)}</b>
      <span class="muted">${esc(r.module)}</span>
      ${Object.entries(c).map(([k, n]) => tag(`${n} ${k}`, CONFIDENCE[k])).join("")}
      ${r.differences.length ? tag(`${r.differences.length} different in custom`, "c-drop") : ""}
      ${r.notes.length ? tag("note", "c-maybe", r.notes.join("; ")) : ""}</summary>
      <div class="fm-body">${S.fomodOpen.has(r.mod) ? fomodBody(r) : ""}</div></details>`;
  };
  return `${head}<div class="toolbar"><span class="meta">${S.fomods.length} baseline mods were installed with a FOMOD installer · ${withDiff} answered differently in custom · options: ${conf.exact || 0} exact, ${conf.likely || 0} likely, ${conf.ambiguous || 0} ambiguous</span><span class="spacer"></span>
    <button class="toggle ${S.fomodOnlyDiff ? "on" : ""}" data-act="fomodDiff">Only different in custom <span class="n">${withDiff}</span></button></div>
    ${list.map(card).join("") || `<div class="empty">Nothing to show.</div>`}`;
}

// --- updates
function snapLabel(s) { return `${s.name} ${s.version} · ${new Date(s.taken * 1000).toLocaleDateString()}`; }
function viewUpdates() {
  const head = `<div class="toolbar"><h2>Wabbajack updates</h2><span class="meta">Compare two versions of the list and see how each change meets your custom instance.</span></div>`;
  if (S.snapError && !S.snapshots) return `${head}<div class="empty error">${esc(S.snapError)}</div>`;
  if (!S.snapshots) return head + working("Reading snapshots…");
  const snaps = S.snapshots;
  const list = snaps.map((s) => `<div class="snap"><b>${esc(s.name)} ${esc(s.version)}</b><span class="muted">taken ${esc(when(s.taken))} · ${s.directives.toLocaleString()} directives · Wabbajack ${esc(s.wabbajack_version)}</span><span class="tpath muted">${esc(s.source)}</span></div>`).join("");
  const opts = (sel) => snaps.map((s) => `<option value="${esc(s.id)}" ${s.id === sel ? "selected" : ""}>${esc(snapLabel(s))}</option>`).join("");
  let html = `${head}<div class="panel"><h3>Baseline snapshots <span class="muted">${snaps.length}</span></h3>
    <p class="hint">A snapshot keeps what a comparison needs from one .wabbajack version (a few MB, in data/snapshots). The same content is only stored once. Take one each time the list updates.</p>
    <div class="snaps">${list || `<p class="hint">None yet.</p>`}</div>
    <div class="row"><button class="btn primary" data-act="snap" ${S.snapBusy ? "disabled" : ""}>${S.snapBusy ? `<span class="spinner"></span> Reading…` : "Snapshot the pair's .wabbajack"}</button><span class="tpath muted">${esc(S.wabbajackFile)}</span></div>
    <div class="row" style="margin-top:12px"><div class="field"><label for="snapPath">Or another .wabbajack file (e.g. a newer version)</label><input id="snapPath" placeholder="Full path to a .wabbajack file"></div>
    <button class="btn" data-act="snapPath" ${S.snapBusy ? "disabled" : ""} style="align-self:flex-end">Snapshot this file</button></div>
    ${S.snapError ? `<p class="error">${esc(S.snapError)}</p>` : ""}</div>`;
  if (snaps.length) {
    html += `<div class="panel"><h3>Compare</h3><div class="row">
      <label class="muted" for="oldSel">Old</label><select class="select" id="oldSel">${opts(S.old)}</select>
      <label class="muted" for="newSel">New</label><select class="select" id="newSel">${opts(S.neu)}</select>
      <button class="btn primary" data-act="compare" ${S.driftBusy ? "disabled" : ""}>${S.driftBusy ? `<span class="spinner"></span> Comparing…` : "Compare"}</button></div>
      ${snaps.length < 2 ? `<p class="hint" style="margin-top:12px">Only one version so far: comparing it with itself reports "identical". When the list updates, snapshot the new .wabbajack and compare.</p>` : ""}
      ${S.driftError ? `<p class="error">${esc(S.driftError)}</p>` : ""}</div>`;
  }
  if (S.drift) html += driftHtml(S.drift);
  return html;
}

function driftHtml(d) {
  if (d.identical) {
    return `<div class="panel"><div class="big-ok">✓ Identical</div><p class="hint">${esc(d.old)} and ${esc(d.new)} deliver the same mods, files, plugin order and INIs. Nothing changed.</p></div>`;
  }
  const all = [...d.mods, ...d.game_root, ...d.order, ...d.inis];
  const counts = countBy(all, (x) => x.relation);
  const keep = (x) => !S.relFilter || x.relation === S.relFilter;
  const rel = (x) => `${st(RELATION, x.relation)}${x.relation_detail ? `<span class="sub">${esc(x.relation_detail)}</span>` : ""}`;
  const arrow = (a, b) => `${val(a || null)}<span class="arrow">→</span>${val(b || null)}`;

  const modRow = (m) => {
    const open = S.driftOpen.has(m.name);
    let r = `<tr class="${m.files.length ? "click" : ""}" ${m.files.length ? `tabindex="0" data-act="driftmod" data-name="${esc(m.name)}"` : ""}>
      <td><div class="tname">${esc(m.name)}</div>${m.old_name && m.old_name !== m.name ? `<span class="sub">was ${esc(m.old_name)}</span>` : ""}${m.custom_name && m.custom_name !== m.name ? `<span class="sub">custom: ${esc(m.custom_name)}</span>` : ""}</td>
      <td>${esc(CHANGE[m.change] || m.change)}</td><td class="nowrap">${arrow(m.old_version, m.new_version)}</td>
      <td>${m.files.length ? `${m.files.length} ${open ? "▾" : "▸"}` : "—"}</td><td>${rel(m)}</td></tr>`;
    if (open) {
      r += `<tr><td colspan="5"><div class="table-wrap"><table><tbody>${m.files.map((f) => `<tr><td class="tpath">${esc(f.path)}</td><td>${esc(CHANGE[f.change] || f.change)}</td><td class="num">${size(f.old_size)}<span class="arrow">→</span>${size(f.new_size)}</td></tr>`).join("")}</tbody></table></div></td></tr>`;
    }
    return r;
  };
  const sec = (title, rows, head, row, cols) => (rows.length ? `<div class="group-head">${title} <span class="n">${rows.length}</span></div>${tbl(`drift/${title}`, head, [{ rows }], row, cols)}` : "");
  return `${statusBar(`${d.old} → ${d.new}: ${all.length} changes`, RELATION, counts, "relFilter")}
    ${sec("Mods", d.mods.filter((m) => keep(m) && matchQ(m.name, m.old_name, m.custom_name)), `<tr><th>Mod</th><th>Change</th><th>Version</th><th>Files</th><th>Your custom instance</th></tr>`, modRow, 5)}
    ${sec("Game root files", d.game_root.filter((f) => keep(f) && matchQ(f.path)), `<tr><th>Path</th><th>Change</th><th class="num">Size</th><th>Your custom instance</th></tr>`, (f) => `<tr><td class="tpath">${esc(f.path)}</td><td>${esc(CHANGE[f.change] || f.change)}</td><td class="num">${size(f.old_size)}<span class="arrow">→</span>${size(f.new_size)}</td><td>${rel(f)}</td></tr>`, 4)}
    ${sec("Plugin and mod order", d.order.filter((o) => keep(o) && matchQ(o.name)), `<tr><th>Name</th><th>Kind</th><th>Change</th><th>Your custom instance</th></tr>`, (o) => `<tr><td class="tname">${esc(o.name)}</td><td class="muted">${esc(o.kind)}</td><td>${esc(CHANGE[o.change] || o.change)}${o.old || o.new ? `<span class="sub">${esc(o.old)} → ${esc(o.new)}</span>` : ""}</td><td>${rel(o)}</td></tr>`, 4)}
    ${sec("Profile INIs", d.inis.filter((i) => keep(i) && matchQ(i.file, i.key, i.section)), `<tr><th>File</th><th>Key</th><th>Value</th><th>Your custom instance</th></tr>`, (i) => `<tr><td class="muted">${esc(i.file)}</td><td><span class="tname">${esc(i.key)}</span><span class="sub">${esc(i.section)}</span></td><td class="tval">${arrow(i.old, i.new)}</td><td>${rel(i)}</td></tr>`, 4)}`;
}

// ------------------------------------------------------------------ drawer (one mod)
async function openMod(name, opener) {
  S.drawer = { name, files: null, error: "", fomod: null, fomodBusy: false, fomodError: "", diffs: {}, open: new Set() };
  S.opener = opener;
  renderDrawer();
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  $(".drawer-close").focus();
  try {
    const r = await api(`/api/mod?${qs({ pair: S.pair, name })}`);
    if (S.drawer && S.drawer.name === name) S.drawer.files = r;
  } catch (e) { if (S.drawer) S.drawer.error = e.message; }
  renderDrawer();
}
function closeDrawer() {
  $("#drawer").classList.remove("open"); $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").classList.remove("open");
  S.drawer = null;
  if (S.opener && document.contains(S.opener)) S.opener.focus();
}
function renderDrawer() {
  const D = S.drawer;
  if (!D) return;
  if (D.kind === "preview") return renderPreview();
  if (D.kind === "pair") return; // the setup form renders once and updates in place
  if (D.kind === "info") return renderPairInfo();
  const m = S.scan.mods.find((x) => x.name === D.name);
  const c = m.custom, b = m.baseline;
  const st2 = (s) => (!s ? "—" : s.in_modlist ? `${s.enabled ? "enabled" : "disabled"} · priority ${s.priority}` : "not in modlist");
  const fact = (label, value) => `<div class="fact"><span>${label}</span><b>${value}</b></div>`;
  let h = `<button class="drawer-close" data-act="close" aria-label="Close">×</button><div class="drawer-body">
    <h2>${esc(m.name)}</h2><div class="by">${st(MOD_STATUS, m.status)} ${modTags(m)}</div>
    <div class="facts">
      ${fact("Baseline version", esc(b ? b.version || b.file || "—" : "—"))}${fact("Baseline state", st2(b))}
      ${fact("Custom", c ? esc(c.name) : "—")}${fact("Custom version", esc(c ? c.version || c.file || "—" : "—"))}${fact("Custom state", st2(c))}
      ${fact("Separator", `${esc(b && b.separator || "—")}${c && b && c.separator !== b.separator ? ` <span class="arrow">→</span> ${esc(c.separator || "—")}` : ""}`)}
      ${m.version_note ? fact("Version", esc(m.version_note)) : ""}
    </div>
    <div class="sec-title">Files</div>`;
  if (D.error) h += `<p class="error">${esc(D.error)}</p>`;
  else if (!D.files) h += working("Hashing files…", "Every file present on both sides is hashed (xxHash64, as Wabbajack does).");
  else {
    const F = D.files;
    const row = (f) => {
      const open = D.open.has(f.path);
      let r = `<tr><td class="tpath">${esc(f.path)} ${exp(f.expected)}</td><td>${st(FILE_STATUS, f.status)}</td><td class="num">${size(f.baseline_size)}<span class="arrow">→</span>${size(f.custom_size)}</td>
        <td>${f.text ? `<button class="btn sm ghost" data-act="dfile" data-path="${esc(f.path)}">${open ? "Hide" : "Diff"}</button>` : ""}</td></tr>`;
      if (open) r += `<tr><td colspan="4">${diffHtml(D.diffs[f.path])}</td></tr>`;
      return r;
    };
    h += `<p class="hint">${F.same} files identical · ${F.files.length} differ.</p>`;
    h += F.files.length ? `<div class="table-wrap"><table><thead><tr><th>Path</th><th>Status</th><th class="num">Baseline → custom</th><th></th></tr></thead><tbody>${F.files.map(row).join("")}</tbody></table></div>` : "";
  }
  if (b) {
    h += `<div class="sec-title">FOMOD choices ${!D.fomod && !D.fomodBusy ? `<button class="btn sm" data-act="dfomod">Read installer choices</button>` : ""}</div>`;
    if (D.fomodBusy) h += working("Reading the installer…");
    else if (D.fomodError) h += `<p class="error">${esc(D.fomodError)}</p>`;
    else if (D.fomod) h += fomodBody(D.fomod);
  }
  h += "</div>";
  const scroll = $(".drawer-body") ? $(".drawer-body").scrollTop : 0;
  $("#drawer").innerHTML = h;
  $(".drawer-body").scrollTop = scroll;
}

// ------------------------------------------------------------------ copy / replace / undo
function showDrawer(opener) {
  S.opener = opener;
  $("#drawer").classList.add("open"); $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").classList.add("open");
  const close = $(".drawer-close");
  if (close) close.focus();
}

async function openPreview(opener) {
  const sel = { copy: [...S.selCopy], replace: [...S.selReplace] };
  S.drawer = { kind: "preview", sel, plan: null, error: "", files: [], applying: false, showAll: new Set(), open: new Set(), diffs: {} };
  renderDrawer(); showDrawer(opener);
  try {
    const plan = await api("/api/plan", { pair: S.pair, ...sel });
    if (S.drawer && S.drawer.sel === sel) S.drawer.plan = plan;
  } catch (e) { if (S.drawer) S.drawer.error = e.message; }
  renderDrawer();
}

const KIND_WORD = { create: "new", overwrite: "overwritten", delete: "deleted" };
function renderPreview() {
  const D = S.drawer, P = D.plan;
  let h = `<button class="drawer-close" data-act="close" aria-label="Close">×</button><div class="drawer-body">
    <h2>Review changes to your instance</h2><div class="by">Add ${D.sel.copy.length} · reset ${D.sel.replace.length} · nothing is written until you apply</div>`;
  if (!P && !D.error) h += working("Working out the changes…", "Reset compares your mod folder with the baseline's by content, so large mods take a moment.");
  if (P) {
    if (P.mo2_running) h += `<div class="blocker"><b>Mod Organizer is running for your instance.</b> Close it, then review again.</div>`;
    for (const m of P.mods) {
      const counts = countBy(m.files, (f) => f.kind);
      h += `<div class="pv ${m.ok ? "" : "refused"}"><div class="pv-head">${tag(ACTION_WORD[m.action], m.action === "copy" ? "c-frost" : "c-maybe", ACTION_HINT[m.action])}<b>${esc(m.mod)}</b>${m.target && m.target !== m.mod ? `<span class="muted">→ ${esc(m.target)}</span>` : ""}
        ${m.ok ? "" : `<span class="st c-muted">skipped: ${esc(m.reason)}</span>`}</div>`;
      if (m.ok) {
        h += `<ul>${m.placement ? `<li>Modlist: ${esc(m.placement)}</li>` : ""}
          ${m.plugins.map((p) => `<li>Plugin <b>${esc(p.name)}</b>, enabled, ${esc(p.placement)}</li>`).join("")}
          ${m.notes.map((n) => `<li>${esc(n)}</li>`).join("")}
          <li>Files: ${["create", "overwrite", "delete"].filter((k) => counts[k]).map((k) => `${counts[k]} ${KIND_WORD[k]}`).join(", ") || "none"} · ${size(m.bytes)} to copy</li></ul>`;
        if (m.action === "replace") h += pvFiles(m);
      }
      h += `</div>`;
    }
    for (const d of P.profile_diffs) {
      h += `<div class="sec-title">${esc(d.file)}</div>${diffHtml({ lines: d.lines })}`;
    }
    const ok = P.mods.filter((m) => m.ok).length;
    h += `<div class="actions-foot"><p class="hint">Everything that changes is backed up first to <span class="tpath">${esc(P.backup_dir)}</span>. The run can be undone from History.</p>
      <button class="btn" data-act="close">Cancel</button>
      <button class="btn primary" data-act="apply" ${!ok || P.mo2_running || D.applying ? "disabled" : ""}>${D.applying ? `<span class="spinner"></span> Applying…` : `Apply ${ok} ${ok === 1 ? "change" : "changes"}`}</button></div>`;
  }
  if (D.error) {
    h += `<div class="blocker">${esc(D.error)}${D.files.length ? `<ul>${D.files.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}</div>`;
  }
  h += "</div>";
  const scroll = $(".drawer-body") ? $(".drawer-body").scrollTop : 0;
  $("#drawer").innerHTML = h;
  $(".drawer-body").scrollTop = scroll;
}

/** Replace: every file it will write or delete, with diffs for changed text files. */
function pvFiles(m) {
  const key = `pv/${m.mod}`;
  const files = firstRows(m.files, key);
  const inMod = (rel) => rel.slice(rel.indexOf("/") + 1);
  const rows = files.map((f) => {
    const k = `${m.mod}\n${f.rel}`;
    const canDiff = f.kind === "overwrite" && TEXT_FILE.test(f.rel);
    let r = `<tr><td class="tpath">${esc(inMod(f.rel))}</td><td>${esc(KIND_WORD[f.kind])}</td><td class="num">${f.kind === "delete" ? "" : size(f.size)}</td>
      <td>${canDiff ? `<button class="btn sm ghost" data-act="pvdiff" data-key="${esc(k)}" data-mod="${esc(m.mod)}" data-path="${esc(inMod(f.rel))}">${S.drawer.open.has(k) ? "Hide" : "Diff"}</button>` : ""}</td></tr>`;
    if (S.drawer.open.has(k)) r += `<tr><td colspan="4">${diffHtml(S.drawer.diffs[k])}</td></tr>`;
    return r;
  }).join("");
  const more = files.length < m.files.length ? `<tr class="more-tr"><td colspan="4"><button class="btn sm" data-act="pvmore" data-key="${esc(key)}">Show all ${m.files.length}</button></td></tr>` : "";
  return m.files.length ? `<div class="table-wrap"><table><thead><tr><th>File (baseline → custom)</th><th>Result</th><th class="num">Size</th><th></th></tr></thead><tbody>${rows}${more}</tbody></table></div>` : "";
}
function firstRows(list, key) { return S.drawer.showAll.has(key) ? list : list.slice(0, 200); }

async function applyPlan() {
  const D = S.drawer;
  D.applying = true; D.error = ""; D.files = []; renderDrawer();
  try {
    const run = await api("/api/apply", { pair: S.pair, ...D.sel, fingerprint: D.plan.fingerprint });
    S.selCopy.clear(); S.selReplace.clear(); S.runs = null;
    closeDrawer();
    toast(`Applied ${run.mods.length} ${run.mods.length === 1 ? "change" : "changes"} · undo from History`);
    loadScan(false);
  } catch (e) {
    D.applying = false; D.error = e.message; D.files = e.files || []; renderDrawer();
  }
}

async function undoRun(id) {
  if (!confirm("Undo these changes? Every file they wrote goes back to how it was before, from the backup.")) return;
  S.undoBusy = id; S.undoError = null; render();
  try {
    await api("/api/undo", { pair: S.pair, run: id });
    toast("Changes undone");
    S.runs = null; S.undoBusy = "";
    await loadRuns();
    S.scan = null; loadScan(false);
    return;
  } catch (e) { S.undoError = { run: id, message: e.message, files: e.files || [] }; }
  S.undoBusy = ""; render();
}

function viewHistory() {
  const head = `<div class="toolbar"><h2>History</h2><span class="meta">Every set of applied changes, newest first. Undo restores your instance exactly from the backup, unless a file the changes wrote has been edited since.</span></div>`;
  if (S.runsError) return `${head}<div class="empty error">${esc(S.runsError)}</div>`;
  if (!S.runs) return head + working("Reading the undo log…");
  if (!S.runs.length) return `${head}<div class="empty"><span class="rune">ᚹ</span>Nothing applied yet. Tick mods in the Change column on the Mods tab to add or reset them.</div>`;
  const status = { done: ["Applied", "c-keep"], undone: ["Undone", "c-muted"], failed: ["Failed: undo to restore", "c-drop"], applying: ["Interrupted: undo to restore", "c-drop"] };
  return head + S.runs.map((r) => {
    const err = S.undoError && S.undoError.run === r.id ? S.undoError : null;
    return `<div class="pv"><div class="pv-head">${st(status, r.status)}<b>${esc(when(r.created))}</b><span class="muted">${r.files} files · ${esc(r.id)}</span><span class="spacer" style="flex:1"></span>
      ${r.status !== "undone" ? `<button class="btn sm" data-act="undo" data-run="${esc(r.id)}" ${S.undoBusy ? "disabled" : ""}>${S.undoBusy === r.id ? `<span class="spinner"></span> Undoing…` : "Undo"}</button>` : `<span class="muted">undone ${esc(when(r.undone))}</span>`}</div>
      <ul>${r.mods.map((m) => `<li>${esc(ACTION_WORD[m.action] || m.action)} <b>${esc(m.mod)}</b>${m.target && m.target !== m.mod ? ` → ${esc(m.target)}` : ""}${m.placement ? ` <span class="muted">· ${esc(m.placement)}</span>` : ""}${m.plugins.length ? ` <span class="muted">· ${m.plugins.length} ${m.plugins.length === 1 ? "plugin" : "plugins"}</span>` : ""}</li>`).join("")}</ul>
      ${r.error ? `<p class="error">${esc(r.error)}</p>` : ""}
      ${err ? `<div class="blocker">${esc(err.message)}${err.files.length ? `<ul>${err.files.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}</div>` : ""}</div>`;
  }).join("");
}

// ------------------------------------------------------------------ comparison setup
const SIDE_TEXT = {
  custom: ["Your instance", "The MO2 instance you play and customise. Adding and resetting mods writes here, and only when you apply the changes."],
  baseline: ["Baseline instance", "A clean install of the same list by Wabbajack, used as the reference. Wabbadrift never writes to it."],
};

/** The selected comparison, read-only; Edit switches to the setup form. */
function openPairInfo(opener) {
  S.drawer = { kind: "info" };
  renderPairInfo();
  showDrawer(opener);
}

function renderPairInfo() {
  let h;
  try { h = pairInfoHtml(); } catch (e) {
    h = `<button class="drawer-close" data-act="close" aria-label="Close">×</button><div class="drawer-body"><h2>${esc(S.pair)}</h2>
      <div class="blocker">Couldn't show the details (${esc(e.message)}). If Wabbadrift was just updated, close its window and start wabbadrift.bat again.</div></div>`;
  }
  const scroll = $(".drawer-body") ? $(".drawer-body").scrollTop : 0;
  $("#drawer").innerHTML = h;
  $(".drawer-body").scrollTop = scroll;
}

function pairInfoHtml() {
  const s = S.scan;
  const kv = (...rows) => `<dl class="kv">${rows.filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  const path = (p) => (p ? esc(p) : `<span class="muted">—</span>`);
  let h = `<button class="drawer-close" data-act="close" aria-label="Close">×</button><div class="drawer-body">
    <h2>${esc(S.pair)}</h2><div class="by">Comparison details</div>`;
  if (!s) {
    h += working(S.busy ? `${S.busy}…` : S.error || "No scan yet.", "The details appear when the scan finishes.");
  } else {
    const listGame = s.list.game_version;
    const gameVersion = (v) => (!v ? `<span class="muted">unknown</span>`
      : esc(v) + (listGame && v !== listGame ? ` ${tag(`list expects ${listGame}`, "c-maybe", "This game version differs from the one the Wabbajack list was built for")}` : ""));
    const side = (key, x) => {
      const [title, hint] = SIDE_TEXT[key];
      return `<div class="sec-title">${title}</div><p class="hint">${hint}</p>
        ${kv(["Profile", esc(x.profile)], ["Game version", gameVersion(x.game_version)], ["MO2 folder", path(x.mo2)], ["Game folder", path(x.game_root)])}`;
    };
    h += kv(["Game", esc(s.game)], ["Last scan", `${esc(shortWhen(s.scanned))} · took ${s.seconds} s`]);
    h += side("baseline", s.baseline) + side("custom", s.custom);
    h += `<div class="sec-title">Wabbajack list</div><p class="hint">The .wabbajack file the baseline was installed from. Wabbadrift only reads it.</p>
      ${kv(["List", `${esc(s.list.name)} ${esc(s.list.version)}`], ["Built for game version", listGame ? esc(listGame) : `<span class="muted">not recorded</span>`], ["File", path(s.list.file)])}
      <div class="sec-title">Other folders</div>
      ${kv(["Downloads", path(s.paths.downloads_dir)], ["Backups of changes", path(s.paths.backup_dir)], s.paths.loot_userlist && ["LOOT userlist", path(s.paths.loot_userlist)])}`;
  }
  h += `<div class="actions-foot"><span class="spacer" style="flex:1"></span>
    <button class="btn" data-act="rescan" ${S.busy ? "disabled" : ""}>Rescan</button><button class="btn primary" data-act="editpair">Edit</button></div></div>`;
  return h;
}

function pathInput(id, label, value, placeholder, inspect = "") {
  return `<div class="field"><label for="${id}">${label}</label><input id="${id}" value="${esc(value || "")}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false"${inspect ? ` data-inspect="${inspect}"` : ""}><div class="field-info" id="${id}-info"></div></div>`;
}

async function openPairForm(name, opener) {
  S.drawer = { kind: "pair", edit: name };
  let f = { name: "", custom: {}, baseline: {} };
  if (name) {
    try { f = await api(`/api/pairconfig?${qs({ pair: name })}`); } catch (e) { toast(e.message); return; }
  }
  const side = (key) => {
    const [title, hint] = SIDE_TEXT[key], s = f[key];
    return `<div class="sec-title">${title}</div><p class="hint">${hint}</p>
      ${pathInput(`pf-${key}-dir`, "MO2 folder", s.mo2_dir, "The folder with ModOrganizer.exe and ModOrganizer.ini", key)}
      <div class="field"><label for="pf-${key}-profile">Profile</label><select class="select wide" id="pf-${key}-profile" disabled data-want="${esc(s.profile || "")}"><option>Enter the MO2 folder first</option></select></div>
      ${pathInput(`pf-${key}-root`, "Game folder (optional)", s.game_root, "Taken from ModOrganizer.ini")}`;
  };
  $("#drawer").innerHTML = `<button class="drawer-close" data-act="close" aria-label="Close">×</button><div class="drawer-body pair-form">
    <h2>${name ? "Edit comparison" : "New comparison"}</h2>
    <div class="by">Wabbadrift compares your MO2 instance with a clean install of the Wabbajack list it came from, file by file, and can add or reset mods in your instance from the baseline.</div>
    <div class="sec-title">Name</div>
    <div class="field"><label for="pf-name">Comparison name</label><input id="pf-name" value="${esc(f.name)}" placeholder="for example my-list-2.3" autocomplete="off" spellcheck="false" ${name ? "disabled" : ""}>
      <div class="field-info">${name ? "The name can't be changed: caches, snapshots and the undo history are kept under it." : "Letters, digits, - and _."}</div></div>
    ${side("baseline")}${side("custom")}
    <div class="sec-title">Wabbajack list</div><p class="hint">The .wabbajack file the baseline was installed from. Wabbadrift only reads it.</p>
    ${pathInput("pf-list", ".wabbajack file", f.wabbajack_file, "…\\downloaded_mod_lists\\<list>.wabbajack", "list")}
    <details class="pf-more" ${f.downloads_dir || f.loot_userlist || f.backup_dir ? "open" : ""}><summary>More settings (optional)</summary>
      ${pathInput("pf-downloads", "Downloads folder", f.downloads_dir, "Taken from your instance's ModOrganizer.ini")}
      ${pathInput("pf-loot", "LOOT userlist.yaml", f.loot_userlist, "Only for the read-only plugin group comparison")}
      ${pathInput("pf-backup", "Backup folder for applied changes", f.backup_dir, "data\\backups\\<name> in the Wabbadrift folder")}
    </details>
    <div id="pf-error"></div>
    <div class="actions-foot">${name ? `<button class="btn ghost danger" data-act="delpair">Remove comparison</button>` : ""}<span class="spacer" style="flex:1"></span>
      <button class="btn" data-act="close">Cancel</button><button class="btn primary" id="pf-save" data-act="savepair">${name ? "Save" : "Create and scan"}</button></div>
  </div>`;
  showDrawer(opener);
  if (!name) $("#pf-name").focus();
  document.querySelectorAll(".pair-form input[data-inspect]").forEach((i) => { if (i.value.trim()) inspectField(i); });
}

/** Checks an MO2 folder or .wabbajack as soon as it's entered; fills the profile list. */
async function inspectField(input) {
  const key = input.dataset.inspect, value = input.value.trim();
  const info = $(`#${input.id}-info`);
  const sel = key === "list" ? null : $(`#pf-${key}-profile`);
  if (sel && !sel.disabled) sel.dataset.want = sel.value;
  const resetProfiles = () => { if (sel) { sel.disabled = true; sel.innerHTML = "<option>Enter the MO2 folder first</option>"; } };
  if (!value) { info.textContent = ""; info.className = "field-info"; resetProfiles(); return; }
  info.className = "field-info"; info.textContent = "Checking…";
  try {
    const r = await api("/api/inspect", key === "list" ? { wabbajack_file: value } : { mo2_dir: value });
    if (!document.contains(input) || input.value.trim() !== value) return;
    info.className = "field-info ok";
    if (key === "list") { info.innerHTML = `✓ <b>${esc(r.name)}</b> ${esc(r.version)} · ${esc(r.game)}`; return; }
    info.innerHTML = `✓ ${esc(r.game || "MO2 instance")} · ${r.profiles.length} ${r.profiles.length === 1 ? "profile" : "profiles"}`;
    const want = sel.dataset.want;
    const pick = r.profiles.includes(want) ? want : r.profiles.includes(r.selected_profile) ? r.selected_profile : r.profiles[0];
    sel.innerHTML = r.profiles.map((p) => `<option value="${esc(p)}" ${p === pick ? "selected" : ""}>${esc(p)}${p === r.selected_profile ? " (active in MO2)" : ""}</option>`).join("");
    sel.disabled = false;
    $(`#pf-${key}-root`).placeholder = r.game_root ? `From ModOrganizer.ini: ${r.game_root}` : "Not set in ModOrganizer.ini: enter it";
    if (key === "custom") $("#pf-downloads").placeholder = `From ModOrganizer.ini: ${r.downloads_dir}`;
  } catch (e) {
    if (!document.contains(input) || input.value.trim() !== value) return;
    info.className = "field-info error"; info.textContent = e.message; resetProfiles();
  }
}

async function savePair() {
  const D = S.drawer, v = (id) => $(`#${id}`).value.trim();
  const side = (k) => {
    const sel = $(`#pf-${k}-profile`);
    return { mo2_dir: v(`pf-${k}-dir`), profile: sel.disabled ? "" : sel.value, game_root: v(`pf-${k}-root`) };
  };
  const pair = {
    name: D.edit || v("pf-name"), custom: side("custom"), baseline: side("baseline"),
    wabbajack_file: v("pf-list"), downloads_dir: v("pf-downloads"), loot_userlist: v("pf-loot"), backup_dir: v("pf-backup"),
  };
  const btn = $("#pf-save"), label = btn.textContent;
  btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Checking…`; $("#pf-error").innerHTML = "";
  try {
    const r = await api("/api/pairs/save", { create: !D.edit, pair });
    closeDrawer();
    toast(D.edit ? "Comparison saved" : "Comparison created");
    await loadPairs(r.name);
  } catch (e) {
    if (S.drawer !== D) return;
    $("#pf-error").innerHTML = `<div class="blocker">${esc(e.message)}</div>`;
    btn.disabled = false; btn.textContent = label;
  }
}

async function deletePair() {
  const name = S.drawer.edit;
  if (!confirm(`Remove the comparison "${name}"?\n\nOnly its setup is removed. Both instances and the .wabbajack are untouched, and its backups and undo history stay on disk.`)) return;
  try {
    await api("/api/pairs/delete", { pair: name });
    closeDrawer();
    toast(`Removed "${name}"`);
    await loadPairs("");
    if (!S.pairs.length) openPairForm("", null);
  } catch (e) { $("#pf-error").innerHTML = `<div class="blocker">${esc(e.message)}</div>`; }
}

// ------------------------------------------------------------------ events
function setTab(t) {
  S.tab = t;
  history.replaceState(null, "", `#${t}`);
  render();
  if (S.scan) loadForTab();
}

async function onClick(e) {
  const el = e.target.closest("[data-act],[data-tab]");
  if (!el) return;
  if (el.dataset.tab) return setTab(el.dataset.tab);
  if (el.dataset.act === "mod" && e.target.closest(".pickcell")) return; // the checkbox's label
  const D = S.drawer;
  switch (el.dataset.act) {
    case "expected":
      S.expected = !S.expected; store.set("wabbadrift-expected", S.expected ? "1" : "0"); render(); break;
    case "filter": S[el.dataset.key] = S[el.dataset.key] === el.dataset.val ? "" : el.dataset.val; render(); break;
    case "modflag": S.modFlags.has(el.dataset.val) ? S.modFlags.delete(el.dataset.val) : S.modFlags.add(el.dataset.val); render(); break;
    case "more": S.showAll.add(el.dataset.key); render(); break;
    case "mod": openMod(el.dataset.name, el); break;
    case "close": closeDrawer(); break;
    case "cfg": {
      const k = el.dataset.key;
      if (S.cfgOpen.has(k)) { S.cfgOpen.delete(k); render(); break; }
      S.cfgOpen.add(k);
      if (!S.cfgDiffs[k]) { const [mod, path] = k.split("\n"); loadDiff(S.cfgDiffs, k, mod, path); } else render();
      break;
    }
    case "dfile": {
      const p = el.dataset.path;
      if (D.open.has(p)) { D.open.delete(p); renderDrawer(); break; }
      D.open.add(p);
      if (!D.diffs[p]) loadDiff(D.diffs, p, D.name, p); else renderDrawer();
      break;
    }
    case "dfomod": {
      D.fomodBusy = true; renderDrawer();
      const name = D.name;
      try { const r = await api(`/api/fomod?${qs({ pair: S.pair, name })}`); if (S.drawer && S.drawer.name === name) D.fomod = r; } catch (err) { D.fomodError = err.message; }
      D.fomodBusy = false; renderDrawer();
      break;
    }
    case "fomodDiff": S.fomodOnlyDiff = !S.fomodOnlyDiff; render(); break;
    case "rootHelp": S.rootHelp = !S.rootHelp; render(); break;
    case "snap": takeSnapshot(""); break;
    case "snapPath": {
      const p = $("#snapPath").value.trim();
      if (p) takeSnapshot(p); else toast("Enter the path of a .wabbajack file");
      break;
    }
    case "compare": compareSnapshots(); break;
    case "driftmod": S.driftOpen.has(el.dataset.name) ? S.driftOpen.delete(el.dataset.name) : S.driftOpen.add(el.dataset.name); render(); break;
    case "pick": {
      const set = el.dataset.kind === "copy" ? S.selCopy : S.selReplace;
      el.checked ? set.add(el.dataset.name) : set.delete(el.dataset.name);
      render();
      break;
    }
    case "pickall": {
      const copy = el.dataset.kind === "copy";
      (S.visibleMods || []).filter(copy ? canCopy : canReplace).forEach((m) => (copy ? S.selCopy : S.selReplace).add(m.name));
      render();
      break;
    }
    case "pickclear": S.selCopy.clear(); S.selReplace.clear(); render(); break;
    case "preview": openPreview(el); break;
    case "pvmore": D.showAll.add(el.dataset.key); renderDrawer(); break;
    case "pvdiff": {
      const k = el.dataset.key;
      if (D.open.has(k)) { D.open.delete(k); renderDrawer(); break; }
      D.open.add(k);
      if (!D.diffs[k]) loadDiff(D.diffs, k, el.dataset.mod, el.dataset.path); else renderDrawer();
      break;
    }
    case "apply": applyPlan(); break;
    case "undo": undoRun(el.dataset.run); break;
    case "newpair": openPairForm("", el); break;
    case "rescan": if (S.pair && !S.busy) loadScan(true); break;
    case "editpair": openPairForm(S.pair, S.opener); break;
    case "savepair": savePair(); break;
    case "delpair": deletePair(); break;
    default: break;
  }
}

function toggleTheme() {
  const root = document.documentElement;
  const cur = root.dataset.theme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  root.dataset.theme = cur === "light" ? "dark" : "light";
  store.set("wabbadrift-theme", root.dataset.theme);
}

function wire() {
  document.addEventListener("click", onClick);
  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); $("#search").focus(); }
    if (e.key === "Escape" && S.drawer) closeDrawer();
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr[data-act]")) { e.preventDefault(); onClick(e); }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "oldSel") S.old = e.target.value;
    if (e.target.id === "newSel") S.neu = e.target.value;
    if (e.target.matches("input[data-inspect]")) inspectField(e.target);
  });
  // FOMOD cards render their body only while open
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (!d.matches || !d.matches("details[data-fm]")) return;
    const name = d.dataset.fm;
    if (d.open === S.fomodOpen.has(name)) return;
    d.open ? S.fomodOpen.add(name) : S.fomodOpen.delete(name);
    const r = S.fomods && S.fomods.find((x) => x.mod === name);
    if (d.open && r) d.querySelector(".fm-body").innerHTML = fomodBody(r);
  }, true);
  $("#search").addEventListener("input", (e) => { S.q = e.target.value.trim().toLowerCase(); render(); });
  $("#pairSelect").addEventListener("change", (e) => loadPairs(e.target.value));
  $("#newPairBtn").addEventListener("click", (e) => openPairForm("", e.currentTarget));
  $("#infoBtn").addEventListener("click", (e) => { if (S.pair) openPairInfo(e.currentTarget); });
  $("#rescanBtn").addEventListener("click", () => { if (S.pair && !S.busy) loadScan(true); });
  $("#themeBtn").addEventListener("click", toggleTheme);
  $("#scrim").addEventListener("click", closeDrawer);
  window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (TABS.includes(t) && t !== S.tab) setTab(t); });
  const bar = $("#topbar");
  new ResizeObserver(() => document.documentElement.style.setProperty("--topbar-h", `${bar.offsetHeight}px`)).observe(bar);
}

init();
