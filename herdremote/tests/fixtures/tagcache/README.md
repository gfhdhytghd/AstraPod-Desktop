These TCH10 files come from the native PodBox `tools/database_pb` build.
It scanned four synthetic 250 ms FLAC tones tagged with FFmpeg. Their
modification time was 2024-02-03 12:00 UTC. The files include a managed
Unicode filename and a podcast title ending in a date.

`walk.txt` contains the TEXT replies from the firmware's unmodified
`MS3 DB` / `MS3 NEXT` track walk. To reproduce it, copy the TCD files into
`.rockbox/library/database` under a temporary root and run
`tools/tests/podlink_library_test.c`'s compiled test binary against that root.
