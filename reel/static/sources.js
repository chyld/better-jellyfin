// How the video gets into the <video> element: one small interface, three ways.
//
//   file         the original file; the browser seeks natively (range requests)
//   progressive  a stream that starts at `offset`; seeking asks for a new stream
//   hls          a playlist of segments (native in Safari, hls.js elsewhere);
//                seeking within it is the browser's own
//
// Each source has: load(start), seek(t), position(), bufferedEnd(), destroy().

/** Build the source for a plan from /plan. `onError(message)` reports fatal errors;
 *  `nativeHls` says to let the browser play HLS itself (Apple's browsers), rather
 *  than hls.js (everywhere else, even browsers that also claim native HLS). */
export async function makeSource(video, plan, onError, { nativeHls = false } = {}) {
  if (plan.delivery === "file") return fileSource(video, plan.url);
  if (plan.delivery === "progressive") return progressiveSource(video, plan.url);
  if (plan.delivery === "hls") return nativeHls ? nativeHlsSource(video, plan.url) : hlsJsSource(video, plan.url, onError);
  throw new Error("This video can't be played.");
}

function lastBuffered(video) {
  return video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0;
}

function release(video) {
  video.pause();
  video.removeAttribute("src");
  video.load(); // drops the connection, which stops any ffmpeg on the server
}

function fileSource(video, url) {
  return {
    load(start) {
      video.src = start ? `${url}#t=${start}` : url;
    },
    seek(t) {
      video.currentTime = t;
    },
    position: () => video.currentTime,
    bufferedEnd: () => lastBuffered(video),
    destroy: () => release(video),
  };
}

export function progressiveSource(video, url) {
  let offset = 0;
  const source = {
    load(start) {
      offset = start;
      video.src = `${url}&start=${start.toFixed(1)}`;
    },
    seek(t) {
      source.load(t);
    },
    position: () => offset + video.currentTime,
    bufferedEnd: () => offset + lastBuffered(video),
    destroy: () => release(video),
  };
  return source;
}

function nativeHlsSource(video, url) {
  // Safari doesn't say which HTTP status failed; a network error may be the 410
  // for a changed video, so reload the playlist where we were (not in a loop).
  let lastReload = 0;
  const onError = () => {
    if (video.error?.code === 2 && shouldReload(410, lastReload)) {
      lastReload = Date.now();
      source.load(video.currentTime);
    }
  };
  video.addEventListener("error", onError);
  const source = {
    load(start) {
      video.src = url;
      if (start) video.addEventListener("loadedmetadata", () => (video.currentTime = start), { once: true });
    },
    seek(t) {
      video.currentTime = t;
    },
    position: () => video.currentTime,
    bufferedEnd: () => lastBuffered(video),
    destroy() {
      video.removeEventListener("error", onError);
      release(video);
    },
  };
  return source;
}

/** Whether to reload after an HLS error: the server answers 410 when the file
 *  changed under a session, and a fresh playlist fixes that. At most once per
 *  10 seconds, so a persistent problem still ends in an error. */
export function shouldReload(code, lastReload, now = Date.now()) {
  return code === 410 && now - lastReload > 10_000;
}

async function hlsJsSource(video, url, onError) {
  const { default: Hls } = await import("./vendor/hls.light.min.mjs");
  let hls = null;
  let lastReload = 0;
  const source = {
    load(start) {
      hls?.destroy();
      hls = new Hls({ startPosition: start || 0, maxBufferLength: 30, backBufferLength: 60 });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
          hls.recoverMediaError(); // the usual fix for a decode hiccup
        } else if (shouldReload(data.response?.code, lastReload)) {
          lastReload = Date.now();
          source.load(video.currentTime);
        } else {
          onError(data.response?.code === 503 ? "The server is busy converting other videos. Try again in a moment." : "The video stopped loading.");
        }
      });
      hls.loadSource(url);
      hls.attachMedia(video);
    },
    seek(t) {
      video.currentTime = t;
    },
    position: () => video.currentTime,
    bufferedEnd: () => lastBuffered(video),
    destroy() {
      hls?.destroy();
      hls = null;
      release(video);
    },
  };
  return source;
}
