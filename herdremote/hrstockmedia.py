"""Prepare local audio for the iPod Classic stock firmware.

`prepare_media(path, directory)` accepts an audio file. `prepare_track(source,
track, directory)` downloads a Jellyfin track first. Both return the same
`StockMedia` metadata and keep generated files inside the caller's temporary
directory. The caller owns the directory and publishes `upload` using
`suffix`. Optional `artwork` is a separate JPEG for the iPod artwork database.
"""

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
from tempfile import mkdtemp

from hrmediasync import music_tags
from podlink import media_command, media_sample_rate


# Use the common playback formats across both device families. The ALAC/WAV
# profile uses 16-bit samples for older decoders, even for high-resolution FLAC.
SAMPLE_RATES = {32000, 44100, 48000}
FAT_FILE_LIMIT = 0xFFFFFFFF
MAX_ART_PIXELS = 16 * 1024 * 1024
MAX_ART_BYTES = 2 * 1024 * 1024

FORMATS = {
    "mp3": ("mp3", ".mp3", "MPEG audio file", ("-c:a", "libmp3lame", "-q:a", "2")),
    "aac": ("m4a", ".m4a", "AAC audio file",
            ("-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k")),
    "alac": ("m4a", ".m4a", "Apple Lossless audio file",
             ("-c:a", "alac", "-af", "aresample=osf=s16p:dither_method=triangular")),
    "pcm_s16le": ("wav", ".wav", "WAV audio file",
                  ("-c:a", "pcm_s16le", "-af", "aresample=osf=s16:dither_method=triangular")),
}
LOSSLESS = {"alac", "ape", "flac", "shorten", "tta", "wavpack"}
M4A_FORMAT = "m4a"


@dataclass(frozen=True)
class StockMedia:
    source: Path
    upload: Path
    suffix: str
    title: str
    artist: str
    album: str
    album_artist: str
    genre: str
    composer: str
    grouping: str
    comment: str
    year: int
    disc: int
    track: int
    length_ms: int
    bitrate_kbps: int
    sample_rate: int
    source_rate: int
    channels: int
    size_bytes: int
    codec: str
    filetype: str
    artwork: Path | None


def _number(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _tag_number(tags, key):
    number = _number(tags.get(key, "").partition("/")[0])
    return number if number <= 0x7FFFFFFF else 0


def _milliseconds(*durations):
    for duration in durations:
        try:
            value = round(float(duration) * 1000)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 < value <= 0x7FFFFFFF:
            return value
    raise ValueError("no audio duration within the iTunesDB range")


@dataclass(frozen=True)
class _Probe:
    audio: dict
    streams: tuple
    formats: frozenset
    tags: dict
    length_ms: int

    @property
    def codec(self):
        return self.audio["codec_name"]

    @property
    def sample_rate(self):
        return _number(self.audio.get("sample_rate"))

    @property
    def channels(self):
        return _number(self.audio.get("channels"))

    @property
    def bitrate(self):
        return _number(self.audio.get("bit_rate"))


def _probe(path):
    if not path.is_file():
        raise ValueError("expected a local audio file")
    probe = json.loads(media_command(
        "ffprobe", "-v", "error", "-protocol_whitelist", "file",
        "-show_streams", "-show_format", "-of", "json", "-i", str(path)))
    streams = tuple(probe.get("streams", ()))
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not audio or not audio.get("codec_name"):
        raise ValueError("no audio stream")
    container = probe.get("format", {})
    tags = {key.casefold(): value.replace("\0", " ").strip()
            for section in (container, audio)
            for key, value in section.get("tags", {}).items() if isinstance(value, str)}
    result = _Probe(audio, streams, frozenset(container.get("format_name", "").split(",")),
                    tags, _milliseconds(audio.get("duration"), container.get("duration")))
    if not result.sample_rate or not result.channels:
        raise ValueError("no audio stream with a valid sample rate and channel count")
    return result


def probe_music_tags(path):
    """Return the same duplicate-match keys used by the existing import path."""
    probe = _probe(Path(path).resolve())
    tags = probe.tags
    return music_tags(tags.get("title"), tags.get("artist", "").split(";"),
                      tags.get("album"), probe.length_ms,
                      _tag_number(tags, "disc"), _tag_number(tags, "track"))


def _profile_ok(probe, codec):
    if (probe.codec != codec or probe.sample_rate not in SAMPLE_RATES
            or probe.channels not in (1, 2)):
        return False
    if codec == "aac":
        return (probe.audio.get("profile") == "LC"
                and 0 < probe.bitrate <= 320000
                and probe.audio.get("codec_tag_string") != "enca")
    if codec == "mp3":
        return 0 < probe.bitrate <= 320000
    if codec == "alac":
        return probe.audio.get("sample_fmt") in ("s16", "s16p")
    return True


def _target_codec(probe):
    if probe.codec in FORMATS:
        return probe.codec
    if "wav" in probe.formats:
        return "pcm_s16le"
    if probe.codec in LOSSLESS or probe.codec.startswith("pcm_"):
        return "alac"
    return "aac"


def _is_picture(stream):
    return (stream.get("codec_type") == "video"
            and stream.get("disposition", {}).get("attached_pic") == 1)


def _copy_as_is(path, probe, muxer, suffix):
    if path.suffix.lower() != suffix or muxer not in probe.formats:
        return False
    if muxer == M4A_FORMAT and probe.tags.get("major_brand", "").strip() != "M4A":
        return False
    if muxer == "wav":
        # The WAV demuxer also accepts RF64 and BW64. Publish a standard RIFF
        # container for the older firmware.
        with path.open("rb") as source:
            header = source.read(12)
        if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return False
    return all(stream == probe.audio or
               (_is_picture(stream) and stream.get("codec_name") in {"mjpeg", "png"})
               for stream in probe.streams)


def _ffmpeg(path):
    return ("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-n",
            "-protocol_whitelist", "file", "-i", str(path))


def _extract_cover(path, probe, directory):
    for stream in probe.streams:
        if not _is_picture(stream):
            continue
        width, height = _number(stream.get("width")), _number(stream.get("height"))
        if not 0 < width * height <= MAX_ART_PIXELS:
            continue
        picture = directory / "cover.jpg"
        try:
            bounds = f"{min(width, 600)}:{min(height, 600)}"
            media_command(
                *_ffmpeg(path), "-map", f"0:{stream['index']}", "-frames:v", "1",
                "-vf", f"scale={bounds}:force_original_aspect_ratio=decrease",
                "-c:v", "mjpeg", "-q:v", "3", "-update", "1", str(picture))
            if 0 < picture.stat().st_size <= MAX_ART_BYTES:
                return picture
        except (OSError, RuntimeError):
            pass
        picture.unlink(missing_ok=True)
    return None


def prepare_media(path, directory, *, fallback_title=None):
    """Probe, normalize and verify audio in a caller-owned staging directory."""
    path = Path(path).resolve()
    try:
        original = _probe(path)
        codec = _target_codec(original)
        muxer, suffix, filetype, encoder = FORMATS[codec]
        profile_ok = _profile_ok(original, codec)
        work = Path(mkdtemp(prefix="stock-audio-", dir=directory))
        upload, prepared = path, original
        if not (profile_ok and _copy_as_is(path, original, muxer, suffix)):
            upload = work / f"audio{suffix}"
            rate = media_sample_rate(original.sample_rate)
            if rate not in SAMPLE_RATES:
                rate = 44100
            if profile_ok:
                encoding = ("-c:a", "copy")
            else:
                encoding = (*encoder, "-ar", str(rate), "-ac", str(min(original.channels, 2)))
            mux_options = (("-movflags", "+faststart", "-f", "ipod")
                           if muxer == M4A_FORMAT else ("-f", muxer))
            media_command(*_ffmpeg(path), "-map", "0:a:0", "-map_metadata", "0",
                          "-map_chapters", "-1", *encoding, *mux_options, str(upload))
            prepared = _probe(upload)
            if muxer not in prepared.formats or not _profile_ok(prepared, codec):
                raise ValueError("converted audio does not match the stock iPod playback profile")
        if upload == path:
            upload = work / f"audio{suffix}"
            shutil.copyfile(path, upload)
            prepared = _probe(upload)
            if not _profile_ok(prepared, codec):
                raise ValueError("audio changed while preparing the stock file")
        if profile_ok:
            # ffprobe can read a header even when the audio cannot be decoded.
            media_command(*_ffmpeg(upload), "-map", "0:a:0", "-f", "null", "-")

        size = upload.stat().st_size
        if not 0 < size <= FAT_FILE_LIMIT:
            raise ValueError("prepared audio exceeds the FAT file size range")
        tags = original.tags
        date = re.match(r"\d{4}", tags.get("date") or tags.get("year") or "")
        bitrate = prepared.bitrate or round(size * 8000 / prepared.length_ms)
        return StockMedia(
            source=path, upload=upload, suffix=suffix,
            title=tags.get("title") or fallback_title or path.stem,
            artist=tags.get("artist", ""), album=tags.get("album", ""),
            album_artist=tags.get("album_artist") or tags.get("albumartist", ""),
            genre=tags.get("genre", ""), composer=tags.get("composer", ""),
            grouping=tags.get("grouping") or tags.get("tit1", ""),
            comment=tags.get("comment", ""), year=int(date[0]) if date else 0,
            disc=_tag_number(tags, "disc"), track=_tag_number(tags, "track"),
            length_ms=prepared.length_ms, bitrate_kbps=round(bitrate / 1000),
            sample_rate=prepared.sample_rate, source_rate=original.sample_rate,
            channels=prepared.channels,
            size_bytes=size, codec=codec, filetype=filetype,
            artwork=_extract_cover(path, original, work))
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(f"{path}: {error}") from error


def prepare_track(source, track, directory):
    """Download one Jellyfin source track and prepare it without PodLink."""
    work = Path(mkdtemp(prefix="stock-jellyfin-", dir=directory))
    suffix = track.suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".audio"
    path = work / f"source{suffix}"
    source.download(track, path)
    return prepare_media(path, work, fallback_title=track.label)
