"""
Per-song explicit status on engine events, the advisory tag call, and the
JSON the desktop UI reads.
"""
import json

import pytest

import antra.utils.tagger as tagger_module
from antra.core.engine import DownloadEngine, EngineConfig
from antra.core.events import EngineEvent, EngineEventType
from antra.core.explicit_common import (
    STATUS_CLEAN_EDIT,
    STATUS_EXPLICIT,
    STATUS_NOT_EXPLICIT,
    STATUS_UNKNOWN,
)
from antra.core.metadata_enricher import MetadataEnricher
from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.core.resolver import SourceResolver
from antra.sources.base import BaseSourceAdapter


class FileAdapter(BaseSourceAdapter):
    """Finds one result and 'downloads' it as a small FLAC-named file."""

    name = "qobuz"
    priority = 1

    def __init__(self, result):
        self._result = result

    def is_available(self):
        return True

    def search(self, track):
        return self._result

    def download(self, result, output_path):
        path = output_path + ".flac"
        with open(path, "wb") as fh:
            fh.write(b"\0" * 4096)
        return path


class FakeOrganizer:
    def __init__(self, base, existing=None):
        self.base = base
        self.existing = existing

    def is_already_downloaded(self, track):
        return self.existing

    def ensure_playlist_copy(self, track, existing):
        return existing

    def get_output_path(self, track):
        return self.base

    def mark_downloaded(self, track, file_path):
        pass

    def mark_failed(self, track, error):
        pass


class FakeTagger:
    def tag(self, file_path, track):
        return True

    def save_cover_art_sidecar(self, file_path, track):
        return None


def make_track(**overrides):
    fields = dict(title="HUMBLE.", artists=["Kendrick Lamar"], album="DAMN.")
    fields.update(overrides)
    return TrackMetadata(**fields)


def make_result(explicit=None, title="HUMBLE."):
    return SearchResult(
        source="qobuz",
        title=title,
        artists=["Kendrick Lamar"],
        album="DAMN.",
        duration_ms=None,
        audio_format=AudioFormat.FLAC,
        quality_kbps=None,
        is_lossless=True,
        download_url=None,
        stream_id="1",
        similarity_score=0.95,
        bit_depth=16,
        sample_rate_hz=44100,
        is_explicit=explicit,
    )


@pytest.fixture
def advisory_calls(monkeypatch):
    calls = []

    def fake_advisory(file_path, status):
        calls.append((file_path, status))
        return True

    monkeypatch.setattr(tagger_module, "write_explicit_advisory", fake_advisory, raising=False)
    return calls


@pytest.fixture(autouse=True)
def offline_enricher(monkeypatch):
    monkeypatch.setattr(MetadataEnricher, "enrich", staticmethod(lambda track, result: None))


def run_download(tmp_path, track, result, prefer_explicit=True, existing=None):
    events = []
    resolver = SourceResolver(
        [FileAdapter(result)],
        preferred_output_format="source",
        prefer_explicit=prefer_explicit,
    )
    engine = DownloadEngine(
        resolver=resolver,
        organizer=FakeOrganizer(str(tmp_path / "song"), existing=existing),
        config=EngineConfig(fetch_lyrics=False, max_retries=1, write_antra_tags=False),
        event_callback=events.append,
    )
    engine.tagger = FakeTagger()
    # The fake file is not real audio; these checks are covered elsewhere.
    engine._probe_duration_seconds = lambda path: None
    engine._get_truncation_reason = lambda *args, **kwargs: None
    download = engine.download_track(track, track_index=1, track_total=1)
    return download, events


def events_of(events, event_type):
    return [e for e in events if e.type is event_type]


def test_event_explicit_status_defaults_to_none():
    assert EngineEvent(type=EngineEventType.TRACK_STARTED).explicit_status is None


@pytest.mark.parametrize(
    "track_fields, result_fields, expected",
    [
        (dict(target_explicit=True), dict(explicit=True), STATUS_EXPLICIT),
        (dict(target_explicit=True), dict(explicit=False), STATUS_CLEAN_EDIT),
        (dict(target_explicit=True), dict(explicit=None), STATUS_UNKNOWN),
        (dict(target_explicit=False), dict(explicit=None), STATUS_NOT_EXPLICIT),
        (dict(target_explicit=None), dict(explicit=None), STATUS_UNKNOWN),
        # No twin step ran: Spotify's own explicit flag sets the target.
        (dict(is_explicit=True), dict(explicit=None, title="HUMBLE. (Clean)"), STATUS_CLEAN_EDIT),
    ],
)
def test_resolved_and_completed_carry_the_status(
    tmp_path, advisory_calls, track_fields, result_fields, expected
):
    download, events = run_download(tmp_path, make_track(**track_fields), make_result(**result_fields))

    assert download.file_path
    [resolved] = events_of(events, EngineEventType.TRACK_RESOLVED)
    [completed] = events_of(events, EngineEventType.TRACK_COMPLETED)
    assert resolved.explicit_status == expected
    assert completed.explicit_status == expected
    assert advisory_calls == [(download.file_path, expected)]


def test_status_is_judged_against_the_target_before_enrichment(tmp_path, monkeypatch, advisory_calls):
    # The enricher fills is_explicit from the winning source after download; the
    # completed status must still match the one the song resolved with.
    def enrich(track, result):
        track.is_explicit = True

    monkeypatch.setattr(MetadataEnricher, "enrich", staticmethod(enrich))
    _, events = run_download(tmp_path, make_track(), make_result(explicit=None))

    [resolved] = events_of(events, EngineEventType.TRACK_RESOLVED)
    [completed] = events_of(events, EngineEventType.TRACK_COMPLETED)
    assert resolved.explicit_status == STATUS_UNKNOWN
    assert completed.explicit_status == STATUS_UNKNOWN


def test_no_advisory_tag_with_the_setting_off(tmp_path, advisory_calls):
    _, events = run_download(
        tmp_path, make_track(is_explicit=True), make_result(explicit=True), prefer_explicit=False
    )
    # The badge still shows; only the file tag depends on the setting.
    [completed] = events_of(events, EngineEventType.TRACK_COMPLETED)
    assert completed.explicit_status == STATUS_EXPLICIT
    assert advisory_calls == []


def test_download_completes_without_the_tagger_function(tmp_path, monkeypatch):
    monkeypatch.delattr(tagger_module, "write_explicit_advisory", raising=False)
    download, events = run_download(tmp_path, make_track(target_explicit=True), make_result(explicit=True))
    assert download.file_path
    assert events_of(events, EngineEventType.TRACK_COMPLETED)


def test_a_failing_advisory_does_not_fail_the_download(tmp_path, monkeypatch):
    def broken(file_path, status):
        raise RuntimeError("boom")

    monkeypatch.setattr(tagger_module, "write_explicit_advisory", broken, raising=False)
    download, events = run_download(tmp_path, make_track(target_explicit=True), make_result(explicit=True))
    assert download.file_path
    assert events_of(events, EngineEventType.TRACK_COMPLETED)


@pytest.mark.parametrize(
    "track_fields, expected",
    [
        (dict(target_explicit=True, is_explicit=True), STATUS_EXPLICIT),
        (dict(target_explicit=True, is_explicit=None), STATUS_UNKNOWN),
        (dict(target_explicit=False, is_explicit=False), STATUS_NOT_EXPLICIT),
    ],
)
def test_skipped_uses_the_tracks_own_flag(tmp_path, advisory_calls, track_fields, expected):
    existing = str(tmp_path / "already.flac")
    _, events = run_download(
        tmp_path, make_track(**track_fields), make_result(explicit=True), existing=existing
    )
    [skipped] = events_of(events, EngineEventType.TRACK_SKIPPED)
    assert skipped.explicit_status == expected
    assert advisory_calls == []


# ── JSON for the desktop UI ───────────────────────────────────────────────────


def test_emit_event_payload_carries_explicit_status(capsys):
    from antra.json_cli import emit_event

    emit_event(EngineEvent(
        type=EngineEventType.TRACK_COMPLETED,
        track=make_track(target_explicit=True),
        source="qobuz",
        quality_label="FLAC 16-bit/44kHz",
        explicit_status=STATUS_CLEAN_EDIT,
    ))
    payload = json.loads(capsys.readouterr().out)["payload"]
    assert payload["explicit_status"] == STATUS_CLEAN_EDIT
    assert payload["source"] == "qobuz"


def test_emit_event_payload_explicit_status_is_null_when_unset(capsys):
    from antra.json_cli import emit_event

    emit_event(EngineEvent(type=EngineEventType.TRACK_STARTED, track=make_track()))
    payload = json.loads(capsys.readouterr().out)["payload"]
    assert "explicit_status" in payload
    assert payload["explicit_status"] is None


@pytest.mark.parametrize("flag", [True, False, None])
def test_playlist_track_entry_carries_spotify_flag(flag):
    from antra.json_cli import _playlist_track_entry

    entry = _playlist_track_entry(make_track(is_explicit=flag, duration_ms=177000))
    assert entry == {
        "artist": "Kendrick Lamar",
        "title": "HUMBLE.",
        "duration_ms": 177000,
        "explicit": flag,
    }
    assert json.loads(json.dumps(entry))["explicit"] is flag
