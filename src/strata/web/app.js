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
  button.disabled = true;
  try { const res = await fn(); if (okMsg) toast(okMsg); return res; }
  catch (e) { toast(e.message, true); }
  finally { button.disabled = false; }
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

// ---------- login and routing ----------

function showLogin() {
  clearPage();
  document.getElementById("nav").hidden = true;
  const token = el("input", { type: "password", placeholder: "strata_…", autocomplete: "off" });
  const go = el("button", { type: "submit" }, "Log in");
  app.append(el("form", { class: "panel login", onsubmit: async (ev) => {
    ev.preventDefault();
    await act(go, async () => { await api("POST", "/api/login", { token: token.value.trim() }); await start(); });
  } }, el("h1", {}, "Strata"),
     el("p", { class: "muted" }, "Paste your token. The admin token is in /var/lib/strata/admin-token on the host."),
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
  document.getElementById("whoami").textContent = me.name + (me.is_admin ? " (admin)" : "");
  route();
}

// ---------- VM list ----------

async function vmsPage() {
  const list = el("tbody");
  const images = el("select");
  const name = el("input", { placeholder: "auto" });
  const vcpus = el("input", { type: "number", min: 1, max: 16, placeholder: "auto", style: "width:80px" });
  const ram = el("input", { type: "number", min: 1024, step: 1024, placeholder: "auto", style: "width:100px" });
  const internet = el("input", { type: "checkbox" });
  const permanent = el("input", { type: "checkbox" });
  const create = el("button", { type: "submit" }, "Create");

  app.append(
    el("h1", {}, "Virtual machines"),
    el("form", { class: "panel", onsubmit: async (ev) => {
      ev.preventDefault();
      toast("Creating… Windows takes a few minutes.");
      const vm = await act(create, () => api("POST", "/api/vms", {
        image: images.value, name: name.value || null, vcpus: +vcpus.value || null, ram_mb: +ram.value || null,
        internet: internet.checked, permanent: permanent.checked }));
      if (vm) navigate(`/vm/${encodeURIComponent(vm.name)}`);
    } }, el("h2", {}, "New VM"),
      el("div", { class: "row" },
        el("label", {}, "Image", images), el("label", {}, "Name", name), el("label", {}, "vCPUs", vcpus),
        el("label", {}, "RAM (MB)", ram), el("label", { class: "check" }, internet, "Internet"),
        me.is_admin ? el("label", { class: "check" }, permanent, "Permanent") : null, create)),
    el("div", { class: "panel" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Name", "OS", "State", "Kind", "Expires", "Owner", "Snapshots"].map(h => el("th", {}, h)))),
      list)));

  for (const img of await api("GET", "/api/images")) images.append(el("option", { value: img.name }, `${img.name} (${img.os})`));
  if (!images.options.length) images.append(el("option", { value: "" }, "no image yet"));

  async function refresh() {
    const vms = await api("GET", "/api/vms");
    list.replaceChildren(...(vms.length ? vms.map(v => el("tr", {},
      el("td", {}, el("a", { href: `/vm/${encodeURIComponent(v.name)}`, "data-link": true }, v.name)),
      el("td", {}, v.os), el("td", {}, el("span", { class: `state ${v.state}` }, v.state)), el("td", {}, v.kind),
      el("td", {}, v.expires_in_hours === null ? "never" : `in ${v.expires_in_hours} h`),
      el("td", {}, v.owner), el("td", {}, v.snapshots.join(", ") || "–")))
      : [el("tr", {}, el("td", { colspan: 7, class: "muted" }, "No VM yet."))]));
  }
  await refresh();
  timers.push(setInterval(refresh, 5000));
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
  const screen = el("div", { id: "screen" }, "Connecting…");
  const info = el("dl", { class: "kv" });
  const snaps = el("tbody");
  const journal = el("tbody");
  const snapName = el("input", { placeholder: "before-test", pattern: "[a-z0-9][a-z0-9-]*" });
  const withMemory = el("input", { type: "checkbox", checked: true });
  const snapBtn = el("button", { type: "submit" }, "Save state");

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
    el("h1", {}, name, " ", el("span", { class: "muted", style: "font-weight:400;font-size:14px" }, `${vm.os} · ${vm.source}`)),
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
          el("table", { style: "margin-top:10px" }, snaps)))));

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
      a.rdp ? ["RDP", [el("code", {}, a.rdp), el("a", { href: `${base}/rdp`, download: `${name}.rdp` }, ".rdp file")]] : null,
    ].filter(Boolean);
    info.replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)]));
    netBtn.textContent = vm.internet ? "Block internet" : "Allow internet";
    const list = await api("GET", `${base}/snapshots`);
    snaps.replaceChildren(...list.map(s => {
      const revert = el("button", { class: "ghost", onclick: () => confirm(`Go back to "${s.name}"? Changes since then are lost.`) &&
        act(revert, () => api("POST", `${base}/snapshots/${encodeURIComponent(s.name)}/revert`), `Back to ${s.name}`).then(() => { refresh(); connectScreen(screen, name); }) }, "Revert");
      const fork = el("button", { class: "ghost", onclick: async () => {
        toast("Creating a VM from this state…");
        const v = await act(fork, () => api("POST", `${base}/snapshots/${encodeURIComponent(s.name)}/fork`, {}));
        if (v) navigate(`/vm/${encodeURIComponent(v.name)}`);
      } }, "New VM");
      const del = el("button", { class: "ghost", onclick: () => confirm(`Forget "${s.name}"?`) &&
        act(del, () => api("DELETE", `${base}/snapshots/${encodeURIComponent(s.name)}`), "Deleted").then(refresh) }, "Delete");
      return el("tr", {}, el("td", {}, el("strong", {}, s.name), el("div", { class: "muted" }, `${ago(s.created_at)}${s.with_memory ? " · with memory" : ""}`)),
        el("td", { style: "text-align:right;white-space:nowrap" }, revert, " ", fork, " ", del));
    }));
    if (!list.length) snaps.append(el("tr", {}, el("td", { class: "muted" }, "No saved state yet.")));
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
    el("form", { class: "panel", onsubmit: async (ev) => {
      ev.preventDefault();
      const res = await act(create, () => api("POST", "/api/agents", {
        name: name.value, max_vms: +maxVms.value, max_ram_mb: +maxRam.value, max_vcpus: +maxCpu.value,
        expires_in_days: +days.value || null }));
      if (!res) return;
      name.value = "";
      result.replaceChildren(el("div", { class: "secret" },
        el("strong", {}, `Token for ${res.name}: shown once, copy it now.`),
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
    el("div", { class: "panel" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Name", "Role", "Limits", "Created", "Status", ""].map(h => el("th", {}, h)))), list)));

  async function refresh() {
    const agents = await api("GET", "/api/agents");
    list.replaceChildren(...agents.map(a => {
      const status = a.revoked_at ? "revoked" : (a.expires_at && a.expires_at * 1000 < Date.now()) ? "expired"
        : a.expires_at ? `expires ${new Date(a.expires_at * 1000).toLocaleDateString()}` : "active";
      const revoke = el("button", { class: "danger", onclick: () => confirm(`Revoke ${a.name}? Its token stops working immediately.`) &&
        act(revoke, () => api("DELETE", `/api/agents/${encodeURIComponent(a.name)}`), "Revoked").then(refresh) }, "Revoke");
      return el("tr", {}, el("td", {}, el("strong", {}, a.name)), el("td", {}, a.is_admin ? "admin" : "agent"),
        el("td", { class: "muted" }, `${a.max_vms} VMs · ${a.max_ram_mb} MB · ${a.max_vcpus} vCPU`),
        el("td", { class: "muted" }, ago(a.created_at)), el("td", {}, status),
        el("td", { style: "text-align:right" }, a.revoked_at ? null : revoke));
    }));
  }
  refresh();
}

start();
