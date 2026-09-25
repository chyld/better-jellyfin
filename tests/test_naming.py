"""Titles, years and poster matching, using names from the real library."""
import pytest

from reel.scanner import derive_title, find_poster, is_video


@pytest.mark.parametrize(
    "rel_path, title, year",
    [
        ("Tapes/1992.zoo-trip.mpg", "zoo-trip", 1992),
        ("Tapes/1996.fourth-of-july-at-the-lake.mpg", "fourth-of-july-at-the-lake", 1996),
        ("Tapes/1992.parade-downtown.mpg", "parade-downtown", 1992),
        ("Tapes/lecture.asf", "lecture", None),
        ("Camcorder/clip01.avi", "clip01", None),
        ("Classics/0360/movie.mp4", "0360", None),
        ("Classics/0370/Movie.MKV", "0370", None),
        ("Drama/0902/rough-cut.mp4", "rough-cut", None),  # not alone in its folder: keeps its own name
        ("Lectures/session14-01-2022.mp4", "session14-01-2022", None),
        ("Lectures/zoom_20201118-135453_2560x1080.mp4",
         "zoom_20201118-135453_2560x1080", None),
        ("movie.mp4", "movie", None),  # generic name at the library root: nothing better to use
        ("2020.mp4", "2020", None),  # a bare year is a title, not a prefix
    ],
)
def test_derive_title(rel_path, title, year):
    assert derive_title(rel_path) == (title, year)


@pytest.mark.parametrize(
    "rel_path, title, year",
    [
        ("Drama/0902/rough-cut.mp4", "0902", None),
        ("Drama/0901/sample-reel.mp4", "0901", None),
        ("Tapes/1992.zoo-trip/tape1.mpg", "zoo-trip", 1992),  # folder names get the year treatment too
        ("rough-cut.mp4", "rough-cut", None),  # at the library root there's no folder name to use
    ],
)
def test_derive_title_for_only_video_in_folder(rel_path, title, year):
    assert derive_title(rel_path, alone_in_folder=True) == (title, year)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("clip01.avi", True), ("movie.MP4", True), ("a.mkv", True), ("tape.mpg", True),
        ("clip.wmv", True), ("clip.asf", True), ("clip.mov", True),
        ("clip01.png", False), ("folder.jpg", False), ("notes.txt", False),
        (".hidden.mp4", False), ("mp4", False),
    ],
)
def test_is_video(name, expected):
    assert is_video(name) is expected


def listing(*names):
    return {n.lower(): n for n in names}


@pytest.mark.parametrize(
    "video, files, poster",
    [
        ("zombie.mp4", ["zombie.mp4", "zombie.png"], "zombie.png"),
        ("movie.mp4", ["movie.mp4", "movie.jpg"], "movie.jpg"),
        ("clip01.avi", ["clip01.avi", "clip01.png", "clip02.avi", "clip02.png"], "clip01.png"),
        ("1992.zoo-trip.mpg", ["1992.zoo-trip.mpg", "1992.zoo-trip.png"], "1992.zoo-trip.png"),
        ("movie.mp4", ["movie.mp4", "MOVIE.JPG"], "MOVIE.JPG"),  # any capitalisation
        ("clip.mp4", ["clip.mp4", "clip.webp"], "clip.webp"),
        ("clip.mp4", ["clip.mp4", "clip.jpeg"], "clip.jpeg"),
    ],
)
def test_video_preview_is_the_image_with_the_same_name(video, files, poster):
    assert find_poster(video, listing(*files)) == poster


@pytest.mark.parametrize(
    "video, files",
    [
        ("rough-cut.mp4", ["rough-cut.mp4"]),
        # folder.* is only ever a folder's preview, even for a lone video.
        ("rough-cut.mp4", ["rough-cut.mp4", "folder.jpg"]),
        ("rough-cut.mp4", ["rough-cut.mp4", "folder.png", "poster.jpg", "cover.png"]),
        # Another video's image doesn't count.
        ("rough-cut.mp4", ["rough-cut.mp4", "movie.png"]),
        ("zombie.mp4", ["zombie.mp4", "zombie.txt", "zombie2.png"]),
    ],
)
def test_no_video_preview(video, files):
    assert find_poster(video, listing(*files)) is None
