# AstraPod input plugins

## Create a plugin

Make a directory in `~/.local/share/astrapod/plugins/` or
`$XDG_DATA_HOME/astrapod/plugins/` if you set `XDG_DATA_HOME`.
Repository plugins go in `astrapod_plugins/`. The host discovers
plugins when it is constructed. It logs invalid manifests and duplicate
IDs. A repository plugin takes priority over a user plugin with the same ID.

Save this manifest as `example-button/plugin.json`:

```json
{
  "id": "example-button",
  "name": "Example Button",
  "version": "1.0.0",
  "kind": "input",
  "entry": "plugin.py",
  "api": 1,
  "description": "Reports a test tap.",
  "settings": {"key": {"type": "string", "default": "select"}},
  "permissions": []
}
```

Use lowercase letters, digits and single hyphens for the ID. Match the
directory name and use at most 64 characters. Give the host a relative
entry path inside the plugin directory. A `.py` entry runs with the host's
Python interpreter. Give any other entry execute permission and a shebang
if it is a script. The host starts the entry with its parent directory as
the working directory.

Use setting names made of lowercase letters, digits, hyphens or underscores.
Each field requires a `type` and `default`. Supported types are `string`,
`boolean`, `integer` and `number` (integer or finite float).
Add a `description` if the setting needs an explanation in the UI, and a
`label` to name it there. To offer a fixed set of values, list them in
`enum`; the host refuses any other value. `labels` maps each value to the
text the desktop app shows for it.

Save this example as `example-button/plugin.py`:

```python
from astrapod_plugins.sdk import Plugin

plugin = Plugin()
plugin.status("running", device="example")
plugin.input(plugin.settings["key"], "tap")
try:
    plugin.receive()
except EOFError:
    pass
```

From the repository root with the virtual environment active, run:

```sh
python -m astrapod_plugins list
python -m astrapod_plugins enable example-button
python -m astrapod_plugins run --events
```

Use Ctrl+C to stop the foreground host. Use `disable example-button` to
stop the plugin and prevent future starts. Plugins start disabled.
The CLI and Python API share `~/.config/astrapod/plugins.json`, or
`$XDG_CONFIG_HOME/astrapod/plugins.json`. Use `--config PATH` before the
command to select another file. The running host reads changes within its
next configuration poll. A change to effective settings restarts the plugin
and sends a fresh settings record. Changes to the manifest require a new host.

## Process protocol

Write UTF-8 JSON objects, each followed by a newline. The limit is 4096
bytes per record, including the newline. Flush each record. Send logs to
stderr. Read stdin until EOF and exit on host loss. Handle SIGTERM to
release held controls and close the device. The host first requests
termination and then forces exit after 20 seconds if necessary. After an
unexpected exit, the host retries after 0.5 seconds, doubles the delay
up to 30 seconds, and resets it after a 30-second run.

| Direction | Record |
| --- | --- |
| Host to plugin, once at startup | `{"type":"settings","api":1,"source":"example-button","settings":{"key":"select"}}` |
| Plugin to host | `{"type":"input","key":"select","phase":"tap","source":"example-button","t":37.5}` |
| Plugin to host | `{"type":"status","state":"running","device":"example","detail":""}` |
| Plugin to host | `{"type":"action","id":1,"action":"ipod.button","args":{"key":"select","value":1}}` |
| Host to plugin | `{"type":"action_result","id":1,"ok":false,"error":"permission required: ep0"}` |

Input times are monotonic seconds. Use `down`, `repeat`, `up` or `tap` for
the phase. Standard keys are `menu`, `select`, `play`, `left`, `right`,
`up`, `down`, `vol_up` and `vol_down`. Use a custom key for any other
control. An Apple Remote input can carry `remote`, `plastic` or
`aluminium`, when the frames show which remote sent it; the host drops
any other value. Set status to `running`, `waiting` or `error`. Set `device` to
a displayable name or `null`, and `detail` to a short string. The host
assigns `source` from the manifest. Status can also carry `mode` with the
values `ipod`, `host-fallback` or `pending`.

`Plugin()` consumes the startup record and exposes `.source` and
`.settings`. Use `.input()` and `.status()` to publish observations.
`.action()` sends a request and waits for the corresponding reply.
Use `.request_action()` and `.receive()` when you need to manage replies
yourself. Match responses by integer ID and do not replay failed or
unanswered actions.

## Actions and permissions

Run plugins from publishers you trust. Each runs with your user account's
device and file access. The manifest gates the host's actions. `usb` and
`serial` declare direct device access.

| Action | Permission | Arguments |
| --- | --- | --- |
| `ipod.button` | `ep0` | `key` is `menu`, `select`, `vol_up` or `vol_down`. For Menu/Select, `value` is 1 for down or 0 for up. For volume, use 1 for a step or 2 for a repeat step. |
| `keys.tap` | `keys` | `key` is `bs`, `enter`, `left`, `right`, `up` or `down`. `count` is 1 for Backspace and Enter, 1 or 3 for Up/Down, and 1, 3 or 10 for Left/Right. |
| `voice.press`, `voice.release`, `voice.cancel` | `voice` | Empty `args` object. Voice uses the current focus. |

Keyboard and voice requests require a top-level `lease` object containing
`id`, `seq`, `sent` and `token` from the active HerdRemote fallback
session. With the bridge's fallback client, pass
`lease=client.credentials()` to the SDK immediately before sending.
HerdRemote checks ownership, the token, request age and sequence. Keep
requests in sequence with the lease owner's status traffic. See
[IR host fallback](ir-host-fallback.md) for lease and timing rules.

Use actions for non-critical requests. Input events sent to the host are
observations. A subscriber can read them without executing any action.

## Apple IR

The reference plugin publishes Menu, Left, Right and volume as `menu`,
`left`, `right`, `vol_up` and `vol_down`. The plastic remote's centre
button is `play`. The aluminium remote's centre button is `select` and its
Play/Pause button is `play`; both send a prefix frame before the centre
code, so these inputs carry `remote: "aluminium"` and a bare centre press
carries `remote: "plastic"`. Other keys are the same on both remotes and
carry no `remote`. The `remote` setting picks the remote the desktop app
draws: `auto` follows the last `remote` seen and draws the aluminium remote
until then. Centre tap and
hold interpretation stays with the bridge and firmware. Each recognized
full frame starts a hold. A repeat refreshes it. A new full frame or
150 ms of silence ends it. The event stream includes observed volume
repeats that the bridge skips for delivery. The bridge releases held input
on receiver loss. If the observation pipe fills, the plugin can drop
events. Set `report_input` to false to suppress input observations.

The bridge handles EP0, heartbeat, Disk Mode and timestamped HerdRemote
fallback calls in the plugin process. The status `mode` reports the
daemon's last sampled decision. Inspect the logs for device access errors.

## Host API

Import `PluginHost` from `astrapod_plugins.host`. Call `list()` for
`PluginInfo` snapshots. Use `settings(id)` for the effective values and
`set_settings(id, values)` to update selected fields. Use `enable(id, bool)`
to set the enabled state. Call `subscribe(callback)` for future input and
status records. Keep callbacks short and use your UI's thread handoff when
necessary. Call the returned function to unsubscribe. Call `start()` to
launch supervision and `stop()` to release the processes.

## Migrate the systemd user service

Keep the receiver's udev rules and install libusb. Set up the checkout and
virtual environment as described in [User services](../README.md#user-services).
Run from that checkout. Stop the desktop's embedded plugin host before starting
the service. If a second host starts, it reports waiting while the first host
owns the plugin.

```sh
install -d "$HOME/.config/systemd/user"
install -m 644 astrapod_plugins/astrapod-plugins.service "$HOME/.config/systemd/user/"
systemctl --user disable --now podbox-usbkey.service
.venv/bin/python -m astrapod_plugins enable apple-ir
systemctl --user daemon-reload
systemctl --user enable --now astrapod-plugins.service
journalctl --user -u astrapod-plugins.service -f
```

To roll back, disable `apple-ir` with the CLI. Wait for it to stop in any
embedded host. Disable and stop `astrapod-plugins.service`, then enable and
start `podbox-usbkey.service`.
