"use strict";
// Remote session window: screen, input, chat, clipboard and file transfer.
(() => {
  const { el, toast, formatId, formatSize } = TL;
  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const partner = (params.get("partner") || "").replace(/\D/g, "");
  const mode = params.get("mode") === "files" ? "files" : "control";

  const canvas = $("screen");
  const ctx = canvas.getContext("2d");
  const wrap = $("screen-wrap");
  const cursorEl = $("remote-cursor");

  const QUALITY = {
    speed: { quality: 40, fps: 15 },
    balanced: { quality: 65, fps: 20 },
    quality: { quality: 85, fps: 25 },
  };

  let ws = null;
  let connected = false;
  let lastPassword = "";
  let info = null;
  let viewOnly = mode === "files";
  let frameChain = Promise.resolve();
  let frames = 0;
  let bytes = 0;
  let lastCursor = null;
  const pressed = new Map(); // code -> key
  const pendingFs = new Map();
  let fsSeq = 0;
  const transfers = new Map();

  $("auth-text").textContent =
    `Introduce la contraseña que aparece en el equipo remoto (ID ${formatId(partner)}).`;
  document.title = `TeamLooker · ${formatId(partner)}`;

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  // --- connection -------------------------------------------------------------
  function attempt(password) {
    lastPassword = password;
    $("auth-error").textContent = "";
    $("auth-submit").disabled = true;
    $("auth-submit").replaceChildren(el("span", { class: "spinner" }), " Conectando…");
    ws = new WebSocket(TL.wsUrl("/ws/viewer"));
    ws.binaryType = "arraybuffer";
    ws.onopen = () => ws.send(JSON.stringify({ type: "connect", partner, password }));
    ws.onmessage = (ev) => {
      if (ev.data instanceof ArrayBuffer) {
        bytes += ev.data.byteLength;
        const buf = ev.data;
        frameChain = frameChain.then(() => drawFrame(buf)).catch((e) => console.error(e));
        return;
      }
      handle(JSON.parse(ev.data));
    };
    ws.onclose = () => {
      if (connected) sessionEnded();
      else resetAuthButton();
    };
  }

  function resetAuthButton() {
    $("auth-submit").disabled = false;
    $("auth-submit").textContent = "Iniciar sesión";
  }

  function handle(msg) {
    switch (msg.type) {
      case "connect_progress":
        $("auth-error").textContent = "";
        break;
      case "connect_error":
        $("auth-error").textContent = msg.message;
        $("auth-overlay").classList.remove("hidden");
        $("ended-overlay").classList.add("hidden");
        $("password").select();
        resetAuthButton();
        break;
      case "connected":
        onConnected(msg);
        break;
      case "cursor":
        moveCursor(msg.cursor);
        break;
      case "chat":
        addChat(info?.hostname || "Remoto", msg.text, false);
        if ($("chat-panel").classList.contains("hidden")) {
          toast(`${info?.hostname || "Remoto"}: ${msg.text}`);
          $("chat-btn").classList.add("active");
        }
        break;
      case "clipboard":
        break; // applied to the local clipboard by the backend
      case "screen_error":
        $("screen-msg").textContent = `${msg.message}. El chat y la transferencia de archivos siguen disponibles.`;
        $("screen-msg").classList.remove("hidden");
        break;
      case "bye":
        $("ended-text").textContent = msg.reason || "El equipo remoto cerró la sesión.";
        break;
      case "disconnected":
        break;
      case "fs_reply": {
        const resolve = pendingFs.get(msg.ui_req);
        if (resolve) { pendingFs.delete(msg.ui_req); resolve(msg); }
        break;
      }
      case "transfer_started":
        addTransfer(msg.tid, msg.direction, msg.name);
        break;
      case "transfer_progress":
        updateTransfer(msg.tid, msg.done, msg.total, msg.name, msg.direction);
        break;
      case "transfer_done":
        finishTransfer(msg);
        break;
    }
  }

  function onConnected(msg) {
    connected = true;
    info = msg.info;
    $("auth-overlay").classList.add("hidden");
    $("ended-overlay").classList.add("hidden");
    $("password").value = "";
    $("partner-label").textContent = `${info.hostname} · ${formatId(partner)}`;
    $("partner-label").title = `${info.os} · TeamLooker ${info.version}`;
    document.title = `${info.hostname} – TeamLooker`;
    const monitorSel = $("monitor");
    monitorSel.replaceChildren(...info.monitors.map((m, i) =>
      el("option", { value: i }, `Monitor ${i + 1} (${m.width}×${m.height})`)));
    monitorSel.classList.toggle("hidden", info.monitors.length < 2);
    const perms = info.permissions || {};
    if (!perms.control || !info.input_available) {
      viewOnly = true;
      $("view-only").disabled = true;
      toast(info.input_available ? "El equipo remoto solo permite ver la pantalla."
        : "El equipo remoto no admite control de entrada: solo se puede ver.", "info", 7000);
    }
    $("view-only").classList.toggle("active", viewOnly);
    $("files-btn").disabled = !perms.files;
    $("clip-sync").disabled = !perms.clipboard || !msg.clipboard_available;
    $("clip-sync").classList.toggle("active", !$("clip-sync").disabled);
    applySettings();
    if (mode === "files") openFiles();
    else canvas.focus();
  }

  function sessionEnded() {
    connected = false;
    releaseAll();
    $("ended-overlay").classList.remove("hidden");
    $("files-overlay").classList.add("hidden");
  }

  // --- screen ---------------------------------------------------------------
  async function drawFrame(buf) {
    const view = new DataView(buf);
    const headerLen = view.getUint32(0);
    const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, headerLen)));
    if (canvas.width !== meta.width || canvas.height !== meta.height) {
      canvas.width = meta.width;
      canvas.height = meta.height;
      layout();
    }
    let offset = 4 + headerLen;
    const parts = meta.rects.map(([x, y, w, h, len]) => {
      const blob = new Blob([new Uint8Array(buf, offset, len)], { type: "image/jpeg" });
      offset += len;
      return { x, y, blob };
    });
    const bitmaps = await Promise.all(parts.map((p) => createImageBitmap(p.blob)));
    bitmaps.forEach((bmp, i) => { ctx.drawImage(bmp, parts[i].x, parts[i].y); bmp.close(); });
    moveCursor(meta.cursor);
    frames++;
    // In file-transfer mode the stream pauses (no ack) while the file manager is open.
    const pausedForFiles = mode === "files" && !$("files-overlay").classList.contains("hidden");
    if (!pausedForFiles) send({ type: "frame_ack" });
  }

  function moveCursor(pos) {
    lastCursor = pos;
    const show = pos && $("show-cursor").checked;
    cursorEl.classList.toggle("hidden", !show);
    if (show) {
      cursorEl.style.left = `${pos[0] * 100}%`;
      cursorEl.style.top = `${pos[1] * 100}%`;
    }
  }

  function layout() {
    const fit = $("fit").checked;
    wrap.classList.toggle("fit", fit);
    if (!fit) {
      canvas.style.width = `${canvas.width}px`;
      canvas.style.height = `${canvas.height}px`;
      return;
    }
    const scale = Math.min(wrap.clientWidth / canvas.width, wrap.clientHeight / canvas.height);
    canvas.style.width = `${Math.floor(canvas.width * scale)}px`;
    canvas.style.height = `${Math.floor(canvas.height * scale)}px`;
  }
  new ResizeObserver(layout).observe(wrap);

  setInterval(() => {
    $("stats").textContent = connected ? `${frames} fps · ${formatSize(bytes)}/s` : "";
    frames = 0;
    bytes = 0;
  }, 1000);

  function applySettings() {
    const preset = QUALITY[$("quality").value];
    send({ type: "settings", quality: preset.quality, fps: preset.fps,
      scale: Number($("scale").value), grayscale: $("grayscale").checked });
  }

  // --- input ------------------------------------------------------------------
  function canControl() {
    return connected && !viewOnly && $("files-overlay").classList.contains("hidden");
  }

  function position(ev) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height)),
    };
  }

  let moveQueued = null;
  canvas.addEventListener("mousemove", (ev) => {
    if (!canControl()) return;
    const first = moveQueued === null;
    moveQueued = position(ev);
    if (first) requestAnimationFrame(() => {
      send({ type: "mouse_move", ...moveQueued });
      moveQueued = null;
    });
  });
  canvas.addEventListener("mousedown", (ev) => {
    canvas.focus();
    if (!canControl()) return;
    ev.preventDefault();
    send({ type: "mouse_button", button: ev.button, down: true, ...position(ev) });
  });
  window.addEventListener("mouseup", (ev) => {
    if (!canControl() || ev.target !== canvas) return;
    send({ type: "mouse_button", button: ev.button, down: false, ...position(ev) });
  });
  canvas.addEventListener("contextmenu", (ev) => ev.preventDefault());

  let wheelAcc = 0;
  canvas.addEventListener("wheel", (ev) => {
    if (!canControl()) return;
    ev.preventDefault();
    const unit = ev.deltaMode === 1 ? 3 : ev.deltaMode === 2 ? 1 : 100;
    wheelAcc += ev.deltaY / unit;
    const steps = Math.trunc(wheelAcc);
    if (steps) {
      wheelAcc -= steps;
      send({ type: "mouse_scroll", dx: 0, dy: -steps });
    }
  }, { passive: false });

  function typingInUi(ev) {
    const t = ev.target;
    return t instanceof HTMLInputElement || t instanceof HTMLTextAreaElement || t instanceof HTMLSelectElement;
  }

  window.addEventListener("keydown", (ev) => {
    if (!canControl() || typingInUi(ev)) return;
    ev.preventDefault();
    pressed.set(ev.code, ev.key);
    send({ type: "key", key: ev.key, code: ev.code, down: true });
  });
  window.addEventListener("keyup", (ev) => {
    if (!canControl() || typingInUi(ev)) return;
    ev.preventDefault();
    pressed.delete(ev.code);
    send({ type: "key", key: ev.key, code: ev.code, down: false });
  });

  function releaseAll() {
    for (const [code, key] of pressed) send({ type: "key", key, code, down: false });
    pressed.clear();
  }
  window.addEventListener("blur", releaseAll);

  // --- toolbar ------------------------------------------------------------------
  function toggleMenu(button, menu) {
    button.addEventListener("click", (ev) => {
      ev.stopPropagation();
      for (const m of document.querySelectorAll(".menu-panel")) if (m !== menu) m.classList.add("hidden");
      menu.classList.toggle("hidden");
    });
    menu.addEventListener("click", (ev) => ev.stopPropagation());
  }
  toggleMenu($("actions-btn"), $("actions-menu"));
  toggleMenu($("view-btn"), $("view-menu"));
  document.addEventListener("click", () => {
    for (const m of document.querySelectorAll(".menu-panel")) m.classList.add("hidden");
  });

  for (const btn of document.querySelectorAll("[data-combo]")) {
    btn.onclick = () => {
      if (!canControl()) return toast("El control está desactivado (modo solo ver).", "error");
      send({ type: "combo", keys: btn.dataset.combo.split(",") });
      $("actions-menu").classList.add("hidden");
      canvas.focus();
    };
  }
  $("type-clipboard").onclick = async () => {
    $("actions-menu").classList.add("hidden");
    if (!canControl()) return toast("El control está desactivado (modo solo ver).", "error");
    try {
      const text = await navigator.clipboard.readText();
      if (text) send({ type: "type_text", text });
    } catch {
      toast("El navegador no permitió leer el portapapeles.", "error");
    }
    canvas.focus();
  };
  $("refresh-btn").onclick = () => { send({ type: "refresh" }); $("actions-menu").classList.add("hidden"); };
  $("fit").onchange = layout;
  $("quality").onchange = applySettings;
  $("scale").onchange = applySettings;
  $("grayscale").onchange = applySettings;
  $("show-cursor").onchange = () => moveCursor(lastCursor);
  $("monitor").onchange = () => send({ type: "monitor", index: Number($("monitor").value) });
  $("view-only").onclick = () => {
    releaseAll();
    viewOnly = !viewOnly;
    $("view-only").classList.toggle("active", viewOnly);
  };
  $("clip-sync").onclick = () => {
    const on = !$("clip-sync").classList.contains("active");
    $("clip-sync").classList.toggle("active", on);
    send({ type: "clipboard_sync", enabled: on });
  };
  $("fullscreen-btn").onclick = async () => {
    if (document.fullscreenElement) return document.exitFullscreen();
    await document.documentElement.requestFullscreen();
    // Capture Alt+Tab, Esc, etc. where the browser supports it.
    try { await navigator.keyboard?.lock(); } catch { /* not supported */ }
  };
  $("disconnect-btn").onclick = () => {
    send({ type: "disconnect" });
    $("ended-text").textContent = "Has cerrado la sesión.";
  };
  $("close-btn").onclick = () => window.close();
  $("reconnect-btn").onclick = () => {
    $("ended-overlay").classList.add("hidden");
    $("auth-overlay").classList.remove("hidden");
    if (lastPassword) attempt(lastPassword);
  };
  $("auth-cancel").onclick = () => window.close();
  $("auth-form").onsubmit = (ev) => {
    ev.preventDefault();
    attempt($("password").value);
  };

  // --- chat -----------------------------------------------------------------
  function addChat(who, text, me) {
    $("chat-log").append(el("div", { class: `chat-msg${me ? " me" : ""}` }, el("div", { class: "who" }, who), text));
    $("chat-log").scrollTop = $("chat-log").scrollHeight;
  }
  $("chat-btn").onclick = () => {
    const panel = $("chat-panel");
    panel.classList.toggle("hidden");
    $("chat-btn").classList.toggle("active", !panel.classList.contains("hidden"));
    if (!panel.classList.contains("hidden")) $("chat-input").focus();
    layout();
  };
  $("chat-close").onclick = () => $("chat-btn").click();
  $("chat-form").onsubmit = (ev) => {
    ev.preventDefault();
    const text = $("chat-input").value.trim();
    if (!text || !connected) return;
    send({ type: "chat", text });
    addChat("Tú", text, true);
    $("chat-input").value = "";
  };

  // --- files --------------------------------------------------------------------
  function fsRequest(side, op, args) {
    return new Promise((resolve) => {
      const ui_req = ++fsSeq;
      pendingFs.set(ui_req, resolve);
      if (side === "local") {
        send({ type: "local_fs", ui_req, op, path: args.path, args });
      } else if (op === "list") {
        send({ type: "remote_fs", ui_req, request: { type: "fs_list", path: args.path } });
      } else {
        send({ type: "remote_fs", ui_req, request: { type: "fs_op", op, args } });
      }
    });
  }
  const localPane = new FilePane($("pane-local"), { title: "Este equipo", request: (op, a) => fsRequest("local", op, a) });
  const remotePane = new FilePane($("pane-remote"), { title: "Equipo remoto", request: (op, a) => fsRequest("remote", op, a) });
  localPane.onOpenFile = () => $("upload-btn").click();
  remotePane.onOpenFile = () => $("download-btn").click();
  let filesLoaded = false;

  function openFiles() {
    releaseAll();
    $("files-overlay").classList.remove("hidden");
    if (!filesLoaded) {
      filesLoaded = true;
      localPane.load("");
      remotePane.load("");
    }
  }
  $("files-btn").onclick = openFiles;
  $("files-close").onclick = () => {
    $("files-overlay").classList.add("hidden");
    if (mode === "files") send({ type: "frame_ack" }); // resume the screen stream
    canvas.focus();
  };

  $("upload-btn").onclick = () => {
    const entries = localPane.selectedEntries().filter((e) => !e.dir);
    if (!entries.length || !remotePane.listing) return toast("Selecciona uno o más archivos locales.", "error");
    for (const e of entries) send({ type: "upload", path: localPane.join(e.name), dest: remotePane.listing.path });
  };
  $("download-btn").onclick = () => {
    const entries = remotePane.selectedEntries().filter((e) => !e.dir);
    if (!entries.length || !localPane.listing) return toast("Selecciona uno o más archivos remotos.", "error");
    for (const e of entries) send({ type: "download", path: remotePane.join(e.name), dest: localPane.listing.path });
  };

  function addTransfer(tid, direction, name) {
    if (transfers.has(tid)) return;
    const bar = el("div");
    const label = el("span", {}, `${direction === "up" ? "▶" : "◀"} ${name}`);
    const cancel = el("button", { class: "icon", title: "Cancelar", onclick: () => send({ type: "cancel_transfer", tid }) }, "✕");
    const node = el("div", { class: "transfer" }, label, cancel, el("div", { class: "progress" }, bar));
    transfers.set(tid, { node, bar, label, cancel, direction, name });
    $("transfers").classList.remove("hidden");
    $("transfers").prepend(node);
  }

  function updateTransfer(tid, done, total, name, direction) {
    if (!transfers.has(tid)) addTransfer(tid, direction, name);
    const t = transfers.get(tid);
    t.bar.style.width = `${total ? (100 * done) / total : 100}%`;
    t.label.textContent = `${t.direction === "up" ? "▶" : "◀"} ${t.name} · ${formatSize(done)} / ${formatSize(total)}`;
  }

  function finishTransfer(msg) {
    const t = transfers.get(msg.tid);
    if (!t) {
      if (!msg.ok) toast(msg.error || "La transferencia falló", "error");
      return;
    }
    t.cancel.remove();
    t.node.classList.add(msg.ok ? "done" : "failed");
    t.bar.style.width = "100%";
    t.label.textContent = `${t.direction === "up" ? "▶" : "◀"} ${t.name} · ${msg.ok ? "completado" : (msg.error || "error")}`;
    if (msg.ok) (t.direction === "up" ? remotePane : localPane).refresh();
    else toast(`${t.name}: ${msg.error || "error"}`, "error");
  }

  // --- start ------------------------------------------------------------------
  if (partner.length !== 9) {
    $("auth-error").textContent = "ID de asociado no válido.";
    $("auth-submit").disabled = true;
  }
  $("password").focus();
})();
