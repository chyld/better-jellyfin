import assert from "node:assert/strict";
import { test } from "node:test";

function fakeVideo({ canPlayHls = "" } = {}) {
  return {
    src: "",
    currentTime: 0,
    buffered: { length: 1, end: () => 12 },
    paused: false,
    canPlayType: (type) => (type.includes("mpegurl") ? canPlayHls : ""),
    pause() { this.paused = true; },
    removeAttribute(name) { if (name === "src") this.src = ""; },
    load() {},
    addEventListener() {},
    removeEventListener() {},
  };
}

const { makeSource, shouldReload } = await import("../../reel/static/sources.js");

test("file source: the browser seeks within the file", async () => {
  const video = fakeVideo();
  const source = await makeSource(video, { delivery: "file", url: "/f" });
  source.load(30);
  assert.equal(video.src, "/f#t=30");
  source.seek(95);
  assert.equal(video.currentTime, 95);
  assert.equal(source.position(), 95);
});

test("progressive source: a seek asks for a new stream, and the clock adds the offset", async () => {
  const video = fakeVideo();
  const source = await makeSource(video, { delivery: "progressive", url: "/s?video=h264" });
  source.load(0);
  assert.equal(video.src, "/s?video=h264&start=0.0");
  source.seek(600);
  assert.equal(video.src, "/s?video=h264&start=600.0");
  video.currentTime = 5; // 5 s into the new stream
  assert.equal(source.position(), 605);
  assert.equal(source.bufferedEnd(), 612);
  source.destroy();
  assert.equal(video.src, "");
});

test("HLS in Safari: played natively, seeking is the browser's", async () => {
  const video = fakeVideo({ canPlayHls: "maybe" });
  const source = await makeSource(video, { delivery: "hls", url: "/p.m3u8" }, () => {}, { nativeHls: true });
  source.load(0);
  assert.equal(video.src, "/p.m3u8");
  source.seek(120);
  assert.equal(source.position(), 120);
});

test("unplayable plans are refused", async () => {
  await assert.rejects(makeSource(fakeVideo(), { delivery: null, url: null }), /can't be played/);
});

test("hlsSupport(): native only for Apple's browsers, otherwise hls.js if MSE exists", async () => {
  const { hlsSupport } = await import("../../reel/static/caps.js");
  const set = (vendor, canPlay, mse) => {
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: { vendor } });
    globalThis.document = { createElement: () => ({ canPlayType: () => canPlay }) };
    if (mse) globalThis.MediaSource = function () {};
    else delete globalThis.MediaSource;
  };
  set("Apple Computer, Inc.", "maybe", true);
  assert.equal(hlsSupport(), "native");      // Safari
  set("Google Inc.", "maybe", true);
  assert.equal(hlsSupport(), "mse");         // a Chrome that also claims HLS: use hls.js
  set("Google Inc.", "", true);
  assert.equal(hlsSupport(), "mse");         // Chrome, Firefox
  set("", "", false);
  assert.equal(hlsSupport(), "none");
});

test("hls: a changed video (410) reloads the playlist, but not in a loop", () => {
  assert.equal(shouldReload(410, 0, 60_000), true);
  assert.equal(shouldReload(410, 55_000, 60_000), false); // reloaded 5 s ago: give up
  assert.equal(shouldReload(404, 0, 60_000), false);
  assert.equal(shouldReload(undefined, 0, 60_000), false);
});
