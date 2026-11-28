# AstraPod Desktop

AstraPod Desktop is the Linux companion for [AstraPod iPod Classic firmware][firmware].
The desktop app browses the iPod library, imports music, and syncs Jellyfin media.
HerdRemote owns the PodLink USB session and connects the iPod's Agents screen,
remote music and input plugins to the computer. This repository also includes
the PodLink CLI and the Apple IR bridge. The Cider plugin that HerdRemote uses
by default lives in [AstraPod-Cider][cider].

## Install

Use Python 3.10 or later and a Linux desktop session. The GUI uses Qt 6.
Grant your user access to the player's serial port. Install libusb for PodLink
Shell and Apple IR, and FFmpeg (including `ffprobe`) for media import and
sync. [HerdRemote's guide][herdremote] covers herdr, T3, voice and keyboard tools.

```sh
git clone https://github.com/gfhdhytghd/AstraPod-Desktop.git
cd AstraPod-Desktop
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Run

From the checkout root:

```sh
.venv/bin/python podlink_gui.py           # --tray for the tray, --demo for fake data
.venv/bin/python herdremote/herdremote.py # headless alternative to the GUI
.venv/bin/python -m astrapod_plugins list
.venv/bin/python -m astrapod_plugins run  # standalone plugin host
```

Connect the iPod in PodLink (non-disk) mode. Open **System > Agents** on the
iPod for HerdRemote. The GUI supervises its own HerdRemote process. Use one
HerdRemote process or other PodLink serial client at a time. Run the standalone
plugin host with `ASTRAPOD_PLUGIN_HOST=off` in the GUI's environment.

For serial diagnostics, stop HerdRemote and run `.venv/bin/python podlink.py info`.
PodLink Shell uses a separate USB endpoint and can run while HerdRemote is
connected. See the [Apple IR setup][ir] and the [Cider bridge setup][cider]
for those optional integrations.

On LCD type 1 firmware, `podlink_shell.py` supports `lcd-timing` and
`lcd-te-sync` without opening a device menu. `lcd-pacing on|off|get` switches
the UI animation clock between the panel refresh and 60 fps on any LCD, and
`lcd-pacing status` prints the refresh period and the last animation's rate,
frames and timings. Run `lcd-diag` to save a report, then use `log-fetch` to
download it. `lcd-diag survey` also samples every GPIO and LCD signal for one
second, and the player's screen stops for that second.
Use `ui-perf OUTPUT` to fetch an existing UI performance log.
See the [PodLink Shell guide][shell] for command arguments and paths.

For plugin launch, screen capture and device checks, see the
[iPod plugin smoke test](docs/plugin-smoke.md).

### Disk Mode for media transfers

Start with the iPod connected in PodLink mode. Enable **磁盘模式** on the
Jellyfin sync or direct import page to transfer through USB mass storage.
Jellyfin preview and the import duplicate check still use PodLink. The app
mounts the selected iPod's FAT volume with UDisks2, ejects it after the job,
then queues a Rockbox database update on the restored PodLink connection.
Keep `/Music/PodLink` inside the player's database scan roots. Disk Mode needs
`udisksctl`, `gdbus`, `lsblk` and HerdRemote from the same desktop checkout.

### Calendars, to-dos and contacts

Open **日历与联系人**. To try the workflow with a temporary player and a
sample calendar, start the desktop app with `--demo`.

- Under **日历与待办**, enter the CalDAV server URL, username and password or
  app password. Select **连接并列出日历** and check the calendars you want.
  The app saves the password through `secret-tool` (libsecret and an unlocked
  desktop Secret Service). Use the same CalDAV server address and account
  as in DankCalendar. Use HTTPS. If discovery redirects to a different
  host, enter the destination server URL directly.
- Use **导入 ICS** to bring one or more files into **本地日历**, which appears
  in the same list. Each file may contain multiple calendars, events and
  tasks. Select **编辑本地日历** to change an entry. The editor preserves
  timezone definitions, recurrence rules and excluded dates. To save a
  local or CalDAV calendar to a file, select its row and use **导出 ICS**.
- Turn on **磁盘模式** and select **同步日历与待办**. Choose where the app
  should send new iPod events and tasks. Items without a destination stay in
  `/Calendars/AstraPod.ics`. The app writes one `AstraPod-DAV-*.ics` per
  selected calendar and syncs edits and deletions in those files back to
  the server. For competing edits, the latest modification wins. A tie
  uses the server's version. The app keeps the losing edit as an `.ics`
  file and lists it in the conflict dialog. A competing deletion keeps
  the edited item and reports the conflict. The local calendar syncs in
  both directions through `/Calendars/AstraPod-Local.ics` and works
  without a CalDAV account. For local conflicts with equal timestamps,
  the app keeps the desktop version.
- Under **联系人**, use **导入 vCard** for one or more `.vcf` files. Select a
  contact and choose **编辑联系人** to update its fields. **同步联系人** reads
  changes from the iPod and writes new contacts to `/Contacts/AstraPod`.
  The app preserves embedded photos and keeps edited contacts in their
  source files. Missing contacts are kept in the desktop library.
- Turn off **磁盘模式** to eject the player when finished.

**打开冲突副本** opens the local folder that holds the saved versions. It also
contains the local contacts and calendar sync cache under
`$XDG_DATA_HOME/astrapod/extras` (default `~/.local/share/astrapod/extras`).
The sync uses the AstraPod firmware's
[Extras data format][extras-format] and the existing USB Disk Mode transport.

## User services

The supplied units expect the checkout and its `.venv` at
`~/.local/share/astrapod/desktop`. Clone or symlink the checkout there.
For another location, edit `WorkingDirectory` and `ExecStart` in the unit
you install under `~/.config/systemd/user/`.

| Unit | Use |
| --- | --- |
| [`herdremote.service`](herdremote/herdremote.service) | Standalone daemon for a headless setup. Use this when the GUI is absent. |
| [`astrapod-plugins.service`](astrapod_plugins/astrapod-plugins.service) | Standalone [input plugin host][plugins]. Use this with `ASTRAPOD_PLUGIN_HOST=off` in the GUI. |
| [`podbox-usbkey.service`](usb_remote_key/podbox-usbkey.service) | Standalone Apple IR bridge. Use this as an alternative to the `apple-ir` plugin. |

Copy the selected unit to `~/.config/systemd/user/`, run
`systemctl --user daemon-reload`, then `systemctl --user enable --now UNIT`.
For a desktop tray launcher, set `ExecStart` to the absolute path to
`.venv/bin/python` followed by the absolute path to `podlink_gui.py` and
`--tray`. Launch it from the graphical session.

## Tests

Install pytest in the venv. Use a short private runtime directory so socket
tests are isolated from the running daemon.

```sh
.venv/bin/python -m pip install pytest
export XDG_RUNTIME_DIR="$(mktemp -d -p /tmp)"
.venv/bin/python -m pytest -q herdremote/tests
.venv/bin/python -m pytest -q astrapod_plugins/tests usb_remote_key/tests tests/test_podlink.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/test_podlink_gui.py
```

Some HerdRemote tests use Node.js for the Cider peer and the system
`ffmpeg`/`ffprobe` executables for media fixtures. The Cider peer tests need
an AstraPod-Cider checkout: set `ASTRAPOD_CIDER_PLUGIN`, or place it at
`../AstraPod-Cider`. Without it they are skipped.

The [firmware contract probes](tests/firmware/README.md) require firmware
sources. Set `ASTRAPOD_FIRMWARE` to that checkout, or place it at
`../AstraPod` or `../podbox` relative to this repository.

## Protocols and license

The firmware repository documents [PodLink][podlink], [HerdRemote][protocol],
[PodLink Shell][shell] and the [Cider remote protocol][cider-protocol].
For host configuration, see [HerdRemote usage](docs/herdremote-usage.md)
and [IR host fallback](docs/ir-host-fallback.md).

AstraPod Desktop is licensed under GPL-2.0-or-later. See [LICENSE](LICENSE).

[firmware]: https://github.com/gfhdhytghd/AstraPod
[podlink]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podlink.md
[protocol]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/herdremote-protocol.md
[shell]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/podlink-shell.md
[cider-protocol]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/cider-remote-protocol.md
[herdremote]: herdremote/README.md
[ir]: usb_remote_key/README.md
[plugins]: docs/input-plugins.md
[cider]: https://github.com/gfhdhytghd/AstraPod-Cider
[extras-format]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/extras-data-format.md

### Rufin Cover Flow background cache

The desktop keeps the bounded source images and rendered XS, S, M and L
artwork. With compatible firmware, HerdRemote advertises `artbulk1`;
a connected idle iPod can request up to six XS, M or S covers per burst.
The host prepares three covers in parallel and packs fragments into
messages up to 16364 bytes. Foreground requests retain priority, and the
player confirms each cover only after it enters the local store. Older
firmware uses its existing per-cover store requests.
