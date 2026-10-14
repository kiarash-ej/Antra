"""
Explicit ranking in the resolver: with "Prefer explicit versions" on and an
explicit target, an explicit copy outranks a better-quality clean one; in every
other case the sort keys order results exactly as upstream did.
"""
import pytest

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.core.resolver import SourceResolver, effective_target_explicit
from antra.sources.base import BaseSourceAdapter


class FakeAdapter(BaseSourceAdapter):
    def __init__(self, name, priority=1, result=None, always_lossy=False):
        self.name = name
        self.priority = priority
        self.always_lossy = always_lossy
        self._result = result

    def is_available(self):
        return True

    def search(self, track):
        return self._result

    def download(self, result, output_path):
        raise NotImplementedError


def make_track(**overrides):
    fields = dict(title="HUMBLE.", artists=["Kendrick Lamar"], album="DAMN.", duration_ms=177000)
    fields.update(overrides)
    return TrackMetadata(**fields)


def make_result(
    source,
    *,
    bit_depth=16,
    explicit=None,
    title="HUMBLE.",
    lossless=True,
    score=0.95,
):
    return SearchResult(
        source=source,
        title=title,
        artists=["Kendrick Lamar"],
        album="DAMN.",
        duration_ms=177000,
        audio_format=AudioFormat.FLAC if lossless else AudioFormat.AAC,
        quality_kbps=None if lossless else 320,
        is_lossless=lossless,
        download_url=None,
        stream_id=f"{source}-id",
        similarity_score=score,
        bit_depth=bit_depth if lossless else None,
        sample_rate_hz=44100,
        is_explicit=explicit,
    )


def keys(resolver, result, adapter, track):
    return (
        resolver._lossless_sort_key(result, adapter, track),
        resolver._candidate_key(result, adapter, track),
    )


# ── Effective target ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target, is_explicit, expected",
    [
        (True, None, True),
        (False, None, False),
        (None, None, None),
        # Paths the twin step never touches still want an explicit song explicit.
        (None, True, True),
        # A clean flag on its own is not "no explicit version exists": Apple
        # storefronts list only the clean edition of some songs.
        (None, False, None),
    ],
)
def test_effective_target_explicit(target, is_explicit, expected):
    track = make_track(target_explicit=target, is_explicit=is_explicit)
    assert effective_target_explicit(track) is expected


def test_effective_target_of_no_track_is_unknown():
    assert effective_target_explicit(None) is None


# ── Sort keys ─────────────────────────────────────────────────────────────────

# A clean copy is recognised by its flag or, when the source has no flag, by a
# clean marker in its title.
CLEAN_24 = [
    pytest.param(dict(explicit=False), id="flagged-clean"),
    pytest.param(dict(explicit=None, title="HUMBLE. (Clean)"), id="titled-clean"),
    pytest.param(dict(explicit=None), id="unknown"),
]


@pytest.mark.parametrize("clean_fields", CLEAN_24)
def test_explicit_16bit_outranks_better_24bit_copy_in_both_keys(clean_fields):
    resolver = SourceResolver([], preferred_output_format="source", prefer_explicit=True)
    track = make_track(target_explicit=True)
    qobuz, tidal = FakeAdapter("qobuz"), FakeAdapter("tidal")
    explicit_16 = make_result("qobuz", bit_depth=16, explicit=True)
    other_24 = make_result("tidal", bit_depth=24, **clean_fields)

    lossless_e, candidate_e = keys(resolver, explicit_16, qobuz, track)
    lossless_c, candidate_c = keys(resolver, other_24, tidal, track)

    assert lossless_e > lossless_c
    assert candidate_e > candidate_c


def test_explicit_rank_values():
    resolver = SourceResolver([], prefer_explicit=True)
    track = make_track(target_explicit=True)
    assert resolver._explicit_rank(track, make_result("q", explicit=True)) == 2
    assert resolver._explicit_rank(track, make_result("q", explicit=None)) == 1
    assert resolver._explicit_rank(track, make_result("q", explicit=False)) == 0
    assert resolver._explicit_rank(track, make_result("q", title="HUMBLE. - Radio Edit")) == 0
    # A marker the requested title carries too is the song's name, not an edit.
    radio_track = make_track(title="Song (Radio Edit)", target_explicit=True)
    assert resolver._explicit_rank(radio_track, make_result("q", title="Song (Radio Edit)")) == 1


def test_explicit_rank_quality_still_decides_between_equal_ranks():
    resolver = SourceResolver([], preferred_output_format="source", prefer_explicit=True)
    track = make_track(target_explicit=True)
    qobuz, tidal = FakeAdapter("qobuz"), FakeAdapter("tidal")
    explicit_16 = make_result("qobuz", bit_depth=16, explicit=True)
    explicit_24 = make_result("tidal", bit_depth=24, explicit=True)
    assert keys(resolver, explicit_24, tidal, track)[0] > keys(resolver, explicit_16, qobuz, track)[0]
    assert keys(resolver, explicit_24, tidal, track)[1] > keys(resolver, explicit_16, qobuz, track)[1]


INACTIVE = [
    pytest.param(False, dict(target_explicit=True), id="setting-off"),
    pytest.param(True, dict(target_explicit=False), id="target-false"),
    pytest.param(True, dict(target_explicit=None), id="target-none"),
    pytest.param(True, dict(target_explicit=None, is_explicit=False), id="target-none-flag-clean"),
]


@pytest.mark.parametrize("prefer_explicit, track_fields", INACTIVE)
def test_keys_match_upstream_when_rank_is_inactive(prefer_explicit, track_fields):
    resolver = SourceResolver([], preferred_output_format="source", prefer_explicit=prefer_explicit)
    track = make_track(**track_fields)
    qobuz, tidal = FakeAdapter("qobuz", priority=1), FakeAdapter("tidal", priority=2)
    explicit_16 = make_result("qobuz", bit_depth=16, explicit=True)
    clean_24 = make_result("tidal", bit_depth=24, explicit=False)

    lossless_e, candidate_e = keys(resolver, explicit_16, qobuz, track)
    lossless_c, candidate_c = keys(resolver, clean_24, tidal, track)

    # Upstream's penalty on a clean result whenever the setting is on is kept.
    clean_similarity = 0.95 - 0.20 if prefer_explicit else 0.95
    # Upstream tuples: (native, bit depth, source match, -priority, rate, kbps, isrc, similarity)
    # and (tier, rate, kbps, isrc, similarity, -priority), behind a constant rank.
    assert lossless_e == pytest.approx((0, 0, 16, 0, -1, 44100, 0, 0, 0.95))
    assert lossless_c == pytest.approx((0, 0, 24, 0, -2, 44100, 0, 0, clean_similarity))
    assert candidate_e == pytest.approx((0, 3.0, 44100, 0, 0, 0.95, -1))
    assert candidate_c == pytest.approx((0, 4.0, 44100, 0, 0, clean_similarity, -2))
    # So quality still wins, as upstream.
    assert lossless_c > lossless_e
    assert candidate_c > candidate_e


# ── _result_looks_clean ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "prefer_explicit, track_fields, expected",
    [
        (True, dict(target_explicit=True), True),
        (True, dict(is_explicit=True), True),
        (True, dict(target_explicit=None), False),
        (True, dict(target_explicit=False), False),
        (False, dict(target_explicit=True), False),
    ],
)
def test_result_looks_clean_uses_effective_target(prefer_explicit, track_fields, expected):
    resolver = SourceResolver([], prefer_explicit=prefer_explicit)
    track = make_track(**track_fields)
    assert resolver._result_looks_clean(track, make_result("q", explicit=False)) is expected


# ── resolve() ─────────────────────────────────────────────────────────────────


def resolve_with(fmt, prefer_explicit, track, *adapters):
    resolver = SourceResolver(list(adapters), preferred_output_format=fmt, prefer_explicit=prefer_explicit)
    return resolver.resolve(track)


@pytest.mark.parametrize("fmt", ["source", "flac", "lossless"])
def test_resolve_picks_explicit_16bit_over_unknown_24bit(fmt):
    qobuz = FakeAdapter("qobuz", priority=1, result=make_result("qobuz", bit_depth=16, explicit=True))
    tidal = FakeAdapter("tidal", priority=2, result=make_result("tidal", bit_depth=24, explicit=None))
    result, adapter = resolve_with(fmt, True, make_track(target_explicit=True), qobuz, tidal)
    assert adapter is qobuz


@pytest.mark.parametrize(
    "prefer_explicit, target",
    [(True, None), (True, False), (False, True)],
)
def test_resolve_keeps_upstream_quality_order_when_rank_is_inactive(prefer_explicit, target):
    qobuz = FakeAdapter("qobuz", priority=1, result=make_result("qobuz", bit_depth=16, explicit=True))
    tidal = FakeAdapter("tidal", priority=2, result=make_result("tidal", bit_depth=24, explicit=None))
    result, adapter = resolve_with("flac", prefer_explicit, make_track(target_explicit=target), qobuz, tidal)
    assert adapter is tidal


@pytest.mark.parametrize("fmt", ["source", "flac", "lossless-16"])
def test_resolve_downloads_the_clean_copy_when_no_source_has_explicit(fmt):
    # The design's rule: a song whose explicit version cannot be found downloads
    # as the clean edit rather than failing.
    tidal = FakeAdapter("tidal", priority=2, result=make_result("tidal", bit_depth=24, explicit=False))
    resolution = resolve_with(fmt, True, make_track(target_explicit=True), tidal)
    assert resolution is not None
    assert resolution[1] is tidal


def test_resolve_lossy_mode_does_not_settle_for_a_clean_copy_first():
    clean = FakeAdapter(
        "jiosaavn", priority=25, always_lossy=True,
        result=make_result("jiosaavn", lossless=False, explicit=False),
    )
    explicit = FakeAdapter(
        "netease", priority=26, always_lossy=True,
        result=make_result("netease", lossless=False, explicit=True),
    )
    result, adapter = resolve_with("mp3", True, make_track(target_explicit=True), clean, explicit)
    assert adapter is explicit


def test_resolve_lossy_mode_falls_back_to_the_clean_copy():
    clean = FakeAdapter(
        "jiosaavn", priority=25, always_lossy=True,
        result=make_result("jiosaavn", lossless=False, explicit=False),
    )
    result, adapter = resolve_with("mp3", True, make_track(target_explicit=True), clean)
    assert adapter is clean


def test_resolve_lossy_mode_unchanged_without_explicit_target():
    clean = FakeAdapter(
        "jiosaavn", priority=25, always_lossy=True,
        result=make_result("jiosaavn", lossless=False, explicit=False),
    )
    explicit = FakeAdapter(
        "netease", priority=26, always_lossy=True,
        result=make_result("netease", lossless=False, explicit=True),
    )
    result, adapter = resolve_with("mp3", True, make_track(target_explicit=None), clean, explicit)
    assert adapter is clean
