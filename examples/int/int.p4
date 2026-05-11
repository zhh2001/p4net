/* In-band Network Telemetry (INT) — single-switch demo.
 *
 * For every IPv4 packet that the LPM table forwards, the switch inserts a
 * 14-byte INT shim header between the Ethernet header and the IPv4 payload.
 *
 * Wire layout produced by the deparser:
 *
 *     [ Ethernet (14 B, etherType=0x88B6) ]
 *     [ INT shim (14 B)                  ]
 *     [ IPv4 + payload                   ]
 *
 * INT shim layout (most-significant bit first, total 14 bytes):
 *
 *     +--------+--------+--------+--------+--------+--------+--------+
 *     |  swid  |        ingress_timestamp_us (48 bits)              |
 *     |  (8)   |                                                    |
 *     +--------+----------------+-----------------+-----------------+
 *     |   egress_port (16)      |  queue_depth (16) |  next_proto (16) |
 *     +-------------------------+-------------------+-------------+
 *     |  reserved (8)  |
 *     +----------------+
 *
 * The shim's `next_proto` field carries the original etherType (0x0800
 * for IPv4) so the receiver can recover the inner IPv4 header. A
 * user-space listener on the receiving host parses the shim from a raw
 * AF_PACKET socket; see `examples/int/listener.py`.
 *
 * Pairs with `examples/int/topology.py`, which programs `ipv4_lpm`,
 * writes the ``switch_id`` register, and pre-seeds static ARP entries.
 */
#include <core.p4>
#include <v1model.p4>

const bit<16> ETHERTYPE_IPV4 = 0x0800;
const bit<16> ETHERTYPE_INT  = 0x88B6;

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header int_shim_t {
    bit<8>  switch_id;
    bit<48> ingress_timestamp_us;
    bit<16> egress_port;
    bit<16> queue_depth;
    bit<16> next_proto;
    bit<8>  reserved;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

struct headers {
    ethernet_t ethernet;
    int_shim_t int_shim;
    ipv4_t     ipv4;
}

struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_IPV4: parse_ipv4;
            default: accept;
        }
    }
    state parse_ipv4 {
        pkt.extract(hdr.ipv4);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    /* One-element register holding the configured switch identifier.
     * The control plane writes this at start via
     * ``client.write_register("MyIngress.switch_id", index=0, value=N)``. */
    register<bit<8>>(1) switch_id;

    action drop() {
        mark_to_drop(std);
    }

    action set_egress_port(bit<9> port) {
        std.egress_spec = port;
    }

    table ipv4_lpm {
        key = {
            hdr.ipv4.dstAddr: lpm;
        }
        actions = {
            drop;
            set_egress_port;
            NoAction;
        }
        default_action = NoAction();
        size = 1024;
    }

    apply {
        if (hdr.ipv4.isValid()) {
            ipv4_lpm.apply();
            /* Only stamp INT shim on packets actually being forwarded. */
            if (std.egress_spec != 0) {
                bit<8> sid;
                switch_id.read(sid, 0);
                hdr.int_shim.setValid();
                hdr.int_shim.switch_id            = sid;
                hdr.int_shim.ingress_timestamp_us = (bit<48>) std.ingress_global_timestamp;
                hdr.int_shim.egress_port          = (bit<16>) std.egress_spec;
                hdr.int_shim.queue_depth          = (bit<16>) std.deq_qdepth;
                hdr.int_shim.next_proto           = hdr.ethernet.etherType;
                hdr.int_shim.reserved             = 0;
                hdr.ethernet.etherType            = ETHERTYPE_INT;
            }
        }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.int_shim);
        pkt.emit(hdr.ipv4);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
