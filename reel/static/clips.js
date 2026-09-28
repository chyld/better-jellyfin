// A video's clips (made on its edit page): the list shown on the video's page and
// on the edit page, each clip a card that plays it, with a Delete that asks first.
import { api, artBox, fill, formatDuration, h } from "./api.js";

/** The player, playing just this clip. */
export function clipUrl(itemId, clip) {
  return `#/play/${itemId}?clip=${clip.id}`;
}

/** "0:12 – 0:40 · 0:28" */
export function clipTimes(clip) {
  return `${formatDuration(clip.start) || "0:00"} – ${formatDuration(clip.end)} · ${formatDuration(clip.end - clip.start) || "0:00"}`;
}

/** Ask before deleting, in a dialog like "Remove library?". Resolves to whether
 *  Delete was chosen (Cancel, Escape or clicking away: false). */
export function confirmDelete(title, text) {
  return new Promise((resolve) => {
    const dialog = h(
      "dialog",
      { class: "confirm-dialog" },
      h(
        "form",
        { method: "dialog" },
        h("h3", {}, title),
        h("p", {}, text),
        h(
          "div",
          { class: "dialog-actions" },
          h("button", { type: "submit", value: "cancel", class: "btn", autofocus: true }, "Cancel"),
          h("button", { type: "submit", value: "delete", class: "btn danger" }, "Delete"),
        ),
      ),
    );
    dialog.addEventListener("click", (e) => e.target === dialog && dialog.close()); // the backdrop
    dialog.addEventListener("close", () => {
      dialog.remove();
      resolve(dialog.returnValue === "delete");
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

/**
 * The "Clips" section: a card per clip (its first frame, name and times; it plays
 * the clip) with a Delete button. `onChange(clips)` is told after a delete.
 * With `emptyText`, an empty list says so; without, the section hides itself.
 * Returns { element, show(clips) }.
 */
export function clipsSection(item, clips, { playable = true, emptyText = null, onChange = () => {} } = {}) {
  const count = h("span", { class: "count" });
  const grid = h("ul", { class: "grid videos clip-grid" });
  const empty = h("p", { class: "summary" }, emptyText || "");
  const error = h("p", { class: "error", hidden: true, role: "alert" });
  const element = h("section", { class: "clips" }, h("h2", { class: "section-title" }, "Clips", count), error, empty, grid);

  async function remove(clip) {
    if (!(await confirmDelete(`Delete ${clip.name}?`, `${clip.name} (${clipTimes(clip)}) will be deleted. The video file is not touched.`))) return;
    try {
      show(await api("DELETE", `/api/items/${item.id}/clips/${clip.id}`));
      error.hidden = true;
      onChange(clips);
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  }

  function card(clip) {
    const art = artBox({ kind: "video", shape: "landscape", src: `/api/items/${item.id}/frame?at=${clip.start.toFixed(1)}`, alt: "" });
    const lines = [h("div", { class: "label" }, clip.name), h("div", { class: "sub" }, clipTimes(clip))];
    return h(
      "li",
      { class: "card-wrap clip" },
      playable
        ? h("a", { class: "card", href: clipUrl(item.id, clip), "aria-label": `Play ${clip.name}` }, art, lines)
        : h("div", { class: "card" }, art, lines),
      h("button", { type: "button", class: "btn small danger clip-delete", "aria-label": `Delete ${clip.name}`, onclick: () => remove(clip) }, "Delete"),
    );
  }

  function show(next) {
    clips = next;
    element.hidden = !clips.length && !emptyText;
    count.textContent = clips.length || "";
    empty.hidden = clips.length > 0;
    fill(grid, clips.map(card));
  }
  show(clips);
  return { element, show };
}
