// Strata web UI. No framework; every value from the API is inserted as text, never as HTML,
// because journal entries and VM names can carry data produced inside VMs.

const app = document.getElementById("app");
let me = null;
let timers = [];
let rfb = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function spinner(label) {
  return el("span", { class: "spin-wrap" }, el("span", { class: "spinner" }), label ? el("span", {}, label) : null);
}

function toast(msg, error = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = error ? "error" : "";
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), error ? 6000 : 2500);
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Strata": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (r.status === 401 && path !== "/api/login") { showLogin(); throw new Error("not logged in"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.blob();
  if (!r.ok) throw new Error(data.error + (data.hint ? ` (${data.hint})` : ""));
  return data;
}

async function act(button, fn, okMsg) {
  const label = button.textContent;
  button.disabled = true;
  button.classList.add("busy");
  try { const res = await fn(); if (okMsg) toast(okMsg); return res; }
  catch (e) { toast(e.message, true); }
  finally { button.disabled = false; button.classList.remove("busy"); button.textContent = label; }
}

function copyButton(text) {
  return el("button", { class: "ghost copy", onclick: () => navigator.clipboard.writeText(text).then(() => toast("Copied")) }, "copy");
}

function ago(ts) {
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleString();
}

function clearPage() {
  timers.forEach(clearInterval);
  timers = [];
  if (rfb) { rfb.disconnect(); rfb = null; }
  app.replaceChildren();
}

const osIcon = (os) => os === "windows" ? "⊞" : "🐧";

// ---------- login and routing ----------

function showLogin() {
  clearPage();
  document.getElementById("nav").hidden = true;
  const token = el("input", { type: "password", placeholder: "strata_…", autocomplete: "off" });
  const go = el("button", { type: "submit" }, "Log in");
  app.append(el("form", { class: "panel login", onsubmit: async (ev) => {
    ev.preventDefault();
    await act(go, async () => { await api("POST", "/api/login", { token: token.value.trim() }); await start(); });
  } }, el("div", { class: "login-brand" }, el("span", { class: "brand-mark big" }), el("h1", {}, "Strata")),
     el("p", { class: "muted" }, "Disposable and long-lived VMs for humans and agents. Paste your token — the admin token is in /var/lib/strata/admin-token on the host."),
     el("div", { class: "row" }, el("label", { style: "flex:1" }, "Token", token), go)));
  token.focus();
}

function navigate(path) {
  history.pushState(null, "", path);
  route();
}

function route() {
  clearPage();
  const path = location.pathname;
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("active", a.getAttribute("href") === path));
  if (path.startsWith("/vm/")) return vmPage(decodeURIComponent(path.slice(4)));
  if (path === "/images") return imagesPage();
  if (path === "/guide") return guidePage();
  if (path === "/agents" && me.is_admin) return agentsPage();
  return vmsPage();
}

document.addEventListener("click", (ev) => {
  const a = ev.target.closest("a[data-link]");
  if (a && !ev.metaKey && !ev.ctrlKey) { ev.preventDefault(); navigate(a.getAttribute("href")); }
});
window.addEventListener("popstate", route);
document.getElementById("logout").addEventListener("click", async () => {
  await api("POST", "/api/logout");
  showLogin();
});

async function start() {
  try { me = await api("GET", "/api/me"); } catch { return; }
  document.getElementById("nav").hidden = false;
  document.getElementById("nav-agents").hidden = !me.is_admin;
  document.getElementById("whoami").textContent = me.name + (me.is_admin ? " · admin" : "");
  route();
}

// ---------- VM list + dashboard ----------

function statCard(n, label, cls = "") {
  return el("div", { class: "stat " + cls }, el("div", { class: "n" }, String(n)), el("div", { class: "l" }, label));
}

async function vmsPage() {
  const stats = el("div", { class: "stats" });
  const list = el("tbody");
  const images = el("select");
  const name = el("input", { placeholder: "auto" });
  const vcpus = el("input", { type: "number", min: 1, max: 16, placeholder: "auto", style: "width:84px" });
  const ram = el("input", { type: "number", min: 1024, step: 1024, placeholder: "auto", style: "width:104px" });
  const internet = el("input", { type: "checkbox" });
  const permanent = el("input", { type: "checkbox" });
  const create = el("button", { type: "submit" }, "Create VM");
  const status = el("div", { class: "create-status", hidden: true });
  const fields = [images, name, vcpus, ram, internet, permanent];

  app.append(
    el("h1", {}, "Virtual machines"),
    stats,
    el("form", { class: "panel", onsubmit: async (ev) => {
      ev.preventDefault();
      if (!images.value) { toast("No image available — build one first (Images page).", true); return; }
      const target = name.value || images.value;
      fields.forEach(f => f.disabled = true);
      create.disabled = true;
      status.hidden = false;
      status.className = "create-status";
      status.replaceChildren(spinner(),
        el("span", {}, `Creating ${target}… this takes about a minute (Windows: a few). It shows up below as it boots, and opens here when ready.`));
      try {
        const vm = await api("POST", "/api/vms", {
          image: images.value, name: name.value || null, vcpus: +vcpus.value || null, ram_mb: +ram.value || null,
          internet: internet.checked, permanent: permanent.checked });
        navigate(`/vm/${encodeURIComponent(vm.name)}`);
      } catch (e) {
        status.className = "create-status bad";
        status.replaceChildren(el("span", {}, "Could not create: " + e.message));
        fields.forEach(f => f.disabled = false);
        create.disabled = false;
      }
    } }, el("h2", {}, "New VM"),
      el("div", { class: "row" },
        el("label", {}, "Image", images), el("label", {}, "Name", name), el("label", {}, "vCPUs", vcpus),
        el("label", {}, "RAM (MB)", ram), el("label", { class: "check" }, internet, "Internet"),
        me.is_admin ? el("label", { class: "check" }, permanent, "Permanent") : null, create),
      status),
    el("div", { class: "panel pad0" }, el("table", { class: "vms" },
      el("thead", {}, el("tr", {}, ["Name", "OS", "State", "Size", "Internet", "Kind", "Expires", "Owner", "Snapshots", ""].map(h => el("th", {}, h)))),
      list)));

  const imgs = await api("GET", "/api/images");
  for (const img of imgs) images.append(el("option", { value: img.name }, `${img.name} · ${img.os}`));
  if (!images.options.length) { images.append(el("option", { value: "" }, "no image yet")); create.disabled = true; }

  async function refresh() {
    const vms = await api("GET", "/api/vms");
    const running = vms.filter(v => v.state === "running").length;
    const cards = [statCard(vms.length, "VMs"), statCard(running, "running", "ok"), statCard(imgs.length, "images")];
    if (me.is_admin) {
      try { const ags = await api("GET", "/api/agents"); cards.push(statCard(ags.filter(a => !a.revoked_at).length, "agents")); } catch {}
    }
    stats.replaceChildren(...cards);
    list.replaceChildren(...(vms.length ? vms.map(v => {
      const quick = v.state === "running"
        ? el("button", { class: "ghost mini", onclick: (e) => { e.stopPropagation(); act(e.target, () => api("POST", `/api/vms/${encodeURIComponent(v.name)}/power`, { action: "stop" }), "Stopping").then(refresh); } }, "Stop")
        : el("button", { class: "ghost mini", onclick: (e) => { e.stopPropagation(); act(e.target, () => api("POST", `/api/vms/${encodeURIComponent(v.name)}/power`, { action: "start" }), "Starting").then(refresh); } }, "Start");
      return el("tr", { class: "clickable", onclick: () => navigate(`/vm/${encodeURIComponent(v.name)}`) },
        el("td", {}, el("a", { href: `/vm/${encodeURIComponent(v.name)}`, "data-link": true }, v.name)),
        el("td", { class: "muted" }, `${osIcon(v.os)} ${v.os}`),
        el("td", {}, el("span", { class: `state ${v.state}` }, v.state)),
        el("td", { class: "muted" }, `${v.vcpus} vCPU · ${v.ram_mb} MB`),
        el("td", {}, el("span", { class: v.internet ? "badge on" : "badge off" }, v.internet ? "on" : "off")),
        el("td", {}, el("span", { class: "badge " + (v.kind === "permanent" ? "perm" : "") }, v.kind)),
        el("td", { class: "muted" }, v.expires_in_hours === null ? "never" : `in ${v.expires_in_hours} h`),
        el("td", { class: "muted" }, v.owner), el("td", { class: "muted" }, v.snapshots.join(", ") || "–"),
        el("td", { style: "text-align:right" }, quick));
    }) : [el("tr", {}, el("td", { colspan: 10, class: "muted empty" }, "No VM yet — create one above."))]));
  }
  await refresh();
  timers.push(setInterval(refresh, 5000));
}

// ---------- images ----------

async function imagesPage() {
  const list = el("tbody");
  app.append(
    el("h1", {}, "Images"),
    el("p", { class: "muted", style: "margin-top:-8px" },
      "Clean base images. Copies and forks are instant overlays on top of these. Build more with the scripts in ", el("code", {}, "images/"), "."),
    el("div", { class: "panel pad0" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Name", "OS", "Description", me.is_admin ? "" : null].filter(h => h !== null).map(h => el("th", {}, h)))),
      list)));

  async function refresh() {
    const imgs = await api("GET", "/api/images");
    list.replaceChildren(...(imgs.length ? imgs.map(img => {
      const del = me.is_admin ? el("button", { class: "danger mini", onclick: () =>
        confirm(`Delete image "${img.name}" and its volume? VMs using it must be gone first.`) &&
        act(del, () => api("DELETE", `/api/images/${encodeURIComponent(img.name)}`), "Image deleted").then(refresh) }, "Delete") : null;
      return el("tr", {},
        el("td", {}, el("strong", {}, img.name)),
        el("td", { class: "muted" }, `${osIcon(img.os)} ${img.os}`),
        el("td", { class: "muted" }, img.description || "–"),
        me.is_admin ? el("td", { style: "text-align:right" }, del) : null);
    }) : [el("tr", {}, el("td", { colspan: 4, class: "muted empty" },
      "No image registered yet. Build one on the host: ", el("code", {}, "images/linux/build-ubuntu.sh 24.04"), "."))]));
  }
  await refresh();
}

// ---------- guide ----------

function guidePage() {
  const origin = location.origin;
  const groups = [
    ["Discover & create", [
      ["list_images", "the clean base images you can start from"],
      ["create_vm", "new VM from an image (image, name?, vcpus?, ram_mb?, internet?)"],
      ["list_vms / get_vm", "your VMs, and one VM's details + access (SSH, console, password)"],
      ["fork", "new VM from a snapshot — branch off any saved state, instantly"],
    ]],
    ["Run things inside", [
      ["exec", "run a command, get stdout/stderr/exit code (command, shell?, timeout?)"],
      ["upload / download", "copy a file in or out — any file, .exe/.dll included, no limit"],
    ]],
    ["See & drive the screen", [
      ["screenshot", "PNG of the live screen"],
      ["click / type_text / send_keys", "mouse and keyboard, e.g. for a GUI installer"],
    ]],
    ["Save & compare states", [
      ["snapshot", "save the VM's state (with_memory? keeps RAM too)"],
      ["list_snapshots / revert", "list saved states; roll back to one"],
      ["delete_snapshot", "forget a saved state"],
      ["diff", "what changed on disk between two states (files, Windows registry)"],
    ]],
    ["Lifecycle & control", [
      ["power", "start / stop / reboot / force_stop"],
      ["keep", "extend expiry or make permanent (admin) — disposable VMs auto-expire"],
      ["set_internet", "allow or block internet for this VM (off by default)"],
      ["destroy", "delete the VM and its snapshots"],
      ["history", "the journal of everything that happened"],
    ]],
  ];

  app.append(
    el("h1", {}, "Guide"),
    el("p", { class: "muted", style: "margin-top:-8px;max-width:70ch" },
      "Agents drive Strata over MCP; humans use this web UI or the ", el("code", {}, "strata"),
      " CLI. Everything goes through the same checked, journaled actions — no raw hypervisor access."),

    el("div", { class: "panel" }, el("h2", {}, "1 · Connect an agent"),
      el("p", { class: "muted" }, "Generate a per-agent token on the ",
        el("a", { href: "/agents", "data-link": true }, "Agents"),
        " page (admin). Each agent gets one line to paste — then zero config:"),
      el("div", { class: "secret" },
        el("code", {}, `claude mcp add --transport http strata ${origin}/mcp \\\n  --header "Authorization: Bearer strata_<the-token>"`),
        copyButton(`claude mcp add --transport http strata ${origin}/mcp --header "Authorization: Bearer strata_<the-token>"`)),
      el("p", { class: "muted", style: "margin-bottom:0" }, "Other MCP clients: endpoint ",
        el("code", {}, `${origin}/mcp`), ", header ", el("code", {}, "Authorization: Bearer <token>"),
        ". A token only ever sees its own VMs.")),

    el("div", { class: "panel" }, el("h2", {}, "2 · What an agent can do (MCP tools)"),
      ...groups.map(([title, tools]) => el("div", { style: "margin-bottom:14px" },
        el("div", { style: "font-weight:600;margin-bottom:6px" }, title),
        el("table", { class: "tools" }, el("tbody", {}, ...tools.map(([n, d]) =>
          el("tr", {}, el("td", { style: "white-space:nowrap;width:1%" }, el("code", {}, n)),
            el("td", { class: "muted" }, d)))))))),

    el("div", { class: "panel" }, el("h2", {}, "3 · A typical session"),
      el("ol", { class: "steps" },
        el("li", {}, el("code", {}, "create_vm"), " from an image → you get a running VM with SSH + a live console."),
        el("li", {}, el("code", {}, "exec"), " / ", el("code", {}, "upload"), " to set things up (drop a payload, run a script)."),
        el("li", {}, el("code", {}, "snapshot"), " the clean state before an experiment."),
        el("li", {}, "Do the risky thing. Then ", el("code", {}, "diff"), " against the snapshot to see exactly what changed."),
        el("li", {}, el("code", {}, "revert"), " to undo, or ", el("code", {}, "fork"), " the snapshot into a fresh VM to branch."),
        el("li", {}, el("code", {}, "destroy"), " when done — or leave it; disposable VMs expire on their own."))),

    el("div", { class: "panel" }, el("h2", {}, "Humans & security"),
      el("p", { class: "muted", style: "margin-top:0" }, "Same actions from a shell with the ",
        el("code", {}, "strata"), " CLI (e.g. ", el("code", {}, "strata exec <vm> -- <cmd>"), ", ",
        el("code", {}, "strata snapshot <vm> <name>"), "), or from this UI (live screen, snapshot tree, journal)."),
      el("p", { class: "muted", style: "margin-bottom:0" }, "Every VM is sealed off by default: no route to the host, the LAN, the hypervisor or the internet. ",
        el("code", {}, "set_internet"), " opens internet for one VM only — never a private network.")));
}

// ---------- one VM ----------

async function connectScreen(screen, name) {
  try {
    const { default: RFB } = await import("/novnc/core/rfb.js");
    const proto = location.protocol === "https:" ? "wss" : "ws";
    screen.replaceChildren();
    rfb = new RFB(screen, `${proto}://${location.host}/api/vms/${encodeURIComponent(name)}/console`, { shared: true });
    rfb.scaleViewport = true;
    rfb.addEventListener("disconnect", () => { rfb = null; screen.replaceChildren(el("span", {}, "Screen disconnected (is the VM running?)")); });
  } catch (e) {
    screen.replaceChildren(el("span", {}, "Live screen unavailable: " + e.message));
  }
}

async function vmPage(name) {
  let vm;
  try { vm = await api("GET", `/api/vms/${encodeURIComponent(name)}`); }
  catch (e) { app.append(el("p", { class: "bad" }, e.message)); return; }
  const base = `/api/vms/${encodeURIComponent(name)}`;
  const screen = el("div", { id: "screen" }, spinner("Connecting to the live screen…"));
  const info = el("dl", { class: "kv" });
  const snaps = el("tbody");
  const journal = el("tbody");
  const snapName = el("input", { placeholder: "before-test", pattern: "[a-z0-9][a-z0-9-]*" });
  const withMemory = el("input", { type: "checkbox", checked: true });
  const snapBtn = el("button", { type: "submit" }, "Save state");
  const diffA = el("select");
  const diffB = el("select");
  const diffBtn = el("button", { type: "submit" }, "Compare");
  const diffOut = el("div", { class: "mono", style: "margin-top:10px;max-height:320px;overflow:auto" });

  const powerBtn = (label, action, cls = "ghost") => {
    const b = el("button", { class: cls, onclick: () => act(b, () => api("POST", `${base}/power`, { action }), `${label}: done`).then(refresh) }, label);
    return b;
  };
  const destroyBtn = el("button", { class: "danger", onclick: async () => {
    if (!confirm(`Destroy ${name} and all its snapshots?`)) return;
    if (await act(destroyBtn, () => api("DELETE", base), "Destroyed")) navigate("/");
  } }, "Destroy");
  const keepBtn = el("button", { class: "ghost", onclick: () => act(keepBtn, () => api("POST", `${base}/keep`, { hours: 24 }), "Expires in 24 h").then(refresh) }, "+24 h");
  const netBtn = el("button", { class: "ghost", onclick: () => act(netBtn, () => api("POST", `${base}/internet`, { enabled: !vm.internet })).then(refresh) });
  const cadBtn = el("button", { class: "ghost", onclick: () => rfb ? rfb.sendCtrlAltDel() : toast("Screen not connected", true) }, "Ctrl+Alt+Del");

  app.append(
    el("h1", {}, el("a", { href: "/", "data-link": true, class: "back" }, "VMs"), " / ", name,
      " ", el("span", { class: "muted", style: "font-weight:400;font-size:14px" }, `${osIcon(vm.os)} ${vm.os} · ${vm.source}`)),
    el("div", { class: "grid" },
      el("div", {},
        el("div", { class: "panel" }, screen,
          el("div", { class: "row", style: "margin-top:10px" }, cadBtn, powerBtn("Start", "start"), powerBtn("Shut down", "stop"),
            powerBtn("Reboot", "reboot"), powerBtn("Force off", "force_stop"))),
        el("div", { class: "panel" }, el("h2", {}, "Journal"),
          el("table", { class: "journal" }, el("thead", {}, el("tr", {}, ["When", "Who", "Action", "Result"].map(h => el("th", {}, h)))), journal))),
      el("div", {},
        el("div", { class: "panel" }, el("h2", {}, "Machine"), info,
          el("div", { class: "row", style: "margin-top:12px" }, netBtn, keepBtn, destroyBtn)),
        el("form", { class: "panel", onsubmit: async (ev) => {
          ev.preventDefault();
          if (await act(snapBtn, () => api("POST", `${base}/snapshots`, { name: snapName.value, with_memory: withMemory.checked }), "State saved")) { snapName.value = ""; refresh(); }
        } }, el("h2", {}, "Snapshots"),
          el("div", { class: "row" }, el("label", { style: "flex:1" }, "Name", snapName), el("label", { class: "check" }, withMemory, "with memory"), snapBtn),
          el("table", { style: "margin-top:10px" }, snaps)),
        el("form", { class: "panel", onsubmit: async (ev) => {
          ev.preventDefault();
          if (diffA.value === diffB.value) return toast("Pick two different states", true);
          diffOut.replaceChildren(spinner("Comparing… (the VM is read offline)"));
          const res = await act(diffBtn, () => api("GET",
            `${base}/diff?from=${encodeURIComponent(diffA.value)}&to=${encodeURIComponent(diffB.value)}`));
          if (res) renderDiff(diffOut, res);
        } }, el("h2", {}, "Compare states"),
          el("div", { class: "row" }, el("label", {}, "From", diffA), el("label", {}, "To", diffB), diffBtn),
          diffOut))));

  async function refresh() {
    vm = await api("GET", base);
    const a = vm.access;
    const rows = [
      ["State", el("span", { class: `state ${vm.state}` }, vm.state)],
      ["Kind", vm.kind + (vm.expires_in_hours !== null ? ` · expires in ${vm.expires_in_hours} h` : "")],
      ["Size", `${vm.vcpus} vCPU · ${vm.ram_mb} MB`],
      ["Internet", vm.internet ? "allowed" : "blocked"],
      ["User", [a.username, copyButton(a.username)]],
      ["Password", [el("code", {}, "••••••••"), copyButton(a.password)]],
      ["SSH", [el("code", {}, a.ssh), copyButton(a.ssh)]],
      ["Console", [el("code", {}, a.console), copyButton(a.console)]],
      a.rdp ? ["RDP", [el("code", {}, a.rdp), el("a", { class: "ghost mini", href: `${base}/rdp`, download: `${name}.rdp` }, ".rdp file")]] : null,
    ].filter(Boolean);
    info.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    netBtn.textContent = vm.internet ? "Block internet" : "Allow internet";
    const list = await api("GET", `${base}/snapshots`);
    snaps.replaceChildren(...list.map(s => {
      const revert = el("button", { class: "ghost mini", onclick: () => confirm(`Go back to "${s.name}"? Changes since then are lost.`) &&
        act(revert, () => api("POST", `${base}/snapshots/${encodeURIComponent(s.name)}/revert`), `Back to ${s.name}`).then(() => { refresh(); connectScreen(screen, name); }) }, "Revert");
      const fork = el("button", { class: "ghost mini", onclick: async () => {
        toast("Creating a VM from this state…");
        const v = await act(fork, () => api("POST", `${base}/snapshots/${encodeURIComponent(s.name)}/fork`, {}));
        if (v) navigate(`/vm/${encodeURIComponent(v.name)}`);
      } }, "New VM");
      const del = el("button", { class: "ghost mini", onclick: () => confirm(`Forget "${s.name}"?`) &&
        act(del, () => api("DELETE", `${base}/snapshots/${encodeURIComponent(s.name)}`), "Deleted").then(refresh) }, "Delete");
      return el("tr", {}, el("td", {}, el("strong", {}, s.name), el("div", { class: "muted" }, `${ago(s.created_at)}${s.with_memory ? " · with memory" : ""}`)),
        el("td", { style: "text-align:right;white-space:nowrap" }, revert, " ", fork, " ", del));
    }));
    if (!list.length) snaps.append(el("tr", {}, el("td", { class: "muted" }, "No saved state yet.")));
    const opts = list.map(s => s.name);
    for (const sel of [diffA, diffB]) {
      const keep = sel.value;
      sel.replaceChildren(...opts.map(n => el("option", { value: n }, n)));
      if (opts.includes(keep)) sel.value = keep;
    }
    if (opts.length > 1 && diffB.value === diffA.value) diffB.value = opts[opts.length - 1];
    const hist = await api("GET", `/api/history?vm=${encodeURIComponent(name)}&limit=40`);
    journal.replaceChildren(...hist.map(j => el("tr", {},
      el("td", { class: "muted", style: "white-space:nowrap" }, ago(j.ts)), el("td", {}, j.actor),
      el("td", {}, el("strong", {}, j.action), el("div", { class: "mono muted" }, summarize(j.params, 160))),
      el("td", { class: j.ok ? "mono" : "mono bad" }, summarize(j.result, 240)))));
  }
  await refresh();
  connectScreen(screen, name);
  timers.push(setInterval(refresh, 5000));
}

function renderDiff(out, res) {
  const mark = { added: ["+", "var(--ok)"], deleted: ["−", "var(--danger)"], changed: ["~", "var(--warn)"] };
  const header = el("div", { style: "margin-bottom:8px" },
    el("strong", {}, `${res.counts.added} added · ${res.counts.deleted} deleted · ${res.counts.changed} changed`),
    res.truncated ? el("span", { class: "muted" }, `  (showing first ${res.changes.length})`) : null);
  const lines = res.changes.map(c => {
    const [sign, color] = mark[c.change] || ["?", "var(--muted)"];
    return el("div", {}, el("span", { style: `color:${color};font-weight:700` }, sign + " "),
      el("span", {}, c.path), c.details ? el("span", { class: "muted" }, "  " + c.details) : null);
  });
  out.replaceChildren(header, ...(lines.length ? lines : [el("span", { class: "muted" }, "No file differences.")]));
}

function summarize(json, max) {
  let text = json;
  try { const v = JSON.parse(json); text = typeof v === "string" ? v : JSON.stringify(v); } catch {}
  return text.length > max ? text.slice(0, max) + "…" : text;
}

// ---------- agents (admin) ----------

async function agentsPage() {
  const list = el("tbody");
  const result = el("div");
  const name = el("input", { placeholder: "claude-1", required: true, pattern: "[a-z0-9][a-z0-9_-]*" });
  const maxVms = el("input", { type: "number", value: 3, min: 1, style: "width:80px" });
  const maxRam = el("input", { type: "number", value: 16384, step: 1024, style: "width:110px" });
  const maxCpu = el("input", { type: "number", value: 8, min: 1, style: "width:80px" });
  const days = el("input", { type: "number", placeholder: "never", min: 1, style: "width:90px" });
  const create = el("button", { type: "submit" }, "Generate token");

  app.append(
    el("h1", {}, "Agents"),
    el("p", { class: "muted", style: "margin-top:-8px" }, "Each agent gets its own token. It only sees its own VMs, bounded by the limits below. Revoke anytime."),
    el("form", { class: "panel", onsubmit: async (ev) => {
      ev.preventDefault();
      const res = await act(create, () => api("POST", "/api/agents", {
        name: name.value, max_vms: +maxVms.value, max_ram_mb: +maxRam.value, max_vcpus: +maxCpu.value,
        expires_in_days: +days.value || null }));
      if (!res) return;
      name.value = "";
      result.replaceChildren(el("div", { class: "secret" },
        el("strong", {}, `Token for ${res.name} — shown once, copy it now.`),
        el("div", { class: "muted", style: "margin-top:8px" }, "Claude Code: paste this line in a terminal"),
        el("code", {}, res.connect), copyButton(res.connect),
        el("div", { class: "muted", style: "margin-top:10px" }, "Other MCP clients: URL and token"),
        el("code", {}, `${location.origin}/mcp\nAuthorization: Bearer ${res.token}`), copyButton(res.token)));
      refresh();
    } }, el("h2", {}, "New agent"),
      el("div", { class: "row" }, el("label", {}, "Name", name), el("label", {}, "Max VMs", maxVms),
        el("label", {}, "Max RAM (MB)", maxRam), el("label", {}, "Max vCPUs", maxCpu),
        el("label", {}, "Expires (days)", days), create),
      el("div", { style: "margin-top:12px" }, result)),
    el("div", { class: "panel pad0" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Name", "Role", "Limits", "Created", "Status", ""].map(h => el("th", {}, h)))), list)));

  async function refresh() {
    const agents = await api("GET", "/api/agents");
    list.replaceChildren(...agents.map(a => {
      const status = a.revoked_at ? "revoked" : (a.expires_at && a.expires_at * 1000 < Date.now()) ? "expired"
        : a.expires_at ? `expires ${new Date(a.expires_at * 1000).toLocaleDateString()}` : "active";
      const revoke = el("button", { class: "danger mini", onclick: () => confirm(`Revoke ${a.name}? Its token stops working immediately.`) &&
        act(revoke, () => api("DELETE", `/api/agents/${encodeURIComponent(a.name)}`), "Revoked").then(refresh) }, "Revoke");
      return el("tr", {}, el("td", {}, el("strong", {}, a.name)), el("td", {}, el("span", { class: "badge " + (a.is_admin ? "perm" : "") }, a.is_admin ? "admin" : "agent")),
        el("td", { class: "muted" }, `${a.max_vms} VMs · ${a.max_ram_mb} MB · ${a.max_vcpus} vCPU`),
        el("td", { class: "muted" }, ago(a.created_at)),
        el("td", {}, el("span", { class: "badge " + (status === "active" ? "on" : status === "revoked" || status === "expired" ? "off" : "") }, status)),
        el("td", { style: "text-align:right" }, a.revoked_at ? null : revoke));
    }));
  }
  refresh();
}

start();
