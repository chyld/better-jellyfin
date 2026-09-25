// The "Tags" page: every tag, with rename (or merge) and delete.
import { api, artBox, fill, h, plural, tagError } from "./api.js";
import { tagImageUrl } from "./browse.js";
import { openImageDialog } from "./imagedialog.js";

export async function renderTags(view) {
  let tags = await api("GET", "/api/tags");

  const list = h("ul", { class: "tag-list" });
  const summary = h("p", { class: "summary" });
  const pageError = h("p", { class: "error", hidden: true });
  const showPageError = (message) => {
    pageError.textContent = message || "";
    pageError.hidden = !message;
  };
  const renameDialog = buildRenameDialog();
  const deleteDialog = buildDeleteDialog();

  async function refresh() {
    tags = await api("GET", "/api/tags");
    draw();
  }

  function draw() {
    summary.textContent = tags.length
      ? plural(tags.length, "tag")
      : "No tags yet. Add tags from a video's page.";
    fill(
      list,
      tags.map((tag) =>
        h(
          "li",
          { class: "tag-row" },
          artBox({ kind: "tag", shape: "mini", src: tagImageUrl(tag), alt: "" }),
          h("a", { class: "chip", href: `#/tag/${tag.id}` }, tag.name),
          h("span", { class: "tag-count" }, plural(tag.count, "video")),
          h(
            "div",
            { class: "buttons" },
            imageButton(tag),
            h("button", { class: "btn", onclick: () => renameDialog.openFor(tag) }, "Rename"),
            h("button", { class: "btn danger", onclick: () => deleteDialog.openFor(tag) }, "Delete"),
          ),
        ),
      ),
    );
  }

  // "Set image" opens the shared dialog: a photo from this device, or a URL.
  function imageButton(tag) {
    const base = `/api/tags/${tag.id}`;
    return h(
      "button",
      {
        class: "btn",
        onclick: () =>
          openImageDialog({
            title: `Image for "${tag.name}"`,
            uploadUrl: `${base}/image`,
            urlUrl: `${base}/image-url`,
            removeUrl: tag.image ? `${base}/image` : null,
            onDone: refresh,
          }),
      },
      tag.image ? "Change image" : "Set image",
    );
  }

  function buildRenameDialog() {
    let current = null;
    const input = h("input", {
      required: true,
      maxlength: 100,
      autocomplete: "off",
      autocapitalize: "none",
      spellcheck: false,
      "aria-label": "New name",
      oninput: () => check(),
    });
    const error = h("p", { class: "error", hidden: true });
    const note = h("p", { class: "note", hidden: true });
    const save = h("button", { type: "submit", class: "btn primary" }, "Save");

    // Validate as you type, and warn when the new name would merge two tags.
    function check() {
      const name = input.value.trim();
      const problem = name && tagError([name]);
      const other = !problem && tags.find((t) => t.name === name && t.id !== current.id);
      error.textContent = problem || "";
      error.hidden = !problem;
      input.classList.toggle("invalid", Boolean(problem));
      note.hidden = !other;
      if (other) {
        note.textContent = `"${other.name}" already exists. Saving merges "${current.name}" into it: videos with either tag end up under "${other.name}".`;
      }
      save.textContent = other ? "Merge" : "Save";
      return problem;
    }

    const dialog = h(
      "dialog",
      {},
      h(
        "form",
        {
          method: "dialog",
          onsubmit: async (event) => {
            event.preventDefault();
            if (check()) return;
            try {
              await api("PATCH", `/api/tags/${current.id}`, { name: input.value.trim() });
              dialog.close();
              refresh();
            } catch (err) {
              error.textContent = err.message;
              error.hidden = false;
            }
          },
        },
        h("h3", {}, "Rename tag"),
        h("label", { class: "field" }, h("span", {}, "Name"), input),
        h("p", { class: "hint" }, "Lowercase a-z, 0-9 and dashes (-) only."),
        error,
        note,
        h(
          "div",
          { class: "dialog-actions" },
          h("button", { type: "button", class: "btn", onclick: () => dialog.close() }, "Cancel"),
          save,
        ),
      ),
    );
    // Not "open": that is the <dialog> element's own property, and setting it shows the dialog.
    dialog.openFor = (tag) => {
      current = tag;
      input.value = tag.name;
      check();
      dialog.showModal();
      input.select();
    };
    return dialog;
  }

  function buildDeleteDialog() {
    let current = null;
    const text = h("p");
    const error = h("p", { class: "error", hidden: true });
    const dialog = h(
      "dialog",
      {},
      h(
        "form",
        {
          method: "dialog",
          onsubmit: async (event) => {
            event.preventDefault();
            try {
              await api("DELETE", `/api/tags/${current.id}`);
              dialog.close();
              refresh();
            } catch (err) {
              error.textContent = err.message;
              error.hidden = false;
            }
          },
        },
        h("h3", {}, "Delete tag?"),
        text,
        error,
        h(
          "div",
          { class: "dialog-actions" },
          h("button", { type: "button", class: "btn", onclick: () => dialog.close() }, "Cancel"),
          h("button", { type: "submit", class: "btn danger" }, "Delete"),
        ),
      ),
    );
    // Not "open": that is the <dialog> element's own property, and setting it shows the dialog.
    dialog.openFor = (tag) => {
      current = tag;
      text.textContent = `"${tag.name}" will be removed from ${plural(tag.count, "video")}. The videos themselves aren't affected.`;
      error.hidden = true;
      dialog.showModal();
    };
    return dialog;
  }

  fill(
    view,
    h("div", { class: "page-head" }, h("h2", { class: "page-title" }, "Tags")),
    summary,
    pageError,
    list,
    renameDialog,
    deleteDialog,
  );
  draw();
}
