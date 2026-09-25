import assert from "node:assert/strict";
import { test } from "node:test";

// A browser that decodes H.264 and AAC, plus HEVC through MediaCapabilities.
globalThis.navigator = {
  mediaCapabilities: {
    async decodingInfo(config) {
      const type = (config.video || config.audio).contentType;
      return { supported: /avc1|hvc1|mp4a/.test(type) };
    },
  },
};

const { capabilities, capsQuery } = await import("../../reel/static/caps.js");

test("capabilities() asks MediaCapabilities about each codec", async () => {
  assert.deepEqual(await capabilities(), { video: ["h264", "hevc"], audio: ["aac"] });
});

test("capabilities() is only worked out once", async () => {
  assert.equal(capabilities(), capabilities());
});

test("capsQuery() builds the query for /plan and /stream", () => {
  assert.equal(capsQuery({ video: ["h264", "hevc"], audio: ["aac", "ac3"] }), "video=h264%2Chevc&audio=aac%2Cac3");
});
