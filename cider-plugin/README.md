# PodBox Cider Bridge (FU38)

Metadata and explicit controls inside Cider 4, with an authenticated local renderer connection to HerdRemote. No audio capture and no Apple credential export.

## Compatibility and installation after review

The current target is Cider 4.0.9.1-1 with the active OTA frontend whose entry is `index-DIvsmjeS.js`. Built-in `/usr/lib/cider/resources/spa/` files can differ from the frontend actually served by Cider. `host-assets.json` pins the active renderer entry (SHA256 `74e5f389b2b4adcd7bd6572445023267d03bc647897306c86539e987cbe80e5e`). The installer checks the profile's `update.cup` first, or the built-in asset directory if no OTA exists. An explicit `--assets` selects an asset directory or OTA ZIP. Unknown or changed bytes cause refusal before profile changes.

At runtime, the plugin resolves `/assets/<pinned entry>` against the renderer's `location.origin`, fetches it without credentials or redirects, and verifies its SHA256 before binding. It does not execute or import that entry. All plugin imports are relative plugin-local modules. Playback uses `__PLUGINSYS__.Stores.appleMusicStore`; library calls use `CiderApp.v3` with `throwOnHTTPError: true`, which the pinned active frontend supports. Metadata comes from `CiderApp.musicKitStore`. No private hashed module aliases or deprecated `MusicKit.getInstance()` call is used.

After review, run from a host with Python 3.10+ and the HerdRemote dependencies:

```sh
python cider-plugin/install.py
```

The default destination is `~/.config/sh.cider.genten/plugins/org.podbox.cider-bridge/`. The script moves any existing plugin directory to a timestamped `podbox-backups/` directory inside the Cider profile before replacing it. It does not edit Cider settings, enable plugins, reload Cider, change playback, or restart services. FU38 only runs this script against temporary profiles in tests.

Enable **PodBox Cider Bridge** in Cider Settings > Extensions > Plugins. On first enable, paste the contents of `~/.config/podbox/cider-plugin.secret` into the PodBox connection prompt. This is a random PodBox bridge secret, never an Apple token. HerdRemote creates it once as an owned regular file with mode 0600. The plugin remembers it in Cider's origin localStorage (`podbox.bridge.secret.v1`) for automatic reconnect after Cider restarts. Cider owns that origin storage; it is not copied into a PodBox package. To re-pair, remove only this localStorage entry in Cider developer tools and reload the renderer manually.

**Why manual first pairing:** Cider exposes installed plugin assets through its local HTTP server. Even a filesystem-0600 `connection.mjs` must not contain a secret. The installer writes only the loopback endpoint into that module. Do not paste an Apple user/developer token into the connection prompt.

Cider remembers enabled identifiers in origin localStorage `c3api/applied-plugins`. Replacing or disabling an already-loaded plugin requires a manual renderer reload to unload old code. No reload is performed by this package.

## HerdRemote configuration

- `CIDER_BACKEND=auto` (default): use the plugin after authenticated hello; use RPC while no plugin is connected.
- `CIDER_BACKEND=plugin`: require the plugin; no RPC fallback.
- `CIDER_BACKEND=rpc`: original RPC only; does not create a plugin listener or secret.
- `CIDER_PLUGIN_PORT=10768`: bind only `127.0.0.1`. Pass the same `--port` to the installer.
- `CIDER_PLUGIN_SECRET_FILE`: override the secret path; pass the same `--secret-file` to the installer.
- `CIDER_PLUGIN_RENDERER_ORIGIN=http://127.0.0.1:10767`: the exact renderer Origin, independent of the RPC URL. Set it to the renderer page's origin if the renderer uses a different loopback host or port. Only exact HTTP(S) loopback origins are accepted; no wildcard, missing or `null` origin is accepted in production.
- Existing `CIDER_API_URL` / `CIDER_API_TOKEN` configure RPC fallback only. Changing a proxy or fallback API URL does not change the renderer Origin allow-list.

Both sides reconnect: Cider retries the outbound connection from 250 ms up to 10 seconds; the host listener accepts a fresh authenticated renderer after the previous connection closes. Host WebSocket ping/pong detects a dead Cider within the configured 10+5 second heartbeat interval. Authentication failure does not select the plugin. A connected plugin reporting offline/unauthorized remains selected and reports that static reason; it does not silently route that operation to RPC. An in-flight mutation is never replayed on fallback.

## Protocol v1

JSON text over WebSocket. The server binds `127.0.0.1`, checks the exact Cider Origin and requires a hello within two seconds:

```text
renderer -> host: {v:1,type:"hello",secret:<PodBox secret>,state:<playback snapshot>,reason:""}
host -> renderer: {v:1,type:"hello",ok:true}
host -> renderer: {v:1,type:"request",id:"N",path:<compatibility route>,body:...,method:...,timeoutMs:...}
renderer -> host: {v:1,type:"reply",id:"N",data:...} or {v:1,type:"reply",id:"N",error:<static reason>}
renderer -> host: {v:1,type:"event",event:<host event name>,state:<playback snapshot>,reason:""}
```

The wire carries neither audio nor Apple tokens. Snapshots contain state, current item identifiers, metadata, artwork template, time, volume, rate, repeat/shuffle, membership/favorite status, loading state, and at most three adjacent queue items. Both hrcider clients return the same 15-key snapshot with identical types. Membership stays in now-playing/status metadata; loading uses the separate host loading_state() accessor. Neither is added to the canonical snapshot or device wire format. The library SQLite schema and cached row format remain unchanged.

Requests are bounded to 16 KiB and 16 outstanding operations. Replies/events are bounded to 2 MiB UTF-8, with WebSocket queue depth four and no compression. Replies use request ids; late replies are discarded. Deadlines are capped at ten seconds. Only one renderer mutation may run at once; after a timeout, its gate remains occupied until the host operation settles. Read-only library calls support cancellation through AbortSignal. No timeout retries a control.

State/time/queue/mode/status events come from Cider reactive store subscriptions. Item hints also subscribe directly to the pinned player engine metadata events, independently of Pinia subscriptions. Engine bursts coalesce in one microtask; listeners detach on unload/player replacement. The plugin does not poll. Loading/buffering/seeking notifications use media-element events only; no samples, media source, DRM internals or audio graph are read. Host snapshot and adjacent-artwork consumers normally read the pushed cache. Any item-change hint schedules a read-only plugin GET /api/v2/playback in the state worker. The single receiver applies refresh replies and pushed events in wire order; late replies are discarded. The existing quarter-second host/device publication tick remains local. Explicit queue pages and library index scans still request data on demand. RPC fallback retains its previous polling behavior.

The adapter handles the compatibility routes consumed by `hrcider*.py`: playback and timing, play/pause/toggle/next/previous/seek/rate/volume/shuffle/repeat, favorites/library mutations, direct song queue, collection queue with verified index jump, queue paging/move/append, songs/albums/playlists and their catalog/library tracks, search, recent additions, history, ratings, lyrics and audio-quality metadata. Library reads allow only those Apple paths. Arbitrary origins, token APIs, account APIs, local files and audio endpoints are rejected. Resources are serialized using a metadata whitelist. Errors cross the bridge only as short static reasons.

## Offline checks

```sh
python -m pytest -q herdremote/tests/test_cider_plugin.py
node --test cider-plugin/core.test.mjs cider-plugin/renderer.test.mjs
```

The Python suite uses a fake renderer peer on the real host listener, plus a Node peer using the actual JS adapter and a mocked host. The Node suite uses mocked playback/library APIs. Neither suite controls live Cider. `mock-host.mjs` and `integration-peer.mjs` are test fixtures and are not copied into the Cider profile by the installer. Offline checks do not establish live playback or Apple Music account acceptance.

## Update the installed copy after review (Claude)

Run the updated `install.py` to regenerate `connection.mjs` with the non-secret asset hash map. Copying only plugin.js is insufficient. The installed directory must contain exactly the current runtime files:

- `plugin.yml`
- `plugin.js`
- `core.mjs`
- `renderer.mjs`
- `connection.mjs` (generated by the installer; no bridge secret)

Use `herdremote/hrcider_plugin.py` and the rest of HerdRemote from the same checkout. Existing origin localStorage retains the paired PodBox secret. Reload/enable and any host-service restart remain manual actions after review; this task performs neither.

## Host worker recovery (Live fix 2)

The canonical snapshot keys are id, repeat5, title, artist, album, composer, duration, position, rate, volume, repeat, shuffle, art, template and bg. Text defaults to an empty string, artwork key to `-`, and invalid/unknown background color to `181818`. Numeric output uses integers. The current RPC encodes repeat as none/one/all and shuffle as a boolean; the plugin uses numeric enums. Both encodings normalize to repeat 0/1/2 and shuffle 0/1 before snapshot construction. Plugin-only loading, membership and queue metadata remain outside this mapping. The Bridge adds host system volume/muted state for both backends in the same way.

Unexpected exceptions in long-lived Cider workers produce one static diagnostic per worker per bridge/transport lifetime. Worker recovery uses an interruptible 250 ms backoff. The state worker preserves its prior snapshot through isolated failures and uses its existing unavailable/backoff policy after repeated failures. State comparisons check added and removed keys safely. Consumed controls are never requeued: an unexpected control error cancels pending loading and returns a failed acknowledgement, then the next command can run.

This repair updates host files only: hrcider.py, hrcider_events.py, hrcider_plugin.py and the new hrcider_workers.py. Keep the complete set together. The existing plugin version 1.0.1 and pinned renderer assets remain unchanged. If the temporary `zz-cider-backend.conf` still forces `CIDER_BACKEND=rpc`, the repaired plugin backend will remain inactive until Claude changes that setting after review and manually restarts the host service. This task changes neither the mitigation nor the running service.

## Live fix 3: current item freshness (plugin 1.0.2)

Keep the RPC Socket.IO listener active alongside plugin events in auto/plugin mode. Either source can wake an item refresh; receiver callbacks never wait on their own WebSocket. Failed refreshes retain the pending hint. Apply identity, title and artwork together, and force CN publication on item changes even within the normal 250 ms publication throttle. Fast loading samples do not combine a new item id with an old title.

Each applied plugin item change logs INFO `Cider plugin item applied id_hash=<12 hex> seq=<host receive revision>`. The revision increments for applied snapshots; the short hash identifies the canonical song id without logging titles or raw ids.

This repair requires both the host runtime files (`hrcider.py`, `hrcider_plugin.py`) and plugin 1.0.2 (`plugin.js`, `plugin.yml`, `renderer.mjs`; keep core.mjs alongside them). After review Claude must replace the host files, run the existing installer to back up/replace the plugin, and manually reload/restart the relevant applications. The task itself does not install, enable, reload or restart anything. The pairing secret, connection.mjs and renderer hash pin do not change.
