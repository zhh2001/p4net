"""Unit tests for `p4net.topo.topology.Topology`."""

from __future__ import annotations

from pathlib import Path

import pytest

from p4net.topo import (
    Host,
    Link,
    LinkEndpoint,
    P4Switch,
    Topology,
    TopologyError,
)

# ---------------------------------------------------------------------------
# Builder happy paths
# ---------------------------------------------------------------------------


def test_empty_topology() -> None:
    t = Topology()
    assert t.hosts == {}
    assert t.switches == {}
    assert list(t.links) == []


def test_add_host_returns_host() -> None:
    t = Topology()
    h = t.add_host("h1", ip="10.0.0.1/24", mac="aa:bb:cc:dd:ee:ff")
    assert isinstance(h, Host)
    assert t.hosts["h1"] is h


def test_add_switch_returns_switch() -> None:
    t = Topology()
    sw = t.add_switch("s1", Path("p.p4"))
    assert isinstance(sw, P4Switch)
    assert t.switches["s1"] is sw


def test_add_link_returns_link() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    link = t.add_link("h1", "s1")
    assert isinstance(link, Link)
    assert link.a.node == "h1"
    assert link.b.node == "s1"
    assert list(t.links) == [link]


def test_add_link_accepts_node_objects() -> None:
    t = Topology()
    h = t.add_host("h1")
    sw = t.add_switch("s1", Path("p.p4"))
    link = t.add_link(h, sw)
    assert link.a.node == "h1"
    assert link.b.node == "s1"


def test_node_lookup() -> None:
    t = Topology()
    h = t.add_host("h1")
    sw = t.add_switch("s1", Path("p.p4"))
    assert t.node("h1") is h
    assert t.node("s1") is sw


def test_node_unknown_raises() -> None:
    t = Topology()
    with pytest.raises(TopologyError, match="no node named"):
        t.node("missing")


def test_duplicate_name_rejected() -> None:
    t = Topology()
    t.add_host("h1")
    with pytest.raises(TopologyError, match="already exists"):
        t.add_host("h1")
    with pytest.raises(TopologyError, match="already exists"):
        t.add_switch("h1", Path("p.p4"))


def test_resolve_rejects_foreign_host_object() -> None:
    t1 = Topology()
    h = t1.add_host("h1")
    t2 = Topology()
    t2.add_host("h1")  # same name, different object
    with pytest.raises(TopologyError, match="not part of"):
        t2.add_link(h, "h1")  # foreign Host object


def test_resolve_rejects_foreign_switch_object() -> None:
    t1 = Topology()
    sw = t1.add_switch("s1", Path("p.p4"))
    t2 = Topology()
    t2.add_host("h1")
    t2.add_switch("s1", Path("p.p4"))
    with pytest.raises(TopologyError, match="not part of"):
        t2.add_link("h1", sw)


def test_resolve_rejects_bad_type() -> None:
    t = Topology()
    t.add_host("h1")
    with pytest.raises(TopologyError, match="must be a name string"):
        t.add_link(42, "h1")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Auto-assignment correctness
# ---------------------------------------------------------------------------


def test_switch_device_id_grpc_thrift_auto_assigned() -> None:
    t = Topology()
    s1 = t.add_switch("s1", Path("p.p4"))
    s2 = t.add_switch("s2", Path("p.p4"))
    assert s1.device_id == 0
    assert s2.device_id == 1
    assert s1.grpc_port == 50051
    assert s2.grpc_port == 50052
    assert s1.thrift_port == 9090
    assert s2.thrift_port == 9091


def test_switch_explicit_values_respected() -> None:
    t = Topology()
    s = t.add_switch("s1", Path("p.p4"), device_id=42, grpc_port=60000, thrift_port=60001)
    assert s.device_id == 42
    assert s.grpc_port == 60000
    assert s.thrift_port == 60001


def test_link_switch_port_starts_at_one() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    link = t.add_link("h1", "s1")
    # h1 is host, port starts at 0; s1 is switch, port starts at 1
    assert link.a.port == 0
    assert link.b.port == 1


def test_link_port_auto_increments() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_host("h2")
    t.add_switch("s1", Path("p.p4"))
    l1 = t.add_link("h1", "s1")
    l2 = t.add_link("h2", "s1")
    assert l1.b.port == 1
    assert l2.b.port == 2
    assert l1.a.port == 0
    assert l2.a.port == 0  # different host, restarts


def test_link_explicit_ports_respected() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    link = t.add_link("h1", "s1", port_a=5, port_b=42)
    assert link.a.port == 5
    assert link.b.port == 42


@pytest.mark.parametrize("cpu_port", [1, 2, 510])
@pytest.mark.parametrize("switch_first", [False, True])
def test_auto_ports_skip_cpu_port_and_survive_round_trip(cpu_port: int, switch_first: bool) -> None:
    topo = Topology()
    topo.add_switch("s1", Path("p.p4"), cpu_port=cpu_port)
    expected = [port for port in range(1, 7) if port != cpu_port][:5]
    for index, port in enumerate(expected[:4]):
        host = topo.add_host(f"h{index}")
        link = topo.add_link("s1", host) if switch_first else topo.add_link(host, "s1")
        switch, host_endpoint = (link.a, link.b) if switch_first else (link.b, link.a)
        assert switch.port == port
        assert host_endpoint.port == 0
    topo.validate()
    restored = Topology.from_dict(topo.to_dict())
    restored.validate()
    restored.add_host("h4")
    assert restored.add_link("h4", "s1").b.port == expected[4]


@pytest.mark.parametrize("cpu_port", [1, 255, 510])
@pytest.mark.parametrize("switch_first", [False, True])
def test_explicit_cpu_port_rejected_without_adding_link(cpu_port: int, switch_first: bool) -> None:
    topo = Topology()
    topo.add_host("h1")
    topo.add_switch("s1", Path("p.p4"), cpu_port=cpu_port)
    with pytest.raises(TopologyError, match="CPU port"):
        if switch_first:
            topo.add_link("s1", "h1", port_a=cpu_port)
        else:
            topo.add_link("h1", "s1", port_b=cpu_port)
    assert topo.links == ()
    assert topo.add_link("h1", "s1").b.port == (2 if cpu_port == 1 else 1)


@pytest.mark.parametrize("side", ["a", "b"])
def test_switch_link_rejects_either_cpu_port(side: str) -> None:
    topo = Topology()
    topo.add_switch("s1", Path("p.p4"), cpu_port=1)
    topo.add_switch("s2", Path("p.p4"), cpu_port=2)
    with pytest.raises(TopologyError, match="CPU port"):
        topo.add_link("s1", "s2", port_a=1 if side == "a" else 3, port_b=2 if side == "b" else 3)
    assert topo.links == ()


def test_switch_link_auto_ports_skip_each_switch_cpu_port() -> None:
    topo = Topology()
    topo.add_switch("s1", Path("p.p4"), cpu_port=1)
    topo.add_switch("s2", Path("p.p4"), cpu_port=2)
    first = topo.add_link("s1", "s2")
    second = topo.add_link("s1", "s2")
    assert (first.a.port, first.b.port) == (2, 1)
    assert (second.a.port, second.b.port) == (3, 3)
    topo.validate()


def test_cpu_port_is_reserved_only_on_its_switch() -> None:
    topo = Topology()
    topo.add_host("h1")
    topo.add_switch("s1", Path("p.p4"), cpu_port=1)
    topo.add_switch("s2", Path("p.p4"), cpu_port=2)
    link = topo.add_link("h1", "s1", port_a=1, port_b=2)
    assert (link.a.port, link.b.port) == (1, 2)
    topo.add_link("s1", "s2", port_a=3, port_b=1)
    topo.validate()


@pytest.mark.parametrize("switch_first", [False, True])
@pytest.mark.parametrize("source", ["dictionary", "direct"])
def test_validate_rejects_cpu_port_from_imported_or_direct_link(
    switch_first: bool, source: str
) -> None:
    topo = Topology()
    topo.add_host("h1")
    topo.add_switch("s1", Path("p.p4"), cpu_port=510)
    if switch_first:
        topo.add_link("s1", "h1", port_a=1)
    else:
        topo.add_link("h1", "s1", port_b=1)
    if source == "dictionary":
        data = topo.to_dict()
        data["links"][0]["a" if switch_first else "b"]["port"] = 510
        topo = Topology.from_dict(data)
    else:
        switch = LinkEndpoint(node="s1", port=510, iface_name="s1-eth510")
        host = LinkEndpoint(node="h1", port=0, iface_name="h1-eth0")
        topo._links[0] = Link(a=switch, b=host) if switch_first else Link(a=host, b=switch)
    with pytest.raises(TopologyError, match="CPU port"):
        topo.validate()


def test_iface_name_format() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    link = t.add_link("h1", "s1")
    assert link.a.iface_name == "h1-eth0"
    assert link.b.iface_name == "s1-eth1"


def test_iface_name_too_long_rejected() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("longswitch12", Path("p.p4"))  # 12 chars
    # "longswitch12-eth100" = 19 chars > 15
    with pytest.raises(TopologyError, match="exceeds 15 chars"):
        t.add_link("h1", "longswitch12", port_b=100)


# ---------------------------------------------------------------------------
# IP handling on links
# ---------------------------------------------------------------------------


def test_ip_on_switch_endpoint_rejected() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    with pytest.raises(TopologyError, match="P4 switch data ports do not carry IP"):
        t.add_link("h1", "s1", ip_b="10.0.0.2/24")
    with pytest.raises(TopologyError, match="remove ip_a"):
        t.add_link("s1", "h1", ip_a="10.0.0.2/24")


def test_link_level_ip_on_host_endpoint_kept() -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_switch("s1", Path("p.p4"))
    link = t.add_link("h1", "s1", ip_a="192.168.1.1/24")
    assert link.a.ip == "192.168.1.1/24"


def test_add_link_mac_overrides_populate_endpoints() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    link = t.add_link(
        "h1",
        "s1",
        mac_a="aa:bb:cc:dd:ee:01",
        mac_b="aa:bb:cc:dd:ee:02",
    )
    assert link.a.mac == "aa:bb:cc:dd:ee:01"
    assert link.b.mac == "aa:bb:cc:dd:ee:02"


def test_add_link_mac_allowed_on_switch_endpoint() -> None:
    """Unlike IP, MAC is allowed on switch endpoints (hint for veth side config)."""
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    link = t.add_link("h1", "s1", mac_b="aa:bb:cc:dd:ee:02")
    assert link.b.mac == "aa:bb:cc:dd:ee:02"


def test_add_link_invalid_mac_rejected() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    with pytest.raises(TopologyError, match="invalid LinkEndpoint MAC"):
        t.add_link("h1", "s1", mac_a="not-a-mac")


# ---------------------------------------------------------------------------
# neighbors_of / port_assignments
# ---------------------------------------------------------------------------


def test_neighbors_of_finds_links() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_host("h2")
    t.add_switch("s1", Path("p.p4"))
    l1 = t.add_link("h1", "s1")
    l2 = t.add_link("h2", "s1")
    n_s1 = t.neighbors_of("s1")
    assert len(n_s1) == 2
    nodes = {far.node for (_, near, far) in n_s1}
    assert nodes == {"h1", "h2"}
    # The near endpoint should always be on s1
    assert all(near.node == "s1" for (_, near, _) in n_s1)
    # h1 has only one neighbor link
    n_h1 = t.neighbors_of("h1")
    assert len(n_h1) == 1
    assert n_h1[0][0] is l1
    # And the order is preserved across multiple links from h2
    assert l2 in [link for (link, _, _) in t.neighbors_of("s1")]


def test_neighbors_of_unknown_returns_empty() -> None:
    t = Topology()
    assert t.neighbors_of("nope") == []


def test_port_assignments_maps_ports_to_far_endpoint() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_host("h2")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")  # s1 port 1
    t.add_link("h2", "s1")  # s1 port 2
    pa = t.port_assignments("s1")
    assert set(pa.keys()) == {1, 2}
    assert pa[1].node == "h1"
    assert pa[2].node == "h2"


def test_port_assignments_rejects_unknown_switch() -> None:
    t = Topology()
    with pytest.raises(TopologyError, match="no switch named"):
        t.port_assignments("missing")


def test_port_assignments_works_when_switch_is_link_b() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("s1", "h1")  # switch on side a
    t.add_link("h1", "s1", port_a=99)  # switch on side b
    pa = t.port_assignments("s1")
    assert "h1" in {ep.node for ep in pa.values()}


# ---------------------------------------------------------------------------
# validate() rules
# ---------------------------------------------------------------------------


def test_validate_clean_passes() -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_host("h2", ip="10.0.0.2/24")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    t.add_link("h2", "s1")
    t.validate()  # no raise


def test_validate_unknown_endpoint_node() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    # Stuff a link that references a phantom node by going under the API.
    t._links.append(Link(a=LinkEndpoint(node="ghost"), b=LinkEndpoint(node="s1")))
    with pytest.raises(TopologyError, match="unknown node 'ghost'"):
        t.validate()


def test_validate_self_loop() -> None:
    t = Topology()
    t.add_switch("s1", Path("p.p4"))
    t._links.append(Link(a=LinkEndpoint(node="s1", port=1), b=LinkEndpoint(node="s1", port=2)))
    with pytest.raises(TopologyError, match="connects node 's1' to itself"):
        t.validate()


def test_validate_port_collision() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_host("h2")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1", port_b=1)
    t.add_link("h2", "s1", port_b=1)  # collide
    with pytest.raises(TopologyError, match="port collision"):
        t.validate()


def test_validate_switch_field_collision_device_id() -> None:
    t = Topology()
    t.add_switch("s1", Path("p.p4"), device_id=5)
    t.add_switch("s2", Path("p.p4"), device_id=5)
    with pytest.raises(TopologyError, match="device_id collision"):
        t.validate()


def test_validate_switch_field_collision_grpc_port() -> None:
    t = Topology()
    t.add_switch("s1", Path("p.p4"), grpc_port=60000)
    t.add_switch("s2", Path("p.p4"), grpc_port=60000)
    with pytest.raises(TopologyError, match="grpc_port collision"):
        t.validate()


def test_validate_switch_field_collision_thrift_port() -> None:
    t = Topology()
    t.add_switch("s1", Path("p.p4"), thrift_port=60000)
    t.add_switch("s2", Path("p.p4"), thrift_port=60000)
    with pytest.raises(TopologyError, match="thrift_port collision"):
        t.validate()


@pytest.mark.parametrize(
    "ports",
    [
        [(60000, 60001), (60001, 60002)],
        [(60001, 60000), (60002, 60001)],
        [(60001, 60001)],
    ],
    ids=["thrift-then-grpc", "grpc-then-thrift", "same-switch"],
)
@pytest.mark.parametrize("roundtrip", [False, True])
def test_validate_service_port_collision(ports: list[tuple[int, int]], roundtrip: bool) -> None:
    t = Topology()
    for i, (grpc_port, thrift_port) in enumerate(ports, 1):
        t.add_switch(f"s{i}", Path("p.p4"), grpc_port=grpc_port, thrift_port=thrift_port)
    if roundtrip:
        t = Topology.from_dict(t.to_dict())

    with pytest.raises(TopologyError, match="service port collision") as error:
        t.validate()
    message = str(error.value)
    assert "60001" in message
    assert "grpc_port" in message and "thrift_port" in message
    for switch in t.switches:
        assert switch in message


def test_validate_service_port_collision_reports_other_errors() -> None:
    t = Topology()
    t.add_switch("s1", Path("p.p4"), device_id=5, grpc_port=60000, thrift_port=60001)
    t.add_switch("s2", Path("p.p4"), device_id=5, grpc_port=60001, thrift_port=60000)
    with pytest.raises(TopologyError) as error:
        t.validate()
    message = str(error.value)
    assert "device_id collision" in message
    assert message.count("service port collision") == 2
    assert "60000" in message and "60001" in message


def test_validate_service_ports_are_separate_from_device_ids() -> None:
    t = Topology()
    t.add_switch("s1", Path("p.p4"), device_id=60001, grpc_port=60000, thrift_port=60001)
    t.add_switch("s2", Path("p.p4"), device_id=60000, grpc_port=60002, thrift_port=60003)
    t.validate()


def test_validate_iface_name_too_long_in_existing_link() -> None:
    """If a link is constructed bypassing add_link, validate() catches the long iface."""
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    t._links.append(
        Link(
            a=LinkEndpoint(node="h1", port=0, iface_name="x" * 16),
            b=LinkEndpoint(node="s1", port=1, iface_name="s1-eth1"),
        )
    )
    with pytest.raises(TopologyError, match="exceeds 15 chars"):
        t.validate()


def test_validate_ip_collision_within_subnet() -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_host("h2", ip="10.0.0.1/24")  # same IP same subnet
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    t.add_link("h2", "s1")
    with pytest.raises(TopologyError, match="IP collision"):
        t.validate()


def test_validate_ip_no_collision_different_subnets() -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_host("h2", ip="192.168.1.1/24")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    t.add_link("h2", "s1")
    t.validate()  # different subnets → fine


def test_validate_ipv6_collision_within_subnet() -> None:
    t = Topology()
    t.add_host("h1", ip6="fd00::1/64")
    t.add_host("h2", ip6="fd00::1/64")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    t.add_link("h2", "s1")
    with pytest.raises(TopologyError, match="IPv6 collision"):
        t.validate()


def test_validate_collects_all_errors() -> None:
    """A topology with three distinct problems must surface all three."""
    t = Topology()
    t.add_host("h1")
    t.add_host("h2", ip="10.0.0.1/24")
    t.add_host("h3", ip="10.0.0.1/24")  # IP collision with h2
    t.add_switch("s1", Path("p.p4"), device_id=0)
    t.add_switch("s2", Path("p.p4"), device_id=0)  # device_id collision
    t.add_link("h2", "s1")
    t.add_link("h3", "s1", port_b=1)  # port collision on s1 port 1
    with pytest.raises(TopologyError) as excinfo:
        t.validate()
    msg = str(excinfo.value)
    assert "device_id collision" in msg
    assert "port collision" in msg
    assert "IP collision" in msg


def test_validate_link_with_invalid_ip_override() -> None:
    """Link-level IP override that fails to parse should be reported."""
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    # Inject a bad IP into an existing link's host endpoint.
    t._links[-1] = Link(
        a=LinkEndpoint(node="h1", port=0, iface_name="h1-eth0", ip="not-an-ip"),
        b=t._links[-1].b,
    )
    with pytest.raises(TopologyError, match="invalid IP"):
        t.validate()


# ---------------------------------------------------------------------------
# Multi-homed host
# ---------------------------------------------------------------------------


def test_multi_homed_host_with_link_override() -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24")
    t.add_switch("s1", Path("p.p4"))
    t.add_switch("s2", Path("p.p4"))
    t.add_link("h1", "s1")
    # Second uplink uses a different subnet on the same host.
    t.add_link("h1", "s2", ip_a="172.16.0.1/24")
    t.validate()  # no collision; different subnets


# ---------------------------------------------------------------------------
# arch rejection
# ---------------------------------------------------------------------------


def test_topology_rejects_non_v1model_arch() -> None:
    t = Topology()
    with pytest.raises(TopologyError, match="only 'v1model'"):
        t.add_switch("s1", Path("p.p4"), arch="psa")


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------


def _build_sample() -> Topology:
    t = Topology()
    t.add_host(
        "h1",
        ip="10.0.0.1/24",
        mac="aa:bb:cc:dd:ee:01",
        default_route="10.0.0.254",
        ip6="fd00::1/64",
        default_route6="fd00::ff",
    )
    t.add_host("h2", ip="10.0.0.2/24")
    t.add_switch("s1", Path("examples/basic.p4"), cpu_port=255)
    t.add_switch("s2", Path("examples/basic.p4"), pcap_enabled=False, log_level="debug")
    # Host endpoint MAC override + switch endpoint MAC override (round-trip coverage).
    t.add_link("h1", "s1", mac_a="aa:bb:cc:dd:ee:11", mac_b="aa:bb:cc:dd:ee:12")
    t.add_link(
        "h2",
        "s2",
        bandwidth="10mbit",
        delay="5ms",
        jitter="1ms",
        loss_pct=0.5,
        mtu=1450,
    )
    # Asymmetric impairment for round-trip coverage.
    t.add_link("s1", "s2", mac_b="aa:bb:cc:dd:ee:22", delay_a_to_b="20ms", loss_pct_b_to_a=2.5)
    return t


def test_round_trip_preserves_macs() -> None:
    t = _build_sample()
    payload = t.to_dict()
    t2 = Topology.from_dict(payload)
    # The first link in the sample has MAC overrides on both endpoints.
    first = t2.links[0]
    assert first.a.mac == "aa:bb:cc:dd:ee:11"
    assert first.b.mac == "aa:bb:cc:dd:ee:12"
    # And the third link has a switch-side MAC override.
    third = t2.links[2]
    assert third.b.mac == "aa:bb:cc:dd:ee:22"


def test_to_dict_from_dict_round_trip() -> None:
    t = _build_sample()
    payload = t.to_dict()
    t2 = Topology.from_dict(payload)
    assert t2.to_dict() == payload


def test_round_trip_preserves_validate_passing() -> None:
    t = _build_sample()
    t.validate()
    t2 = Topology.from_dict(t.to_dict())
    t2.validate()


def test_to_dict_serialises_path_as_string() -> None:
    t = Topology()
    t.add_switch("s1", Path("examples/basic.p4"))
    payload = t.to_dict()
    assert payload["switches"]["s1"]["p4_src"] == "examples/basic.p4"


def test_from_dict_handles_missing_optional_sections() -> None:
    t = Topology.from_dict({})
    assert t.hosts == {}
    assert t.switches == {}
    assert list(t.links) == []


def test_links_property_returns_immutable_view() -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    snapshot = t.links
    # Mutating the snapshot must not affect the topology.
    assert isinstance(snapshot, tuple)
    assert len(snapshot) == 1


# ---------------------------------------------------------------------------
# Graphviz (phase 13)
# ---------------------------------------------------------------------------


def test_to_graphviz_contains_every_node_and_edge() -> None:
    t = Topology()
    t.add_host("h1", ip="10.0.0.1/24", ip6="fd00::1/64")
    t.add_host("h2", ip="10.0.0.2/24")
    t.add_switch("s1", Path("p.p4"), grpc_port=50051)
    t.add_link("h1", "s1", port_b=1)
    t.add_link("h2", "s1", port_b=2)
    out = t.to_graphviz()
    assert out.startswith("digraph p4net")
    assert "rankdir=LR" in out
    assert '"h1"' in out
    assert '"h2"' in out
    assert '"s1"' in out
    assert "10.0.0.1/24" in out
    assert "fd00::1/64" in out
    assert "grpc :50051" in out
    assert '"h1" -> "s1" [arrowhead=none]' in out
    assert '"h2" -> "s1" [arrowhead=none]' in out


def test_to_graphviz_layout_TB() -> None:
    t = Topology()
    t.add_host("h1")
    out = t.to_graphviz(layout="TB")
    assert "rankdir=TB" in out


def test_to_graphviz_rejects_bad_layout() -> None:
    t = Topology()
    with pytest.raises(TopologyError, match="layout"):
        t.to_graphviz(layout="diagonal")


def test_render_graphviz_format_dot_writes_source(tmp_path: Path) -> None:
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    out_path = tmp_path / "topo.dot"
    t.render_graphviz(out_path, format="dot")
    text = out_path.read_text()
    assert text.startswith("digraph p4net")


def test_render_graphviz_invokes_dot(tmp_path: Path, mocker) -> None:
    import shutil
    import subprocess

    mocker.patch("shutil.which", return_value="/usr/bin/dot")
    fake_run = mocker.patch(
        "subprocess.run", return_value=subprocess.CompletedProcess([], returncode=0)
    )
    t = Topology()
    t.add_host("h1")
    t.add_switch("s1", Path("p.p4"))
    t.add_link("h1", "s1")
    out_path = tmp_path / "topo.png"
    t.render_graphviz(out_path, format="png")
    args = fake_run.call_args.args[0]
    assert args[0] == "/usr/bin/dot"
    assert args[1] == "-Tpng"
    assert args[2] == "-o"
    assert args[3] == str(out_path)
    # The DOT source is piped on stdin.
    kwargs = fake_run.call_args.kwargs
    assert kwargs["input"].startswith(b"digraph p4net")
    # Imported but referenced only via mocker.patch above.
    assert shutil.which is not None


def test_render_graphviz_missing_dot_raises(tmp_path: Path, mocker) -> None:
    mocker.patch("shutil.which", return_value=None)
    t = Topology()
    t.add_host("h1")
    with pytest.raises(TopologyError, match=r"dot.*not found"):
        t.render_graphviz(tmp_path / "topo.png", format="png")


def test_render_graphviz_subprocess_failure_wrapped(tmp_path: Path, mocker) -> None:
    import subprocess

    mocker.patch("shutil.which", return_value="/usr/bin/dot")
    mocker.patch(
        "subprocess.run",
        side_effect=subprocess.CalledProcessError(
            returncode=1, cmd=["dot"], output=b"", stderr=b"layout error"
        ),
    )
    t = Topology()
    t.add_host("h1")
    with pytest.raises(TopologyError, match="layout error"):
        t.render_graphviz(tmp_path / "topo.png", format="png")
