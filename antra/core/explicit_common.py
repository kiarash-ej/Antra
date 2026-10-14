"""
Shared vocabulary for the explicit-version feature: reading Spotify's explicit
flag, recognising clean-edit titles, and the per-song explicit status that the
resolver, the tagger and the desktop UI all agree on.

Pure functions only, so every rule here is testable offline.
"""
import re
from typing import Optional

from antra.utils.matching import normalize

# Per-song outcome, reported to the UI and written to file tags.
STATUS_EXPLICIT = "explicit"          # got the explicit version
STATUS_CLEAN_EDIT = "clean_edit"      # an explicit version exists, only a clean copy was found
STATUS_NOT_EXPLICIT = "not_explicit"  # the song has no explicit version
STATUS_UNKNOWN = "unknown"            # could not tell

# Same markers the resolver has always treated as a clean edition.
CLEAN_VERSION_RE = re.compile(
    r"\b(radio\s*edit|clean(\s+(version|edit|mix))?|edited(\s+version)?|censored)\b",
    re.IGNORECASE,
)

# A marker as a trailing "(Clean)", "[Radio Edit]" or " - Clean Version".
_MARKER_SUFFIX_RE = re.compile(
    r"\s*(?:[\(\[]\s*(?:radio\s*edit|clean(?:\s+(?:version|edit|mix))?|edited(?:\s+version)?|censored)\s*[\)\]]"
    r"|-\s*(?:radio\s*edit|clean(?:\s+(?:version|edit|mix))?|edited(?:\s+version)?|censored))\s*$",
    re.IGNORECASE,
)


def explicit_from_content_rating(content_rating) -> Optional[bool]:
    """Spotify GraphQL `contentRating` to an explicit flag.

    `{"label": "EXPLICIT"}` is explicit and any other label is clean. A missing
    or malformed field is unknown rather than clean: treating it as clean would
    send every song from a response that simply lacks the field into a twin
    search.
    """
    if not isinstance(content_rating, dict):
        return None
    label = content_rating.get("label")
    if not isinstance(label, str) or not label:
        return None
    return label.upper() == "EXPLICIT"


def looks_clean(title: Optional[str]) -> bool:
    """True when a title names itself a clean edition ("Song (Clean)")."""
    return bool(title) and bool(CLEAN_VERSION_RE.search(title))


def strip_clean_markers(title: str) -> str:
    """Remove trailing clean-edition markers, repeatedly ("Song - Clean (Radio Edit)")."""
    previous = None
    while previous != title:
        previous = title
        title = _MARKER_SUFFIX_RE.sub("", title)
    return title.strip()


def comparable_title(title: str) -> str:
    """The form two titles are compared in when matching a clean song to its twin."""
    return normalize(strip_clean_markers(title or ""))


def explicit_status(
    target_explicit: Optional[bool],
    result_is_explicit: Optional[bool],
    result_title: Optional[str] = None,
    *,
    original_title: Optional[str] = None,
    isrc_match: bool = False,
    original_is_explicit: Optional[bool] = None,
) -> str:
    """The status table from the design.

    `target_explicit`: True when the song (or its Spotify twin) is explicit,
    False when Spotify confirmed it has no explicit version, None when unknown.

    An ISRC names one recording, and the explicit and clean versions carry
    different ones. So a result matched on the requested song's ISRC is that
    recording, and inherits its flag when the source gives none (the Tidal
    mirror and Deezer never flag). Without this, every twin-swapped song
    fetched from those sources would read as unknown.

    A result whose title marks it as a clean edition counts as clean even when
    the source did not flag it, unless the requested title carries the same
    marker (a song genuinely called "... (Radio Edit)").
    """
    if result_is_explicit is None and isrc_match and original_is_explicit is not None:
        result_is_explicit = original_is_explicit
    if result_is_explicit is True:
        return STATUS_EXPLICIT
    result_clean = result_is_explicit is False or (
        looks_clean(result_title) and not looks_clean(original_title)
    )
    if target_explicit is True:
        return STATUS_CLEAN_EDIT if result_clean else STATUS_UNKNOWN
    if target_explicit is False:
        return STATUS_NOT_EXPLICIT
    return STATUS_UNKNOWN


def advisory_value(status: Optional[str]) -> Optional[int]:
    """iTunes advisory value for a status: 1 explicit, 2 clean edit, else none."""
    if status == STATUS_EXPLICIT:
        return 1
    if status == STATUS_CLEAN_EDIT:
        return 2
    return None
