"""Offline tests for PodLink Shell control requests."""

import errno
import json
import struct

import pytest

import podlink_shell as shell


class Player:
    bus = 1
    address = 2

    def __init__(self, revision):
        self.revision = revision
        self.state = 0
        self.path = b""
        self.commands = []

    def ctrl_transfer(self, direction, request, value, index, length, *, timeout):
        assert (request, index, timeout) == (0x5D, 0x4951, 1000)
        if direction == 0xC0 and value == 0:
            assert length == 16
            count = len(self.commands)
            return struct.pack("<4I", 0, count, count, 0)
        if direction == 0xC0 and value == 1:
            assert length == 96
            return struct.pack("<II88s", self.revision, self.state, self.path)
        assert direction == 0x40 and length == b""
        self.commands.append(value)
        if value == 7 and not self.revision:
            self.state = 4
        elif value == 8:
            self.state = 2
            self.path = b"/Recordings/PodLink-0001.wav"
        else:
            assert value in (6, 7)
            self.state = 1


@pytest.mark.parametrize(("source", "command"), [("line-in", 6), ("mic", 7)])
def test_record_start_stop_fetch(monkeypatch, capsys, source, command):
    player = Player(1)
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda seconds: None)
    calls = []
    monkeypatch.setattr(shell, "record_fetch", lambda *args: calls.append(args))

    shell.execute("serial", "record-info")
    assert "hw_revision=1 inputs=line-in,mic" in capsys.readouterr().out
    shell.execute("serial", "record-start", source)
    with pytest.raises(RuntimeError, match="Stop recording"):
        shell.execute("serial", "record-fetch", "capture.wav")
    shell.execute("serial", "record-stop")
    shell.execute("serial", "record-fetch", "capture.wav")

    assert player.commands == [command, 8]
    assert calls == [("serial", player.path.decode(), "capture.wav")]


def test_record_start_reports_unavailable_mic(monkeypatch):
    player = Player(0)
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="input unsupported"):
        shell.execute("serial", "record-start", "mic")


def test_record_info_reports_pcm_allocation_failure(monkeypatch, capsys):
    class FailedPlayer(Player):
        def ctrl_transfer(self, direction, request, value, index, length, *, timeout):
            if direction == 0xC0 and value == 2:
                assert (request, index, length) == (0x5D, 0x4951, 20)
                return struct.pack("<5I", 3, 0x10, 0x80400000, 0, 0)
            return super().ctrl_transfer(
                direction, request, value, index, length, timeout=timeout
            )

    player = FailedPlayer(1)
    player.state = 3
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    shell.execute("serial", "record-info")
    assert "stage=init audio_status=0x00000010 pcm_error=0x80400000 fs_errno=0 dma_blocks=0" in (
        capsys.readouterr().out
    )


MENU = {"kind": "menu", "id": "0", "label": "Settings", "path": ["Settings"]}
SETTING = {
    "kind": "setting", "id": "0/0/0", "label": "音量",
    "path": ["Settings", "Sound", "音量"], "cfg": "volume",
    "type": "sound", "value": "-20", "min": -74, "max": 6, "step": 1, "unit": "dB",
}
ACTION = {
    "kind": "action", "id": "0/1/0", "label": "Update Database",
    "path": ["Settings", "Library", "Maintenance", "Update Database"],
}


class SettingsPlayer:
    def __init__(self, rows):
        self.rows = rows
        self.commands = []
        self.reads = []
        self.cookie = 0
        self.reply = b""
        self.stale = False

    def ctrl_transfer(self, direction, request, value, index, data, *, timeout):
        assert (request, index, timeout) == (0x5D, 0x4953, 1000)
        if direction == 0x40:
            cookie, = struct.unpack_from("<I", data)
            assert cookie != 0
            body = data[4:]
            self.commands.append((value, body))
            self.cookie = cookie
            self.stale = True
            if value == 1:
                ordinal, = struct.unpack("<H", body)
                result = self.rows[ordinal] if ordinal < len(self.rows) else None
            elif value == 2:
                result = next((row for row in self.rows if row.get("cfg", "").encode() == body),
                              {"error": "unknown cfg name"})
            elif value == 3:
                cfg, separator, setting_value = body.decode("utf-8").partition(": ")
                assert separator
                row = next(row for row in self.rows if row.get("cfg") == cfg)
                result = {**row, "value": setting_value}
            else:
                assert value == 4
                assert body == b"\x01" + ACTION["id"].encode()
                result = {"status": "opened"}
            self.reply = json.dumps(result, ensure_ascii=False).encode("utf-8")
            return len(data)

        assert direction == 0xC0 and data == 128
        self.reads.append(value)
        if self.stale:
            assert value == 0
            self.stale = False
            return bytes(128)
        offset = value * 122
        return struct.pack("<IH122s", self.cookie, len(self.reply),
                           self.reply[offset:offset + 122])


@pytest.fixture
def settings_player(monkeypatch):
    player = SettingsPlayer([MENU, SETTING, ACTION])
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda seconds: None)
    return player


def test_settings_tree_pages_and_json(settings_player, capsys):
    shell.execute("serial", "settings-tree", json_output=True)
    assert json.loads(capsys.readouterr().out) == [MENU, SETTING, ACTION]
    assert settings_player.commands == [(1, struct.pack("<H", i)) for i in range(4)]
    assert settings_player.reads == [0, 0, 0, 0, 1, 0, 0, 1, 0, 0]


def test_settings_get_and_set(settings_player, capsys):
    shell.execute("serial", "settings-get", "volume")
    output = capsys.readouterr().out
    assert '音量 (volume) = "-20"' in output
    assert "min=-74" in output and "unit=dB" in output
    shell.execute("serial", "settings-set", "volume", value="-19", json_output=True)
    assert json.loads(capsys.readouterr().out) == {**SETTING, "value": "-19"}
    assert settings_player.commands == [(2, b"volume"), (3, b"volume: -19")]


def test_settings_error_and_cfg_injection(settings_player):
    with pytest.raises(RuntimeError, match="settings-get: unknown cfg name"):
        shell.execute("serial", "settings-get", "missing")
    with pytest.raises(ValueError, match="single-line"):
        shell.execute("serial", "settings-set", "volume", value="-19\nfont: foo")
    with pytest.raises(ValueError, match="exceeds 704 bytes"):
        shell.execute("serial", "settings-set", "volume", value="音" * 233)
    assert settings_player.commands == [(2, b"missing")]


def test_settings_set_accepts_a_full_context_menu_mask(settings_player, capsys):
    cfg = "context hidden now playing"
    label = "Now Playing Menu"
    settings_player.rows.append({
        "kind": "action", "id": "0/3", "label": label, "cfg": cfg,
        "path": ["Settings", "Appearance", "Context Menus", label],
    })
    value = (
        "Shuffle, Repeat, Set Song Rating, Go to Album, Go to Artist, Go to Podcast, "
        "Go to Audiobook, Go to Author, Lyrics, Sleep Timer, Play Similar, Play with Spike, "
        "Queue, Add to Playlist..., +Theme Options, Show in Files, View Album Art, Wind Down, "
        "Bookmarks, Browse Cuesheet, Browse Chapters, Equalizer, Sound Settings, "
        "Playback Settings, Delete"
    )
    shell.execute("serial", "settings-set", cfg, value=value, json_output=True)
    assert json.loads(capsys.readouterr().out)["value"] == value
    assert settings_player.commands == [(3, f"{cfg}: {value}".encode("utf-8"))]


def test_settings_get_supports_action_backed_settings(settings_player, capsys):
    row = {**ACTION, "cfg": "scan roots", "value": "/Music", "writable": False}
    settings_player.rows.append(row)
    shell.execute("serial", "settings-get", "scan roots", json_output=True)
    assert json.loads(capsys.readouterr().out) == row


def test_settings_tree_prints_format_and_availability(settings_player, capsys):
    settings_player.rows[1] = {**SETTING, "format": "decimal"}
    settings_player.rows[2] = {**ACTION, "visible": False, "available": False}
    shell.execute("serial", "settings-tree")
    output = capsys.readouterr().out
    assert "format=decimal" in output
    assert "[hidden] [unavailable]" in output


@pytest.mark.parametrize("selector", [ACTION["id"], "update database",
                                     " / ".join(ACTION["path"])])
def test_settings_action_confirmation_and_selectors(settings_player, capsys, selector):
    with pytest.raises(ValueError, match="--yes"):
        shell.execute("serial", "settings-action", selector)
    assert not settings_player.commands
    shell.execute("serial", "settings-action", selector, yes=True, json_output=True)
    assert json.loads(capsys.readouterr().out) == {"status": "opened"}
    assert settings_player.commands[-1] == (4, b"\x01" + ACTION["id"].encode())


def test_settings_action_rejects_ambiguous_label(settings_player):
    settings_player.rows.append({**ACTION, "id": "0/2",
                                 "path": ["Settings", "Update Database"]})
    with pytest.raises(RuntimeError, match="Ambiguous Settings action"):
        shell.execute("serial", "settings-action", ACTION["label"], yes=True)
    assert all(value == 1 for value, _ in settings_player.commands)


def test_settings_reply_detects_replacement(settings_player, monkeypatch):
    transfer = settings_player.ctrl_transfer

    def replace(direction, request, value, index, data, *, timeout):
        result = transfer(direction, request, value, index, data, timeout=timeout)
        if direction == 0xC0 and value == 1:
            return bytes(4) + result[4:]
        return result

    monkeypatch.setattr(settings_player, "ctrl_transfer", replace)
    with pytest.raises(RuntimeError, match="changed during transfer"):
        shell.execute("serial", "settings-get", "volume")
    assert settings_player.commands == [(2, b"volume")]


@pytest.mark.parametrize("failure", ["timeout", "short write"])
def test_settings_set_does_not_resend_after_uncertain_write(settings_player, monkeypatch, failure):
    transfer = settings_player.ctrl_transfer

    def interrupted(direction, request, value, index, data, *, timeout):
        result = transfer(direction, request, value, index, data, timeout=timeout)
        assert direction == 0x40
        if failure == "timeout":
            raise shell.usb.core.USBError("timed out", errno=errno.ETIMEDOUT)
        return result - 1

    monkeypatch.setattr(settings_player, "ctrl_transfer", interrupted)
    with pytest.raises((RuntimeError, OSError), match="timed out|Short"):
        shell.execute("serial", "settings-set", "volume", value="-19")
    assert settings_player.commands == [(3, b"volume: -19")]


def test_settings_cli_options_and_signed_value(settings_player, monkeypatch, capsys):
    monkeypatch.setattr(shell.sys, "argv", [
        "podlink_shell.py", "--serial", "serial", "settings-set", "volume", "--json", "-19",
    ])
    shell.main()
    assert json.loads(capsys.readouterr().out)["value"] == "-19"


def test_settings_cli_requires_full_confirmation_option(settings_player, monkeypatch):
    monkeypatch.setattr(shell.sys, "argv", [
        "podlink_shell.py", "--serial", "serial", "settings-action", ACTION["label"], "--y",
    ])
    with pytest.raises(SystemExit) as error:
        shell.main()
    assert error.value.code == 2
    assert not settings_player.commands


class PlaybackPlayer(Player):
    def __init__(self):
        super().__init__(1)
        self.operation = 0
        self.error = 0
        self.path = "/Music/世界.m4a".encode()

    def ctrl_transfer(self, direction, request, value, index, data, *, timeout):
        if index != 0x4954:
            return super().ctrl_transfer(direction, request, value, index, data, timeout=timeout)
        assert (request, timeout) == (0x5D, 1000)
        if direction == 0x40:
            self.commands.append((value, data))
            self.operation = value
            return len(data)
        assert (direction, value, data) == (0xC0, 0, 328)
        return struct.pack("<IIIi6Ii24s260s", 1, len(self.commands), self.operation,
                           self.error, 1, 6237, 50000, 21, 48000, 24, 0, b"ALAC",
                           self.path)


def test_playback_commands_and_status(monkeypatch, capsys):
    player = PlaybackPlayer()
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda seconds: None)
    shell.execute("serial", "play", player.path.decode(), start_ms=1000)
    shell.execute("serial", "open-lyrics")
    shell.execute("serial", "seek", "6237")
    shell.execute("serial", "next")
    shell.execute("serial", "prev")
    assert player.commands == [
        (1, struct.pack("<I", 1000) + player.path),
        (5, b""), (2, struct.pack("<I", 6237)), (3, b""), (4, b""),
    ]
    capsys.readouterr()
    shell.execute("serial", "playback-status", json_output=True)
    status = json.loads(capsys.readouterr().out)
    assert (status["state"], status["path"], status["codec"]) == (
        "playing", player.path.decode(), "ALAC"
    )
    assert (status["elapsed_ms"], status["length_ms"], status["frequency_hz"],
            status["bits_per_sample"], status["playback_error"]) == (6237, 50000, 48000, 24, 0)


def test_playback_validation_and_firmware_error(monkeypatch):
    player = PlaybackPlayer()
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda seconds: None)
    with pytest.raises(ValueError, match="absolute iPod path"):
        shell.execute("serial", "play", "Music/track.m4a")
    with pytest.raises(ValueError, match="Playback position"):
        shell.execute("serial", "seek", "-1")
    assert not player.commands
    player.error = 2
    with pytest.raises(RuntimeError, match="play: file not found"):
        shell.execute("serial", "play", "/Music/absent.m4a")


def test_log_fetch_borrows_existing_download(monkeypatch):
    calls = []
    monkeypatch.setattr(shell, "record_fetch", lambda *args: calls.append(args))
    shell.execute("serial", "log-fetch", "/.rockbox/crumbs-last.txt", value="crumbs.txt")
    assert calls == [("serial", "/.rockbox/crumbs-last.txt", "crumbs.txt")]
