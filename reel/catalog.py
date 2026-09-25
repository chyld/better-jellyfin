"""The catalog's shared vocabulary: used by the listings (browse.py) and by what
changes the catalog (tags, pictures). Nothing here renders a page."""

PAGE_SIZE = 200      # videos per page when the caller doesn't say
MAX_PAGE_SIZE = 500


class NotFound(LookupError):
    pass


def clean_dir(rel_dir: str | None) -> str:
    """Normalise a folder path from the URL; reject anything that climbs out."""
    parts = [p for p in (rel_dir or "").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise NotFound("Folder not found.")
    return "/".join(parts)


def page_bounds(limit: int | None, offset: int | None) -> tuple[int, int]:
    limit = PAGE_SIZE if limit is None else max(1, min(int(limit), MAX_PAGE_SIZE))
    return limit, max(0, int(offset or 0))


def descendants(rel_dir: str) -> tuple[str, str | None]:
    """The index range of parent_dir values at or below a folder (not the folder itself).

    Everything under "Show 07" sorts between "Show 07/" and "Show 070", because
    "/" comes just before "0".
    """
    if not rel_dir:
        return "\x01", None          # any non-empty parent_dir: every subfolder
    return rel_dir + "/", rel_dir + "0"
