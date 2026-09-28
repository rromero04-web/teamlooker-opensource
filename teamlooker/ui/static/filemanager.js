"use strict";
// Two-pane file manager used by the viewer window.
class FilePane {
  /**
   * @param {HTMLElement} root
   * @param {{title: string, request: (op: string, args: object) => Promise<object>}} opts
   */
  constructor(root, opts) {
    const { el } = TL;
    this.root = root;
    this.request = opts.request;
    this.listing = null;
    this.selected = new Set();
    this.onOpenFile = null;
    this.pathInput = el("input", { type: "text", spellcheck: "false" });
    this.status = el("span", { class: "small muted" });
    this.tbody = el("tbody");
    root.append(
      el("div", { class: "pane-head" },
        el("div", { class: "row" }, el("span", { class: "pane-title grow" }, opts.title), this.status),
        el("form", { class: "row", onsubmit: (ev) => { ev.preventDefault(); this.load(this.pathInput.value); } },
          el("button", { type: "button", class: "icon", title: "Carpeta superior", onclick: () => this.up() }, "↑"),
          el("button", { type: "button", class: "icon", title: "Carpeta personal", onclick: () => this.load(this.listing?.home || "") }, "⌂"),
          this.pathInput,
          el("button", { type: "button", class: "icon", title: "Actualizar", onclick: () => this.refresh() }, "⟳"))),
      el("div", { class: "pane-list" },
        el("table", {},
          el("thead", {}, el("tr", {}, el("th", {}, "Nombre"), el("th", {}, "Tamaño"), el("th", {}, "Modificado"))),
          this.tbody)),
      el("div", { class: "pane-actions" },
        el("button", { onclick: () => this.mkdir() }, "Nueva carpeta"),
        el("button", { onclick: () => this.rename() }, "Renombrar"),
        el("button", { class: "danger", onclick: () => this.remove() }, "Eliminar")));
  }

  join(name) {
    const { path, sep } = this.listing;
    return path.endsWith(sep) ? path + name : path + sep + name;
  }

  selectedEntries() {
    if (!this.listing) return [];
    return this.listing.entries.filter((e) => this.selected.has(e.name));
  }

  async load(path) {
    this.status.textContent = "Cargando…";
    const res = await this.request("list", { path });
    this.status.textContent = "";
    if (!res.ok) {
      TL.toast(res.error || "No se pudo abrir la carpeta", "error");
      return;
    }
    this.listing = res.data;
    this.selected.clear();
    this.pathInput.value = res.data.path;
    this.render();
  }

  refresh() { return this.load(this.listing?.path || ""); }

  up() { if (this.listing?.parent) this.load(this.listing.parent); }

  render() {
    const { el, formatSize, formatDate } = TL;
    const rows = [];
    if (this.listing.parent) {
      rows.push(el("tr", { ondblclick: () => this.up() }, el("td", {}, "📁 .."), el("td"), el("td")));
    }
    for (const entry of this.listing.entries) {
      const row = el("tr", { class: this.selected.has(entry.name) ? "selected" : "" },
        el("td", { title: entry.name }, `${entry.dir ? "📁" : "📄"} ${entry.name}`),
        el("td", { class: "num" }, entry.dir ? "" : formatSize(entry.size)),
        el("td", { class: "num" }, formatDate(entry.mtime)));
      row.addEventListener("click", (ev) => {
        if (!(ev.ctrlKey || ev.metaKey)) this.selected.clear();
        if (this.selected.has(entry.name)) this.selected.delete(entry.name);
        else this.selected.add(entry.name);
        this.render();
      });
      row.addEventListener("dblclick", () => {
        if (entry.dir) this.load(this.join(entry.name));
        else if (this.onOpenFile) this.onOpenFile(entry);
      });
      rows.push(row);
    }
    if (!this.listing.entries.length) rows.push(el("tr", {}, el("td", { class: "muted" }, "(carpeta vacía)"), el("td"), el("td")));
    this.tbody.replaceChildren(...rows);
  }

  async op(op, args) {
    const res = await this.request(op, args);
    if (!res.ok) TL.toast(res.error || "La operación falló", "error");
    await this.refresh();
    return res.ok;
  }

  mkdir() {
    const name = prompt("Nombre de la nueva carpeta:");
    if (name) this.op("mkdir", { path: this.listing.path, name });
  }

  rename() {
    const [entry] = this.selectedEntries();
    if (!entry) return TL.toast("Selecciona un elemento", "error");
    const name = prompt("Nuevo nombre:", entry.name);
    if (name && name !== entry.name) this.op("rename", { path: this.join(entry.name), name });
  }

  async remove() {
    const entries = this.selectedEntries();
    if (!entries.length) return TL.toast("Selecciona un elemento", "error");
    if (!confirm(`¿Eliminar ${entries.length === 1 ? `«${entries[0].name}»` : `${entries.length} elementos`}? (Solo archivos y carpetas vacías)`)) return;
    for (const e of entries) await this.op("delete", { path: this.join(e.name) });
  }
}
