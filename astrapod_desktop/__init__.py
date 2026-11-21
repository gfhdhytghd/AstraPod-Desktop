"""AstraPod desktop: the iPod library, import, sync and remote companion.

Parts map:
  qt.py         Qt binding, sys.path for tools/ and tools/herdremote/
  contracts.py  dataclasses of the shared interfaces, used when the real
                modules are missing
  backends.py   real adapters: device library, daemon socket, plugin host
  fakes.py      in-memory stand-ins for all three, for demo and tests
  jobs.py       worker threads, the device lane and the busy guard
  artwork.py    artwork memory/disk cache, lazy device fetch
  models.py     songs/albums/browser item models
  widgets.py    painted M3 widgets (switch, indicators, drawer, FAB ...)
  icons.py      vector icons
  pages/        one module per sidebar page, plus the Get Info sheet
  window.py     the main window
  runtime.py    tray, close-to-tray, supervised HerdRemote and plugin host
  app.py        entry point
"""
