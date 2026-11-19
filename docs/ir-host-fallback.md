# FU44: Apple IR to host when the iPod link is unavailable

This is a host-only extension of FU42/FU43. Firmware is unchanged. The IR
receiver is still opened O_RDONLY; only HerdRemote runs keyboard and voice
executors. There is no network listener and no sudo.

## Exact existing Usage/Detail meanings

The captured receiver decodes six physical keys: MENU (`02`/`03`), centre
(`04`), RIGHT (`07`), LEFT (`08`), Vol+ (`0b`), Vol- (`0d`). SELECT and PLAY are
semantic interpretations of the same centre key, not seven independent IR
buttons. There is no evidenced separate SELECT IR code to invent.

| Key / gesture | Existing Usage computer action | Existing Detail difference | FU44 host-fallback action |
| --- | --- | --- | --- |
| Centre down (remote PLAY) | Start `vd` hold-to-talk immediately | Targets the detail pane | Same configured `HoldToTalk.press('-')` |
| Centre release before 300 ms | `vc` discards the short recording; a single classified tap becomes SELECT/Enter after the 200 ms next-tap window | Same Enter | Discard, then one Enter after the same window |
| Centre held at least 300 ms, then release | `vu` submits voice; repeats do not restart it | Same voice lifecycle | Same release/submission; no Enter |
| SELECT tap (synthetic centre tap, or physical body SELECT) | One Enter | One Enter | The centre single-tap interpretation above |
| Body SELECT held | First context repeat sends one Enter; subsequent repeats/release do nothing | Same | No separate body key is available over IR |
| Centre double short tap | Switch Agents/WPS; no host key | Same | UI-only gesture: no-op, no Enter |
| Centre triple short tap | Switch Cover Flow; no host key | Same | UI-only gesture: no-op, no Enter |
| LEFT press | One Backspace | Same | One Backspace |
| LEFT held/repeat | Repeated Backspace via the native repeat scheduler | Same | Same 100 Hz schedule: first repeat after 310 ms, then 160/150/140/.../50 ms intervals, capped at 50 ms; no catch-up burst |
| RIGHT press/hold | Leave Usage; no host key and no repeated navigation | No host key | No-op; never type Right/Enter |
| MENU tap | Discard a current recording and leave Agents; no host key | Same menu exit | Discard a current fallback recording; otherwise no-op |
| MENU hold/repeat | QuickScreen UI; no host key | Same | No-op outside Disk Mode; Disk Mode reserves the entire MENU gesture for FU42 eject |
| Vol+ initial report | One Left arrow | Same at the focused detail pane | Same current-focus Left arrow |
| Vol- initial report | One Right arrow | Same | Same current-focus Right arrow |
| Vol± associated repeats | Skip first NEC repeat, then batches below | Same | Identical batches; no key-scale multiplication |

The native body PLAY key has a separate 150 ms short-press threshold. Its
short tap discards the recording and invokes local playback pause/resume
(`agents_screen.c:2056`). FU44 centre uses the remote 300 ms rule above; it
does not turn a remote single tap into a media transport command.

Volume hold age is measured from the first IR edge of the full frame. Every
new full volume frame resets age and repeat skip. Silence of 150 ms ends the
hold. Orphan NEC repeats do nothing.

| Hold age | Vol+ | Vol- |
| --- | --- | --- |
| `<0.5 s` | Left ×1 | Right ×1 |
| `0.5–<1 s` | Left ×3 | Right ×3 |
| `1–<4 s` | Left ×10 | Right ×10 |
| `4–<7 s` | Up ×1 | Down ×1 |
| `>=7 s` | Up ×3 | Down ×3 |

Source evidence for the existing mappings (unchanged firmware at 527949c328):

- `apps-ipod/input/agents_remote.c:36`: short centre emits SELECT press/release in Agents. `:67` sets the Agents hold threshold to 300 ms; `:85` emits remote PLAY on down; `:113` discards a short recording; `:116` classifies triple; `:48` classifies double. `agents_remote.h:8` sets the 200 ms tap window.
- `apps-ipod/input/agents_remote.c:224`: Dock LEFT/RIGHT become native LEFT/RIGHT in Agents. `apps-ipod/input/keymap_ipod.c:386` maps remote PLAY to talk; `:387` maps a short release to discard; `:399` maps MENU release/hold; `:402` maps explicit remote SELECT release to OK.
- `apps-ipod/input/keymap_ipod.c:127`: native PLAY start/end and LEFT repeat. `:35` defines native LEFT/RIGHT/SELECT/MENU. `firmware/drivers/button.c:71`, `:83`, `:296`, `:343` define native repeat delay, acceleration and the strictly-greater first-repeat boundary.
- `apps-ipod/screens/system/agents_screen.c:838`: Usage has zero selectable rows. `:1967` selects the voice target, so Usage uses `-`; Detail uses its pane. `:1993` starts `vd`; `:2024` sends `vc` or `vu`; `:2049` distinguishes short remote/native holds (300/150 ms).
- `apps-ipod/screens/system/agents_screen.c:3151` maps Usage SELECT to Enter and RIGHT to local navigation; `:3180` maps LEFT to Backspace; `:3243` makes held body SELECT one Enter; `:3250` repeats Backspace. `:2800` and `:3132` make MENU leave and discard. `:3123` gives QuickScreen priority.
- `apps-ipod/screens/system/agents_screen.c:361` sends `vw\t1` or `vw\tdetail\t<pane>` after Detail focus is ready. `:2942` refreshes it. `tools/herdremote/hrusagekeys.py:11` defines the 750 ms lease, `:76` validates report age under 100 ms and `:101` dispatches guarded batches through `KeyTaps`.
- `tools/usb_remote_key/usb_remote_key.py:28` names the 0.5/1/4/7 s thresholds; `:178` implements Usage batches; `:212` implements full-frame reset and first-repeat skip.
- `tools/herdremote/herdremote.py` dispatches `k` via `key_command` and `vd/vu/vc` via `voice_command`; `tools/herdremote/hrkeys.py:149` runs the shared, bounded `KeyTaps` worker. `tools/herdremote/hrvoice.py:149` implements press, `:180` release and `:191` discard. The same configured HoldStart/HoldStartRaw, bus, max-hold watchdog and keyboard backend are used in fallback.

The desktop focus is never changed by fallback. Keyboard taps target the
current keyboard focus, as Usage does. Voice uses Usage's `-` target, so even
`--voice-focus on` does not focus a remembered pane. This does not replace
Usage semantics with Detail's pane-focus behavior.

## Mode definition and arbitration

The bridge polls every 100 ms. EP0 health is a fresh USB GET_CONFIGURATION on
the verified AstraPod device. Failed transfers dispose the handle and back off
500 ms. Absence, unplug and transfer errors count as unavailable. In parallel,
HerdRemote reports its currently open PodLink session. Disk Mode is unavailable
for this purpose, even if EP0 itself responds. Disk presence uses EP0 status,
or, when EP0 fails, the existing UUID + sysfs-verified storage identity.

All three conditions must hold to use the iPod: EP0 works, the daemon has an
open PodLink session, and Disk Mode is false. An unavailable interval must
remain continuous for at least one second before the daemon authorizes
`host-fallback`. A successful combined sample returns to `ipod` immediately,
without a reverse debounce (up to the next 100 ms poll and IPC scheduling).
Transient failures reset the unavailable timer. A missing/stalled HerdRemote
cannot execute fallback actions; no process runs keys or voice on its behalf.

The router chooses exactly one path per decoded event. In `ipod` mode, the
existing FU43 KeyForwarder and VolumeForwarder are unchanged: MENU/volume use
EP0, LEFT/RIGHT/PLAY use Dock. In fallback, those IR events do not feed either
forwarder or the old Usage-volume datagram path. A mode change cancels pending
taps/repeats and blocks the already-held gesture until release; it never
replays that gesture into the new mode. A fresh full frame begins a new press.

EP0 may fail while CDC and the Dock still work. While fallback owns desktop
input, HerdRemote rejects iPod keyboard/voice/focus/question actions, disables
the old Usage/Detail volume lease and ignores new Cider control mutations.
An old iPod-owned recording is discarded on takeover. Already-running bounded
keyboard/Cider operations can finish; no operation is retried. Dock can still
navigate the iPod locally because FU43 deliberately leaves those physical
keys with firmware. Host-only code does not intercept local iPod UI actions.

In Disk Mode, MENU is exclusively FU42: a one-second remote hold unmounts the
verified FAT volume and sends SCSI eject. Short MENU is a no-op on the desktop.
Other keys can use fallback after its one-second debounce. MENU never types
Escape or Enter. Busy unmount remains a logged refusal, never a forced eject.

## Local API and lifecycle

FU42's `$XDG_RUNTIME_DIR/podbox-herdremote/disk.sock` is extended with bounded
`ir-<JSON>\n` records. The directory remains 0700, the socket 0600, and every
connection checks SO_PEERCRED against the daemon UID before dispatch. No new
listener is exposed. An old daemon rejects the extension and stays on FU43.

Status records contain a random bridge identity, increasing sequence number,
monotonic send time, EP0 status and Disk status. The reply supplies mode and a
random mode token. Action records echo that token and sequence. The daemon
rejects wrong owners, old tokens, duplicate/out-of-order sequence numbers,
nonfinite/future/100 ms-old timestamps, unsupported keys and counts. Another
bridge cannot replace a live owner. Status leases last 750 ms. A deferred
keyboard worker rechecks the same token, lease and timestamp immediately
before injecting. Busy work is dropped rather than queued. A lost reply is
never retried, because its action may already have run.

Voice runs on a bounded, one-hold worker using the daemon's configured
HoldToTalk instance. Short tap, MENU, mode change, receiver loss, bridge lease
expiry and shutdown discard the owned recording. A normal long release submits
it. D-Bus operations already in flight may finish before cancellation; the
existing voice stop retry/watchdog remains responsible for failed stop calls.
A daemon process crash cannot run this Python cleanup. A hung/dead daemon or
unavailable keyboard/voice backend therefore cannot be claimed as successful
input delivery. No typed content or voice transcript is added to logs.

The bridge logs one INFO `IR mode ipod` / `IR mode host-fallback` per change.
HerdRemote logs whitelisted fallback action kinds and bounded arrow counts.

## Deployment (instructions only; FU44 does not execute these)

Read-only `systemctl --user cat herdremote.service podbox-usbkey.service`
confirmed the installed launch paths for this host. Both units use copied
scripts, not this worktree. Keep the existing virtual environments, other
runtime siblings, voice settings and units.

```sh
src=/home/wilf/data/PodJS/podbox.wt-disk
hr="$HOME/.local/share/podbox-herdremote/tools/herdremote"
ir="$HOME/.local/share/podbox-usbkey"
install -m 644 "$src/tools/herdremote/herdremote.py" "$src/tools/herdremote/hrdisk.py" "$src/tools/herdremote/hrfallback.py" "$hr/"
install -m 644 "$src/tools/usb_remote_key/usb_remote_key.py" "$src/tools/usb_remote_key/disk_mode.py" "$src/tools/usb_remote_key/host_fallback.py" "$ir/"
systemctl --user restart herdremote.service
systemctl --user restart podbox-usbkey.service
journalctl --user -u herdremote.service -u podbox-usbkey.service -n 50 --no-pager
```

Copy all six runtime files before restarting either service. Restart
HerdRemote first so the new local API is ready before the bridge. Unit files
are unchanged, so daemon-reload is not required. These commands are operator
instructions, not actions performed during the task. No firmware installation
is required. The package preserves the base firmware and version metadata.

For a ZIP-based deployment, extract the package outside the mounted iPod,
then use its `.rockbox/tools/herdremote/` and `.rockbox/tools/usb_remote_key/`
files in the same copy commands. `.rockbox/docs/ir-host-fallback.md` contains
this mapping and deployment guide.
