import { STATUS_LABEL, STATUSES, buttonsFor, costSummary, filterRows, formatUsd, groupByVersion, nextPollDelay,
  statusOf, versionButtons } from "/static/table-logic.js";

const $ = (id) => document.getElementById(id);
let csrf = "";
let rows = [];
let spend = null;
let timer = null;
let filter = "all";
const openTips = new Set();
const inflight = new Set();   // drawings whose "make clip" was just clicked

async function api(path, { method = "GET", json, form } = {}) {
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  let body;
  if (json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(json); }
  if (form) body = form;
  const res = await fetch(path, { method, headers, body, credentials: "same-origin" });
  if (res.status === 401) { showLogin(); throw new Error("signed out"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== false && v !== undefined) node.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) node.append(kid);
  return node;   // text goes through append(): never innerHTML
}

function showLogin() { $("login").hidden = false; $("app").hidden = true; clearTimeout(timer); }
function showApp() { $("login").hidden = true; $("app").hidden = false; }

// Modal. With opts.prompt (a string), shows an editable box and resolves to {ok, prompt}.
function ask(title, lines, okLabel, opts = {}) {
  return new Promise((resolve) => {
    const dlg = $("modal");
    $("modal-title").textContent = title;
    const kids = lines.map((t) => el("p", {}, t));
    let box = null;
    if (typeof opts.prompt === "string") {
      box = el("textarea", { id: "prompt-box", rows: "4", maxlength: String(opts.maxPrompt || 600), "aria-label": "Prompt" });
      box.value = opts.prompt;
      kids.unshift(el("label", {}, "What should the clip show?", box));
    }
    $("modal-body").replaceChildren(...kids);
    $("modal-ok").textContent = okLabel;
    dlg.addEventListener("close", () => resolve({ ok: dlg.returnValue === "ok", prompt: box ? box.value.trim() : "" }), { once: true });
    dlg.showModal();
  });
}

async function generate(d) {
  const est = await api("/api/estimate");
  const sum = costSummary(est);
  const lines = [...sum.lines];
  if (!sum.free && !sum.allowed) lines.push("Budget left is below the worst case, so the cap may stop this run partway.");
  const label = sum.free ? "Make clip (free)" : `Make clip (about ${formatUsd(sum.typical)})`;
  const answer = await ask("Make a clip?", lines, label, { prompt: est.default_prompt, maxPrompt: est.max_prompt });
  if (!answer.ok) return;
  inflight.add(d.id);
  render();
  await act(() => api(`/api/drawings/${d.id}/generate`, { method: "POST", json: { prompt: answer.prompt } }));
  inflight.delete(d.id);
}

async function remove(d) {
  const answer = await ask("Remove this drawing?",
    ["The drawing, its characters and its clips are deleted from this machine. This cannot be undone.",
     "Money already spent stays in the ledger."], "Delete for good");
  if (answer.ok) await act(() => api(`/api/drawings/${d.id}`, { method: "DELETE" }));
}

async function toggleChannel(d, clip, channel) {
  const active = d.publication && d.publication.clip_id === clip.id && d.publication.channel === channel;
  if (active) await act(() => api(`/api/drawings/${d.id}/unpublish`, { method: "POST" }));
  else await act(() => api(`/api/drawings/${d.id}/publish`, { method: "POST", json: { channel, clip_id: clip.id } }));
}

async function act(fn) {
  try { await fn(); } catch (e) { if (e.message !== "signed out") alert(e.message); }
  await refresh();
}

function whyButton(key, reason) {
  if (!reason) return [];
  const open = openTips.has(key);
  const btn = el("button", { class: "why", type: "button", "aria-expanded": String(open), "aria-label": "Why is this off?",
    onclick: () => { open ? openTips.delete(key) : openTips.add(key); render(); } }, "?");
  return [btn, ...(open ? [el("p", { class: "tip" }, reason)] : [])];
}

function button(label, state, onclick, key) {
  const attrs = { type: "button", title: state.reason || "", onclick };
  if (!state.enabled) attrs.disabled = true;
  if (state.active) attrs.class = "active";
  return [el("button", attrs, label), ...(state.enabled ? [] : whyButton(key, state.reason))];
}

function characterGroups(d) {
  return groupByVersion(d.characters).map((g) => el("div", { class: "version" },
    el("span", { class: "vlabel" }, `v${g.version}`),
    el("div", { class: "strip" }, ...g.items.map((c) => el("label", { title: c.description },
      el("img", { src: `/media/character/${c.id}`, alt: c.description, loading: "lazy" }),
      el("input", { type: "checkbox", checked: c.used, "aria-label": `Use ${c.description} (v${g.version}) in the clip`,
        onchange: (e) => act(() => api(`/api/characters/${c.id}/used`, { method: "POST", json: { used: e.target.checked } })) }))))));
}

function clipGroups(d) {
  return groupByVersion(d.clips).map((g) => el("div", { class: "version" },
    el("span", { class: "vlabel" }, `v${g.version}`),
    ...g.items.map((c) => {
      const b = versionButtons(d, c);
      return el("div", { class: "clip", "data-clip": c.id },
        el("video", { src: `/media/clip/${c.id}`, controls: true, preload: "none", muted: true, playsinline: true }),
        ...(c.prompt ? [el("p", { class: "prompt", title: c.prompt }, c.prompt)] : []),   // text node + attribute: never HTML
        el("div", { class: "pair" },
          ...button("Preview", b.preview, () => toggleChannel(d, c, "preview"), `pv${c.id}`),
          ...button("Public", b.public, () => toggleChannel(d, c, "public"), `pb${c.id}`)));
    })));
}

function rowFor(d) {
  const status = statusOf(d);
  const b = buttonsFor(d, spend);
  if (inflight.has(d.id)) b.generate = { enabled: false, reason: "Sending..." };
  const drawingCell = el("td", { class: "frozen", "data-label": "Drawing" }, el("div", { class: "drawing" },
    el("img", { src: `/media/original/${d.id}`, alt: "Drawing", loading: "lazy" }),
    el("button", { class: "x", type: "button", title: b.remove.reason || "Delete this drawing", disabled: !b.remove.enabled,
      "aria-label": "Delete this drawing", onclick: () => remove(d) }, "✕")));
  const chars = el("td", { "data-label": "Characters" }, ...characterGroups(d));
  const clips = el("td", { "data-label": "Clips" }, ...clipGroups(d));
  const pill = el("span", { class: `pill ${status}` }, STATUS_LABEL[status]);
  const statusCell = el("td", { "data-label": "Status" }, pill,
    ...(status === "failed" && d.job ? [el("p", { class: "err" }, d.job.error || "The job failed.")] : []),
    el("label", { class: "check" },
      el("input", { type: "checkbox", checked: d.ok_online,
        onchange: (e) => act(() => api(`/api/drawings/${d.id}/online`, { method: "POST", json: { ok: e.target.checked } })) }),
      "OK online"));
  const actions = el("td", { class: "actions", "data-label": "Make" },
    ...button(d.clips.length ? "Make another clip" : "Make clip", b.generate, () => generate(d), `g${d.id}`));
  return el("tr", { "data-status": status }, drawingCell, chars, clips, statusCell, actions);
}

function render() {
  const visible = filterRows(rows, filter);
  $("rows").replaceChildren(...visible.map(rowFor));
  $("empty").hidden = rows.length > 0;
  if (spend) $("spend").textContent =
    `Spent ${formatUsd(spend.spent_micro)} of ${formatUsd(spend.cap_micro)} (estimated)`;
}

async function refresh() {
  clearTimeout(timer);
  try {
    [rows, spend] = [(await api("/api/drawings")).drawings, await api("/api/spend")];
    render();
  } catch (e) { if (e.message === "signed out") return; }
  timer = setTimeout(refresh, nextPollDelay(rows));
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: $("password").value }), credentials: "same-origin" });
  if (!res.ok) { $("login-error").textContent = res.status === 429 ? "Too many tries. Wait a minute." : "Wrong password."; return; }
  csrf = (await res.json()).csrf;
  $("password").value = "";
  $("login-error").textContent = "";
  showApp();
  refresh();
});

$("filter").append(el("option", { value: "all" }, "All"), ...STATUSES.map((s) => el("option", { value: s }, STATUS_LABEL[s])));
$("filter").addEventListener("change", (e) => { filter = e.target.value; render(); });
$("add-drawing").addEventListener("click", () => $("upload-modal").showModal());
$("upload-cancel").addEventListener("click", () => $("upload-modal").close());
$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData();
  form.append("file", $("upload-file").files[0]);
  form.append("consent", $("upload-consent").checked ? "1" : "0");
  try {
    await api("/api/drawings", { method: "POST", form });
    $("upload-modal").close(); $("upload-form").reset(); $("upload-error").textContent = "";
    refresh();
  } catch (err) { $("upload-error").textContent = err.message; }
});

// A reload keeps the cookie: ask the server for this session's CSRF token instead of asking for the password again.
async function start() {
  try {
    const res = await fetch("/api/session", { credentials: "same-origin" });
    if (res.ok) { csrf = (await res.json()).csrf; showApp(); refresh(); return; }
  } catch (e) { /* offline or server down: fall through to the sign-in screen */ }
  showLogin();
}
start();
