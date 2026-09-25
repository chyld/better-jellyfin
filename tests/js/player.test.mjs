import assert from "node:assert/strict";
import { test } from "node:test";

const { clampSeek } = await import("../../reel/static/player.js");

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
