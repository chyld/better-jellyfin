"""Natural sorting ("clip2" before "clip10"), in Python and in SQL.

natural_key() is for sorting small lists in Python. sort_key() turns the same
order into plain text (numbers zero-padded), so it can be stored in the
database, indexed, and used in ORDER BY with LIMIT/OFFSET.
"""
import re

_DIGITS = re.compile(r"\d+")
PAD = 20


def natural_key(text: str) -> list:
    """Sort 'clip2' before 'clip10' and '0360' after '0305' (numbers compare as numbers)."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def natural_text(text: str) -> str:
    """natural_key() as a string: lowercase, with every number padded to PAD digits."""
    return _DIGITS.sub(lambda m: m.group().lstrip("0").rjust(PAD, "0")[-PAD:], text.lower())


def sort_key(title: str, rel_path: str) -> str:
    """How a video sorts by name: its title, then its path to break ties."""
    return natural_text(title) + "\x00" + natural_text(rel_path)


def path_key(rel_path: str) -> str:
    """How a video sorts by its full path, for "show all" lists.

    "/" becomes "\x01", which sorts before any character in a name, so a folder's
    videos follow the folder's own name at once ("A/x" before "A b/y"), matching
    the order folders are listed in. The raw path breaks ties ("Clip" / "clip").
    """
    return natural_text(rel_path).replace("/", "\x01") + "\x00" + rel_path


def parent_dir(rel_path: str) -> str:
    """The folder a video sits in, relative to its library ('' at the top)."""
    return rel_path.rsplit("/", 1)[0] if "/" in rel_path else ""
