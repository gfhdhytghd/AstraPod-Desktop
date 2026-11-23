# Stock iPod acceptance

The stock transport lives in `herdremote/hrstock.py`. See its module docstring
for the mounted-volume dispatch contract. The transport runs on Linux and
uses `libgpod.so.4`, `ffmpeg`, and `ffprobe`. On Arch, the `libgpod` package
also brings in `sg3_utils` for the read-only SCSI fallback.

This checklist exercises the transport directly. The transport supports iPod Classic 6G, 6.5G, and 7G. Other stock
iPods raise `UnsupportedStockModel`. Repeat the checks on each supported
generation. Use a FAT iPod with a stock iTunes library and an existing playlist.
Disconnect any PodBox test iPod before starting.

1. Connect the stock iPod. Mount its FAT volume in the desktop file manager.
   Confirm the mount contains `iPod_Control/iTunes/iTunesDB`. On the desktop
   device page, check for the Classic main menu and the album cover slideshow.
   With animation disabled in Settings, the cover stays still.
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
