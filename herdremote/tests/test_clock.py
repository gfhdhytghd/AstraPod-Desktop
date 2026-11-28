"""Clock wire/scheduler tests; no serial port or live clock changes."""
import datetime
import unittest
from zoneinfo import ZoneInfo
from unittest import mock
from test_herdremote import herdremote, hrproto
import hrtimezone


class ClockTests(unittest.TestCase):
    def test_local_wire(self):
        for offset in (-18000, -14400, 19800, 50400, -50400):
            dt = datetime.datetime(2026, 11, 13, 19, 2, 3,
                                   tzinfo=datetime.timezone(datetime.timedelta(seconds=offset)))
            self.assertEqual(hrproto.clock(dt),
                             ("TS\t1\t2026-11-13T19:02:03\t%+d" % offset).encode())
            self.assertEqual(hrproto.decode(hrproto.clock(dt))[0][0:2], ["TS", "1"])

    def test_extended_fold_encodes_two_distinct_instants(self):
        zone = ZoneInfo("America/New_York")
        rule = "EST5EDT,M3.2.0,M11.1.0"
        wall = datetime.datetime(2026, 11, 1, 1, 30, tzinfo=zone)
        for fold, offset, utc in ((0, -14400, 5), (1, -18000, 6)):
            fields = hrproto.decode(hrproto.clock(
                wall.replace(fold=fold), ("America/New_York", rule)))[0]
            self.assertEqual(fields, [
                "TS", "1", "2026-11-01T01:30:00", str(offset),
                f"2026-11-01T0{utc}:30:00Z", "America/New_York", rule,
            ])

    def test_capability_and_same_offset_zone_change(self):
        clock = herdremote.ClockSync()
        sent = []
        instant = 1794556800
        with mock.patch.object(hrtimezone, 'local_zone') as zone:
            zone.return_value = ("Europe/Paris", "CET-1CEST,M3.5.0,M10.5.0/3")
            self.assertTrue(clock.poll(sent.append, 100, instant, 0))
            zone.assert_not_called()
            self.assertEqual(len(hrproto.decode(sent[-1])[0]), 4)

            daemon = mock.Mock(clock_sync=clock)
            daemon.send.side_effect = sent.append
            with mock.patch.object(herdremote.time, 'monotonic', return_value=101), \
                 mock.patch.object(herdremote.time, 'time', return_value=instant + 1):
                herdremote.Daemon.command(daemon, b"tz\t1")
                self.assertTrue(clock.supports_zone)
                self.assertEqual(len(hrproto.decode(sent[-1])[0]), 7)
                self.assertFalse(clock.poll(sent.append, 101, instant + 1))
                zone.return_value = ("Europe/Berlin", zone.return_value[1])
                self.assertTrue(clock.poll(sent.append, 101, instant + 1))
                self.assertIn(b"Europe/Berlin", sent[-1])

    def test_zone_source_fallbacks(self):
        with mock.patch.dict("os.environ", {"TZ": "America/New_York"}):
            self.assertEqual(hrtimezone.local_zone()[0], "America/New_York")
            self.assertIn("M3.", hrtimezone.local_zone()[1])
        for value, wanted in [
            ("", ("Etc/UTC", "UTC0")),
            ("<+0545>-5:45", ("Local", "<+0545>-5:45")),
            ("Invalid!;name", ("Local", "-")),
        ]:
            with mock.patch.dict("os.environ", {"TZ": value}):
                self.assertEqual(hrtimezone.local_zone(), wanted)

    def test_invalid(self):
        for dt in (datetime.datetime(2026, 1, 1),
                   datetime.datetime(2038, 1, 1, tzinfo=datetime.timezone.utc),
                   datetime.datetime(1999, 1, 1, tzinfo=datetime.timezone.utc)):
            with self.assertRaises(ValueError):
                hrproto.clock(dt)

    def test_schedule(self):
        clock = herdremote.ClockSync()
        sent = []
        with mock.patch.object(herdremote.hrproto, 'clock', return_value=b'TS'):
            self.assertTrue(clock.poll(sent.append, 100, 10000, 0))
            self.assertFalse(clock.poll(sent.append, 101, 10001, 0))
            self.assertFalse(clock.poll(sent.append, 699, 10599, 0))
            self.assertTrue(clock.poll(sent.append, 700, 10600, 0))
            self.assertTrue(clock.poll(sent.append, 701, 14201, 0))  # forward NTP step
            self.assertTrue(clock.poll(sent.append, 702, 10002, 0))  # backward step
            self.assertTrue(clock.poll(sent.append, 703, 10003, 3600))  # DST/TZ
            self.assertTrue(clock.poll(sent.append, 704, 13604, 3600))  # suspend
        self.assertEqual(len(sent), 6)

    def test_exact_jump_threshold_and_gradual_slew(self):
        clock = herdremote.ClockSync()
        with mock.patch.object(herdremote.hrproto, 'clock', return_value=b'TS'):
            self.assertTrue(clock.poll(lambda _: None, 0, 10000, 0))
            self.assertFalse(clock.poll(lambda _: None, 1, 10003, 0))
            self.assertTrue(clock.poll(lambda _: None, 2, 10005, 0))

    def test_failed_send_retries(self):
        clock = herdremote.ClockSync()
        with mock.patch.object(herdremote.hrproto, 'clock', return_value=b'TS'):
            with self.assertRaises(OSError):
                clock.poll(mock.Mock(side_effect=OSError), 0, 10000, 0)
            self.assertTrue(clock.poll(lambda _: None, 1, 10001, 0))

    def test_session_reset_and_loop_wiring(self):
        import inspect
        self.assertIn('self.clock_sync = ClockSync()', inspect.getsource(herdremote.Daemon.reset_session))
        self.assertIn('self.clock_sync.poll(self.send)', inspect.getsource(herdremote.Daemon.open_link))
        self.assertIn('self.clock_sync.poll(self.send)', inspect.getsource(herdremote.Daemon.run_loop))
