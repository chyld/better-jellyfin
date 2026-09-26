import assert from "node:assert/strict";
import { test } from "node:test";

const { clampSeek, enterFullscreen, exitFullscreen, isFullscreen, startTime } = await import("../../reel/static/player.js");

test("jumping a minute moves by 60 seconds", () => {
  assert.equal(clampSeek(600 + 60, 3600), 660);
  assert.equal(clampSeek(600 - 60, 3600), 540);
});

test("rewinding near the start stops at 0", () => {
  assert.equal(clampSeek(30 - 60, 3600), 0);
});

test("fast-forwarding near the end stops just before it", () => {
  assert.equal(clampSeek(3580 + 60, 3600), 3599);
});

test("unknown length never seeks below 0", () => {
  assert.equal(clampSeek(60, 0), 0);
});

test("going to the beginning lands on 0", () => {
  assert.equal(clampSeek(0, 3600), 0);
});

// ---- Full screen on every kind of browser ----

function fakes({ standard = false, prefixed = false, iphone = false } = {}) {
  const calls = [];
  const doc = { fullscreenElement: null, webkitFullscreenElement: null };
  const player = {};
  const video = { webkitDisplayingFullscreen: false };
  if (standard) {
    player.requestFullscreen = () => { calls.push("player"); doc.fullscreenElement = player; return Promise.resolve(); };
    doc.exitFullscreen = () => { calls.push("exit"); doc.fullscreenElement = null; };
  }
  if (prefixed) {
    player.webkitRequestFullscreen = () => { calls.push("player (webkit)"); doc.webkitFullscreenElement = player; };
    doc.webkitExitFullscreen = () => { calls.push("exit (webkit)"); doc.webkitFullscreenElement = null; };
  }
  if (iphone) {
    video.webkitEnterFullscreen = () => { calls.push("video"); video.webkitDisplayingFullscreen = true; };
    video.webkitExitFullscreen = () => { calls.push("exit video"); video.webkitDisplayingFullscreen = false; };
  }
  return { calls, doc, player, video };
}

test("full screen: desktop, Android and iPad put the whole player full screen", async () => {
  const { calls, doc, player, video } = fakes({ standard: true, iphone: true });
  await enterFullscreen(player, video);
  assert.ok(isFullscreen(player, video, doc));
  exitFullscreen(player, video, doc);
  assert.deepEqual(calls, ["player", "exit"]);
});

test("full screen: older Safari uses the webkit- names", () => {
  const { calls, doc, player, video } = fakes({ prefixed: true });
  enterFullscreen(player, video);
  assert.ok(isFullscreen(player, video, doc));
  exitFullscreen(player, video, doc);
  assert.deepEqual(calls, ["player (webkit)", "exit (webkit)"]);
});

test("full screen: on iPhone only the video can go full screen", () => {
  const { calls, doc, player, video } = fakes({ iphone: true });
  enterFullscreen(player, video);
  assert.ok(isFullscreen(player, video, doc));
  exitFullscreen(player, video, doc);
  assert.deepEqual(calls, ["video", "exit video"]);
});

test("full screen: iPhone before the video is ready does nothing, quietly", () => {
  const { player, video } = fakes();
  video.webkitEnterFullscreen = () => { throw new Error("InvalidStateError"); };
  assert.doesNotThrow(() => enterFullscreen(player, video));
});

test("full screen: a refused request falls back to the video", async () => {
  const { calls, player, video } = fakes({ iphone: true });
  player.requestFullscreen = () => Promise.reject(new Error("not allowed"));
  await enterFullscreen(player, video);
  assert.deepEqual(calls, ["video"]);
});

test("a play link can say where to start (?t=seconds)", () => {
  assert.equal(startTime("t=335"), 335);
  assert.equal(startTime("t=12.5"), 12.5);
  assert.equal(startTime(""), 0);
  assert.equal(startTime(undefined), 0);
  assert.equal(startTime("t=-4"), 0);
  assert.equal(startTime("t=soon"), 0);
});
