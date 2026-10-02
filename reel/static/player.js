// Full-page video player.
//
// The server decides how each video is sent (plan.py); sources.js hides the
// difference between a file, a progressive stream and HLS from the controls.
import { api, formatDuration, h } from "./api.js";
import { capabilities, capsQuery, hlsSupport } from "./caps.js";
import { makeSource } from "./sources.js";
import { parseVtt, textAt } from "./vtt.js";

const HIDE_CONTROLS_AFTER = 3000;
const BADGES = { remux: "Repackaging", audio: "Converting audio", transcode: "Converting" };
const SKIP_SECONDS = 10; // arrow keys
const JUMP_SECONDS = 60; // the 1-minute buttons, and Shift + arrow keys
const SUBTITLES_KEY = "reel.subtitles"; // "off" once they're turned off (remembered in this browser)

/** Whether subtitles are on: they are, unless turned off in this browser. */
export function subtitlesWanted(storage = globalThis.localStorage) {
  try {
    return storage?.getItem(SUBTITLES_KEY) !== "off";
  } catch {
    return true; // private mode: on, and not remembered
  }
}

function rememberSubtitles(on, storage = globalThis.localStorage) {
  try {
    storage?.setItem(SUBTITLES_KEY, on ? "on" : "off");
  } catch {
    /* not remembered */
  }
}

/** Does the video have subtitles (made by Reel, or a file beside it)? */
export function hasSubtitles(item) {
  return Boolean(item.subtitles || item.subtitle_file);
}

// The player that owns the page-wide state (the "playing" look). A replaced player
// whose loading finishes late is cleaned up then, and must not undo the current one's.
let activePlayer = null;

// ---- Full screen, in whatever form the browser offers ----
// Desktop browsers, Android and iPad put any element (our player, with its own
// controls) full screen, some only with the older webkit- names. Safari on
// iPhone can't: only the <video> itself goes full screen, in iOS's own player.

/** Whether the player (or, on iPhone, its video) is full screen now. */
export function isFullscreen(player, video, doc = globalThis.document) {
  const element = doc.fullscreenElement || doc.webkitFullscreenElement || null;
  return element === player || Boolean(video.webkitDisplayingFullscreen);
}

export function enterFullscreen(player, video) {
  if (player.requestFullscreen) {
    // A promise in current browsers; if it's refused, try the video on its own.
    return Promise.resolve(player.requestFullscreen()).catch(() => videoFullscreen(video));
  }
  if (player.webkitRequestFullscreen) return player.webkitRequestFullscreen();
  return videoFullscreen(video); // iPhone
}

function videoFullscreen(video) {
  try {
    video.webkitEnterFullscreen?.();
  } catch {
    // Not ready yet (iOS needs the video's metadata first): nothing to do.
  }
}

export function exitFullscreen(player, video, doc = globalThis.document) {
  if (doc.fullscreenElement === player) doc.exitFullscreen();
  else if (doc.webkitFullscreenElement === player) doc.webkitExitFullscreen();
  else if (video.webkitDisplayingFullscreen) video.webkitExitFullscreen();
}

/** Where a seek to `t` actually lands: never before the start or past the end. */
export function clampSeek(t, duration) {
  return Math.max(0, Math.min(t, duration - 1));
}

// Line icons on a 24px grid, drawn in the current text color.
const ICONS = {
  play: '<path d="M8 5.2v13.6a.8.8 0 0 0 1.2.7l10.9-6.8a.8.8 0 0 0 0-1.4L9.2 4.5A.8.8 0 0 0 8 5.2z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="6.5" y="5" width="3.6" height="14" rx="1.2" fill="currentColor" stroke="none"/><rect x="13.9" y="5" width="3.6" height="14" rx="1.2" fill="currentColor" stroke="none"/>',
  start: '<path d="M6 5v14"/><path d="M18.5 6.1v11.8a.7.7 0 0 1-1.1.6L9.3 12.6a.7.7 0 0 1 0-1.2l8.1-5.9a.7.7 0 0 1 1.1.6z" fill="currentColor" stroke="none"/>',
  back: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.5 3.8v4.6h4.6"/><text x="12.3" y="15.6" font-size="8.2" font-weight="800" text-anchor="middle" fill="currentColor" stroke="none" font-family="system-ui, sans-serif">1m</text>',
  forward: '<path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><path d="M20.5 3.8v4.6h-4.6"/><text x="11.7" y="15.6" font-size="8.2" font-weight="800" text-anchor="middle" fill="currentColor" stroke="none" font-family="system-ui, sans-serif">1m</text>',
  volume: '<path d="M4 9.5h3.2L12 5.5v13l-4.8-4H4z" fill="currentColor" stroke="none"/><path d="M15.5 9a4.2 4.2 0 0 1 0 6"/><path d="M18 6.5a7.8 7.8 0 0 1 0 11"/>',
  muted: '<path d="M4 9.5h3.2L12 5.5v13l-4.8-4H4z" fill="currentColor" stroke="none"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
  expand: '<path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15"/>',
  shrink: '<path d="M9 4v3.5A1.5 1.5 0 0 1 7.5 9H4M20 9h-3.5A1.5 1.5 0 0 1 15 7.5V4M15 20v-3.5a1.5 1.5 0 0 1 1.5-1.5H20M4 15h3.5A1.5 1.5 0 0 1 9 16.5V20"/>',
  chevron: '<path d="M14.5 5.5L8 12l6.5 6.5"/>',
  prevMark: '<path d="M6 5v14"/><path d="M19 12h-9M13.5 8l-4 4 4 4"/>',
  nextMark: '<path d="M18 5v14"/><path d="M5 12h9M10.5 8l4 4-4 4"/>',
  mark: '<path d="M7 4.5h10a1 1 0 0 1 1 1v14l-6-3.8-6 3.8v-14a1 1 0 0 1 1-1z"/><path d="M12 8v5M9.5 10.5h5"/>',
  cc: '<rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M10.6 10.2a2.3 2.3 0 1 0 0 3.6M17.4 10.2a2.3 2.3 0 1 0 0 3.6"/>',
  camera: '<path d="M4 8.5A1.5 1.5 0 0 1 5.5 7h2.2l1.5-2h5.6l1.5 2h2.2A1.5 1.5 0 0 1 20 8.5v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z"/><circle cx="12" cy="13" r="3.3"/>',
};

function icon(name) {
  const t = document.createElement("template");
  t.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  return t.content.firstChild;
}

function iconButton(name, label, shortcut, extraClass = "") {
  return h(
    "button",
    { type: "button", class: `pbtn ${extraClass}`.trim(), "aria-label": label, title: shortcut ? `${label} (${shortcut})` : label },
    icon(name),
  );
}

/** The first mark after `position` (marks are {time}, earliest first), or null. */
export function nextMark(marks, position) {
  return marks.find((m) => m.time > position + 0.5) || null;
}

/** The mark before `position`, or null. Just after a mark (within 2 s), it's the
 *  one before that, so pressing again keeps going back (like a music player). */
export function prevMark(marks, position) {
  return [...marks].reverse().find((m) => m.time < position - 2) || null;
}

/** Where to start a clip: `start` if it's inside the clip, else the clip's start. */
export function clipStart(clip, start) {
  return start > clip.start && start < clip.end ? start : clip.start;
}

/** "#/play/<id>?t=335": where to start, in seconds (0 if not given). */
export function startTime(query) {
  const t = Number(new URLSearchParams(query || "").get("t"));
  return Number.isFinite(t) && t > 0 ? t : 0;
}

/** `backUrl`: where the back button goes (the video's page, keeping the list it was opened from).
 *  `clipId`: play just that clip (made on the edit page), as if it were a video. */
export async function renderPlayer(page, itemId, start = 0, backUrl = `#/item/${itemId}`, clipId = null) {
  // Ask the server how *this* browser should play it (see plan.py).
  const caps = await capabilities();
  const [item, plan] = await Promise.all([
    api("GET", `/api/items/${itemId}`),
    api("GET", `/api/items/${itemId}/plan?${capsQuery(caps)}&hls_support=${hlsSupport()}`),
  ]);
  if (plan.mode === "unsupported" || item.missing) throw new Error("This video can't be played.");
  // A clip is a window on the video: the seek bar, clock and controls cover just
  // that stretch, and it stops at its end.
  const clip = clipId ? (item.clips || []).find((c) => c.id === clipId) : null;
  if (clipId && !clip) throw new Error("This clip doesn't exist any more.");
  const from = clip ? clip.start : 0;

  const streamed = plan.streamed;
  let dragTime = null; // while dragging the seek bar: where it would seek to
  let hideTimer = null;

  const video = h("video", { class: "screen", autoplay: true, playsinline: true });
  const spinner = h("div", { class: "spinner", hidden: true });
  const flash = h("div", { class: "flash" });
  const message = h("div", { class: "player-message", role: "alert", hidden: true });
  // A stopped video says why, with Retry: it starts again where it was.
  function showError(text) {
    spinner.hidden = true;
    message.hidden = false;
    message.replaceChildren(
      h("span", {}, text),
      h("button", { type: "button", class: "btn small retry", onclick: retry }, "Retry"),
    );
  }
  function retry() {
    message.hidden = true;
    spinner.hidden = false;
    load(position());
  }

  const startBtn = iconButton("start", "Go to beginning", "Home");
  const backBtn = iconButton("back", "Back 1 minute", "Shift + ←", "jump");
  const playBtn = iconButton("play", "Play", "Space", "primary");
  const forwardBtn = iconButton("forward", "Forward 1 minute", "Shift + →", "jump");
  const muteBtn = iconButton("volume", "Mute", "M");
  const fullBtn = iconButton("expand", "Full screen", "F");
  const snapBtn = iconButton("camera", "Use this frame as the preview", "P");
  const markBtn = iconButton("mark", "Mark this spot");
  const prevMarkBtn = iconButton("prevMark", "Previous mark");
  const nextMarkBtn = iconButton("nextMark", "Next mark");
  const ccBtn = iconButton("cc", "Subtitles", "C");
  // The subtitles, drawn by the player from its own clock (see showSubtitles).
  const subtitleBox = h("div", { class: "subtitles", hidden: true, "aria-live": "off" });
  const toast = h("div", { class: "player-toast", role: "status", "aria-live": "polite", hidden: true });
  const volume = h("input", { type: "range", class: "volume", min: 0, max: 1, step: 0.05, value: 1, "aria-label": "Volume" });

  const timeNow = h("span", { class: "clock now" }, "0:00");
  const timeTotal = h("span", { class: "clock total" }, "–");

  // Seek bar: buffered strip, gradient fill, glowing knob, and a time bubble.
  const buffered = h("div", { class: "seek-buffer" });
  const fillBar = h("div", { class: "seek-fill" });
  const knob = h("div", { class: "seek-knob" });
  const tip = h("div", { class: "seek-tip" });
  // Your marks, as ticks on the seek bar: click one to go there.
  const ticks = h("div", { class: "seek-marks" });
  const seekBar = h(
    "div",
    { class: "seek", role: "slider", tabindex: 0, "aria-label": "Seek", "aria-valuemin": 0 },
    h("div", { class: "seek-rail" }, buffered, fillBar),
    ticks,
    knob,
    tip,
  );

  const dock = h(
    "div",
    { class: "dock" },
    seekBar,
    h(
      "div",
      { class: "dock-row" },
      h("div", { class: "dock-side" }, timeNow),
      h("div", { class: "dock-center" }, startBtn, backBtn, playBtn, forwardBtn),
      h("div", { class: "dock-side right" }, timeTotal, h("div", { class: "vol" }, muteBtn, volume), prevMarkBtn, markBtn, nextMarkBtn, ccBtn, snapBtn, fullBtn),
    ),
  );
  const backLink = h("a", { class: "pbtn glass", href: backUrl, "aria-label": "Back", title: "Back" }, icon("chevron"));
  const top = h(
    "div",
    { class: "player-top" },
    backLink,
    h(
      "div",
      { class: "player-heading" },
      h("span", { class: "player-title" }, clip ? `${item.title} · ${clip.name}` : item.title),
      streamed && h("span", { class: "badge" }, h("i", { class: "pulse" }), BADGES[plan.mode] || "Converting"),
    ),
  );
  const player = h("div", { class: "player" }, video, h("div", { class: "scrim" }), subtitleBox, flash, spinner, message, toast,
    top, dock);
  page.replaceChildren(player);
  // Only the current page takes the page-wide state: a replaced one (the router
  // has detached it) is about to be cleaned up.
  if (page.isConnected) {
    activePlayer = player;
    document.body.classList.add("playing");
  }
  const releasePage = () => {
    if (activePlayer !== player) return; // a newer player owns it now
    activePlayer = null;
    document.body.classList.remove("playing");
  };

  // How the video arrives (file, progressive stream or HLS): see sources.js.
  let source;
  try {
    source = await makeSource(
      video,
      plan,
      (text) => showError(text),
      { nativeHls: hlsSupport() === "native" },
    );
  } catch (err) {
    releasePage(); // failed before a cleanup was handed back: undo what was set
    throw err;
  }
  const duration = () =>
    plan.delivery === "progressive" || !Number.isFinite(video.duration) ? item.duration || 0 : video.duration;
  const position = () => source.position();
  // The end of what's shown: the clip's end, or the video's.
  const until = () => (clip ? Math.min(clip.end, duration() || clip.end) : duration());

  function load(start) {
    source.load(start);
    video.play().catch(() => {}); // autoplay may be blocked until the user clicks
  }

  function seek(t) {
    source.seek(clip ? Math.max(from, Math.min(t, clip.end - 0.5)) : clampSeek(t, duration()));
    updateTime();
    showSubtitles();
  }

  // Seek bar positions and the clock are measured from `from` (a clip's start).
  const pct = (t) => {
    const total = until() - from;
    return total > 0 ? `${Math.min(100, Math.max(0, ((t - from) / total) * 100))}%` : "0%";
  };

  function updateTime() {
    const total = until() - from;
    const shown = dragTime ?? position();
    timeNow.textContent = formatDuration(shown - from) || "0:00";
    timeTotal.textContent = formatDuration(total) || "–";
    fillBar.style.width = pct(shown);
    knob.style.left = pct(shown);
    buffered.style.width = pct(source.bufferedEnd());
    seekBar.setAttribute("aria-valuemax", Math.round(total));
    seekBar.setAttribute("aria-valuenow", Math.round(shown - from));
    seekBar.setAttribute("aria-valuetext", timeNow.textContent);
  }

  function setIcon(button, name) {
    button.replaceChildren(icon(name));
  }

  // A short note at the top of the screen, e.g. "Preview updated".
  let toastTimer = null;
  function showToast(text, tone = "", stay = false) {
    clearTimeout(toastTimer);
    toast.textContent = text;
    toast.className = `player-toast ${tone}`.trim();
    toast.hidden = false;
    if (!stay) toastTimer = setTimeout(() => (toast.hidden = true), 3000);
  }

  // Use the frame on screen as the video's preview (replacing any): the server
  // takes it from the original file at this exact time.
  let snapping = false;
  async function takeSnapshot() {
    if (snapping) return;
    snapping = true;
    snapBtn.disabled = true;
    video.pause();
    const time = Math.max(0, position());
    showToast("Saving preview…", "", true);
    try {
      await api("POST", `/api/items/${item.id}/snapshot`, { time });
      flashIcon("camera");
      showToast(`Preview updated (${formatDuration(time) || "0:00"})`, "ok");
    } catch (err) {
      showToast(err.message, "err");
    } finally {
      snapping = false;
      snapBtn.disabled = false;
    }
  }

  // ---- Marks: spots to jump back to (deleted from the video's page) ----
  let marks = item.marks || [];
  // Previous/next: hidden without marks; disabled when there's none that way.
  function updateMarkButtons() {
    prevMarkBtn.hidden = nextMarkBtn.hidden = marks.length === 0;
    const at = position();
    prevMarkBtn.disabled = !prevMark(marks, at);
    nextMarkBtn.disabled = !nextMark(marks, at);
  }
  function goToMark(mark) {
    if (!mark) return;
    seek(mark.time);
    showToast(`Mark ${formatDuration(mark.time) || "0:00"}`);
    updateMarkButtons();
  }

  function showMarks() {
    updateMarkButtons();
    const total = duration();
    const shown = marks.filter((m) => m.time >= from && m.time <= until());
    ticks.replaceChildren(
      ...(total ? shown : []).map((m) =>
        h(
          "button",
          {
            type: "button",
            class: "seek-mark",
            style: `left:${pct(m.time)}`,
            title: `Go to ${formatDuration(m.time - from) || "0:00"}`,
            "aria-label": `Go to mark at ${formatDuration(m.time - from) || "0:00"}`,
            // Its own click, not the seek bar's drag: go exactly to the mark.
            onpointerdown: (e) => e.stopPropagation(),
            onclick: (e) => {
              e.stopPropagation();
              seek(m.time);
            },
          },
        ),
      ),
    );
  }
  let marking = false;
  async function addMark() {
    if (marking) return;
    marking = true;
    const time = Math.max(0, position());
    try {
      marks = await api("POST", `/api/items/${item.id}/marks`, { time });
      showMarks();
      showToast(`Marked ${formatDuration(time) || "0:00"}`, "ok");
    } catch (err) {
      showToast(err.message, "err");
    } finally {
      marking = false;
    }
  }

  // ---- Subtitles: fetched once, then the line for the player's position is shown.
  // Drawn here rather than by the browser's own <track>: a repackaged stream's clock
  // restarts at each seek, but position() is always the video's real time (clips too).
  let cues = [];
  let subtitlesOn = subtitlesWanted();
  let shownText = null;
  let frame = null;
  ccBtn.hidden = !hasSubtitles(item);

  function showSubtitles() {
    const text = subtitlesOn && cues.length ? textAt(cues, position()) : "";
    if (text === shownText) return;
    shownText = text;
    subtitleBox.hidden = !text;
    subtitleBox.replaceChildren(...text.split("\n").filter(Boolean).map((line) => h("span", {}, line)));
  }
  // Smoother than timeupdate's few updates a second: every frame while it plays.
  function followSubtitles() {
    cancelAnimationFrame(frame);
    showSubtitles();
    if (!video.paused && subtitlesOn && cues.length) frame = requestAnimationFrame(followSubtitles);
  }
  function showCcState() {
    ccBtn.classList.toggle("on", subtitlesOn);
    ccBtn.setAttribute("aria-pressed", String(subtitlesOn));
    ccBtn.setAttribute("aria-label", subtitlesOn ? "Turn subtitles off" : "Turn subtitles on");
    ccBtn.title = `${subtitlesOn ? "Turn subtitles off" : "Turn subtitles on"} (C)`;
  }
  function toggleSubtitles() {
    if (ccBtn.hidden) return;
    subtitlesOn = !subtitlesOn;
    rememberSubtitles(subtitlesOn);
    showCcState();
    showToast(subtitlesOn ? "Subtitles on" : "Subtitles off");
    followSubtitles();
  }
  async function loadSubtitles() {
    try {
      const res = await fetch(`/api/items/${item.id}/subtitles.vtt`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      cues = parseVtt(await res.text());
      followSubtitles();
    } catch (err) {
      ccBtn.hidden = true; // nothing to show after all
      console.warn("Reel: couldn't load subtitles", err);
    }
  }
  // Keep them clear of the controls: they sit just above the dock while it shows.
  const dockSize = new ResizeObserver(() => player.style.setProperty("--dock-height", `${dock.offsetHeight}px`));
  dockSize.observe(dock);
  showCcState();
  if (!ccBtn.hidden) loadSubtitles();

  function flashIcon(name) {
    flash.replaceChildren(icon(name));
    flash.classList.remove("show");
    void flash.offsetWidth; // restart the animation
    flash.classList.add("show");
  }

  function togglePlay() {
    if (video.paused) {
      if (clip && position() >= clip.end - 0.3) seek(from); // at the clip's end: play it again
      video.play().catch(() => {});
    } else video.pause();
  }

  // A clip stops at its end.
  function stopAtClipEnd() {
    if (clip && position() >= clip.end - 0.05 && !video.paused) {
      video.pause();
      showToast("End of clip");
    }
  }

  function toggleFullscreen() {
    if (isFullscreen(player, video)) exitFullscreen(player, video);
    else enterFullscreen(player, video);
  }

  function showControls() {
    player.classList.remove("idle");
    clearTimeout(hideTimer);
    if (!video.paused && dragTime === null) {
      hideTimer = setTimeout(() => player.classList.add("idle"), HIDE_CONTROLS_AFTER);
    }
  }

  // ---- Seek bar dragging and hover time ----
  const timeAt = (clientX) => {
    const rect = seekBar.getBoundingClientRect();
    return from + (Math.min(Math.max(clientX - rect.left, 0), rect.width) / rect.width) * (until() - from);
  };
  const showTip = (clientX) => {
    const rect = seekBar.getBoundingClientRect();
    const x = Math.min(Math.max(clientX - rect.left, 0), rect.width);
    tip.textContent = formatDuration(timeAt(clientX) - from) || "0:00";
    tip.style.left = `${x}px`;
  };
  seekBar.addEventListener("pointermove", (e) => {
    showTip(e.clientX);
    if (dragTime !== null) {
      dragTime = timeAt(e.clientX);
      updateTime();
    }
  });
  seekBar.addEventListener("pointerdown", (e) => {
    seekBar.setPointerCapture(e.pointerId);
    seekBar.classList.add("dragging");
    dragTime = timeAt(e.clientX);
    showTip(e.clientX);
    updateTime();
  });
  const endDrag = () => {
    if (dragTime === null) return;
    const target = dragTime;
    dragTime = null;
    seekBar.classList.remove("dragging");
    seek(target);
    showControls();
  };
  seekBar.addEventListener("pointerup", endDrag);
  seekBar.addEventListener("pointercancel", endDrag);

  // ---- Video events ----
  video.addEventListener("click", togglePlay);
  video.addEventListener("dblclick", toggleFullscreen);
  video.addEventListener("play", () => {
    setIcon(playBtn, "pause");
    playBtn.setAttribute("aria-label", "Pause");
    flashIcon("play");
    showControls();
  });
  video.addEventListener("pause", () => {
    setIcon(playBtn, "play");
    playBtn.setAttribute("aria-label", "Play");
    flashIcon("pause");
    showControls();
  });
  video.addEventListener("timeupdate", updateTime);
  video.addEventListener("timeupdate", stopAtClipEnd);
  video.addEventListener("progress", updateTime);
  video.addEventListener("durationchange", updateTime);
  video.addEventListener("waiting", () => (spinner.hidden = false));
  video.addEventListener("playing", () => ((spinner.hidden = true), (message.hidden = true)));
  video.addEventListener("canplay", () => (spinner.hidden = true));
  video.addEventListener("ended", showControls);
  video.addEventListener("volumechange", () => {
    const silent = video.muted || video.volume === 0;
    setIcon(muteBtn, silent ? "muted" : "volume");
    volume.value = video.muted ? 0 : video.volume;
    volume.style.setProperty("--level", `${Number(volume.value) * 100}%`);
  });
  video.addEventListener("error", () => showError("This video couldn't be played."));
  function onFullscreen() {
    setIcon(fullBtn, isFullscreen(player, video) ? "shrink" : "expand");
  }
  document.addEventListener("fullscreenchange", onFullscreen);
  document.addEventListener("webkitfullscreenchange", onFullscreen);
  // iPhone: the video's own full screen.
  video.addEventListener("webkitbeginfullscreen", onFullscreen);
  video.addEventListener("webkitendfullscreen", onFullscreen);

  // ---- Buttons ----
  playBtn.addEventListener("click", togglePlay);
  startBtn.addEventListener("click", () => seek(from));
  backBtn.addEventListener("click", () => seek(position() - JUMP_SECONDS));
  forwardBtn.addEventListener("click", () => seek(position() + JUMP_SECONDS));
  fullBtn.addEventListener("click", toggleFullscreen);
  snapBtn.addEventListener("click", takeSnapshot);
  markBtn.addEventListener("click", addMark);
  prevMarkBtn.addEventListener("click", () => goToMark(prevMark(marks, position())));
  nextMarkBtn.addEventListener("click", () => goToMark(nextMark(marks, position())));
  video.addEventListener("timeupdate", updateMarkButtons);
  video.addEventListener("timeupdate", showSubtitles);
  video.addEventListener("play", followSubtitles);
  video.addEventListener("seeked", followSubtitles);
  ccBtn.addEventListener("click", toggleSubtitles);
  video.addEventListener("durationchange", showMarks);
  muteBtn.addEventListener("click", () => (video.muted = !video.muted));
  volume.addEventListener("input", () => {
    video.volume = Number(volume.value);
    video.muted = video.volume === 0;
  });
  volume.style.setProperty("--level", "100%");
  player.addEventListener("mousemove", showControls);

  function onKey(e) {
    if (e.target.tagName === "INPUT" && e.target.type !== "range") return;
    const keys = {
      " ": togglePlay,
      k: togglePlay,
      f: toggleFullscreen,
      m: () => (video.muted = !video.muted),
      p: takeSnapshot,
      c: toggleSubtitles,
      Home: () => seek(from),
      ArrowLeft: () => seek(position() - (e.shiftKey ? JUMP_SECONDS : SKIP_SECONDS)),
      ArrowRight: () => seek(position() + (e.shiftKey ? JUMP_SECONDS : SKIP_SECONDS)),
    };
    const action = keys[e.key];
    if (!action) return;
    e.preventDefault();
    action();
    showControls();
  }
  document.addEventListener("keydown", onKey);

  spinner.hidden = false;
  load(clip ? clipStart(clip, start) : start);
  updateTime();
  showMarks();

  return () => {
    document.removeEventListener("keydown", onKey);
    cancelAnimationFrame(frame);
    dockSize.disconnect();
    document.removeEventListener("fullscreenchange", onFullscreen);
    document.removeEventListener("webkitfullscreenchange", onFullscreen);
    releasePage();
    clearTimeout(hideTimer);
    if (isFullscreen(player, video)) exitFullscreen(player, video);
    // Dropping the source closes the connection, which stops ffmpeg on the server.
    source.destroy();
  };
}
