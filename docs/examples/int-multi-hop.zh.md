---
description: 多跳 INT 示例。两台交换机串联运行同一份 P4，每台插入自己的元数据 shim；接收端解析整条逐跳链。
---

# 多跳 INT

两台交换机串联，各自在转发的 IPv4 报文中插入一个 14 字节的 INT shim 头。接收端读取这两个头部，就能看到报文经过了哪些交换机。[单交换机 INT 示例](int.md)介绍了 shim 的格式。

## 跑起来

在第一个终端中，从仓库根目录启动拓扑。

```sh
sudo p4net examples/int_multi_hop/topology.py
```

出现 `p4net>` 提示符后，打开第二个终端，在仓库根目录启动 `h2` 中的监听器。

```sh
sudo ip netns exec h2 python3 examples/int_multi_hop/listener.py --iface h2-eth0 --count 3
```

看到监听器输出 `boot times loaded` 后，在第三个终端发送三次 ICMP 请求。

```sh
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

两台交换机都会给请求加上 INT 头。监听器能收到三个帧。接收主机没有返回 ICMP 回复，所以 `ping` 显示 `100% packet loss`。

### 示例输出

监听器为每个报文打印两跳的数据。下面是三个报文中的一个。

```
packet (2 hop(s), final proto 0x0800): 10.0.0.1 -> 10.0.0.2
  hop 1: switch_id=1 ts=676402us aligned=1795161753919021us egress_port=2 queue_depth=0
  hop 2: switch_id=2 ts=467984us aligned=1795161753918136us egress_port=2 queue_depth=0
  latency_s1_to_s2 = -885us
```

`hop 1` 对应 `s1`，`hop 2` 对应 `s2`。`ts` 来自各自交换机的 `ingress_global_timestamp`。`aligned` 是监听器估算的到达时刻，用 Unix 微秒表示。两个估算值之差显示在 `latency_s1_to_s2` 一行。关于上例中的负值，见[跨交换机时间戳对齐](#跨交换机时间戳对齐如何工作)。

没有启动时间戳文件时，监听器会显示原始的 `ts`，并标注 `[unaligned]`。

监听器显示三个帧后自动退出。然后在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/int_multi_hop/topology.py`：

```python
--8<-- "examples/int_multi_hop/topology.py"
```

报文沿 `h1 → s1 → s2 → h2` 转发。`setup(net)` 为两台交换机下发二层转发表项，并为两台主机配置静态 ARP。

## P4 程序

`examples/int_multi_hop/int_multi_hop.p4`：

```p4
--8<-- "examples/int_multi_hop/int_multi_hop.p4"
```

要点：

- 用两个命名 header 实例 `int_shim_1`、`int_shim_2`，不用 P4 header stack。两跳情况下更易读；要做 N 跳，请参考示例 README 的「扩展到 N 跳」一节。
- ingress 选择第一个未填的 shim slot，从 `standard_metadata` 与配置的 `switch_id` 写入。`next_proto` 链路被重新拼接，使接收方看到 `eth → shim_1 → shim_2 → ipv4` 的顺序。
- deparser 按声明顺序 emit 所有 valid header。
- 拓扑启动时通过 `write_register("MyIngress.switch_id_reg", index=0, value=N)` 为两台交换机分别设置 ID。

## listener

`examples/int_multi_hop/listener.py`：

```python
--8<-- "examples/int_multi_hop/listener.py"
```

listener 从外层 EtherType 开始遍历 shim 链，每跳解出一个 14 字节 shim，直到 `next_proto` 离开 INT 范围为止。

## 跨交换机时间戳对齐如何工作

每个 BMv2 进程在初始化时启动自己的微秒计数器。p4net 在启动进程前记录 `RunningSwitch.boot_timestamp_us`，单位为 Unix 微秒。监听器用这个值估算计数器的起点。参见 [BMv2 时间戳实现说明](https://github.com/p4lang/behavioral-model/blob/2bdd0b7b/docs/simple_switch.md#bmv2-timestamp-implementation-notes)。

```
aligned = boot_timestamp_us + ts
latency_s1_to_s2 = aligned_s2 - aligned_s1
```

这个差值估算报文先后到达 `s1` 和 `s2` 的时间间隔。从 p4net 记录启动时间戳到 BMv2 开始计时，两台交换机经过的时间可能不同。这会给估算带来误差，有时会像上面的示例一样算出负值。

`setup(net)` 默认把 `Network.boot_timestamps` 保存到 `/tmp/p4net-int-multi-hop-boot-times.json`，监听器启动时读取。每次访问这个属性都会得到一个新的字典，以交换机名为键。可以修改这份副本，运行中的网络状态不受影响。

## 同主机并行运行多份拓扑

每份拓扑使用相同的节点名和默认 BMv2 gRPC、Thrift 端口。为每份拓扑创建独立的网络命名空间来隔离链路和端口，再用独立的挂载命名空间和私有 `/run/netns` 隔离 `h1`、`h2` 的名字。每份拓扑也使用各自的时间戳协调文件。

在仓库根目录为每份拓扑打开一个独立的 shell。

```bash
sudo unshare --mount --net --propagation private bash
```

在每个新 shell 中执行以下命令。

```bash
mkdir -p /run/netns
mount -t tmpfs tmpfs /run/netns
ip link set lo up
int_boot_times=$(mktemp /tmp/p4net-int-boot-times.XXXXXXXX)
trap 'rm -f -- "$int_boot_times"' EXIT
printf 'int_shell_pid=%s int_boot_times=%s\n' "$$" "$int_boot_times"
P4NET_INT_BOOT_TIMES_PATH="$int_boot_times" p4net examples/int_multi_hop/topology.py
```

看到 `p4net>` 后，把本次启动输出的 `int_shell_pid=... int_boot_times=...` 整行复制到位于仓库根目录的第二个终端，再进入对应的 `h2` 命名空间启动 listener。

```bash
sudo nsenter --target "$int_shell_pid" --mount --net -- \
  ip netns exec h2 python3 "$PWD/examples/int_multi_hop/listener.py" \
  --iface h2-eth0 --boot-times "$int_boot_times" --count 3
```

listener 显示 `boot times loaded` 后，在对应的 `p4net>` 中发送流量。

```text
h1 ping h2 3 1
```

listener 会显示三个包，每个包都有两跳对齐后的时间戳。带 INT 头的帧会到达 listener，`ping` 因为收不到 ICMP 回复会显示 `3 sent, 0 received, 100% loss`。完成后，先在 `p4net>` 输入 `exit`，再退出隔离的 Bash shell。Bash 退出时会删除本次的协调文件。

## 值得注意的点

- **出端口对应路径方向**。s1 从 port 2 朝 s2 转发；s2 从 port 2 朝 h2 转发。不同拓扑会得到不同的端口号。

## 注意事项

- **当前流水线只支持两跳**。第三台交换机会发现两个 shim slot 都已 valid，直接转发不再追加。真实部署用 MAX_HOPS 深度的 P4 header stack——示例 README 里有改写步骤。
- **队列深度**。在本示例的发包速率下，`queue_depth` 通常为 0。
- **不重算插入 shim 的校验和**。IPv4 校验和只覆盖 IPv4 头本身；位于以太网与 IPv4 之间的 shim 层是无保护的，这与 INT 规范的假设（链路层完整性）一致。
