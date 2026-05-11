/* Multi-hop in-band network telemetry — two-switch demo.
 *
 * Each switch on the path inserts its own 14-byte INT shim header between
 * Ethernet and IPv4 on every forwarded packet. Shim chaining uses each
 * shim's ``next_proto`` field rather than a P4 header stack:
 *
 *     [ Ethernet (etherType = 0x88B6 if any shim is present) ]
 *     [ INT shim 1 (14 B; next_proto = 0x88B6 or 0x0800)     ]   <- inserted by hop 1
 *     [ INT shim 2 (14 B; next_proto = 0x0800)               ]   <- inserted by hop 2
 *     [ IPv4 + payload                                       ]
 *
 * Shim format (identical to ``examples/int/int.p4`` in v1.1.0/v1.2.0):
 *     switch_id            uint8
 *     ingress_timestamp_us uint48
 *     egress_port          uint16
 *     queue_depth          uint16
 *     next_proto           uint16  (chains to next header in order)
 *     reserved             uint8
 *
 * Wire-compatible with the single-switch INT listener: a v1.2.0 listener
 * pointed at h2 will decode the first shim correctly and stop at the
 * ``next_proto`` it doesn't recognize. The multi-hop listener
 * (``listener.py``) walks the full chain.
 *
 * The same P4 program runs on both switches; each switch's identity comes
 * from the ``switch_id_reg`` register, written at start-up via the v1.2
 * register API.
 *
 * 2-hop maximum. Real production INT uses a P4 header stack of MAX_HOPS
 * depth and ``push_front``; that's left as an extension exercise — see
 * the README for the recipe.
 *
 * Pairs with ``examples/int_multi_hop/topology.py`` (4-node linear:
 * h1 — s1 — s2 — h2).
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
    int_shim_t int_shim_1;
    int_shim_t int_shim_2;
    ipv4_t     ipv4;
}

struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_IPV4: parse_ipv4;
            ETHERTYPE_INT:  parse_shim_1;
            default: accept;
        }
    }
    state parse_shim_1 {
        pkt.extract(hdr.int_shim_1);
        transition select(hdr.int_shim_1.next_proto) {
            ETHERTYPE_IPV4: parse_ipv4;
            ETHERTYPE_INT:  parse_shim_2;
            default: accept;
        }
    }
    state parse_shim_2 {
        pkt.extract(hdr.int_shim_2);
        transition select(hdr.int_shim_2.next_proto) {
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
    /* One-element register holding this switch's INT identifier.
     * Written by the controller via P4RuntimeClient.write_register. */
    register<bit<8>>(1) switch_id_reg;

    action drop_packet() {
        mark_to_drop(std);
    }

    action set_egress_port(bit<9> port) {
        std.egress_spec = port;
    }

    table l2_forward {
        key = {
            hdr.ethernet.dstAddr: exact;
        }
        actions = {
            drop_packet;
            set_egress_port;
            NoAction;
        }
        default_action = NoAction();
        size = 1024;
    }

    apply {
        if (hdr.ipv4.isValid()) {
            l2_forward.apply();
            if (std.egress_spec != 0) {
                bit<8> sid;
                switch_id_reg.read(sid, 0);
                if (!hdr.int_shim_1.isValid()) {
                    /* First hop on path. */
                    hdr.int_shim_1.setValid();
                    hdr.int_shim_1.switch_id            = sid;
                    hdr.int_shim_1.ingress_timestamp_us = (bit<48>) std.ingress_global_timestamp;
                    hdr.int_shim_1.egress_port          = (bit<16>) std.egress_spec;
                    hdr.int_shim_1.queue_depth          = (bit<16>) std.deq_qdepth;
                    hdr.int_shim_1.next_proto           = hdr.ethernet.etherType;
                    hdr.int_shim_1.reserved             = 0;
                    hdr.ethernet.etherType              = ETHERTYPE_INT;
                } else if (!hdr.int_shim_2.isValid()) {
                    /* Second hop. Chain shim_1.next_proto -> 0x88B6 so the
                     * receiver sees shim_1 -> shim_2 -> IPv4. */
                    hdr.int_shim_2.setValid();
                    hdr.int_shim_2.switch_id            = sid;
                    hdr.int_shim_2.ingress_timestamp_us = (bit<48>) std.ingress_global_timestamp;
                    hdr.int_shim_2.egress_port          = (bit<16>) std.egress_spec;
                    hdr.int_shim_2.queue_depth          = (bit<16>) std.deq_qdepth;
                    hdr.int_shim_2.next_proto           = hdr.int_shim_1.next_proto;
                    hdr.int_shim_2.reserved             = 0;
                    hdr.int_shim_1.next_proto           = ETHERTYPE_INT;
                }
                /* Both shim slots full = 3+ hop topology; this example does
                 * not support that. Real deployments use a header stack of
                 * MAX_HOPS depth and push_front(1). The packet still
                 * forwards correctly through this switch; the receiver
                 * just won't see the third hop's metadata. */
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
        pkt.emit(hdr.int_shim_1);
        pkt.emit(hdr.int_shim_2);
        pkt.emit(hdr.ipv4);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
