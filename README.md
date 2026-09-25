# Reel

A small, self-hosted video server for a homelab, in the spirit of Jellyfin. Point it at a
folder of videos (for example a NAS share), click **Scan**, and browse and watch everything
in a web browser, including old formats like AVI, WMV, VHS captures in MPEG-2, and MKV, which
ffmpeg converts on the fly.

- **Backend:** Python 3.12+ with FastAPI and SQLite. ffmpeg and ffprobe do all media work.
- **Frontend:** plain HTML, CSS and JavaScript modules, with no build step and no framework.
- **Deployment:** Docker Compose, with the newest ffmpeg release built into the image.

---

## Contents

- [Features](#features)
- [Quick start (Docker)](#quick-start-docker)
- [Mounting the NAS](#mounting-the-nas)
- [Configuration](#configuration)
- [Using Reel](#using-reel)
- [How it works](#how-it-works)
  - [Scanning](#scanning)
  - [Titles](#titles)
  - [Pictures](#pictures)
  - [Playback](#playback)
  - [Tags](#tags)
  - [Uploaded images](#uploaded-images)
  - [IDs and URLs](#ids-and-urls)
- [The data folder](#the-data-folder)
- [ffmpeg](#ffmpeg)
- [API](#api)
- [Development](#development)
- [Project layout](#project-layout)
- [Limitations](#limitations)
- [Roadmap](#roadmap)

---

## Features

- **Libraries.** Add any folder under the media root as a library, using a folder picker, then
  scan it. Rescans only re-read new or changed files.
- **Browsing.** Libraries and folders appear as movie-poster cards and videos as landscape
  cards. Numbers sort naturally (`clip2` before `clip10`), and videos can be sorted by name or year.
  Big folders stay fast: videos load 200 at a time as you scroll.
- **Playback of everything.** Browser-ready files play directly. Others are repackaged or
  converted live by ffmpeg, starting in about half a second.
- **A modern player.** A frosted-glass control dock, a gradient seek bar with a time preview,
  ±1 minute jumps, a go-to-beginning button, keyboard shortcuts and full screen.
- **Snap a preview.** The camera button in the player (or **P**) makes the frame on screen the
  video's picture, replacing any it had.
- **Pictures from your files.** `movie.png` beside `movie.mp4` and `folder.png` in a folder are
  picked up automatically, then shrunk and cached.
- **Your own pictures.** Any folder or video can get an image from your device or a URL, and it
  wins over the picture on the NAS. The image is stored by Reel, never on the NAS.
- **Tags.** Put any number of tags on a video, browse by tag from Home, rename, merge or delete
  tags, and give each tag a picture.
- **Read-only media.** Reel never writes to your video folders.
- **Docker.** Runs as your user, keeps all its state in one folder, and includes a health check.

---

## Quick start (Docker)

```sh
cp .env.example .env     # set MEDIA_PATH (your videos) and PUID/PGID (see `id`)
mkdir -p data            # create it yourself, or Docker makes it owned by root
docker compose up -d --build
```

Open `http://<server>:8000`, then:

1. Go to **Libraries**, click **Add library**, and pick a folder. Inside the container your
   videos are at `/media`, so for example pick `/media/Personal`.
2. Click **Scan** (or **Scan all**). The first scan of about 500 videos over SMB takes 2–3 minutes.
3. Go **Home** and browse.

Useful commands:

```sh
docker compose logs -f                        # follow the logs
curl localhost:8000/api/health                # {"ok": true, "ffmpeg": "n9.0.2-...", "ffprobe": ..., ...}
git pull && docker compose up -d --build      # update Reel (keeps your data)
docker compose build --no-cache && docker compose up -d   # also pull the newest ffmpeg
scripts/docker-smoke.sh                       # build and test the image end to end
```

If you're not in the `docker` group, prefix Docker commands with `sudo`, or add yourself with
`sudo usermod -aG docker $USER` and log in again.

---

## Mounting the NAS

Docker needs a real mount on the host. A desktop GVFS mount (`/run/user/1000/gvfs/...`) is
**not** visible to Docker.

**Option 1: mount on the host (recommended).** Add the share to `/etc/fstab`:

```
//nas.local/videos  /mnt/nas/videos  cifs  ro,soft,echo_interval=15,credentials=/etc/nas.cred,uid=1000,gid=1000,vers=3.0,iocharset=utf8,_netdev,nofail,x-systemd.automount  0 0
```

- `ro`: read-only; Reel never needs to write to your videos.
- `uid`/`gid`: match `PUID`/`PGID` in `.env`, so the container can read the files.
- `iocharset=utf8`: shows file names with accents or other non-English characters correctly.
- `x-systemd.automount`: mounts on first use, so boot doesn't hang if the NAS is down.
- `soft,echo_interval=15`: if the NAS stops answering, file reads fail after a while instead of
  waiting forever, and the client notices a dead connection sooner. `soft` is the CIFS default;
  it's written out so it isn't lost. This is what lets Reel (and a scan) actually stop when the
  NAS hangs: a read stuck in the kernel can't be interrupted from Python.

Create the mount point and a root-only credentials file, then mount it. `cifs-utils` must be
installed (`mount.cifs`).

```sh
sudo mkdir -p /mnt/nas/videos
sudo install -m 600 /dev/null /etc/nas.cred
read -rp 'SMB user: ' u; read -rsp 'SMB password: ' p; echo
printf 'username=%s\npassword=%s\n' "$u" "$p" | sudo tee /etc/nas.cred >/dev/null; unset u p
sudo systemctl daemon-reload
sudo mount /mnt/nas/videos
findmnt /mnt/nas/videos
```

**Option 2: let Docker mount the share itself.** `compose.yaml` contains a commented-out
`cifs` volume. Set `SMB_USER`/`SMB_PASSWORD` in `.env` and swap the media volume line as
described there.

---

## Configuration

All settings are environment variables. With Docker, put them in `.env`; `.env.example`
documents each one.

| Variable | Default | Meaning |
|---|---|---|
| `MEDIA_PATH` | `/mnt/nas/videos` | *(compose)* Where your videos are on the host. Mounted read-only at `/media`. |
| `DATA_PATH` | `./data` | *(compose)* Where Reel keeps its database, thumbnails and uploads. Back this up. |
| `PUID` / `PGID` | `1000` | *(compose)* The user and group the container runs as. It must be able to read the media and write the data folder. |
| `REEL_PORT` | `8000` | *(compose)* Port on the host. |
| `FFMPEG_SERIES` | `auto` | *(build)* ffmpeg release series to build in: `auto` means the newest; or pin one, e.g. `9.0`. |
| `REEL_MEDIA_ROOT` | `/media` | Libraries must be inside this folder, and the folder picker can't leave it. |
| `REEL_DATA_DIR` | `./data` (`/data` in Docker) | The data folder. |
| `REEL_PROBE_WORKERS` | `4` | How many ffprobe processes run at once during a scan. |
| `REEL_MAX_STREAMS` | `3` | How many videos may be converted or repackaged at once. More viewers get a "try again in a moment" message. |
| `REEL_HLS_CACHE_MB` | `2048` | Size target for the HLS segment cache (`<data>/hls`). Over it, segments no viewer is near are deleted first. A soft target: see [HLS details](#playback). |
| `REEL_MISSING_GRACE_DAYS` | `7` | How long a video a scan can no longer find stays in the catalog (hidden, with its tags and pictures) before it's removed. |
| `REEL_IMAGE_URLS` | `internet` | Where pictures may be downloaded from when you paste a URL: `internet` (public addresses only), `lan` (also your local network) or `off`. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | *(uvicorn)* Behind a reverse proxy, set this to the proxy's address so its forwarded headers are trusted. Nothing else's are. |

Numbers are checked when Reel starts: something that isn't a number stops it with a message
naming the setting, and values below the minimum are raised to it (at least 1 probe worker and 1
stream, a 100 MB HLS cache, and a grace period of 0 days).

If the data folder isn't writable, Reel stops at startup with a message saying how to fix the
permissions. The usual cause is a `./data` that Docker created owned by root.

---

## Using Reel

### Pages

| Page | URL | What's there |
|---|---|---|
| Home | `#/` | Library tiles, and a card for every tag. |
| Library / folder | `#/library/<library-uuid>/<folder path>` | Subfolders as posters, then videos. Breadcrumbs and a Name/Year sort. |
| Video | `#/item/<video-uuid>` | Picture, title, pills (year, length, resolution, play mode), **Play**, tags and file details. |
| Player | `#/play/<video-uuid>` | Full-window player. |
| Tags | `#/tags` | Every tag: set its image, rename or merge, delete. |
| Tag | `#/tag/<tag-uuid>` | The videos with that tag. |
| Libraries | `#/manage` | Add, rename, remove and scan libraries, with live progress. |

Going **Back** returns you to the same scroll position in long grids.

### Player controls

| Action | Mouse | Keyboard |
|---|---|---|
| Play / pause | ▶ button, or click the video | **Space** or **K** |
| Back / forward 10 seconds | | **←** / **→** |
| Back / forward 1 minute | **↺1m** / **↻1m** buttons | **Shift + ←** / **Shift + →** |
| Go to beginning | **⏮** button | **Home** |
| Seek | Click or drag the seek bar; hovering shows the time | |
| Mute / volume | Speaker button; the slider appears on hover | **M** |
| Full screen | ⛶ button, or double-click the video | **F** |
| Use this frame as the preview | Camera button | **P** |

The controls fade out after 3 seconds without mouse movement while playing. Converted videos
show a pulsing **CONVERTING** badge, and repackaged ones show **REPACKAGING**.

**Use this frame as the preview** pauses the video and asks the server for the frame at that
exact moment, taken from the **original file** (full quality, whatever the player was sent;
VHS captures are deinterlaced as for playback). It's stored like an uploaded picture, replaces
any earlier one, and wins over a picture on the NAS. A note at the top says "Preview updated"
(or what went wrong). Nothing is written to the NAS.

---

## How it works

### Scanning

A scan walks the library folder and records every video in SQLite. Pages are then built from
the database, so **browsing never touches the NAS**.

- **Videos** are recognised by extension: `mp4 m4v mov mkv webm avi wmv asf mpg mpeg m2ts mts
  ts vob flv 3gp ogv divx`. Hidden files and folders (`.zfs`, `.Trash`, …) are skipped.
- Each new or changed file is read with **ffprobe**: container, video and audio codecs, pixel
  format, resolution, length, and whether it's interlaced. From that, Reel works out its
  [play mode](#playback).
- **Rescans are incremental.** A file is re-probed only if its size or modification time
  changed, or its last probe failed. Titles and pictures are refreshed for every file, which is
  cheap, so a poster added later or a title-rule change is picked up without re-probing.
- **A scan never throws data away because of something it couldn't read:**
  - A folder it can't list, or a video it can't read, is left exactly as it was: the videos,
    their tags, pictures and the folder's art are all kept. The scan finishes, and the
    Libraries page shows a warning naming what it couldn't read.
  - A library folder that's missing, or that used to have videos but is now completely
    empty (an unmounted NAS usually leaves an empty mount point), stops the scan with nothing
    changed.
  - A video that's really gone is first marked **missing**: hidden from browsing, counts and
    tag pages, but kept with its tags and pictures. It's removed only after
    `REEL_MISSING_GRACE_DAYS` (default 7). If it comes back before then, for example when the
    NAS is mounted again, it reappears with everything intact. Its page shows "Missing from
    the library".
- **Symlinks** that lead out of the library are ignored, videos and pictures alike, and the
  scan reports how many. Symlinks that stay inside the library are fine. Symlinked folders
  aren't followed.
- While someone watches (as above), a scan starts new ffprobes one at a time, leaving the NAS
  to the video; otherwise a few run at once.
- Scans run one at a time on a background thread, and the Libraries page shows progress
  ("Scanning: 71 / 312"). Each file is committed as it's done, so a long scan never blocks
  other actions.
- **Stopping Reel mid-scan is safe and quick.** Queued scans are dropped, and a running one stops
  at its next folder or file. Videos it already recorded stay recorded. It stops *before* the
  step that marks unseen videos missing, so a half-finished walk never hides anything. The next
  scan picks up where it left off, since unchanged files aren't probed again.
  - Running ffprobes are killed, and no new one starts once stopping has begun. The scan checks
    for the stop between folders, between files, and around fingerprinting (including the
    fingerprints that spot moved files).
  - One thing can't be interrupted: a file-system call stuck on a hung NAS mount. It finishes
    when the operating system returns it, and the scan writes nothing after it. Reel stops
    *waiting* for the scan after 10 seconds, but the process can't fully exit until that call
    returns, because Python waits for the scan's worker threads on exit. With a healthy NAS
    this takes a moment; with a hung mount, as long as the mount takes to time out.
- A file ffprobe can't read is kept, marked *unsupported* with the error, and retried on the
  next scan.
- After every scan, uploaded pictures whose video or folder no longer exists are deleted (see
  [Uploaded images](#uploaded-images)).

**Library rules:**
- A library's folder must be inside the media root and readable.
- Two libraries can't overlap: neither the same folder, nor one inside another.
- Library names are required, at most 100 characters, and unique regardless of capitalisation.
- Removing a library only removes it from Reel. Your files are never touched.

### Titles

| File | Title | Year | Rule |
|---|---|---|---|
| `Tapes/1992.zoo-trip.mpg` | zoo-trip | 1992 | A leading year followed by `.`, `_`, `-` or a space becomes the year. |
| `Classics/0360/movie.mp4` | 0360 | | Generic names (`movie`, `video`, `film`, `main`, `feature`) use the folder name. |
| `Drama/0902/rough-cut.mp4` | 0902 | | The only video in a folder **with no subfolders** uses the folder name. |
| `Personal/loose.mp4` | loose | | …but a lone video beside other folders keeps its own name. |
| `Camcorder/clip01.avi` | clip01 | | Otherwise, the file name without its extension. |

### Pictures

| Shows | Shape | Picture comes from |
|---|---|---|
| Video cards and the video page | 16:9 landscape (thumbnail up to 640×360) | an [uploaded image](#uploaded-images) or a frame snapped in the player, else the image beside the video **with the same name** (`zombie.mp4` → `zombie.png`), else a film-strip placeholder |
| Folder cards and library tiles | 2:3 poster (up to 480×720) | an uploaded image, else `folder.<ext>` **in that folder**, else a folder placeholder |
| Tag cards | 2:3 poster | the tag's uploaded image, else a tag placeholder |

- Image extensions: `jpg`, `jpeg`, `png`, `webp`, in any capitalisation.
- `folder.<ext>` is only ever a folder's picture, never a video's.
- NAS images can be several megabytes (up to 65 MB in practice), so the server shrinks each
  one with ffmpeg into a small cached JPEG, **cropped to the shape it's shown in**. It's
  roughly 20–80 KB and never enlarged; making one takes about 0.2–0.6 s, serving a cached one
  about 2 ms.
- **Thumbnails don't touch the NAS once made.** The scan records each NAS picture's version
  (its size and modification time), and thumbnails are cached under it, so showing a cached
  one needs no trip to the NAS. The version is also in the picture's URL, so the browser keeps
  it for good. A picture replaced on the NAS shows up after the next scan.
- **Once the scan queue is empty, missing thumbnails are made** one at a time, so the first
  look at a folder doesn't wait for ffmpeg. Scans always go first ("Scan all" scans every
  library before making any), and it gives way as soon as another scan is queued, someone starts
  watching, or Reel stops (a thumbnail being made is ended); the rest are made on first view or
  after the next scan. Thumbnails of old picture versions are deleted then too.
- **While someone watches, thumbnails are made one at a time** (otherwise up to 4 at once), so a
  new folder doesn't take the CPU or the NAS from playback. "Watching" means a stream is running
  or any video bytes (direct play, a stream, an HLS segment) were asked for in the last 30
  seconds.
- Cards load their pictures lazily as they scroll into view.
- Placeholders are drawn in the browser, so a missing picture costs no request. A picture that
  fails to load (for example with the NAS offline) also turns into its placeholder.

### Playback

The scanner stores **facts** about each file: container, codecs, pixel format, interlacing.
**How to play it is decided when you press Play** (`reel/plan.py`), from those facts and from what
*your* browser says it can decode. The browser is asked once per page load
(`MediaCapabilities.decodingInfo()`, falling back to `canPlayType()`). Rules can therefore change
without a rescan, and a browser that plays more (HEVC on Safari, AC-3 on Edge) gets less
conversion. **Video and audio are decided separately:**

| Mode | When | How it's sent |
|---|---|---|
| **direct** | MP4/MOV/M4V (or WebM) whose video and audio the browser plays: H.264 (8-bit 4:2:0), VP9, AV1, and HEVC if the browser says so; AAC, MP3, Opus, Vorbis, FLAC, and AC-3/E-AC-3 if the browser says so. Not interlaced. | The original file, with HTTP range requests, so the browser seeks natively. |
| **remux** | Playable tracks in the wrong container, e.g. MKV, or MPEG-TS saved as `.mp4` | ffmpeg copies both tracks unchanged into a fragmented MP4, streamed as it's made. No quality loss, very little CPU. |
| **audio** | The video plays but the audio doesn't, e.g. H.264 with AC-3, DTS or Vorbis in an MKV | The video is **copied untouched**; only the audio is converted (AAC stereo 160 kb/s). |
| **transcode** | Video the browser can't play: Xvid/DivX, MPEG-1/2, WMV/VC-1, MJPEG, Cinepak, Sorenson, HEVC (unless the browser plays it), 10-bit H.264, interlaced video | ffmpeg converts the video to H.264 (veryfast, CRF 21); the audio is copied when the browser plays it (e.g. MP3), otherwise converted. |
| **unsupported** | ffprobe couldn't read it, or there's no video stream | Not playable; the Play button is disabled. |

The video page shows the mode for your browser, and the player shows a **CONVERTING**,
**CONVERTING AUDIO** or **REPACKAGING** badge.

**Delivery** (how the bytes get to the player, `reel/static/sources.js`):

| Delivery | Used for | Seeking |
|---|---|---|
| **file** | direct play | native (range requests) |
| **HLS** | converted video; and everything streamed to Safari/iOS, which can't play the progressive stream | the player's own: jumping into already-encoded video is instant; a far jump restarts the encoder there |
| **progressive** | repackaged and audio-only streams in other browsers (the video is copied, so it can't be cut into exact segments) | a new stream from the new time (~0.5 s) |

HLS details: the playlist lists the **whole video** as 6-second MPEG-TS segments up front, so the
length is known and any point can be sought. ffmpeg encodes ahead of the viewer into a cache
(`<data>/hls`, emptied at startup), with a keyframe forced on every segment boundary, and
**pauses once it's 2 minutes ahead**.

- **Sessions and viewers.** A session is one video, one plan and one *version* of the file: its
  size and modification time, the facts the encoder uses (length, height, interlacing, audio
  codec) and the encoder settings' version. Each playlist load is a
  viewer with its own position and at most one encoder of its own. Viewers share the session's
  segments but never restart each other's encoder, so two tabs at different points of a video
  don't fight. (At most 8 viewers per session; the least recently used is dropped.)
- **Overlapping encoders.** Each encoder writes into its own staging folder, and finished
  segments are published into the shared folder with a hard link, which fails if the segment is
  already there. When two viewers' encoders cover the same stretch, the first finished copy of a
  segment wins, and a published segment is never written over or read half-written.
- **Failures are cleaned up.** An encoder that makes nothing for 60 seconds, or whose viewer
  gives up waiting, is killed and reaped and its slot freed; the error says why.
- **Changed files.** Opening a replaced file, or one a rescan re-probed with different facts,
  retires the old session: its encoders are stopped, then its segments deleted. A file replaced mid-play is noticed when an encoder starts, and the
  player gets a 410 and reloads the playlist where it was. A moved file keeps its session.
- **Expiry and space.** Viewers idle for 10 minutes are dropped, then sessions without viewers.
  Segment URLs carry everything needed to make the session again, so resuming after a long pause
  just works. Segments far behind every viewer are deleted.
- **Cache size is a soft target.** `REEL_HLS_CACHE_MB` counts everything in the cache,
  including segments still being written or not yet published. Over it, segments no viewer is
  near are deleted first, from the sessions used longest ago. The segments around each viewer
  (26: the one just behind, and 2½ minutes ahead) are never deleted for space, so the cache can
  stay over the target while people watch. That's at most about 25–75 MB per viewer at 1080p.
  Reel logs a warning when that happens instead of interrupting playback. The check runs every
  15 seconds: what to keep is decided from the sessions in memory, and the disk work (measuring,
  deleting) happens in a worker thread, so a slow disk doesn't hold up playback. The size shown
  in `/api/health` is from that last check.

Encoders use the same `REEL_MAX_STREAMS` slots. Safari plays HLS natively; other browsers use the
bundled [hls.js](https://github.com/video-dev/hls.js) (light build, Apache-2.0). (Segments are
MPEG-TS rather than fragmented MP4 because ffmpeg restarts fMP4 timestamps at zero on each run,
which would misplace segments from a restarted encoder.)

**The plan covers both.** The tracks and the delivery are decided together, so what `/plan`
reports is what actually runs:
- HLS segments must start on exact keyframes, so **HLS always encodes the video**. When the
  video alone could have been copied (Safari, for an MKV with H.264), the plan says `transcode`
  and gives a `note` explaining why; the video page shows it on the mode's tooltip.
- MPEG-TS segments only carry some audio: **AAC and MP3** are copied (plus **AC-3/E-AC-3** for
  Safari's own player). FLAC, Opus and Vorbis, which the MP4 stream would copy, are converted
  to AAC for HLS (ffmpeg would otherwise write FLAC into TS as an unplayable data stream, and
  the video would be silent).

Details:
- **HEVC** is converted unless the browser reports it can decode it (which depends on its
  hardware); then it's played or copied as is.
- **Converting** deinterlaces when the file is flagged interlaced (`bwdif`), scales anything
  taller than 1080p down to 1080p, and rounds odd frame sizes to even. A keyframe every 2
  seconds keeps start-up fast.
- **Copying** AAC applies `aac_adtstoasc`, because AAC from MPEG-TS uses ADTS framing, which
  MP4 can't hold as-is; copying AC-3/E-AC-3 adds `delay_moov`, without which ffmpeg can't
  write the streamed MP4's header.
- **Cover art** stored as a video stream (as in some WMV files) is skipped (`-map 0:V:0`), so
  the real video plays.
- **Seeking** in remuxed and converted videos: a live stream has no byte ranges, so seeking
  requests a new stream with `?start=<seconds>` and ffmpeg starts again from there, in about
  half a second. The player shows its own clock and seek bar, based on the length recorded at
  scan time.
- **Clean-up:** when the browser drops a stream (a seek, leaving the player, closing the tab),
  the server kills that ffmpeg process at once, so no encoders are left running. All streams
  are stopped when the server shuts down.
- **Limits:** at most `REEL_MAX_STREAMS` (default 3) remuxes and conversions run at once. A new
  one waits up to 5 seconds for a free slot (so a seek, which frees its old slot, doesn't
  bounce), then gets a 503 "try again in a moment".
- **Deadlines:** ffmpeg must start sending video within 30 seconds, and a stream that produces
  nothing for 60 seconds while the browser is waiting is stopped. ffmpeg's error output is
  read continuously (so it can't fill up and freeze ffmpeg), and its last lines are kept for
  the log and for error messages.
- **Files are re-checked when used:** the file must still be inside its library, which must
  still be inside the media root. A file swapped for a symlink leading elsewhere since the
  scan isn't served.
- **Speed:** on a typical machine, conversion runs 4–54× faster than real time depending on
  the format. Streams start in under 0.6 s.
- **Errors:** if ffmpeg can't read a file, the stream request fails with a clear error instead
  of an empty video. A file missing from the NAS gives "The video file is missing. Is the NAS
  connected?"

### Tags

- A video can have **any number of tags**.
- **Tag names:** lowercase `a-z`, `0-9` and `-` only, no spaces, at most 50 characters, for
  example `family`, `1990s`, `road-trip`.
- **Breaking the rule is an error, never silently fixed.** While you type, the box turns red
  and names each bad tag, and pressing Enter adds nothing until every tag is valid. Capitals are
  an error too. The server enforces the same rule.
- In the add box, **spaces and commas separate tags**: `family 1990s road-trip` adds three.
  Existing tags are suggested as you type.
- On Home, tags are listed alphabetically. A tag's videos show **in the order they were
  tagged** (not sorted).
- **Tags page:**
  - **Rename** checks the rule as you type.
  - Renaming onto an existing tag **merges** the two (after a warning); a video that had both
    keeps one.
  - **Delete** removes the tag from every video, after confirmation. The videos aren't affected.
- Removing a tag's last video deletes the tag.
- Tags survive rescans, and go away when their video is removed from the catalog.
- **Tag pictures:** each tag can have an uploaded image, shown on its card on Home. When
  merging, the surviving tag keeps its own image, or takes the other tag's if it had none.

### Uploaded images

Tags, folders and videos can be given a picture: hover the card (on touch screens, tap the
round image icon), or use **Set image** on the Tags page. A video can also take one of its own
frames (see [Player controls](#player-controls)). The dialog offers:

- **Choose a photo from this device**, or
- **Image URL**, which the server downloads.

Rules:
- Formats: JPG, PNG, WebP, GIF, BMP, up to **20 MB**. The format is checked from the file's
  contents, not its name. Uploads are stored as a JPEG at most 800 px wide.
- **Your pictures win.** An uploaded (or snapped) picture is shown instead of the one on the NAS,
  including a NAS picture added later. **Remove image** deletes only yours, and the NAS picture
  shows again.
- A failed upload leaves the previous image in place.
- The button reads **Add image** when there's no picture, **Replace image** over a NAS
  picture, and **Change image** once you've set one; then the dialog also offers
  **Remove image**.
- **URL safety:** only `http://` and `https://`, a 15-second limit for the whole download, at
  most 5 redirects, and the result must really be an image. A web page (an HTML, text, JSON or
  XML answer) or an announced size over 20 MB is refused before anything is downloaded; a
  picture labelled as a generic download is fine, since its contents are checked.
  - Which addresses are allowed is set by `REEL_IMAGE_URLS`: `internet` (the default) allows
    only public addresses; `lan` also allows your local network (192.168.x.x, 10.x.x.x, …),
    for pictures on another homelab server; `off` turns URL downloads off.
  - **This machine** (localhost), **link-local addresses** such as `169.254.169.254` (cloud
    metadata), multicast and reserved addresses are always refused, including via redirects.
  - Each host name is looked up once, every address it returns is checked, and the
    connection goes to exactly the checked address (with HTTPS still verified against the
    name). A DNS server can't pass the check with one answer and redirect the connection with
    another ("DNS rebinding").
- **Each upload gets its own file** (`<uuid>-<version>.jpg`), which is never overwritten. An
  upload writes its new file, records it, then deletes the previous one. Clean-up only
  deletes unreferenced files older than an hour, so a picture that's just been uploaded is
  never removed before it's recorded.
- Uploads are stored in the data folder, never on the NAS (see below).

### IDs and URLs

Libraries, videos and tags are addressed by **random UUIDs** in URLs and the API, so URLs don't
reveal the library's size and can't be walked by counting. The database also has integer keys,
used only internally.

A video keeps its UUID, tags and uploaded picture **when its file is moved or renamed**, and
when its contents change in place:

- Each file has a **fingerprint**: its size plus hashes of its first and last 64 KB. When a scan
  finds a new path whose fingerprint (and extension) matches exactly one video that vanished,
  the video's record moves to the new path instead of becoming a new video. Moved files
  aren't re-probed, and the scan summary shows how many moved.
- Matching is **conservative**: identical copies, or a file that changed while it moved, are
  treated as new videos rather than guessed.
- **Renamed folders** keep their uploaded picture: the moved files show where the folder went,
  including nested folders.

---

## The data folder

Everything Reel stores is in the data folder: `./data` with Docker, or `REEL_DATA_DIR`.

```
data/
├── reel.db             SQLite database: libraries, videos, tags, image records
├── reel.lock           held by the running Reel (one per data folder)
├── thumbs/             cached thumbnails of NAS pictures (safe to delete; they're remade)
│   └── ab/abcdef….jpg
├── hls/                HLS segments being served (emptied at startup)
└── images/             pictures you uploaded (and frames snapped in the player)
    ├── tags/<tag uuid>-<version>.jpg
    ├── videos/<video uuid>-<version>.jpg
    └── folders/<uuid>-<version>.jpg
```

- **Stopping or killing Reel can't corrupt the database.** SQLite runs in WAL mode: a crash or
  `docker compose stop` loses nothing that was saved. Your edits (tags, pictures, libraries)
  are saved with `synchronous=FULL`, so they survive a power cut too. Scans save with
  `synchronous=NORMAL` (no disk flush per file): a power cut may roll back recent scan results,
  which just means those files are probed again on the next scan.
- **Back up** `reel.db` and `images/`. `thumbs/` and `hls/` are only caches. Don't copy
  `reel.db` while Reel runs: recent changes can still be in `reel.db-wal`, so the copy may miss
  them. Either:
  - **stop Reel first** (simplest and fully consistent):
    ```sh
    docker compose stop
    tar czf reel-backup.tgz -C data reel.db images
    docker compose start
    ```
  - or, **while it runs**, snapshot the database with SQLite, then copy the pictures:
    ```sh
    sqlite3 data/reel.db ".backup 'reel-backup.db'"
    tar czf reel-backup.tgz reel-backup.db -C data images
    ```
    A picture changed in the moment between the two steps may be missing from the backup (the
    video then shows its NAS picture or a placeholder); nothing else is affected.
  - To restore, stop Reel, put `reel.db` (renamed from `reel-backup.db` if needed) and
    `images/` back in the data folder, delete any `reel.db-wal` and `reel.db-shm`, and start it.
- Uploaded images whose tag, video, folder or library is gone are cleaned up at startup, after
  every scan, and when a library is removed.
- **Upgrades are automatic.** The schema version is kept in the database (`PRAGMA user_version`),
  and numbered migrations run once, in order, each in a transaction, so a failed one changes
  nothing. A database from a newer Reel is refused rather than misread. Databases from before
  versioning are upgraded too.
- **Re-probing when the rules change:** each video records which version of the probe and
  play-mode rules produced it. When that version goes up, the next scan re-probes older rows.
  The first scan after upgrading to fingerprints re-probes every video once.
- **Fast folders:** each video stores its folder (`parent_dir`) and a sort key (`title_key`, the
  title with numbers zero-padded so text order is natural order), under one index that skips
  missing videos. Opening a folder reads only that folder's rows, already sorted, one page at a
  time; its subfolders and their counts come from the same index. With 50,000 videos, a library's
  top level answers in under 10 ms.
- **Users:** there's one built-in local user (`GET /api/me`). Nothing is per-user yet; the table is
  there so that, if login is ever added, per-person data has somewhere to attach.

---

## ffmpeg

ffmpeg matters a lot here, so the Docker image carries **the newest ffmpeg release** (currently
n9.0.2), not the distribution's older package:

- `scripts/fetch-ffmpeg.sh` reads the build list at
  [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) (the static Linux builds linked
  from ffmpeg.org) and picks the **newest release series**. That's the `n<series>` build, which
  follows the release branch, so it includes the latest point release and fixes. When a new
  major version is released, a rebuild picks it up.
- It downloads the GPL build with shared libraries for amd64 or arm64, **verifies its SHA-256**,
  and installs `ffmpeg`, `ffprobe` and their libraries into `/opt/ffmpeg`, about 190 MB. That's
  roughly 130 MB less than two static binaries.
- Builds with an unknown series or architecture **fail loudly**, rather than producing an image
  without ffmpeg.
- Docker caches this step. To get a newer build: `docker compose build --no-cache`. To pin a
  series: `FFMPEG_SERIES=9.0` in `.env`.
- Check what's running: `curl localhost:8000/api/health`.

The whole test suite passes with this build. Outside Docker, Reel uses whatever `ffmpeg` and
`ffprobe` are on `PATH`.

---

## API

JSON over HTTP. Every ID is a UUID. There's no authentication yet (see
[Limitations](#limitations)).

**Libraries**

| Method | Path | |
|---|---|---|
| GET | `/api/libraries` | All libraries, with video count, scan status and picture info. |
| POST | `/api/libraries` | `{name, path}`: add a library (not scanned yet). |
| PATCH | `/api/libraries/{id}` | `{name}`: rename. |
| DELETE | `/api/libraries/{id}` | Remove from Reel (409 while scanning). Files untouched. |
| POST | `/api/libraries/{id}/scan` | Queue a scan (202). |
| POST | `/api/libraries/scan` | Queue a scan of every library. |
| GET | `/api/libraries/{id}/browse?path=&sort=name\|year&limit=&offset=` | Subfolders and one page of the videos in a folder (`limit` defaults to 200, at most 500). `total_items` is how many videos the folder has. |
| GET | `/api/libraries/{id}/folder-art?path=` | A folder's picture (NAS `folder.<ext>`, else uploaded). |
| PUT | `/api/libraries/{id}/folder-image?path=` | Upload a folder picture (request body = the image). |
| POST | `/api/libraries/{id}/folder-image-url?path=` | `{url}`: set a folder picture from a URL. |
| DELETE | `/api/libraries/{id}/folder-image?path=` | Remove an uploaded folder picture. |
| GET | `/api/folders?path=` | Folder picker: subfolders under the media root. |

**Videos**

| Method | Path | |
|---|---|---|
| GET | `/api/items/{id}` | Details: codecs, size, path, breadcrumbs, tags. |
| GET | `/api/items/{id}/thumb` | The video's picture (landscape JPEG). |
| GET, HEAD | `/api/items/{id}/file` | The original file, with range requests (direct play). |
| GET | `/api/items/{id}/plan?video=&audio=&hls_support=` | How this browser should play it: `video`/`audio` list the codecs it decodes (e.g. `video=h264,hevc&audio=aac,ac3`), `hls_support` is `native`, `mse` or `none`. Returns the mode, what happens to each track, the delivery (`file`, `progressive`, `hls`), a `note` when more work is done than the codecs alone need, and the URL to load. An empty `video=`/`audio=` means none; leaving one out means a typical browser. |
| GET | `/api/items/{id}/hls.m3u8?video=&audio=&hls_support=` | The whole video as an HLS playlist of 6-second segments (video converted). 409 if the plan for this browser isn't HLS. |
| GET | `/api/items/{id}/hls/{session}/{viewer}/{n}.ts?video=&audio=&hls_support=` | Segment `n` for one player, encoded on demand. An expired session is made again from the URL; 410 if the file has changed (load the playlist again). |
| GET | `/api/items/{id}/stream?start=&video=&audio=` | A fragmented MP4 from `start` seconds, each track copied or converted per the plan for those codecs. |
| PUT | `/api/items/{id}/image` | Upload a picture (request body = the image). |
| POST | `/api/items/{id}/image-url` | `{url}`: set a picture from a URL. |
| POST | `/api/items/{id}/snapshot` | `{time}`: use the frame at `time` seconds (from the original file) as the picture, replacing any. |
| DELETE | `/api/items/{id}/image` | Remove the uploaded picture (the NAS one, if any, shows again). |
| POST | `/api/items/{id}/tags` | `{name}`: tag the video. Returns its tags. |
| DELETE | `/api/items/{id}/tags/{tag}` | Untag. Returns its remaining tags. |

**Tags**

| Method | Path | |
|---|---|---|
| GET | `/api/tags` | Tags on at least one video, with counts. |
| GET | `/api/tags/{id}?limit=&offset=` | A tag and one page of its videos (in tagging order), with `total_items`. |
| PATCH | `/api/tags/{id}` | `{name}`: rename; merges into an existing tag of that name. |
| DELETE | `/api/tags/{id}` | Delete the tag from every video. |
| GET, PUT, DELETE | `/api/tags/{id}/image` | The tag's picture: fetch, upload, remove. |
| POST | `/api/tags/{id}/image-url` | `{url}`: set the tag picture from a URL. |

**Other:** `GET /api/me` returns the current user (for now, always the built-in local user).
`GET /api/health` returns `{"ok", "ffmpeg", "ffprobe", "streams": {"active", "limit"}, "hls":
{"sessions", "cache_mb", "target_mb"}}`. It answers **503** with `ok: false` when ffmpeg or ffprobe
is missing, so Docker marks the container unhealthy. The tools' versions are checked once, at
startup (with a time limit), not on every poll. It's used by the Docker health check and kept
out of the access log. The Libraries page shows the same numbers ("Streams in use: 1 of 3 · HLS
cache: 120 MB of 2048 MB"), which is what a "try again in a moment" is about.

Errors are JSON `{"detail": "…"}` with a message meant for people: 400 for invalid input, 404
for not found, 409 for conflicts, 413 for an upload over 20 MB, 416 for a start time outside
the video, 502 when ffmpeg can't read a file, and 503 (with `Retry-After`) when every stream
slot is busy.

---

## Development

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), ffmpeg/ffprobe, and Node (for the
frontend tests).

```sh
uv sync
REEL_MEDIA_ROOT=/mnt/nas/videos REEL_DATA_DIR=./data \
  uv run uvicorn --factory reel.main:app --host 0.0.0.0 --port 8000 --reload
```

The frontend is served as-is from `reel/static/`, so edit and reload. Browsers re-check the
app's files on every load (`Cache-Control: no-cache`), so updates show up without a hard
refresh. Thumbnail URLs carry a version (`THUMBS` in `browse.js`, plus each picture's own
version); bump `THUMBS` when the server changes how thumbnails are made.

### Tests

```sh
uv run pytest              # backend: 545 tests
node --test tests/js/      # frontend: 30 tests
uv run pytest -m browser   # browser: 15 tests (about 2 minutes; needs Chromium and ffmpeg)
scripts/docker-smoke.sh    # builds the image and checks it end to end (needs Docker)
```

- The backend tests use temporary folders shaped like the real library. Tests marked `ffmpeg`
  generate tiny real clips (H.264, HEVC, 10-bit, VP9/Opus, Xvid, WMV, interlaced MPEG-2, MPEG-TS
  named `.mp4`, a file with cover art first, a corrupt file) and check probing, play modes,
  thumbnails, remuxing and conversion with the real ffprobe and ffmpeg. They're skipped if ffmpeg
  isn't installed.
- URL downloads are tested against a local test web server.
- The **browser tests** (`tests/browser`) start a real Reel on a free port, scan generated clips
  with the real ffprobe, and drive headless Chromium over the DevTools protocol. They check that
  each delivery (file, progressive, HLS with converted FLAC audio, progressive conversion for a
  browser without HLS) really plays **with audio**; repeated seeking in HLS and progressive
  streams; that leaving the player stops ffmpeg; two tabs at different points of one video;
  playing on after the HLS session expired; a file replaced mid-play (the player reloads and
  carries on); simultaneous first uploads of a folder picture; and the player's snapshot button
  (the saved picture is the frame that was on screen, before and after a seek). They're skipped by a plain
  `uv run pytest` because they're slow.
- The smoke test generates clips, builds the image, and checks the ffmpeg version, scanning,
  thumbnails, direct play with range requests, live conversion, the data volume and the health
  check.

### Conventions

- Every change is committed with a descriptive message once the tests pass.
- User-facing errors are plain sentences that say what to do.
- NAS media is only ever read.

---

## Project layout

```
reel/
  main.py            FastAPI app: routes, error handling, startup clean-up
  config.py          settings from environment variables; data-folder check
  db.py              SQLite schema and numbered migrations
  users.py           users (for now, the built-in local user)
  paths.py           resolving paths without leaving the allowed folder
  libraries.py       adding/renaming/removing libraries; folder picker
  scanner.py         walking folders; titles; matching pictures; incremental scans
  scan_manager.py    background scan queue with progress
  probe.py           ffprobe wrapper (facts only)
  plan.py            deciding how to play a video for a given browser
  hls.py             HLS playlists and on-demand segment encoding
  playback.py        ffmpeg commands for remux/convert; streaming and killing ffmpeg
  images.py          shrinking/cropping thumbnails; processing uploads
  pictures.py        your pictures for tags, videos and folders: one way to set, remove and clean up
  thumbnails.py      pictures on cards: finding, thumbnailing (limited), and after-scan warming/cleanup
  tags.py            tags: add/remove, rename/merge, delete
  fetch.py           safe image downloads from URLs
  browse.py          read-only views: folders (indexed, paged), video details
  catalog.py         shared by reads and writes: NotFound, folder paths, paging
  sorting.py         natural sort keys stored for SQL ordering
  static/
    index.html       page shell and dialogs
    app.js           wires the pages to the router; scroll restore
    router.js        hash routing; each visit owns its page and cleans up (even A → B → A)
    api.js           fetch helper, h() element builder, formatting, tag rules
    browse.js        Home, folders, video page, tag page, tag editor
    player.js        the player
    caps.js          what this browser can decode (and whether it plays HLS)
    sources.js       file / progressive / HLS delivery behind one interface
    vendor/          hls.js (light build, Apache-2.0)
    manage.js        Libraries page, folder picker
    tags.js          Tags page
    imagedialog.js   the shared "photo or URL" dialog
    style.css        app styles;  player.css  player styles
    fonts/           Inter (SIL Open Font License)
    favicon.svg
scripts/
  fetch-ffmpeg.sh    download and verify the newest ffmpeg build
  docker-smoke.sh    build and test the Docker image end to end
tests/               pytest suite, plus tests/js for the frontend and tests/browser (real Chromium)
Dockerfile, compose.yaml, .env.example
```

---

## Limitations

- **No login.** Anyone who can reach the port can browse, play, and change libraries, tags and
  images. Keep it on your home network or behind a reverse proxy with authentication.
- **One process per data folder.** Scans, stream limits and HLS sessions are managed inside the
  process, so Reel runs with exactly one uvicorn worker (the Docker image does). More workers
  would each run their own scanner and stream limits. To make that impossible by accident, a
  started Reel holds a lock on `reel.lock` in the data folder; a second one on the same folder
  stops at startup with "Another Reel is already using the data folder". Nothing in the data
  folder is changed until the lock is held (migrations, clean-up and emptying the HLS cache all
  happen then, not when the app is built).
- **No subtitles** (neither external `.srt` nor embedded tracks).
- **No hardware transcoding.** Conversion runs on the CPU, which is fine for this library.
  `compose.yaml` notes where a GPU would go.
- **Tags** are limited to lowercase ASCII letters, digits and dashes, by design.
- A tag's videos are listed in tagging order (kept explicitly, so it survives a database
  rebuild), with no sort options yet.

---

## Roadmap

Nothing planned right now: Reel does what it was built to do.

Decided against: watch progress / resume (single-user setup), sorting a tag's videos (they stay
in tagging order), subtitles, hardware transcoding, and a background pass that pre-converts old
formats (videos are converted live, when played).

---

Fonts: [Inter](https://github.com/rsms/inter), © The Inter Project Authors, SIL Open Font
License 1.1 (`reel/static/fonts/OFL.txt`). HLS playback: [hls.js](https://github.com/video-dev/hls.js)
1.7.3, © Dailymotion, Apache License 2.0 (`reel/static/vendor/hls.LICENSE`).
