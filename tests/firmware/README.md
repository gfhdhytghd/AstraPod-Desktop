# Firmware contract probes

Run these probes from the AstraPod Desktop root. Each probe reads
`ASTRAPOD_FIRMWARE` or falls back to a sibling checkout named `AstraPod` or
`podbox`. When the checkout is absent the probes report a skip.

```sh
export ASTRAPOD_FIRMWARE=/path/to/AstraPod
out="$(mktemp -d -p /tmp)"
python -m pytest -q tests/firmware
python tests/firmware/cider_featured_test.py "$out/featured"
python tests/firmware/remote_menu_pipeline_test.py "$out/menu"
python tests/firmware/remote_palette_test.py "$out/palette"
python tests/firmware/fu76_handoff_contract_test.py "$out/handoff"
python tests/firmware/time_zone_contract_test.py "$out/time-zone"
```

The wire check uses the firmware's `tools/tests/podlink_rust_build.py`
and its pinned Rust toolchain. Set `PODLINK_TEST_BUILD` to cache the native
Rust build outside the firmware checkout. The other probes use a C compiler.
The palette probe also needs the SDL simulator build prerequisites.

The firmware checkout supplies the shared album-art builder, USB remote key
fixture and handoff fixture. The desktop checkout supplies the host mocks
and the palette-specific C probe.

Additional probes have their own build or fixture requirements:

- `fu46_e2e/build.py` takes a completed iPod 6G build directory
  with its generated headers.
  It requires the ARM compiler and native 32-bit ASan/UBSan support.
  See `fu46_agents_e2e.py --help` for the capture files needed for replay.
- `fu79_remote_art_sim.py` and `fu81_spectrum_sim.py` use an SDL simulator
  and a display for frame capture. Pass a **disposable firmware source
  copy** to their `--instrument` options. They edit that copy in place.
  Build the instrumented simulator before capture. The FU81 probe requires
  a pre-native-UI revision with the skin renderer, such as `2eb3a0d2c5`.
