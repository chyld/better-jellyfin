// Reading WebVTT subtitles (what /api/items/<id>/subtitles.vtt sends: Reel's own,
// or a file beside the video; an .srt arrives converted) and finding what's said
// at a moment. The player draws them itself (see player.js), from its own clock.

const TIME = /^(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})$/;

/** "01:02:03.450" or "02:03.450" -> seconds (NaN if it isn't a time). */
export function parseTime(text) {
  const m = TIME.exec(text.trim());
  if (!m) return NaN;
  const [, h = "0", min, s, ms] = m;
  return Number(h) * 3600 + Number(min) * 60 + Number(s) + Number(ms.padEnd(3, "0")) / 1000;
}

/** What a cue says, as plain lines: tags (<i>, <c.yellow>, <v Speaker>) and
 *  entities taken out, since it's shown as text. */
function plain(lines) {
  return lines
    .map((line) =>
      line
        .replace(/<[^>]*>/g, "")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&nbsp;/g, " ")
        .replace(/&amp;/g, "&")
        .trim(),
    )
    .filter(Boolean)
    .join("\n");
}

/** A WebVTT file's cues: [{ start, end, text }], earliest first. Headers, NOTE,
 *  STYLE and REGION blocks, cue ids and cue settings are skipped. */
export function parseVtt(text) {
  const blocks = text.replace(/^﻿/, "").replace(/\r\n?/g, "\n").split(/\n{2,}/);
  const cues = [];
  for (const block of blocks) {
    const lines = block.split("\n");
    const at = lines.findIndex((line) => line.includes("-->"));
    if (at < 0) continue; // the header, NOTE, STYLE, REGION
    const [from, rest = ""] = lines[at].split("-->");
    const start = parseTime(from);
    const end = parseTime(rest.trim().split(/\s+/)[0] || "");
    const said = plain(lines.slice(at + 1));
    if (Number.isFinite(start) && Number.isFinite(end) && end > start && said) cues.push({ start, end, text: said });
  }
  return cues.sort((a, b) => a.start - b.start || a.end - b.end);
}

/** What's said at `t` seconds: the text of every cue showing then, one under the
 *  other (usually one), or "" for none. `cues` are sorted by start (parseVtt). */
export function textAt(cues, t) {
  // The cues that started by t: a binary search, then back over the few that may still show.
  let lo = 0;
  let hi = cues.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (cues[mid].start <= t) lo = mid + 1;
    else hi = mid;
  }
  const showing = [];
  for (let i = lo - 1; i >= 0 && showing.length < 3; i--) {
    if (cues[i].end > t) showing.unshift(cues[i].text);
    if (t - cues[i].start > 60) break; // nothing that started a minute ago is still up
  }
  return showing.join("\n");
}
