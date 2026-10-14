"""Explicit flags read from Spotify's partner GraphQL responses.

The fixtures in tests/fixtures/spotify/ are live responses captured on
2026-10-14 and trimmed to catalog fields. getAlbum carries `contentRating` but
no `externalIds`, so the ISRC case adds one to a copy of the response.
"""
import copy
import json
import threading
from pathlib import Path

import pytest

import antra.core.spotify as spotify_module
from antra.core.spotify import SpotifyClient
from antra.core.spotify_library import SpotifyLibraryClient
from antra.core.spotify_partner import SpotifyPartnerClient

FIXTURES = Path(__file__).parent / "fixtures" / "spotify"

KENDRICK = "2YZyLoL8N0Wb9xBt1NhZWg"
GNX_EXPLICIT = "0hvT3yIEysuuvkK73vgdcW"
GNX_CLEAN = "1Ss0ArMRr91m83mOgRBjSZ"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def album_tracks(payload: dict) -> list[dict]:
    return [item["track"] for item in payload["data"]["albumUnion"]["tracksV2"]["items"]]


# ── SpotifyClient._fetch_album_via_partner_api ──────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.ok = True
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def fetch_album(monkeypatch, payload: dict, album_id: str = GNX_EXPLICIT):
    client = SpotifyClient("", "")
    monkeypatch.setattr(client, "_fetch_full_album_data", lambda _id: None)
    monkeypatch.setattr(client, "_get_anonymous_access_token", lambda *a: "token")
    monkeypatch.setattr(spotify_module.requests, "post",
                        lambda url, **kwargs: _FakeResponse(payload))
    return client._fetch_album_via_partner_api(album_id)


def test_album_songs_take_the_explicit_flag(monkeypatch):
    tracks = fetch_album(monkeypatch, load("getAlbum_gnx_explicit.json"))

    assert len(tracks) == 12
    flags = {t.track_number: t.is_explicit for t in tracks}
    # "luther" is the one song Spotify rates clean on the explicit edition.
    assert flags[3] is False
    assert all(flags[n] is True for n in flags if n != 3)


def test_clean_edition_songs_are_flagged_clean(monkeypatch):
    tracks = fetch_album(monkeypatch, load("getAlbum_gnx_clean.json"), GNX_CLEAN)

    assert len(tracks) == 12
    assert all(t.is_explicit is False for t in tracks)


def test_missing_content_rating_is_unknown_not_clean(monkeypatch):
    payload = load("getAlbum_gnx_explicit.json")
    del album_tracks(payload)[0]["contentRating"]

    tracks = fetch_album(monkeypatch, payload)

    assert tracks[0].is_explicit is None
    assert tracks[1].is_explicit is True


def test_isrc_is_read_when_the_response_has_it(monkeypatch):
    payload = load("getAlbum_gnx_explicit.json")
    album_tracks(payload)[0]["externalIds"] = {"isrc": "QZTEST2400001"}

    tracks = fetch_album(monkeypatch, payload)

    assert tracks[0].isrc == "QZTEST2400001"
    assert tracks[1].isrc is None


def test_playlist_songs_take_the_explicit_flag(monkeypatch):
    payload = load("fetchPlaylist_todays_top_hits.json")
    del payload["data"]["playlistV2"]["content"]["items"][2]["itemV2"]["data"]["contentRating"]
    client = SpotifyClient("", "")
    monkeypatch.setattr(client, "_get_totp_access_token", lambda: "token")
    monkeypatch.setattr(spotify_module.requests, "post",
                        lambda url, **kwargs: _FakeResponse(payload))

    tracks = client._fetch_public_playlist_embed("37i9dQZF1DXcBWIGoYBM5M")

    assert [t.is_explicit for t in tracks] == [False, True, None]


def test_web_api_album_songs_take_the_explicit_flag():
    class FakeSpotipy:
        def album(self, album_id, market=None):
            return {"name": "GNX", "release_date": "2024-11-22", "images": [],
                    "total_tracks": 3, "artists": [{"name": "Kendrick Lamar"}]}

        def album_tracks(self, album_id, offset=0, limit=50, market=None):
            return {"items": [
                {"id": "a" * 22, "name": "one", "artists": [], "explicit": True},
                {"id": "b" * 22, "name": "two", "artists": [], "explicit": False},
                {"id": "c" * 22, "name": "three", "artists": []},
            ], "next": None}

    client = SpotifyClient("", "")
    client.sp = FakeSpotipy()

    tracks = client._fetch_album(GNX_EXPLICIT)

    assert [t.is_explicit for t in tracks] == [True, False, None]


# ── Liked Songs ─────────────────────────────────────────────────────────────


def _liked_item(track_id: str, rating):
    data = {
        "uri": f"spotify:track:{track_id}",
        "name": f"Song {track_id}",
        "artists": {"items": [{"profile": {"name": "Artist"}}]},
        "albumOfTrack": {"name": "Album", "uri": "spotify:album:a"},
        "duration": {"totalMilliseconds": 1000},
    }
    if rating is not None:
        data["contentRating"] = rating
    return {"track": {"_uri": data["uri"], "data": data}}


def test_liked_songs_use_the_shared_flag(monkeypatch):
    client = SpotifyLibraryClient("cookie")
    page = {"data": {"me": {"library": {"tracks": {"items": [
        _liked_item("e", {"label": "EXPLICIT"}),
        _liked_item("c", {"label": "NONE"}),
        _liked_item("u", None),
    ]}}}}}
    monkeypatch.setattr(client, "_graphql", lambda *a, **k: page)

    tracks = {t["id"]: t["explicit"] for t in client.get_liked_songs_tracks()}

    assert tracks == {"e": True, "c": False, "u": None}


# ── SpotifyPartnerClient ────────────────────────────────────────────────────


class FakeGraphQL:
    """Stands in for SpotifyPartnerClient._query and records every call."""

    def __init__(self, albums=None, overview=None, discography_all=None, fail_albums=()):
        self.albums = albums or {}
        self.overview = overview
        self.discography_all = discography_all
        self.fail_albums = set(fail_albums)
        self.calls: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def __call__(self, operation: str, variables: dict):
        with self._lock:
            self.calls.append((operation, variables))
        if operation == "queryArtistOverview":
            return self.overview
        if operation == "queryArtistDiscographyAll":
            # The live web player bundle does not register this operation, so
            # in practice the query is never sent and the overview is used.
            return self.discography_all
        if operation == "getAlbum":
            album_id = variables["uri"].rsplit(":", 1)[-1]
            if album_id in self.fail_albums:
                raise RuntimeError("429 Too Many Requests")
            return self.albums.get(album_id)
        raise AssertionError(f"unexpected operation {operation}")

    def album_calls(self) -> list[str]:
        return [v["uri"].rsplit(":", 1)[-1] for op, v in self.calls if op == "getAlbum"]


def partner(fake: FakeGraphQL) -> SpotifyPartnerClient:
    client = SpotifyPartnerClient("cookie")
    client._query = fake
    return client


def gnx_albums() -> dict:
    return {GNX_EXPLICIT: load("getAlbum_gnx_explicit.json"),
            GNX_CLEAN: load("getAlbum_gnx_clean.json")}


def test_album_summary_shape():
    summary = partner(FakeGraphQL(albums=gnx_albums())).get_album_summary(GNX_EXPLICIT)

    assert {k: summary[k] for k in ("id", "name", "type", "year", "track_count", "artist_ids")} == {
        "id": GNX_EXPLICIT,
        "name": "GNX",
        "type": "album",
        "year": 2024,
        "track_count": 12,
        "artist_ids": [KENDRICK],
    }
    assert len(summary["tracks"]) == 12
    luther = summary["tracks"][2]
    assert luther == {
        "id": "45J4avUb9Ni0bnETYaYFVJ",
        "name": "luther (with sza)",
        "artists": ["Kendrick Lamar", "SZA"],
        "duration_ms": luther["duration_ms"],
        "isrc": None,
        "explicit": False,
        "disc_number": 1,
        "track_number": 3,
    }
    assert isinstance(luther["duration_ms"], int) and luther["duration_ms"] > 0
    assert summary["tracks"][0]["explicit"] is True
    # getAlbum links the other edition of the same release.
    assert summary["edition_ids"] == [GNX_CLEAN]


def test_album_summary_accepts_urls_and_uris():
    client = partner(FakeGraphQL(albums=gnx_albums()))

    assert client.get_album_summary(f"https://open.spotify.com/album/{GNX_CLEAN}?si=x")["id"] == GNX_CLEAN
    assert client.get_album_summary(f"spotify:album:{GNX_CLEAN}")["id"] == GNX_CLEAN


def test_album_summary_pages_through_long_albums():
    full = load("getAlbum_gnx_explicit.json")
    items = full["data"]["albumUnion"]["tracksV2"]["items"]

    def page(offset):
        payload = copy.deepcopy(full)
        payload["data"]["albumUnion"]["tracksV2"]["items"] = items[:6] if offset == 0 else items[6:]
        return payload

    calls = []

    def query(operation, variables):
        calls.append(variables["offset"])
        return page(variables["offset"])

    client = SpotifyPartnerClient("cookie")
    client._query = query

    summary = client.get_album_summary(GNX_EXPLICIT)

    assert len(calls) == 2
    assert [t["track_number"] for t in summary["tracks"]] == list(range(1, 13))


@pytest.mark.parametrize("response", [
    None,
    {},
    {"data": {"albumUnion": {"__typename": "NotFound"}}},
    {"errors": [{"message": "PersistedQueryNotFound"}]},
])
def test_album_summary_is_none_for_a_bad_response(response):
    client = SpotifyPartnerClient("cookie")
    client._query = lambda op, v: response

    assert client.get_album_summary(GNX_EXPLICIT) is None


def test_album_summary_is_none_when_the_request_fails():
    client = partner(FakeGraphQL(fail_albums={GNX_EXPLICIT}))

    assert client.get_album_summary(GNX_EXPLICIT) is None


def test_album_summary_is_none_when_a_later_page_fails():
    # A partial track list could score an explicit album as clean.
    full = load("getAlbum_gnx_explicit.json")
    first = copy.deepcopy(full)
    first["data"]["albumUnion"]["tracksV2"]["items"] = first["data"]["albumUnion"]["tracksV2"]["items"][:6]

    def query(operation, variables):
        if variables["offset"] == 0:
            return first
        raise RuntimeError("timeout")

    client = SpotifyPartnerClient("cookie")
    client._query = query

    assert client.get_album_summary(GNX_EXPLICIT) is None


def test_album_summary_without_session_makes_no_request():
    client = SpotifyPartnerClient("")
    fake = FakeGraphQL(albums=gnx_albums())
    client._query = fake

    assert client.get_album_summary(GNX_EXPLICIT) is None
    assert fake.calls == []


def _unrated(payload: dict) -> dict:
    payload = copy.deepcopy(payload)
    for track in album_tracks(payload):
        track.pop("contentRating", None)
    return payload


@pytest.mark.parametrize("album_id, payload, score", [
    (GNX_EXPLICIT, "getAlbum_gnx_explicit.json", 2),
    (GNX_CLEAN, "getAlbum_gnx_clean.json", 0),
    (GNX_EXPLICIT, "unrated", 1),
])
def test_album_explicit_score(album_id, payload, score):
    body = _unrated(load("getAlbum_gnx_explicit.json")) if payload == "unrated" else load(payload)
    client = partner(FakeGraphQL(albums={album_id: body}))

    assert client.album_explicit_score(album_id) == score


def test_album_explicit_score_is_unknown_on_failure():
    client = partner(FakeGraphQL(fail_albums={GNX_EXPLICIT}))

    assert client.album_explicit_score(GNX_EXPLICIT) == 1


# ── discography ─────────────────────────────────────────────────────────────


def overview_with(album_ids_in_order=None, drop=()) -> dict:
    overview = load("queryArtistOverview_kendrick.json")
    albums = overview["data"]["artistUnion"]["discography"]["albums"]
    groups = [g for g in albums["items"] if g["releases"]["items"][0]["id"] not in drop]
    if album_ids_in_order:
        by_id = {g["releases"]["items"][0]["id"]: g for g in groups}
        groups = [by_id[i] for i in album_ids_in_order] + [
            g for g in groups if g["releases"]["items"][0]["id"] not in album_ids_in_order]
    albums["items"] = groups
    return overview


def test_list_artist_releases_returns_every_edition():
    fake = FakeGraphQL(overview=overview_with())

    releases = partner(fake).list_artist_releases(f"https://open.spotify.com/artist/{KENDRICK}")

    ids = [r["id"] for r in releases]
    assert GNX_EXPLICIT in ids and GNX_CLEAN in ids
    assert len(ids) == len(set(ids)) == 7
    assert set(releases[0]) == {"id", "name", "year", "track_count", "type", "url", "artwork_url"}
    assert fake.album_calls() == []


def test_list_artist_releases_prefers_the_paged_discography():
    groups = overview_with()["data"]["artistUnion"]["discography"]["albums"]["items"]
    paged = {"data": {"artistUnion": {"discography": {"all": {"items": groups,
                                                              "totalCount": len(groups)}}}}}
    fake = FakeGraphQL(discography_all=paged)

    releases = partner(fake).list_artist_releases(KENDRICK)

    assert {r["id"] for r in releases} >= {GNX_EXPLICIT, GNX_CLEAN}
    assert [op for op, _ in fake.calls] == ["queryArtistDiscographyAll"]


def test_list_artist_releases_never_raises():
    assert partner(FakeGraphQL(overview=None)).list_artist_releases(KENDRICK) == []
    assert partner(FakeGraphQL()).list_artist_releases("not an artist") == []


@pytest.mark.parametrize("order", [None, [GNX_CLEAN, GNX_EXPLICIT]])
def test_discography_keeps_the_explicit_edition(order):
    fake = FakeGraphQL(overview=overview_with(order), albums=gnx_albums())

    info = partner(fake).fetch_artist_discography_info(KENDRICK)

    assert set(info) == {"artist_id", "artist_name", "artwork_url", "albums"}
    assert info["artist_id"] == KENDRICK
    assert info["artist_name"] == "Kendrick Lamar"
    ids = [a["id"] for a in info["albums"]]
    assert GNX_EXPLICIT in ids and GNX_CLEAN not in ids
    gnx = next(a for a in info["albums"] if a["id"] == GNX_EXPLICIT)
    assert gnx["is_explicit"] is True
    # Releases with no duplicate are neither fetched nor labelled.
    assert all("is_explicit" not in a for a in info["albums"] if a["id"] != GNX_EXPLICIT)
    assert sorted(fake.album_calls()) == sorted([GNX_EXPLICIT, GNX_CLEAN])


def test_single_editions_cost_no_album_requests():
    fake = FakeGraphQL(overview=overview_with(drop={GNX_CLEAN}), albums=gnx_albums())

    info = partner(fake).fetch_artist_discography_info(KENDRICK)

    assert fake.album_calls() == []
    assert len(info["albums"]) == 6
    assert all("is_explicit" not in a for a in info["albums"])


def test_discography_labels_a_kept_clean_only_edition_false():
    clean = load("getAlbum_gnx_clean.json")
    fake = FakeGraphQL(overview=overview_with(), albums={GNX_EXPLICIT: clean, GNX_CLEAN: clean})

    info = partner(fake).fetch_artist_discography_info(KENDRICK)

    kept = [a for a in info["albums"] if a["id"] in (GNX_EXPLICIT, GNX_CLEAN)]
    assert len(kept) == 1 and kept[0]["is_explicit"] is False


def test_discography_survives_failed_edition_lookups():
    fake = FakeGraphQL(overview=overview_with(), fail_albums={GNX_EXPLICIT, GNX_CLEAN})

    info = partner(fake).fetch_artist_discography_info(KENDRICK)

    kept = [a for a in info["albums"] if a["id"] in (GNX_EXPLICIT, GNX_CLEAN)]
    assert len(kept) == 1 and "is_explicit" not in kept[0]
    assert len(info["albums"]) == 6


def test_discography_still_raises_when_the_artist_lookup_fails():
    with pytest.raises(RuntimeError):
        partner(FakeGraphQL(overview=None)).fetch_artist_discography_info(KENDRICK)
