"""Spotify artist search, discography and album lookups via the web player's partner GraphQL API.

Why this exists: `api.spotify.com` returns 429 for both anonymous TOTP tokens and
sp_dc-derived user tokens on throttled networks (the same condition the VPS
metadata proxy exists for), so `SpotifyClient.search_artists` and
`fetch_artist_discography_info` silently fall through to Apple Music. The result
was a "Spotify" option that returned Apple artists, Apple discographies and
Apple album URLs.

The partner API (`api-partner.spotify.com/pathfinder/v1/query`) is what the web
player itself uses. It is reachable where the public API is not, and it is
already the mechanism behind `spotify_library.py`, so the auth (TOTP token ->
clientId -> client token) is reused rather than reimplemented.

Persisted-query hashes are read from the live web player bundle and cached, with
hardcoded values as a fallback, so a rotation self-heals instead of breaking.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from antra.core.explicit_common import explicit_from_content_rating

logger = logging.getLogger(__name__)

_BUNDLE_INDEX = "https://open.spotify.com/"
_BUNDLE_RE = re.compile(r'src="(https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[^"]+\.js)"')
# Operations are registered as: new X.Y("<name>","query","<sha256>",null)
_OP_RE = re.compile(
    r'new [A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*\("([A-Za-z]\w*)","(?:query|mutation)","([0-9a-f]{64})"'
)

# Last-known-good hashes. Only used when the bundle cannot be read. The first
# two are what the live bundle registered on 2026-10-14 (the earlier values
# were placeholders, one of them 65 characters long). getAlbum's is the hash
# SpotifyClient sends for its anonymous album fetch; it answers here too.
_FALLBACK_HASHES = {
    "assistedCurationSearch": "f78953bf9207d73493c27284103f5aeb6e728876d5793851bf79bc706127ff70",
    "queryArtistOverview": "9f8134ef565e78621f1e1793555bd6633c5ac144ae0f89604ed3ae3f80b3c8e6",
    "getAlbum": "b9bfabef66ed756e5e13f68a942deb60bd4125ec1f1be8cc42769dc0259b4b10",
}
# getAlbum page size. SpotifyClient's anonymous album fetch uses the same.
_ALBUM_PAGE = 300
_HASH_CACHE_TTL = 12 * 60 * 60


class SpotifyPartnerClient:
    """Thin wrapper: reuses SpotifyLibraryClient for auth, adds hash discovery."""

    _hashes: dict = {}
    _hashes_at: float = 0.0

    def __init__(self, sp_dc: str):
        self.sp_dc = (sp_dc or "").strip()
        self._client = None

    # ── auth (delegated) ─────────────────────────────────────────────────────

    def _lib(self):
        if self._client is None:
            from antra.core.spotify_library import SpotifyLibraryClient
            self._client = SpotifyLibraryClient(self.sp_dc)
        return self._client

    # ── persisted-query hashes ───────────────────────────────────────────────

    @classmethod
    def _cache_path(cls) -> str:
        from antra.utils.config import get_config_dir
        d = os.path.join(get_config_dir(), "cache")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "spotify_op_hashes.json")

    @classmethod
    def operation_hashes(cls) -> dict:
        """Operation name -> sha256, scraped from the live bundle and cached.

        Spotify rotates these. Reading them at runtime means a rotation costs one
        refetch rather than a broken feature and a code change.
        """
        now = time.time()
        if cls._hashes and now - cls._hashes_at < _HASH_CACHE_TTL:
            return cls._hashes

        path = cls._cache_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
            if now - blob.get("timestamp", 0) < _HASH_CACHE_TTL and blob.get("hashes"):
                cls._hashes, cls._hashes_at = blob["hashes"], now
                return cls._hashes
        except Exception:
            pass

        hashes = {}
        try:
            from curl_cffi import requests as cr
            index = cr.get(_BUNDLE_INDEX, impersonate="chrome124", timeout=25).text
            m = _BUNDLE_RE.search(index)
            if m:
                js = cr.get(m.group(1), impersonate="chrome124", timeout=90).text
                for name, h in _OP_RE.findall(js):
                    hashes.setdefault(name, h)
        except Exception as e:
            logger.debug(f"[SpotifyPartner] hash discovery failed: {e}")

        if hashes:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"timestamp": now, "hashes": hashes}, f)
            except Exception:
                pass
            cls._hashes, cls._hashes_at = hashes, now
            return hashes

        cls._hashes, cls._hashes_at = dict(_FALLBACK_HASHES), now
        return cls._hashes

    def _hash_for(self, operation: str) -> Optional[str]:
        return self.operation_hashes().get(operation) or _FALLBACK_HASHES.get(operation)

    def _query(self, operation: str, variables: dict) -> Optional[dict]:
        h = self._hash_for(operation)
        if not h:
            return None
        return self._lib()._graphql(operation, variables, h)

    # ── artist search ────────────────────────────────────────────────────────

    def search_artists(self, query: str, limit: int = 8) -> list[dict]:
        """Real Spotify artist results: name, avatar, followers, Spotify URL."""
        if not self.sp_dc or not (query or "").strip():
            return []
        try:
            data = self._query("assistedCurationSearch", {"term": query.strip()})
        except Exception as e:
            logger.debug(f"[SpotifyPartner] artist search failed: {e}")
            return []

        uris = []
        for item in (((data or {}).get("data") or {}).get("searchV2") or {}) \
                .get("artists", {}).get("items", []) or []:
            uri = ((item.get("data") or {}).get("uri")) or ""
            if uri.startswith("spotify:artist:"):
                uris.append(uri)
        if not uris:
            return []
        uris = uris[:limit]

        # assistedCurationSearch only returns URIs, so each artist is hydrated.
        # Done in parallel: serially this is one round trip per result.
        with ThreadPoolExecutor(max_workers=min(len(uris), 6)) as pool:
            overviews = list(pool.map(self._artist_overview_safe, uris))

        from antra.utils.matching import string_similarity
        out = []
        for uri, ov in zip(uris, overviews):
            if not ov:
                continue
            artist_id = uri.rsplit(":", 1)[-1]
            name = ((ov.get("profile") or {}).get("name")) or ""
            if not name:
                continue
            out.append({
                "artist_id": artist_id,
                "name": name,
                "artwork_url": self._best_image((ov.get("visuals") or {}).get("avatarImage")),
                "genres": [],
                "followers": ((ov.get("stats") or {}).get("followers")) or 0,
                "match_score": string_similarity(query, name),
                "profile_url": f"https://open.spotify.com/artist/{artist_id}",
                "source": "spotify",
            })
        out.sort(key=lambda a: (-a["match_score"], -(a.get("followers") or 0)))
        return out

    def _artist_overview_safe(self, uri: str) -> Optional[dict]:
        try:
            d = self._query("queryArtistOverview", {"uri": uri})
            return ((d or {}).get("data") or {}).get("artistUnion")
        except Exception as e:
            logger.debug(f"[SpotifyPartner] overview failed for {uri}: {e}")
            return None

    # ── discography ──────────────────────────────────────────────────────────

    def fetch_artist_discography_info(self, url_or_id: str) -> dict:
        """Same contract as AppleFetcher/SpotifyClient.fetch_artist_discography_info.

        Editions of one release (usually an explicit and a clean one) are
        collapsed to the explicit edition, as the unsigned route's
        SpotifyClient._dedupe_discography_albums does.
        """
        artist_id = self._artist_id_from(url_or_id)
        if not artist_id:
            raise ValueError(f"Not a Spotify artist URL: {url_or_id}")

        uri = f"spotify:artist:{artist_id}"
        ov = self._artist_overview_safe(uri)
        if not ov:
            raise RuntimeError("Spotify artist lookup failed")

        releases = self._keep_best_editions(self.list_artist_releases(artist_id, overview=ov))
        # The key MUST be "albums" — both AppleFetcher and SpotifyClient return it
        # under that name and the discography modal reads
        # `discographyArtist.albums`. Returning "releases" renders an empty list
        # with the artist header still populated, which is what was reported.
        return {
            "artist_id": artist_id,
            "artist_name": ((ov.get("profile") or {}).get("name")) or "",
            "artwork_url": self._best_image((ov.get("visuals") or {}).get("avatarImage")),
            "albums": releases,
        }

    def list_artist_releases(self, url_or_id: str, overview: Optional[dict] = None) -> list[dict]:
        """Every release on the artist page, newest first, one entry per edition.

        Editions are deliberately not collapsed: the explicit twin lookup
        searches them for the explicit edition of a clean album. `overview`
        saves a request when the caller already holds the artist overview.
        Returns [] on failure and never raises.
        """
        artist_id = self._artist_id_from(url_or_id)
        if not artist_id or not self.sp_dc:
            return []
        try:
            return self._collect_releases(artist_id, overview)
        except Exception as e:
            logger.debug(f"[SpotifyPartner] release listing failed for {artist_id}: {e}")
            return []

    def _collect_releases(self, artist_id: str, ov: Optional[dict]) -> list[dict]:
        uri = f"spotify:artist:{artist_id}"
        releases: list[dict] = []
        seen: set[str] = set()

        def collect(container: dict, kind_hint: Optional[str]) -> int:
            for group in (container or {}).get("items", []) or []:
                for rel in (group.get("releases") or {}).get("items", []) or []:
                    formatted = self._format_release(rel, kind_hint)
                    if formatted and formatted["id"] not in seen:
                        seen.add(formatted["id"])
                        releases.append(formatted)
            return (container or {}).get("totalCount") or 0

        # queryArtistDiscographyAll returns every release in one shot, where the
        # artist overview only carries the first page of each bucket (10 of 16
        # albums, 10 of 28 singles for J. Cole). offset/limit are optional in the
        # schema but the resolver 500s without them, so they are always sent.
        paged = False
        try:
            offset, limit = 0, 50
            while True:
                d = self._query("queryArtistDiscographyAll",
                                {"uri": uri, "offset": offset, "limit": limit})
                bucket = (((d or {}).get("data") or {}).get("artistUnion") or {}) \
                    .get("discography", {}).get("all") or {}
                total = collect(bucket, None)
                paged = True
                offset += limit
                if offset >= total or not (bucket.get("items") or []):
                    break
        except Exception as e:
            logger.debug(f"[SpotifyPartner] paged discography failed: {e}")

        if not paged or not releases:
            # Fall back to the overview's first page rather than returning nothing.
            if ov is None:
                ov = self._artist_overview_safe(uri)
            disc = (ov or {}).get("discography") or {}
            for key, kind in (("albums", "album"), ("singles", "single"),
                              ("compilations", "compilation")):
                collect(disc.get(key) or {}, kind)

        releases.sort(key=lambda r: (-(r.get("year") or 0), r.get("name") or ""))
        return releases

    def _keep_best_editions(self, releases: list[dict]) -> list[dict]:
        """One release per edition group, preferring the explicit edition.

        Groups on SpotifyClient._discography_release_key and ranks like
        _discography_release_sort_key, so both discography routes keep the same
        edition. Only groups of two or more cost requests, one getAlbum per
        edition; the kept release gains `is_explicit` when its songs say.
        """
        from antra.core.spotify import SpotifyClient

        groups: dict[tuple, list[dict]] = {}
        for release in releases:
            groups.setdefault(SpotifyClient._discography_release_key(release), []).append(release)

        to_score = list(dict.fromkeys(
            r["id"] for group in groups.values() if len(group) > 1 for r in group))
        scores: dict[str, int] = {}
        if to_score:
            # In parallel: an artist with many doubled releases would otherwise
            # hold the artist page for one round trip per edition.
            with ThreadPoolExecutor(max_workers=min(len(to_score), 6)) as pool:
                scores = dict(zip(to_score, pool.map(self.album_explicit_score, to_score)))

        kept: list[dict] = []
        for group in groups.values():
            if len(group) == 1:
                kept.append(group[0])
                continue
            best = dict(max(group, key=lambda r: (
                scores.get(r["id"], 1),
                1 if r.get("artwork_url") else 0,
                int(r.get("track_count") or 0),
                str(r.get("id") or ""),
            )))
            score = scores.get(best["id"], 1)
            if score != 1:
                best["is_explicit"] = score == 2
            kept.append(best)
        return kept

    # ── albums ───────────────────────────────────────────────────────────────

    def get_album_summary(self, album_id: str) -> Optional[dict]:
        """An album and its songs' explicit flags, from getAlbum.

        {"id", "name", "type", "year", "track_count", "artist_ids",
         "edition_ids", "tracks": [{"id", "name", "artists", "duration_ms",
         "isrc", "explicit", "disc_number", "track_number"}]}

        `explicit` is True, False or None (no rating). `edition_ids` are the
        other editions getAlbum links to this one, which is how Spotify ties an
        explicit album to its clean counterpart. `isrc` stays None while
        getAlbum sends no externalIds (checked 2026-10-14).

        None on any failure, including a failed later page: a partial track list
        could make an explicit album look clean. Never raises.
        """
        aid = self._album_id_from(album_id)
        if not aid or not self.sp_dc:
            return None
        try:
            album: Optional[dict] = None
            items: list = []
            offset = 0
            while True:
                d = self._query("getAlbum", {"uri": f"spotify:album:{aid}", "locale": "",
                                             "offset": offset, "limit": _ALBUM_PAGE})
                au = ((d or {}).get("data") or {}).get("albumUnion") or {}
                # An unknown album still answers 200, as __typename "NotFound".
                if au.get("__typename", "Album") != "Album" or not au.get("name"):
                    return None
                if album is None:
                    album = au
                page = au.get("tracksV2") or {}
                page_items = page.get("items") or []
                items.extend(page_items)
                if not page_items or len(items) >= (page.get("totalCount") or 0):
                    break
                # Advance by what arrived, not by the limit asked for, so a
                # server-side cap on page size cannot skip songs.
                offset += len(page_items)
            return self._album_summary(aid, album, items)
        except Exception as e:
            logger.debug(f"[SpotifyPartner] album summary failed for {aid}: {e}")
            return None

    def album_explicit_score(self, album_id: str) -> int:
        """2 if any song is explicit, 0 if the rated songs are all clean, 1 if unknown.

        SpotifyClient._discography_release_explicit_score's scoring, read from
        getAlbum. A failed lookup scores 1, so it neither wins nor loses on
        explicitness alone.
        """
        try:
            summary = self.get_album_summary(album_id) or {}
            flags = [t.get("explicit") for t in summary.get("tracks") or []]
        except Exception:
            return 1
        if any(flag is True for flag in flags):
            return 2
        if any(flag is False for flag in flags):
            return 0
        return 1

    @classmethod
    def _album_summary(cls, album_id: str, album: dict, items: list) -> dict:
        tracks = []
        for idx, item in enumerate(items, start=1):
            track = item.get("track") if isinstance(item.get("track"), dict) else item
            if not isinstance(track, dict) or not track.get("name"):
                continue
            uri = track.get("uri") or ""
            external_ids = track.get("externalIds")
            duration = (track.get("duration") or {}).get("totalMilliseconds")
            artists = [((a or {}).get("profile") or {}).get("name") or ""
                       for a in (track.get("artists") or {}).get("items") or []]
            tracks.append({
                "id": uri.rsplit(":", 1)[-1] if ":" in uri else (track.get("id") or ""),
                "name": track.get("name") or "",
                "artists": [name for name in artists if name],
                "duration_ms": int(duration) if duration else None,
                "isrc": (external_ids.get("isrc") or None) if isinstance(external_ids, dict) else None,
                "explicit": explicit_from_content_rating(track.get("contentRating")),
                # The defaults SpotifyClient._fetch_album_via_partner_api uses, so
                # the numbers line up with the TrackMetadata built from an album.
                "disc_number": track.get("discNumber") or 1,
                "track_number": track.get("trackNumber") or idx,
            })

        artist_ids = []
        for artist in (album.get("artists") or {}).get("items") or []:
            artist_id = (artist or {}).get("id") or ((artist or {}).get("uri") or "").rsplit(":", 1)[-1]
            if artist_id:
                artist_ids.append(artist_id)

        own_id = (album.get("uri") or "").rsplit(":", 1)[-1] or album_id
        edition_ids = []
        for release in (album.get("releases") or {}).get("items") or []:
            release_id = ((release or {}).get("uri") or "").rsplit(":", 1)[-1]
            if release_id and release_id not in (own_id, album_id) and release_id not in edition_ids:
                edition_ids.append(release_id)

        raw_type = str(album.get("type") or "").upper()
        return {
            "id": own_id,
            "name": album.get("name") or "",
            # Same vocabulary as _format_release, so a summary compares with a
            # discography entry.
            "type": cls._TYPE_MAP.get(raw_type) or "album",
            "year": cls._year_of(album.get("date")),
            "track_count": (album.get("tracksV2") or {}).get("totalCount") or len(tracks),
            "artist_ids": artist_ids,
            "edition_ids": edition_ids,
            "tracks": tracks,
        }

    @staticmethod
    def _artist_id_from(url_or_id: str) -> str:
        s = (url_or_id or "").strip()
        m = re.search(r"(?:artist[:/])([A-Za-z0-9]{22})", s)
        if m:
            return m.group(1)
        return s if re.fullmatch(r"[A-Za-z0-9]{22}", s) else ""

    @staticmethod
    def _album_id_from(url_or_id: str) -> str:
        s = (url_or_id or "").strip()
        m = re.search(r"(?:album[:/])([A-Za-z0-9]{22})", s)
        if m:
            return m.group(1)
        return s if re.fullmatch(r"[A-Za-z0-9]{22}", s) else ""

    _TYPE_MAP = {"ALBUM": "album", "SINGLE": "single", "EP": "single",
                 "COMPILATION": "compilation"}

    @classmethod
    def _format_release(cls, rel: dict, kind: Optional[str]) -> Optional[dict]:
        uri = rel.get("uri") or ""
        rid = uri.rsplit(":", 1)[-1] if ":" in uri else (rel.get("id") or "")
        if not rid:
            return None
        year = cls._year_of(rel.get("date"))
        tracks = rel.get("tracks") or {}
        # The combined `all` bucket mixes types, so prefer the release's own
        # label and fall back to the caller's hint only when it is absent.
        raw_type = str(rel.get("type") or rel.get("albumType") or "").upper()
        resolved = cls._TYPE_MAP.get(raw_type) or kind or "album"
        return {
            "id": rid,
            "name": rel.get("name") or "Unknown",
            "year": year,
            "track_count": tracks.get("totalCount") if isinstance(tracks, dict) else None,
            "type": resolved,
            "url": f"https://open.spotify.com/album/{rid}",
            "artwork_url": cls._best_image(rel.get("coverArt")),
        }

    @staticmethod
    def _year_of(date) -> Optional[int]:
        # Discography entries carry `year`; getAlbum carries only `isoString`.
        if not isinstance(date, dict):
            return None
        year = date.get("year")
        if not year and date.get("isoString"):
            try:
                year = int(str(date["isoString"])[:4])
            except Exception:
                year = None
        return year

    @staticmethod
    def _best_image(node: Optional[dict]) -> str:
        sources = (node or {}).get("sources") or []
        if not sources:
            return ""
        best = max(sources, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
        return best.get("url", "") or ""
