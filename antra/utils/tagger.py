"""
File tagger using mutagen.
Supports MP3 (ID3) and FLAC (VorbisComment).
Embeds: title, artists, album, year, track number, genre, artwork, lyrics.
"""
import logging
import os
import threading
from io import BytesIO
from typing import Optional

import requests
from mutagen import File as MutagenFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    ID3,
    TIT2,  # Title
    TPE1,  # Artist
    TPE2,  # Album artist
    TALB,  # Album
    TDRC,  # Year
    TRCK,  # Track number
    TPOS,  # Disc number
    TCON,  # Genre
    TCOM,  # Composer
    TPUB,  # Publisher/Label
    APIC,  # Artwork
    USLT,  # Unsynced lyrics
    SYLT,  # Synced lyrics
    TSRC,  # ISRC
    TXXX,  # Custom text
    Encoding,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.mp3 import MP3
from mutagen.oggflac import OggFLAC
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from antra.utils.lyrics import validate_and_strip_lrc, lrc_to_sylt_frames
try:
    from PIL import Image
except ImportError:
    Image = None

from antra.core.explicit_common import advisory_value
from antra.core.models import TrackMetadata

from antra.utils.longpath import extended_path

logger = logging.getLogger(__name__)


def _sniff_image_mime(data: bytes, response_mime: Optional[str]) -> str:
    mime = (response_mime or "").split(";")[0].strip().lower()
    if mime in {"image/jpeg", "image/png"}:
        return mime

    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    return "image/jpeg"


class FileTagger:
    _MAX_EMBEDDED_ART_EDGE = 1000

    def __init__(self, write_antra_tags: bool = True):
        # Cache artwork by URL so albums only hit the CDN once regardless of
        # how many tracks share the same artwork_url.
        self._artwork_cache: dict[str, Optional[tuple[bytes, str, int, int, int]]] = {}
        self._raw_artwork_cache: dict[str, Optional[tuple[bytes, str]]] = {}
        self._sidecar_lock = threading.Lock()
        # v1.1.8 FEAT-4 — record which Antra version/source produced the file.
        self.write_antra_tags = write_antra_tags

    @staticmethod
    def _antra_provenance(track: TrackMetadata) -> list[tuple[str, str]]:
        """Provenance key/value pairs written into every tagged file (FEAT-4).

        The version comes from `antra.__version__`, the single source of truth,
        so it cannot drift from the label shown in the UI. `ANTRA_SOURCE` is
        recorded alongside it because "which version wrote this" and "where did
        it come from" are the same question when auditing a library for folders
        that need re-fetching.
        """
        from antra import __version__ as _antra_version
        import datetime as _dt

        pairs = [
            ("ANTRA_VERSION", str(_antra_version)),
            ("ANTRA_DOWNLOADED", _dt.datetime.now(_dt.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ")),
        ]
        source = (getattr(track, "source_service", None) or "").strip()
        if source:
            pairs.append(("ANTRA_SOURCE", source))
        return pairs

    def tag(
        self,
        file_path: str,
        track: TrackMetadata,
    ) -> bool:
        """Tag file at file_path with all available metadata. Returns True on success."""
        # MusicBrainz fallback enrichment
        from antra.core.musicbrainz_fetcher import enrich_metadata
        if not track.genres and track.isrc:
            logger.debug(f"[Tagger] Genres missing for {track.title}, querying MusicBrainz...")
            enrich_metadata(track)

        # v1.1.8 BUG-6: use the extended-length form so tagging works on paths
        # over the Windows 260-char limit instead of failing with [Errno 2]
        # and leaving an untagged file that later gets duplicated (BUG-3).
        file_path = extended_path(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".flac":
                self._tag_flac(file_path, track)
            elif ext == ".mp3":
                self._tag_mp3(file_path, track)
            elif ext in {".m4a", ".mp4"}:
                self._tag_mp4(file_path, track)
            else:
                logger.warning(f"Unsupported format for tagging: {ext}")
                self._write_lyrics_sidecars(file_path, track)
                return False
            
            self._write_antra_provenance(file_path, track)

            self.embed_lyrics(
                file_path,
                track.lyrics or "",
                track.synced_lyrics or "",
                track.duration_ms or 0
            )
            self._write_lyrics_sidecars(file_path, track)
            logger.debug(f"Tagged: {file_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to tag {file_path}: {e}")
            return False

    def save_cover_art_sidecar(self, file_path: str, track: TrackMetadata) -> Optional[str]:
        if not track.artwork_url:
            return None

        folder = os.path.dirname(file_path)
        if not folder:
            return None

        with self._sidecar_lock:
            existing = self._existing_cover_sidecar(folder)
            if existing:
                return existing

            artwork = self._fetch_raw_artwork(track.artwork_url)
            if not artwork:
                return None

            data, mime = artwork
            ext = ".png" if mime == "image/png" else ".jpg"
            out_path = os.path.join(folder, f"cover{ext}")
            try:
                with open(out_path, "wb") as fh:
                    fh.write(data)
                logger.debug("Saved cover art sidecar: %s", out_path)
                return out_path
            except Exception as e:
                logger.warning("Failed to save cover art sidecar for %s: %s", file_path, e)
                return None

    # ── FLAC ──────────────────────────────────────────────────────────────

    def _write_antra_provenance(self, file_path: str, track: TrackMetadata) -> None:
        """Write ANTRA_VERSION / ANTRA_DOWNLOADED / ANTRA_SOURCE (v1.1.8 FEAT-4).

        Written as a separate pass (the same pattern `embed_lyrics` already uses)
        so the three per-container tagging methods stay untouched. Failures are
        swallowed: provenance is a convenience for library auditing and must
        never be able to fail an otherwise good download.
        """
        if not self.write_antra_tags:
            return
        pairs = self._antra_provenance(track)
        ext = os.path.splitext(file_path)[1].lower()
        try:
            if ext == ".flac":
                from mutagen.flac import FLAC
                audio = FLAC(file_path)
                for key, value in pairs:
                    audio[key.lower()] = value
                audio.save()
            elif ext == ".mp3":
                from mutagen.id3 import ID3, TXXX
                try:
                    audio = ID3(file_path)
                except Exception:
                    audio = ID3()
                for key, value in pairs:
                    audio.add(TXXX(encoding=3, desc=key, text=value))
                audio.save(file_path)
            elif ext in {".m4a", ".mp4"}:
                from mutagen.mp4 import MP4
                audio = MP4(file_path)
                for key, value in pairs:
                    # Freeform atoms must be bytes and namespaced.
                    audio[f"----:com.antra:{key}"] = [value.encode("utf-8")]
                audio.save()
        except Exception as e:
            logger.debug(f"[Tagger] Could not write Antra provenance tags to {file_path}: {e}")

    def _tag_flac(self, path: str, track: TrackMetadata):
        audio = FLAC(path)

        audio["title"] = track.title
        audio["artist"] = track.artists
        album_artist_str = ", ".join(track.album_artists) if track.album_artists else track.primary_artist
        audio["albumartist"] = [album_artist_str]
        audio["album"] = track.album
        date_str = track.release_date or (str(track.release_year) if track.release_year else None)
        if date_str:
            audio["date"] = date_str
            # Also write YEAR (4-digit) — Windows Media Player reads YEAR, not DATE
            year_str = date_str[:4] if len(date_str) >= 4 and date_str[:4].isdigit() else None
            if year_str:
                audio["year"] = year_str
        if track.track_number:
            tn = str(track.track_number)
            if track.total_tracks:
                tn += f"/{track.total_tracks}"
            audio["tracknumber"] = tn
        if track.disc_number:
            audio["discnumber"] = str(track.disc_number)
        if track.genres:
            audio["genre"] = track.genres
        if track.isrc:
            audio["isrc"] = track.isrc
        if track.upc:
            audio["barcode"] = track.upc
        if track.iswc:
            audio["iswc"] = track.iswc
        if track.audio_traits:
            audio["audio_traits"] = track.audio_traits
        if track.spotify_id:
            audio["spotify_id"] = track.spotify_id
        if track.composer:
            audio["composer"] = [track.composer]
        lbl = getattr(track, "label", None)
        if lbl:
            audio["organization"] = [lbl]

        artwork = self._fetch_artwork(track.artwork_url)
        if artwork:
            artwork_data, mime, width, height, depth = artwork
            audio.clear_pictures()
            pic = Picture()
            pic.type = 3  # Cover (front)
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = artwork_data
            pic.width = width
            pic.height = height
            pic.depth = depth
            pic.colors = 0
            audio.add_picture(pic)

        audio.save()

    # ── MP3 ───────────────────────────────────────────────────────────────

    def _tag_mp3(self, path: str, track: TrackMetadata):
        try:
            audio = ID3(path)
        except Exception:
            audio = ID3()

        audio.add(TIT2(encoding=3, text=track.title))
        audio.add(TPE1(encoding=3, text=track.artist_string))
        album_artist_str = ", ".join(track.album_artists) if track.album_artists else track.primary_artist
        audio.add(TPE2(encoding=3, text=album_artist_str))
        audio.add(TALB(encoding=3, text=track.album))

        date_str = track.release_date or (str(track.release_year) if track.release_year else None)
        if date_str:
            audio.add(TDRC(encoding=3, text=date_str))
        if track.track_number:
            tn = str(track.track_number)
            if track.total_tracks:
                tn += f"/{track.total_tracks}"
            audio.add(TRCK(encoding=3, text=tn))
        if track.disc_number:
            audio.add(TPOS(encoding=3, text=str(track.disc_number)))
        if track.genres:
            audio.add(TCON(encoding=3, text=", ".join(track.genres)))
        if track.isrc:
            audio.add(TSRC(encoding=3, text=track.isrc))
        if track.upc:
            audio.add(TXXX(encoding=3, desc="BARCODE", text=track.upc))
        if track.iswc:
            audio.add(TXXX(encoding=3, desc="ISWC", text=track.iswc))
        if track.audio_traits:
            audio.add(TXXX(encoding=3, desc="AUDIO_TRAITS", text=", ".join(track.audio_traits)))
        if track.spotify_id:
            audio.add(TXXX(encoding=3, desc="SPOTIFYID", text=track.spotify_id))
        if track.composer:
            audio.add(TCOM(encoding=3, text=track.composer))
        lbl = getattr(track, "label", None)
        if lbl:
            audio.add(TPUB(encoding=3, text=lbl))

        # Artwork
        artwork = self._fetch_artwork(track.artwork_url)
        if artwork:
            artwork_data, mime, _width, _height, _depth = artwork
            audio.delall("APIC")
            audio.add(APIC(
                encoding=3,
                mime=mime,
                type=3,
                desc="Cover",
                data=artwork_data,
            ))

        # Save as ID3v2.3 instead of mutagen's native v2.4 so Windows Explorer
        # and standard mobile players can reliably read the Cover Art APIC frame.
        audio.save(path, v1=2, v2_version=3)

    # ── MP4 / M4A ────────────────────────────────────────────────────────────

    def _tag_mp4(self, path: str, track: TrackMetadata):
        audio = MP4(path)

        audio["\xa9nam"] = [track.title]
        audio["\xa9ART"] = track.artists
        album_artist_str = ", ".join(track.album_artists) if track.album_artists else track.primary_artist
        audio["aART"] = [album_artist_str]
        audio["\xa9alb"] = [track.album]

        date_str = track.release_date or (str(track.release_year) if track.release_year else None)
        if date_str:
            audio["\xa9day"] = [date_str]
        if track.track_number:
            audio["trkn"] = [(track.track_number, track.total_tracks or 0)]
        if track.disc_number:
            audio["disk"] = [(track.disc_number, 0)]
        if track.genres:
            audio["\xa9gen"] = track.genres
        if track.spotify_id:
            audio["----:com.apple.iTunes:SPOTIFYID"] = [MP4FreeForm(track.spotify_id.encode("utf-8"))]
        if track.isrc:
            audio["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(track.isrc.encode("utf-8"))]
        if track.composer:
            audio["\xa9wrt"] = [track.composer]
        lbl = getattr(track, "label", None)
        if lbl:
            audio["----:com.apple.iTunes:LABEL"] = [MP4FreeForm(lbl.encode("utf-8"))]

        artwork = self._fetch_artwork(track.artwork_url)
        if artwork:
            artwork_data, mime, _width, _height, _depth = artwork
            image_format = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(artwork_data, imageformat=image_format)]

        audio.save()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _fetch_artwork(self, url: Optional[str]) -> Optional[tuple[bytes, str, int, int, int]]:
        if not url:
            return None
        if url in self._artwork_cache:
            return self._artwork_cache[url]

        result = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                result = FileTagger._normalize_artwork(resp.content, resp.headers.get("Content-Type"))
                break
            except Exception as e:
                if attempt < 2:
                    import time
                    time.sleep(1.5 ** attempt)
                else:
                    logger.warning(f"Failed to download artwork from {url}: {e}")

        self._artwork_cache[url] = result
        return result

    @staticmethod
    def _existing_cover_sidecar(folder: str) -> Optional[str]:
        for name in ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png"):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return path
        return None

    def _fetch_raw_artwork(self, url: Optional[str]) -> Optional[tuple[bytes, str]]:
        if not url:
            return None
        if url in self._raw_artwork_cache:
            return self._raw_artwork_cache[url]

        result: Optional[tuple[bytes, str]] = None
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=10)
                resp.raise_for_status()
                result = FileTagger._prepare_sidecar_artwork(resp.content, resp.headers.get("Content-Type"))
                break
            except Exception as e:
                if attempt < 2:
                    import time
                    time.sleep(1.5 ** attempt)
                else:
                    logger.warning(f"Failed to download sidecar artwork from {url}: {e}")

        self._raw_artwork_cache[url] = result
        return result

    @staticmethod
    def _prepare_sidecar_artwork(data: bytes, response_mime: Optional[str]) -> tuple[bytes, str]:
        mime = _sniff_image_mime(data, response_mime)
        if Image is None:
            return data, mime

        image = Image.open(BytesIO(data))
        image.load()
        if mime == "image/png" and image.mode in ("RGBA", "LA"):
            out = BytesIO()
            image.save(out, format="PNG", optimize=True)
            return out.getvalue(), "image/png"

        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
            mime = "image/jpeg"

        if mime == "image/png":
            out = BytesIO()
            image.save(out, format="PNG", optimize=True)
            return out.getvalue(), "image/png"

        out = BytesIO()
        image = image.convert("RGB")
        image.save(out, format="JPEG", quality=95, optimize=True)
        return out.getvalue(), "image/jpeg"

    @staticmethod
    def _normalize_artwork(data: bytes, response_mime: Optional[str]) -> tuple[bytes, str, int, int, int]:
        mime = _sniff_image_mime(data, response_mime)
        if Image is None:
            return data, mime, 0, 0, 0

        image = Image.open(BytesIO(data))
        image.load()
        width, height = image.size
        if max(width, height) > FileTagger._MAX_EMBEDDED_ART_EDGE:
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
            image.thumbnail(
                (FileTagger._MAX_EMBEDDED_ART_EDGE, FileTagger._MAX_EMBEDDED_ART_EDGE),
                resampling,
            )
            width, height = image.size

        if mime == "image/png" and image.mode in ("RGBA", "LA"):
            image = image.convert("RGB")
            mime = "image/jpeg"
        elif image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
            mime = "image/jpeg"

        if mime == "image/png":
            out = BytesIO()
            image.save(out, format="PNG", optimize=True)
            depth = 32 if image.mode == "RGBA" else 8
            return out.getvalue(), "image/png", width, height, depth

        out = BytesIO()
        image = image.convert("RGB")
        image.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue(), "image/jpeg", width, height, 24

    @staticmethod
    def _write_lyrics_sidecars(file_path: str, track: TrackMetadata):
        """
        Writes .lrc and .txt sidecar files for external players.

        Skipped for formats that already carry lyrics in their tags:
          - FLAC  → SYNCEDLYRICS / LYRICS VorbisComment tags
          - MP3   → SYLT (synced) / USLT (plain) ID3 frames
          - M4A   → ©lyr atom
        For any other extension (e.g. .ogg, .opus) sidecars are still written.
        """
        ext = os.path.splitext(file_path)[1].lower()

        # These formats have embedded lyrics written by embed_lyrics(); no sidecar needed.
        _EMBEDDED_FORMATS = {".flac", ".mp3", ".m4a", ".mp4", ".aac"}

        if ext in _EMBEDDED_FORMATS:
            return

        base, _ = os.path.splitext(file_path)

        if track.synced_lyrics:
            try:
                with open(base + ".lrc", "w", encoding="utf-8") as handle:
                    handle.write(track.synced_lyrics)
            except Exception as e:
                logger.warning(f"Failed to write synced lyrics sidecar for {file_path}: {e}")

        if track.lyrics:
            try:
                with open(base + ".txt", "w", encoding="utf-8") as handle:
                    handle.write(track.lyrics)
            except Exception as e:
                logger.warning(f"Failed to write plain lyrics sidecar for {file_path}: {e}")

    def embed_lyrics(
        self,
        path: str,
        lyrics: str = "",
        synced_lyrics: str = "",
        duration_ms: int = 0,
    ) -> None:
        """
        Embed lyrics into audio file metadata.
        Prioritizes synced (LRC) lyrics over plain text.
        Validates LRC timestamps against duration before embedding.
        """
        # Validate and clean LRC if we have duration info
        if synced_lyrics and duration_ms:
            synced_lyrics = validate_and_strip_lrc(synced_lyrics, duration_ms)

        # Best available lyric string for formats that don't support synced
        best_plain = synced_lyrics or lyrics  # LRC is readable as plain text too

        ext = os.path.splitext(path)[1].lower()

        try:
            if ext == ".flac":
                audio = FLAC(path)
                if synced_lyrics:
                    audio["SYNCEDLYRICS"] = [synced_lyrics]
                if lyrics:
                    audio["LYRICS"] = [lyrics]
                elif synced_lyrics:
                    audio["LYRICS"] = [synced_lyrics]
                audio.save()

            elif ext == ".mp3":
                try:
                    audio = ID3(path)
                except Exception:
                    audio = ID3()
                # Plain/unsynced lyrics
                best = lyrics or synced_lyrics
                if best:
                    audio.add(USLT(
                        encoding=Encoding.UTF8,
                        lang="eng",
                        desc="",
                        text=best,
                    ))
                # Synced lyrics via SYLT
                if synced_lyrics:
                    frames = lrc_to_sylt_frames(synced_lyrics)
                    if frames:
                        audio.add(SYLT(
                            encoding=Encoding.UTF8,
                            lang="eng",
                            format=2,   # milliseconds
                            type=1,     # lyrics
                            desc="",
                            text=frames,
                        ))
                audio.save(path, v1=2, v2_version=3)

            elif ext in (".m4a", ".mp4", ".aac"):
                audio = MP4(path)
                # M4A only supports plain text lyrics via \xa9lyr atom
                # Use LRC as plain text if no plain lyrics available
                if best_plain:
                    audio["\xa9lyr"] = [best_plain]
                audio.save()

            else:
                from logging import getLogger
                getLogger(__name__).debug(f"[Tagger] Unsupported format for lyric embedding: {ext}")

        except Exception as e:
            from logging import getLogger
            getLogger(__name__).warning(f"[Tagger] Failed to embed lyrics into {path}: {e}")


# ── Explicit advisory ─────────────────────────────────────────────────────

# The iTunes content-advisory tag, which Apple Music, foobar2000, MusicBee and
# Plex read as the explicit flag. The same name serves as the Vorbis comment key
# and the ID3 TXXX description; MP4 has its own `rtng` atom.
_ADVISORY_KEY = "ITUNESADVISORY"

# An ".ogg" can hold Vorbis, Opus or FLAC, so mutagen picks the reader from the
# stream header instead of trusting the extension.
_OGG_TYPES = [OggVorbis, OggOpus, OggFLAC]


def write_explicit_advisory(file_path: str, status: Optional[str]) -> bool:
    """Write the iTunes advisory for an explicit status: 1 explicit, 2 clean edit.

    Any other status removes an advisory the file already carries, because a
    source can ship its own flag and a re-download must not keep a stale one.
    Returns True when the file now holds the right value (nothing to remove
    counts), False for a format without an advisory tag or a file mutagen
    cannot read or save. Never raises: like the provenance tags, the flag is a
    convenience and must not be able to fail an otherwise good download.
    """
    value = advisory_value(status)
    file_path = extended_path(file_path)
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == ".flac":
            audio = FLAC(file_path)
            if _set_vorbis_advisory(audio, value):
                audio.save()
        elif ext in {".ogg", ".opus"}:
            audio = MutagenFile(file_path, options=_OGG_TYPES)
            if audio is None:
                logger.debug(f"[Tagger] No Ogg stream in {file_path}, explicit advisory not written")
                return False
            if _set_vorbis_advisory(audio, value):
                audio.save()
        elif ext == ".mp3":
            # MP3() rather than the bare ID3() the other passes use: it refuses a
            # file with no MPEG audio instead of prepending a tag to it.
            audio = MP3(file_path)
            if _set_id3_advisory(audio, value):
                # ID3v2.3, as `_tag_mp3` saves, so this pass does not undo it.
                audio.save(v1=2, v2_version=3)
        elif ext in {".m4a", ".mp4"}:
            audio = MP4(file_path)
            if _set_mp4_advisory(audio, value):
                audio.save()
        else:
            logger.debug(f"[Tagger] Unsupported format for explicit advisory: {ext}")
            return False
        return True
    except Exception as e:
        logger.debug(f"[Tagger] Could not write explicit advisory to {file_path}: {e}")
        return False


def _set_vorbis_advisory(audio, value: Optional[int]) -> bool:
    """Set or clear the advisory comment; True when the tags changed.

    Vorbis comment keys are case-insensitive, so this also replaces or removes
    an "iTunesAdvisory" written by another tool.
    """
    if value is not None:
        audio[_ADVISORY_KEY] = [str(value)]
        return True
    if audio.tags is not None and _ADVISORY_KEY in audio.tags:
        del audio.tags[_ADVISORY_KEY]
        return True
    return False


def _set_id3_advisory(audio: MP3, value: Optional[int]) -> bool:
    """Set or clear the advisory TXXX frame; True when the tags changed."""
    tags = audio.tags
    # TXXX descriptions are case-sensitive, so every spelling of the key is
    # cleared, not just the one written here; otherwise a file could end up
    # with two advisories that disagree.
    stale = [] if tags is None else [
        frame.HashKey for frame in tags.getall("TXXX") if frame.desc.upper() == _ADVISORY_KEY
    ]
    for key in stale:
        del tags[key]
    if value is None:
        return bool(stale)
    if tags is None:
        audio.add_tags()
        tags = audio.tags
    tags.add(TXXX(encoding=3, desc=_ADVISORY_KEY, text=str(value)))
    return True


def _set_mp4_advisory(audio: MP4, value: Optional[int]) -> bool:
    """Set or clear the `rtng` atom; True when the tags changed.

    Players read `rtng` as an integer, so it is stored as one, not as text.
    """
    if value is not None:
        audio["rtng"] = [value]
        return True
    if audio.tags is not None and "rtng" in audio.tags:
        del audio.tags["rtng"]
        return True
    return False
