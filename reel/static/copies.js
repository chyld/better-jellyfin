// The "Copies" page: every MP4 copy (see copies.py), and the ones being made,
// waiting their turn or failed, with progress. Polls while any is being made.
import { api, artBox, fill, formatSize, h } from "./api.js";
import { itemUrl, videoImageSrc } from "./browse.js";
import { confirmDelete } from "./clips.js";

/** "2026-09-29 23:41:07" (UTC, from SQLite) as a local date and time. */
export function madeAt(utc) {
  if (!utc) return "";
  const date = new Date(utc.replace(" ", "T") + "Z");
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** Where a waiting copy is in the queue: "next", "2nd in line"... */
export function place(n) {
  if (n === 1) return "next";
  const suffix = n % 100 >= 11 && n % 100 <= 13 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th";
  return `${n}${suffix} in line`;
}

const countCopies = (n) => `${n} ${n === 1 ? "copy" : "copies"}`;

/** A row on the Copies and Subtitles pages: the video's picture and title (both
 *  open its page), where it is, a status line, anything `extra`, and buttons. */
export function row(video, { status = null, extra = [], buttons = [] } = {}) {
  return h(
    "li",
    { class: "copy-row" },
    h("a", { class: "copy-art", href: itemUrl(video.id), tabindex: -1, "aria-hidden": "true" },
      artBox({ kind: "video", shape: "landscape", src: videoImageSrc(video) })),
    h(
      "div",
      { class: "copy-info" },
      h("a", { class: "copy-title", href: itemUrl(video.id) }, video.title),
      h("div", { class: "copy-path" }, `${video.library_name} › ${video.rel_path}`),
      status && h("div", { class: `copy-status ${status.tone || ""}`.trim() }, status.text),
      extra,
    ),
    h("div", { class: "buttons" }, buttons),
  );
}

export async function renderCopies(view) {
  const summary = h("p", { class: "summary" });
  const error = h("p", { class: "error", hidden: true, role: "alert" });
  const jobsSection = h("section", { class: "copies-jobs" });
  const madeSection = h("section", { class: "copies-made" });
  let timer = null;
  let closed = false;

  const showError = (message) => {
    error.textContent = message || "";
    error.hidden = !message;
  };

  // Every action refreshes the list; a failure says why and leaves it as it was.
  const act = (work) => async () => {
    try {
      await work();
      showError();
    } catch (err) {
      showError(err.message);
    }
    refresh();
  };

  const remove = (video, question, what) => act(async () => {
    if (!(await confirmDelete(`${question} ${video.title}?`, `${what} The file on the NAS is not touched.`))) return;
    await api("DELETE", `/api/items/${video.id}/mp4-copy`);
  });

  function jobRow(job) {
    if (job.state === "running") {
      const pct = Math.floor(job.progress * 100);
      return row(job, {
        status: { text: `Copying: ${pct}% of ${formatSize(job.file_size)}` },
        extra: h("div", { class: "progress" }, h("div", { style: `width:${pct}%` })),
        buttons: h("button", { class: "btn danger", onclick: remove(job, "Stop the MP4 copy of", "The copy being made is stopped and thrown away.") }, "Stop"),
      });
    }
    if (job.state === "queued") {
      return row(job, {
        status: { text: `Waiting: ${place(job.place)} · ${formatSize(job.file_size)}` },
        buttons: h("button", { class: "btn", onclick: act(() => api("DELETE", `/api/items/${job.id}/mp4-copy`)) }, "Cancel"),
      });
    }
    return row(job, {
      status: { text: `Failed: ${job.error}`, tone: "err" },
      buttons: [
        h("button", { class: "btn", onclick: act(() => api("POST", `/api/items/${job.id}/mp4-copy`)) }, "Retry"),
        h("button", { class: "btn", onclick: act(() => api("DELETE", `/api/items/${job.id}/mp4-copy`)) }, "Dismiss"),
      ],
    });
  }

  function copyRow(copy) {
    const saved = copy.file_size ? ` (the original is ${formatSize(copy.file_size)})` : "";
    return row(copy, {
      status: copy.current
        ? { text: `${formatSize(copy.copy_size)}${saved} · made ${madeAt(copy.made_at)}`, tone: "ok" }
        : { text: "Out of date: the file on the NAS has changed. It's removed after the next scan.", tone: "err" },
      buttons: h("button", { class: "btn danger", onclick: remove(copy, "Remove the MP4 copy of", "The video plays from the file on the NAS again.") }, "Remove"),
    });
  }

  async function refresh() {
    clearTimeout(timer);
    let data;
    try {
      data = await api("GET", "/api/copies");
    } catch (err) {
      if (!closed) showError(err.message);
      timer = setTimeout(refresh, 3000);
      return;
    }
    if (closed) return;
    const { jobs, copies, totals } = data;
    const running = jobs.filter((j) => j.state !== "error").length;
    summary.textContent = [
      copies.length ? `${countCopies(totals.count)} · ${formatSize(totals.mb * 1024 * 1024)}` : "No MP4 copies yet.",
      running && `${running} being made or waiting`,
    ].filter(Boolean).join(" · ");
    fill(jobsSection, jobs.length > 0 && [
      h("h2", { class: "section-title" }, "In progress", h("span", { class: "count" }, jobs.length)),
      h("ul", { class: "copy-list" }, jobs.map(jobRow)),
    ]);
    fill(madeSection, copies.length > 0
      ? [h("h2", { class: "section-title" }, "Copies", h("span", { class: "count" }, copies.length)),
         h("ul", { class: "copy-list" }, copies.map(copyRow))]
      : !jobs.length && h("p", { class: "empty" },
          "Videos in the wrong container (a blue TS or MKV badge) can be copied into a real MP4 from their page, " +
          "with Make MP4 copy. They're listed here."));
    // Keep watching while copies are being made.
    if (running) timer = setTimeout(refresh, 1000);
  }

  fill(view, h("div", { class: "page-head" }, h("h2", { class: "page-title" }, "Copies")), summary, error, jobsSection, madeSection);
  await refresh();
  return () => {
    closed = true;
    clearTimeout(timer);
  };
}
