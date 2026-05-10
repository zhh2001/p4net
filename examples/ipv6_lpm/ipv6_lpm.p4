/* Minimal IPv6 LPM forwarding pipeline.
 *
 * Pairs with examples/ipv6_lpm/topology.py, which programs the table at
 * runtime over P4Runtime. Both v6 endpoints are pre-seeded with static
 * neighbor entries so ICMP unicast does not need to resolve at test time.
 */
#include <core.p4>
#include <v1model.p4>

const bit<16> ETHERTYPE_IPV6 = 0x86DD;

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

header ipv6_t {
    bit<4>   version;
    bit<8>   trafficClass;
    bit<20>  flowLabel;
    bit<16>  payloadLen;
    bit<8>   nextHdr;
    bit<8>   hopLimit;
    bit<128> srcAddr;
    bit<128> dstAddr;
}

struct headers {
    ethernet_t ethernet;
    ipv6_t     ipv6;
}

struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        pkt.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            ETHERTYPE_IPV6: parse_ipv6;
            default: accept;
        }
    }
    state parse_ipv6 {
        pkt.extract(hdr.ipv6);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    counter(256, CounterType.packets) ipv6_pkts;

    action drop() {
        mark_to_drop(std);
    }

    action set_egress_port(bit<9> port) {
        std.egress_spec = port;
        ipv6_pkts.count((bit<32>) port);
    }

    table ipv6_lpm {
        key = {
            hdr.ipv6.dstAddr: lpm;
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
        if (hdr.ipv6.isValid()) {
            ipv6_lpm.apply();
        }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.ethernet);
        pkt.emit(hdr.ipv6);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
