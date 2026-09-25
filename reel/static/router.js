// Hash routing with page ownership: each visit renders into a fresh element and
// may return a cleanup function. A visit that finishes loading after another has
// started (even a new visit to the same URL: A, then B, then A) is cleaned up at
// once and never becomes the current page.

/**
 * routes: [[regex, render(page, ...matches) -> cleanup?], ...]
 * mount(): a fresh, attached element for the next page
 * getHash(): the current location hash
 * showError(page, err): show a failed render
 * notFound(): called when no route matches
 * onVisit(hash): before rendering (e.g. highlight the nav)
 * saveScroll(hash) / restoreScroll(hash): keep each page's scroll position
 */
export function createRouter({ routes, mount, getHash, showError, notFound, onVisit = () => {},
                               saveScroll = () => {}, restoreScroll = () => {} }) {
  let cleanup = null;
  let currentHash = null;
  let visits = 0;

  async function route() {
    const visit = ++visits;
    if (cleanup) cleanup();
    cleanup = null;
    if (currentHash !== null) saveScroll(currentHash);
    const hash = getHash();
    currentHash = hash;
    onVisit(hash);
    for (const [pattern, render] of routes) {
      const match = hash.match(pattern);
      if (!match) continue;
      const page = mount();
      try {
        const done = (await render(page, ...match.slice(1))) || null;
        if (visit !== visits) return done && done(); // replaced while loading
        cleanup = done;
        restoreScroll(hash);
      } catch (err) {
        if (visit === visits) showError(page, err);
      }
      return;
    }
    notFound();
  }

  return { route };
}
