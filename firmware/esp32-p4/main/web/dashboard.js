"use strict";
const $ = selector => document.querySelector(selector);
const encode = text => new TextEncoder().encode(text);
const hex = data => [...new Uint8Array(data)].map(x => x.toString(16).padStart(2, "0")).join("");
const bytes = text => Uint8Array.from(text.match(/../g), x => parseInt(x, 16));
const hash = async data => hex(await crypto.subtle.digest("SHA-256", data));
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
const units = {3:"A",5:"V",9:"kVA",11:"var",12:"kvar",15:"PF",18:"Wh",19:"kWh",27:"Hz",47:"W",48:"kW",71:"h",72:"min",73:"s",95:"",98:"%"};
let state, identity, key, points = [], busy = false, refreshing = false, loadedConfig = false, proposedConfig, diagnosticBoot;

function notice(message, error = false) {
  $("#notice").textContent = message; $("#notice").hidden = !message;
  $("#notice").className = error ? "error" : "";
}
function access() {
  document.querySelectorAll("[data-auth]").forEach(button => button.disabled = !key || busy || !state?.ota?.startup_health?.accepted);
  $("#observe-pins").disabled ||= !!state?.recovery || !!state?.config?.poll_enabled || !state?.pico?.qualified;
  $("#forget").disabled = !key || busy; $("#key-file").disabled = busy;
  $("#auth-state").textContent = key ? "Unlocked for this tab · Commands authenticated" : "Locked · Reading status requires no key.";
}
async function api(path, options = {}) {
  const reply = await fetch(path, {cache:"no-store", credentials:"omit", signal:AbortSignal.timeout(12000), ...options});
  if (!reply.ok) throw new Error(`HTTP ${reply.status}: ${(await reply.text()).slice(0, 180)}`);
  return reply.json();
}
async function readStatus() {
  const next = await api("/api/status");
  if (next.project !== "iqdata_p4_gateway" || next.config.device_instance === 75151 || (next.ethernet?.ip || next.ip) === "192.168.75.151") throw new Error("Protected or unexpected gateway; management blocked.");
  if (!identity) identity = next.ethernet_mac;
  if (next.ethernet_mac !== identity) { key = undefined; access(); throw new Error("Gateway identity changed. Reload and verify its certificate."); }
  state = next; renderStatus(); return next;
}
async function authenticatedHeaders(path, body) {
  if (!key) throw new Error("Load this gateway’s private key first.");
  await readStatus();
  const digest = await hash(body);
  const {nonce} = await api("/api/auth/challenge");
  if (!/^[0-9a-f]{32}$/.test(nonce)) throw new Error("Invalid authentication challenge.");
  const context = `IQDATA-AUTH-V1\nPOST\n${path}\n${nonce}\n${body.byteLength}\n${digest}\n`;
  const signature = hex(await crypto.subtle.sign("HMAC", key, encode(context)));
  return {"Content-Type":"application/octet-stream", "X-IQ-Nonce":nonce, "X-IQ-Auth":signature, "X-SHA256":digest};
}
async function command(path, body = new Uint8Array()) {
  return api(path, {method:"POST", body, headers:await authenticatedHeaders(path, body)});
}
async function transaction(operation) {
  if (busy) return;
  busy = true; access(); notice("");
  try { await operation(); } catch (error) { notice(error.message, true); }
  finally { busy = false; access(); }
}
function element(tag, text, className) {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}
function detail(selector, pairs) {
  $(selector).replaceChildren(...pairs.flatMap(([label, value]) => [element("dt", label), element("dd", String(value))]));
}
function renderStatus() {
  if (diagnosticBoot && diagnosticBoot !== state.boot_id) {
    diagnosticBoot = undefined;$("#meter-traces").replaceChildren();
    $("#diagnostic-state").textContent = "Gateway restarted. Refresh diagnostics for this boot.";
    $("#passive-result").textContent = "Gateway restarted. No passive observation loaded.";
  }
  if (state.recovery) {
    $("#identity").textContent = `${state.ethernet_mac} · Recovery firmware ${state.version}`;
    $("#connection").textContent = "Recovery";$("#connection").className = "badge pending";
    for (const name of ["overview","points","settings"]) { $(`#${name}`).hidden = true;$(`[data-view=${name}]`).hidden = true; }
    $("#maintenance").hidden = false;$("#target").value = "esp32p4";$("#target").disabled = true;
    notice("Recovery mode. Install the full ESP32-P4 application to restore BACnet and the Pico interface.");access();return;
  }
  const s = state, b = s.bacnet, r = b.restart_notification || {};
  $("#meter-error").hidden = !s.pico.last_error;
  $("#meter-error").textContent = s.pico.last_error ? `Last interface error: ${s.pico.last_error}. Inspect meter communication diagnostics in Maintenance.` : "";
  $("#identity").textContent = `${s.config.name} · Device ${s.config.device_instance} · ${s.ethernet_mac}`;
  $("#connection").textContent = "Connected"; $("#connection").className = "badge";
  $("#updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  const conflict = b.instance_status === "conflict";
  const cards = [
    ["Ethernet",s.ethernet.link_up ? "Link up" : "Link down",s.ethernet.ip,s.ethernet.ready ? "ok" : "bad"],
    ["Pico interface",s.pico.qualified ? "Ready" : "Unavailable",s.pico.version || s.pico.last_error || "Waiting for USB",s.pico.qualified ? "ok" : "bad"],
    ["Meter polling",s.config.poll_enabled ? "Enabled" : "Disabled",s.config.poll_enabled ? `Meter address ${s.config.meter_address}` : "Awaiting commissioning",s.config.poll_enabled ? "ok" : "warn"],
    ["BACnet identity",conflict ? "Duplicate detected" : b.instance_status === "checked" ? "No conflict seen" : "Checking",conflict ? `${b.last_conflict.ip}:${b.last_conflict.port}` : `Device ${s.config.device_instance} · UDP ${s.config.bacnet_port}`,conflict ? "bad" : "ok"],
    ["Clock",s.clock.synchronized ? "Synchronized" : "Not synchronized",s.clock.synchronized ? new Date(s.clock.utc_ms).toISOString().replace("T"," ").slice(0,19)+" UTC" : "Events use uptime",s.clock.synchronized ? "ok" : "warn"],
    ["Startup health",s.ota.startup_health.accepted ? "Accepted" : "Checking",`Firmware ${s.version} · ${s.ota.running_slot}`,s.ota.startup_health.accepted ? "ok" : "warn"]
  ];
  $("#health").replaceChildren(...cards.map(([label, value, info, tone]) => {
    const card = element("article", "", "card");
    card.append(element("h3",label),element("strong",value,tone),element("p",info));return card;
  }));
  detail("#gateway-details",[["Firmware",s.version],["Source revision",s.source_revision],["Hostname",`${s.hostname}.local`],["Uptime",`${Math.floor(s.uptime_seconds/3600)}h ${Math.floor(s.uptime_seconds/60)%60}m ${Math.floor(s.uptime_seconds)%60}s`],["Internal free heap",`${Math.round(s.internal_free_heap/1024)} KiB`],["Pico failures",s.pico.failures],["Point quality",`${b.good_points} valid / ${b.fault_points} unavailable`]]);
  detail("#boot-details",[["Restart notice",`${r.sent || 0} sent · ${r.failures || 0} failures`],["Boot timestamp",r.timestamp_source || "Pending"],["Boot UTC",r.boot_utc_ms ? new Date(r.boot_utc_ms).toISOString() : "Unavailable"],["Reset reason",s.reset_reason],["NTP server",s.clock.server || "Disabled"],["Instance checks",b.instance_checks],["COV timeouts",b.cov_timeouts]]);
  if (!loadedConfig) { fillConfig(); loadedConfig = true; }
  access();
}
function renderDiagnostics(snapshot) {
  diagnosticBoot = snapshot.boot_id;
  const probe = snapshot.passive_observation;
  const passive = $("#passive-result");passive.replaceChildren();
  if (probe && probe.state !== "idle") {
    const result = probe.result;
    passive.append(element("p",`Observation #${probe.sequence}: ${probe.state}. ${probe.error || (probe.full_duration ? "Full 500 ms captured." : "Full duration not confirmed.")}`));
    if (result.clock_rises !== null) passive.append(element("p",`Sampled CLK rises: ${result.clock_rises}; RW falls/rises: ${result.rw_falls}/${result.rw_rises}; DATA/INT edges: ${result.data_edges}/${result.int_edges}. Stop ${result.stop_code}; elapsed ${result.elapsed_us} µs.`));
    const raw = document.createElement("details");raw.append(element("summary","Passive observation JSON"),element("pre",JSON.stringify(probe,null,2)));passive.append(raw);
  } else passive.textContent = "No passive observation recorded this boot.";
  const meter = snapshot.meter_transactions, entries = meter?.entries || [];
  $("#diagnostic-state").textContent = meter ? `${entries.length} retained / ${meter.total} reads this boot · ${meter.overwritten} overwritten · Snapshot ${new Date().toLocaleTimeString()}` : "This firmware does not expose meter transaction traces.";
  const levels = pins => Object.entries(pins || {}).map(([pin,value]) => `${pin}: ${value === null ? "unknown" : value ? "HIGH" : "LOW"}`).join(" · ") || "Unavailable";
  const observed = value => value === null || value === undefined ? "unknown" : String(value);
  const rows = entries.map((entry,index) => {
    const panel = document.createElement("details"), host = entry.host_observed, pico = entry.pico_reported;
    panel.open = index === 0;
    panel.append(element("summary",`#${entry.sequence} · ${entry.request_kind} · ${entry.buffer_accepted ? "Buffer accepted" : "Failed"} · ${(entry.uptime_ms/1000).toFixed(1)} s uptime`));
    panel.append(element("p",entry.error || "Transport and data-buffer validation passed.",entry.buffer_accepted ? "ok" : "warn"));
    panel.append(element("p",`Progress: ${entry.progress}. Failed checks: ${entry.failed_checks.join(", ") || (entry.terminal_checks_evaluated ? "none" : "terminal checks not reached")}.`));
    panel.append(element("p",`Requests clocked: ${host.completed_requests}/${host.requests}; data words: ${host.data_words}; completions clocked: ${host.completed_completions}/${host.completions}. Events: host ${host.events}, Pico ${observed(pico.events)}.`));
    panel.append(element("p",`Pico stop: ${pico.stop_name} (${observed(pico.stop_code)}); outputs released: ${observed(pico.released)}; elapsed: ${observed(pico.elapsed_us)} µs.`));
    panel.append(element("p",`Initial pins — ${levels(entry.pin_levels.initial)}`),element("p",`Final pins — ${levels(entry.pin_levels.final)}`));
    const raw = document.createElement("details");raw.append(element("summary",`Full transaction JSON · ${entry.event_trace.events.length} events retained · ${entry.event_trace.omitted} omitted`),element("pre",JSON.stringify(entry,null,2)));
    panel.append(raw);return panel;
  });
  $("#meter-traces").replaceChildren(...(rows.length ? rows : [element("p","No retained meter transactions. Polling may be disabled or Pico not qualified.")]));
}
function renderPoints() {
  const search = $("#search").value.trim().toLowerCase(), quality = $("#quality").value;
  const shown = points.filter(p => (quality === "all" || p.valid === (quality === "valid")) && `${p.name} ${p.description} ${p.type} ${p.instance} ${p.key}`.toLowerCase().includes(search));
  $("#point-count").textContent = `${shown.length} / ${points.length} points`;
  $("#point-rows").replaceChildren(...shown.map(p => {
    const row = document.createElement("tr");
    const label = element("td",p.name);label.append(element("small",p.description));
    const value = p.valid && Number.isFinite(p.value) ? `${p.value.toLocaleString(undefined,{maximumFractionDigits:4})} ${units[p.units] ?? `unit ${p.units}`}` : "Unavailable";
    const age = p.age_seconds === null ? "Never sampled" : `${p.age_seconds.toFixed(1)} s`;
    row.append(label,element("td",`${p.type === "analog-input" ? "AI" : "BI"} ${p.instance}`),element("td",value),element("td",p.valid ? "Valid" : "Communication fault",p.valid ? "ok" : "warn"),element("td",age));
    row.title = `${p.time_context}. Maximum age: ${p.max_age_seconds ? p.max_age_seconds + " s" : "not age limited"}`;
    return row;
  }));
}
async function refresh() {
  if (busy || refreshing) return;
  refreshing = true;
  try { await readStatus(); points = await api("/api/points"); renderPoints(); }
  catch (error) {
    $("#connection").textContent = "Unavailable";$("#connection").className = "badge offline";
    $("#updated").textContent = "Connection lost; displayed system details may be stale";
    points = [];renderPoints();notice(`Gateway unavailable. Retrying automatically. ${error.message}`,true);
  } finally { refreshing = false; }
}
function fillConfig() {
  const form = $("#config-form");
  for (const [name, value] of Object.entries(state.config)) {
    if (!form.elements[name]) continue;
    if (typeof value === "boolean") form.elements[name].checked = value;
    else form.elements[name].value = value;
  }
  networkFields();
}
function networkFields() {
  const form = $("#config-form");
  for (const name of ["ip","mask","gateway"]) { form.elements[name].disabled = form.elements.dhcp.checked; form.elements[name].required = !form.elements.dhcp.checked; }
}
function collectConfig() {
  const form = $("#config-form"), next = {};
  for (const name of ["device_instance","bacnet_port","meter_address"]) next[name] = Number(form.elements[name].value);
  for (const name of ["name","ip","mask","gateway"]) next[name] = form.elements[name].value.trim();
  for (const name of ["dhcp","poll_enabled"]) next[name] = form.elements[name].checked;
  if (next.device_instance === 75151 || next.ip === "192.168.75.151") throw new Error("Production Device 75151 and IP 192.168.75.151 are protected.");
  if (!/^[\x20-\x7e]{1,63}$/.test(next.name)) throw new Error("Device name must contain 1–63 printable ASCII characters.");
  return next;
}
async function waitForBoot(before, match) {
  const until = Date.now()+90000;
  await pause(3000);
  while (Date.now()<until) {
    try {
      const after = await readStatus();
      if (after.boot_id && after.boot_id !== before.boot_id && after.ota.startup_health.accepted && match(after)) return after;
    } catch (_) { /* Expected during reboot. Never report success on a lost connection. */ }
    await pause(2000);
  }
  throw new Error(`Restart not verified. Reconnect at https://${before.hostname}.local/ and inspect startup health or rollback.`);
}
function upload(path, body, headers) {
  return new Promise((resolve,reject) => {
    const request = new XMLHttpRequest();request.open("POST",path);request.timeout = 150000;
    for (const [name,value] of Object.entries(headers)) request.setRequestHeader(name,value);
    request.upload.onprogress = event => { if (event.lengthComputable) $("#progress").value = event.loaded/event.total*100; };
    request.onload = () => {
      if (request.status !== 200) { reject(new Error(`Update rejected: HTTP ${request.status} ${request.responseText.slice(0,180)}`));return; }
      try { resolve(JSON.parse(request.responseText)); } catch (_) { reject(new Error("Invalid update response; activation not verified.")); }
    };
    request.onerror = request.ontimeout = () => reject(new Error("Connection lost during update. Inspect gateway status before retrying."));
    request.send(body);
  });
}
document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".view").forEach(section => section.hidden = section.id !== button.dataset.view);
  document.querySelectorAll("[data-view]").forEach(tab => tab.removeAttribute("aria-current"));button.setAttribute("aria-current","page");
}));
$("#search").addEventListener("input",renderPoints);$("#quality").addEventListener("change",renderPoints);
$("#reload-config").addEventListener("click",() => transaction(async () => { await readStatus();fillConfig();notice("Saved settings loaded."); }));
$("#config-form").elements.dhcp.addEventListener("change",networkFields);
$("#key-file").addEventListener("change",() => transaction(async () => {
  key = undefined;
  const file = $("#key-file").files[0];$("#key-file").value = "";
  if (!file) return;
  if (file.size > 256) throw new Error("Expected a 64-character private key file.");
  const text = (await file.text()).trim();
  if (!/^[0-9a-f]{64}$/.test(text)) throw new Error("Expected 64 lowercase hexadecimal characters.");
  const raw = bytes(text);
  key = await crypto.subtle.importKey("raw",raw,{name:"HMAC",hash:"SHA-256"},false,["sign"]);raw.fill(0);
  try { const result = await command("/api/auth/check");if (!result.authenticated) throw new Error("Authentication failed.");notice("Management unlocked for this device."); }
  catch (error) { key = undefined;throw error; }
}));
$("#forget").addEventListener("click",() => { key = undefined;access();notice("Management locked. Key removed from tab memory."); });
window.addEventListener("pagehide",() => { key = undefined; });
$("#config-form").addEventListener("submit",event => {
  event.preventDefault();if (!key || busy) return;
  try { proposedConfig = collectConfig();$("#config-preview").textContent = JSON.stringify(proposedConfig,null,2);$("#config-review").showModal(); }
  catch (error) { notice(error.message,true); }
});
$("#cancel-config").addEventListener("click",() => $("#config-review").close());
$("#apply-config").addEventListener("click",() => transaction(async () => {
  $("#config-review").close();const before = await readStatus(), desired = {...proposedConfig};
  const reply = await command("/api/config",encode(JSON.stringify(desired)));
  if (!reply.saved) throw new Error("Configuration save not confirmed.");
  notice(`Settings saved; verifying restart. Hostname: ${before.hostname}.local`);
  await waitForBoot(before,after => Object.keys(desired).every(name => after.config[name] === desired[name]));
  fillConfig();notice("Settings saved. Restart and configuration verified.");
}));
$("#reboot").addEventListener("click",() => transaction(async () => {
  if (!confirm("Restart this gateway now?")) return;
  const before = await readStatus();await command("/api/reboot");notice("Restart requested; waiting for healthy gateway.");
  await waitForBoot(before,() => true);notice("Gateway restart verified.");
}));
$("#download").addEventListener("click",() => transaction(async () => {
  const snapshot = await command("/api/diagnostics");
  renderDiagnostics(snapshot);
  const blob = new Blob([JSON.stringify(snapshot,null,2)+"\n"],{type:"application/json"});
  const url = URL.createObjectURL(blob), link = document.createElement("a");link.href = url;
  link.download = `iqdata-diagnostics-${identity.replaceAll(":","")}-${Date.now()}.json`;link.click();setTimeout(() => URL.revokeObjectURL(url),1000);
  notice("Diagnostic snapshot downloaded.");
}));
$("#refresh-diagnostics").addEventListener("click",() => transaction(async () => {
  renderDiagnostics(await command("/api/diagnostics"));
  notice("Diagnostic snapshot refreshed. No meter read was started.");
}));
$("#observe-pins").addEventListener("click",() => transaction(async () => {
  const queued = await command("/api/pico/observe"), deadline = Date.now()+20000;
  notice("Sampling inputs for 500 ms; waiting for Pico report.");
  while (Date.now()<deadline) {
    const snapshot = await command("/api/diagnostics"), probe = snapshot.passive_observation;
    if (snapshot.boot_id !== queued.boot_id || probe?.sequence !== queued.sequence) throw new Error("Gateway restarted or observation replaced. Refresh diagnostics.");
    renderDiagnostics(snapshot);
    if (probe.state === "failed") throw new Error(probe.error);
    if (probe.state === "complete") { notice("Passive observation captured. Meter readings remain unvalidated.");return; }
    await pause(500);
  }
  throw new Error("Passive observation timed out. Refresh diagnostics.");
}));
$("#update-form").addEventListener("submit",event => {
  event.preventDefault();transaction(async () => {
    const file = $("#image-file").files[0], manifestFile = $("#signature-file").files[0], target = $("#target").value;
    if (!file || !manifestFile || manifestFile.size > 4096) throw new Error("Select firmware and its matching signature manifest.");
    const before = await readStatus();
    const maximum = target === "esp32p4" ? before.ota.slot_bytes : 1048576;
    if (file.size < 512 || file.size > maximum) throw new Error("Firmware size is outside this processor’s update limits.");
    const data = new Uint8Array(await file.arrayBuffer()), signed = JSON.parse(await manifestFile.text());
    if (signed.schema !== 1 || signed.target !== target || signed.bytes !== data.length || signed.sha256 !== await hash(data) || !/^(?:[0-9a-f]{2}){64,72}$/.test(signed.signature)) throw new Error("Signature manifest does not match the selected firmware and processor.");
    if (target === "esp32p4" && (data[0] !== 0xe9 || data[12] !== 0x12 || data[13] !== 0 || new TextDecoder().decode(data.slice(80,112)).split("\0")[0] !== "iqdata_p4_gateway")) throw new Error("Expected an IQData ESP32-P4 application image.");
    if (target === "pico2" && (data.length < 1024 || data.length%512 || hex(data.slice(0,8)) !== "5546320a57515d9e")) throw new Error("Expected a Pico 2 UF2 image.");
    const path = target === "esp32p4" ? "/api/firmware" : "/api/pico/firmware";
    const headers = {...await authenticatedHeaders(path,data),"X-Image-Signature":signed.signature};
    $("#progress").hidden = false;$("#progress").value = 0;$("#update-result").textContent = "Uploading and verifying signed firmware…";
    try {
      const result = await upload(path,data,headers);
      if (target === "esp32p4") {
        if (!result.verified) throw new Error("Image upload not confirmed.");
        $("#update-result").textContent = "Upload verified. Waiting for exact image and startup health…";
        await waitForBoot(before,after => after.elf_sha256 === hex(data.slice(176,208)) && after.ota.running_slot !== before.ota.running_slot && after.ota.image_state === 2);
        $("#update-result").textContent = "ESP32-P4 update verified. Exact image is running and healthy.";
      } else {
        if (!result.pico_boot_verified || result.board !== "pico2" || result.upload_sha256 !== signed.sha256) throw new Error("Expected Pico runtime return was not verified.");
        await readStatus();if (!state.pico.qualified) throw new Error("Pico returned but runtime qualification is not ready.");
        $("#update-result").textContent = `Pico 2 update verified. Firmware ${result.version} returned and qualified.`;
      }
    } catch (error) { $("#update-result").textContent = `Update not verified: ${error.message}`;throw error; }
    finally { $("#image-file").value = "";$("#signature-file").value = ""; }
  });
});
access();refresh();setInterval(refresh,5000);
