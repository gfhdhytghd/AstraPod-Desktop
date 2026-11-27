"""Stage the Classic Photo Database and its RGB565/I420 thumbnail files.

libgpod uses /Photos/Photo Database and /Photos/Thumbs on Classic models.
Do not stage or overwrite original photos or unrelated files in /Photos.
"""

from contextlib import contextmanager
from ctypes import CDLL, POINTER, Structure, byref, c_char_p, c_int, c_void_p, cast
from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
from struct import unpack_from

from hrstockdb import StockDatabaseError, _ErrorPointer, _ErrorOut, _List, _entries
from hrstockdb import _library, _NATIVE_IO_LOCK, _native_call, _text
from hrstockdevice import checked_path

PHOTO_FORMATS = {
    ".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".heic": "heic", ".heif": "heic",
    ".tiff": "tiff", ".tif": "tiff", ".bmp": "bmp", ".webp": "webp", ".gif": "gif",
}
PHOTO_DB = Path("Photos/Photo Database")
THUMBS = Path("Photos/Thumbs")
THUMB_NAME = re.compile(r"F[0-9]+_[0-9]+\.ithmb", re.IGNORECASE | re.ASCII)


def snapshot(root):
    files = {}
    database = checked_path(root, *PHOTO_DB.parts)
    if database:
        files[PHOTO_DB] = database
    thumbs = checked_path(root, *THUMBS.parts, directory=True)
    if thumbs:
        for path in thumbs.iterdir():
            if THUMB_NAME.fullmatch(path.name):
                name = path.name[0].upper() + path.name[1:].lower()
                files[THUMBS / name] = checked_path(thumbs, name, required=True)
    result = {}
    for relative, path in files.items():
        info = path.stat(follow_symlinks=False)
        result[relative] = (path, (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return result


def normalize(source, destination):
    """Decode the first frame, apply EXIF orientation and prepare a bounded image."""
    fmt = PHOTO_FORMATS[source.suffix.lower()]
    with source.open("rb") as stream:
        try:
            subprocess.run(
                ["magick", "-limit", "area", "100MP", "-limit", "disk", "512MiB",
                 f"{fmt}:-[0]", "-auto-orient", "-thumbnail", "720x720>",
                 "-colorspace", "sRGB", "-background", "black", "-alpha", "remove",
                 "-quality", "94", str(destination)],
                stdin=stream, capture_output=True, text=True, check=True, timeout=90)
        except FileNotFoundError as error:
            raise RuntimeError("ImageMagick (magick) is required for stock photo import") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() or "ImageMagick could not decode the photo"
            raise ValueError(detail) from error
        except subprocess.TimeoutExpired as error:
            raise ValueError("ImageMagick timed out decoding the photo") from error


class _PhotoDB(Structure):
    _fields_ = [("photos", _List), ("albums", _List), ("device", c_void_p)]


class _PhotoAlbum(Structure):
    _fields_ = [("db", c_void_p), ("name", c_char_p), ("members", _List)]


_PhotoDBPointer = POINTER(_PhotoDB)


@lru_cache(maxsize=1)
def _photo_library():
    library: CDLL = _library()
    for name, result, *args in (
        ("itdb_photodb_create", _PhotoDBPointer, c_char_p),
        ("itdb_photodb_parse", _PhotoDBPointer, c_char_p, _ErrorOut),
        ("itdb_photodb_free", None, _PhotoDBPointer),
        ("itdb_photodb_write", c_int, _PhotoDBPointer, _ErrorOut),
        ("itdb_photodb_add_photo", c_void_p, _PhotoDBPointer, c_char_p, c_int, c_int, _ErrorOut),
        ("itdb_artwork_get_pixbuf", c_void_p, c_void_p, c_void_p, c_int, c_int),
        ("g_object_unref", None, c_void_p),
        ("itdb_photodb_photoalbum_create", c_void_p, _PhotoDBPointer, c_char_p, c_int),
        ("itdb_photodb_photoalbum_add_photo", None, _PhotoDBPointer, c_void_p, c_void_p, c_int),
        ("itdb_device_set_sysinfo", None, c_void_p, c_char_p, c_char_p),
        ("itdb_device_supports_photo", c_int, c_void_p),
    ):
        function = getattr(library, name)
        function.restype = result
        function.argtypes = args
    return library


@contextmanager
def _open(stage, *, existing):
    library = _photo_library()
    error = _ErrorPointer()
    with _NATIVE_IO_LOCK:
        pointer = (library.itdb_photodb_parse(os.fsencode(stage), byref(error)) if existing
                   else library.itdb_photodb_create(os.fsencode(stage)))
    try:
        if error:
            raise StockDatabaseError(f"Could not read Photo Database: {_text(error.contents.message)}")
        if not pointer:
            raise StockDatabaseError("Could not open Photo Database")
        yield pointer
    finally:
        if error:
            library.g_error_free(error)
        if pointer:
            library.itdb_photodb_free(pointer)


def _albums(db):
    return [(address, cast(address, POINTER(_PhotoAlbum)).contents)
            for address in _entries(db.contents.albums)]


def _contents(db):
    photos = list(_entries(db.contents.photos))
    albums = tuple((album.name, sum(1 for _ in _entries(album.members)))
                   for _, album in _albums(db))
    return photos, albums


def _counts(stage):
    with _open(stage, existing=True) as db:
        photos, albums = _contents(db)
        return len(photos), len(albums)


def _section_counts(stage):
    """Check the root and each list count before trusting the native parser."""
    data = (stage / PHOTO_DB).read_bytes()
    if len(data) < 16 or data[:4] != b"mhfd":
        raise StockDatabaseError("Invalid Photo Database header")
    offset, end = unpack_from("<II", data, 4)
    if offset < 16 or end != len(data):
        raise StockDatabaseError("Invalid Photo Database length")
    counts = {}
    while offset + 16 <= end and data[offset:offset + 4] == b"mhsd":
        header, size = unpack_from("<II", data, offset + 4)
        if header < 16 or size < header + 12 or offset + size > end:
            break
        start = offset + header
        counts[data[start:start + 4]] = unpack_from("<I", data, start + 8)[0]
        offset += size
    if offset != end or not {b"mhli", b"mhla"} <= counts.keys():
        raise StockDatabaseError("Photo Database is missing the photo or album section")
    return counts[b"mhli"], counts[b"mhla"]


def write(stage, images):
    """Publish nothing until the native writer and a fresh parse agree."""
    library = _photo_library()
    path = stage / PHOTO_DB
    existing = path.exists()
    if existing and _section_counts(stage) != _counts(stage):
        raise StockDatabaseError("Could not read all photos or albums in the Photo Database")
    with _open(stage, existing=existing) as pointer:
        device = pointer.contents.device
        # All Classic revisions use this fixed profile. The isolated stage
        # keeps mobile-device SysInfo format overrides out of the photo writer.
        library.itdb_device_set_sysinfo(device, b"ModelNumStr", b"MB029")
        if not library.itdb_device_supports_photo(device):
            raise StockDatabaseError("libgpod does not recognize this iPod Classic photo profile")
        photos_before, _ = _contents(pointer)
        albums = {album.name: address for address, album in _albums(pointer)[1:]}
        for image, album_name in images:
            photo = _native_call("libgpod could not add photo", library.itdb_photodb_add_photo,
                                 pointer, os.fsencode(image), -1, 0)
            name = album_name.encode("utf-8")
            album = albums.get(name)
            if not album:
                album = library.itdb_photodb_photoalbum_create(pointer, name, -1)
                if not album:
                    raise StockDatabaseError("libgpod could not create photo album")
                albums[name] = album
            library.itdb_photodb_photoalbum_add_photo(pointer, album, photo, -1)
        expected_photos, expected_albums = _contents(pointer)
        _native_call("libgpod could not write Photo Database", library.itdb_photodb_write, pointer)
    if _section_counts(stage) != (len(expected_photos), len(expected_albums)):
        raise StockDatabaseError("Photo Database is missing photos or albums")
    with _open(stage, existing=True) as db:
        photos, albums = _contents(db)
        if len(photos) != len(expected_photos) or albums != expected_albums:
            raise StockDatabaseError("Could not read the newly written Photo Database")
        for photo in photos[len(photos_before):]:
            for width, height in ((64, 64), (320, 240), (720, 480)):
                thumbnail = library.itdb_artwork_get_pixbuf(
                    db.contents.device, photo, width, height)
                if not thumbnail:
                    raise StockDatabaseError("Could not read a staged photo thumbnail")
                library.g_object_unref(thumbnail)
    return snapshot(stage)
