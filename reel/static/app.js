import { fromList, itemUrl, renderBrowse, renderHome, renderItem, renderTag } from "./browse.js";
import { renderClip } from "./clippage.js";
import { renderCopies } from "./copies.js";
import { clipPageUrl } from "./clips.js";
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
    [/^#\/copies$/, (page) => renderCopies(page)],
    [/^#\/library\/([0-9a-f-]{36})(?:\/([^?]*))?(\?.*)?$/, (page, uid, path, query) =>
      renderBrowse(page, uid, decodeURIComponent(path || ""), new URLSearchParams(query?.slice(1)).has("all"))],
    // `?all=<folder>`: opened from that folder's "Show all" list (prev/next buttons).
    [/^#\/item\/([0-9a-f-]{36})(\?.*)?$/, (page, uid, query) => renderItem(page, uid, fromList(query))],
    // `?clip=<clip>`: just that clip (made on the edit page); Back goes to the clip's page.
    [/^#\/play\/([0-9a-f-]{36})(\?.*)?$/, (page, uid, query) => {
      const clip = new URLSearchParams(query?.slice(1)).get("clip");
      const back = clip ? clipPageUrl(clip, fromList(query)) : itemUrl(uid, fromList(query));
      return renderPlayer(page, uid, startTime(query?.slice(1)), back, clip);
    }],
    [/^#\/clip\/([0-9a-f-]{36})(\?.*)?$/, (page, uid, query) => renderClip(page, uid, fromList(query))],
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
    const section = hash.startsWith("#/manage") ? "#/manage" : hash.startsWith("#/copies") ? "#/copies"
      : /^#\/tags?(\/|$)/.test(hash) ? "#/tags" : "#/";
    document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === section));
  },
  saveScroll: (hash) => scrollPositions.set(hash, window.scrollY),
  restoreScroll: (hash) => window.scrollTo(0, scrollPositions.get(hash) || 0),
});

window.addEventListener("hashchange", router.route);
router.route();
