"use strict";
// Shared helpers for the TeamLooker local UI.
const TL = (() => {
  const params = new URLSearchParams(location.search);
  const token = params.get("token") || "";

  function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}?token=${encodeURIComponent(token)}`;
  }

  function formatId(id) {
    const d = String(id || "").replace(/\D/g, "");
    return d.replace(/(\d{3})(?=\d)/g, "$1 ");
  }

  function formatSize(n) {
    if (n == null) return "";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
  }

  function formatDate(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" });
  }

  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c != null) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function toast(message, kind = "info", ms = 4500) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const t = el("div", { class: `toast ${kind}` }, message);
    box.append(t);
    setTimeout(() => t.remove(), ms);
  }

  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copiado al portapapeles", "ok", 1800);
    } catch {
      toast("No se pudo copiar", "error");
    }
  }

  return { token, wsUrl, formatId, formatSize, formatDate, el, toast, copy };
})();
