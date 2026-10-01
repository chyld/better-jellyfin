// The "Subtitles" page: every video with English subtitles made by Whisper (see
// subtitles.py), and the ones being made, paused, waiting or failed. Polls while
// any is being made.
import { api, fill, formatDuration, h } from "./api.js";
import { confirmDelete } from "./clips.js";
import { madeAt, place, row } from "./copies.js";

/** "ja" -> "Japanese" (whatever the browser can name). */
export function languageName(code) {
  if (!code) return "";
  try {
    return new Intl.DisplayNames(["en"], { type: "language" }).of(code) || code;
  } catch {
    return code;
  }
}

/** What a job is doing, in words: "Making subtitles: 42%". */
export function subtitleStatus(job) {
  const pct = `${Math.floor((job.progress || 0) * 100)}%`;
  if (job.state === "queued") return job.place ? `Waiting: ${place(job.place)}` : "Waiting…";
  if (job.state === "paused") return `Paused while a video plays (${pct})`;
  if (job.state === "error") return `Failed: ${job.error}`;
  if (job.stage === "model") return "Getting Whisper ready (the first time, a 3 GB download)…";
  if (job.stage === "audio") return "Reading the audio…";
  return `Making subtitles: ${pct}`;
}

export async function renderSubtitles(view) {
  const summary = h("p", { class: "summary" });
  const error = h("p", { class: "error", hidden: true, role: "alert" });
  const jobsSection = h("section");
  const madeSection = h("section", { class: "subtitles-made" });
  let timer = null;
  let closed = false;

  const act = (work) => async () => {
    try {
      await work();
      error.hidden = true;
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    }
    refresh();
  };
  const stop = (video, question, what) => act(async () => {
    if (!(await confirmDelete(`${question} ${video.title}?`, what))) return;
    await api("DELETE", `/api/items/${video.id}/subtitles`);
  });

  function jobRow(job) {
    const busy = job.state === "running" || job.state === "paused";
    const pct = Math.floor((job.progress || 0) * 100);
    const buttons = job.state === "error"
      ? [h("button", { class: "btn", onclick: act(() => api("POST", `/api/items/${job.id}/subtitles`)) }, "Retry"),
         h("button", { class: "btn", onclick: act(() => api("DELETE", `/api/items/${job.id}/subtitles`)) }, "Dismiss")]
      : busy
        ? h("button", { class: "btn danger", onclick: stop(job, "Stop the subtitles for", "What's been made so far is thrown away.") }, "Stop")
        : h("button", { class: "btn", onclick: act(() => api("DELETE", `/api/items/${job.id}/subtitles`)) }, "Cancel");
    return row(job, {
      status: { text: `${subtitleStatus(job)}${job.duration && job.state === "queued" ? ` · ${formatDuration(job.duration)} long` : ""}`,
                tone: job.state === "error" ? "err" : "" },
      extra: busy && h("div", { class: "progress" }, h("div", { style: `width:${pct}%` })),
      buttons,
    });
  }

  function madeRow(sub) {
    if (sub.kind === "nas") {
      const lang = sub.language ? `${languageName(sub.language)} · ` : "";
      return row(sub, {
        status: { text: `${lang}${sub.file}, beside the video`, tone: "ok" },
        buttons: h("a", { class: "btn", href: `/api/items/${sub.id}/subtitles.vtt?download=true` }, "Download"),
      });
    }
    const from = sub.source_language && sub.source_language !== "en" ? ` from ${languageName(sub.source_language)}` : "";
    return row(sub, {
      status: { text: `English${from} · Whisper ${sub.model} · made ${madeAt(sub.made_at)}`, tone: "ok" },
      buttons: [
        h("a", { class: "btn", href: `/api/items/${sub.id}/subtitles.vtt?download=true` }, "Download"),
        h("button", { class: "btn danger", onclick: stop(sub, "Remove the subtitles of", "The video itself is not touched.") }, "Remove"),
      ],
    });
  }

  async function refresh() {
    clearTimeout(timer);
    let data;
    try {
      data = await api("GET", "/api/subtitles");
    } catch (err) {
      if (!closed) {
        error.textContent = err.message;
        error.hidden = false;
      }
      timer = setTimeout(refresh, 3000);
      return;
    }
    if (closed) return;
    const { jobs, subtitles } = data;
    const busy = jobs.filter((j) => j.state !== "error").length;
    const beside = subtitles.filter((s) => s.kind === "nas").length;
    summary.textContent = [
      subtitles.length ? `${subtitles.length} ${subtitles.length === 1 ? "video has" : "videos have"} subtitles` : "No subtitles yet.",
      beside && `${beside} beside the video on the NAS`,
      busy && `${busy} being made or waiting`,
    ].filter(Boolean).join(" · ");
    fill(jobsSection, jobs.length > 0 && [
      h("h2", { class: "section-title" }, "In progress", h("span", { class: "count" }, jobs.length)),
      h("ul", { class: "copy-list" }, jobs.map(jobRow)),
    ]);
    fill(madeSection, subtitles.length > 0
      ? [h("h2", { class: "section-title" }, "Subtitles", h("span", { class: "count" }, subtitles.length)),
         h("ul", { class: "copy-list" }, subtitles.map(madeRow))]
      : !jobs.length && h("p", { class: "empty" },
          "Make English subtitles from a video's page: Whisper listens on this machine and translates what's said. " +
          "They're listed here."));
    if (busy) timer = setTimeout(refresh, 1000);
  }

  fill(view, h("div", { class: "page-head" }, h("h2", { class: "page-title" }, "Subtitles")), summary, error, jobsSection, madeSection);
  await refresh();
  return () => {
    closed = true;
    clearTimeout(timer);
  };
}
