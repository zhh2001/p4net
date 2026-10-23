#include <core.p4>
#include <v1model.p4>

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers {
    ethernet_t ethernet;
}

struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        pkt.extract(hdr.ethernet);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) {
    apply {}
}

control MyIngress(inout headers hdr,
                  inout metadata meta,
                  inout standard_metadata_t std) {
    action drop() {
        mark_to_drop(std);
    }

    action set_egress_port(bit<9> port) {
        std.egress_spec = port;
    }

    table l2_match {
        key = {
            hdr.ethernet.dstAddr: exact;
            hdr.ethernet.srcAddr: ternary;
            hdr.ethernet.etherType: range;
        }
        actions = {
            drop;
            set_egress_port;
        }
        default_action = drop();
        size = 32;
    }

    apply {
        l2_match.apply();
    }
}

control MyEgress(inout headers hdr,
                 inout metadata meta,
                 inout standard_metadata_t std) {
    apply {}
}

control MyComputeChecksum(inout headers hdr, inout metadata meta) {
    apply {}
}

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.ethernet);
    }
}

V1Switch(MyParser(),
         MyVerifyChecksum(),
         MyIngress(),
         MyEgress(),
         MyComputeChecksum(),
         MyDeparser()) main;
