# HerdRemote wire protocols

The firmware's [HerdRemote protocol reference](https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/herdremote-protocol.md)
describes the Agents messages and player behavior.

## Computer clock sync

HerdRemote sends standalone `TS` TEXT records. Fields use literal TAB
separators and have no trailing newline. A session begins with the legacy
four-field form:

```text
TS\t1\t2026-11-13T19:02:03\t-18000
```

The record carries the host's local date and time, then a signed UTC offset
in seconds east of UTC. HerdRemote also sends the standalone probe `tz\t?`. After the player replies
with `tz\t1`, HerdRemote immediately sends the extended form:

```text
TS\t1\t2026-11-13T19:02:03\t-18000\t2026-11-14T00:02:03Z\tAmerica/New_York\tEST5EDT,M3.2.0,M11.1.0
```

The three new fields carry a matching UTC date and time with a `Z`
suffix, the IANA zone name (at most 63 ASCII bytes) and the TZif POSIX
rule (at most 95 ASCII bytes). Both dates are in 2000–2037. If the
operating system supplies no name, HerdRemote sends `Local`. If it
supplies no usable rule, HerdRemote sends `-` and the offset serves
as the fallback.

The capability belongs to the PodLink connection and survives opening
or leaving Agents. Old players receive only the legacy form. No
protocol version changes. Once negotiated, changing zones triggers
a send even when both zones have the same current offset. Clock
steps over two seconds, offset changes and a ten-minute interval
also trigger a send.

On the player, **Sync time from computer** controls the clock. **Follow
computer time zone** defaults to On. Turn it off to keep the selected city
while still syncing UTC.
