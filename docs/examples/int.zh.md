---
description: 在一台交换机转发的 IPv4 报文中插入并读取 14 字节的 INT 遥测头。
---

# INT（带内网络遥测）

交换机为每个转发的 IPv4 报文插入一个 14 字节的遥测头（shim）。接收端从中读取交换机 ID、入口时间戳、出端口、队列深度和原始 EtherType。

## 跑起来

在第一个终端中，从仓库根目录启动拓扑。

```sh
sudo p4net examples/int/topology.py
```

出现 `p4net>` 提示符后，打开第二个终端，在仓库根目录启动 `h2` 中的监听器。

```sh
sudo ip netns exec h2 python3 examples/int/listener.py --iface h2-eth0 --count 3
```

看到监听器输出 `waiting for INT frames` 后，在第三个终端发送三次 ICMP 请求。

```sh
sudo ip netns exec h1 ping -c 3 -W 1 10.0.0.2
```

交换机给每个请求加上一个 INT 头。监听器能收到三个帧。接收主机没有返回 ICMP 回复，所以 `ping` 显示 `100% packet loss`。

监听器为每个帧打印一行。

```
[listener] bound on h2-eth0, waiting for INT frames
[switch=1 ts=745907us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=1750021us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
[switch=1 ts=2754336us egress=2 queue=0 next_proto=0x0800] 10.0.0.1 -> 10.0.0.2
```

监听器显示三个帧后自动退出。然后在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/int/topology.py`：

```python
--8<-- "examples/int/topology.py"
```

`setup(net)` 通过 P4Runtime 下发 IPv4 转发表项，并为两台主机配置静态 ARP。

## P4 程序

`examples/int/int.p4`：

```p4
--8<-- "examples/int/int.p4"
```

- 报文重组时，将有效的遥测头放在以太网头和 IPv4 头之间。外层 EtherType 为 `0x88B6`，`next_proto` 保存原始 EtherType（IPv4 为 `0x0800`）。
- LPM 表选定出端口后，入口处理插入遥测头。报文出队后，出口处理将队列中剩余的报文数写入 `queue_depth`。
- `setup(net)` 把交换机 ID 1 写入 `MyIngress.switch_id` 寄存器。修改这个值只需调整 `setup(net)`。

## 监听器

`examples/int/listener.py`：

```python
--8<-- "examples/int/listener.py"
```

监听器通过原始 `AF_PACKET` 套接字接收以太网帧，筛选 EtherType 为 `0x88B6` 的帧，再读取并显示 14 字节的遥测头。

在本示例的发包速率下，出口队列通常为空，因此 `queue_depth` 通常为 0。

## 可以试试

- 比较 `ingress_timestamp_us`，计算相邻报文到达交换机的时间间隔。
- 运行[两台交换机的 INT 示例](int-multi-hop.md)，读取每一跳插入的遥测头。
