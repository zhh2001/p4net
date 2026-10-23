"""Ping count and success semantics under deterministic IPv4/IPv6 packet loss."""

from __future__ import annotations

import os
import re
import signal
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from p4net import Network
from p4net.cli import CommandDispatcher
from p4net.runtime import NSProcess
from p4net.topo import Topology

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ipv6", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_ping_loss_respects_count_and_any_reply(tmp_path: Path, ipv6: bool, partial: bool) -> None:
    name = "h" + uuid.uuid4().hex[:6]
    topo = Topology()
    topo.add_host(name)
    target, protocol, offset, echo = (
        ("::1", "ipv6", 40, 128) if ipv6 else ("127.0.0.1", "ip", 20, 8)
    )
    with Network(topo, log_dir=tmp_path / "logs") as net:
        host = net.host(name)
        host.exec(["tc", "qdisc", "replace", "dev", "lo", "clsact"])
        filter_command = ["tc", "filter", "add", "dev", "lo", "egress", "protocol", protocol]
        echo_match = ["u32", "match", "u8", str(echo), "0xff", "at", str(offset)]
        if partial:
            host.exec(
                [
                    *filter_command,
                    "pref",
                    "1",
                    *echo_match,
                    "match",
                    "u16",
                    "1",
                    "0xffff",
                    "at",
                    str(offset + 6),
                    "action",
                    "pass",
                ]
            )
            # A delayed first reply makes ping's final wait exceed our process deadline.
            host.exec(["tc", "qdisc", "replace", "dev", "lo", "root", "netem", "delay", "800ms"])
        host.exec([*filter_command, "pref", "2", *echo_match, "action", "drop"])
        outputs: list[bytes] = []
        processes: list[NSProcess] = []
        original_popen = host.namespace.popen
        signals: list[int] = []

        def capture_popen(argv: list[str], **kwargs: object) -> NSProcess:
            kwargs["env"] = dict(os.environ, LC_ALL="C")
            process = original_popen(argv, **kwargs)  # type: ignore[arg-type]
            communicate, send_signal = process.communicate, process.send_signal

            def capture_signal(signum: int) -> None:
                signals.append(signum)
                send_signal(signum)

            process.send_signal = capture_signal  # type: ignore[method-assign]

            def capture_output(
                *args: object, **options: object
            ) -> tuple[bytes | None, bytes | None]:
                stdout, stderr = communicate(*args, **options)  # type: ignore[arg-type]
                outputs.append(stdout or b"")
                return stdout, stderr

            process.communicate = capture_output  # type: ignore[method-assign]
            processes.append(process)
            return process

        with patch.object(host.namespace, "popen", side_effect=capture_popen):
            api_result = net.ping(name, target, count=3, timeout=0.25)
            verb = "ping6" if ipv6 else "ping"
            cli_result = CommandDispatcher(net).dispatch(f"{name} {verb} {target} 3 0.25")
        assert len(outputs) == 2
        for output in outputs:
            assert re.search(
                rb"3 packets transmitted, "
                + (b"1" if partial else b"0")
                + rb" (?:packets )?received",
                output,
            ), output
        assert api_result is partial
        assert cli_result == ("OK" if partial else "FAIL")
        assert all(process.poll() is not None for process in processes)
        assert signals == ([signal.SIGINT, signal.SIGINT] if partial else [])


@pytest.mark.parametrize("ipv6", [False, True])
def test_ping_zero_timeout_is_bounded_and_reaped(tmp_path: Path, ipv6: bool) -> None:
    name = "h" + uuid.uuid4().hex[:6]
    topo = Topology()
    topo.add_host(name)
    with Network(topo, log_dir=tmp_path / "logs") as net:
        host = net.host(name)
        host.exec(["tc", "qdisc", "replace", "dev", "lo", "root", "netem", "loss", "100%"])
        start = time.monotonic()
        with patch.object(
            NSProcess, "send_signal", autospec=True, side_effect=NSProcess.send_signal
        ) as stop:
            assert host.ping("::1" if ipv6 else "127.0.0.1", timeout=0) is False
        assert time.monotonic() - start < 5
        stop.assert_called_once()
        process, signum = stop.call_args.args
        assert signum == signal.SIGINT
        assert process.poll() is not None
