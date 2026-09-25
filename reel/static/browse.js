// Home (library tiles), folder browsing and the video details page.
import { api, artBox, encodePath, fill, formatDuration, formatSize, h, parseTags, plural, tagError } from "./api.js";
import { openImageDialog } from "./imagedialog.js";
import { capabilities, capsQuery } from "./caps.js";

const SORT_KEY = "reel.sort";

function getSort() {
  try {
    return localStorage.getItem(SORT_KEY) || "name";
  } catch {
    return "name";
  }
}

function setSort(value) {
  try {
    localStorage.setItem(SORT_KEY, value);
  } catch {
    /* private mode: the choice just isn't remembered */
  }
}

// Bump when the server changes how thumbnails are made (size, crop, shape), so
// browsers fetch the new ones instead of reusing cached old ones.
const THUMBS = "t2";

// Re-render the current page in place (scroll position is kept), e.g. after an upload.
const rerender = () => window.dispatchEvent(new HashChangeEvent("hashchange"));

/** A video's picture: its image on the NAS, else one you uploaded, else null (placeholder). */
function videoImageSrc(item) {
  if (item.has_poster) return `/api/items/${item.id}/thumb?${THUMBS}`;
  if (item.custom_image) return `/api/items/${item.id}/thumb?${THUMBS}&v=${item.custom_image}`;
  return null;
}

/** A folder's picture: folder.<ext> on the NAS, else one you uploaded, else null. */
function folderImageSrc(libraryId, path, hasArt, customArt) {
  if (hasArt) return folderArtUrl(libraryId, path);
  if (customArt) return folderArtUrl(libraryId, path, customArt);
  return null;
}

/**
 * The "Add image" / "Change image" button over a card's picture. Only shown
 * when there's no image on the NAS: NAS images always win.
 */
function imageEditButton({ title, base, query = "", custom }) {
  return h(
    "button",
    {
      type: "button",
      class: "art-edit",
      title: custom ? "Change image" : "Add image",
      "aria-label": `${custom ? "Change" : "Add"} image for ${title}`,
      onclick: (event) => {
        event.preventDefault();
        openImageDialog({
          title: `Image for "${title}"`,
          uploadUrl: `${base}${query}`,
          urlUrl: `${base}-url${query}`,
          removeUrl: custom ? `${base}${query}` : null,
          onDone: rerender,
        });
      },
    },
    imageIcon(),
    h("span", {}, custom ? "Change image" : "Add image"),
  );
}

// A small picture icon (the button shrinks to just this on touch screens).
function imageIcon() {
  const t = document.createElement("template");
  t.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="M21 16l-5-5-9 9"/></svg>';
  return t.content.firstChild;
}

function videoImageButton(item) {
  if (item.has_poster) return null;
  return imageEditButton({ title: item.title, base: `/api/items/${item.id}/image`, custom: item.custom_image });
}

function folderImageButton(libraryId, path, name, hasArt, customArt) {
  if (hasArt) return null;
  return imageEditButton({
    title: name,
    base: `/api/libraries/${libraryId}/folder-image`,
    query: `?path=${encodeURIComponent(path)}`,
    custom: customArt,
  });
}

/** A tag's uploaded image, or null if it has none. */
export function tagImageUrl(tag) {
  return tag.image ? `/api/tags/${tag.id}/image?v=${tag.image}` : null;
}

function folderArtUrl(libraryId, path, version) {
  const url = `/api/libraries/${libraryId}/folder-art?${THUMBS}&path=${encodeURIComponent(path)}`;
  return version ? `${url}&v=${encodeURIComponent(version)}` : url;
}

function folderUrl(libraryId, path) {
  return `#/library/${libraryId}${path ? "/" + encodePath(path) : ""}`;
}

function crumbs(libraryId, breadcrumbs, { linkLast = false } = {}) {
  return h(
    "nav",
    { class: "crumbs", "aria-label": "Folder" },
    breadcrumbs.map((c, i) => {
      const last = i === breadcrumbs.length - 1;
      return [
        i > 0 && h("span", { class: "sep", "aria-hidden": "true" }, "›"),
        last && !linkLast ? h("span", { class: "current" }, c.name) : h("a", { href: folderUrl(libraryId, c.path) }, c.name),
      ];
    }),
  );
}

// ---- Home ---------------------------------------------------------------------

export async function renderHome(view) {
  const [libraries, tags] = await Promise.all([api("GET", "/api/libraries"), api("GET", "/api/tags")]);
  if (!libraries.length) {
    fill(view, 
      h(
        "div",
        { class: "welcome" },
        h("h2", {}, "Welcome to Reel"),
        h("p", {}, "Add a folder of videos to get started."),
        h("a", { class: "btn primary", href: "#/manage" }, "Add a library"),
      ),
    );
    return;
  }
  fill(view, 
    h("h2", { class: "section-title" }, "Libraries", h("span", { class: "count" }, libraries.length)),
    h(
      "ul",
      { class: "grid folders" },
      libraries.map((lib) =>
        h(
          "li",
          { class: "card-wrap" },
          lib.item_count > 0 && folderImageButton(lib.id, "", lib.name, lib.has_art, lib.custom_art),
          h(
            "a",
            { class: "card", href: lib.item_count ? folderUrl(lib.id, "") : "#/manage" },
            artBox({
              kind: "folder",
              shape: "poster",
              src: lib.has_art ? folderArtUrl(lib.id, "", lib.last_scan_at) : folderImageSrc(lib.id, "", false, lib.custom_art),
            }),
            h("div", { class: "label" }, lib.name),
            h("div", { class: "sub" }, lib.item_count ? plural(lib.item_count, "video") : "Not scanned yet"),
          ),
        ),
      ),
    ),
    tags.length > 0 && [
      h("h2", { class: "section-title" }, "Tags", h("span", { class: "count" }, tags.length)),
      h(
        "ul",
        { class: "grid folders" },
        tags.map((tag) =>
          h(
            "li",
            {},
            h(
              "a",
              { class: "card", href: `#/tag/${tag.id}` },
              artBox({ kind: "tag", shape: "poster", src: tagImageUrl(tag) }),
              h("div", { class: "label" }, tag.name),
              h("div", { class: "sub" }, plural(tag.count, "video")),
            ),
          ),
        ),
      ),
    ],
  );
}

// ---- Tag ------------------------------------------------------------------------

export async function renderTag(view, tagId) {
  const { tag, items } = await api("GET", `/api/tags/${tagId}`);
  fill(
    view,
    h(
      "div",
      { class: "page-head" },
      h("nav", { class: "crumbs" }, h("a", { href: "#/tags" }, "Tags"), h("span", { class: "sep", "aria-hidden": "true" }, "›"), h("span", { class: "current" }, tag.name)),
    ),
    h("p", { class: "summary" }, items.length ? plural(items.length, "video") : "No videos have this tag."),
    items.length > 0 && h("ul", { class: "grid videos" }, items.map(videoCard)),
  );
}

/** The tag chips on a video's page, with a box to add more. */
function tagEditor(item) {
  const list = h("ul", { class: "tag-cloud" });
  const error = h("p", { class: "error", hidden: true });
  const suggestions = h("datalist", { id: "tag-suggestions" });
  const input = h("input", {
    type: "text",
    list: "tag-suggestions",
    placeholder: "Add tags: a-z, 0-9 and dashes",
    title: "Tags use only a-z, 0-9 and dashes. Separate several with spaces or commas.",
    maxlength: 500,
    "aria-label": "Add tags",
    autocomplete: "off",
    autocapitalize: "none",
    spellcheck: false,
    // Point out broken rules as soon as they're typed; nothing is changed for you.
    oninput: () => checkInput(),
  });

  const checkInput = () => {
    const problem = tagError(parseTags(input.value));
    input.classList.toggle("invalid", Boolean(problem));
    input.setAttribute("aria-invalid", problem ? "true" : "false");
    showError(problem);
    return problem;
  };

  const showError = (message) => {
    error.textContent = message || "";
    error.hidden = !message;
  };

  const show = (tags) =>
    fill(
      list,
      tags.map((tag) =>
        h(
          "li",
          { class: "chip" },
          h("a", { href: `#/tag/${tag.id}` }, tag.name),
          h(
            "button",
            {
              type: "button",
              class: "chip-x",
              "aria-label": `Remove tag ${tag.name}`,
              title: "Remove tag",
              onclick: async () => {
                try {
                  show(await api("DELETE", `/api/items/${item.id}/tags/${tag.id}`));
                  loadSuggestions();
                } catch (err) {
                  showError(err.message);
                }
              },
            },
            "×",
          ),
        ),
      ),
    );

  const loadSuggestions = async () => {
    const all = await api("GET", "/api/tags").catch(() => []);
    fill(suggestions, all.map((tag) => h("option", { value: tag.name })));
  };

  const form = h(
    "form",
    {
      class: "tag-add",
      onsubmit: async (event) => {
        event.preventDefault();
        const names = parseTags(input.value);
        if (!names.length) return;
        // Add nothing unless every tag follows the rule.
        if (checkInput()) return input.focus();
        try {
          let tags;
          for (const name of names) tags = await api("POST", `/api/items/${item.id}/tags`, { name });
          show(tags);
          input.value = "";
          loadSuggestions();
        } catch (err) {
          showError(err.message);
        }
      },
    },
    input,
    h("button", { type: "submit", class: "btn" }, "Add"),
  );

  show(item.tags);
  loadSuggestions();
  return h("section", { class: "tags-section" }, h("h3", {}, "Tags"), list, form, suggestions, error);
}

// ---- Folder -------------------------------------------------------------------

function videoCard(item) {
  const meta = [item.year, formatDuration(item.duration)].filter(Boolean).join(" · ");
  return h(
    "li",
    { class: "card-wrap" },
    videoImageButton(item),
    h(
      "a",
      { class: "card", href: `#/item/${item.id}`, title: item.title },
      artBox({ kind: "video", shape: "landscape", src: videoImageSrc(item) }),
      h("div", { class: "label" }, item.title),
      meta && h("div", { class: "sub" }, meta),
    ),
  );
}

function folderCard(libraryId, folder) {
  return h(
    "li",
    { class: "card-wrap" },
    folderImageButton(libraryId, folder.path, folder.name, folder.has_art, folder.custom_art),
    h(
      "a",
      { class: "card", href: folderUrl(libraryId, folder.path) },
      artBox({
        kind: "folder",
        shape: "poster",
        src: folderImageSrc(libraryId, folder.path, folder.has_art, folder.custom_art),
      }),
      h("div", { class: "label" }, folder.name),
      h("div", { class: "sub" }, plural(folder.item_count, "video")),
    ),
  );
}

export async function renderBrowse(view, libraryId, path) {
  const load = () =>
    api("GET", `/api/libraries/${libraryId}/browse?path=${encodeURIComponent(path)}&sort=${getSort()}`);
  let data = await load();

  const itemsList = h("ul", { class: "grid videos" });
  const fillItems = () => itemsList.replaceChildren(...data.items.map(videoCard));
  fillItems();

  const sortSelect = h(
    "select",
    {
      "aria-label": "Sort videos",
      onchange: async (e) => {
        setSort(e.target.value);
        data = await load();
        fillItems();
      },
    },
    h("option", { value: "name" }, "Name"),
    h("option", { value: "year" }, "Year"),
  );
  sortSelect.value = data.sort;

  const counts = [
    data.folders.length && plural(data.folders.length, "folder"),
    data.items.length && plural(data.items.length, "video"),
  ].filter(Boolean);

  fill(view, 
    h(
      "div",
      { class: "page-head" },
      crumbs(libraryId, data.breadcrumbs),
      data.items.length > 1 && h("label", { class: "sort" }, "Sort ", sortSelect),
    ),
    h("p", { class: "summary" }, counts.join(" · ") || "This folder is empty."),
    data.folders.length > 0 &&
      h("ul", { class: "grid folders" }, data.folders.map((f) => folderCard(libraryId, f))),
    data.items.length > 0 && itemsList,
  );
}

// ---- Video details ----------------------------------------------------------

// [tone, short label, what it means]
const PLAY_MODES = {
  direct: ["ok", "Direct play", "Plays directly in the browser"],
  remux: ["ok", "Quick repackage", "Repackaged on the fly, with no quality loss"],
  audio: ["ok", "Audio converted", "The video plays as is; only the audio is converted"],
  transcode: ["warn", "Converted live", "Converted by the server while it plays"],
  unsupported: ["err", "Can't play", "This video can't be played"],
};

/** "1080p", "720p", "480i" for common sizes; otherwise "1680 × 1050". */
function resolutionLabel(item) {
  const scan = item.interlaced ? "i" : "p";
  if ([2160, 1440, 1080, 720, 576, 480].includes(item.height)) return item.height === 2160 ? "4K" : `${item.height}${scan}`;
  return `${item.width} × ${item.height}`;
}

function codecLabel(codec) {
  const names = {
    h264: "H.264", hevc: "HEVC", av1: "AV1", vp9: "VP9", vp8: "VP8", mpeg4: "MPEG-4 (Xvid/DivX)",
    mpeg2video: "MPEG-2", mpeg1video: "MPEG-1", wmv2: "WMV 8", msmpeg4v3: "MS MPEG-4 v3",
    vc1image: "VC-1", mjpeg: "Motion JPEG", cinepak: "Cinepak", svq1: "Sorenson",
    aac: "AAC", mp3: "MP3", mp2: "MP2", opus: "Opus", vorbis: "Vorbis", ac3: "AC-3",
    wmav2: "WMA", adpcm_ms: "MS ADPCM", pcm_u8: "PCM", pcm_s16le: "PCM", pcm_s16be: "PCM", flac: "FLAC",
  };
  return codec ? names[codec] || codec : "none";
}

export async function renderItem(view, itemId) {
  const caps = await capabilities();
  const [item, plan] = await Promise.all([
    api("GET", `/api/items/${itemId}`),
    api("GET", `/api/items/${itemId}/plan?${capsQuery(caps)}`),
  ]);
  // The mode for this browser (it may play more than a typical one, e.g. HEVC).
  item.play_mode = plan.mode;
  const [tone, modeLabel, modeText] = PLAY_MODES[item.play_mode] || ["err", item.play_mode, item.play_mode];
  const image = videoImageSrc(item);
  const pills = [
    item.year && h("span", { class: "pill" }, item.year),
    item.duration && h("span", { class: "pill" }, formatDuration(item.duration)),
    item.height && h("span", { class: "pill" }, resolutionLabel(item)),
    item.missing
      ? h("span", { class: "pill mode err", title: "The last scan couldn't find this file. It's kept for a while in case it comes back." }, "Missing from the library")
      : h(
          "span",
          { class: `pill mode ${tone}`, title: item.probe_error ? `${modeText}: ${item.probe_error}` : modeText },
          modeLabel,
        ),
  ];
  const playable = item.play_mode !== "unsupported" && !item.missing;
  const playUrl = `#/play/${item.id}`;
  const facts = [
    ["Length", formatDuration(item.duration) || "unknown"],
    ["Resolution", item.width ? `${item.width} × ${item.height}${item.interlaced ? " (interlaced)" : ""}` : "unknown"],
    ["Video", codecLabel(item.video_codec)],
    ["Audio", codecLabel(item.audio_codec)],
    ["File size", formatSize(item.size)],
    ["File", item.rel_path],
  ];

  fill(view,
    // The video's picture, blurred, glowing behind the top of the page.
    image && h("div", { class: "backdrop", style: `background-image:url("${image}")`, "aria-hidden": "true" }),
    h("div", { class: "page-head" }, crumbs(item.library_id, item.breadcrumbs, { linkLast: true })),
    h(
      "article",
      { class: "detail" },
      h("div", { class: "card-wrap hero-wrap" }, videoImageButton(item), artBox(
        {
          kind: "video",
          shape: "landscape hero",
          src: image,
          alt: item.title,
          tag: playable ? "a" : "div",
          attrs: playable ? { href: playUrl, "aria-label": `Play ${item.title}` } : {},
        },
        playable && h("span", { class: "hero-play" }, "▶"),
      )),
      h(
        "div",
        { class: "info" },
        h("h2", {}, item.title),
        h("div", { class: "pills" }, pills),
        playable
          ? h("a", { class: "btn primary play", href: playUrl }, "▶ Play")
          : h("button", { class: "btn primary play", disabled: true }, "▶ Play"),
        tagEditor(item),
        h("dl", { class: "facts" }, facts.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)])),
      ),
    ),
  );
}
