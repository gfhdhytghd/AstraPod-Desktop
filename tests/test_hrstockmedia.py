"""Host audio fixtures for the stock iPod playback profile."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "herdremote")]

import hrstockmedia


pytestmark = pytest.mark.skipif(
    not all(shutil.which(program) for program in ("ffmpeg", "ffprobe")),
    reason="ffmpeg and ffprobe are required for audio fixtures",
)


def ffmpeg(*args):
    return subprocess.run(("ffmpeg", "-nostdin", "-v", "error", *map(str, args)),
                          check=True, capture_output=True).stdout


def audio_stream(path):
    output = subprocess.run(
        ("ffprobe", "-v", "error", "-select_streams", "a:0", "-show_streams",
         "-of", "json", str(path)), check=True, capture_output=True).stdout
    return json.loads(output)["streams"][0]


def test_flac_resample_downmix_metadata_and_artwork(tmp_path):
    cover = tmp_path / "original.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=800x600", "-frames:v", 1, "-update", 1, cover)
    flac = tmp_path / "Source.flac"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=96000:cl=5.1", "-i", cover,
           "-map", "0:a", "-map", "1:v", "-t", "0.5", "-c:a", "flac", "-sample_fmt", "s32",
           "-c:v", "copy", "-disposition:v", "attached_pic",
           "-metadata", "title=测试曲", "-metadata", "artist=Artist", "-metadata", "album=Album",
           "-metadata", "album_artist=Various Artists", "-metadata", "composer=Composer",
           "-metadata", "disc=2/3", "-metadata", "track=5/12", "-metadata", "date=2020", flac)
    media = hrstockmedia.prepare_media(flac, tmp_path)
    assert media.source == flac
    assert media.upload != flac
    assert media.suffix == ".m4a"
    assert media.codec == "alac"
    assert media.length_ms == 500
    assert media.bitrate_kbps > 0
    assert media.size_bytes == media.upload.stat().st_size
    assert (media.title, media.artist, media.album) == ("测试曲", "Artist", "Album")
    assert (media.album_artist, media.composer) == ("Various Artists", "Composer")
    assert (media.year, media.disc, media.track) == (2020, 2, 5)
    assert (media.sample_rate, media.channels) == (48000, 2)
    result_stream = audio_stream(media.upload)
    assert result_stream["codec_name"] == "alac"
    assert result_stream["sample_fmt"] in ("s16", "s16p")
    assert (result_stream["sample_rate"], result_stream["channels"]) == ("48000", 2)
    assert media.artwork is not None
    assert media.artwork.read_bytes().startswith(b"\xff\xd8")
    tags = hrstockmedia.probe_music_tags(flac)
    assert (tags.title, tags.album, tags.milliseconds, tags.disc, tags.number) == (
        "测试曲", "album", 500, 2, 5)


def test_flac_conversion_preserves_compatible_pcm_samples(tmp_path):
    path = tmp_path / "source.flac"
    ffmpeg("-f", "lavfi", "-i", "sine=sample_rate=44100", "-t", "0.5", "-c:a", "flac", path)
    media = hrstockmedia.prepare_media(path, tmp_path)
    assert (media.codec, media.sample_rate, media.channels) == ("alac", 44100, 1)
    decode = ("-map", "0:a:0", "-f", "s16le", "-")
    assert ffmpeg("-i", media.upload, *decode) == ffmpeg("-i", path, *decode)


@pytest.mark.parametrize(("suffix", "codec", "encoding"), [
    (".mp3", "mp3", ("-c:a", "libmp3lame")),
    (".m4a", "aac", ("-c:a", "aac", "-profile:a", "aac_low")),
    (".m4a", "alac", ("-c:a", "alac", "-sample_fmt", "s16p")),
    (".wav", "pcm_s16le", ("-c:a", "pcm_s16le")),
])
def test_passes_through_compatible_audio(tmp_path, suffix, codec, encoding):
    path = tmp_path / f"source{suffix}"
    ffmpeg("-f", "lavfi", "-i", "sine=sample_rate=44100", "-t", "0.4", *encoding, path)
    media = hrstockmedia.prepare_media(path, tmp_path)
    assert (media.source, media.codec, media.suffix) == (path, codec, suffix)
    assert media.upload != path
    assert media.upload.read_bytes() == path.read_bytes()
    assert media.title == "source"
    assert media.artwork is None


def test_jellyfin_download_normalizes_unknown_codec_and_title(tmp_path):
    opus = tmp_path / "source.opus"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "0.4",
           "-c:a", "libopus", opus)
    track = SimpleNamespace(label="Server title", suffix="../../unsafe")

    class Source:
        def download(self, requested, path):
            assert requested is track
            assert path.name == "source.audio"
            shutil.copyfile(opus, path)

    media = hrstockmedia.prepare_track(Source(), track, tmp_path)
    assert (media.codec, media.suffix, media.title) == ("aac", ".m4a", "Server title")
    assert media.source.is_relative_to(tmp_path)
    assert audio_stream(media.upload)["profile"] == "LC"


def test_high_rate_wav_converts_to_compatible_pcm(tmp_path):
    path = tmp_path / "source.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=sample_rate=88200", "-t", "0.4",
           "-c:a", "pcm_f32le", path)
    media = hrstockmedia.prepare_media(path, tmp_path)
    assert media.upload != path
    assert (media.codec, media.sample_rate, media.suffix) == ("pcm_s16le", 44100, ".wav")
    assert audio_stream(media.upload)["sample_fmt"] == "s16"


def test_rf64_wav_remuxes_without_changing_pcm(tmp_path):
    path = tmp_path / "source.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=sample_rate=44100", "-t", "0.4",
           "-c:a", "pcm_s16le", "-rf64", "always", path)
    media = hrstockmedia.prepare_media(path, tmp_path)
    assert media.upload != path
    assert media.upload.read_bytes()[:4] == b"RIFF"
    decode = ("-map", "0:a:0", "-f", "s16le", "-")
    assert ffmpeg("-i", media.upload, *decode) == ffmpeg("-i", path, *decode)


def test_invalid_input_fails_before_publishing(tmp_path):
    invalid = tmp_path / "broken.mp3"
    invalid.write_bytes(b"Invalid audio")
    with pytest.raises(RuntimeError, match=r"broken\.mp3: ffprobe:"):
        hrstockmedia.prepare_media(invalid, tmp_path)
    assert sorted(tmp_path.iterdir()) == [invalid]
