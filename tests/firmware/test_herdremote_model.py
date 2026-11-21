"""Check the firmware model against the HerdRemote encoder."""
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from _support import DESKTOP, MISSING_FIRMWARE, find_firmware
import hrdetect
import hrproto


class CrossLanguage(unittest.TestCase):
    """The player's C parser reads what this daemon writes."""

    def test_c_model_reads_python_messages(self):
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc:
            self.skipTest("no host C compiler")
        root = find_firmware()
        if root is None:
            self.skipTest(MISSING_FIRMWARE)
        src = root / "apps-ipod/screens/system"
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "hrmodel")
            subprocess.check_call([cc, "-std=gnu11", "-Wall", "-Werror",
                "-I" + str(src), root / "tools/tests/herdremote_model_test.c",
                src / "herdremote_model.c", "-o", exe])
            sample = DESKTOP / "herdremote/tests/samples/claude-perm-bash.txt"
            q = hrdetect.find_question(sample.read_text(encoding="utf-8"))
            q.update(id="0badf00d", pane="w1:p1", workspace="w1",
                     agent="项目 · claude")
            messages = [
                hrproto.hello("herdremote-1"),
                hrproto.workspaces([dict(id="w1", label="项目" * 20,
                    focused=True, working=1, waiting=1, done=0, idle=2)]),
                hrproto.status([dict(id="w1", tab1="i", other="w")]),
                hrproto.agents("w1", [dict(pane="w1:p1", kind="claude",
                    status="blocked", question=True, name=None,
                    title="标题", last="行" * 40)]),
                # Pre-NAV2 firmware ignores these extensions, including T
                # with seven fields, without replacing its heartbeat state.
                hrproto.tabs("w1", [dict(id="w1:t1", label="build", status="b"),
                                    dict(id="w1:t2", label="review", status="w")]),
                hrproto.detail("w1:p1", dict(state_age_s=12, task="😀" * 80,
                    now="Working", say="中" * 300,
                    steps=["Read 3 files", "Ran 7 shell commands"],
                    input="First\\literal\n第二行😀", cursor=8)),
                hrproto.question(q),
                hrproto.result("5", True, "Answered"),
                hrproto.tick(3, True)]
            path = os.path.join(tmp, "messages")
            with open(path, "wb") as f:
                for m in messages:
                    f.write(struct.pack("<H", len(m)) + m)
            out = subprocess.check_output([exe, path]).decode("utf-8")
            failure = hrproto.result("6", False, "No local herdr window")
            with open(path, "wb") as f:
                f.write(struct.pack("<H", len(failure)) + failure)
            failure_out = subprocess.check_output([exe, path]).decode("utf-8")
            for maximum, wanted, wanted_cursor in [
                    (16384, "😀" * 3000, 3000),
                    (4096, "…" + "😀" * 374, 375)]:
                wire = hrproto.detail("w1:p1", dict(state_age_s=0, task="task", now="Working",
                    input="😀" * 3000, cursor=3000), max_message=maximum)
                self.assertLessEqual(len(wire), maximum)
                if maximum == 16384:
                    self.assertGreater(len(wire), 4096)
                with open(path, "wb") as f:
                    f.write(struct.pack("<H", len(wire)) + wire)
                wide_out = subprocess.check_output([exe, path]).decode("utf-8")
                self.assertIn("D w1:p1 0 task|Working|||||" + wanted + "|" + str(wanted_cursor), wide_out)
        # The old C harness reports ignored unknown messages on stdout.
        lines = [line for line in out.splitlines() if not line.startswith("rejected ")]
        self.assertEqual(lines[0], "hello=1 herdr_ok=1 age=3")
        # The daemon cuts to its byte budgets on a character boundary, and
        # each budget fits the player's field.
        self.assertEqual(lines[1], "W w1 1 1 1 0 2 i w " + "项目" * 6 + "项")
        self.assertEqual(lines[2], "A w1 w1:p1 claude b 1 |标题|" + "行" * 30)
        self.assertEqual(lines[3], "Q 0badf00d w1:p1 w1 0 项目 · claude")
        self.assertIn("? Do you want to proceed?", lines)
        self.assertIn("o 4 No", lines)
        self.assertIn("D w1:p1 12 " + "😀" * 59 + "…|Working|" + "中" * 300 +
                      "|Read 3 files|Ran 7 shell commands||First\\literal\n第二行😀|8", out)
        self.assertEqual(lines[-1], "R 5 1 Answered")
        self.assertEqual(failure_out.splitlines()[-1],
                         "R 6 0 No local herdr window")
