"use strict";
// Main window: this computer's code/password, connect to a partner, incoming sessions.
(() => {
  const { el, toast, formatId, formatDate } = TL;
  const $ = (id) => document.getElementById(id);
  let ws = null;
  let state = null;
  let passwordVisible = false;

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  function connectSocket() {
    ws = new WebSocket(TL.wsUrl("/ws/main"));
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "state") render(msg);
      else if (msg.type === "host_event") onHostEvent(msg.event);
      else if (msg.type === "error") toast(msg.message, "error");
      else if (msg.type === "notice") toast(msg.message, "ok");
    };
    ws.onclose = () => {
      $("status-dot").className = "dot offline";
      $("status-text").textContent = "TeamLooker no está en ejecución";
      setTimeout(connectSocket, 2000);
    };
  }

  const STATUS = {
    online: ["online", (h) => h.mode === "direct" ? "Esperando conexiones (directo)" : "Listo para conectar"],
    connecting: ["", () => "Preparando…"],
    offline: ["offline", () => "Sin conexión"],
  };

  function formatCode(code) {
    return code ? code.match(/.{1,5}/g).join("-") : "·····";
  }

  function render(s) {
    state = s;
    $("version").textContent = s.version;
    const direct = s.mode === "direct";
    $("mode-badge").textContent = direct ? "Directo" : "Servidor";
    $("code-block").hidden = !direct;
    $("id-block").hidden = direct;
    $("share-hint").textContent = direct
      ? "Comparte el código (o una dirección) y la contraseña con quien vaya a controlar este equipo."
      : "Comparte tu ID y la contraseña con quien vaya a conectarse.";
    $("connect-hint").textContent = direct
      ? "Escribe el código de conexión (o IP:puerto) del equipo remoto."
      : "Escribe el ID de 9 dígitos del equipo remoto.";
    $("target-label").textContent = direct ? "Código del asociado" : "ID del asociado";
    $("target").placeholder = direct ? "XXXXX-XXXXX" : "123 456 789";

    const host = s.host;
    if (host) {
      const [cls, textFn] = STATUS[host.status] || STATUS.offline;
      $("status-dot").className = `dot ${cls}`;
      $("status-text").textContent = host.status === "offline" && host.status_detail
        ? `Sin conexión: ${host.status_detail}` : textFn(host);
      $("status").title = host.status_detail || "";
      renderPassword(host.password);
      if (direct) renderDirect(host.direct);
      else $("my-id").textContent = formatId(host.id);
      for (const box of document.querySelectorAll("[data-perm]")) box.checked = !!host.permissions[box.dataset.perm];
      $("unattended-note").textContent = host.permanent_password
        ? "✓ Acceso desatendido activado: también se acepta la contraseña permanente."
        : "";
      renderSessions(host.sessions);
    }
    // settings
    for (const r of document.querySelectorAll('input[name="conn-mode"]')) r.checked = r.value === s.mode;
    $("direct-settings").hidden = !direct;
    $("relay-settings").hidden = direct;
    if (document.activeElement !== $("direct-port")) $("direct-port").value = s.direct_port;
    if (document.activeElement !== $("relay")) $("relay").value = s.relay;
    $("upnp").checked = s.enable_upnp;
    if (document.activeElement !== $("device-name")) $("device-name").value = s.device_name;
    $("permanent-clear").disabled = !(host && host.permanent_password);
    renderRecent(s.recent, direct);
  }

  function renderPassword(pw) {
    $("my-password").textContent = passwordVisible ? pw : "•".repeat(pw ? pw.length : 6);
    $("my-password").dataset.pw = pw || "";
  }

  function renderDirect(d) {
    $("my-code").textContent = formatCode(d.code);
    $("my-code").dataset.code = d.code || "";
    const list = $("address-list");
    list.replaceChildren();
    if (d.public_ip) {
      list.append(addressItem(`${d.public_ip}:${d.public_port}`, "Pública", true, d.mapping_method));
    }
    for (const ip of d.local_ips || []) {
      list.append(addressItem(`${ip}:${d.port}`, "Red local", false));
    }
    if (!d.public_ip && (d.local_ips || []).length) {
      list.append(el("div", { class: "small muted", style: "margin-top:2px" },
        "Entre redes distintas: activa UPnP en Opciones o abre el puerto en tu router."));
    }
  }

  function addressItem(addr, tag, pub, method) {
    return el("div", { class: "address-item" },
      el("span", { class: `tag${pub ? " pub" : ""}` }, tag),
      el("code", {}, addr),
      method ? el("span", { class: "small muted" }, `(${method})`) : null,
      el("button", { class: "icon", title: "Copiar", onclick: () => TL.copy(addr) }, "⧉"));
  }

  function renderRecent(recent, direct) {
    const box = $("recent");
    box.replaceChildren();
    if (!recent.length) {
      box.append(el("div", { class: "empty" }, "Todavía no te has conectado a ningún equipo."));
      return;
    }
    for (const r of recent) {
      const label = direct ? (r.id.length === 10 ? formatCode(r.id) : r.id) : formatId(r.id);
      box.append(el("div", { class: "list-item" },
        el("div", { class: "grow" },
          el("div", { class: "title" }, label),
          el("div", { class: "small muted" }, [r.name, formatDate(r.last)].filter(Boolean).join(" · "))),
        el("button", { class: "primary", onclick: () => openViewer(r.id, "control") }, "Conectar"),
        el("button", { onclick: () => openViewer(r.id, "files"), title: "Transferencia de archivos" }, "Archivos"),
        el("button", { class: "icon danger", title: "Quitar", onclick: () => send({ type: "remove_recent", id: r.id }) }, "✕")));
    }
  }

  function renderSessions(sessions) {
    const box = $("sessions");
    const ids = new Set(sessions.map((s) => s.id));
    for (const node of [...box.querySelectorAll(".session")]) if (!ids.has(node.dataset.id)) node.remove();
    box.querySelector(".empty")?.remove();
    if (!sessions.length) { box.append(el("div", { class: "empty" }, "No hay nadie conectado.")); return; }
    for (const s of sessions) {
      if (box.querySelector(`.session[data-id="${s.id}"]`)) continue;
      const node = $("session-tpl").content.firstElementChild.cloneNode(true);
      node.dataset.id = s.id;
      node.querySelector(".title").textContent = `${s.viewer} está conectado`;
      node.querySelector(".meta").textContent =
        `Desde ${s.peer || "?"} · ${formatDate(s.started)} · contraseña ${s.auth === "permanent" ? "permanente" : "de sesión"}`;
      node.querySelector(".disconnect").onclick = () => send({ type: "host_disconnect", session: s.id });
      node.querySelector(".chat-form").onsubmit = (ev) => {
        ev.preventDefault();
        const input = ev.target.querySelector("input");
        const text = input.value.trim();
        if (!text) return;
        send({ type: "host_chat", session: s.id, text });
        input.value = "";
      };
      box.append(node);
    }
    for (const s of sessions) renderChat(s.id, (state.chats || {})[s.id] || []);
  }

  function renderChat(sessionId, messages) {
    const node = document.querySelector(`.session[data-id="${sessionId}"] .chat-log`);
    if (!node || node.childElementCount === messages.length) return;
    node.replaceChildren(...messages.map((m) =>
      el("div", { class: `chat-msg${m.me ? " me" : ""}` }, el("div", { class: "who" }, m.who), m.text)));
    node.scrollTop = node.scrollHeight;
  }

  function onHostEvent(ev) {
    switch (ev.type) {
      case "session_started":
        toast(`${ev.session.viewer} se ha conectado a este equipo`, "info", 8000);
        notify("TeamLooker", `${ev.session.viewer} se ha conectado`);
        break;
      case "session_ended": toast("Sesión entrante finalizada"); break;
      case "chat": notify(`Mensaje de ${ev.sender}`, ev.text); break;
      case "auth_failed":
        toast(ev.locked_for
          ? `Contraseña incorrecta. Acceso bloqueado ${ev.locked_for} s.`
          : "Intento de conexión con contraseña incorrecta.", "error", 8000);
        break;
      case "file_received": toast(`Archivo recibido: ${ev.path}`, "ok", 8000); break;
    }
  }

  function notify(title, body) {
    if (!("Notification" in window) || document.hasFocus()) return;
    if (Notification.permission === "granted") new Notification(title, { body });
  }

  function openViewer(target, mode) {
    const t = String(target).trim();
    if (!t) return toast("Escribe un código o dirección.", "error");
    if (state?.mode === "relay" && t.replace(/\D/g, "").length !== 9)
      return toast("El ID debe tener 9 dígitos.", "error");
    const url = `/viewer?token=${encodeURIComponent(TL.token)}&target=${encodeURIComponent(t)}&mode=${mode}`;
    const win = window.open(url, `tl-${Date.now()}`, "popup,width=1280,height=840");
    if (!win) location.href = url;
  }

  // --- wiring --------------------------------------------------------------
  $("connect-form").onsubmit = (ev) => {
    ev.preventDefault();
    openViewer($("target").value, new FormData(ev.target).get("mode"));
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  };
  $("target").addEventListener("input", (ev) => {
    if (state?.mode === "relay") {
      ev.target.value = formatId(ev.target.value.replace(/\D/g, "").slice(0, 9));
    }
  });
  $("copy-code").onclick = () => state?.host && TL.copy($("my-code").dataset.code || "");
  $("copy-id").onclick = () => state?.host && TL.copy(state.host.id);
  $("toggle-password").onclick = () => { passwordVisible = !passwordVisible; if (state?.host) renderPassword(state.host.password); };
  $("new-password").onclick = () => send({ type: "regenerate_password" });
  for (const r of document.querySelectorAll('input[name="conn-mode"]')) {
    r.onchange = () => send({ type: "set_mode", mode: r.value });
  }
  $("port-form").onsubmit = (ev) => { ev.preventDefault(); send({ type: "set_direct_port", port: Number($("direct-port").value) }); };
  $("upnp").onchange = () => send({ type: "set_upnp", enabled: $("upnp").checked });
  $("relay-form").onsubmit = (ev) => { ev.preventDefault(); send({ type: "set_relay", relay: $("relay").value.trim() }); toast("Servidor guardado", "ok"); };
  $("name-form").onsubmit = (ev) => { ev.preventDefault(); send({ type: "set_device_name", name: $("device-name").value.trim() }); toast("Nombre guardado", "ok"); };
  $("permanent-form").onsubmit = (ev) => { ev.preventDefault(); send({ type: "set_permanent_password", password: $("permanent").value }); $("permanent").value = ""; };
  $("permanent-clear").onclick = () => send({ type: "set_permanent_password", password: "" });
  for (const box of document.querySelectorAll("[data-perm]")) {
    box.onchange = () => send({ type: "set_permissions", permissions: { [box.dataset.perm]: box.checked } });
  }
  connectSocket();
})();
