"""Prepare H.264 video and iTunesDB metadata for the iPod Classic."""

from fractions import Fraction
import json
from pathlib import Path

from hrstockmedia import StockMedia, _extract_cover, _ffmpeg, _is_picture, _milliseconds
from hrstockmedia import _number, FAT_FILE_LIMIT, SAMPLE_RATES
from podlink import media_command

VIDEO_SUFFIXES = frozenset({".m4v", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".mpg", ".mpeg"})
VIDEO_TYPES = {"movie": 2, "music-video": 32, "tv-show": 64}


def _probe(path):
    data = json.loads(media_command(
        "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-show_streams",
        "-show_format", "-of", "json", "-i", str(path)))
    streams = data.get("streams", [])
    video = next((s for s in streams if s["codec_type"] == "video" and not _is_picture(s)), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    return data["format"], streams, video, audio


def _fps(stream, field):
    try:
        return Fraction(stream.get(field, "0"))
    except (ValueError, TypeError, ZeroDivisionError):
        return 0


def _compatible(container, streams, video, audio):
    return (
        "mp4" in container.get("format_name", "").split(",")
        and video and audio and len(streams) == 2
        and video.get("codec_name") == "h264" and video.get("codec_tag_string") == "avc1"
        and video.get("profile") in ("Constrained Baseline", "Baseline")
        and video.get("pix_fmt") == "yuv420p" and 0 < video.get("level", 0) <= 30
        and 0 < video.get("width", 0) <= 640 and 0 < video.get("height", 0) <= 480
        and 0 < _fps(video, "avg_frame_rate") <= 30 and _fps(video, "r_frame_rate") <= 30
        and 0 < _number(video.get("bit_rate")) <= 1_500_000
        and video.get("field_order") in (None, "unknown", "progressive")
        and all(not s.get("rotation") for s in video.get("side_data_list", ()))
        and audio.get("codec_name") == "aac" and audio.get("codec_tag_string") == "mp4a"
        and audio.get("profile") == "LC" and 0 < _number(audio.get("bit_rate")) <= 160_000
        and audio.get("channels") in (1, 2) and _number(audio.get("sample_rate")) in SAMPLE_RATES
    )


def prepare_video(path, directory, *, kind=None):
    path = Path(path).resolve(strict=True)
    container, streams, video, audio = _probe(path)
    if video is None:
        raise ValueError("no moving video stream")
    tags = {key.lower(): value for key, value in container.get("tags", {}).items()}
    if kind is None:
        kind = ("tv-show" if tags.get("show") or tags.get("media_type") == "10"
                else "music-video" if tags.get("media_type") == "6" else "movie")
    if kind not in VIDEO_TYPES:
        raise ValueError(f"unknown iPod video category: {kind}")

    output = directory / "video.m4v"
    source_rate = _number(audio.get("sample_rate")) if audio else 0
    command = _ffmpeg(path)
    maps = ("-map", f"0:{video['index']}")
    if audio:
        maps += ("-map", f"0:{audio['index']}")
    else:
        command += ("-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100")
        maps += ("-map", "1:a", "-shortest")
    mux = ("-map_metadata", "0", "-map_chapters", "-1", "-movflags", "+faststart",
           "-f", "ipod", str(output))
    if _compatible(container, streams, video, audio):
        media_command(*command, *maps, "-c", "copy", *mux)
        media_command(*_ffmpeg(output), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-")
    else:
        fps = max(1, min(_fps(video, "avg_frame_rate") or 30, 30))
        filters = (
            "scale=w='min(640,iw)':h='min(480,ih)':force_original_aspect_ratio=decrease:"
            "force_divisible_by=2:reset_sar=1"
        )
        if video.get("field_order") not in (None, "unknown", "progressive"):
            filters = "bwdif=mode=send_frame," + filters
        audio_options = ("-c:a", "aac", "-profile:a", "aac_low", "-ar", "44100",
                         "-ac", "2", "-b:a", "128k")
        video_options = ("-vf", f"{filters},fps={fps}", "-pix_fmt", "yuv420p",
                         "-profile:v", "baseline", "-level:v", "3.0", "-bf", "0",
                         "-refs", "1", "-g", str(round(fps * 2)), "-b:v", "1200k",
                         "-maxrate", "1450k", "-bufsize", "1450k")
        for encoder in ("h264_nvenc", "libx264"):
            try:
                media_command(*command, *maps, "-c:v", encoder, *video_options, *audio_options, *mux)
                break
            except RuntimeError:
                output.unlink(missing_ok=True)
                if encoder == "libx264":
                    raise

    final, final_streams, final_video, final_audio = _probe(output)
    if not _compatible(final, final_streams, final_video, final_audio):
        raise ValueError("converted video does not match the iPod Classic playback profile")
    length = _milliseconds(final_video.get("duration"), final.get("duration"))
    size = output.stat().st_size
    if not 0 < size <= FAT_FILE_LIMIT:
        raise ValueError("prepared video exceeds the FAT file size range")
    # libgpod can use a tagged cover, or the desktop can import the video without one.
    artwork = _extract_cover(path, streams, directory)
    return StockMedia(
        source=path, upload=output, suffix=".m4v", title=tags.get("title") or path.stem,
        artist=tags.get("artist", ""), album=tags.get("album", ""),
        album_artist=tags.get("album_artist", ""), genre=tags.get("genre", ""),
        composer=tags.get("composer", ""), grouping="", comment=tags.get("comment", ""),
        year=0, disc=0, track=0, length_ms=length, bitrate_kbps=round(size * 8 / length),
        sample_rate=_number(final_audio["sample_rate"]), source_rate=source_rate,
        channels=final_audio["channels"], size_bytes=size, codec="h264",
        filetype="MPEG-4 video file", artwork=artwork, media_type=VIDEO_TYPES[kind],
        show=(tags.get("show") or tags.get("album") or path.parent.name) if kind == "tv-show" else "",
        episode=tags.get("episode_id", ""), season=_number(tags.get("season_number")),
        episode_number=_number(tags.get("episode_sort")),
    )
