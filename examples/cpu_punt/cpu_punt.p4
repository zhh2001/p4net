/* CPU-punt demo pipeline.
 *
 * Every dataplane packet is punted to the controller (via the CPU port).
 * Packets injected from the controller carry a `packet_out` header that
 * names the desired egress port; the ingress control copies that into
 * `std.egress_spec` and invalidates the header before the packet is
 * deparsed onto the wire.
 *
 * Pairs with `examples/cpu_punt/topology.py`, which sets `cpu_port=510`
 * on the BMv2 switch.
 */
#include <core.p4>
#include <v1model.p4>

const bit<9> CPU_PORT = 510;

@controller_header("packet_in")
header packet_in_t {
    bit<9>  ingress_port;
    bit<7>  _pad0;
}

@controller_header("packet_out")
header packet_out_t {
    bit<9>  egress_port;
    bit<7>  _pad0;
}

header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

struct headers {
    packet_in_t  packet_in;
    packet_out_t packet_out;
    ethernet_t   ethernet;
}

struct metadata {}

parser MyParser(packet_in pkt, out headers hdr, inout metadata meta,
                inout standard_metadata_t std) {
    state start {
        transition select(std.ingress_port) {
            CPU_PORT: parse_packet_out;
            default:  parse_ethernet;
        }
    }
    state parse_packet_out {
        pkt.extract(hdr.packet_out);
        transition parse_ethernet;
    }
    state parse_ethernet {
        pkt.extract(hdr.ethernet);
        transition accept;
    }
}

control MyVerifyChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyIngress(inout headers hdr, inout metadata meta,
                  inout standard_metadata_t std) {
    apply {
        if (std.ingress_port == CPU_PORT) {
            // Controller-injected packet: forward as instructed and strip
            // the controller header before deparsing.
            std.egress_spec = hdr.packet_out.egress_port;
            hdr.packet_out.setInvalid();
        } else {
            // Dataplane packet: punt to controller; stamp ingress_port.
            std.egress_spec = CPU_PORT;
            hdr.packet_in.setValid();
            hdr.packet_in.ingress_port = std.ingress_port;
            hdr.packet_in._pad0 = 0;
        }
    }
}

control MyEgress(inout headers hdr, inout metadata meta,
                 inout standard_metadata_t std) { apply {} }

control MyComputeChecksum(inout headers hdr, inout metadata meta) { apply {} }

control MyDeparser(packet_out pkt, in headers hdr) {
    apply {
        pkt.emit(hdr.packet_in);
        pkt.emit(hdr.ethernet);
    }
}

V1Switch(MyParser(), MyVerifyChecksum(), MyIngress(), MyEgress(),
         MyComputeChecksum(), MyDeparser()) main;
