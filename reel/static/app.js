import { renderBrowse, renderHome, renderItem, renderTag } from "./browse.js";
import { renderManage } from "./manage.js";
import { renderPlayer } from "./player.js";
import { renderTags } from "./tags.js";

const view = document.querySelector("#view");
let cleanup = null;
// Remember where each page was scrolled to, so Back returns to the same spot in a long grid.
const scrollPositions = new Map();
let currentHash = null;

const routes = [
  [/^#?\/?$/, (page) => renderHome(page)],
  [/^#\/manage$/, (page) => renderManage(page)],
  [/^#\/tags$/, (page) => renderTags(page)],
  [/^#\/library\/([0-9a-f-]{36})(?:\/(.*))?$/, (page, uid, path) => renderBrowse(page, uid, decodeURIComponent(path || ""))],
  [/^#\/item\/([0-9a-f-]{36})$/, (page, uid) => renderItem(page, uid)],
  [/^#\/play\/([0-9a-f-]{36})$/, (page, uid) => renderPlayer(page, uid)],
  [/^#\/tag\/([0-9a-f-]{36})$/, (page, uid) => renderTag(page, uid)],
];

async function route() {
  if (cleanup) cleanup();
  cleanup = null;
  if (currentHash !== null) scrollPositions.set(currentHash, window.scrollY);
  const hash = location.hash;
  currentHash = hash;
  // Tag pages belong to Tags, library settings to Libraries, everything else to Home.
  const section = hash.startsWith("#/manage") ? "#/manage" : /^#\/tags?(\/|$)/.test(hash) ? "#/tags" : "#/";
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === section));
  for (const [pattern, render] of routes) {
    const match = hash.match(pattern);
    if (match) {
      // Each visit renders into a fresh element, so a slow page that finishes
      // after the user has moved on only fills a detached element.
      const page = document.createElement("div");
      view.replaceChildren(page);
      try {
        const done = (await render(page, ...match.slice(1))) || null;
        if (currentHash !== hash) return done && done();
        cleanup = done;
        window.scrollTo(0, scrollPositions.get(hash) || 0);
      } catch (err) {
        page.replaceChildren(Object.assign(document.createElement("p"), { className: "error", textContent: err.message }));
      }
      return;
    }
  }
  location.hash = "#/";
}

window.addEventListener("hashchange", route);
route();
