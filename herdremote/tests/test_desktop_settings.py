import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from serial.tools import list_ports

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import podlink
import podlink_settings as settings


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return dict(url="https://media.example.test/jellyfin", user_id="u1", user_name="听众",
                token="session-token", device_id="desktop-device")


@pytest.mark.parametrize("source", [None, {"kind": "all", "id": None},
                                    {"kind": "library", "id": "f1"},
                                    {"kind": "playlist", "id": "p1"}])
def test_jellyfin_session_round_trip_and_clear(session, source):
    assert settings.load_jellyfin_session() is None
    if source is not None:
        session["source"] = source
    path = settings.settings_path().with_name(settings.JELLYFIN_SESSION)
    path.parent.mkdir(mode=0o755)
    settings.save_jellyfin_session({**session, "password": "do-not-store"})
    assert settings.load_jellyfin_session() == session
    assert json.loads(path.read_text()) == session
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    settings.clear_jellyfin_session()
    settings.clear_jellyfin_session()
    assert settings.load_jellyfin_session() is None


@pytest.mark.parametrize("data", [b"{", b"\xff", b"[]", b'{"url":"https://example.test"}'])
def test_invalid_jellyfin_session_reads_empty(session, data):
    path = settings.settings_path().with_name(settings.JELLYFIN_SESSION)
    settings.save_jellyfin_session(session)
    path.write_bytes(data)
    assert settings.load_jellyfin_session() is None


def test_invalid_jellyfin_save_keeps_previous_session(session):
    settings.save_jellyfin_session(session)
    for invalid in [dict(url="http://insecure.example.test"), dict(token=""),
                    dict(source={"kind": "playlist", "id": None})]:
        with pytest.raises(ValueError):
            settings.save_jellyfin_session({**session, **invalid})
    assert settings.load_jellyfin_session() == session
    path = settings.settings_path().with_name(settings.JELLYFIN_SESSION)
    path.chmod(0o644)
    assert settings.load_jellyfin_session() is None


def test_detect_ipod_port_prefers_matching_by_id(tmp_path, monkeypatch):
    tty = tmp_path / "ttyACM0"
    tty.touch()
    stable = tmp_path / "apple"
    stable.symlink_to(tty)
    unrelated = tmp_path / "other"
    unrelated.symlink_to(tmp_path / "ttyACM1")
    paths = [unrelated, stable]
    glob = Path.glob
    monkeypatch.setattr(Path, "glob",
                        lambda path, pattern: iter(paths) if str(path) == "/dev/serial/by-id"
                        else glob(path, pattern))
    port = SimpleNamespace(device=str(tty), vid=podlink.VID, pid=podlink.PID)
    ports = [port]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    assert settings.detect_ipod_port() == str(stable)
    paths.clear()
    assert settings.detect_ipod_port() == str(tty)
    ports.append(SimpleNamespace(device="/dev/ttyACM2", vid=port.vid, pid=port.pid))
    assert settings.detect_ipod_port() is None
    ports.clear()
    assert settings.detect_ipod_port() is None
