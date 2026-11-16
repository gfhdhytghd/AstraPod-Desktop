# Apple Remote Menu and volume over USB

This Linux bridge reads `EDGE <microseconds> <level>` from the IR receiver
with serial `2543030725032a2a`. It forwards Apple (`ee 87`) Menu commands
`02` and `03`, Vol+ `0b` and Vol- `0d`; the fourth byte may contain any
remote pairing ID. Left/Right/Play continue through the Dock unchanged. It never writes to or flashes the IR
receiver. Stop any other reader of that receiver before starting this bridge.

Install on the computer connected to the dock:

```sh
python3 -m venv ~/.local/share/podbox-usbkey/venv
~/.local/share/podbox-usbkey/venv/bin/pip install -r tools/usb_remote_key/requirements.txt
sudo cp tools/usb_remote_key/70-podbox-usbkey.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
mkdir -p ~/.config/systemd/user
cp tools/usb_remote_key/podbox-usbkey.service ~/.config/systemd/user/
# Edit ExecStart if this checkout is at another path.
systemctl --user daemon-reload
systemctl --user enable --now podbox-usbkey
journalctl --user -u podbox-usbkey -f
```

Install the host's libusb runtime if PyUSB reports no backend. Replug the
iPod and receiver after installing the rule. `uaccess` grants access to the
active local login; this is not a headless-machine rule. This service is
independent of HerdRemote and does not change it.

The bridge automatically finds the receiver's `/dev/serial/by-id/` entry.
It accepts only VID `05ac`, PID `1261`, and serial matching
`?AA0000000000360`: exactly one leading character, then the fixed suffix.
This includes charge-only `4AA0000000000360` and PodLink `2AA0000000000360`.
The udev rule uses the same single-character wildcard. It retries
after absence, USB reset, unplug or disk mode. It never selects a USB
configuration, claims an interface, detaches a driver, or sends a key saved
while disconnected.
Disk mode and firmware without this request receive no working Menu input.

Menu is forwarded as a real held button. The first decoded frame sends
EP0 Menu down immediately. Each continuing frame/repeat refreshes the same
down state. After 150 ms of silence, another key, or orderly shutdown, the
bridge sends Menu up. It never turns a Menu hold into Select. Firmware's
ordinary button sampler supplies Menu repeats and long-press actions.
A missing release is bounded by the existing one-second device timeout.

`tests/fixtures/ir-gestures-raw.log` contains host-timed EDGE lines. Tests
feed these through the production decoder, separating the initial full
frame and repeat even when `ir-gestures.log` reports them together as 72
edges. The fixtures cover Menu taps, collapsed doubles and five one-second
Menu holds. The bridge continues to ignore Play and Left/Right commands.
Deploy this bridge with matching Follow-up 20 firmware accepting IDs
1 through 5; older firmware stalls the new volume/heartbeat IDs. The firmware keeps a short EP0 pulse down for at least 50 ms so the
button sampler cannot lose its back-to-back down/up requests. Its one-second
safety release still covers a missing up request.

## EP0 protocol

The firmware protocol is independent of interfaces and USB configurations:

| Setup field | Value |
| --- | --- |
| bmRequestType | `0x40` (vendor, device, OUT) |
| bRequest | `0x5b` |
| wValue | `1` Menu, `2` Select, `3` volume up, `4` volume down, `5` heartbeat |
| wIndex, Menu/Select | `1` down/keepalive or `0` up (unchanged) |
| wIndex, volume | `1` full-frame step or `2` repeat step; no down/up pair |
| wIndex, heartbeat | `1` alive |
| wLength | `0` |

Only configured normal iPod Classic firmware accepts the request. All other
key IDs, request directions, recipients, states and payload lengths STALL.
Bootloader and simulator omit the feature. No descriptor changes are needed.
The native button sampler merges the state with the dock button state;
its existing queue emits `BUTTON_RC_MENU` or `BUTTON_RC_SELECT` and their
press/repeat/release transitions. Firmware does not classify a Menu hold into Select.

Run offline tests without opening either device:

```sh
python3 -m unittest discover -s tools/usb_remote_key/tests -v
python3 tools/tests/usb_remote_key_test.py build-usbkey/probe
```

## Volume reports and fallback

Each complete volume IR frame sends one EP0 step immediately. Every later
REPEAT is also a step except the first `VOL_IGNORE_FIRST_REPEATS = 1`
repeat after each full frame. There is no volume tap/hold classifier,
hold threshold or rate shaping. Full frames always reset the skipped-repeat
count, even when they arrive rapidly. Setting the constant to **0** makes
the first repeat a step too: a tap carrying one repeat produces two steps.
A hold produces one step plus one per later repeat, roughly nine per second.
Unknown keys reset the volume repeat context; orphan/stale repeats do nothing.

Valid receiver STATE/EDGE records allow one EP0 heartbeat per second. An
open serial fd alone is insufficient. After three seconds without a
heartbeat, the resident PodLink host signal still keeps Dock input disabled.
With both signals absent, the player uses the original Dock behavior.
With Follow-up 20, PC volume reports work in every UI screen. Only the
actual Now Playing input context changes audio volume. Other action-based
lists receive one up/down item step, Cover Flow receives one previous/next
album step, and raw viewers receive wheel steps where supported. QuickScreen,
non-list dialogs and editors can ignore list movement; it never invokes their
volume shortcut. Usage/detail continue using the existing host arrow-key
lease and acceleration while connected. Disconnected Usage now pages, rather
than changing audio volume.

The connection predicate remains `computer_connected(now)`: resident PodLink
on the current configured 2AA stream OR the bridge's three-second heartbeat.
While connected, Dock L3/0E volume is ignored in all screens, including WPS.
Only PC reports count. No 80 ms window or per-press Dock fallback is used.
The 64-report FIFO is retained, with screen/mode tags to drop stale batches.
When both connection signals are absent, non-WPS screens use Dock's original
300 ms grouping and 600/250 ms navigation-repeat schedule. Disconnected WPS
keeps the absolute Dock volume setter. A heartbeat can keep the logical
connection true briefly after USB unplug; a charger/profile alone is not a
resident host session.

Live volume KEY and VOL records use `decision=ignored-pc-connected`
for ignored Dock input in every connected screen. PC steps retain `source=PC` and
`decision=used`; page emits identify the selected source. The existing
`pc_tick`/`dock_tick` fields record independent source events; Follow-up 15
performs no pairing. There is no measured/matched 80 ms window to calibrate.
The LOG writer is unchanged. S2 below requires both bridge and HerdRemote
runtime updates. No service deployment is performed by this offline task.

```sh
python3 tools/tests/agents_volume_sources_test.py build-usbkey/sources
```

## Agents MENU exit and USB consent (Follow-up 15 S1)

A confirmed MENU exit from Agents reopens the ordinary USB popup on a
configured computer connection: SELECT enters Disk Mode; MENU keeps charge
only. The existing Agents leave-confirmation remains. Wheel MENU and the
forwarded remote Menu use the same exit path. Remote Play double-tap and
SELECT+PLAY use the music-switch result and do not reopen the popup.

Reopening or cancelling this popup does not detach USB, reset the resident
PodLink session, or invalidate the cached Agents view. Selecting Disk Mode
uses the existing exclusive-storage transition. Unplugged, charger-only,
stale cable-epoch, accessory and already-prompting cases do not reopen it.
FU8 made client close leave the resident USB profile intact; that removed
the former close/re-enumeration prompt. S1 explicitly re-arms consent only
after a MENU exit, without relying on a transport reset.

## Usage remote arrow keys (Follow-up 15 S2)

This replaces the initial FU15 Usage audio-volume decision. In connected
Usage, neither Dock volume nor PC EP0 volume changes iPod audio or pages.
PC EP0 reports in other Agents views still page one step each. With neither
computer signal, Usage retains its original Dock audio-volume setter.

The player sends `vw\t1` (Usage) or `vw\t0` (any other Agents view), as a
single PodLink TEXT frame. A direct, nonqueued refresh runs every 250 ms;
a view change sends immediately on the next UI loop and exit sends 0.
Failed sends retry without enqueueing old leases. HerdRemote accepts only
those two exact records, requires an introduced open Agents session, and
invalidates on reset, foreground loss, link close or shutdown. Older hosts
ignore vw; old firmware sends no lease, so S2 stays inactive.

HerdRemote owns `$XDG_RUNTIME_DIR/podbox-usage-keys/` (0700; default root
`/run/user/<uid>`). It atomically publishes `view.json`: version 1, boolean
usage, random per-view/session token and monotonic expiry, 750 ms after the
last valid view record. `keys.sock` is a nonblocking Unix datagram endpoint,
0600. An exclusive owner.lock prevents a second daemon from removing an
active endpoint. The bridge reads this state for each counted volume
report, and sends nothing when absent, malformed, false or expired. It sends
`UK1 <token> <monotonic-send-time> <left|right|up|down> <count>` to the socket.
HerdRemote rechecks token, open session, view expiry and report age under
100 ms, including immediately before a deferred key worker starts. Buffered
old datagrams are discarded; no command queue/retry or stale replay exists.
Lost view-leave messages expire within 750 ms; detected link loss disables
immediately. A stopped/hung daemon cannot turn a fresh state file into keys.

Each full volume frame starts a new hold and sends exactly one horizontal
tap. The first REPEAT is skipped (`VOL_IGNORE_FIRST_REPEATS = 1`). Every
later associated REPEAT produces one batch by age from the full frame's
first IR edge. Final named thresholds in usb_remote_key.py are:

| Hold age | Batch per counted report | Constant starting tier |
| --- | --- | --- |
| <0.5 s | 1 Left/Right | initial |
| 0.5–1.0 s | 3 Left/Right | USAGE_THREE_CHARS_SECONDS = 0.5 |
| 1.0–4.0 s | 10 Left/Right | USAGE_TEN_CHARS_SECONDS = 1.0 |
| 4.0–7.0 s | 1 Up/Down | USAGE_VERTICAL_SECONDS = 4.0 |
| >=7.0 s | 3 Up/Down (cap) | USAGE_THREE_LINES_SECONDS = 7.0 |

Vol+ selects Left/Up; Vol- selects Right/Down. A new full frame resets age
and repeat skip even during rapid taps. Silence/orphan repeats do nothing.
The same KeyTaps/KeyTool worker as Usage k performs one tool call per
accepted batch at the current desktop focus (ydotool, wtype fallback).
These batches bypass --key-scale to preserve exact counts. No herdr focus
or pane.send_keys call is involved. A busy worker drops new batches; it
never queues them. A running bounded tool call may finish after release;
an unstarted worker expires before the 150 ms IR release timeout. Error
cleanup sends releases only, with no retry of the batch.

Host files to deploy together, from this ZIP:
- tools/usb_remote_key/usb_remote_key.py and its README.md;
- tools/herdremote/herdremote.py, hrproto.py, hrkeys.py, new hrusagekeys.py,
  and README.md;
- docs/herdremote-protocol.md (this document).

The existing remaining HerdRemote siblings, podlink.py, units, requirements
and udev rules remain compatible and are bundled. The IR serial open remains
O_RDONLY. No installation, key injection, device access or service operation
is performed by the offline build/tests.

## Detail host keys and Cover Flow gestures (Follow-up 16)

T1 extends the FU15 host-arrow lease to detail. The player sends
`vw\tdetail\t<pane-id>` only after that detail's pane-focus reply succeeds;
focus pending/failed, another view and exit send `vw\t0`. Usage still sends
`vw\t1`. Refresh/expiry/report bounds remain 250/750/100 ms. HerdRemote
also requires a successful fp for the currently subscribed detail pane,
invalidates on focus attempts, pane changes and session resets, and rechecks
this authorization before starting a batch. Keys use the same current-focus
backend and exact 0.5/1/4/7 s Usage acceleration, with no herdr send_keys.

The player sends legacy vw=0 before entering/changing detail, revoking an
earlier Usage lease even on an old daemon. The new detail record is
deliberately rejected by FU15 daemons. Detail
view.json uses version 2, view=detail, pane=<id>, usage=false. FU15 bridges
reject it; the new bridge handles it and still accepts version 1 Usage.
The token rotates between Usage/detail/panes, so queued reports cannot cross
a view/focus boundary. Old firmware still supplies Usage only. Connected
detail ignores PC EP0 volume locally and never pages; disconnected detail
retains FU13 Dock paging. List one-item steps remain as installed.

T2' replaces the proposed three-screen cycle. Remote Play double-tap keeps
Agents <-> Now Playing. Three short taps, each next press beginning within
200 ms of the previous release, toggle Cover Flow. Triple in Cover Flow
returns to its caller: cached Agents view/selection after normal client
cleanup/reopen, or the existing WPS album-modal helper's Now Playing frame.
Other action-based screens and raw viewers suspend their caller as well.
Both music switches leave Agents silently without USB consent; MENU exit
still uses S1's popup. SELECT+PLAY retains its existing Agents/WPS toggle
and open-Agents shortcut elsewhere.

A second short release waits for the third-tap window before firing double.
That adds a 200 ms classification window; at HZ=100 the strict inclusive
boundary commits at the first tick after it (210 ms), plus scheduling.
The input timeout is bounded by that deadline, rather than adding another
100 ms poll interval. A third short release commits triple immediately.
Single taps keep their meanings except T3: in ACTIVITY_ALBUMCOVERS a remote
Play tap emits the same native SELECT press/release as the wheel. Double
there opens Agents, and triple returns to the origin. The existing 600 ms
remote Play hold there still opens Now Playing; native Play hold still
performs the carousel's original random-spin action. Agents' 300 ms remote
hold and native 150 ms recording classification are unchanged. LOG PLAY now
also reports triple with its context; older record-kind numbers are retained.

Deploy matching usb_remote_key.py, herdremote.py, hrproto.py and
hrusagekeys.py, with the two READMEs and this protocol document. The existing
hrkeys.py backend and other runtime siblings remain compatible and bundled.
No serial-write, service-operation or installation step is part of this
build-only package. All hardware/desktop gesture delivery requires separately
authorized acceptance; local tests use mocked keys and private IPC endpoints.

FU30 forwards captured Apple NEC centre `04` (Dock Play), next `07`, previous
`08`, Menu `02`/`03`, and volume `0b`/`0d`. KeyForwarder sends press, refresh and
silence-release for centre/left/right/menu; volume retains per-report steps.
EP0 key ids 6/7/8 are Left/Right/Play; existing 1..5 retain their meanings.
Heartbeat id 5 with index 2 declares the complete key bridge. Firmware suppresses
duplicate Dock centre/left/right/menu/select bits only while that heartbeat is
fresh. Legacy index-1 heartbeats retain the previous Menu/volume ownership and
never suppress Dock transport keys. Loss of heartbeat restores Dock ownership.
Deploy the paired firmware and this source for the complete mapping.

Remote centre holds emit native Play press/repeat/release, rather than the old
jump-to-WPS shortcut. Existing tap/double/triple meanings and their timing are
retained (300 ms Agents hold, 600 ms other centre hold). Explicit EP0 Select
holds also enter native Play keymaps. Raw viewers share the classifier and a
bounded UI poll so queued events and releases do not wait for another key.

Both native and Cider quickscreens map remote Left/Right to their corresponding
slots, once per press. Vol+/Vol- map to TOP/BOTTOM; an empty slot is a no-op.
Menu and short centre/Select leave. Only the wheel retains quickscreen volume.
This source was tested offline; no serial device was opened for FU30.

## FU43 routing (supersedes FU30 transport forwarding above)

LEFT/RIGHT/PLAY now always use the Dock. The host stops forwarding EP0 IDs
6/7/8 and sends presence heartbeat profile 1. New firmware masks only Dock
MENU/SELECT with either fresh profile; legacy transport reports are ACKed but
not injected, including late duplicates. Volume and FU42 Disk Mode long-MENU
logic remain unchanged. QuickScreen Play remains immediate Select; its physical
triple still toggles Cover Flow across an overlay close.

An old profile-2 host paired with new firmware keeps Dock transport without
duplicates. A new host paired with old firmware keeps Dock transport through
profile 1, but strict MENU/SELECT ownership requires the new firmware. The full
compatibility table is in `docs/covers-navigation.md` in the release package
(source: `docs/podbox/covers-navigation.md`).

The package includes `disk_mode.py`, `herdremote.py`, `hrdisk.py`, and
`podlink_disk.py` from the FU42 base revision so the forwarder has its runtime
dependencies and the combined firmware's Disk Mode handshake remains usable.

## FU44 host fallback

When EP0/PodLink is unavailable continuously for one second, the bridge uses
HerdRemote's private local API for the exact Usage keyboard/voice mapping.
Recovery requires working EP0 plus an open PodLink session. FU43 routing is
retained while connected. FU42 Disk Mode long-MENU eject has exclusive MENU
priority. Six physical IR keys include centre's SELECT-tap/PLAY-hold meanings.
See `docs/podbox/ir-host-fallback.md` in source, or
`.rockbox/docs/ir-host-fallback.md` in the package, for all gestures, safety
limits, tests, host files and exact service restart instructions.
