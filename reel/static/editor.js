// A video's edit page: make clips while it plays.
//
// Play the video and press Mark where a clip should start and again where it
// should end (never more than two marks: Clear marks starts over). With two
// marks, Make clip saves the stretch between them as "Clip 1", "Clip 2", ...
// and clears the marks for the next one. Nothing here changes the file (Reel
// never writes to your videos): clips are saved in Reel's database.
import { api, formatTime, h } from "./api.js";
import { capabilities, capsQuery, hlsSupport } from "./caps.js";
import { clipsSection } from "./clips.js";
import { makeSource } from "./sources.js";

export const MIN_CLIP = 0.5; // seconds, as the server requires

/** What the edit page says, for these marks (seconds, in the order pressed). */
export function markStatus(marks) {
  if (marks.length === 0) return "Play the video and press Mark where a clip should start.";
  if (marks.length === 1) return `Marked ${formatTime(marks[0])}. Press Mark again where the clip should end.`;
  const [start, end] = [Math.min(...marks), Math.max(...marks)];
  if (end - start < MIN_CLIP) return `The marks are only ${formatTime(end - start)} apart; a clip is at least ${MIN_CLIP} seconds. Clear marks and try again.`;
  return `${formatTime(start)} – ${formatTime(end)} (${formatTime(end - start)}). Make clip, or Clear marks to start over.`;
}

/** Whether these marks make a clip: two of them, far enough apart. */
export function canMakeClip(marks) {
  return marks.length === 2 && Math.abs(marks[1] - marks[0]) >= MIN_CLIP;
}

export async function renderEditor(view, itemId) {
  const caps = await capabilities();
  const [item, plan] = await Promise.all([
    api("GET", `/api/items/${itemId}`),
    api("GET", `/api/items/${itemId}/plan?${capsQuery(caps)}&hls_support=${hlsSupport()}`),
  ]);
  if (item.missing) throw new Error("This video is missing from the library, so clips can't be made.");
  if (plan.mode === "unsupported") throw new Error("This video can't play in this browser, so clips can't be made here.");
  const total = item.duration || 0;
  if (!total) throw new Error("This video's length is unknown, so clips can't be made.");

  let clips = item.clips || [];
  let marks = []; // at most two, in the order pressed

  const video = h("video", { class: "edit-video", playsinline: true, preload: "auto" });
  const message = h("p", { class: "error", hidden: true, role: "alert" });
  const playBtn = h("button", { type: "button", class: "btn", title: "Play / pause (Space)" }, "▶ Play");
  const markBtn = h("button", { type: "button", class: "btn mark-btn", title: "Mark this moment (M)" }, "Mark");
  const clearBtn = h("button", { type: "button", class: "btn", title: "Clear the marks" }, "Clear marks");
  const makeBtn = h("button", { type: "button", class: "btn primary", title: "Make a clip between the two marks" }, "Make clip");
  const clock = h("span", { class: "edit-clock" });
  const status = h("p", { class: "edit-status", role: "status", "aria-live": "polite" });

  // The timeline: the clips made so far, the marks, the stretch between them, and where the video is.
  const bands = h("div", { class: "edit-bands", "aria-hidden": "true" });
  const between = h("div", { class: "edit-between", hidden: true });
  const ticks = h("div", { class: "edit-marks", "aria-hidden": "true" });
  const head = h("div", { class: "edit-head" });
  const timeline = h(
    "div",
    { class: "edit-timeline", role: "slider", tabindex: 0, "aria-label": "Position", "aria-valuemin": 0, "aria-valuemax": Math.round(total) },
    bands,
    between,
    ticks,
    head,
  );

  const source = await makeSource(video, plan, (text) => ((message.textContent = text), (message.hidden = false)), {
    nativeHls: hlsSupport() === "native",
  });
  source.load(0);
  const position = () => Math.max(0, source.position());
  const pct = (t) => `${Math.min(100, Math.max(0, (t / total) * 100))}%`;
  const span = (a, b) => `left:${pct(Math.min(a, b))};width:calc(${pct(Math.max(a, b))} - ${pct(Math.min(a, b))})`;

  function seek(t) {
    source.seek(Math.min(Math.max(t, 0), Math.max(0, total - 0.5)));
    showPosition();
  }
  function togglePlay() {
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  }
  function showPosition() {
    const at = position();
    clock.textContent = `${formatTime(at)} / ${formatTime(total)}`;
    head.style.left = pct(at);
    timeline.setAttribute("aria-valuenow", Math.round(at));
    timeline.setAttribute("aria-valuetext", formatTime(at));
  }

  function showMarks() {
    ticks.replaceChildren(...marks.map((t) => h("div", { class: "edit-mark", style: `left:${pct(t)}`, title: formatTime(t) })));
    between.hidden = marks.length < 2;
    if (marks.length === 2) between.setAttribute("style", span(marks[0], marks[1]));
    markBtn.disabled = marks.length >= 2;
    clearBtn.disabled = marks.length === 0;
    makeBtn.disabled = !canMakeClip(marks);
    status.textContent = markStatus(marks);
  }
  function showBands() {
    bands.replaceChildren(...clips.map((c) => h("div", { class: "edit-band", style: span(c.start, c.end), title: c.name })));
  }

  function mark() {
    if (marks.length >= 2) return;
    marks.push(Math.round(position() * 10) / 10);
    message.hidden = true;
    showMarks();
  }
  function clearMarks() {
    marks = [];
    message.hidden = true;
    showMarks();
  }
  let making = false;
  async function makeClip() {
    if (making || !canMakeClip(marks)) return;
    making = true;
    makeBtn.disabled = true;
    try {
      clips = await api("POST", `/api/items/${item.id}/clips`, { start: marks[0], end: marks[1] });
      marks = [];
      list.show(clips);
      showBands();
      showMarks();
      status.textContent = `${clips[clips.length - 1].name} made. ${markStatus(marks)}`;
      message.hidden = true;
    } catch (err) {
      message.textContent = err.message;
      message.hidden = false;
      showMarks();
    } finally {
      making = false;
    }
  }

  video.addEventListener("timeupdate", showPosition);
  video.addEventListener("play", () => (playBtn.textContent = "❚❚ Pause"));
  video.addEventListener("pause", () => (playBtn.textContent = "▶ Play"));
  video.addEventListener("click", togglePlay);
  playBtn.addEventListener("click", togglePlay);
  markBtn.addEventListener("click", mark);
  clearBtn.addEventListener("click", clearMarks);
  makeBtn.addEventListener("click", makeClip);

  // Click or drag on the timeline to move there.
  const timeAt = (clientX) => {
    const rect = timeline.getBoundingClientRect();
    return (Math.min(Math.max(clientX - rect.left, 0), rect.width) / rect.width) * total;
  };
  let dragging = false;
  timeline.addEventListener("pointerdown", (e) => {
    dragging = true;
    timeline.setPointerCapture(e.pointerId);
    seek(timeAt(e.clientX));
  });
  timeline.addEventListener("pointermove", (e) => dragging && seek(timeAt(e.clientX)));
  timeline.addEventListener("pointerup", () => (dragging = false));
  timeline.addEventListener("pointercancel", () => (dragging = false));

  function onKey(e) {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (document.querySelector("dialog[open]")) return; // asking about a delete
    const step = e.shiftKey ? 10 : 1;
    const keys = {
      " ": togglePlay,
      k: togglePlay,
      m: mark,
      ArrowLeft: () => seek(position() - step),
      ArrowRight: () => seek(position() + step),
    };
    const action = keys[e.key.length === 1 ? e.key.toLowerCase() : e.key];
    if (!action) return;
    e.preventDefault();
    action();
  }
  document.addEventListener("keydown", onKey);

  const list = clipsSection(item, clips, {
    emptyText: "No clips yet.",
    onChange: (next) => {
      clips = next;
      showBands();
    },
  });

  view.replaceChildren(
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
    h("p", { class: "summary" }, "Make clips of this video: mark where one starts and where it ends. The file itself is never changed."),
    h(
      "section",
      { class: "edit-player" },
      h("div", { class: "edit-screen" }, video),
      timeline,
      h(
        "div",
        { class: "edit-controls" },
        playBtn,
        clock,
        h("div", { class: "edit-buttons" }, markBtn, clearBtn, makeBtn),
      ),
      status,
      message,
      h("p", { class: "edit-keys" }, "Space play / pause · ← / → 1 s (Shift: 10 s) · M mark"),
    ),
    list.element,
  );
  showBands();
  showMarks();
  showPosition();

  return () => {
    document.removeEventListener("keydown", onKey);
    // Dropping the source closes the connection, which stops ffmpeg on the server.
    source.destroy();
  };
}
