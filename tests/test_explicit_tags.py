"""
The iTunes advisory tag `write_explicit_advisory` puts in each format, checked
on real files: a second of silence encoded by the ffmpeg binary imageio-ffmpeg
ships, so mutagen reads exactly what a download would produce.
"""
import os
import shutil
import subprocess

import pytest
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, TIT2, TXXX
from mutagen.mp4 import MP4

from antra.core.explicit_common import (
    STATUS_CLEAN_EDIT,
    STATUS_EXPLICIT,
    STATUS_NOT_EXPLICIT,
    STATUS_UNKNOWN,
)
from antra.utils.tagger import write_explicit_advisory

imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")

# Extension -> ffmpeg encoder arguments. ".ogg" is Vorbis and ".opus" is Opus,
# the two Ogg flavours a download can arrive as.
_ENCODERS = {
    "flac": ["-c:a", "flac"],
    "mp3": ["-c:a", "libmp3lame", "-b:a", "64k"],
    "m4a": ["-c:a", "aac", "-b:a", "64k"],
    "ogg": ["-c:a", "libvorbis"],
    "opus": ["-c:a", "libopus", "-b:a", "32k"],
}

_CORRUPT = b"this is not audio\n" * 64


@pytest.fixture(scope="module")
def masters(tmp_path_factory):
    """One encoded file per format, made once; each test works on a copy."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    folder = tmp_path_factory.mktemp("masters")
    paths = {}
    for ext, codec in _ENCODERS.items():
        path = folder / f"silence.{ext}"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "1",
             *codec, str(path)],
            check=True,
            capture_output=True,
        )
        paths[ext] = path
    return paths


@pytest.fixture(params=sorted(_ENCODERS))
def audio_file(request, masters, tmp_path):
    dest = tmp_path / f"song.{request.param}"
    shutil.copyfile(masters[request.param], dest)
    return str(dest)


def read_advisory(path: str) -> list[str]:
    """Every advisory value in the file, in any key case, as strings."""
    ext = os.path.splitext(path)[1]
    if ext == ".mp3":
        frames = [f for f in ID3(path).getall("TXXX") if f.desc.upper() == "ITUNESADVISORY"]
        return [str(text) for frame in frames for text in frame.text]
    if ext == ".m4a":
        tags = MP4(path).tags
        return [str(v) for v in (tags.get("rtng", []) if tags is not None else [])]
    tags = MutagenFile(path).tags
    return list(tags.get("ITUNESADVISORY") or []) if tags is not None else []


def plant_foreign_tags(path: str) -> None:
    """A title plus an advisory written the way another tool might have
    (mixed-case key, or iTunes' old explicit value 4), as a source file or an
    earlier download could carry them."""
    ext = os.path.splitext(path)[1]
    if ext == ".mp3":
        tags = ID3(path)
        tags.add(TIT2(encoding=3, text="Kept Title"))
        tags.add(TXXX(encoding=3, desc="iTunesAdvisory", text="1"))
        tags.save(path, v2_version=3)
    elif ext == ".m4a":
        audio = MP4(path)
        audio["\xa9nam"] = ["Kept Title"]
        audio["rtng"] = [4]
        audio.save()
    else:
        audio = MutagenFile(path)
        audio["title"] = ["Kept Title"]
        audio["iTunesAdvisory"] = ["1"]
        audio.save()


def read_title(path: str) -> str:
    ext = os.path.splitext(path)[1]
    if ext == ".mp3":
        return str(ID3(path)["TIT2"].text[0])
    if ext == ".m4a":
        return MP4(path)["\xa9nam"][0]
    return MutagenFile(path)["title"][0]


@pytest.mark.parametrize("status, expected", [(STATUS_EXPLICIT, "1"), (STATUS_CLEAN_EDIT, "2")])
def test_writes_the_advisory_value(audio_file, status, expected):
    assert write_explicit_advisory(audio_file, status) is True
    assert read_advisory(audio_file) == [expected]


def test_a_new_status_replaces_the_old_value(audio_file):
    assert write_explicit_advisory(audio_file, STATUS_EXPLICIT) is True
    assert write_explicit_advisory(audio_file, STATUS_CLEAN_EDIT) is True
    assert read_advisory(audio_file) == ["2"]


@pytest.mark.parametrize("status", [STATUS_NOT_EXPLICIT, STATUS_UNKNOWN, None])
def test_no_advisory_removes_an_existing_one(audio_file, status):
    assert write_explicit_advisory(audio_file, STATUS_EXPLICIT) is True
    assert write_explicit_advisory(audio_file, status) is True
    assert read_advisory(audio_file) == []


def test_removal_catches_another_tools_spelling_and_keeps_other_tags(audio_file):
    # A re-download over a file someone else flagged must not keep that flag,
    # and clearing it must not take the rest of the tags with it.
    plant_foreign_tags(audio_file)
    assert read_advisory(audio_file) != []
    assert write_explicit_advisory(audio_file, STATUS_NOT_EXPLICIT) is True
    assert read_advisory(audio_file) == []
    assert read_title(audio_file) == "Kept Title"


def test_writing_over_another_tools_spelling_leaves_one_value(audio_file):
    plant_foreign_tags(audio_file)
    assert write_explicit_advisory(audio_file, STATUS_CLEAN_EDIT) is True
    assert read_advisory(audio_file) == ["2"]


def test_removal_from_an_untagged_file_succeeds(audio_file):
    assert read_advisory(audio_file) == []
    assert write_explicit_advisory(audio_file, None) is True
    assert read_advisory(audio_file) == []


def test_m4a_rating_is_an_integer_atom(masters, tmp_path):
    # Players read `rtng` as a number; a string or freeform atom is ignored.
    path = tmp_path / "song.m4a"
    shutil.copyfile(masters["m4a"], path)
    assert write_explicit_advisory(str(path), STATUS_EXPLICIT) is True
    assert MP4(str(path))["rtng"] == [1]


def test_mp3_stays_id3v23(masters, tmp_path):
    # The rest of the tagger saves ID3v2.3 for Windows Explorer and phones;
    # the advisory pass must not quietly upgrade the tag to v2.4.
    path = tmp_path / "song.mp3"
    shutil.copyfile(masters["mp3"], path)
    assert write_explicit_advisory(str(path), STATUS_EXPLICIT) is True
    assert ID3(str(path)).version == (2, 3, 0)


def test_unknown_extension_returns_false(tmp_path):
    path = tmp_path / "song.wav"
    path.write_bytes(b"RIFF" + b"\x00" * 64)
    assert write_explicit_advisory(str(path), STATUS_EXPLICIT) is False


def test_missing_file_returns_false(tmp_path):
    assert write_explicit_advisory(str(tmp_path / "gone.flac"), STATUS_EXPLICIT) is False


@pytest.mark.parametrize("ext", sorted(_ENCODERS))
@pytest.mark.parametrize("status", [STATUS_EXPLICIT, None])
def test_corrupt_file_returns_false_and_is_left_alone(tmp_path, ext, status):
    path = tmp_path / f"broken.{ext}"
    path.write_bytes(_CORRUPT)
    assert write_explicit_advisory(str(path), status) is False
    assert path.read_bytes() == _CORRUPT
