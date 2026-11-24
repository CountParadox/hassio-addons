/* Panalapse UI. All URLs are relative so it works behind Home Assistant ingress. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

let S = null;                 // last /api/state
let liveCam = localStorage.getItem("liveCam") || "";
let activeTab = "live";

// ------------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const init = { method: opts.method || (opts.body !== undefined ? "POST" : "GET"), headers: {} };
  if (opts.body !== undefined) { init.headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
  const r = await fetch(path, init);
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok || (data && data.ok === false)) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), bad ? 6000 : 3000);
}
function fmtTime(iso) {
  if (!iso) return "–";
  const d = new Date(iso), now = new Date();
  const same = d.toDateString() === now.toDateString();
  return same ? d.toLocaleTimeString() : d.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function fmtInterval(s) {
  if (s % 3600 === 0) return `${s / 3600} h`;
  if (s % 60 === 0) return `${s / 60} min`;
  return `${s} s`;
}
function fmtSize(b) { return b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB"; }
const camById = (id) => S?.cameras.find((c) => c.id === id);
const destById = (id) => S?.destinations.find((d) => d.id === id);
function formData(form) {
  const o = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") o[el.name] = el.checked;
    else if (el.type === "number") o[el.name] = el.value === "" ? "" : Number(el.value);
    else o[el.name] = el.value;
  }
  return o;
}
function fillForm(form, obj) {
  for (const el of form.elements) {
    if (!el.name || !(el.name in obj)) continue;
    if (el.type === "checkbox") el.checked = !!obj[el.name];
    else el.value = obj[el.name] ?? "";
  }
}
function options(sel, items, value) {
  sel.innerHTML = items.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  if (value !== undefined) sel.value = value;
}

// ------------------------------------------------------------------ tabs
function showTab(name) {
  activeTab = name;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  localStorage.setItem("tab", name);
  if (name === "live") schedulePreview(0);
  if (name === "images") imagesShown();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) { e.preventDefault(); showTab(g.dataset.goto); }
});

// ------------------------------------------------------------------ state
async function refresh() {
  try {
    S = await api("api/state");
  } catch (e) {
    return;
  }
  $("#pauseAll").checked = !!S.settings.paused;
  renderLiveHeader();
  renderJobs();
  renderSetup();
  renderVideos();
  renderImagesHeader();
  renderHa();
}
setInterval(refresh, 2000);
$("#pauseAll").addEventListener("change", async (e) => {
  await api("api/settings", { body: { paused: e.target.checked } });
  toast(e.target.checked ? "All scheduled captures paused" : "Scheduled captures resumed");
  refresh();
});

// ================================================================== LIVE
function renderLiveHeader() {
  const sel = $("#liveCam");
  const has = S.cameras.length > 0;
  $("#liveEmpty").hidden = has;
  $("#liveGrid").hidden = !has;
  if (!has) return;
  if (!camById(liveCam)) liveCam = S.cameras[0].id;
  const want = S.cameras.map((c) => c.id + c.name).join();
  if (sel.dataset.sig !== want) {
    options(sel, S.cameras.map((c) => [c.id, `${c.name} (${c.host})`]), liveCam);
    sel.dataset.sig = want;
    renderPresets(); renderLiveControls();
  }
  const c = camById(liveCam);
  const note = $("#ptzNote");
  const isUE4 = /^AW-UE4$/.test(c.info?.model || "");
  note.hidden = !isUE4;
  if (isUE4) note.textContent = "AW-UE4: pan and tilt work within the picture, so zoom in first. At full wide they don't move.";
  const st = $("#camStatus");
  const model = c.info?.model ? c.info.model + " · " : "";
  if (c.busy) { st.textContent = model + "capturing"; st.className = "pill warn"; }
  else if (c.online) { st.textContent = model + "online"; st.className = "pill ok"; }
  else { st.textContent = model + (c.last_error ? "offline" : "–"); st.className = "pill bad"; st.title = c.last_error || ""; }
  const sig = JSON.stringify(c.presets) + JSON.stringify(c.info?.controls ? Object.keys(c.info.controls) : []);
  if ($("#presetGrid").dataset.sig !== sig) { renderPresets(); renderLiveControls(); }
}
$("#liveCam").addEventListener("change", (e) => {
  liveCam = e.target.value; localStorage.setItem("liveCam", liveCam);
  renderPresets(); renderLiveControls(); schedulePreview(0);
});

// ---- preview loop
let previewTimer = null, previewBusy = false, lastFrameAt = 0;
function schedulePreview(delay) {
  clearTimeout(previewTimer);
  const rate = Number($("#refreshRate").value);
  if (delay === undefined) delay = rate;
  if (activeTab !== "live" || document.hidden || (!rate && delay !== 0)) return;
  previewTimer = setTimeout(loadPreview, delay);
}
function loadPreview() {
  if (!liveCam || previewBusy) return;
  previewBusy = true;
  const img = $("#preview");
  const t0 = performance.now();
  const next = new Image();
  next.onload = () => {
    img.src = next.src;
    previewBusy = false;
    lastFrameAt = Date.now();
    $("#previewMsg").hidden = true;
    $("#previewInfo").textContent = `${next.naturalWidth}×${next.naturalHeight} · fetched in ${Math.round(performance.now() - t0)} ms · ${new Date().toLocaleTimeString()}`;
    schedulePreview();
  };
  next.onerror = async () => {
    let why = "";
    try {   // the image request only says "failed"; ask again for the reason
      const r = await fetch(next.src);
      if (!r.ok) { const d = await r.json().catch(() => null); why = d?.error || `HTTP ${r.status}`; }
    } catch (e) { why = e.message; }
    previewBusy = false;
    const m = $("#previewMsg"); m.hidden = false;
    m.textContent = "Couldn't get a picture from the camera" + (why ? `: ${why}` : "") + " – retrying…";
    schedulePreview(Math.max(3000, Number($("#refreshRate").value)));
  };
  next.src = `api/cameras/${liveCam}/snapshot?t=${Date.now()}`;
}
$("#refreshRate").addEventListener("change", () => schedulePreview(0));
document.addEventListener("visibilitychange", () => schedulePreview(0));

// ---- PTZ
const speed = () => Number($("#speed").value);
$("#speed").addEventListener("input", (e) => ($("#speedVal").textContent = e.target.value));
let holdTimer = null, holding = null;
async function ptz(body) {
  try { await api(`api/cameras/${liveCam}/ptz`, { body }); }
  catch (e) { toast(e.message, true); }
}
function startHold(btn, body) {
  stopHold();
  holding = btn; btn.classList.add("held");
  ptz({ ...body, speed: speed() });
  // keep the server-side watchdog alive while the button is held
  holdTimer = setInterval(() => ptz({ action: "refresh" }), 400);
}
function stopHold() {
  if (!holding) return;
  holding.classList.remove("held"); holding = null;
  clearInterval(holdTimer);
  ptz({ action: "stop" });
}
function bindHold(btn, bodyFn) {
  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    try { btn.setPointerCapture(e.pointerId); } catch { /* not a live pointer */ }
    startHold(btn, bodyFn());
  });
  ["pointerup", "pointercancel", "lostpointercapture"].forEach((ev) => btn.addEventListener(ev, stopHold));
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
}
$$("#dpad button[data-pan]").forEach((b) => bindHold(b, () => ({ action: "move", pan: +b.dataset.pan, tilt: +b.dataset.tilt })));
$("#dpad [data-stop]").addEventListener("click", () => ptz({ action: "stop" }));
$$("[data-zoom]").forEach((b) => bindHold(b, () => ({ action: "zoom", dir: +b.dataset.zoom })));
$$("[data-focus]").forEach((b) => bindHold(b, () => ({ action: "focus", dir: +b.dataset.focus })));
window.addEventListener("blur", stopHold);

// keyboard control on the preview
const wrap = $(".preview-wrap"); wrap.tabIndex = 0;
const keyMap = { ArrowLeft: { action: "move", pan: -1, tilt: 0 }, ArrowRight: { action: "move", pan: 1, tilt: 0 },
  ArrowUp: { action: "move", pan: 0, tilt: 1 }, ArrowDown: { action: "move", pan: 0, tilt: -1 },
  "+": { action: "zoom", dir: 1 }, "=": { action: "zoom", dir: 1 }, "-": { action: "zoom", dir: -1 } };
let keyHeld = null;
wrap.addEventListener("keydown", (e) => {
  const m = keyMap[e.key]; if (!m) return;
  e.preventDefault();
  if (keyHeld === e.key) return;
  keyHeld = e.key; startHold(wrap, m);
});
wrap.addEventListener("keyup", (e) => { if (e.key === keyHeld) { keyHeld = null; stopHold(); } });
wrap.addEventListener("blur", () => { keyHeld = null; stopHold(); });

const ONCE_DONE = { af: "Focusing once", awb: "White balance set", abb: "Black balance set" };
$$("[data-once]").forEach((b) => b.addEventListener("click", async () => {
  b.disabled = true;
  try {
    await api(`api/cameras/${liveCam}/ptz`, { body: { action: "onetouch", kind: b.dataset.once } });
    toast(ONCE_DONE[b.dataset.once]);
  } catch (e) { toast(e.message, true); }
  finally { b.disabled = false; }
}));

// ---- presets
function renderPresets() {
  const c = camById(liveCam); if (!c) return;
  const grid = $("#presetGrid");
  grid.dataset.sig = JSON.stringify(c.presets) + JSON.stringify(c.info?.controls ? Object.keys(c.info.controls) : []);
  const nums = Object.keys(c.presets || {}).map(Number).sort((a, b) => a - b);
  if (!nums.length) { grid.innerHTML = `<div class="small muted" style="grid-column:1/-1">No presets saved from here yet. Move the camera, pick a slot and save. Presets already stored in the camera can be recalled by slot number.</div>`; }
  else grid.innerHTML = nums.map((n) => {
    const p = c.presets[n];
    const img = p.thumb ? `<img src="api/cameras/${c.id}/preset/${n}/thumb?v=${encodeURIComponent(p.name)}${Date.now() / 60000 | 0}" alt="">` : `<div class="noimg">${n}</div>`;
    return `<div class="preset" data-n="${n}" title="Click to recall">${img}
      <div class="meta"><span>${esc(p.name)}</span><span class="muted">#${n}</span></div>
      <button class="menu small-btn" data-menu="${n}" title="Options">⋯</button></div>`;
  }).join("");
  const free = [...Array(100).keys()].map((i) => i + 1).find((i) => !nums.includes(i)) || 1;
  if (!$("#presetSlot").dataset.touched) $("#presetSlot").value = free;
}
$("#presetSlot").addEventListener("input", (e) => (e.target.dataset.touched = "1"));
$("#presetGrid").addEventListener("click", async (e) => {
  const menu = e.target.closest("[data-menu]");
  const card = e.target.closest(".preset");
  if (!card) return;
  const n = Number(card.dataset.n);
  if (menu) {
    e.stopPropagation();
    const c = camById(liveCam);
    const choice = prompt(`Preset #${n} “${c.presets[n].name}”\nType a new name to rename it,\n“overwrite” to store the current position here,\nor “forget” to remove it from this list.`, c.presets[n].name);
    if (choice === null) return;
    try {
      if (choice.trim().toLowerCase() === "overwrite") await api(`api/cameras/${liveCam}/preset`, { body: { action: "save", n } });
      else if (choice.trim().toLowerCase() === "forget") await api(`api/cameras/${liveCam}/preset`, { body: { action: "forget", n } });
      else await api(`api/cameras/${liveCam}/preset`, { body: { action: "rename", n, name: choice } });
      refresh();
    } catch (err) { toast(err.message, true); }
    return;
  }
  try {
    await api(`api/cameras/${liveCam}/preset`, { body: { action: "recall", n } });
    $$(".preset").forEach((p) => p.classList.toggle("active", p === card));
    toast(`Going to preset #${n}`);
  } catch (err) { toast(err.message, true); }
});
$("#presetSave").addEventListener("submit", async (e) => {
  e.preventDefault();
  const n = Number($("#presetSlot").value);
  const c = camById(liveCam);
  if (c.presets?.[n] && !confirm(`Slot ${n} already holds “${c.presets[n].name}”. Overwrite it with the current position?`)) return;
  try {
    await api(`api/cameras/${liveCam}/preset`, { body: { action: "save", n, name: $("#presetName").value || `Preset ${n}` } });
    toast(`Saved preset #${n}`);
    $("#presetName").value = ""; delete $("#presetSlot").dataset.touched;
    refresh();
  } catch (err) { toast(err.message, true); }
});
$("#presetRecallNum").addEventListener("click", async () => {
  const n = Number($("#presetSlot").value);
  try { await api(`api/cameras/${liveCam}/preset`, { body: { action: "recall", n } }); toast(`Going to preset #${n}`); }
  catch (err) { toast(err.message, true); }
});

// ---- image controls (shared by the Live tab and the job editor)
// Controls this camera lets us change, with the min/max it actually accepts.
function supportedControls(cam) {
  const sup = cam?.info?.controls || {};
  const ro = cam?.info?.readonly || [];
  const ranges = cam?.info?.ranges || {};
  const accepted = cam?.info?.accepted || {};
  return S.controls.filter((c) => c.key in sup && !ro.includes(c.key)).map((c) => {
    if (ranges[c.key]) return { ...c, min: ranges[c.key][0], max: ranges[c.key][1] };
    if (accepted[c.key]) return { ...c, options: c.options.filter(([o]) => accepted[c.key].includes(o)) };
    return c;
  });
}
const ctlByKey = (cam, key) => supportedControls(cam).find((c) => c.key === key) || S.controls.find((c) => c.key === key);
// Range values are shown as an offset from the centre (e.g. −4…+4) or from the minimum.
const shownValue = (c, n) => (c.center != null ? (n - c.center > 0 ? "+" : "") + (n - c.center) : String(n - c.min));
function controlInputs(container, cam, values, { blankLabel = "(don't change)", showCurrent = null } = {}) {
  const ctrls = supportedControls(cam);
  const ro = (cam?.info?.readonly || []).map((k) => S.controls.find((c) => c.key === k)?.label).filter(Boolean);
  const roNote = ro.length ? `<div class="small muted" style="grid-column:1/-1">Not adjustable over IP on this camera: ${esc(ro.join(", "))}.</div>` : "";
  if (!ctrls.length) {
    container.innerHTML = `<div class="small muted" style="grid-column:1/-1">${cam?.info?.model ? "This camera didn't report any adjustable image settings. Use extra/raw commands instead." : "Press “Detect” on the camera under Cameras &amp; storage to find which settings it supports."}</div>` + roNote;
    return;
  }
  container.dataset.cam = cam.id;
  container.innerHTML = roNote + ctrls.map((c) => {
    const v = values?.[c.key] ?? "";
    const cur = showCurrent && showCurrent[c.key] != null ? `<span class="cur">now: ${esc(labelFor(c, showCurrent[c.key]))}</span>` : "";
    if (c.kind === "select") {
      const opts = [["", blankLabel], ...c.options];
      if (v && !opts.some(([o]) => o === v)) opts.push([v, `value ${v}`]);
      return `<label>${esc(c.label)} ${cur}<select data-ctl="${c.key}">${opts.map(([o, l]) => `<option value="${esc(o)}" ${o === v ? "selected" : ""}>${esc(l)}</option>`).join("")}</select></label>`;
    }
    const num = v === "" ? "" : Math.min(c.max, Math.max(c.min, parseInt(v, 16)));
    const start = num === "" ? (c.center ?? Math.round((c.min + c.max) / 2)) : num;
    return `<label>${esc(c.label)} ${cur}<span class="row gap"><input type="checkbox" data-ctl-on="${c.key}" ${v !== "" ? "checked" : ""} title="Set this value">
      <input type="range" data-ctl="${c.key}" min="${c.min}" max="${c.max}" value="${start}" ${v === "" ? "disabled" : ""}>
      <output>${num === "" ? "–" : shownValue(c, num)}</output></span></label>`;
  }).join("");
  const ctl = (k) => ctrls.find((x) => x.key === k);
  $$("input[type=range]", container).forEach((r) => r.addEventListener("input", () => {
    r.nextElementSibling.textContent = shownValue(ctl(r.dataset.ctl), Number(r.value));
  }));
  $$("[data-ctl-on]", container).forEach((cb) => cb.addEventListener("change", () => {
    const r = $(`input[type=range][data-ctl="${cb.dataset.ctlOn}"]`, container);
    r.disabled = !cb.checked;
    r.nextElementSibling.textContent = cb.checked ? shownValue(ctl(r.dataset.ctl), Number(r.value)) : "–";
  }));
}
function labelFor(c, v) {
  if (c.kind === "select") return (c.options.find(([o]) => o === v) || [v, v])[1];
  const n = parseInt(v, 16);
  return isNaN(n) ? v : shownValue(c, n);
}
function readControlInputs(container) {
  const out = {};
  $$("[data-ctl]", container).forEach((el) => {
    const c = S.controls.find((x) => x.key === el.dataset.ctl);
    if (el.tagName === "SELECT") { if (el.value !== "") out[c.key] = el.value; }
    else if (!el.disabled) out[c.key] = Number(el.value).toString(16).toUpperCase().padStart(c.hexwidth, "0");
  });
  return out;
}
let liveCurrent = null;
function renderLiveControls() { controlInputs($("#controlsForm"), camById(liveCam), {}, { showCurrent: liveCurrent }); }
$("#readControls").addEventListener("click", async () => {
  $("#controlsMsg").textContent = "Reading…";
  try {
    const r = await api(`api/cameras/${liveCam}/controls`);
    liveCurrent = r.values;
    controlInputs($("#controlsForm"), camById(liveCam), r.values, { showCurrent: r.values });
    $("#controlsMsg").textContent = `Camera is ${r.power}.`;
  } catch (e) { $("#controlsMsg").textContent = e.message; }
});
$("#applyControls").addEventListener("click", async () => {
  const vals = readControlInputs($("#controlsForm"));
  if (!Object.keys(vals).length) { $("#controlsMsg").textContent = "Nothing selected to change."; return; }
  $("#controlsMsg").textContent = "Sending…";
  try {
    const r = await api(`api/cameras/${liveCam}/controls`, { body: vals });
    const errs = Object.entries(r.results).filter(([, v]) => String(v).startsWith("error"));
    $("#controlsMsg").innerHTML = errs.length ? `<span class="bad-text">${errs.map(([k, v]) => esc(v)).join("<br>")}</span>` : `<span class="ok-text">Applied.</span>`;
  } catch (e) { $("#controlsMsg").textContent = e.message; }
});
$("#rawForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const cmd = $("#rawCmd").value.trim(); if (!cmd) return;
  try { const r = await api(`api/cameras/${liveCam}/command`, { body: { cmd } }); $("#rawOut").textContent = `${cmd} → ${r.reply}\n` + $("#rawOut").textContent; }
  catch (err) { $("#rawOut").textContent = `${cmd} → ${err.message}\n` + $("#rawOut").textContent; }
});

// ================================================================== JOBS
function renderJobs() {
  const list = $("#jobList");
  if (!S.jobs.length) { list.innerHTML = `<div class="empty">No timelapses yet. Press “New timelapse”.</div>`; return; }
  if (list.querySelector("button:disabled")) return;   // a "Capture now" is in flight
  const sig = JSON.stringify([S.jobs, S.settings.paused, S.cameras.map((c) => c.name), S.destinations.map((d) => d.name)]);
  if (list.dataset.sig === sig) return;   // avoid reloading thumbnails every poll
  list.dataset.sig = sig;
  list.innerHTML = S.jobs.map((j) => {
    const r = j.runtime, cam = camById(j.camera_id), dest = destById(j.destination_id);
    const days = j.days.length === 7 ? "every day" : j.days.map((d) => DAYS[d]).join(" ");
    const hours = j.start_time === j.end_time ? "all day" : `${j.start_time}–${j.end_time}`;
    const status = !j.enabled ? `<span class="pill">disabled</span>` : r.running ? `<span class="pill warn">capturing…</span>`
      : S.settings.paused ? `<span class="pill warn">paused</span>` : r.next_due ? `<span class="pill ok">scheduled</span>` : `<span class="pill">finished / no times</span>`;
    const thumb = r.has_image ? `<img class="thumb" src="api/jobs/${j.id}/last.jpg?t=${encodeURIComponent(r.last_capture || "")}" alt="">` : `<div class="thumb"></div>`;
    return `<div class="card job">${thumb}<div>
      <div class="row between"><span class="title">${esc(j.name)}</span>${status}</div>
      <dl>
        <dt>Camera</dt><dd>${esc(cam ? cam.name : "– missing –")}${j.preset ? ` · preset #${j.preset}` : ""}</dd>
        <dt>Every</dt><dd>${fmtInterval(j.interval_seconds)}, ${hours}, ${days}</dd>
        <dt>Next</dt><dd>${j.enabled ? fmtTime(r.next_due) : "–"}</dd>
        <dt>Last</dt><dd>${fmtTime(r.last_capture)} · ${r.count} frames</dd>
        <dt>Saving to</dt><dd>${esc(dest ? dest.name : "– missing –")}</dd>
      </dl>
      ${r.last_error ? `<div class="err">⚠ ${esc(r.last_error)} (${fmtTime(r.last_error_at)})</div>` : ""}
      ${r.last_warning ? `<div class="warn small">Camera refused some settings: ${esc(r.last_warning)}</div>` : ""}
      </div>
      <div class="actions">
        <button class="secondary small-btn" data-job-edit="${j.id}">Edit</button>
        <button class="secondary small-btn" data-job-toggle="${j.id}">${j.enabled ? "Disable" : "Enable"}</button>
        <button class="secondary small-btn" data-job-now="${j.id}">Capture now</button>
        <button class="secondary small-btn" data-job-images="${j.id}">Images</button>
        <button class="secondary small-btn" data-job-video="${j.id}">Make video</button>
      </div></div>`;
  }).join("");
}
$("#jobList").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  const id = b.dataset.jobEdit || b.dataset.jobToggle || b.dataset.jobNow || b.dataset.jobVideo || b.dataset.jobImages;
  const job = S.jobs.find((j) => j.id === id);
  if (b.dataset.jobEdit) openJob(job);
  if (b.dataset.jobToggle) { await api("api/jobs", { body: { id, enabled: !job.enabled } }); refresh(); }
  if (b.dataset.jobNow) {
    b.disabled = true; b.textContent = "Capturing…";
    try { const r = await api(`api/jobs/${id}/capture`, { body: {} }); toast(`Saved ${r.path}`); }
    catch (err) { toast(err.message, true); }
    refresh();
  }
  if (b.dataset.jobImages) { img.job = id; $("#imgJob").value = id; img.loaded = ""; showTab("images"); }
  if (b.dataset.jobVideo) { showTab("videos"); $("#renderJob").value = id; $("#renderJob").dispatchEvent(new Event("change")); }
});

const jobForm = $("#jobForm");
$("#daysBox").innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" data-day="${i}"> ${d}</label>`).join("")
  + `<button type="button" class="secondary small-btn" id="daysAll">All</button><button type="button" class="secondary small-btn" id="daysWk">Mon–Fri</button>`;
$("#daysAll").addEventListener("click", () => { $$("[data-day]").forEach((c) => (c.checked = true)); updateJobPreview(); });
$("#daysWk").addEventListener("click", () => { $$("[data-day]").forEach((c) => (c.checked = c.dataset.day < 5)); updateJobPreview(); });

function fillJobCameraBits(job) {
  const cam = camById(jobForm.camera_id.value);
  const presets = Object.entries(cam?.presets || {}).sort((a, b) => a[0] - b[0]).map(([n, p]) => [n, `#${n} ${p.name}`]);
  const cur = String(job?.preset ?? jobForm.preset.value ?? 0);
  const opts = [["0", "Don't move"], ...presets];
  if (cur !== "0" && !presets.some(([n]) => n === cur)) opts.push([cur, `#${cur}`]);
  opts.push(["other", "Other slot number…"]);
  options(jobForm.preset, opts, cur);
  const max = cam?.info?.max_resolution;
  const res = [["0", `Largest${max ? ` (${max})` : ""}`], ...[3840, 1920, 1280, 640].filter((r) => !max || r <= max).map((r) => [String(r), String(r)])];
  options(jobForm.resolution, res, String(job?.resolution ?? jobForm.resolution.value ?? 0));
  controlInputs($("#jobExposure"), cam, job ? job.exposure : readControlInputs($("#jobExposure")));
  syncExposureVisibility();
}
function syncExposureVisibility() {
  const manual = jobForm.exposure_mode.value === "manual";
  $("#jobExposure").hidden = !manual;
  $("#copyFromCam").hidden = !manual;
}
function openJob(job) {
  const fresh = !job;
  job = job || { name: "New timelapse", enabled: true, camera_id: liveCam || S.cameras[0]?.id || "", destination_id: S.destinations[0]?.id || "",
    interval_seconds: 60, days: [0, 1, 2, 3, 4, 5, 6], start_time: "07:00", end_time: "19:00", start_date: "", end_date: "",
    preset: 0, settle_seconds: 5, exposure_mode: "leave", exposure: {}, custom_commands: "", power_on: true, reapply: true, resolution: 0,
    folder_template: "{job}/{date}", filename_template: "{job}_{datetime}.jpg" };
  $("#jobDialogTitle").textContent = fresh ? "New timelapse" : `Edit “${job.name}”`;
  options(jobForm.camera_id, S.cameras.map((c) => [c.id, c.name]), job.camera_id);
  options(jobForm.destination_id, S.destinations.map((d) => [d.id, d.name]), job.destination_id);
  if (!S.destinations.length) jobForm.destination_id.innerHTML = `<option value="">– add storage first –</option>`;
  fillForm(jobForm, job);
  const iv = job.interval_seconds;
  const unit = iv % 3600 === 0 ? 3600 : iv % 60 === 0 ? 60 : 1;
  jobForm.interval_unit.value = unit; jobForm.interval_value.value = iv / unit;
  $$("[data-day]").forEach((c) => (c.checked = job.days.includes(Number(c.dataset.day))));
  fillJobCameraBits(job);
  $("#deleteJob").hidden = fresh;
  $("#jobFormMsg").textContent = ""; $("#testShot").hidden = true;
  updateJobPreview();
  $("#jobDialog").showModal();
}
function jobFromForm() {
  const f = formData(jobForm);
  const job = {
    id: f.id || undefined, name: f.name.trim() || "Timelapse", enabled: f.enabled, camera_id: f.camera_id, destination_id: f.destination_id,
    interval_seconds: Math.max(1, Math.round(Number(f.interval_value) * Number(f.interval_unit))),
    days: $$("[data-day]").filter((c) => c.checked).map((c) => Number(c.dataset.day)),
    start_time: f.start_time || "00:00", end_time: f.end_time || "00:00", start_date: f.start_date, end_date: f.end_date,
    preset: Number(f.preset) || 0, settle_seconds: Number(f.settle_seconds) || 0, exposure_mode: f.exposure_mode,
    exposure: readControlInputs($("#jobExposure")), custom_commands: f.custom_commands, power_on: f.power_on, reapply: f.reapply,
    resolution: Number(f.resolution) || 0, folder_template: f.folder_template, filename_template: f.filename_template,
  };
  return job;
}
let previewSchedT = null;
function updateJobPreview() {
  const job = jobFromForm();
  const cam = camById(job.camera_id) || { name: "camera" };
  const now = new Date();
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  const vals = { job: job.name, camera: cam.name, date: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
    time: `${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`, year: now.getFullYear(), month: pad(now.getMonth() + 1),
    day: pad(now.getDate()), hour: pad(now.getHours()) };
  vals.datetime = `${vals.year}${vals.month}${vals.day}_${vals.time}`;
  const seq = (S.jobs.find((j) => j.id === job.id)?.runtime.count || 0) + 1;
  const sub = (t) => t.replace(/\{([a-z]+)(?::0?(\d+)d)?\}/g, (m, k, w) => k === "seq" ? String(seq).padStart(Number(w || 0), "0") : (k in vals ? vals[k] : m));
  const dest = destById(job.destination_id);
  const folder = sub(job.folder_template).replace(/^\/+|\/+$/g, "");
  let name = sub(job.filename_template); if (!/\.jpe?g$/i.test(name)) name += ".jpg";
  const base = dest ? dest.path.replace(/[\\/]+$/, "") : "(storage)";
  const sep = dest && dest.type === "smb" ? "\\" : "/";
  $("#pathExample").textContent = [base, ...folder.split("/").filter(Boolean), name].join(sep);
  clearTimeout(previewSchedT);
  previewSchedT = setTimeout(async () => {
    try {
      const r = await api("api/jobs/preview-schedule", { body: job });
      $("#schedPreview").innerHTML = r.times.length ? "Next shots: " + r.times.map(fmtTime).join(", ") : `<span class="bad-text">No capture times match these settings.</span>`;
    } catch { /* ignore */ }
  }, 250);
}
jobForm.addEventListener("input", updateJobPreview);
jobForm.camera_id.addEventListener("change", () => fillJobCameraBits(null));
jobForm.exposure_mode.addEventListener("change", syncExposureVisibility);
jobForm.preset.addEventListener("change", () => {
  if (jobForm.preset.value !== "other") return;
  const n = Number(prompt("Preset slot number (1–100):", "1"));
  const cur = jobFromForm();
  fillJobCameraBits({ ...cur, preset: n >= 1 && n <= 100 ? n : 0 });
});
$("#copyFromCam").addEventListener("click", async () => {
  try {
    const r = await api(`api/cameras/${jobForm.camera_id.value}/controls`);
    const vals = Object.fromEntries(Object.entries(r.values).filter(([, v]) => v != null));
    controlInputs($("#jobExposure"), camById(jobForm.camera_id.value), vals);
    toast("Copied the camera's current settings");
  } catch (e) { toast(e.message, true); }
});
$("#newJob").addEventListener("click", () => {
  if (!S.cameras.length) { toast("Add a camera first", true); showTab("setup"); return; }
  openJob(null);
});
$("#cancelJob").addEventListener("click", () => $("#jobDialog").close());
jobForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const job = jobFromForm();
  if (!job.destination_id) { $("#jobFormMsg").textContent = "Pick where to save images (add storage first)."; return; }
  try { await api("api/jobs", { body: job }); $("#jobDialog").close(); toast("Saved"); refresh(); }
  catch (err) { $("#jobFormMsg").textContent = err.message; }
});
$("#deleteJob").addEventListener("click", async () => {
  if (!confirm("Delete this timelapse? Images already saved are kept.")) return;
  await api(`api/jobs/${jobForm.id.value}`, { method: "DELETE" });
  $("#jobDialog").close(); refresh();
});
$("#testJob").addEventListener("click", async () => {
  // Save first (so the server uses these settings), then take a shot without writing it to storage.
  const job = jobFromForm();
  $("#jobFormMsg").textContent = "Saving and taking a test shot…";
  try {
    const r = await api("api/jobs", { body: job });
    jobForm.id.value = r.id; $("#deleteJob").hidden = false;
    await api(`api/jobs/${r.id}/capture`, { body: { save: false } });
    const img = $("#testShot"); img.src = `api/jobs/${r.id}/last.jpg?t=${Date.now()}`; img.hidden = false;
    $("#jobFormMsg").textContent = "Test shot (not saved to storage):";
  } catch (err) { $("#jobFormMsg").textContent = err.message; }
});

// ================================================================== VIDEOS
const renderForm = $("#renderForm");
function renderVideos() {
  const jsel = $("#renderJob");
  const sig = S.jobs.map((j) => j.id + j.name).join();
  if (jsel.dataset.sig !== sig) {
    const v = jsel.value;
    options(jsel, [["", "– pick a folder by hand –"], ...S.jobs.map((j) => [j.id, j.name])], v);
    jsel.dataset.sig = sig;
  }
  const dsel = renderForm.destination_id;
  const dsig = S.destinations.map((d) => d.id + d.name).join();
  if (dsel.dataset.sig !== dsig) { const v = dsel.value; options(dsel, S.destinations.map((d) => [d.id, d.name]), v); dsel.dataset.sig = dsig; }

  $("#renderList").innerHTML = S.renders.map((r) => {
    const pct = r.total ? Math.round((100 * r.done) / r.total) : 0;
    const active = ["queued", "listing", "encoding", "uploading"].includes(r.status);
    return `<div class="card render"><div><b>${esc(r.name || "timelapse")}</b> · ${esc(r.status)} ${r.total ? `· ${r.done}/${r.total}` : ""}
      <div class="small ${r.status === "error" ? "bad-text" : "muted"}">${esc(r.message)}${r.uploaded_to ? `<br>Saved to ${esc(r.uploaded_to)}` : ""}</div>
      ${active ? `<progress max="100" value="${pct}"></progress>` : ""}</div>
      <div>${active ? `<button class="secondary small-btn" data-cancel="${r.id}">Cancel</button>` : ""}</div></div>`;
  }).join("");
  const vsig = S.videos.map((v) => v.name).join();
  const vl = $("#videoList");
  if (vl.dataset.sig !== vsig) {
    vl.dataset.sig = vsig;
    vl.innerHTML = S.videos.length ? S.videos.map((v) => `<div class="card">
      <video src="api/videos/${encodeURIComponent(v.name)}" controls preload="metadata"></video>
      <div class="row between"><div class="small">${esc(v.name)}<br><span class="muted">${fmtSize(v.size)} · ${fmtTime(v.modified)}</span></div>
      <div class="row gap"><a href="api/videos/${encodeURIComponent(v.name)}?download=1"><button class="secondary small-btn" type="button">Download</button></a>
      <button class="danger small-btn" data-delvid="${esc(v.name)}">Delete</button></div></div></div>`).join("")
      : `<div class="small muted">Videos you make appear here (kept inside the add-on, and also copied to your storage if ticked).</div>`;
  }
}
$("#renderJob").addEventListener("change", (e) => {
  const j = S.jobs.find((x) => x.id === e.target.value);
  if (!j) return;
  renderForm.destination_id.value = j.destination_id;
  // the part of the folder template before the first per-day placeholder
  const base = j.folder_template.split("/").filter((p) => !/\{(date|year|month|day|hour|time|datetime|seq)/.test(p)).join("/");
  renderForm.folder.value = base.replace(/\{job\}/g, j.name).replace(/\{camera\}/g, camById(j.camera_id)?.name || "");
  renderForm.name.value = j.name;
});
renderForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await api("api/renders", { body: formData(renderForm) }); $("#renderMsg").textContent = "Started – progress below."; refresh(); }
  catch (err) { $("#renderMsg").textContent = err.message; }
});
$("#renderList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-cancel]"); if (!b) return;
  await api(`api/renders/${b.dataset.cancel}/cancel`, { body: {} }); refresh();
});
$("#videoList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-delvid]"); if (!b) return;
  if (!confirm(`Delete ${b.dataset.delvid} from the add-on? (Any copy on your storage is kept.)`)) return;
  await api(`api/videos/${encodeURIComponent(b.dataset.delvid)}`, { method: "DELETE" }); refresh();
});

// ================================================================== IMAGES
const IMG_PAGE = 120;
const img = { job: localStorage.getItem("imgJob") || "", day: "", days: [], all: [], page: 0, step: 1,
  folder: "", loaded: "", lastCapture: null };
const imgUrl = (p, size) => `api/jobs/${img.job}/image?p=${encodeURIComponent(p)}&size=${size}`;
const shownImages = () => img.all.filter((_, i) => i % img.step === 0);
const todayStr = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
function fmtDay(s) {
  const d = new Date(s + "T12:00:00");
  return s === todayStr() ? "Today" : d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}
function imgMessage(msg) {
  $("#imgEmpty").innerHTML = msg; $("#imgEmpty").hidden = !msg;
  if (msg) { $("#imgGrid").innerHTML = ""; $("#imgPager").hidden = true; }
}
function renderImagesHeader() {
  const sel = $("#imgJob");
  const sig = S.jobs.map((j) => j.id + j.name).join();
  if (sel.dataset.sig !== sig) {
    if (!S.jobs.find((j) => j.id === img.job)) img.job = S.jobs[0]?.id || "";
    options(sel, S.jobs.map((j) => [j.id, j.name]), img.job);
    sel.dataset.sig = sig;
  }
  // new frames for the day being looked at: fetch the list again
  const job = S.jobs.find((j) => j.id === img.job);
  const last = job?.runtime.last_capture || null;
  if (activeTab === "images" && img.loaded === img.job && last !== img.lastCapture) {
    img.lastCapture = last;
    if (img.day === todayStr() || !img.days.length) loadDays(false, true);
  }
}
function imagesShown() {
  if (img.loaded !== img.job) loadDays();
}
async function loadDays(refresh = false, quiet = false) {
  if (!img.job) { imgMessage("No timelapses yet."); return; }
  const forJob = img.job;
  img.loaded = img.job;
  img.lastCapture = S.jobs.find((j) => j.id === img.job)?.runtime.last_capture || null;
  if (!quiet) { $("#imgInfo").textContent = "Reading the folder…"; imgMessage(""); }
  try {
    const r = await api(`api/jobs/${forJob}/days${refresh ? "?refresh=1" : ""}`);
    if (forJob !== img.job) return;
    img.days = r.days; img.folder = r.folder || "";
    const keep = r.days.some((d) => d.day === img.day) ? img.day : r.days[0]?.day || "";
    options($("#imgDay"), r.days.map((d) => [d.day, `${fmtDay(d.day)} · ${d.count} (${d.first}–${d.last})`]), keep);
    if (!r.days.length) {
      $("#imgInfo").textContent = "";
      imgMessage(`No images yet in <code>${esc(img.folder)}</code>.`);
      return;
    }
    const changedDay = img.day !== keep;
    img.day = keep;
    await loadImages(quiet && !changedDay);
  } catch (e) {
    $("#imgInfo").textContent = "";
    imgMessage(`Couldn't read the images: ${esc(e.message)}`);
  }
}
async function loadImages(keepPage = false) {
  const forKey = img.job + img.day;
  const r = await api(`api/jobs/${img.job}/images?day=${encodeURIComponent(img.day)}`);
  if (forKey !== img.job + img.day) return;
  const wasAtEnd = img.page >= Math.ceil(shownImages().length / IMG_PAGE) - 1;
  img.all = r.images;
  const pages = Math.max(1, Math.ceil(shownImages().length / IMG_PAGE));
  // start at the newest page for today, at the start for earlier days
  if (!keepPage) img.page = img.day === todayStr() ? pages - 1 : 0;
  else if (wasAtEnd) img.page = pages - 1;
  img.page = Math.min(img.page, pages - 1);
  renderGrid();
}
function renderGrid() {
  const list = shownImages();
  imgMessage(list.length ? "" : "No images for this day.");
  const pages = Math.max(1, Math.ceil(list.length / IMG_PAGE));
  const start = img.page * IMG_PAGE;
  const sig = img.job + img.day + img.step + img.page + list.length;
  if ($("#imgGrid").dataset.sig !== sig) {
    $("#imgGrid").dataset.sig = sig;
    $("#imgGrid").innerHTML = list.slice(start, start + IMG_PAGE).map((it, k) =>
      `<figure data-i="${start + k}"><img loading="lazy" src="${imgUrl(it.p, "thumb")}" alt=""><figcaption>${esc(it.t)}</figcaption></figure>`).join("");
  }
  $("#imgPager").hidden = pages <= 1;
  $("#imgPage").textContent = `Page ${img.page + 1} of ${pages}`;
  $("#imgPrev").disabled = img.page === 0;
  $("#imgNext").disabled = img.page >= pages - 1;
  $("#imgInfo").innerHTML = `${img.all.length} images${img.step > 1 ? `, showing ${list.length}` : ""} · <code>${esc(img.folder)}</code>`;
}
$("#imgJob").addEventListener("change", (e) => { img.job = e.target.value; localStorage.setItem("imgJob", img.job); img.day = ""; loadDays(); });
$("#imgDay").addEventListener("change", (e) => { img.day = e.target.value; loadImages().catch((er) => imgMessage(esc(er.message))); });
$("#imgStep").addEventListener("change", (e) => { img.step = Number(e.target.value); img.page = 0; renderGrid(); });
$("#imgRefresh").addEventListener("click", () => loadDays(true));
$("#imgPrev").addEventListener("click", () => { img.page--; renderGrid(); window.scrollTo(0, 0); });
$("#imgNext").addEventListener("click", () => { img.page++; renderGrid(); window.scrollTo(0, 0); });
$("#imgGrid").addEventListener("click", (e) => {
  const f = e.target.closest("figure"); if (f) openViewer(Number(f.dataset.i));
});

// ---- viewer
let vIdx = 0, vTimer = null;
const vList = () => shownImages();
function vShow(i, size = "full") {
  const list = vList();
  if (!list.length) return;
  vIdx = Math.max(0, Math.min(list.length - 1, i));
  const it = list[vIdx];
  $("#vImg").src = imgUrl(it.p, size);
  $("#vTime").textContent = `${fmtDay(img.day)} ${it.t}`;
  $("#vName").textContent = it.p.split("/").pop();
  $("#vDownload").href = imgUrl(it.p, "full") + "&download=1";
  $("#vSlider").max = list.length - 1; $("#vSlider").value = vIdx;
  $("#vPos").textContent = `${vIdx + 1} / ${list.length}`;
  // warm the cache for the next frames
  for (let k = 1; k <= (vTimer ? 6 : 1); k++) {
    const n = list[vIdx + k]; if (n) new Image().src = imgUrl(n.p, vTimer ? "medium" : size);
  }
}
function openViewer(i) { vStop(); $("#viewer").showModal(); vShow(i); }
function vStop() { clearInterval(vTimer); vTimer = null; $("#vPlay").textContent = "▶ Play"; }
function vPlay() {
  if (vTimer) { vStop(); vShow(vIdx); return; }
  if (vIdx >= vList().length - 1) vIdx = 0;
  $("#vPlay").textContent = "❚❚ Pause";
  vTimer = setInterval(() => {
    if (vIdx >= vList().length - 1) { vStop(); vShow(vIdx); return; }
    vShow(vIdx + 1, "medium");
  }, 1000 / Number($("#vSpeed").value));
}
$("#vPrev").addEventListener("click", () => { vStop(); vShow(vIdx - 1); });
$("#vNext").addEventListener("click", () => { vStop(); vShow(vIdx + 1); });
$("#vClose").addEventListener("click", () => $("#viewer").close());
$("#vPlay").addEventListener("click", vPlay);
$("#vSpeed").addEventListener("change", () => { if (vTimer) { vStop(); vPlay(); } });
$("#vSlider").addEventListener("input", (e) => { vStop(); vShow(Number(e.target.value), "thumb"); });
$("#vSlider").addEventListener("change", (e) => vShow(Number(e.target.value)));
$("#viewer").addEventListener("close", vStop);
$("#viewer").addEventListener("keydown", (e) => {
  if (e.target.tagName === "SELECT") return;
  if (e.key === "ArrowLeft") { e.preventDefault(); vStop(); vShow(vIdx - 1); }
  if (e.key === "ArrowRight") { e.preventDefault(); vStop(); vShow(vIdx + 1); }
  if (e.key === " ") { e.preventDefault(); vPlay(); }
});
$("#viewer").addEventListener("click", (e) => { if (e.target === $("#viewer")) $("#viewer").close(); });
let swipeX = null;   // swipe left / right on phones
$("#vImg").addEventListener("touchstart", (e) => { swipeX = e.touches[0].clientX; }, { passive: true });
$("#vImg").addEventListener("touchend", (e) => {
  if (swipeX === null) return;
  const dx = e.changedTouches[0].clientX - swipeX; swipeX = null;
  if (Math.abs(dx) > 40) { vStop(); vShow(vIdx + (dx < 0 ? 1 : -1)); }
});

// ================================================================== HOME ASSISTANT
const haForm = $("#haForm");
function syncMqttMode() { $$(".mqtt-custom", haForm).forEach((el) => (el.hidden = haForm.mode.value !== "custom")); }
haForm.mode.addEventListener("change", syncMqttMode);
function renderHa() {
  const m = S.mqtt || {};
  const pill = $("#mqttStatus");
  const label = { connected: "connected", connecting: "connecting…", error: "error", unavailable: "no broker", off: "off" }[m.status] || m.status;
  pill.textContent = label;
  pill.className = "pill " + ({ connected: "ok", connecting: "warn", error: "bad", unavailable: "warn" }[m.status] || "");
  $("#mqttDetail").textContent = [m.broker && m.status !== "off" ? m.broker : "", m.detail].filter(Boolean).join(" · ");
  if (haForm.contains(document.activeElement) && document.activeElement !== haForm.querySelector("button")) return;
  const sig = JSON.stringify([m.settings, S.ha]);
  if (haForm.dataset.sig === sig) return;
  haForm.dataset.sig = sig;
  fillForm(haForm, { ...m.settings, password: "", ha_notifications: S.ha?.notifications });
  haForm.password.placeholder = m.settings?.has_password ? "unchanged" : "";
  syncMqttMode();
}
haForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = formData(haForm);
  const { ha_notifications, ...mqtt } = f;
  $("#haMsg").textContent = "Saving…";
  try {
    await api("api/settings", { body: { ha_notifications, mqtt: { ...mqtt, reconnect: true } } });
    $("#haMsg").textContent = "Saved.";
    haForm.dataset.sig = "";
    setTimeout(refresh, 1500);
  } catch (err) { $("#haMsg").textContent = err.message; }
});

// ================================================================== SETUP
function renderSetup() {
  $("#camList").innerHTML = S.cameras.length ? S.cameras.map((c) => {
    const i = c.info || {};
    const ctl = Object.keys(i.controls || {}).map((k) => S.controls.find((x) => x.key === k)?.label || k).join(", ");
    return `<div class="card item"><div><b>${esc(c.name)}</b> <span class="pill ${c.online ? "ok" : "bad"}">${c.online ? "online" : "offline"}</span>
      <div class="sub">${esc(c.host)}${c.port != 80 ? ":" + c.port : ""} · ${esc(i.model || "model unknown")}${i.firmware ? " · fw " + esc(i.firmware) : ""}${i.max_resolution ? " · snapshots up to " + i.max_resolution : ""}</div>
      <div class="sub">${ctl ? "Adjustable: " + esc(ctl) : "Image settings: not detected yet"}</div>
      ${c.last_error && !c.online ? `<div class="sub bad-text">${esc(c.last_error)}</div>` : ""}</div>
      <div class="row gap"><button class="secondary small-btn" data-cam-probe="${c.id}">Detect</button><button class="secondary small-btn" data-cam-edit="${c.id}">Edit</button></div></div>`;
  }).join("") : `<div class="small muted">No cameras yet.</div>`;
  $("#destList").innerHTML = S.destinations.length ? S.destinations.map((d) => `<div class="card item"><div><b>${esc(d.name)}</b>
      <div class="sub">${d.type === "smb" ? "Network share" : "Local folder"}: ${esc(d.path)}${d.username ? " · as " + esc(d.username) : ""}</div></div>
      <div class="row gap"><button class="secondary small-btn" data-dest-test="${d.id}">Test</button><button class="secondary small-btn" data-dest-edit="${d.id}">Edit</button></div></div>`).join("")
    : `<div class="small muted">No storage yet. Add a network share like <code>\\\\nas\\timelapse</code>.</div>`;
}
const camForm = $("#camForm"), destForm = $("#destForm");
$("#newCam").addEventListener("click", () => openCam(null));
$("#newDest").addEventListener("click", () => openDest(null));
function openCam(c) {
  camForm.reset(); fillForm(camForm, c || { port: 80, preview_resolution: 0, username: "admin" });
  camForm.id.value = c?.id || ""; camForm.password.value = "";
  camForm.password.placeholder = c?.has_password ? "unchanged" : "";
  $("#deleteCam").hidden = !c; $("#camFormMsg").textContent = "";
  const max = c?.info?.max_resolution;
  [...camForm.preview_resolution.options].forEach((o) => {
    o.disabled = !!(max && +o.value > max);
    o.textContent = o.textContent.replace(/ \(not on this camera\)$/, "") + (o.disabled ? " (not on this camera)" : "");
  });
  if (camForm.preview_resolution.selectedOptions[0]?.disabled) camForm.preview_resolution.value = "0";
  $("#camDialog").showModal();
}
function openDest(d) {
  destForm.reset(); fillForm(destForm, d || { type: "smb" });
  destForm.id.value = d?.id || ""; destForm.password.value = "";
  destForm.password.placeholder = d?.has_password ? "unchanged" : "";
  $("#deleteDest").hidden = !d; $("#destFormMsg").textContent = ""; syncDestType();
  $("#destDialog").showModal();
}
function syncDestType() {
  const smb = destForm.type.value === "smb";
  $$(".smb-only", destForm).forEach((el) => (el.hidden = !smb));
  destForm.path.placeholder = smb ? "\\\\nas\\timelapse\\site1" : "/share/panalapse";
}
destForm.type.addEventListener("change", syncDestType);
$(".setup-grid").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.camEdit) openCam(camById(b.dataset.camEdit));
  if (b.dataset.destEdit) openDest(destById(b.dataset.destEdit));
  if (b.dataset.camProbe) {
    b.disabled = true; b.textContent = "Detecting…";
    try { const r = await api(`api/cameras/${b.dataset.camProbe}/probe`, { body: {} }); toast(`Found ${r.info.model} (${Object.keys(r.info.controls).length} adjustable settings)`); }
    catch (err) { toast(err.message, true); }
    refresh();
  }
  if (b.dataset.destTest) {
    b.disabled = true; b.textContent = "Testing…";
    try { const r = await api(`api/destinations/${b.dataset.destTest}/test`, { body: {} }); toast(r.message); }
    catch (err) { toast("Storage test failed: " + err.message, true); }
    b.disabled = false; b.textContent = "Test";
  }
});
camForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#camFormMsg").textContent = "Saving…";
  try {
    const r = await api("api/cameras", { body: formData(camForm) });
    $("#camDialog").close(); liveCam = liveCam || r.id;
    toast("Saved – detecting camera…"); refresh();
  } catch (err) { $("#camFormMsg").textContent = err.message; }
});
destForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#destFormMsg").textContent = "Saving and testing…";
  try {
    const r = await api("api/destinations", { body: formData(destForm) });
    try { const t = await api(`api/destinations/${r.id}/test`, { body: {} }); toast(t.message); }
    catch (err) { toast("Saved, but the write test failed: " + err.message, true); }
    $("#destDialog").close(); refresh();
  } catch (err) { $("#destFormMsg").textContent = err.message; }
});
$("#cancelCam").addEventListener("click", () => $("#camDialog").close());

// ---- find cameras
const findForm = $("#findForm");
let findResults = [];
$("#findCams").addEventListener("click", async () => {
  $("#findMsg").textContent = ""; $("#findList").innerHTML = "";
  $("#findDialog").showModal();
  if (!findForm.targets.value) {
    findForm.targets.placeholder = "Looking up your network…";
    try { const r = await api("api/discover"); if (!findForm.targets.value) findForm.targets.value = r.subnets.join(", "); }
    catch (err) { /* leave it for the user to type */ }
    findForm.targets.placeholder = "192.168.1.0/24, 10.0.0.10-40";
  }
});
$("#closeFind").addEventListener("click", () => $("#findDialog").close());
findForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const go = $("#findGo"); go.disabled = true;
  $("#findMsg").textContent = "Scanning…"; $("#findList").innerHTML = "";
  try {
    const r = await api("api/discover", { body: formData(findForm) });
    findResults = r.found;
    const n = r.found.filter((f) => f.status === "ok").length;
    $("#findMsg").textContent = `Checked ${r.scanned} addresses in ${r.seconds} s` +
      (r.found.length ? "" : ": no cameras answered.");
    $("#findList").innerHTML = r.found.map((f, i) => `<div class="card item"><div>
        <b>${esc(f.model || (f.status === "login" ? "Login needed" : "Camera"))}</b> <span class="muted">${esc(f.host)}</span>
        <div class="sub">${f.status === "added" ? "Already added as " + esc(f.name) : f.status === "login" ? esc(f.detail) : "Panasonic AW camera"}</div></div>
        ${f.status === "added" ? "" : `<button class="small-btn" data-find-add="${i}">Add</button>`}</div>`).join("");
    if (n === 0 && r.found.some((f) => f.status === "login") && !findForm.username.value)
      $("#findMsg").textContent += " Some devices want a login: enter the camera user name and password and scan again to confirm them.";
  } catch (err) { $("#findMsg").textContent = err.message; }
  go.disabled = false;
});
$("#findList").addEventListener("click", (e) => {
  const b = e.target.closest("[data-find-add]"); if (!b) return;
  const f = findResults[+b.dataset.findAdd];
  $("#findDialog").close();
  openCam(null);
  camForm.host.value = f.host;
  camForm.name.value = f.model ? `${f.model} (${f.host.split(".").pop()})` : "";
  if (findForm.username.value) camForm.username.value = findForm.username.value;
  if (findForm.password.value) camForm.password.value = findForm.password.value;
});
$("#cancelDest").addEventListener("click", () => $("#destDialog").close());
$("#deleteCam").addEventListener("click", async () => {
  if (!confirm("Remove this camera? Timelapses using it will stop working until you pick another camera.")) return;
  await api(`api/cameras/${camForm.id.value}`, { method: "DELETE" }); $("#camDialog").close(); refresh();
});
$("#deleteDest").addEventListener("click", async () => {
  if (!confirm("Remove this storage location? Files already on it are not touched.")) return;
  await api(`api/destinations/${destForm.id.value}`, { method: "DELETE" }); $("#destDialog").close(); refresh();
});

// ------------------------------------------------------------------ boot
refresh().then(() => showTab(localStorage.getItem("tab") || "live"));
