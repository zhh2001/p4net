/* Minimal pipeline that declares a register, reads it at every ingress,
 * but never modifies it from the dataplane.
 *
 * Used by tests/control/test_register_integration.py to drive the
 * P4RuntimeClient.write_register / read_register API end-to-end.
 */
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

struct metadata {
    bit<32> rv;
}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        pkt.extract(hdr.ethernet);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    register<bit<32>>(16) test_register;

    apply {
        /* Read but don't write. Whatever the controller plants in the
         * register stays there for read-back. */
        test_register.read(meta.rv, 0);
        mark_to_drop(std);
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.ethernet);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
