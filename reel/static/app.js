import { fromList, itemUrl, renderBrowse, renderHome, renderItem, renderTag } from "./browse.js";
import { renderEditor } from "./editor.js";
import { renderManage } from "./manage.js";
import { renderPlayer, startTime } from "./player.js";
import { createRouter } from "./router.js";
import { renderTags } from "./tags.js";

const view = document.querySelector("#view");
// Remember where each page was scrolled to, so Back returns to the same spot in a long grid.
const scrollPositions = new Map();

const router = createRouter({
  routes: [
    [/^#?\/?$/, (page) => renderHome(page)],
    [/^#\/manage$/, (page) => renderManage(page)],
    [/^#\/tags$/, (page) => renderTags(page)],
    [/^#\/library\/([0-9a-f-]{36})(?:\/([^?]*))?(\?.*)?$/, (page, uid, path, query) =>
      renderBrowse(page, uid, decodeURIComponent(path || ""), new URLSearchParams(query?.slice(1)).has("all"))],
    // `?all=<folder>`: opened from that folder's "Show all" list (prev/next buttons).
    [/^#\/item\/([0-9a-f-]{36})(\?.*)?$/, (page, uid, query) => renderItem(page, uid, fromList(query))],
    // `?clip=<range>`: just that clip, saved on the edit page.
    [/^#\/play\/([0-9a-f-]{36})(\?.*)?$/, (page, uid, query) =>
      renderPlayer(page, uid, startTime(query?.slice(1)), itemUrl(uid, fromList(query)),
        new URLSearchParams(query?.slice(1)).get("clip"))],
    [/^#\/edit\/([0-9a-f-]{36})$/, (page, uid) => renderEditor(page, uid)],
    [/^#\/tag\/([0-9a-f-]{36})$/, (page, uid) => renderTag(page, uid)],
  ],
  // Each visit renders into a fresh element, so a slow page that finishes after
  // the user has moved on only fills a detached element.
  mount() {
    const page = document.createElement("div");
    view.replaceChildren(page);
    return page;
  },
  getHash: () => location.hash,
  showError(page, err) {
    page.replaceChildren(Object.assign(document.createElement("p"), { className: "error", textContent: err.message }));
  },
  notFound: () => (location.hash = "#/"),
  onVisit(hash) {
    // Tag pages belong to Tags, library settings to Libraries, everything else to Home.
    const section = hash.startsWith("#/manage") ? "#/manage" : /^#\/tags?(\/|$)/.test(hash) ? "#/tags" : "#/";
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === section));
  },
  saveScroll: (hash) => scrollPositions.set(hash, window.scrollY),
  restoreScroll: (hash) => window.scrollTo(0, scrollPositions.get(hash) || 0),
});

window.addEventListener("hashchange", router.route);
router.route();
