import assert from "node:assert/strict";
import { test } from "node:test";

const { canMakeClip, markStatus } = await import("../../reel/static/editor.js");

test("a clip needs two marks", () => {
  assert.equal(canMakeClip([]), false);
  assert.equal(canMakeClip([12]), false);
  assert.equal(canMakeClip([12, 40]), true);
});

test("the marks can be pressed in either order", () => {
  assert.equal(canMakeClip([40, 12]), true);
  assert.match(markStatus([40, 12]), /^0:12\.0 – 0:40\.0 \(0:28\.0\)/);
});

test("marks too close together don't make a clip", () => {
  assert.equal(canMakeClip([12, 12.3]), false);
  assert.match(markStatus([12, 12.3]), /only 0:00\.3 apart/);
});

test("the edit page says what to do next", () => {
  assert.match(markStatus([]), /press Mark where a clip should start/);
  assert.match(markStatus([75.2]), /^Marked 1:15\.2\. Press Mark again/);
});
