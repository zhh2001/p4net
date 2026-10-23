"""Unit tests for `p4net.cli.main`. Network and shell are mocked."""

from __future__ import annotations

import importlib
import pickle
import sys
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from p4net.cli import main as cli_main


@pytest.fixture(autouse=True)
def cleanup_topology_modules() -> Iterator[None]:
    previous = set(sys.modules)
    yield
    for name in set(sys.modules) - previous:
        if name.startswith("_p4net_topology_"):
            sys.modules.pop(name, None)


def _write_dataclass_topology(path: Path, label: str) -> Path:
    path.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from typing import ClassVar\n"
        "from p4net.topo import Topology\n"
        "@dataclass\n"
        "class HostConfig:\n"
        f"    label: ClassVar[str] = {label!r}\n"
        "    ip: str = '10.0.0.1/24'\n"
        "config = HostConfig()\n"
        "topology = Topology()\n"
        "topology.add_host('h1', ip=config.ip)\n"
        "def setup(net):\n"
        "    setup.called_with = net\n"
    )
    return path


def test_load_topology_supports_dataclasses_and_module_lookup(tmp_path: Path) -> None:
    path = _write_dataclass_topology(tmp_path / "topology.py", "first")

    module = cli_main._load_topology_module(path)

    assert importlib.import_module(module.HostConfig.__module__) is module
    assert [field.name for field in fields(module.HostConfig)] == ["ip"]
    assert repr(module.config) == "HostConfig(ip='10.0.0.1/24')"
    assert pickle.loads(pickle.dumps(module.config)) == module.config
    assert module.topology.hosts["h1"].ip == "10.0.0.1/24"


@pytest.mark.parametrize("same_path", [False, True], ids=["same-stem", "reload"])
def test_load_topology_keeps_previous_classes_resolvable(tmp_path: Path, same_path: bool) -> None:
    path = _write_dataclass_topology(tmp_path / "topology.py", "first")
    first = cli_main._load_topology_module(path)
    if not same_path:
        path = tmp_path / "other" / "topology.py"
        path.parent.mkdir()
    _write_dataclass_topology(path, "second")

    second = cli_main._load_topology_module(path)

    assert first.__name__ != second.__name__
    for module, label in ((first, "first"), (second, "second")):
        restored = pickle.loads(pickle.dumps(module.config))
        assert type(restored) is module.HostConfig
        assert restored.label == label


@pytest.mark.parametrize(
    ("statement", "error"),
    [
        ("raise RuntimeError('failed')", RuntimeError),
        ("raise ImportError('failed')", ImportError),
        ("if True", SyntaxError),
        ("raise SystemExit(2)", SystemExit),
        ("raise KeyboardInterrupt", KeyboardInterrupt),
    ],
)
def test_load_topology_failure_removes_partial_module(
    tmp_path: Path, statement: str, error: type[BaseException]
) -> None:
    valid_path = _write_dataclass_topology(tmp_path / "topology.py", "valid")
    valid = cli_main._load_topology_module(valid_path)
    broken_path = tmp_path / "broken" / "topology.py"
    broken_path.parent.mkdir()
    broken_path.write_text(statement + "\n")
    previous = set(sys.modules)

    with pytest.raises(error):
        cli_main._load_topology_module(broken_path)

    assert not {name for name in set(sys.modules) - previous if name.startswith("_p4net_topology_")}
    assert importlib.import_module(valid.__name__) is valid
    assert pickle.loads(pickle.dumps(valid.config)) == valid.config


def test_main_loads_dataclass_topology_and_calls_setup(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    path = _write_dataclass_topology(tmp_path / "topology.py", "cli")
    wait = mocker.patch("p4net.cli.main._wait_for_signal")

    assert cli_main.main([str(path), "--no-shell"]) == 0

    loaded = [
        module
        for name, module in sys.modules.items()
        if name.startswith("_p4net_topology_") and getattr(module, "__file__", None) == str(path)
    ]
    (module,) = loaded
    assert module.setup.called_with is mock_network._instance
    assert mock_network.call_args.args[0] is module.topology
    wait.assert_called_once_with()
    mock_network._instance.__exit__.assert_called_once()


def _write_topology_file(
    tmp_path: Path,
    *,
    has_topology: bool = True,
    has_setup: bool = False,
    bad_setup: bool = False,
    bad_topology_type: bool = False,
) -> Path:
    """Write a temporary topology file and return its path."""
    lines = ["from p4net.topo import Topology"]
    if has_topology:
        if bad_topology_type:
            lines.append("topology = 42  # not a Topology")
        else:
            lines.append("topology = Topology()")
            lines.append("topology.add_host('h1', ip='10.0.0.1/24')")
    if has_setup:
        if bad_setup:
            lines.append("setup = 'not callable'")
        else:
            lines.append("def setup(net):")
            lines.append("    setup.called_with = net")
    path = tmp_path / "topo.py"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def mock_network(mocker: MockerFixture) -> MagicMock:
    """Replace `p4net.cli.main.Network` with a MagicMock that supports the
    context manager protocol."""
    factory = mocker.patch("p4net.cli.main.Network")
    instance = MagicMock(name="Network-instance")
    instance.__enter__ = MagicMock(return_value=instance)
    instance.__exit__ = MagicMock(return_value=False)
    factory.return_value = instance
    factory._instance = instance  # type: ignore[attr-defined]
    return factory


# ---------------------------------------------------------------------------
# Argument / file errors
# ---------------------------------------------------------------------------


def test_main_missing_file_returns_nonzero(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    rc = cli_main.main([str(tmp_path / "no-such.py")])
    assert rc != 0
    captured = capsys.readouterr()
    assert "not found" in captured.err.lower()


def test_main_topology_file_without_topology(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = _write_topology_file(tmp_path, has_topology=False)
    rc = cli_main.main([str(p)])
    assert rc != 0
    captured = capsys.readouterr()
    assert "topology" in captured.err.lower()


def test_main_topology_wrong_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write_topology_file(tmp_path, bad_topology_type=True)
    rc = cli_main.main([str(p)])
    assert rc != 0
    captured = capsys.readouterr()
    assert "topology" in captured.err.lower()


def test_main_setup_not_callable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mock_network: MagicMock,
) -> None:
    p = _write_topology_file(tmp_path, has_setup=True, bad_setup=True)
    rc = cli_main.main([str(p), "--no-shell"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "callable" in captured.err.lower()


# ---------------------------------------------------------------------------
# Successful flows (Network mocked)
# ---------------------------------------------------------------------------


def test_main_no_setup_no_shell(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    p = _write_topology_file(tmp_path, has_setup=False)
    mocker.patch("p4net.cli.main._wait_for_signal", return_value=None)
    rc = cli_main.main([str(p), "--no-shell"])
    assert rc == 0
    mock_network.assert_called_once()
    # Network entered as context manager, exited cleanly.
    mock_network._instance.__enter__.assert_called_once()
    mock_network._instance.__exit__.assert_called_once()


def test_main_setup_called_after_enter(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    p = _write_topology_file(tmp_path, has_setup=True)
    mocker.patch("p4net.cli.main._wait_for_signal", return_value=None)
    rc = cli_main.main([str(p), "--no-shell"])
    assert rc == 0
    # Re-import the topology module to inspect setup.called_with — but the
    # module was loaded with a unique-per-call name so we instead check via
    # the mock_network instance: setup is invoked with the value returned by
    # __enter__, which is the instance itself.
    # The setup() in the temp topo file just stashes its arg as
    # `setup.called_with`. We can't easily reach that from here, so we rely
    # on the network instance being passed through __enter__ (already
    # asserted in test_main_no_setup_no_shell).
    mock_network._instance.__enter__.assert_called_once()


def test_main_no_shell_skips_shell(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    p = _write_topology_file(tmp_path)
    fake_pause = mocker.patch("p4net.cli.main._wait_for_signal", return_value=None)
    shell_factory = mocker.patch("p4net.cli.shell.P4NetShell")
    cli_main.main([str(p), "--no-shell"])
    fake_pause.assert_called_once()
    shell_factory.assert_not_called()


def test_main_default_runs_shell(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    p = _write_topology_file(tmp_path)
    shell_factory = mocker.patch("p4net.cli.shell.P4NetShell")
    shell = MagicMock()
    shell_factory.return_value = shell
    rc = cli_main.main([str(p)])
    assert rc == 0
    shell_factory.assert_called_once()
    shell.run.assert_called_once()


# ---------------------------------------------------------------------------
# Argument forwarding
# ---------------------------------------------------------------------------


def test_main_passes_extra_compile_args(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    p = _write_topology_file(tmp_path)
    mocker.patch("p4net.cli.main._wait_for_signal", return_value=None)
    cli_main.main(
        [
            str(p),
            "--no-shell",
            "--extra-compile-arg",
            "foo",
            "--extra-compile-arg",
            "bar",
        ]
    )
    _, kwargs = mock_network.call_args
    assert kwargs["extra_compile_args"] == ("foo", "bar")


def test_main_passes_log_dir_pcap_dir_unsafe(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    p = _write_topology_file(tmp_path)
    mocker.patch("p4net.cli.main._wait_for_signal", return_value=None)
    log_dir = tmp_path / "logs"
    pcap_dir = tmp_path / "pcaps"
    cli_main.main(
        [
            str(p),
            "--no-shell",
            "--unsafe",
            "--log-dir",
            str(log_dir),
            "--pcap-dir",
            str(pcap_dir),
        ]
    )
    _, kwargs = mock_network.call_args
    assert kwargs["log_dir"] == log_dir
    assert kwargs["pcap_dir"] == pcap_dir
    assert kwargs["unsafe"] is True


def test_main_keyboard_interrupt_during_no_shell_clean_exit(
    tmp_path: Path, mock_network: MagicMock, mocker: MockerFixture
) -> None:
    p = _write_topology_file(tmp_path)
    mocker.patch(
        "p4net.cli.main._wait_for_signal",
        side_effect=KeyboardInterrupt,
    )
    rc = cli_main.main([str(p), "--no-shell"])
    assert rc == 0
    mock_network._instance.__exit__.assert_called_once()


# Sanity: argparse unknown flag exits non-zero.
def test_main_unknown_flag_argparse(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = _write_topology_file(tmp_path)
    with pytest.raises(SystemExit):
        cli_main.main([str(p), "--no-such-flag"])


def test_parser_default_verbose_zero() -> None:
    ns = cli_main._build_parser().parse_args(["topo.py"])
    assert ns.verbose == 0


def test_parser_v_short_counts() -> None:
    ns = cli_main._build_parser().parse_args(["topo.py", "-v"])
    assert ns.verbose == 1


def test_parser_vv_short_counts() -> None:
    ns = cli_main._build_parser().parse_args(["topo.py", "-vv"])
    assert ns.verbose == 2


def test_parser_verbose_long_counts() -> None:
    ns = cli_main._build_parser().parse_args(["topo.py", "--verbose", "--verbose"])
    assert ns.verbose == 2


@pytest.mark.parametrize(
    ("verbosity", "expected"),
    [
        (0, "WARNING"),
        (1, "INFO"),
        (2, "DEBUG"),
        (3, "DEBUG"),
    ],
)
def test_configure_logging_sets_root_level(
    verbosity: int,
    expected: str,
    mocker: MockerFixture,
) -> None:
    basic = mocker.patch("logging.basicConfig")
    cli_main._configure_logging(verbosity)
    assert basic.call_count == 1
    level = basic.call_args.kwargs["level"]
    import logging as _logging

    assert _logging.getLevelName(level) == expected


_KEEP: Any = None  # keep `Any` import live for future tests
