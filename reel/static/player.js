// Full-page video player.
//
// The server decides how each video is sent (plan.py); sources.js hides the
// difference between a file, a progressive stream and HLS from the controls.
import { api, formatDuration, h } from "./api.js";
import { capabilities, capsQuery, hlsSupport } from "./caps.js";
import { makeSource } from "./sources.js";

const HIDE_CONTROLS_AFTER = 3000;
const BADGES = { remux: "Repackaging", audio: "Converting audio", transcode: "Converting" };
const SKIP_SECONDS = 10; // arrow keys
const JUMP_SECONDS = 60; // the 1-minute buttons, and Shift + arrow keys

// The player that owns the page-wide state (the "playing" look). A replaced player
// whose loading finishes late is cleaned up then, and must not undo the current one's.
let activePlayer = null;

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

export async function renderPlayer(page, itemId) {
  // Ask the server how *this* browser should play it (see plan.py).
  const caps = await capabilities();
  const [item, plan] = await Promise.all([
    api("GET", `/api/items/${itemId}`),
    api("GET", `/api/items/${itemId}/plan?${capsQuery(caps)}&hls_support=${hlsSupport()}`),
  ]);
  if (plan.mode === "unsupported" || item.missing) throw new Error("This video can't be played.");

  const streamed = plan.streamed;
  let dragTime = null; // while dragging the seek bar: where it would seek to
  let hideTimer = null;

  const video = h("video", { class: "screen", autoplay: true, playsinline: true });
  const spinner = h("div", { class: "spinner", hidden: true });
  const flash = h("div", { class: "flash" });
  const message = h("div", { class: "player-message", hidden: true });

  const startBtn = iconButton("start", "Go to beginning", "Home");
  const backBtn = iconButton("back", "Back 1 minute", "Shift + ←", "jump");
  const playBtn = iconButton("play", "Play", "Space", "primary");
  const forwardBtn = iconButton("forward", "Forward 1 minute", "Shift + →", "jump");
  const muteBtn = iconButton("volume", "Mute", "M");
  const fullBtn = iconButton("expand", "Full screen", "F");
  const snapBtn = iconButton("camera", "Use this frame as the preview", "P");
  const toast = h("div", { class: "player-toast", role: "status", "aria-live": "polite", hidden: true });
  const volume = h("input", { type: "range", class: "volume", min: 0, max: 1, step: 0.05, value: 1, "aria-label": "Volume" });

  const timeNow = h("span", { class: "clock now" }, "0:00");
  const timeTotal = h("span", { class: "clock total" }, "–");

  // Seek bar: buffered strip, gradient fill, glowing knob, and a time bubble.
  const buffered = h("div", { class: "seek-buffer" });
  const fillBar = h("div", { class: "seek-fill" });
  const knob = h("div", { class: "seek-knob" });
  const tip = h("div", { class: "seek-tip" });
  const seekBar = h(
    "div",
    { class: "seek", role: "slider", tabindex: 0, "aria-label": "Seek", "aria-valuemin": 0 },
    h("div", { class: "seek-rail" }, buffered, fillBar),
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
      h("div", { class: "dock-side right" }, timeTotal, h("div", { class: "vol" }, muteBtn, volume), snapBtn, fullBtn),
    ),
  );
  const backLink = h("a", { class: "pbtn glass", href: `#/item/${item.id}`, "aria-label": "Back", title: "Back" }, icon("chevron"));
  const top = h(
    "div",
    { class: "player-top" },
    backLink,
    h(
      "div",
      { class: "player-heading" },
      h("span", { class: "player-title" }, item.title),
      streamed && h("span", { class: "badge" }, h("i", { class: "pulse" }), BADGES[plan.mode] || "Converting"),
    ),
  );
  const player = h("div", { class: "player" }, video, h("div", { class: "scrim" }), flash, spinner, message, toast, top, dock);
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
      (text) => {
        spinner.hidden = true;
        message.hidden = false;
        message.textContent = text;
      },
      { nativeHls: hlsSupport() === "native" },
    );
  } catch (err) {
    releasePage(); // failed before a cleanup was handed back: undo what was set
    throw err;
  }
  const duration = () =>
    plan.delivery === "progressive" || !Number.isFinite(video.duration) ? item.duration || 0 : video.duration;
  const position = () => source.position();

  function load(start) {
    source.load(start);
    video.play().catch(() => {}); // autoplay may be blocked until the user clicks
  }

  function seek(t) {
    source.seek(clampSeek(t, duration()));
    updateTime();
  }

  const pct = (t) => {
    const total = duration();
    return total ? `${Math.min(100, Math.max(0, (t / total) * 100))}%` : "0%";
  };

  function updateTime() {
    const total = duration();
    const shown = dragTime ?? position();
    timeNow.textContent = formatDuration(shown) || "0:00";
    timeTotal.textContent = formatDuration(total) || "–";
    fillBar.style.width = pct(shown);
    knob.style.left = pct(shown);
    buffered.style.width = pct(source.bufferedEnd());
    seekBar.setAttribute("aria-valuemax", Math.round(total));
    seekBar.setAttribute("aria-valuenow", Math.round(shown));
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

  function flashIcon(name) {
    flash.replaceChildren(icon(name));
    flash.classList.remove("show");
    void flash.offsetWidth; // restart the animation
    flash.classList.add("show");
  }

  function togglePlay() {
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else player.requestFullscreen?.();
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
    return (Math.min(Math.max(clientX - rect.left, 0), rect.width) / rect.width) * duration();
  };
  const showTip = (clientX) => {
    const rect = seekBar.getBoundingClientRect();
    const x = Math.min(Math.max(clientX - rect.left, 0), rect.width);
    tip.textContent = formatDuration(timeAt(clientX)) || "0:00";
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
  video.addEventListener("error", () => {
    spinner.hidden = true;
    message.hidden = false;
    message.textContent = "This video couldn't be played.";
  });
  function onFullscreen() {
    setIcon(fullBtn, document.fullscreenElement ? "shrink" : "expand");
  }
  document.addEventListener("fullscreenchange", onFullscreen);

  // ---- Buttons ----
  playBtn.addEventListener("click", togglePlay);
  startBtn.addEventListener("click", () => seek(0));
  backBtn.addEventListener("click", () => seek(position() - JUMP_SECONDS));
  forwardBtn.addEventListener("click", () => seek(position() + JUMP_SECONDS));
  fullBtn.addEventListener("click", toggleFullscreen);
  snapBtn.addEventListener("click", takeSnapshot);
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
      Home: () => seek(0),
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
  load(0);
  updateTime();

  return () => {
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("fullscreenchange", onFullscreen);
    releasePage();
    clearTimeout(hideTimer);
    if (document.fullscreenElement === player) document.exitFullscreen();
    // Dropping the source closes the connection, which stops ffmpeg on the server.
    source.destroy();
  };
}
