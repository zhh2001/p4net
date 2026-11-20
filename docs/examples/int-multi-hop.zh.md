---
description: 读取两台交换机插入的 INT 遥测头，估算报文到达各跳的时间。
---

# 多跳 INT

两台串联的交换机分别在转发的 IPv4 报文中插入一个 14 字节的 INT 遥测头。接收端读取这两个遥测头，就能知道报文经过了哪些交换机。[单交换机 INT 示例](int.md)介绍了遥测头的格式。

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

在本拓扑中，`s1` 从端口 2 发往 `s2`，`s2` 从端口 2 发往 `h2`。在这个发包速率下，`queue_depth` 通常为 0。

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

- 程序为遥测头预留 `int_shim_1` 和 `int_shim_2` 两个位置。每台交换机在第一个空位写入 `standard_metadata` 中的相关字段和自己的 `switch_id`。路径上后续的交换机直接转发带这两个遥测头的报文。
- `next_proto` 标明各报头的顺序：以太网头、遥测头 1、遥测头 2、IPv4 头。IPv4 校验和只覆盖 IPv4 头。
- 拓扑启动时通过 `write_register("MyIngress.switch_id_reg", index=0, value=N)` 为两台交换机分别设置 ID。

## 监听器

`examples/int_multi_hop/listener.py`：

```python
--8<-- "examples/int_multi_hop/listener.py"
```

监听器从外层 EtherType 开始，每跳读取一个 14 字节的遥测头，并通过 `next_proto` 确定下一个报头的类型。

## 跨交换机时间戳对齐如何工作

每个 BMv2 进程在初始化时启动自己的微秒计数器。p4net 在启动进程前记录 `RunningSwitch.boot_timestamp_us`，单位为 Unix 微秒。监听器用这个值估算计数器的起点。参见 [BMv2 时间戳实现说明](https://github.com/p4lang/behavioral-model/blob/2bdd0b7b/docs/simple_switch.md#bmv2-timestamp-implementation-notes)。

```
aligned = boot_timestamp_us + ts
latency_s1_to_s2 = aligned_s2 - aligned_s1
```

这个差值估算报文先后到达 `s1` 和 `s2` 的时间间隔。从 p4net 记录启动时间戳到 BMv2 开始计时，两台交换机经过的时间可能不同。这会给估算带来误差，有时会像上面的示例一样算出负值。

`setup(net)` 默认把 `Network.boot_timestamps` 保存到 `/tmp/p4net-int-multi-hop-boot-times.json`。监听器在启动时读取这个文件。

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

看到 `p4net>` 后，把本次启动输出的 `int_shell_pid=... int_boot_times=...` 整行复制到位于仓库根目录的第二个终端，再进入对应的 `h2` 命名空间启动监听器。

```bash
sudo nsenter --target "$int_shell_pid" --mount --net -- \
  ip netns exec h2 python3 "$PWD/examples/int_multi_hop/listener.py" \
  --iface h2-eth0 --boot-times "$int_boot_times" --count 3
```

监听器显示 `boot times loaded` 后，在对应的 `p4net>` 中发送流量。

```text
h1 ping h2 3 1
```

监听器会显示三个报文及每个报文在两跳的对齐时间戳。此时 `ping` 收不到 ICMP 回复，会显示 `3 sent, 0 received, 100% loss`。完成后，先在 `p4net>` 输入 `exit`，再退出隔离的 Bash。退出 Bash 时会删除本次运行的时间戳文件。
