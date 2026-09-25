// What this browser can decode, so the server can send each video with as little
// conversion as possible (see plan.py). Checked once per page load.

// A representative MIME type per codec name the server knows.
export const VIDEO_TYPES = {
  h264: 'video/mp4; codecs="avc1.640028"',
  hevc: 'video/mp4; codecs="hvc1.1.6.L120.90"',
  av1: 'video/mp4; codecs="av01.0.08M.08"',
  vp9: 'video/mp4; codecs="vp09.00.40.08"',
  vp8: 'video/webm; codecs="vp8"',
};
export const AUDIO_TYPES = {
  aac: 'audio/mp4; codecs="mp4a.40.2"',
  mp3: "audio/mpeg",
  opus: 'audio/mp4; codecs="opus"',
  flac: 'audio/mp4; codecs="flac"',
  vorbis: 'audio/webm; codecs="vorbis"',
  ac3: 'audio/mp4; codecs="ac-3"',
  eac3: 'audio/mp4; codecs="ec-3"',
};

async function canDecode(kind, contentType) {
  // MediaCapabilities knows about hardware decoders (e.g. HEVC); canPlayType is the fallback.
  if (globalThis.navigator?.mediaCapabilities?.decodingInfo) {
    try {
      const config =
        kind === "video"
          ? { type: "file", video: { contentType, width: 1920, height: 1080, bitrate: 8_000_000, framerate: 30 } }
          : { type: "file", audio: { contentType, channels: "2", bitrate: 192_000, samplerate: 48_000 } };
      return (await navigator.mediaCapabilities.decodingInfo(config)).supported;
    } catch {
      /* unknown type for this API: fall through */
    }
  }
  try {
    return document.createElement(kind).canPlayType(contentType) !== "";
  } catch {
    return false;
  }
}

async function detect() {
  const check = async (kind, types) => {
    const results = await Promise.all(Object.entries(types).map(async ([name, type]) => [name, await canDecode(kind, type)]));
    return results.filter(([, ok]) => ok).map(([name]) => name);
  };
  return { video: await check("video", VIDEO_TYPES), audio: await check("audio", AUDIO_TYPES) };
}

let cached = null;

/** { video: ["h264", ...], audio: ["aac", ...] } for this browser. */
export function capabilities() {
  cached ??= detect();
  return cached;
}

/** "video=h264,vp9&audio=aac,opus" for /plan and /stream. */
export function capsQuery(caps) {
  return `video=${encodeURIComponent(caps.video.join(","))}&audio=${encodeURIComponent(caps.audio.join(","))}`;
}
