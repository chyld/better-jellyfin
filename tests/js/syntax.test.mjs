import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { test } from "node:test";

// Some modules touch the DOM as they load, so not all can be imported here; a
// syntax error in any of them still stops the whole app from starting.
const dir = new URL("../../reel/static/", import.meta.url);

test("every frontend module parses", () => {
  const files = readdirSync(dir).filter((f) => f.endsWith(".js"));
  assert.ok(files.includes("browse.js"));
  for (const file of files) execFileSync(process.execPath, ["--check", new URL(file, dir).pathname], { stdio: "pipe" });
});
