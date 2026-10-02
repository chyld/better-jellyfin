import assert from "node:assert/strict";
import { test } from "node:test";

const { parseTime, parseVtt, textAt } = await import("../../reel/static/vtt.js");

test("times", () => {
  assert.equal(parseTime("01:02:03.450"), 3723.45);
  assert.equal(parseTime("02:03.4"), 123.4);
  assert.equal(parseTime("00:00:01,500"), 1.5); // a comma, as in SRT
  assert.ok(Number.isNaN(parseTime("soon")));
});

test("a WebVTT file's cues, in order, with headers, notes, ids and settings skipped", () => {
  const cues = parseVtt(
    "﻿WEBVTT - Reel\r\nKind: captions\r\n\r\nNOTE made by Whisper\r\n\r\nSTYLE\r\n::cue { color: red }\r\n\r\n" +
      "2\r\n00:00:05.000 --> 00:00:07.000 align:start line:90%\r\n<v Mika>Second</v>\r\n\r\n" +
      "00:01.000 --> 00:02.500\r\n<i>First</i> &amp; one\r\nline two\r\n\r\n" +
      "00:00:09.000 --> 00:00:08.000\r\nbackwards\r\n\r\n00:00:10.000 --> 00:00:11.000\r\n\r\n",
  );
  assert.deepEqual(cues, [
    { start: 1, end: 2.5, text: "First & one\nline two" },
    { start: 5, end: 7, text: "Second" },
  ]);
});

test("what's said at a moment", () => {
  const cues = parseVtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nA\n\n00:00:03.000 --> 00:00:06.000\nB\n\n" +
    "00:00:04.000 --> 00:00:05.000\nC\n");
  assert.equal(textAt(cues, 0.5), "");
  assert.equal(textAt(cues, 1), "A");
  assert.equal(textAt(cues, 1.999), "A");
  assert.equal(textAt(cues, 2), "");          // the end is exclusive
  assert.equal(textAt(cues, 4.5), "B\nC");    // overlapping: both, in order
  assert.equal(textAt(cues, 7), "");
  assert.equal(textAt([], 3), "");
});

test("finding a line is quick in a long file", () => {
  const cues = Array.from({ length: 5000 }, (_, i) => ({ start: i * 2, end: i * 2 + 1, text: `line ${i}` }));
  assert.equal(textAt(cues, 7000.5), "line 3500");
  assert.equal(textAt(cues, 7001.5), "");
});
