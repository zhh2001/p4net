"""Sanity checks for the session-level cleanup-fixture regexes."""

from __future__ import annotations

from tests._artifact_patterns import TEST_IFACE_PATTERN, TEST_NS_PATTERN


def test_ns_pattern_matches_expected_test_names() -> None:
    # phase-1 runtime integration
    assert TEST_NS_PATTERN.match("ns12345678")
    assert TEST_NS_PATTERN.match("nsA_12345678")
    assert TEST_NS_PATTERN.match("nsB_deadbeef")
    # phase-6 e2e: 6-hex with optional a/b/c suffix
    assert TEST_NS_PATTERN.match("h123456")
    assert TEST_NS_PATTERN.match("s123456")
    assert TEST_NS_PATTERN.match("habcdef")
    assert TEST_NS_PATTERN.match("habcdefa")
    assert TEST_NS_PATTERN.match("sabcdefb")
    assert TEST_NS_PATTERN.match("sabcdefc")


def test_ns_pattern_rejects_user_names() -> None:
    # Short user names — must NOT be deleted.
    assert not TEST_NS_PATTERN.match("hostname")
    assert not TEST_NS_PATTERN.match("h1")
    assert not TEST_NS_PATTERN.match("s1")
    assert not TEST_NS_PATTERN.match("switch0")
    # Lower-case hex prefix without the right shape.
    assert not TEST_NS_PATTERN.match("ns1234")
    assert not TEST_NS_PATTERN.match("h12345")  # only 5 hex chars
    assert not TEST_NS_PATTERN.match("h12345678a")  # 8 hex + suffix is too long
    # Capital-letter prefix anywhere other than the documented `nsA_`/`nsB_`.
    assert not TEST_NS_PATTERN.match("NS12345678")


def test_iface_pattern_matches_expected_iface_names() -> None:
    # phase-1 veth ifaces
    assert TEST_IFACE_PATTERN.match("vA_12345678")
    assert TEST_IFACE_PATTERN.match("vB_12345678")
    assert TEST_IFACE_PATTERN.match("vC_deadbeef")
    assert TEST_IFACE_PATTERN.match("vD_cafebabe")
    # phase-6 host/switch veth ifaces
    assert TEST_IFACE_PATTERN.match("h123456-eth0")
    assert TEST_IFACE_PATTERN.match("h123456a-eth0")
    assert TEST_IFACE_PATTERN.match("s123456-eth1")
    assert TEST_IFACE_PATTERN.match("s123456b-eth42")


def test_iface_pattern_rejects_real_ifaces() -> None:
    assert not TEST_IFACE_PATTERN.match("lo")
    assert not TEST_IFACE_PATTERN.match("eth0")
    assert not TEST_IFACE_PATTERN.match("docker0")
    assert not TEST_IFACE_PATTERN.match("wlan0")
    assert not TEST_IFACE_PATTERN.match("h1-eth0")  # too short hex prefix
