"""What each video card shows: its real format, and (coloured) how this browser will play it."""
import pytest

from reel.browse import video_type
from reel.plan import Capabilities, plan
from reel.probe import ProbeResult

from conftest import make_files


@pytest.mark.parametrize("container, path, expected", [
    ("mov,mp4,m4a,3gp,3g2,mj2", "a/movie.mp4", "MP4"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "a/clip.MOV", "MOV"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "phone.3gp", "3GP"),
    ("mpegts", "a/movie.mp4", "TS"),               # renamed: the real format wins
    ("avi", "x.avi", "AVI"),
    ("mpeg", "tape.mpg", "MPG"),
    ("mpeg", "DVD/VTS_01_1.VOB", "VOB"),
    ("asf", "old.wmv", "WMV"),
    ("asf", "old.asf", "ASF"),
    ("matroska,webm", "show.mkv", "MKV"),
    ("matroska,webm", "clip.webm", "WebM"),
    ("matroska,webm", "clip.mp4", "MKV"),          # the extension only picks within a family
    ("flv", "x.flv", "FLV"),                       # not in the table: ffprobe's name
    (None, "broken.avi", "AVI"),                   # unreadable: the extension
    (None, "Folder.v2/noext", None),
])
def test_video_type(container, path, expected):
    assert video_type(container, path) == expected


@pytest.fixture
def mixed(client, media_root, fake_probe, monkeypatch):
    """Videos that play different ways (the fake probe answers by file name here)."""
    facts = {
        "direct.mp4": ("mov,mp4,m4a,3gp,3g2,mj2", "h264", "aac"),
        "renamed.mp4": ("mpegts", "h264", "aac"),
        "tape.avi": ("avi", "mpeg4", "mp3"),
        "phone.mp4": ("mov,mp4,m4a,3gp,3g2,mj2", "hevc", "aac"),
    }

    def by_name(self, path):
        container, v, a = facts[path.name]
        return ProbeResult(container, v, a, "yuv420p", 640, 360, 30.0)

    monkeypatch.setattr(type(fake_probe), "__call__", by_name)
    make_files(media_root, *[f"Cards/{n}" for n in facts])
    lib = client.post("/api/libraries", json={"name": "Media", "path": str(media_root)}).json()["id"]
    client.post(f"/api/libraries/{lib}/scan")
    client.scans.wait_idle()
    return lib


def cards(client, lib, **params):
    res = client.get(f"/api/libraries/{lib}/browse", params={"path": "Cards", **params})
    assert res.status_code == 200, res.text
    return {i["rel_path"].rsplit("/", 1)[-1]: i for i in res.json()["items"]}


def test_cards_say_the_format_and_how_this_browser_plays_it(client, mixed):
    items = cards(client, mixed)
    assert {n: (i["type"], i["play_mode"]) for n, i in items.items()} == {
        "direct.mp4": ("MP4", "direct"),
        "renamed.mp4": ("TS", "remux"),
        "tape.avi": ("AVI", "transcode"),
        "phone.mp4": ("MP4", "transcode"),         # a typical browser has no HEVC
    }


def test_play_mode_follows_the_browser(client, mixed):
    hevc = cards(client, mixed, video="h264,hevc", audio="aac")
    assert hevc["phone.mp4"]["play_mode"] == "direct"
    safari = cards(client, mixed, video="h264", audio="aac", hls_support="native")
    assert safari["renamed.mp4"]["play_mode"] == "transcode"   # Safari streams only as HLS
    assert cards(client, mixed, hls_support="bogus")["renamed.mp4"]["play_mode"] == "remux"


def test_cards_match_the_video_page(client, mixed):
    """For the same browser, a card's play_mode is what /plan (its page, the player) says."""
    for params in ({}, {"video": "h264,hevc", "audio": "aac"}, {"video": "h264", "audio": "aac", "hls_support": "native"},
                   {"video": "", "audio": ""}):
        for item in cards(client, mixed, **params).values():
            page = client.get(f"/api/items/{item['id']}/plan", params=params).json()
            assert item["play_mode"] == page["mode"], (item["rel_path"], params)


def test_show_all_and_tag_cards_carry_the_same_facts(client, mixed):
    listed = cards(client, mixed, all="true", video="h264,hevc", audio="aac")
    assert listed["phone.mp4"]["play_mode"] == "direct" and listed["renamed.mp4"]["type"] == "TS"
    item = listed["renamed.mp4"]
    tag = client.post(f"/api/items/{item['id']}/tags", json={"name": "check"}).json()[0]
    tagged = client.get(f"/api/tags/{tag['id']}", params={"video": "h264", "audio": "aac"}).json()["items"][0]
    assert (tagged["rel_path"], tagged["type"], tagged["play_mode"]) == ("Cards/renamed.mp4", "TS", "remux")


def test_plan_agrees_on_a_row_like_mapping():
    """item_out hands plan() a database row; plan() also takes a dict (sanity check)."""
    facts = {"container": "mpegts", "video_codec": "h264", "audio_codec": "aac", "pix_fmt": "yuv420p",
             "interlaced": 0, "probe_error": None, "rel_path": "a.mp4", "duration": 5.0}
    assert plan(facts, Capabilities()).mode == "remux"
