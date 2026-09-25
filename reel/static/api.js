export async function api(method, url, body) {
  // A File (from a file picker) is uploaded as-is; anything else is sent as JSON.
  const isFile = typeof Blob !== "undefined" && body instanceof Blob;
  const res = await fetch(url, {
    method,
    headers: body && !isFile ? { "Content-Type": "application/json" } : {},
    body: isFile ? body : body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    const detail = Array.isArray(data.detail) ? "Please fill in every field." : data.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return res.status === 204 ? null : res.json();
}

/** Build an element: h("a", { href: "#/", class: "x" }, "text", child). */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  el.append(...clean(children));
  return el;
}

/** Replace an element's contents; like h(), skips false/null/undefined and flattens lists. */
export function fill(el, ...children) {
  el.replaceChildren(...clean(children));
  return el;
}

// Lets callers write `condition && h(...)` without "false" showing up on the page.
function clean(children) {
  return children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false);
}

/**
 * A picture box for a folder or video card.
 *
 * With a `src` it shows that image (loaded lazily); without one, or if the image
 * fails to load, it shows the placeholder for its kind: "folder" or "video".
 */
export function artBox({ kind, shape, src, alt = "", tag = "div", attrs = {} }, ...children) {
  const box = h(tag, { ...attrs, class: `art ${shape}${src ? "" : ` placeholder ${kind}`}` }, ...children);
  if (src) {
    const img = h("img", {
      src,
      alt,
      loading: "lazy",
      decoding: "async",
      onerror: () => box.classList.add("placeholder", kind),
    });
    box.prepend(img);
  }
  return box;
}

const TAG_RE = /^[a-z0-9-]{1,50}$/;

/** Spaces and commas separate tags: "family, 1990s  road-trip" -> ["family", "1990s", "road-trip"] */
export function parseTags(text) {
  return text.split(/[\s,]+/).filter(Boolean);
}

/** Why these tags can't be added, or null if they're all fine.
 *  Tags use only lowercase a-z, 0-9 and dashes, at most 50 characters. */
export function tagError(names) {
  const bad = names.filter((n) => !TAG_RE.test(n));
  if (!bad.length) return null;
  const list = bad.map((n) => `"${n.length > 30 ? n.slice(0, 30) + "…" : n}"`).join(", ");
  const verb = bad.length === 1 ? "isn't a valid tag" : "aren't valid tags";
  const tooLong = bad.some((n) => n.length > 50 && /^[a-z0-9-]+$/.test(n));
  return `${list} ${verb}. Use only lowercase a-z, 0-9 and dashes (-)${tooLong ? ", up to 50 characters" : ""}.`;
}

export function formatDuration(seconds) {
  if (!seconds) return "";
  const s = Math.round(seconds);
  const hours = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = String(s % 60).padStart(2, "0");
  return hours ? `${hours}:${String(mins).padStart(2, "0")}:${secs}` : `${mins}:${secs}`;
}

export function formatSize(bytes) {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`;
  return `${Math.round(bytes / 1e3)} KB`;
}

/** Path segments are encoded one by one so "/" stays readable in the URL. */
export function encodePath(path) {
  return path.split("/").map(encodeURIComponent).join("/");
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
