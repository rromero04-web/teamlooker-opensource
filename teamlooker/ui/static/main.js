"use strict";
// Main window: this computer's ID/password, connect to a partner, incoming sessions.
(() => {
  const { el, toast, formatId, formatDate } = TL;
  const $ = (id) => document.getElementById(id);
  let ws = null;
  let state = null;
  let permanentConfigured = false;

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
      $("status-text").textContent = "La aplicación TeamLooker no está en ejecución";
      setTimeout(connectSocket, 2000);
    };
  }

  const STATUS = {
    online: ["online", "Listo para conectar (conexión segura)"],
    connecting: ["", "Conectando con el servidor…"],
    offline: ["offline", "Sin conexión con el servidor"],
  };

  function render(s) {
    state = s;
    $("version").textContent = s.version;
    const host = s.host;
    if (host) {
      $("my-id").textContent = formatId(host.id);
      $("my-password").textContent = host.password;
      const [cls, text] = STATUS[host.status] || STATUS.offline;
      $("status-dot").className = `dot ${cls}`;
      $("status-text").textContent = host.status === "offline" && host.status_detail
        ? `${text}: ${host.status_detail}` : text;
      $("status-text").title = host.relay;
      permanentConfigured = host.permanent_password;
      $("unattended-note").textContent = host.permanent_password
        ? "Acceso desatendido activado: también se acepta la contraseña permanente."
        : "";
      for (const box of document.querySelectorAll("[data-perm]")) {
        box.checked = !!host.permissions[box.dataset.perm];
      }
      renderSessions(host.sessions);
    }
    if (document.activeElement !== $("relay")) $("relay").value = s.relay;
    if (document.activeElement !== $("device-name")) $("device-name").value = s.device_name;
    $("permanent-clear").disabled = !permanentConfigured;
    renderRecent(s.recent);
  }

  function renderRecent(recent) {
    const box = $("recent");
    box.replaceChildren();
    if (!recent.length) {
      box.append(el("div", { class: "empty" }, "Todavía no te has conectado a ningún equipo."));
      return;
    }
    for (const r of recent) {
      box.append(el("div", { class: "list-item" },
        el("div", { class: "grow" },
          el("div", { class: "title" }, formatId(r.id)),
          el("div", { class: "small muted" }, [r.name, formatDate(r.last)].filter(Boolean).join(" · "))),
        el("button", { class: "primary", onclick: () => openViewer(r.id, "control") }, "Conectar"),
        el("button", { onclick: () => openViewer(r.id, "files"), title: "Transferencia de archivos" }, "Archivos"),
        el("button", { class: "icon danger", title: "Quitar de la lista",
          onclick: () => send({ type: "remove_recent", id: r.id }) }, "✕")));
    }
  }

  function renderSessions(sessions) {
    const box = $("sessions");
    const ids = new Set(sessions.map((s) => s.id));
    for (const node of [...box.querySelectorAll(".session")]) {
      if (!ids.has(node.dataset.id)) node.remove();
    }
    box.querySelector(".empty")?.remove();
    if (!sessions.length) {
      box.append(el("div", { class: "empty" }, "No hay nadie conectado."));
      return;
    }
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
    for (const s of sessions) renderChat(s.id, state.chats[s.id] || []);
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
        notify("TeamLooker", `${ev.session.viewer} se ha conectado a este equipo`);
        break;
      case "session_ended":
        toast("Sesión entrante finalizada");
        break;
      case "chat":
        notify(`Mensaje de ${ev.sender}`, ev.text);
        break;
      case "auth_failed":
        toast(ev.locked_for
          ? `Intento de conexión con contraseña incorrecta. Acceso bloqueado ${ev.locked_for} s.`
          : "Intento de conexión con contraseña incorrecta.", "error", 8000);
        break;
      case "file_received":
        toast(`Archivo recibido: ${ev.path}`, "ok", 8000);
        break;
    }
  }

  function notify(title, body) {
    if (!("Notification" in window) || document.hasFocus()) return;
    if (Notification.permission === "granted") new Notification(title, { body });
  }

  function openViewer(partnerId, mode) {
    const id = String(partnerId).replace(/\D/g, "");
    if (id.length !== 9) {
      toast("El ID del asociado debe tener 9 dígitos.", "error");
      return;
    }
    if (state?.host && id === state.host.id) {
      toast("Ese es el ID de este mismo equipo.", "error");
      return;
    }
    const url = `/viewer?token=${encodeURIComponent(TL.token)}&partner=${id}&mode=${mode}`;
    const win = window.open(url, `tl-${id}-${Date.now()}`, "popup,width=1280,height=820");
    if (!win) location.href = url;
  }

  // --- wiring --------------------------------------------------------------
  $("connect-form").onsubmit = (ev) => {
    ev.preventDefault();
    const mode = new FormData(ev.target).get("mode");
    openViewer($("partner-id").value, mode);
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  };
  $("partner-id").addEventListener("input", (ev) => {
    const digits = ev.target.value.replace(/\D/g, "").slice(0, 9);
    ev.target.value = formatId(digits);
  });
  $("copy-id").onclick = () => state?.host && TL.copy(state.host.id);
  $("new-password").onclick = () => send({ type: "regenerate_password" });
  $("relay-form").onsubmit = (ev) => {
    ev.preventDefault();
    send({ type: "set_relay", relay: $("relay").value.trim() });
    toast("Servidor guardado. Reconectando…", "ok");
  };
  $("name-form").onsubmit = (ev) => {
    ev.preventDefault();
    send({ type: "set_device_name", name: $("device-name").value.trim() });
    toast("Nombre guardado", "ok");
  };
  $("permanent-form").onsubmit = (ev) => {
    ev.preventDefault();
    send({ type: "set_permanent_password", password: $("permanent").value });
    $("permanent").value = "";
  };
  $("permanent-clear").onclick = () => send({ type: "set_permanent_password", password: "" });
  for (const box of document.querySelectorAll("[data-perm]")) {
    box.onchange = () => send({ type: "set_permissions", permissions: { [box.dataset.perm]: box.checked } });
  }
  connectSocket();
})();
