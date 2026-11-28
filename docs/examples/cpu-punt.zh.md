---
description: 通过交换机的 CPU 端口接收 PacketIn 和发送 PacketOut。
---

# CPU 上送

一台主机接在一台交换机上。交换机通过 CPU 端口（510）将主机发来的报文上送控制器。控制器也可以用 `PacketOut` 向指定的出端口发送报文。

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

`h1` 发出 ARP 请求来解析 `10.0.0.99`，交换机将请求作为 PacketIn 上送控制器。终端每收到一个请求就显示类似下面的一行，监听结束时会显示收到的包数。由于没有主机使用 `10.0.0.99`，`ping` 收不到应答。

```text
[_pad0=0] [ingress_port=1] ffffffffffff000000000001080600010800060400010000000000010a000001...
```

元数据字段来自 `packet_in` 控制器报头。CLI 将较长的帧截断到 64 个十六进制字符，可在 Python 中用 `client.expect_packet_in()` 读取完整帧。

### 发送 PacketOut

等到监听结束、`p4net>` 提示符重新出现后，再发送 PacketOut。

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

完成后，在 `p4net>` 提示符下输入 `exit` 停止拓扑。

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

两个 `@controller_header` 声明分别定义 PacketIn 和 PacketOut 的元数据格式。来自 CPU 端口的报文先解析 `packet_out` 报头，再解析以太网头。

- 控制器发出的报文按 `egress_port` 转发。发送前将控制器报头置为无效。
- 主机发来的报文转至 CPU 端口，并在 `packet_in` 报头中记录 `ingress_port`。

## 可尝试的变体

- 用 `s1.client.on_packet_in(handler)` 注册 Python 处理函数，从 PacketIn 中读取源 MAC 地址和入端口。
- 在初始化函数中用 `s1.client.send_packet_out(payload, {"egress_port": 1})` 从端口 1 发送探测帧。
