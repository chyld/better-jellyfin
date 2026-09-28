// A clip's page (#/clip/<id>), like a video's page: the clip's picture, name and
// times, Play (just the clip), its video, Delete (asks first), and prev / next
// when opened from a "Show all" list.
import { api, artBox, fill, formatDuration, h } from "./api.js";
import { crumbs, itemUrl, listNav } from "./browse.js";
import { capabilities, capsQuery, hlsSupport } from "./caps.js";
import { clipImageUrl, clipPlayUrl, clipTimes, confirmDelete } from "./clips.js";

/** A clip's page, like a video's: its first frame, name and times, Play (just
 *  the clip), its video, and Delete (asks first). `list`: the folder whose "Show
 *  all" list it was opened from (prev/next through videos and clips), or null. */
export async function renderClip(view, clipId, list = null) {
  const caps = await capabilities();
  const clip = await api("GET", `/api/clips/${clipId}`);
  const video = clip.video;
  const [plan, around] = await Promise.all([
    api("GET", `/api/items/${video.id}/plan?${capsQuery(caps)}&hls_support=${hlsSupport()}`),
    list === null ? null : api("GET", `/api/clips/${clipId}/neighbors?path=${encodeURIComponent(list)}`).catch(() => null),
  ]);
  const inList = around ? list : null;
  const playable = plan.mode !== "unsupported" && !video.missing;
  const image = clipImageUrl(clip);
  const playHref = clipPlayUrl(video.id, clip.id, inList);
  const videoHref = itemUrl(video.id, inList);
  const time = (t) => formatDuration(t) || "0:00";
  const error = h("p", { class: "error", hidden: true, role: "alert" });

  async function remove() {
    if (!(await confirmDelete(`Delete ${clip.name}?`, `${clip.name} (${clipTimes(clip)}) will be deleted. The video file is not touched.`))) return;
    try {
      await api("DELETE", `/api/items/${video.id}/clips/${clip.id}`);
      location.replace(videoHref); // Back shouldn't return to a clip that's gone
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
  }

  const facts = [
    ["Clip of", h("a", { href: videoHref }, video.title)],
    ["Starts", time(clip.start)],
    ["Ends", time(clip.end)],
    ["Length", time(clip.end - clip.start)],
    ["File", video.rel_path],
  ];
  fill(view,
    h("div", { class: "backdrop", style: `background-image:url("${image}")`, "aria-hidden": "true" }),
    h("div", { class: "page-head" }, crumbs(video.library_id, video.breadcrumbs, { linkLast: true }), around && listNav(around, list)),
    h(
      "article",
      { class: "detail clip-detail" },
      h("div", { class: "card-wrap hero-wrap" }, artBox(
        {
          kind: "video",
          shape: "landscape hero",
          src: image,
          alt: `${video.title} · ${clip.name}`,
          tag: playable ? "a" : "div",
          attrs: playable ? { href: playHref, "aria-label": `Play ${clip.name}` } : {},
        },
        playable && h("span", { class: "hero-play" }, "▶"),
      )),
      h(
        "div",
        { class: "info" },
        h("h2", {}, `${video.title} · ${clip.name}`),
        h(
          "div",
          { class: "pills" },
          h("span", { class: "pill clip" }, "Clip"),
          h("span", { class: "pill" }, time(clip.end - clip.start)),
          h("span", { class: "pill" }, `${time(clip.start)} – ${time(clip.end)}`),
        ),
        h(
          "div",
          { class: "detail-actions" },
          playable
            ? h("a", { class: "btn primary play", href: playHref }, "▶ Play")
            : h("button", { class: "btn primary play", disabled: true }, "▶ Play"),
          h("a", { class: "btn edit-link", href: videoHref }, "Go to video"),
          h("button", { type: "button", class: "btn danger edit-link", onclick: remove }, "Delete"),
        ),
        error,
        h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
      ),
    ),
  );
}
