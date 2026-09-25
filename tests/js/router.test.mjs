import assert from "node:assert/strict";
import { test } from "node:test";

const { createRouter } = await import("../../reel/static/router.js");

/** A router over fake pages whose renders finish when the test says so. */
function setup() {
  let hash = "";
  const log = [];
  const pending = [];
  const render = (name) => (page) =>
    new Promise((resolve) => {
      pending.push({ name, page, finish: () => resolve(() => log.push(`cleanup ${name}#${page.id}`)) });
    });
  let pages = 0;
  const router = createRouter({
    routes: [
      [/^#\/a$/, render("a")],
      [/^#\/b$/, render("b")],
    ],
    mount: () => ({ id: ++pages }),
    getHash: () => hash,
    showError: (page, err) => log.push(`error ${page.id}: ${err.message}`),
    notFound: () => log.push("not found"),
  });
  const go = (to) => {
    hash = to;
    return router.route();
  };
  return { go, pending, log };
}

test("router: a slow first visit to A, finishing after A was visited again, is cleaned up", async () => {
  const { go, pending, log } = setup();
  const first = go("#/a");      // A (slow)
  const b = go("#/b");          // then B
  pending[1].finish();
  await b;
  const second = go("#/a");     // then A again, which loads first
  pending[2].finish();
  await second;
  pending[0].finish();          // the first A finally finishes
  await first;
  assert.deepEqual(log, ["cleanup b#2", "cleanup a#1"]); // B left; the stale A cleaned at once
  go("#/b");                    // leaving now cleans the *current* A
  assert.deepEqual(log.at(-1), "cleanup a#3");
});

test("router: a failed render only shows its error if it's still the current page", async () => {
  let hash = "#/bad";
  const log = [];
  let fail;
  const router = createRouter({
    routes: [[/^#\/bad$/, () => new Promise((_, reject) => (fail = reject))], [/^#\/ok$/, () => null]],
    mount: () => ({ id: hash }),
    getHash: () => hash,
    showError: (page, err) => log.push(`${page.id}: ${err.message}`),
    notFound: () => {},
  });
  const bad = router.route();
  hash = "#/ok";
  await router.route();
  fail(new Error("boom"));
  await bad;
  assert.deepEqual(log, []);
});

test("router: unknown URLs go to notFound", async () => {
  const { go, log } = setup();
  await go("#/nowhere");
  assert.deepEqual(log, ["not found"]);
});
