// Tests for the frontend helpers. Run with: node --test tests/js/
import assert from "node:assert/strict";
import { test } from "node:test";

// A tiny stand-in for the browser DOM: just enough for h().
class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attrs = {};
    this.listeners = {};
    this.className = "";
    const el = this;
    this.classList = {
      add: (...names) => (el.className = [...new Set([...el.className.split(" ").filter(Boolean), ...names])].join(" ")),
      contains: (name) => el.className.split(" ").includes(name),
    };
  }
  prepend(node) {
    this.children.unshift(node);
  }
  append(...nodes) {
    for (const n of nodes) this.children.push(typeof n === "object" ? n : String(n));
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }
  setAttribute(k, v) {
    this.attrs[k] = v;
  }
  addEventListener(type, fn) {
    this.listeners[type] = fn;
  }
  get textContent() {
    return this.children.map((c) => (typeof c === "string" ? c : c.textContent)).join("");
  }
}
globalThis.document = { createElement: (tag) => new FakeElement(tag) };

const { h, fill, artBox, parseTags, tagError, formatDuration, formatSize, encodePath, plural } = await import("../../reel/static/api.js");

test("h() adds nested lists of children as elements, not text", () => {
  // Breadcrumbs and the details list build lists of pairs like this.
  const pairs = [["a", "b"], [h("dt", {}, "Length"), h("dd", {}, "1:00")]];
  const el = h("dl", {}, pairs.map((p) => p));
  assert.equal(el.children.length, 4);
  assert.ok(el.children.every((c) => typeof c === "string" || c instanceof FakeElement));
  assert.equal(el.textContent, "abLength1:00");
  assert.ok(!el.textContent.includes("[object"));
});

test("h() drops false, null and undefined children but keeps text", () => {
  const el = h("div", {}, false, null, undefined, "x", [false, "y"]);
  assert.equal(el.textContent, "xy");
});

test("fill() skips false from `condition && h(...)` sections", () => {
  // An empty folder section used to show the word "false" on the page.
  const page = h("div", {}, "old");
  const folders = [];
  fill(page, h("p", {}, "1 video"), folders.length > 0 && h("ul"), [h("ul", {}, "card")]);
  assert.equal(page.textContent, "1 videocard");
  assert.equal(page.children.length, 2);
});

test("artBox() shows the image when there is one", () => {
  const box = artBox({ kind: "video", shape: "wide", src: "/api/items/x/thumb" });
  assert.equal(box.className, "art wide");
  const img = box.children[0];
  assert.equal(img.tag, "img");
  assert.equal(img.attrs.src, "/api/items/x/thumb");
  assert.equal(img.attrs.loading, "lazy");
});

test("artBox() shows a folder or video placeholder when there's no image", () => {
  const folder = artBox({ kind: "folder", shape: "square", src: null });
  const video = artBox({ kind: "video", shape: "wide", src: null });
  assert.equal(folder.className, "art square placeholder folder");
  assert.equal(video.className, "art wide placeholder video");
  assert.equal(folder.children.length, 0); // no request for a missing image
});

test("artBox() switches to the placeholder if the image fails to load", () => {
  const box = artBox({ kind: "folder", shape: "square", src: "/broken" });
  box.children[0].listeners.error();
  assert.ok(box.classList.contains("placeholder") && box.classList.contains("folder"));
});

test("artBox() can be a link with extra children", () => {
  const box = artBox(
    { kind: "video", shape: "wide hero", src: null, tag: "a", attrs: { href: "#/play/x" } },
    h("span", {}, "▶"),
  );
  assert.equal(box.tag, "a");
  assert.equal(box.attrs.href, "#/play/x");
  assert.equal(box.textContent, "▶");
});

test("h() sets attributes, classes and listeners", () => {
  const onclick = () => {};
  const el = h("a", { href: "#/", class: "card", hidden: true, disabled: false, onclick });
  assert.equal(el.attrs.href, "#/");
  assert.equal(el.className, "card");
  assert.equal(el.attrs.hidden, "");
  assert.ok(!("disabled" in el.attrs));
  assert.equal(el.listeners.click, onclick);
});

test("formatters", () => {
  assert.equal(formatDuration(0), "");
  assert.equal(formatDuration(59.6), "1:00");
  assert.equal(formatDuration(8172.7), "2:16:13");
  assert.equal(formatSize(1_500_000_000), "1.5 GB");
  assert.equal(formatSize(20_000_000), "20 MB");
  assert.equal(encodePath("Collection/Drama/0911"), "Collection/Drama/0911");
  assert.equal(encodePath("a b/c#d"), "a%20b/c%23d");
  assert.equal(plural(1, "video"), "1 video");
  assert.equal(plural(3, "video"), "3 videos");
});

test("parseTags() splits on spaces and commas without changing anything", () => {
  assert.deepEqual(parseTags("family, 1990s  road-trip"), ["family", "1990s", "road-trip"]);
  assert.deepEqual(parseTags("a,,b , c\td"), ["a", "b", "c", "d"]);
  assert.deepEqual(parseTags("Family"), ["Family"]); // not lowercased: that's an error, not a fix
  assert.deepEqual(parseTags("   "), []);
});

test("tagError() accepts lowercase a-z, 0-9 and dashes", () => {
  assert.equal(tagError(["family", "1990s", "road-trip", "x".repeat(50)]), null);
  assert.equal(tagError([]), null);
});

test("tagError() names every tag that breaks the rule", () => {
  assert.equal(
    tagError(["Family"]),
    '"Family" isn\'t a valid tag. Use only lowercase a-z, 0-9 and dashes (-).',
  );
  assert.equal(
    tagError(["ok", "road_trip", "#vhs", "café"]),
    '"road_trip", "#vhs", "café" aren\'t valid tags. Use only lowercase a-z, 0-9 and dashes (-).',
  );
});

test("tagError() explains the length limit", () => {
  const msg = tagError(["x".repeat(51)]);
  assert.match(msg, /up to 50 characters/);
  assert.match(msg, /…/); // long tags are shortened in the message
});
