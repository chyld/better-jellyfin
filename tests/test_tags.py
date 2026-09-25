"""Tagging videos, and listing the videos with a tag."""
import pytest

from conftest import make_files


@pytest.fixture
def videos(client, media_root):
    """Scan a small library and return {title: video id}."""
    make_files(media_root, "Tapes/1992.zoo-trip.mpg", "Tapes/1997.holiday.mpg", "Tapes/1998.hiking.mpg",
               "Lectures/session1.mp4", "Lectures/session2.mp4")
    lib = client.post("/api/libraries", json={"name": "Media", "path": str(media_root)}).json()
    client.post(f"/api/libraries/{lib['id']}/scan")
    client.scans.wait_idle()
    ids = {}
    for folder in ("Tapes", "Lectures"):
        page = client.get(f"/api/libraries/{lib['id']}/browse", params={"path": folder}).json()
        ids.update({i["title"]: i["id"] for i in page["items"]})
    ids["_library"] = lib["id"]
    return ids


def tag(client, video, name):
    return client.post(f"/api/items/{video}/tags", json={"name": name})


def names(tags):
    return [t["name"] for t in tags]


def test_add_tags_to_a_video(client, videos):
    res = tag(client, videos["zoo-trip"], "family")
    assert res.status_code == 200
    assert names(res.json()) == ["family"]
    tag(client, videos["zoo-trip"], "1990s")
    tag(client, videos["zoo-trip"], "road-trip")
    item = client.get(f"/api/items/{videos['zoo-trip']}").json()
    assert names(item["tags"]) == ["family", "1990s", "road-trip"]  # in the order added


def test_as_many_tags_as_you_like(client, videos):
    for i in range(40):
        tag(client, videos["zoo-trip"], f"tag-{i}")
    assert len(client.get(f"/api/items/{videos['zoo-trip']}").json()["tags"]) == 40


def test_new_videos_have_no_tags(client, videos):
    assert client.get(f"/api/items/{videos['hiking']}").json()["tags"] == []


def test_tags_are_shared(client, videos):
    first = tag(client, videos["zoo-trip"], "family").json()[0]
    second = tag(client, videos["holiday"], "family").json()[0]
    assert second == first  # the same tag
    assert client.get("/api/tags").json() == [{"id": first["id"], "name": "family", "image": None, "count": 2}]


def test_adding_a_tag_twice_does_nothing(client, videos):
    tag(client, videos["zoo-trip"], "family")
    assert names(tag(client, videos["zoo-trip"], "family").json()) == ["family"]


@pytest.mark.parametrize(
    "name, stored",
    [
        ("family", "family"),
        ("1990s", "1990s"),
        ("road-trip", "road-trip"),
        ("  vhs  ", "vhs"),          # surrounding spaces are ignored
        ("2", "2"),
    ],
)
def test_good_tag_names(client, videos, name, stored):
    assert names(tag(client, videos["zoo-trip"], name).json()) == [stored]


@pytest.mark.parametrize(
    "name",
    ["", "   ", "x" * 51, "Family", "Road-Trip", "FAMILY", "road trip", "road_trip", "road.trip", "#family", "tag!", "café", "a/b", "a,b", "ｆａｍｉｌｙ"],
)
def test_bad_tag_names(client, videos, name):
    res = tag(client, videos["zoo-trip"], name)
    assert res.status_code == 400
    assert res.json()["detail"]
    assert client.get("/api/tags").json() == []


def test_bad_tag_message_explains_the_rule(client, videos):
    detail = tag(client, videos["zoo-trip"], "road trip").json()["detail"]
    assert "lowercase a-z, 0-9 and dashes" in detail


def test_capitals_are_rejected_not_changed(client, videos):
    res = tag(client, videos["zoo-trip"], "Family")
    assert res.status_code == 400
    assert '"Family"' in res.json()["detail"]


def test_fifty_characters_is_fine(client, videos):
    assert tag(client, videos["zoo-trip"], "x" * 50).status_code == 200


def test_remove_a_tag(client, videos):
    family = tag(client, videos["zoo-trip"], "family").json()[0]
    tag(client, videos["zoo-trip"], "1990s")
    tag(client, videos["holiday"], "family")
    res = client.delete(f"/api/items/{videos['zoo-trip']}/tags/{family['id']}")
    assert res.status_code == 200
    assert names(res.json()) == ["1990s"]
    # The tag is still on the other video.
    assert names(client.get(f"/api/items/{videos['holiday']}").json()["tags"]) == ["family"]


def test_tag_with_no_videos_left_disappears(client, videos):
    solo = tag(client, videos["zoo-trip"], "solo").json()[0]
    client.delete(f"/api/items/{videos['zoo-trip']}/tags/{solo['id']}")
    assert client.get("/api/tags").json() == []
    assert client.get(f"/api/tags/{solo['id']}").status_code == 404
    # Tagging again later just makes a new tag.
    assert names(tag(client, videos["hiking"], "solo").json()) == ["solo"]


def test_home_tag_list(client, videos):
    for video in ("zoo-trip", "holiday", "hiking"):
        tag(client, videos[video], "family")
    tag(client, videos["session1"], "coding")
    tag(client, videos["zoo-trip"], "anniversary")
    listed = [(t["name"], t["count"]) for t in client.get("/api/tags").json()]
    assert listed == [("anniversary", 1), ("coding", 1), ("family", 3)]


def test_videos_with_a_tag(client, videos):
    for video in ("hiking", "zoo-trip", "holiday"):
        family = tag(client, videos[video], "family").json()[0]
    tag(client, videos["session1"], "coding")
    res = client.get(f"/api/tags/{family['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["tag"] == {"id": family["id"], "name": "family", "image": None}
    # Not sorted: in the order they were tagged.
    assert [i["title"] for i in body["items"]] == ["hiking", "zoo-trip", "holiday"]
    assert set(body["items"][0]) == {
        "id", "title", "year", "duration", "width", "height", "play_mode", "has_poster", "custom_image",
    }


def test_unknown_ids(client, videos):
    assert tag(client, "not-a-video", "x").status_code == 404
    assert client.get("/api/tags/not-a-tag").status_code == 404
    real = tag(client, videos["zoo-trip"], "family").json()[0]
    assert client.delete(f"/api/items/{videos['zoo-trip']}/tags/not-a-tag").status_code == 404
    assert client.delete(f"/api/items/not-a-video/tags/{real['id']}").status_code == 404


def test_tags_survive_rescans(client, videos, media_root):
    tag(client, videos["zoo-trip"], "family")
    (media_root / "Tapes/1992.zoo-trip.mpg").write_bytes(b"changed")  # forces a re-probe
    client.post(f"/api/libraries/{videos['_library']}/scan")
    client.scans.wait_idle()
    assert names(client.get(f"/api/items/{videos['zoo-trip']}").json()["tags"]) == ["family"]


def test_deleted_video_leaves_its_tags(client, videos, media_root):
    family = tag(client, videos["zoo-trip"], "family").json()[0]
    tag(client, videos["holiday"], "family")
    tag(client, videos["zoo-trip"], "only-zoo-trip")
    (media_root / "Tapes/1992.zoo-trip.mpg").unlink()
    client.post(f"/api/libraries/{videos['_library']}/scan")
    client.scans.wait_idle()
    assert [i["title"] for i in client.get(f"/api/tags/{family['id']}").json()["items"]] == ["holiday"]
    # A tag whose only video is gone no longer shows on Home.
    assert [t["name"] for t in client.get("/api/tags").json()] == ["family"]


def test_removing_a_library_removes_its_videos_from_tags(client, videos):
    tag(client, videos["zoo-trip"], "family")
    assert client.delete(f"/api/libraries/{videos['_library']}").status_code == 204
    assert client.get("/api/tags").json() == []


# ---- Managing tags: rename, merge, delete ------------------------------------------


def tag_named(client, name):
    return next(t for t in client.get("/api/tags").json() if t["name"] == name)


def test_rename_a_tag(client, videos):
    tag(client, videos["zoo-trip"], "famly")
    tag(client, videos["holiday"], "famly")
    old = tag_named(client, "famly")
    res = client.patch(f"/api/tags/{old['id']}", json={"name": "family"})
    assert res.status_code == 200
    assert res.json() == {"id": old["id"], "name": "family", "image": None, "count": 2, "merged": False}
    # Every video with it shows the new name.
    assert names(client.get(f"/api/items/{videos['holiday']}").json()["tags"]) == ["family"]


def test_rename_to_the_same_name(client, videos):
    t = tag(client, videos["zoo-trip"], "family").json()[0]
    res = client.patch(f"/api/tags/{t['id']}", json={"name": "family"})
    assert res.json() == {"id": t["id"], "name": "family", "image": None, "count": 1, "merged": False}


@pytest.mark.parametrize("name", ["", "Family", "road trip", "x" * 51, "a_b"])
def test_rename_follows_the_tag_rule(client, videos, name):
    t = tag(client, videos["zoo-trip"], "family").json()[0]
    res = client.patch(f"/api/tags/{t['id']}", json={"name": name})
    assert res.status_code == 400
    assert tag_named(client, "family")["id"] == t["id"]  # unchanged


def test_rename_onto_an_existing_tag_merges_them(client, videos):
    tag(client, videos["zoo-trip"], "road-trip")
    tag(client, videos["hiking"], "roadtrip")
    tag(client, videos["holiday"], "roadtrip")
    tag(client, videos["holiday"], "road-trip")  # on both: must not be doubled
    keep, drop = tag_named(client, "road-trip"), tag_named(client, "roadtrip")

    res = client.patch(f"/api/tags/{drop['id']}", json={"name": "road-trip"})
    assert res.status_code == 200
    assert res.json() == {"id": keep["id"], "name": "road-trip", "image": None, "count": 3, "merged": True}

    assert [t["name"] for t in client.get("/api/tags").json()] == ["road-trip"]
    assert client.get(f"/api/tags/{drop['id']}").status_code == 404
    merged = client.get(f"/api/tags/{keep['id']}").json()["items"]
    # In the order each video was tagged, across both tags: zoo-trip (road-trip),
    # hiking (roadtrip), then holiday (road-trip; its roadtrip tag merged away).
    assert [i["title"] for i in merged] == ["zoo-trip", "hiking", "holiday"]
    assert names(client.get(f"/api/items/{videos['holiday']}").json()["tags"]) == ["road-trip"]


def test_delete_a_tag(client, videos):
    tag(client, videos["zoo-trip"], "family")
    tag(client, videos["zoo-trip"], "vhs")
    tag(client, videos["holiday"], "family")
    family = tag_named(client, "family")
    assert client.delete(f"/api/tags/{family['id']}").status_code == 204
    assert [t["name"] for t in client.get("/api/tags").json()] == ["vhs"]
    assert names(client.get(f"/api/items/{videos['zoo-trip']}").json()["tags"]) == ["vhs"]
    assert client.get(f"/api/items/{videos['holiday']}").json()["tags"] == []
    # The videos themselves are still there.
    assert client.get(f"/api/items/{videos['holiday']}").status_code == 200


def test_manage_unknown_tag(client, videos):
    assert client.patch("/api/tags/nope", json={"name": "x"}).status_code == 404
    assert client.delete("/api/tags/nope").status_code == 404
