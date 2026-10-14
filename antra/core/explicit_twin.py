"""
Twin swap: before resolving, replace clean Spotify songs with their explicit
versions ("twins") and record what each song should be downloaded as.

Runs once per download batch, from `AntraService.download_tracks`. With
`prefer_explicit` on and a signed-in Spotify partner client (`spotify_sp_dc`):

- an album with no explicit song is swapped for the artist's explicit edition
  of the same release, song by song (disc and track number, confirmed by
  title);
- any other song flagged clean is searched for an explicit twin with the same
  primary artist, the same title once clean markers are removed, and a
  duration within 3 seconds.

Every song leaves with `target_explicit`: True when it or its twin is
explicit, False when Spotify confirmed it has no explicit version, None when
that could not be told. With the setting off, or without a partner client, no
lookups are made and only Spotify's own flag is used.

The matching rules are pure functions; the network calls are confined to
`TwinFinder`. Nothing here raises into the download flow: a failed lookup
leaves the song as it was, with `target_explicit = None`.
"""
from __future__ import annotations

import logging
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Optional

from antra.core.explicit_common import (
    comparable_title,
    explicit_from_content_rating,
    strip_clean_markers,
)
from antra.utils.matching import version_mismatch

if TYPE_CHECKING:
    from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

TWIN_DURATION_WINDOW_MS = 3000
# Consecutive failed lookups before the rest of the batch is left alone. A
# rate-limited or signed-out client fails every call; without the breaker a
# large playlist would spend minutes timing out before its first download.
BREAKER_LIMIT = 5
# Same-name releases checked for explicit songs per album. Real catalogues have
# two editions, occasionally three; more than that is a compilation series.
MAX_EDITION_CANDIDATES = 3
# getTrack calls per song when the batch hydration (decorateContextTracks)
# fails. Search ranks by popularity, so an explicit twin sits near the top.
MAX_GETTRACK_FALLBACK = 3
# Twin searches run in parallel: at two requests per clean song, a 50-song
# playlist would otherwise stall for half a minute before its first download.
LOOKUP_WORKERS = 4

_FOUND, _NONE, _SELF, _FAILED = "found", "none", "self", "failed"
_MISS = object()


# ── Pure rules ───────────────────────────────────────────────────────────────

def off_rule_target(track: "TrackMetadata") -> Optional[bool]:
    """`target_explicit` without lookups: an explicit song wants explicit, nothing else is known."""
    return True if track.is_explicit is True else None


def is_spotify_song(track: "TrackMetadata") -> bool:
    """Songs the twin swap may touch.

    Apple, Amazon, TIDAL, Qobuz and Deezer links carry their own catalogue's
    flags, and a Spotify id on them comes from fuzzy enrichment, so swapping it
    could replace the song the user linked with a different one.
    """
    source = (track.source_service or "spotify").strip().lower()
    return bool(track.spotify_id) and source == "spotify"


def artist_key(name: Optional[str]) -> str:
    """Case- and accent-insensitive form of an artist name ("Beyoncé" == "BEYONCE")."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    bare = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(bare.casefold().split())


def durations_close(a_ms: Any, b_ms: Any, window_ms: int = TWIN_DURATION_WINDOW_MS) -> bool:
    """Both durations known and within the window. An unknown one cannot confirm a twin."""
    try:
        a, b = int(a_ms or 0), int(b_ms or 0)
    except (TypeError, ValueError):
        return False
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= window_ms


def is_explicit_twin(track: "TrackMetadata", candidate: Optional[dict]) -> bool:
    """True when `candidate` (a track dict) is the explicit version of `track`."""
    if not isinstance(candidate, dict) or candidate.get("explicit") is not True:
        return False
    if candidate.get("id") and candidate.get("id") == track.spotify_id:
        return False
    candidate_artists = candidate.get("artists") or []
    if not track.artists or not candidate_artists:
        return False
    if artist_key(track.artists[0]) != artist_key(candidate_artists[0]):
        return False
    title = track.title or ""
    candidate_title = candidate.get("name") or ""
    key = comparable_title(title)
    if not key or key != comparable_title(candidate_title):
        return False
    # comparable_title drops parentheticals, so without this "Song (Live)" or
    # "Song (Remix)" would pass for "Song" whenever the durations happen to agree.
    if version_mismatch(strip_clean_markers(title), strip_clean_markers(candidate_title)):
        return False
    return durations_close(track.duration_ms, candidate.get("duration_ms"))


def _spotify_id(uri_or_id: Any, kind: str = "track") -> str:
    value = str(uri_or_id or "")
    prefix = f"spotify:{kind}:"
    if value.startswith(prefix):
        return value[len(prefix):]
    return "" if ":" in value else value


def track_from_graphql(node: Any) -> Optional[dict]:
    """A partner-API track node to the track dict the rules compare.

    Handles the getTrack shape (`firstArtist` / `otherArtists`) and the
    decorateContextTracks and album shape (`artists`).
    """
    if not isinstance(node, dict):
        return None
    track_id = node.get("id") or _spotify_id(node.get("uri"))
    if not track_id:
        return None
    artists: list[str] = []
    for key in ("firstArtist", "otherArtists", "artists"):
        for item in ((node.get(key) or {}).get("items") or []):
            name = ((item or {}).get("profile") or {}).get("name")
            if name and name not in artists:
                artists.append(name)
    duration = (node.get("duration") or node.get("trackDuration") or {}).get("totalMilliseconds")
    external_ids = node.get("externalIds")
    album = node.get("albumOfTrack") or {}
    return {
        "id": track_id,
        "name": node.get("name") or "",
        "artists": artists,
        "duration_ms": int(duration) if isinstance(duration, (int, float)) else None,
        "isrc": (external_ids.get("isrc") if isinstance(external_ids, dict) else None) or None,
        "explicit": explicit_from_content_rating(node.get("contentRating")),
        "album_id": album.get("id") or _spotify_id(album.get("uri"), "album") or None,
        "album_name": album.get("name"),
    }


def is_complete(candidate: Optional[dict]) -> bool:
    """Enough of a track to judge it as a twin, explicit flag included."""
    return bool(
        isinstance(candidate, dict)
        and candidate.get("name")
        and candidate.get("artists")
        and candidate.get("duration_ms")
        and candidate.get("explicit") is not None
    )


def search_candidates(response: Any) -> list[dict]:
    """Tracks from an assistedCurationSearch response, in rank order.

    The operation answers with bare URIs at
    `data.searchV2.tracksV2.items[].item.data.uri`; whatever else a node
    carries is parsed too, so a richer response needs no hydration.
    """
    search = (((response or {}).get("data") or {}).get("searchV2") or {}) if isinstance(response, dict) else {}
    out: list[dict] = []
    seen: set[str] = set()
    for entry in ((search.get("tracksV2") or {}).get("items") or []):
        node = (((entry or {}).get("item") or {}).get("data")) or (entry or {}).get("data")
        candidate = track_from_graphql(node)
        if candidate and candidate["id"] not in seen:
            seen.add(candidate["id"])
            out.append(candidate)
    return out


def tracks_from_decorate(response: Any) -> list[dict]:
    """Tracks from a decorateContextTracks response (`data.tracks[]`)."""
    nodes = (((response or {}).get("data") or {}).get("tracks") or []) if isinstance(response, dict) else []
    return [t for t in (track_from_graphql(n) for n in nodes) if t]


def track_from_get_track(response: Any) -> Optional[dict]:
    """The track from a getTrack response (`data.trackUnion`)."""
    if not isinstance(response, dict):
        return None
    return track_from_graphql((response.get("data") or {}).get("trackUnion"))


def tracks_have_explicit(tracks: Any) -> bool:
    return any(isinstance(t, dict) and t.get("explicit") is True for t in (tracks or []))


def _track_count(album: dict) -> Optional[int]:
    count = album.get("track_count")
    if isinstance(count, int):
        return count
    tracks = album.get("tracks")
    return len(tracks) if isinstance(tracks, list) and tracks else None


def matching_editions(album: dict, releases: list[dict]) -> list[dict]:
    """Other releases that are editions of `album`: same normalised name, type and track count."""
    name = comparable_title(album.get("name") or "")
    kind = (album.get("type") or "").lower()
    count = _track_count(album)
    if not name or not kind or count is None:
        return []
    out: list[dict] = []
    seen = {album.get("id")}
    for release in releases or []:
        if not isinstance(release, dict):
            continue
        release_id = release.get("id")
        if not release_id or release_id in seen:
            continue
        if comparable_title(release.get("name") or "") != name:
            continue
        if (release.get("type") or "").lower() != kind or release.get("track_count") != count:
            continue
        seen.add(release_id)
        out.append(release)
    return out


def map_edition(tracks: list["TrackMetadata"], edition: dict) -> dict[int, dict]:
    """Index in `tracks` -> the edition's song at the same disc and track number.

    Each pair is confirmed by comparable title, so a song the editions number
    differently (a bonus track, a reordered interlude) stays unmapped instead
    of being swapped for a different song.
    """
    by_position: dict[tuple[int, int], dict] = {}
    for song in (edition or {}).get("tracks") or []:
        if not isinstance(song, dict) or not song.get("id") or not song.get("track_number"):
            continue
        by_position.setdefault((song.get("disc_number") or 1, song["track_number"]), song)
    mapped: dict[int, dict] = {}
    for index, track in enumerate(tracks):
        if not track.track_number:
            continue
        song = by_position.get((track.disc_number or 1, track.track_number))
        if not song:
            continue
        key = comparable_title(track.title or "")
        if key and key == comparable_title(song.get("name") or ""):
            mapped[index] = song
    return mapped


def apply_twin(track: "TrackMetadata", twin: dict) -> None:
    """Point `track` at `twin`, keeping album, numbering and artwork so the file
    lands where the user expects.

    The twin's ISRC is used when known, but getAlbum and getTrack send none, so
    usually the old ISRC is simply dropped: left in place, exact ISRC matching
    would fetch the clean recording again. Only an edition song Spotify rates
    clean keeps it, since that song has no explicit recording to miss.
    """
    explicit = twin.get("explicit")
    track.spotify_id = twin["id"]
    track.isrc = twin.get("isrc") or (track.isrc if explicit is False else None)
    track.is_explicit = explicit
    if twin.get("duration_ms"):
        track.duration_ms = int(twin["duration_ms"])
    if track.spotify_url:
        track.spotify_url = f"https://open.spotify.com/track/{twin['id']}"
    # The resolver's per-album source memory and the Amazon album fallback key
    # on album_id, so it follows the twin to the release that holds it. The
    # album name stays, which is what the folder layout uses.
    if twin.get("album_id"):
        track.album_id = twin["album_id"]
    track.target_explicit = explicit


def album_groups(songs: list["TrackMetadata"]) -> dict[str, list["TrackMetadata"]]:
    """Songs that came in as a whole album, by album id.

    A pasted album link marks its songs `request_kind == "album"`; a saved
    album picked from the library arrives as its own batch with no kind. Songs
    of one album inside a playlist are not grouped: playlist numbering is
    renumbered per album, so edition mapping would fail and waste the lookup.
    """
    groups: dict[str, list] = {}
    for track in songs:
        if track.album_id and (track.request_kind or "").lower() == "album":
            groups.setdefault(track.album_id, []).append(track)
    if not groups and songs and not any(t.request_kind for t in songs):
        album_ids = {t.album_id for t in songs}
        if len(album_ids) == 1 and None not in album_ids and len(songs) > 1:
            groups[songs[0].album_id] = list(songs)
    return groups


# ── Network side ─────────────────────────────────────────────────────────────

class TwinFinder:
    """Lookups through a `SpotifyPartnerClient`, with caches and a breaker.

    One instance per batch, so the breaker resets between batches; the caches
    are class-level and last for the process, including "no twin" answers.
    Failed lookups are not cached, so a later batch tries again.
    """

    _twins: dict[str, Optional[dict]] = {}      # clean song id -> twin, or None for "no twin"
    _editions: dict[str, tuple] = {}            # album id -> (status, explicit edition or None)
    _hydrated: dict[str, dict] = {}             # track id -> complete track dict

    def __init__(self, client: Any, breaker_limit: int = BREAKER_LIMIT):
        self.client = client
        self.breaker_limit = breaker_limit
        self._failures = 0
        self._lock = threading.Lock()

    @classmethod
    def clear_caches(cls) -> None:
        cls._twins.clear()
        cls._editions.clear()
        cls._hydrated.clear()

    @property
    def tripped(self) -> bool:
        return self._failures >= self.breaker_limit

    def _record(self, ok: bool) -> None:
        with self._lock:
            if ok:
                self._failures = 0
                return
            self._failures += 1
            if self._failures == self.breaker_limit:
                logger.debug(
                    "[ExplicitTwin] %d lookups failed in a row; no more lookups this batch",
                    self._failures,
                )

    def remember(self, songs: list["TrackMetadata"]) -> None:
        """Seed the hydration cache with the batch's own songs.

        A search for one song of an album often returns its neighbours, which
        the batch already describes; this saves hydrating them. Called before
        any swap, so it records what Spotify said about the originals.
        """
        for track in songs:
            candidate = {
                "id": track.spotify_id,
                "name": track.title,
                "artists": list(track.artists or []),
                "duration_ms": track.duration_ms,
                "isrc": track.isrc,
                "explicit": track.is_explicit,
                "album_id": track.album_id,
                "album_name": track.album,
            }
            if track.spotify_id and is_complete(candidate):
                self._hydrated.setdefault(track.spotify_id, candidate)

    # album edition

    def album_edition(self, album_id: str) -> tuple[str, Optional[dict]]:
        """("self", None) when the album has explicit songs itself; ("found",
        edition) for its explicit edition; ("none", None) when there is none;
        ("failed", None) when that could not be told."""
        cached = self._editions.get(album_id, _MISS)
        if cached is not _MISS:
            return cached
        if self.tripped:
            return _FAILED, None
        try:
            result = self._lookup_edition(album_id)
        except Exception as e:
            logger.debug(f"[ExplicitTwin] edition lookup failed for album {album_id}: {e}")
            result = (_FAILED, None)
        self._record(result[0] != _FAILED)
        if result[0] != _FAILED:
            self._editions[album_id] = result
        return result

    def _lookup_edition(self, album_id: str) -> tuple[str, Optional[dict]]:
        album = self.client.get_album_summary(album_id)
        if not album:
            return _FAILED, None
        if tracks_have_explicit(album.get("tracks")):
            return _SELF, None
        # getAlbum links an album to its other editions, which is how Spotify
        # ties a clean album to its explicit one. It is the first route because
        # the artist page can miss an edition entirely (the clean GNX is not on
        # it), and without a discography hash it only lists the first page.
        edition_ids = [i for i in (album.get("edition_ids") or []) if i and i != album_id]
        if edition_ids:
            same_count = True
        else:
            artist_ids = album.get("artist_ids") or []
            if not artist_ids:
                return _NONE, None
            releases = self.client.list_artist_releases(artist_ids[0])
            if not releases:
                # The artist has at least this album, so an empty list is a
                # failed lookup, not proof that no explicit edition exists.
                return _FAILED, None
            edition_ids = [r["id"] for r in matching_editions(album, releases)]
            same_count = False  # matching_editions already compared track counts
        failed = False
        for edition_id in edition_ids[:MAX_EDITION_CANDIDATES]:
            try:
                edition = self.client.get_album_summary(edition_id)
            except Exception as e:
                logger.debug(f"[ExplicitTwin] edition {edition_id} unavailable: {e}")
                edition = None
            if not edition:
                failed = True
                continue
            if same_count and _track_count(edition) != _track_count(album):
                continue
            if tracks_have_explicit(edition.get("tracks")):
                logger.debug(f"[ExplicitTwin] album {album_id} has explicit edition {edition_id}")
                return _FOUND, edition
        return (_FAILED, None) if failed else (_NONE, None)

    # single song

    def find_twin(self, track: "TrackMetadata") -> tuple[str, Optional[dict]]:
        """("found", twin), ("none", None) when Spotify has no explicit twin,
        or ("failed", None)."""
        cached = self._twins.get(track.spotify_id, _MISS)
        if cached is not _MISS:
            return (_FOUND, cached) if cached else (_NONE, None)
        if self.tripped:
            return _FAILED, None
        try:
            status, twin = self._search_twin(track)
        except Exception as e:
            logger.debug(f"[ExplicitTwin] twin search failed for '{track.title}': {e}")
            status, twin = _FAILED, None
        self._record(status != _FAILED)
        if status != _FAILED:
            self._twins[track.spotify_id] = twin
        return status, twin

    def _search_twin(self, track: "TrackMetadata") -> tuple[str, Optional[dict]]:
        term = f"{track.primary_artist} {strip_clean_markers(track.title or '')}".strip()
        response = self.client._query("assistedCurationSearch", {"term": term})
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            return _FAILED, None
        candidates = [c for c in search_candidates(response) if c["id"] != track.spotify_id]
        # Search returns bare URIs. decorateContextTracks completes them all in
        # one request; getTrack, one request per song, only covers what that
        # call left out, and only when no twin turned up among the rest.
        unread: list[str] = []
        for candidate in self._hydrate(candidates):
            if isinstance(candidate, str):
                unread.append(candidate)
            elif is_explicit_twin(track, candidate):
                return self._found(track, candidate)
        for track_id in unread[:MAX_GETTRACK_FALLBACK]:
            candidate = self._get_track(track_id)
            if candidate is None:
                continue
            self._hydrated[track_id] = candidate
            unread.remove(track_id)
            if is_explicit_twin(track, candidate):
                return self._found(track, candidate)
        # A candidate that could not be read might have been the twin, so its
        # absence is not proof that none exists.
        return (_FAILED, None) if unread else (_NONE, None)

    @staticmethod
    def _found(track: "TrackMetadata", twin: dict) -> tuple[str, dict]:
        logger.debug(
            f"[ExplicitTwin] '{track.title}' ({track.spotify_id}) -> explicit twin {twin['id']}"
        )
        return _FOUND, twin

    def _hydrate(self, candidates: list[dict]) -> list:
        """Each candidate completed, in order, or its bare id where that failed."""
        missing = [
            c["id"] for c in candidates
            if not is_complete(c) and c["id"] not in self._hydrated
        ]
        if missing:
            self._hydrated.update(self._decorate(missing))
        return [
            c if is_complete(c) else self._hydrated.get(c["id"], c["id"])
            for c in candidates
        ]

    def _decorate(self, track_ids: list[str]) -> dict[str, dict]:
        try:
            response = self.client._query(
                "decorateContextTracks",
                {"uris": [f"spotify:track:{i}" for i in track_ids]},
            )
        except Exception as e:
            logger.debug(f"[ExplicitTwin] decorateContextTracks failed: {e}")
            return {}
        return {t["id"]: t for t in tracks_from_decorate(response) if is_complete(t)}

    def _get_track(self, track_id: str) -> Optional[dict]:
        try:
            track = track_from_get_track(
                self.client._query("getTrack", {"uri": f"spotify:track:{track_id}"})
            )
        except Exception as e:
            logger.debug(f"[ExplicitTwin] getTrack failed for {track_id}: {e}")
            return None
        return track if is_complete(track) else None


# ── Batch entry point ────────────────────────────────────────────────────────

def _partner_client(cfg: Any) -> Any:
    sp_dc = (getattr(cfg, "spotify_sp_dc", "") or "").strip()
    if not sp_dc:
        return None
    from antra.core.spotify_partner import SpotifyPartnerClient
    return SpotifyPartnerClient(sp_dc)


def apply_explicit_targets(
    tracks: list["TrackMetadata"],
    cfg: Any,
    client: Any = None,
    workers: int = LOOKUP_WORKERS,
) -> list["TrackMetadata"]:
    """Set `target_explicit` on every song, swapping clean Spotify songs for
    their explicit twins when `prefer_explicit` is on and a partner client is
    available. Mutates and returns `tracks`; never raises.

    `client` is for tests; normally one is built from `cfg.spotify_sp_dc`.
    """
    if not tracks:
        return tracks
    # Every song starts from the no-lookup answer, so whatever happens below a
    # song is never left without a valid target.
    for track in tracks:
        track.target_explicit = off_rule_target(track)
    try:
        if not getattr(cfg, "prefer_explicit", True):
            return tracks
        if client is None:
            client = _partner_client(cfg)
        if client is None:
            return tracks
        _swap_batch(tracks, TwinFinder(client), workers)
    except Exception as e:
        logger.debug(f"[ExplicitTwin] twin swap stopped: {e}")
    return tracks


def _swap_batch(tracks: list["TrackMetadata"], finder: TwinFinder, workers: int) -> None:
    songs = [t for t in tracks if is_spotify_song(t)]
    finder.remember(songs)
    settled: set[int] = set()

    for album_id, group in album_groups(songs).items():
        flags = {t.is_explicit for t in group}
        if False not in flags:
            continue  # explicit songs already want explicit; unknown ones stay unknown
        if True in flags:
            # This is the explicit edition, so its clean songs have no explicit version.
            status, edition = _SELF, None
        else:
            status, edition = finder.album_edition(album_id)
        if status == _SELF:
            for track in group:
                if track.is_explicit is False:
                    track.target_explicit = False
                    settled.add(id(track))
        elif status == _FOUND:
            for index, twin in map_edition(group, edition).items():
                apply_twin(group[index], {**twin, "album_id": edition.get("id")})
                settled.add(id(group[index]))

    by_id: dict[str, list] = {}
    for track in songs:
        if track.is_explicit is False and id(track) not in settled:
            by_id.setdefault(track.spotify_id, []).append(track)
    if not by_id:
        return

    def settle(song_id: str) -> str:
        same_song = by_id[song_id]
        status, twin = finder.find_twin(same_song[0])
        for track in same_song:
            if status == _FOUND:
                apply_twin(track, twin)
            elif status == _NONE:
                track.target_explicit = False
        return status

    if workers <= 1 or len(by_id) == 1:
        statuses = [settle(song_id) for song_id in by_id]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(by_id))) as pool:
            statuses = list(pool.map(settle, by_id))
    logger.debug(
        "[ExplicitTwin] %d clean songs: %d twins, %d without, %d unknown",
        len(statuses), statuses.count(_FOUND), statuses.count(_NONE), statuses.count(_FAILED),
    )
