// A video's edit page: mark stretches of it (ranges) with a start and an end.
//
// Nothing here changes the file (Reel never writes to your videos). A range is
// saved in Reel's database as one of three kinds:
//   range  just marked, to find again
//   skip   jumped over when the video plays (the "deleted" part)
//   clip   shown on the video's page as a video of its own
//
// The preview plays the video the same way the player does (sources.js), but a
// converted stream isn't frame-exact when paused, so the start and end each show
// a picture the server takes from the original file at exactly that time.
import { api, fill, formatDuration, formatTime, h, parseTime } from "./api.js";
import { capabilities, capsQuery, hlsSupport } from "./caps.js";
import { makeSource } from "./sources.js";

export const KINDS = {
  range: { name: "Range", hint: "Just marked, to find again." },
  skip: { name: "Skip", hint: "Jumped over when the video plays, as if it were cut out." },
  clip: { name: "Clip", hint: "Shown on the video's page as a video of its own." },
};
const NUDGES = [-1, -0.1, 0.1, 1];
const STILL_DELAY = 250; // ms after the last change before a picture is asked for

/** A range's name: its label, or its kind ("Clip"). */
export function rangeName(range) {
  return range.label || KINDS[range.kind]?.name || "Range";
}

/** `t` kept inside the video, to a tenth of a second. */
export function clampTime(t, total) {
  return Math.round(Math.min(Math.max(t, 0), total) * 10) / 10;
}

export async function renderEditor(view, itemId) {
  const caps = await capabilities();
  const [item, plan] = await Promise.all([
    api("GET", `/api/items/${itemId}`),
    api("GET", `/api/items/${itemId}/plan?${capsQuery(caps)}&hls_support=${hlsSupport()}`),
  ]);
  if (item.missing) throw new Error("This video is missing from the library, so it can't be edited.");
  const total = item.duration || 0;
  if (!total) throw new Error("This video's length is unknown, so it can't be edited.");
  const playable = plan.mode !== "unsupported";
  let ranges = item.ranges || [];

  // What's being edited: a new range (id null) or a saved one.
  const draft = { id: null, start: 0, end: clampTime(Math.min(10, total), total), kind: "range", label: "" };
  let previewUntil = null; // playing a range: pause at its end

  // ---- Preview ----
  const video = h("video", { class: "edit-video", playsinline: true, preload: "auto" });
  const playBtn = h("button", { type: "button", class: "btn small", title: "Play / pause (Space)" }, "▶ Play");
  const clock = h("span", { class: "edit-clock" }, `${formatTime(0)} / ${formatTime(total)}`);
  const selection = h("div", { class: "edit-selection" });
  const saved = h("div", { class: "edit-saved" });
  const head = h("div", { class: "edit-head" });
  const timeline = h(
    "div",
    { class: "edit-timeline", role: "slider", tabindex: 0, "aria-label": "Position", "aria-valuemin": 0, "aria-valuemax": Math.round(total) },
    saved,
    selection,
    head,
  );
  const message = h("p", { class: "edit-note", hidden: playable }, "This video can't play in this browser. Type the times, and use the pictures to check them.");

  let source = null;
  if (playable) {
    source = await makeSource(video, plan, (text) => ((message.textContent = text), (message.hidden = false)), {
      nativeHls: hlsSupport() === "native",
    });
    source.load(0);
  }
  const position = () => (source ? Math.max(0, source.position()) : 0);
  const pct = (t) => `${Math.min(100, Math.max(0, (t / total) * 100))}%`;

  function seek(t) {
    if (!source) return;
    source.seek(clampTime(t, Math.max(0, total - 0.1)));
    showPosition();
  }
  function togglePlay() {
    if (!source) return;
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  }
  function showPosition() {
    const at = position();
    clock.textContent = `${formatTime(at)} / ${formatTime(total)}`;
    head.style.left = pct(at);
    timeline.setAttribute("aria-valuenow", Math.round(at));
    timeline.setAttribute("aria-valuetext", formatTime(at));
    if (previewUntil !== null && at >= previewUntil) {
      previewUntil = null;
      video.pause();
    }
  }
  function playRange(start, end) {
    if (!source) return;
    seek(start);
    previewUntil = end;
    video.play().catch(() => {});
  }
  video.addEventListener("timeupdate", showPosition);
  video.addEventListener("play", () => (playBtn.textContent = "❚❚ Pause"));
  video.addEventListener("pause", () => (playBtn.textContent = "▶ Play"));
  video.addEventListener("click", togglePlay);
  playBtn.addEventListener("click", togglePlay);
  playBtn.disabled = !playable;

  // Click or drag on the timeline to move there.
  const timeAt = (clientX) => {
    const rect = timeline.getBoundingClientRect();
    return (Math.min(Math.max(clientX - rect.left, 0), rect.width) / rect.width) * total;
  };
  let dragging = false;
  timeline.addEventListener("pointerdown", (e) => {
    if (!source) return;
    dragging = true;
    timeline.setPointerCapture(e.pointerId);
    previewUntil = null;
    seek(timeAt(e.clientX));
  });
  timeline.addEventListener("pointermove", (e) => dragging && seek(timeAt(e.clientX)));
  timeline.addEventListener("pointerup", () => (dragging = false));
  timeline.addEventListener("pointercancel", () => (dragging = false));

  // ---- The range being edited: start, end (each with its picture), label, kind ----
  function endpoint(which, label, key) {
    const input = h("input", { type: "text", class: "edit-time", inputmode: "decimal", "aria-label": `${label} time`, spellcheck: "false" });
    const still = h("img", { class: "edit-still", alt: `The frame at the ${which}` });
    const stillBox = h("div", { class: "edit-still-box" }, still);
    still.addEventListener("load", () => stillBox.classList.remove("loading"));
    still.addEventListener("error", () => stillBox.classList.remove("loading"));
    let timer = null;
    const set = (t) => {
      draft[which] = clampTime(t, total);
      showDraft();
    };
    input.addEventListener("change", () => {
      const t = parseTime(input.value);
      input.classList.toggle("invalid", t === null);
      if (t === null) return showError(`"${input.value}" isn't a time. Try 1:02:10.5, 2:05 or 75.`);
      set(t);
    });
    const box = h(
      "div",
      { class: "edit-end" },
      h("div", { class: "edit-end-head" }, h("span", { class: "edit-label" }, label), input),
      stillBox,
      h(
        "div",
        { class: "edit-nudges" },
        NUDGES.map((d) =>
          h("button", { type: "button", class: "btn small", title: `${d > 0 ? "Later" : "Earlier"} by ${Math.abs(d)} s`, onclick: () => set(draft[which] + d) },
            `${d > 0 ? "+" : "−"}${Math.abs(d)}`),
        ),
      ),
      h(
        "div",
        { class: "edit-nudges" },
        h("button", { type: "button", class: "btn small", disabled: !playable, title: `Set to where the preview is (${key})`, onclick: () => set(position()) }, `Set to now (${key})`),
        h("button", { type: "button", class: "btn small", disabled: !playable, title: "Move the preview here", onclick: () => seek(draft[which]) }, "Go to"),
      ),
    );
    return {
      box,
      set,
      show() {
        if (document.activeElement !== input) input.value = formatTime(draft[which]);
        input.classList.remove("invalid");
        clearTimeout(timer);
        const url = `/api/items/${item.id}/frame?at=${draft[which].toFixed(1)}`;
        if (still.getAttribute("src") === url) return;
        timer = setTimeout(() => {
          stillBox.classList.add("loading");
          still.src = url;
        }, STILL_DELAY);
      },
      stop: () => clearTimeout(timer),
    };
  }
  const start = endpoint("start", "Start", "I");
  const end = endpoint("end", "End", "O");

  const length = h("span", { class: "edit-length" });
  const labelInput = h("input", { type: "text", maxlength: 100, placeholder: "Label (optional)", "aria-label": "Label" });
  labelInput.addEventListener("input", () => (draft.label = labelInput.value));
  const kindButtons = Object.entries(KINDS).map(([kind, { name, hint }]) =>
    h("button", { type: "button", class: "seg", title: hint, "data-kind": kind, onclick: () => ((draft.kind = kind), showDraft()) }, name),
  );
  const kindHint = h("p", { class: "edit-hint" });
  const saveBtn = h("button", { type: "button", class: "btn primary", onclick: save });
  const newBtn = h("button", { type: "button", class: "btn", onclick: () => edit(null) }, "New range");
  const previewBtn = h("button", { type: "button", class: "btn", disabled: !playable, onclick: () => playRange(draft.start, draft.end) }, "▶ Preview range");
  const error = h("p", { class: "error", hidden: true, role: "alert" });
  const formTitle = h("h3", {});

  function showError(text) {
    error.textContent = text;
    error.hidden = !text;
  }

  function showDraft() {
    start.show();
    end.show();
    const size = draft.end - draft.start;
    length.textContent = size > 0 ? `Length ${formatTime(size)}` : "The end is before the start";
    length.classList.toggle("bad", size <= 0);
    selection.style.left = pct(Math.min(draft.start, draft.end));
    selection.style.width = `calc(${pct(Math.max(draft.start, draft.end))} - ${pct(Math.min(draft.start, draft.end))})`;
    selection.className = `edit-selection ${draft.kind}`;
    for (const b of kindButtons) b.setAttribute("aria-pressed", String(b.dataset.kind === draft.kind));
    kindHint.textContent = KINDS[draft.kind].hint;
    formTitle.textContent = draft.id ? `Editing: ${rangeName(draft)}` : "New range";
    saveBtn.textContent = draft.id ? "Save changes" : "Save range";
    newBtn.hidden = !draft.id;
  }

  /** Load a saved range into the form (null: start a new one here). */
  function edit(range) {
    if (range) Object.assign(draft, { id: range.id, start: range.start, end: range.end, kind: range.kind, label: range.label });
    else Object.assign(draft, { id: null, label: "", kind: "range" });
    labelInput.value = draft.label;
    showError("");
    showDraft();
    showList();
    if (range) seek(range.start);
  }

  let saving = false;
  async function save() {
    if (saving) return;
    saving = true;
    saveBtn.disabled = true;
    const body = { start: draft.start, end: draft.end, kind: draft.kind, label: draft.label };
    try {
      const before = new Set(ranges.map((r) => r.id));
      ranges = draft.id
        ? await api("PUT", `/api/items/${item.id}/ranges/${draft.id}`, body)
        : await api("POST", `/api/items/${item.id}/ranges`, body);
      // A new range stays selected, so it can be adjusted and saved again.
      draft.id ??= ranges.find((r) => !before.has(r.id))?.id ?? null;
      const stored = ranges.find((r) => r.id === draft.id);
      if (stored) Object.assign(draft, { start: stored.start, end: stored.end, label: stored.label });
      labelInput.value = draft.label;
      showError("");
      showDraft();
      showList();
    } catch (err) {
      showError(err.message);
    } finally {
      saving = false;
      saveBtn.disabled = false;
    }
  }

  // ---- Saved ranges ----
  const list = h("ul", { class: "range-list" });
  const empty = h("p", { class: "edit-hint" }, "Nothing saved yet. Set a start and an end above, then save.");
  let confirming = null; // the range whose Delete was pressed once (it asks again for 4 s)

  async function remove(range) {
    if (confirming !== range.id) {
      confirming = range.id;
      showList();
      setTimeout(() => confirming === range.id && ((confirming = null), showList()), 4000);
      return;
    }
    try {
      ranges = await api("DELETE", `/api/items/${item.id}/ranges/${range.id}`);
      confirming = null;
      if (draft.id === range.id) draft.id = null;
      showDraft();
      showList();
    } catch (err) {
      showError(err.message);
    }
  }

  function showList() {
    empty.hidden = ranges.length > 0;
    fill(
      list,
      ranges.map((r) =>
        h(
          "li",
          { class: `range-row${r.id === draft.id ? " selected" : ""}` },
          h("span", { class: `kind-badge ${r.kind}` }, KINDS[r.kind]?.name || r.kind),
          h(
            "div",
            { class: "range-what" },
            h("div", { class: "range-name" }, rangeName(r)),
            h("div", { class: "range-times" }, `${formatTime(r.start)} – ${formatTime(r.end)} · ${formatDuration(r.end - r.start) || "0:00"}`),
          ),
          h(
            "div",
            { class: "range-actions" },
            h("button", { type: "button", class: "btn small", onclick: () => edit(r) }, "Edit"),
            h("button", { type: "button", class: "btn small", disabled: !playable, onclick: () => playRange(r.start, r.end) }, "▶ Preview"),
            r.kind === "clip" && h("a", { class: "btn small", href: `#/play/${item.id}?clip=${r.id}` }, "Open clip"),
            h(
              "button",
              { type: "button", class: "btn small danger", onclick: () => remove(r) },
              confirming === r.id ? "Really delete?" : "Delete",
            ),
          ),
        ),
      ),
    );
    fill(
      saved,
      ranges.map((r) => h("div", { class: `edit-band ${r.kind}`, style: `left:${pct(r.start)};width:calc(${pct(r.end)} - ${pct(r.start)})`, title: rangeName(r) })),
    );
  }

  // ---- Keys (not while typing) ----
  function onKey(e) {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
    const step = e.shiftKey ? 10 : 1;
    const keys = {
      " ": togglePlay,
      k: togglePlay,
      ArrowLeft: () => seek(position() - step),
      ArrowRight: () => seek(position() + step),
      ",": () => seek(position() - 0.1),
      ".": () => seek(position() + 0.1),
      i: () => playable && start.set(position()),
      o: () => playable && end.set(position()),
    };
    const action = keys[e.key.length === 1 ? e.key.toLowerCase() : e.key];
    if (!action) return;
    e.preventDefault();
    action();
  }
  document.addEventListener("keydown", onKey);

  fill(
    view,
    h(
      "div",
      { class: "page-head" },
      h(
        "nav",
        { class: "crumbs" },
        h("a", { href: `#/item/${item.id}` }, item.title),
        h("span", { class: "sep", "aria-hidden": "true" }, "›"),
        h("span", { class: "current" }, "Edit"),
      ),
      h("a", { class: "btn small", href: `#/item/${item.id}` }, "Done"),
    ),
    h(
      "p",
      { class: "summary" },
      "Mark stretches of this video to skip when it plays, keep as clips, or just find again. The file itself is never changed.",
    ),
    h(
      "section",
      { class: "edit-preview" },
      h("div", { class: "edit-screen", hidden: !playable }, video),
      message,
      h("div", { class: "edit-controls" }, playBtn, clock, h("span", { class: "edit-keys" }, "Space play · ←/→ 1 s (Shift 10 s) · , . 0.1 s · I start · O end")),
      timeline,
    ),
    h(
      "section",
      { class: "edit-form" },
      formTitle,
      h("div", { class: "edit-ends" }, start.box, end.box),
      h("div", { class: "edit-meta" }, length, labelInput),
      h("div", { class: "segmented", role: "group", "aria-label": "What the range does" }, kindButtons),
      kindHint,
      error,
      h("div", { class: "edit-actions" }, saveBtn, previewBtn, newBtn),
    ),
    h("section", { class: "edit-list" }, h("h3", {}, "Saved"), empty, list),
  );
  showDraft();
  showList();
  showPosition();

  return () => {
    document.removeEventListener("keydown", onKey);
    start.stop();
    end.stop();
    // Dropping the source closes the connection, which stops ffmpeg on the server.
    source?.destroy();
  };
}
