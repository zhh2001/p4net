---
description: 把每个数据平面包都通过 CPU 端口上送控制器的流水线。演示 `<switch> packet send` 与 `<switch> packet listen`。
---

# CPU 上送

一台主机、一台交换机，每个数据平面包都被通过 CPU 端口（510）上送控制器。控制器也可以通过带有显式出口端口元数据的 `PacketOut` 把数据包注入回数据平面。

## 你将看到什么

主机产生 ARP、IPv6 ND 或 ICMP 流量时，`s1 packet listen` 会收集上送的数据包，达到目标数量或超时后显示结果。`s1 packet send` 把控制器构造的帧注入数据平面。

## 拓扑

`examples/cpu_punt/topology.py`：

```python
--8<-- "examples/cpu_punt/topology.py"
```

交换机上的 `cpu_port=510` 是把 CPU 端口接入 BMv2 的关键参数。

## P4 程序

`examples/cpu_punt/cpu_punt.p4`：

```p4
--8<-- "examples/cpu_punt/cpu_punt.p4"
```

两个 `@controller_header` 声明分别定义 PacketIn（上送控制器）与 PacketOut（控制器注入）的元数据布局。parser 根据 `std.ingress_port == CPU_PORT` 判断是否需要在 ethernet 之前先解析 `packet_out` 报头。入口控制：

- 控制器注入的包：把 `egress_port` 复制进 `std.egress_spec`，并使控制器报头无效。
- 数据平面包：把 `std.egress_spec = CPU_PORT`，使 `packet_in` 报头有效，并写入 `ingress_port`。

## 运行

在第一个终端中，从仓库根目录启动拓扑。

```sh
sudo p4net examples/cpu_punt/topology.py
```

### 接收 PacketIn

在 `p4net>` 中开始监听。

```text
s1 packet listen count=10 timeout=15
```

看到 `Waiting for 10 packets on s1` 后，在第二个终端运行。

```sh
sudo ip netns exec h1 ping -c 1 -W 1 10.0.0.99
```

`10.0.0.99` 上没有主机，因此 `ping` 会超时。交换机会把 `h1` 发出的 ARP 请求上送控制器。监听达到包数或超时后，会显示类似下面的内容，也可能同时显示 IPv6 帧。

```text
[ingress_port=1] ffffffffffff000000000001080600010800060400010000000000010a000001...
```

`[ingress_port=1]` 来自 `packet_in` 控制器报头。CLI 将较长的帧截断到 64 个十六进制字符，可在 Python 中用 `client.expect_packet_in()` 读取完整帧。

### 发送 PacketOut

如果已安装 `tcpdump`，可以在第二个终端抓包，确认帧到达 `h1`。过滤器只匹配示例帧使用的 EtherType。

```sh
sudo ip netns exec h1 tcpdump -i h1-eth0 -n -e -X -Q in -c 1 'ether proto 0x88b5'
```

在 `p4net>` 中整行执行下面的命令。如果启动了抓包，先等到出现 `listening on h1-eth0`。

```text
s1 packet send 00000000000102000000009988b548656c6c6f metadata: egress_port=1
```

`egress_port=1` 选择交换机上连接 `h1` 的端口。CLI 返回 `ok`，抓包结果中可以看到 EtherType `0x88b5` 和负载 `Hello`。

`encode_packet_out_metadata` 会将 P4Info 中未提供的元数据字段默认置零，包括本例的 `_pad0` 字段。

## 可尝试的变体

- 用 `s1.client.on_packet_in(handler)` 注册一个 Python 处理器，解析上送的以太网帧，做学习交换机式的逻辑。
- 在 setup 脚本里用 `s1.client.send_packet_out(payload, {"egress_port": 1})` 注入一串探测帧。
- 加一张 `default_action = punt()` 的 `ipv4_lpm` 表，把已编程的转发与控制器兜底逻辑混合起来。
