// A video's clips (made on its edit page): links to a clip's page and the player,
// the question before deleting one, and the list on the edit page (each clip a
// card that opens its page, with a Delete that asks first).
import { api, artBox, fill, formatDuration, h } from "./api.js";

// `list`: the folder whose "Show all" list it was opened from (prev/next), or null.
const listQuery = (list, sep) => (list === null || list === undefined ? "" : `${sep}all=${encodeURIComponent(list)}`);

/** A clip's page. */
export function clipPageUrl(clipId, list = null) {
  return `#/clip/${clipId}${listQuery(list, "?")}`;
}

/** The player, playing just this clip. */
export function clipPlayUrl(videoId, clipId, list = null) {
  return `#/play/${videoId}?clip=${clipId}${listQuery(list, "&")}`;
}

/** A clip's picture: its video's frame at its start (cached by the server; the
 *  version changes when the video file does, so the browser keeps it for good). */
export function clipImageUrl(clip) {
  return `/api/clips/${clip.id}/thumb?v=${encodeURIComponent(clip.picture)}`;
}

/**
 * A clip's card: its picture, `label` (its name, or "<video> · <name>"), the
 * video's path (`where`, if given), and its times, with a Clip badge in lists of
 * videos (`badge`). It opens the clip's page, keeping `list`. With `onDelete`,
 * a Delete button under it.
 */
export function clipCard(clip, { label = clip.name, where = null, badge = false, list = null, onDelete = null } = {}) {
  const times = clipTimes(clip);
  return h(
    "li",
    { class: `card-wrap clip${onDelete ? " deletable" : ""}` },
    h(
      "a",
      { class: "card", href: clipPageUrl(clip.id, list), title: label },
      artBox({ kind: "video", shape: "landscape", src: clipImageUrl(clip), alt: "" }),
      h("div", { class: "label" }, label),
      where !== null && h("div", { class: "sub where" }, where),
      badge
        ? h("div", { class: "sub meta" }, h("span", {}, times), h("span", { class: "type-badge clip", title: "A clip of the video before it" }, "Clip"))
        : h("div", { class: "sub" }, times),
    ),
    onDelete && h("button", { type: "button", class: "btn small danger clip-delete", "aria-label": `Delete ${clip.name}`, onclick: onDelete }, "Delete"),
  );
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
 * The "Clips" section: a card per clip (its first frame, name and times; it opens
 * the clip's page, keeping `list`) with a Delete button. `onChange(clips)` is told after a delete.
 * With `emptyText`, an empty list says so; without, the section hides itself.
 * Returns { element, show(clips) }.
 */
export function clipsSection(item, clips, { emptyText = null, list = null, onChange = () => {} } = {}) {
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

  function show(next) {
    clips = next;
    element.hidden = !clips.length && !emptyText;
    count.textContent = clips.length || "";
    empty.hidden = clips.length > 0;
    fill(grid, clips.map((clip) => clipCard(clip, { list, onDelete: () => remove(clip) })));
  }
  show(clips);
  return { element, show };
}
