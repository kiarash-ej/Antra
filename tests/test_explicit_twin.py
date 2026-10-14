"""
The twin swap (design section 2), offline.

Fixtures in tests/fixtures/twin are trimmed live responses captured on
2026-10-14 through SpotifyPartnerClient: assistedCurationSearch for "Kendrick
Lamar HUMBLE. clean" (bare URIs only), getTrack and decorateContextTracks for
its results, and the two editions of DAMN. (clean 0bLX..., explicit 4eLP...)
from getAlbum, written in the get_album_summary shape. In the explicit edition
every song is rated explicit except "LOVE. FEAT. ZACARI.", which is a real
example of an edition song with no explicit version. artist_releases_kendrick
is built by hand in the list_artist_releases shape from those real ids.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from antra.core import explicit_twin as twin_mod
from antra.core.explicit_twin import (
    TwinFinder,
    apply_explicit_targets,
    artist_key,
    is_explicit_twin,
    map_edition,
    matching_editions,
    search_candidates,
    track_from_get_track,
    tracks_from_decorate,
)
from antra.core.models import TrackMetadata

FIXTURES = Path(__file__).parent / "fixtures" / "twin"

CLEAN_HUMBLE = "131OLY5J8XyfGuSjXRiTRM"
EXPLICIT_HUMBLE = "7KXjTSCq5nL1LoYtL7XAwS"
CLEAN_DAMN = "0bLXUfNT34mna9aXq8ex68"
EXPLICIT_DAMN = "4eLPsYPBmXABThSJ821sqY"
KENDRICK = "2YZyLoL8N0Wb9xBt1NhZWg"
EMPTY_SEARCH = {"data": {"searchV2": {"tracksV2": {"items": []}}}}


def load(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _fresh_caches():
    # The caches last for the process by design; tests must not see each other's.
    TwinFinder.clear_caches()
    yield
    TwinFinder.clear_caches()


class FakeClient:
    """Stands in for SpotifyPartnerClient and records every request."""

    def __init__(self, searches=None, nodes=None, albums=None, releases=None, fail=()):
        self.searches = searches or {}
        self.nodes = nodes or {}           # uri -> decorateContextTracks node
        self.albums = albums or {}
        self.releases = releases or {}
        self.fail = set(fail)
        self.calls = []

    def _query(self, operation, variables):
        self.calls.append(operation)
        if operation in self.fail:
            raise RuntimeError("429 Too Many Requests")
        if operation == "assistedCurationSearch":
            return copy.deepcopy(self.searches.get(variables["term"], EMPTY_SEARCH))
        if operation == "decorateContextTracks":
            return {"data": {"tracks": [copy.deepcopy(self.nodes[u]) for u in variables["uris"] if u in self.nodes]}}
        if operation == "getTrack":
            track_id = variables["uri"].rsplit(":", 1)[-1]
            path = FIXTURES / f"gettrack_{track_id}.json"
            return load(path.name) if path.exists() else {"data": {"trackUnion": {"__typename": "NotFound"}}}
        raise AssertionError(f"unexpected operation {operation}")

    def get_album_summary(self, album_id):
        self.calls.append("get_album_summary")
        if "get_album_summary" in self.fail:
            raise RuntimeError("boom")
        return copy.deepcopy(self.albums.get(album_id))

    def list_artist_releases(self, artist_id):
        self.calls.append("list_artist_releases")
        return copy.deepcopy(self.releases.get(artist_id, []))


def song(title="HUMBLE.", artists=("Kendrick Lamar",), spotify_id=CLEAN_HUMBLE,
         explicit=False, duration_ms=177000, **kw):
    kw.setdefault("album", "DAMN.")
    kw.setdefault("request_kind", "track")
    return TrackMetadata(title=title, artists=list(artists), spotify_id=spotify_id,
                         is_explicit=explicit, duration_ms=duration_ms, **kw)


def album_batch(summary, request_kind="album"):
    """TrackMetadata as an album fetch builds it, from a get_album_summary dict."""
    return [
        TrackMetadata(
            title=t["name"], artists=list(t["artists"]), album=summary["name"],
            album_id=summary["id"], spotify_id=t["id"], is_explicit=t["explicit"],
            duration_ms=t["duration_ms"], disc_number=t["disc_number"],
            track_number=t["track_number"], request_kind=request_kind,
            isrc=f"CLEAN{t['track_number']:02d}", artwork_url="art://clean",
            spotify_url=f"https://open.spotify.com/track/{t['id']}",
        )
        for t in summary["tracks"]
    ]


def humble_client():
    """Search and hydration for the clean HUMBLE., all from captured responses."""
    decorate = load("decorate_humble.json")["data"]["tracks"]
    nodes = {n["uri"]: n for n in decorate}
    # The two lower-ranked results were not hydrated live; rated clean here.
    for track_id in ("6g6W3pMAFbp2FS1QCijkpM", "5KWwD9LzZa6ruNYGNqAqoN"):
        nodes[f"spotify:track:{track_id}"] = {
            "uri": f"spotify:track:{track_id}", "name": "Something Else",
            "contentRating": {"label": "NONE"}, "duration": {"totalMilliseconds": 200000},
            "artists": {"items": [{"profile": {"name": "Kendrick Lamar"}}]},
        }
    return FakeClient(searches={"Kendrick Lamar HUMBLE.": load("search_humble.json")}, nodes=nodes)


ON = SimpleNamespace(prefer_explicit=True, spotify_sp_dc="")


def run(tracks, client, workers=1):
    return apply_explicit_targets(tracks, ON, client=client, workers=workers)


# ── Twin rules ───────────────────────────────────────────────────────────────

def test_parses_real_get_track_responses():
    explicit = track_from_get_track(load(f"gettrack_{EXPLICIT_HUMBLE}.json"))
    clean = track_from_get_track(load(f"gettrack_{CLEAN_HUMBLE}.json"))
    assert explicit == {
        "id": EXPLICIT_HUMBLE, "name": "HUMBLE.", "artists": ["Kendrick Lamar"],
        "duration_ms": 177000, "isrc": None, "explicit": True,
        "album_id": EXPLICIT_DAMN, "album_name": "DAMN.",
    }
    assert clean["explicit"] is False and clean["album_id"] == CLEAN_DAMN


def test_search_gives_bare_uris_in_rank_order():
    candidates = search_candidates(load("search_humble.json"))
    assert [c["id"] for c in candidates] == [
        EXPLICIT_HUMBLE, CLEAN_HUMBLE, "3UHm2lhVeuywLXRAXEdUYW",
        "6g6W3pMAFbp2FS1QCijkpM", "5KWwD9LzZa6ruNYGNqAqoN",
    ]
    assert all(c["explicit"] is None and not c["name"] for c in candidates)


def test_decorate_completes_candidates_in_one_response():
    tracks = {t["id"]: t for t in tracks_from_decorate(load("decorate_humble.json"))}
    assert tracks[EXPLICIT_HUMBLE]["explicit"] is True
    assert tracks[CLEAN_HUMBLE]["explicit"] is False
    assert tracks["3UHm2lhVeuywLXRAXEdUYW"]["album_name"] == "DAMN. COLLECTORS EDITION."


def test_accepts_the_explicit_twin_of_a_clean_song():
    candidate = track_from_get_track(load(f"gettrack_{EXPLICIT_HUMBLE}.json"))
    assert is_explicit_twin(song(), candidate)


def test_rejects_a_candidate_that_is_not_explicit():
    candidate = track_from_get_track(load(f"gettrack_{CLEAN_HUMBLE}.json"))
    assert not is_explicit_twin(song(spotify_id="someotherid0000000000"), candidate)


def test_rejects_the_song_itself():
    candidate = track_from_get_track(load(f"gettrack_{EXPLICIT_HUMBLE}.json"))
    assert not is_explicit_twin(song(spotify_id=EXPLICIT_HUMBLE), candidate)


def test_rejects_a_different_primary_artist():
    candidate = track_from_get_track(load(f"gettrack_{EXPLICIT_HUMBLE}.json"))
    assert not is_explicit_twin(song(artists=("Future",)), candidate)
    # Same people, different lead: a different single.
    candidate["artists"] = ["Rihanna", "Kendrick Lamar"]
    assert not is_explicit_twin(song(artists=("Kendrick Lamar", "Rihanna")), candidate)


def test_artist_match_ignores_case_and_accents():
    assert artist_key("Beyoncé") == artist_key("BEYONCE")
    candidate = {"id": "x", "name": "Song", "artists": ["BEYONCE"], "duration_ms": 200000, "explicit": True}
    assert is_explicit_twin(song(title="Song", artists=("Beyoncé",), duration_ms=200000), candidate)


@pytest.mark.parametrize("clean_title", [
    "Mask Off - Clean", "Mask Off (Clean Version)", "Mask Off [Radio Edit]", "Mask Off - Edited",
])
def test_clean_markers_are_stripped_before_comparing_titles(clean_title):
    candidate = {"id": "x", "name": "Mask Off", "artists": ["Future"], "duration_ms": 204600, "explicit": True}
    assert is_explicit_twin(song(title=clean_title, artists=("Future",), duration_ms=204600), candidate)


def test_rejects_another_recording_of_the_song():
    candidate = {"id": "x", "name": "Mask Off (Live)", "artists": ["Future"], "duration_ms": 204600, "explicit": True}
    assert not is_explicit_twin(song(title="Mask Off", artists=("Future",), duration_ms=204600), candidate)
    candidate["name"] = "Mask Off (Remix)"
    assert not is_explicit_twin(song(title="Mask Off", artists=("Future",), duration_ms=204600), candidate)


@pytest.mark.parametrize("delta, accepted", [(0, True), (2999, True), (3000, True), (-3000, True),
                                             (3001, False), (-3001, False)])
def test_duration_window_is_three_seconds(delta, accepted):
    candidate = {"id": "x", "name": "HUMBLE.", "artists": ["Kendrick Lamar"],
                 "duration_ms": 177000 + delta, "explicit": True}
    assert is_explicit_twin(song(), candidate) is accepted


def test_unknown_duration_cannot_confirm_a_twin():
    candidate = {"id": "x", "name": "HUMBLE.", "artists": ["Kendrick Lamar"], "duration_ms": None, "explicit": True}
    assert not is_explicit_twin(song(), candidate)
    candidate["duration_ms"] = 177000
    assert not is_explicit_twin(song(duration_ms=None), candidate)


# ── Album edition rules ──────────────────────────────────────────────────────

def test_matching_editions_wants_same_name_type_and_track_count():
    clean = load("album_summary_damn_clean.json")
    found = matching_editions(clean, load("artist_releases_kendrick.json"))
    # Not itself, not the COLLECTORS EDITION. (other name), not GNX.
    assert [r["id"] for r in found] == [EXPLICIT_DAMN]

    releases = load("artist_releases_kendrick.json")
    for r in releases:
        if r["id"] == EXPLICIT_DAMN:
            r["track_count"] = 15
    assert matching_editions(clean, releases) == []
    for r in releases:
        if r["id"] == EXPLICIT_DAMN:
            r["track_count"], r["type"] = 14, "single"
    assert matching_editions(clean, releases) == []


def test_map_edition_pairs_by_position_confirmed_by_title():
    batch = album_batch(load("album_summary_damn_clean.json"))
    batch[13].title = "OUTRO."               # numbered like DUCKWORTH., named otherwise
    batch[12].track_number = 30              # no song at that position
    mapped = map_edition(batch, load("album_summary_damn_explicit.json"))
    assert sorted(mapped) == list(range(12))
    assert mapped[7]["id"] == EXPLICIT_HUMBLE


# ── Album swap ───────────────────────────────────────────────────────────────

def damn_client(**kw):
    return FakeClient(
        albums={CLEAN_DAMN: load("album_summary_damn_clean.json"),
                EXPLICIT_DAMN: load("album_summary_damn_explicit.json")},
        releases={KENDRICK: load("artist_releases_kendrick.json")},
        **kw,
    )


def test_clean_album_is_swapped_for_its_explicit_edition():
    client = damn_client()
    batch = album_batch(load("album_summary_damn_clean.json"))
    batch[13].title = "OUTRO."               # unmappable: keeps its original
    run(batch, client)

    humble = batch[7]
    assert (humble.spotify_id, humble.is_explicit, humble.target_explicit) == (EXPLICIT_HUMBLE, True, True)
    assert humble.isrc is None               # the clean ISRC would fetch the clean recording
    assert humble.spotify_url == f"https://open.spotify.com/track/{EXPLICIT_HUMBLE}"
    # Album name, numbering and artwork stay so the file lands where it would
    # have; album_id follows the twin, since source memory keys on it.
    assert (humble.album, humble.album_id, humble.track_number, humble.artwork_url) == (
        "DAMN.", EXPLICIT_DAMN, 8, "art://clean")

    love = batch[9]                          # rated clean in the explicit edition too
    assert (love.spotify_id, love.target_explicit, love.isrc) == ("6PGoSes0D9eUDeeAafB2As", False, "CLEAN10")

    outro = batch[13]
    assert outro.spotify_id == "2bCJKjeHVf8WoE1lGMX9cO" and outro.isrc == "CLEAN14"
    assert outro.album_id == CLEAN_DAMN
    assert outro.target_explicit is False    # searched on its own; Spotify found no twin
    # getAlbum's own edition link found the explicit edition: no artist page needed.
    assert client.calls == ["get_album_summary", "get_album_summary", "assistedCurationSearch"]


def test_artist_releases_are_the_fallback_when_getalbum_links_no_edition():
    client = damn_client()
    client.albums[CLEAN_DAMN]["edition_ids"] = []
    batch = album_batch(load("album_summary_damn_clean.json"))
    run(batch, client)
    assert all(t.spotify_id != CLEAN_HUMBLE for t in batch)
    assert batch[7].spotify_id == EXPLICIT_HUMBLE
    assert client.calls == ["get_album_summary", "list_artist_releases", "get_album_summary"]


def test_edition_with_another_track_count_is_not_used():
    client = damn_client()
    client.albums[EXPLICIT_DAMN]["track_count"] = 15
    batch = album_batch(load("album_summary_damn_clean.json"))[:1]   # BLOOD. only, to keep searches few
    batch[0].request_kind = "album"
    run(batch, client)
    assert batch[0].spotify_id == "5NMTBJ4AR3TMSVjGQX0Wxk"


def test_explicit_edition_settles_its_clean_songs_without_requests():
    client = damn_client()
    batch = album_batch(load("album_summary_damn_explicit.json"))
    run(batch, client)
    assert client.calls == []
    assert batch[9].target_explicit is False             # LOVE. has no explicit version
    assert all(t.target_explicit is True for i, t in enumerate(batch) if i != 9)
    assert [t.spotify_id for t in batch] == [t["id"] for t in load("album_summary_damn_explicit.json")["tracks"]]


def test_album_whose_summary_has_explicit_songs_is_not_searched():
    # The batch's flags can lag Spotify's (a partial sync); the summary decides.
    client = damn_client()
    batch = album_batch(load("album_summary_damn_explicit.json"))
    for t in batch:
        t.is_explicit, t.album_id = False, EXPLICIT_DAMN
    run(batch, client)
    assert client.calls == ["get_album_summary"]
    assert all(t.target_explicit is False for t in batch)


def test_saved_album_without_request_kind_takes_the_album_route():
    client = damn_client()
    batch = album_batch(load("album_summary_damn_clean.json"), request_kind=None)
    run(batch, client)
    assert batch[7].spotify_id == EXPLICIT_HUMBLE
    assert "assistedCurationSearch" not in client.calls


# ── Single-song twin ─────────────────────────────────────────────────────────

def test_clean_song_is_swapped_for_its_twin_in_two_requests():
    client = humble_client()
    track = song(isrc="CLEANISRC", spotify_url=f"https://open.spotify.com/track/{CLEAN_HUMBLE}",
                 artwork_url="art://clean", album_id=CLEAN_DAMN, track_number=8)
    run([track], client)
    assert (track.spotify_id, track.is_explicit, track.target_explicit, track.isrc) == (
        EXPLICIT_HUMBLE, True, True, None)
    assert (track.title, track.album, track.album_id, track.track_number, track.artwork_url) == (
        "HUMBLE.", "DAMN.", EXPLICIT_DAMN, 8, "art://clean")
    assert client.calls == ["assistedCurationSearch", "decorateContextTracks"]


def test_get_track_covers_a_failed_batch_hydration():
    client = humble_client()
    client.fail.add("decorateContextTracks")
    track = song()
    run([track], client)
    assert track.spotify_id == EXPLICIT_HUMBLE
    # The top result was the twin, so no further getTrack calls.
    assert client.calls == ["assistedCurationSearch", "decorateContextTracks", "getTrack"]


def test_song_without_twin_is_confirmed_not_explicit():
    client = humble_client()
    track = song(title="LOVE. FEAT. ZACARI.", artists=("Kendrick Lamar", "Zacari"),
                 spotify_id="5CRHxhQOzuj5iPASXodlmm", duration_ms=213400)
    client.searches["Kendrick Lamar LOVE. FEAT. ZACARI."] = load("search_humble.json")
    run([track], client)
    assert track.spotify_id == "5CRHxhQOzuj5iPASXodlmm"
    assert track.target_explicit is False


def test_cache_answers_a_second_batch_without_requests():
    client = humble_client()
    run([song()], client)
    no_twin = song(title="Nothing", spotify_id="nothing000000000000000")
    run([no_twin], client)
    calls = list(client.calls)

    again, again_none = song(), song(title="Nothing", spotify_id="nothing000000000000000")
    run([again, again_none], client)
    assert client.calls == calls
    assert again.spotify_id == EXPLICIT_HUMBLE and again.target_explicit is True
    assert again_none.target_explicit is False


def test_duplicate_songs_in_a_batch_share_one_lookup():
    client = humble_client()
    first, second = song(), song()
    run([first, second], client, workers=4)
    assert client.calls.count("assistedCurationSearch") == 1
    assert first.spotify_id == second.spotify_id == EXPLICIT_HUMBLE


# ── Failure and the breaker ──────────────────────────────────────────────────

def test_failed_search_leaves_the_song_unknown():
    client = humble_client()
    client.fail.add("assistedCurationSearch")
    track = song(isrc="CLEANISRC")
    run([track], client)
    assert (track.spotify_id, track.isrc, track.target_explicit) == (CLEAN_HUMBLE, "CLEANISRC", None)


def test_unreadable_candidates_are_unknown_not_no_twin():
    client = humble_client()
    client.fail.update({"decorateContextTracks", "getTrack"})
    track = song()
    run([track], client)
    assert track.spotify_id == CLEAN_HUMBLE
    assert track.target_explicit is None


def test_failed_lookups_are_not_cached():
    client = humble_client()
    client.fail.add("assistedCurationSearch")
    run([song()], client)
    client.fail.clear()
    track = song()
    run([track], client)
    assert track.spotify_id == EXPLICIT_HUMBLE


def test_breaker_stops_lookups_after_five_failures():
    client = FakeClient(fail={"assistedCurationSearch"})
    batch = [song(title=f"Song {i}", spotify_id=f"clean{i:017d}") for i in range(9)]
    run(batch, client)
    assert client.calls.count("assistedCurationSearch") == 5
    assert all(t.target_explicit is None and t.spotify_id.startswith("clean") for t in batch)


def test_breaker_holds_with_parallel_lookups():
    client = FakeClient(fail={"assistedCurationSearch"})
    batch = [song(title=f"Song {i}", spotify_id=f"clean{i:017d}") for i in range(30)]
    run(batch, client, workers=4)
    # At most the lookups already in flight when the fifth failure lands.
    assert client.calls.count("assistedCurationSearch") <= 5 + 3
    assert all(t.target_explicit is None for t in batch)


def test_a_success_resets_the_breaker():
    finder = TwinFinder(FakeClient())
    for _ in range(4):
        finder._record(False)
    finder._record(True)
    for _ in range(4):
        finder._record(False)
    assert not finder.tripped
    finder._record(False)
    assert finder.tripped


def test_failed_album_lookup_counts_and_falls_back_to_song_search():
    client = humble_client()
    client.fail.add("get_album_summary")
    track = song(request_kind="album", album_id=CLEAN_DAMN)
    other = song(title="DNA.", spotify_id="1C1OBpwB6tRViTiWE2ZgNR", duration_ms=185946,
                 request_kind="album", album_id=CLEAN_DAMN)
    run([track, other], client)
    assert track.spotify_id == EXPLICIT_HUMBLE
    assert other.target_explicit is False


def test_never_raises_into_the_download_flow():
    class Broken:
        def _query(self, *a, **k):
            raise TypeError("schema changed")

        def get_album_summary(self, *a, **k):
            raise KeyError("albumUnion")

        def list_artist_releases(self, *a, **k):
            raise ValueError("nope")

    batch = album_batch(load("album_summary_damn_clean.json")) + [song(spotify_id="x" * 22)]
    run(batch, Broken())
    assert all(t.target_explicit is None for t in batch)


def test_unexpected_error_still_leaves_valid_targets(monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(twin_mod, "_swap_batch", explode)
    batch = [song(explicit=True), song(), song(explicit=None)]
    run(batch, FakeClient())
    assert [t.target_explicit for t in batch] == [True, None, None]


# ── Without lookups ──────────────────────────────────────────────────────────

def test_off_rule_when_prefer_explicit_is_off():
    client = humble_client()
    batch = [song(explicit=True), song(), song(explicit=None)]
    apply_explicit_targets(batch, SimpleNamespace(prefer_explicit=False, spotify_sp_dc="cookie"), client=client)
    assert client.calls == []
    assert [t.target_explicit for t in batch] == [True, None, None]
    assert batch[1].spotify_id == CLEAN_HUMBLE


def test_off_rule_without_a_signed_in_client(monkeypatch):
    import antra.core.spotify_partner as partner

    def no_client(*a, **k):
        raise AssertionError("no partner client without sp_dc")

    monkeypatch.setattr(partner, "SpotifyPartnerClient", no_client)
    batch = [song(explicit=True), song()]
    apply_explicit_targets(batch, SimpleNamespace(prefer_explicit=True, spotify_sp_dc=""))
    assert [t.target_explicit for t in batch] == [True, None]


def test_songs_without_a_flag_are_not_searched():
    client = humble_client()
    track = song(explicit=None)
    run([track], client)
    assert client.calls == [] and track.target_explicit is None


def test_explicit_songs_want_explicit_without_requests():
    client = humble_client()
    track = song(spotify_id=EXPLICIT_HUMBLE, explicit=True)
    run([track], client)
    assert client.calls == [] and track.target_explicit is True


@pytest.mark.parametrize("service", ["apple", "amazon", "deezer", "tidal", "qobuz"])
def test_songs_from_other_catalogues_are_not_swapped(service):
    client = humble_client()
    clean = song(source_service=service)
    explicit = song(source_service=service, explicit=True)
    run([clean, explicit], client)
    assert client.calls == []
    assert (clean.spotify_id, clean.target_explicit, explicit.target_explicit) == (CLEAN_HUMBLE, None, True)


# ── Hook ─────────────────────────────────────────────────────────────────────

def test_download_tracks_sets_targets_before_the_engine_runs(monkeypatch):
    from antra.core import service as service_mod
    from antra.core.config import Config

    seen = {}

    class Engine:
        def download_playlist(self, tracks):
            seen["targets"] = [t.target_explicit for t in tracks]
            return []

    calls = []

    def spy(tracks, cfg, *a, **k):
        calls.append(cfg)
        return apply_explicit_targets(tracks, cfg, *a, **k)

    monkeypatch.setattr(service_mod.AntraService, "build_engine", lambda self, cfg, **kw: Engine())
    monkeypatch.setattr(service_mod, "apply_explicit_targets", spy)
    cfg = Config(prefer_explicit=False, spotify_sp_dc="")
    service_mod.AntraService(cfg).download_tracks([song(explicit=True), song()])
    assert len(calls) == 1 and calls[0].prefer_explicit is False
    assert seen["targets"] == [True, None]
