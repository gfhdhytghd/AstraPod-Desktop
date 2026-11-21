import base64
from contextlib import nullcontext
from datetime import date
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import hragent
import hrdesktop
import hrproto
import hrt3


def token(*scopes):
    claims = dict(kind="session", method="bearer-access-token",
                  sid="4071c2ab-e1ba-4d82-aef3-2f6bbf68b699", scopes=scopes)
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return payload + ".signature"


def exit_message(identity, value):
    return dict(_tag="Exit", requestId=identity, exit={"_tag": "Success", "value": value})


@pytest.mark.parametrize("control", ["herdr", "t3"])
@pytest.mark.parametrize("summary_supported", [True, False])
def test_usage_rpc_is_independent_of_agent_control(monkeypatch, tmp_path, control, summary_supported):
    runtime = tmp_path / "server-runtime.json"
    runtime.write_text(json.dumps(dict(port=3773, serverVersion="0.1.10",
                                       origin="http://127.0.0.1:3773")))
    client = hrt3.Client(runtime)
    assert hragent.config({"T3_CONTROL": "on"})[0] == "herdr"
    scopes = (*hrt3.AGENT_SCOPES, hrt3.USAGE_SCOPE) if summary_supported else hrt3.AGENT_SCOPES
    client.set_config(control == "t3", None, token(*scopes), None)
    reset = "2026-11-28T06:00:00Z"
    providers = [
        dict(driver="claudeAgent", instanceId="claudeAgent", usageLimits={"windows": [
            dict(kind="session", windowDurationMins=300, resetsAt=reset, usedPercent=19.5),
            dict(kind="weekly", usedPercent=90, modelIds=["special-model"]),
            dict(kind="weekly", usedPercent=25),
        ]}),
        dict(driver="codex", instanceId="codex", usageLimits={"windows": [
            dict(kind="session", windowDurationMins=60, usedPercent=42),
        ]}),
    ]
    totals = dict(uncachedInputTokens=12, cachedInputTokens=100, cacheCreationTokens=6,
                  outputTokens=3, reasoningTokens=2)
    summary = {"buckets": [
        dict(provider="claude", totals=totals, costUsd=.1234, unpricedRecords=0),
        dict(provider="codex", totals=totals, costUsd=0, unpricedRecords=1),
    ]}
    messages = [exit_message("0", {"providers": providers})]
    if summary_supported:
        messages.insert(0, exit_message("1", summary))
    socket = Mock()
    socket.recv.return_value = json.dumps(messages)
    connect = Mock(return_value=nullcontext(socket))
    monkeypatch.setattr(hrt3, "socket_connection", connect)

    rows = hrt3.read_usage(client.runtime, client.usage_token())
    connect.assert_called_once_with("ws://127.0.0.1:3773/ws?orchestrationProtocol=2",
                                    client.usage_token())
    requests = [json.loads(call.args[0]) for call in socket.send.call_args_list]
    assert requests[0]["tag"] == "server.getConfig"
    assert len(requests) == (2 if summary_supported else 1)
    if summary_supported:
        assert requests[1]["tag"] == "server.getUsageSummary"
        payload = requests[1]["payload"]
        assert payload["timeZone"] == "UTC"
        assert (date.fromisoformat(payload["untilDay"])
                - date.fromisoformat(payload["sinceDay"])).days == 6
    assert not client.state()["connected"]
    desktop = [hrdesktop.usage_state(row) for row in rows]
    assert desktop[0]["five_hour"] == dict(percent=None, resets_at=None)
    assert desktop[0]["cost_usd"] is None
    assert desktop[1]["five_hour"] == dict(percent=19.5, resets_at=hrt3.epoch(reset))
    assert desktop[1]["weekly"] == dict(percent=25, resets_at=None)
    assert desktop[1]["tokens_week"] == (121 if summary_supported else None)
    assert desktop[1]["cost_usd"] == (.123 if summary_supported else None)
    assert desktop[2]["tokens_week"] is None
    wire = hrproto.decode(hrproto.usage(rows, hrt3.epoch("2026-11-27T06:00:00Z")))
    assert wire[2][3:7] == ["19", "86400", "25", "-"]
    assert wire[-1] == (["N", "242", "123"] if summary_supported else ["N", "-", "-"])


def test_usage_connection_failure_replaces_stale_player_and_desktop_fields(monkeypatch):
    client = hrt3.Client()
    client.set_config(False, None, token(*hrt3.AGENT_SCOPES), None)
    monkeypatch.setattr(hrt3, "read_usage", Mock(side_effect=ConnectionRefusedError))
    usage = hrt3.Usage(client)
    try:
        assert usage.request()
        usage.job[1].result(timeout=1)
        rows = usage.snapshot()
        assert all(hrdesktop.usage_state(row)["status"] == "T3 unavailable" for row in rows)
        frame = hrproto.decode(hrproto.usage(rows, hrt3.time.time(), replace_errors=True))
        assert frame[0] == ["U", "0", "1"]
        assert all(row[2:] == ["err", "-", "-", "-", "-", "-", "-", "T3 unavailable"]
                   for row in frame[1:-1])
        assert frame[-1] == ["N", "-", "-"]
    finally:
        usage.close()
