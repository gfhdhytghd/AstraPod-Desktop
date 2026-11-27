# Stock iPod acceptance

The stock transport lives in `herdremote/hrstock.py`. See its module docstring
for the mounted-volume dispatch contract. The transport runs on Linux and
uses `libgpod.so.4`, `ffmpeg`, and `ffprobe`. On Arch, the `libgpod` package
also brings in `sg3_utils` for the read-only SCSI fallback.

Repeat the checks on an iPod Classic 6G, 6.5G, and 7G. Use a FAT iPod
with a stock iTunes library and an existing playlist. Disconnect other
iPods before starting. A dual-boot volume can keep its PodBox and Apple
files. Boot Apple firmware for these checks.

## Import photos and videos

With the iPod in stock firmware, open **直接导入** and add individual files
or a folder. Photos use their immediate parent folder as the album name.
Photo import uses ImageMagick 7 (`magick`). For HEIC and HEIF, ImageMagick
must include its HEIC decoder. Albums and screen-sized copies are stored in
`/Photos/Photo Database` and `/Photos/Thumbs`. The Classic thumbnail IDs are
1066 (64 × 64 RGB565), 1024 (320 × 240 RGB565), and 1067 (720 × 480 I420).

Import video into Movies, TV Shows, or Music Videos using the category
picker. Automatic mode reads the container's media type and show tags.
The import remuxes compatible video or converts it to H.264 Baseline Level
3.0 with AAC-LC. Conversion tries FFmpeg's NVIDIA encoder before libx264.
Compatible files and converted files both use `.m4v` under
`iPod_Control/Music/Fxx`. Embedded cover art takes priority. When the file
has no cover, import uses a frame from the prepared video.

The stock transaction publishes the video files, photo database and
thumbnails together with the library changes. The **跳过 iPod 上已有的歌曲**
switch applies to audio tracks. A second import of the same photo or video
creates another entry.

## Desktop app

1. Connect the stock iPod with its FAT volume unmounted. On the device page,
   check the model, firmware version, serial, capacity, free space, Classic
   main menu, and album covers. Disable animations in Settings and check
   that the displayed cover stays still. Compare the device library and
   playlists with the iPod's Music menu.
2. Mount the volume in the file manager and back up the entire
   `iPod_Control` directory to a fresh host directory. Safely unmount the
   volume before returning to AstraPod.
3. In **直接导入**, add four short tagged samples with distinct titles. Use
   MP3, AAC-LC in M4A, PCM WAV, and 96 kHz FLAC. Include an embedded cover in
   one sample. Check that the page explains USB disk transfer and hides the
   Disk Mode switch.
4. Import the samples. If polkit prompts for authorization, confirm the
   request. Wait for the page to confirm safe ejection. On the iPod, play
   each new song, seek near its end, and check the tags and cover. Play an
   original song from the existing playlist. Reconnect the iPod and verify
   that importing the same samples with duplicate skipping selected adds
   no songs.
5. In **Jellyfin 同步**, sign in to a test account and select a playlist with
   one short track absent from the iPod. Preview and check one addition
   and zero removals. Apply the preview and wait for safe ejection.
   Play the track on the iPod. Reconnect and verify that preview shows
   zero changes.
6. Remove the track from the Jellyfin playlist. Preview the same scope and
   check one removal and zero additions. Confirm removal and wait for safe
   ejection. Verify that the iPod still plays the original and locally
   imported tracks.

## Direct transport

1. Mount the iPod's FAT volume in the file manager. Confirm that the mount
   contains `iPod_Control/iTunes/iTunesDB` or `iPod_Control/iTunes/iTunesCDB`.
2. Back up the entire `iPod_Control` directory to a fresh host directory.
3. Prepare four short, tagged samples with different titles: MP3, AAC-LC in
   M4A, PCM WAV, and 96 kHz FLAC. Include an embedded cover in one sample.
4. In the repository root run `PYTHONPATH="$PWD/herdremote" python`. Enter the
   following with the mounted root and your sample paths.

   ```python
   from pathlib import Path
   import hrstock

   root = Path("/run/media/USER/IPOD")
   sources = ["/path/sample.mp3", "/path/sample.m4a",
              "/path/sample.wav", "/path/sample-96k.flac"]
   device = hrstock.detect(root)
   assert device is not None
   print(device.model, device.db_version, device.checksum, device.sqlite)
   before = hrstock.read_library(root)
   print(len(before.tracks), [playlist.name for playlist in before.playlists])
   result = hrstock.import_files(root, sources, print)
   assert len(result) == 4
   print(result)
   after = hrstock.read_library(root)
   assert len(after.tracks) == len(before.tracks) + 4
   assert hrstock.import_files(root, sources) == []
   ```

   If the write prompts for polkit authorization, confirm the read-only
   identity query. Check for `iPod_Control/iTunes/iTunesDB.astrapod-bak`.
   Check that the FLAC result reports an M4A path and 48,000 Hz.
5. Safely eject the iPod. Open Music on the iPod. Play each new song and
   seek near its end. Check the tags and cover. Play an original track
   from the existing playlist.
6. Use a test Jellyfin account with no previously synced copies on this
   iPod. Create a playlist with one short track absent from the iPod.
   Mount the iPod again. Substitute your server URL and username below.
   Pick the actual playlist scope from the displayed list.

   ```python
   from getpass import getpass
   from hrmediasync import JellyfinSource

   source = JellyfinSource.with_password(
       "https://jellyfin.example.com", "username", getpass())
   scopes = source.scopes()
   for index, scope in enumerate(scopes):
       print(index, scope.kind, scope.label)

   scope = scopes[int(input("Test playlist number: "))]
   plan = hrstock.preview(root, source, scope)
   assert len(plan.additions) == 1 and not plan.removals
   assert hrstock.apply(root, source, plan) == (1, 0)
   assert hrstock.apply(root, source, hrstock.preview(root, source, scope)) == (0, 0)
   ```

7. Safely eject. Play the Jellyfin track on the iPod. Remove that track from
   the same Jellyfin playlist and mount the iPod again.
8. Run the following with the same `source`, `scope`, and `root`.

   ```python
   plan = hrstock.preview(root, source, scope)
   assert not plan.additions and len(plan.removals) == 1
   assert hrstock.apply(root, source, plan) == (0, 1)
   ```

9. Safely eject. Check that Music no longer lists the Jellyfin track.
   Play an original track and a locally imported track again.

If an operation reports an interrupted write, keep the stock volume mounted and
run `hrstock.recover(root)` before reopening the library. An interrupted
operation may require the original mount to be reconnected first.

## Import the Classic preview font

Run this from the AstraPod Desktop checkout with the application's Python
environment and 7-Zip (`7zz` or `7z`) installed:

```sh
python -m astrapod_desktop.stock_font /path/to/iPod_35.2.0.4.ipsw
```

Use a local Apple iPod Classic restore image. The importer reads the firmware
resource volume from the IPSW and verifies the SHA-256 of `HelveticaBold.ttf`
before copying it to the user's private cache
(`${XDG_CACHE_HOME:-~/.cache}/astrapod/stock-classic/`). Device access is not
required. Restart AstraPod to load the font. Without the cached font, the
preview uses Liberation Sans Bold when installed, or Qt's sans-serif fallback.

The 11 Classic restore images checked across 6G (1.0.1 through 1.1.2),
6.5G (2.0 and 2.0.1), and 7G (2.0.2 through 2.0.5) contain byte-identical
Helvetica Regular and Helvetica Bold fonts. Apple returned HTTP 403 for
6G 1.0 and 1.0.2.
Both are TrueType outline fonts. The preview uses Bold at 14 LCD pixels for
the title and 16 for the menu, based on glyph measurements from the
[firmware framebuffer captures][captures]. The rounded pixel sizes are
estimates. The preview draws its own chrome and overlays the album slideshow.

[captures]: https://github.com/davidmonterocrespo24/qemu-ipod-classic/tree/1c33be87c7e97b56053142ae9d7f0686feaed504/docs/screenshots
