"""Display formatting for durations, sizes, dates and counts (Chinese UI)."""
from __future__ import annotations

import time


def duration(ms: int) -> str:
    seconds = max(0, int(ms) // 1000)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def long_duration(ms: int) -> str:
    minutes = max(0, int(ms) // 60000)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    days, hours = divmod(hours, 24)
    return f"{days} 天 {hours} 小时"


def size(num: int) -> str:
    value = float(max(0, num))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            break
        value /= 1000
    else:
        unit = "TB"
    if value >= 1000 and unit == "GB":
        return f"{value / 1000:.2f} TB"
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"


def date(seconds: int) -> str:
    if not seconds:
        return ""
    return time.strftime("%Y/%m/%d %H:%M", time.localtime(seconds))


def until(timestamp: float | None, now: float | None = None) -> str:
    """"2 小时 5 分钟后重置" style text for a reset time in unix seconds."""
    if not timestamp:
        return "重置时间未知"
    remaining = int(timestamp - (time.time() if now is None else now))
    if remaining <= 0:
        return "即将重置"
    minutes = remaining // 60
    if minutes < 60:
        return f"{max(1, minutes)} 分钟后重置"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} 小时 {minutes} 分钟后重置"
    return f"{hours // 24} 天 {hours % 24} 小时后重置"


def songs(count: int) -> str:
    return f"{count:,} 首歌曲"


def kind(path: str, codec: str = "") -> str:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    names = {
        "mp3": "MPEG 音频文件", "m4a": "AAC 音频文件", "flac": "FLAC 音频文件",
        "ogg": "Ogg Vorbis 文件", "opus": "Opus 音频文件", "wav": "WAV 音频文件",
        "aiff": "AIFF 音频文件", "ape": "Monkey's Audio 文件",
        "wv": "WavPack 文件", "wma": "WMA 音频文件",
    }
    if codec.lower() == "alac" and suffix == "m4a":
        return "Apple 无损音频文件"
    return names.get(suffix, f"{suffix.upper()} 文件" if suffix else "音频文件")


def estimated_bytes(length_ms: int, bitrate_kbps: int) -> int:
    return int(length_ms) * int(bitrate_kbps) // 8
