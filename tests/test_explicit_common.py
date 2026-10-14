import pytest

from antra.core.explicit_common import (
    STATUS_CLEAN_EDIT,
    STATUS_EXPLICIT,
    STATUS_NOT_EXPLICIT,
    STATUS_UNKNOWN,
    advisory_value,
    comparable_title,
    explicit_from_content_rating,
    explicit_status,
    looks_clean,
    strip_clean_markers,
)


@pytest.mark.parametrize(
    "rating, expected",
    [
        ({"label": "EXPLICIT"}, True),
        ({"label": "explicit"}, True),
        ({"label": "NONE"}, False),
        ({"label": ""}, None),
        ({}, None),
        (None, None),
        ("EXPLICIT", None),
    ],
)
def test_explicit_from_content_rating(rating, expected):
    assert explicit_from_content_rating(rating) is expected


@pytest.mark.parametrize(
    "title, expected",
    [
        ("HUMBLE.", "HUMBLE."),
        ("HUMBLE. (Clean)", "HUMBLE."),
        ("HUMBLE. [Clean Version]", "HUMBLE."),
        ("HUMBLE. - Radio Edit", "HUMBLE."),
        ("HUMBLE. - Clean (Radio Edit)", "HUMBLE."),
        ("Cleaning Out My Closet", "Cleaning Out My Closet"),
        ("Clean", "Clean"),
    ],
)
def test_strip_clean_markers(title, expected):
    assert strip_clean_markers(title) == expected


def test_comparable_title_matches_clean_and_explicit_names():
    assert comparable_title("Mask Off - Clean") == comparable_title("Mask Off")
    assert comparable_title("Mask Off (Remix)") != comparable_title("Mask Off - Live")


def test_looks_clean():
    assert looks_clean("Song (Radio Edit)")
    assert looks_clean("Song - Clean Version")
    assert not looks_clean("Cleaning Out My Closet")
    assert not looks_clean(None)


@pytest.mark.parametrize(
    "target, result, title, expected",
    [
        (True, True, "Song", STATUS_EXPLICIT),
        (True, False, "Song", STATUS_CLEAN_EDIT),
        (True, None, "Song", STATUS_UNKNOWN),
        (True, None, "Song (Clean)", STATUS_CLEAN_EDIT),
        (False, True, "Song", STATUS_EXPLICIT),
        (False, False, "Song", STATUS_NOT_EXPLICIT),
        (False, None, "Song", STATUS_NOT_EXPLICIT),
        (None, True, "Song", STATUS_EXPLICIT),
        (None, False, "Song", STATUS_UNKNOWN),
        (None, None, "Song", STATUS_UNKNOWN),
    ],
)
def test_explicit_status_table(target, result, title, expected):
    assert explicit_status(target, result, title) == expected


def test_isrc_match_inherits_the_requested_recordings_flag():
    # Twin swapped to the explicit ISRC; the Tidal mirror gives no flag.
    assert explicit_status(True, None, "Song", isrc_match=True, original_is_explicit=True) == STATUS_EXPLICIT
    # Matched the clean recording's ISRC while the explicit one was wanted.
    assert explicit_status(True, None, "Song", isrc_match=True, original_is_explicit=False) == STATUS_CLEAN_EDIT
    # A title match alone proves nothing.
    assert explicit_status(True, None, "Song", isrc_match=False, original_is_explicit=True) == STATUS_UNKNOWN
    # A source's own flag wins over the inherited one.
    assert explicit_status(True, False, "Song", isrc_match=True, original_is_explicit=True) == STATUS_CLEAN_EDIT


def test_a_marker_in_the_requested_title_is_not_a_clean_edit():
    assert explicit_status(True, None, "Song (Radio Edit)", original_title="Song (Radio Edit)") == STATUS_UNKNOWN
    assert explicit_status(True, None, "Song (Radio Edit)", original_title="Song") == STATUS_CLEAN_EDIT


def test_advisory_value():
    assert advisory_value(STATUS_EXPLICIT) == 1
    assert advisory_value(STATUS_CLEAN_EDIT) == 2
    assert advisory_value(STATUS_NOT_EXPLICIT) is None
    assert advisory_value(STATUS_UNKNOWN) is None
    assert advisory_value(None) is None
