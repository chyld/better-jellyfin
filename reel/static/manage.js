// The "Libraries" page: add, rename, remove and scan libraries.
import { api, fill, h, plural } from "./api.js";

const $ = (sel) => document.querySelector(sel);
// The Libraries page open now, if any. Its state lives in the page itself; the
// dialogs (shared, in index.html) just ask this one to refresh.
let activePage = null;

function showError(el, message) {
  el.textContent = message || "";
  el.hidden = !message;
}

function timeAgo(utc) {
  const date = new Date(utc.replace(" ", "T") + "Z");
  const seconds = (Date.now() - date) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return date.toLocaleDateString();
}

function statusLine(lib) {
  const scan = lib.scan;
  if (scan?.state === "queued") return { text: "Waiting to scan…" };
  if (scan?.state === "scanning") {
    return {
      text: scan.total ? `Scanning: ${scan.done} / ${scan.total}` : "Scanning: looking for videos…",
      progress: scan.total ? scan.done / scan.total : 0,
    };
  }
  if (scan?.state === "error") return { text: `Scan failed: ${scan.error}`, cls: "err" };
  if (scan?.state === "done") {
    const r = scan.result;
    const parts = [`${r.added} added`, `${r.updated} updated`];
    if (r.moved) parts.push(`${r.moved} moved`);
    if (r.missing) parts.push(`${r.missing} missing`);
    if (r.removed) parts.push(`${r.removed} removed`);
    if (r.failed) parts.push(`${r.failed} unplayable`);
    const warned = Boolean(lib.last_scan_warning);
    const text = `Scan finished: ${parts.join(", ")}${warned ? `. ${lib.last_scan_warning}` : ""}`;
    return { text, cls: r.failed || warned ? "err" : "ok" };
  }
  if (lib.last_scan_error) return { text: `Last scan failed: ${lib.last_scan_error}`, cls: "err" };
  if (lib.last_scan_warning) return { text: `Last scanned ${timeAgo(lib.last_scan_at)}. ${lib.last_scan_warning}`, cls: "err" };
  if (lib.last_scan_at) return { text: `Last scanned ${timeAgo(lib.last_scan_at)}` };
  return { text: "Not scanned yet" };
}

const isBusy = (lib) => lib.scan?.state === "queued" || lib.scan?.state === "scanning";

function libraryRow(lib) {
  const status = statusLine(lib);
  const busy = isBusy(lib);
  return h(
    "li",
    { class: "library" },
    h("h3", {}, h("a", { href: `#/library/${lib.id}` }, lib.name)),
    h("div", { class: "path" }, lib.path),
    h(
      "div",
      { class: "meta" },
      h("span", {}, plural(lib.item_count, "video")),
      " · ",
      h("span", { class: status.cls || "" }, status.text),
    ),
    status.progress !== undefined &&
      h("div", { class: "progress" }, h("div", { style: `width:${Math.round(status.progress * 100)}%` })),
    h(
      "div",
      { class: "buttons" },
      h("button", { class: "btn", disabled: busy, onclick: () => actions.scan(lib) }, "Scan"),
      h("button", { class: "btn", onclick: () => actions.rename(lib) }, "Rename"),
      h("button", { class: "btn danger", disabled: busy, onclick: () => actions.remove(lib) }, "Remove"),
    ),
  );
}

const refresh = () => activePage?.refresh();

const actions = {
  async scan(lib) {
    await api("POST", `/api/libraries/${lib.id}/scan`);
    refresh();
  },
  rename(lib) {
    const dialog = $("#rename-dialog");
    dialog.dataset.id = lib.id;
    $("#rename-name").value = lib.name;
    showError($("#rename-error"));
    dialog.showModal();
  },
  remove(lib) {
    const dialog = $("#remove-dialog");
    dialog.dataset.id = lib.id;
    $("#remove-name").textContent = lib.name;
    showError($("#remove-error"));
    dialog.showModal();
  },
};

export function renderManage(view) {
  const listEl = h("ul", { class: "library-list" });
  const scanAllBtn = h("button", { class: "btn", disabled: true, onclick: scanAll }, "Scan all");
  const empty = h("p", { class: "empty", hidden: true }, "No libraries yet. Add a folder of videos to get started.");
  fill(view,
    h(
      "div",
      { class: "page-head" },
      h("h2", { class: "page-title" }, "Libraries"),
      h("div", { class: "actions" }, scanAllBtn, h("button", { class: "btn primary", onclick: openAddDialog }, "Add library")),
    ),
    empty,
    listEl,
  );

  let pollTimer = null;
  let closed = false;
  const page = {
    async refresh() {
      const libraries = await api("GET", "/api/libraries");
      if (closed) return; // the page was left while this was loading
      empty.hidden = libraries.length > 0;
      scanAllBtn.disabled = libraries.length === 0 || libraries.every(isBusy);
      listEl.replaceChildren(...libraries.map(libraryRow));
      // Keep polling while any scan is queued or running.
      clearTimeout(pollTimer);
      if (libraries.some(isBusy)) pollTimer = setTimeout(() => page.refresh(), 1000);
    },
  };
  activePage = page;
  page.refresh();
  return () => {
    closed = true;
    clearTimeout(pollTimer);
    if (activePage === page) activePage = null;
  };
}

async function scanAll() {
  await api("POST", "/api/libraries/scan");
  refresh();
}

// ---- Folder picker ----------------------------------------------------------

let pickerPath = null;
let nameWasTyped = false;

async function openFolder(path) {
  const url = path ? `/api/folders?path=${encodeURIComponent(path)}` : "/api/folders";
  try {
    const data = await api("GET", url);
    pickerPath = data.path;
    $("#picker-current").textContent = data.path;
    $("#picker-up").disabled = !data.parent;
    $("#picker-up").onclick = () => openFolder(data.parent);
    const items = data.folders.map((f) =>
      h("li", {}, h("button", { type: "button", onclick: () => openFolder(f.path) }, f.name)),
    );
    if (!items.length) items.push(h("li", { class: "none" }, "No subfolders. This folder will be used."));
    $("#picker-list").replaceChildren(...items);
    // Suggest the folder's name until the user types their own.
    if (!nameWasTyped) $("#add-name").value = data.parent ? data.path.split("/").pop() : "";
    showError($("#add-error"));
  } catch (err) {
    showError($("#add-error"), err.message);
  }
}

function openAddDialog() {
  nameWasTyped = false;
  $("#add-name").value = "";
  showError($("#add-error"));
  $("#add-dialog").showModal();
  openFolder(null);
}

$("#add-name").addEventListener("input", () => (nameWasTyped = true));

$("#add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("POST", "/api/libraries", { name: $("#add-name").value, path: pickerPath });
    $("#add-dialog").close();
    refresh();
  } catch (err) {
    showError($("#add-error"), err.message);
  }
});

$("#rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const dialog = $("#rename-dialog");
  try {
    await api("PATCH", `/api/libraries/${dialog.dataset.id}`, { name: $("#rename-name").value });
    dialog.close();
    refresh();
  } catch (err) {
    showError($("#rename-error"), err.message);
  }
});

$("#remove-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const dialog = $("#remove-dialog");
  try {
    await api("DELETE", `/api/libraries/${dialog.dataset.id}`);
    dialog.close();
    refresh();
  } catch (err) {
    showError($("#remove-error"), err.message);
  }
});

document.querySelectorAll("[data-close]").forEach((btn) =>
  btn.addEventListener("click", () => btn.closest("dialog").close()),
);
