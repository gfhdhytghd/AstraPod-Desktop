# iPod plugin smoke test

Use the ipod6g firmware and all modules from the same test ZIP. Use a
PodLink connection and keep the HerdRemote daemon running. This test
uses the USB serial number to select one player.

## Prepare the playback fixture

Put an audible 44.1 kHz stereo FLAC of at least two minutes at
`/.rockbox/sm1-test/irq-dma44.flac`. For example, while the iPod is
mounted in Disk Mode, substitute its mount point below.

```sh
DEVICE_ROOT=/path/to/mounted/ipod
mkdir -p "$DEVICE_ROOT/.rockbox/sm1-test"
ffmpeg -n -f lavfi -i 'sine=frequency=440:sample_rate=44100:duration=120' \
  -ac 2 -c:a flac "$DEVICE_ROOT/.rockbox/sm1-test/irq-dma44.flac"
```

Unmount and eject the device. Bring it back to PodLink mode. Back up
the current playlist before the test. Shell `play` replaces it with
the test track.

## Run the suite

From this checkout, use the normal Desktop Python environment.

```sh
.venv/bin/python plugin_smoke.py --serial 2AA0000000000360 --out plugin-smoke-results
```

Use the actual USB serial number. Use a new output directory. Supply
`--control-socket PATH` if HerdRemote uses a custom Disk Mode socket.

The script launches Snow, Stopwatch, Dice, Rockbox Info, Maze,
Rockblox 1D and Oscilloscope. It saves before and after PNGs and checks
the exit status. The two games use Menu as their ordinary exit key.
The script starts the test track for Oscilloscope and verifies playback
during and after the plugin.

The final case launches Snow and requests Disk Mode through HerdRemote.
The script identifies the same iPod volume, unmounts with
`udisksctl unmount` if mounted and calls UDisks `Drive.Eject`.
It checks the USB exit after the player returns to PodLink. It also
captures the restored native screen. See `result.json` and the PNGs.

## Run individual commands

```sh
.venv/bin/python podlink_shell.py --serial 2AA0000000000360 plugin-launch stopwatch
.venv/bin/python podlink_shell.py --serial 2AA0000000000360 plugin-status
.venv/bin/python podlink_shell.py --serial 2AA0000000000360 screenshot stopwatch.png
.venv/bin/python podlink_shell.py --serial 2AA0000000000360 plugin-exit
```

`plugin-exit` requests one Menu/back action. A plugin with a nested
screen can require another action. Read `plugin-status` after the
action to confirm its state and exit reason.

Run the offline shell and script tests with
`python -m pytest -q tests/test_plugin_smoke.py tests/test_podlink_shell.py`.
