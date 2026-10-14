"""
Structured events emitted by the download engine.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from antra.core.models import TrackMetadata


class EngineEventType(Enum):
    PLAYLIST_STARTED = "playlist_started"
    PLAYLIST_COMPLETED = "playlist_completed"
    PLAYLIST_CANCELLED = "playlist_cancelled"
    TRACK_STARTED = "track_started"
    TRACK_SKIPPED = "track_skipped"
    TRACK_RESOLVED = "track_resolved"
    TRACK_DOWNLOAD_ATTEMPT = "track_download_attempt"
    TRACK_COMPLETED = "track_completed"
    TRACK_FAILED = "track_failed"


@dataclass
class EngineEvent:
    type: EngineEventType
    track: Optional[TrackMetadata] = None
    track_index: Optional[int] = None
    track_total: Optional[int] = None
    message: Optional[str] = None
    source: Optional[str] = None
    quality_label: Optional[str] = None
    attempt: Optional[int] = None
    file_path: Optional[str] = None
    error: Optional[str] = None
    # One of the STATUS_* values in explicit_common, for the copy actually
    # chosen: the track payload only says what was asked for.
    explicit_status: Optional[str] = None
