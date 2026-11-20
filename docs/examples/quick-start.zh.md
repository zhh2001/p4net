---
description: 两台主机通过 P4 交换机相连，交换机在端口 1 和端口 2 之间转发报文。
---

# 快速上手（端口翻转）

两台主机分别接入 BMv2 交换机的端口 1 和端口 2。P4 程序在这两个端口之间转发报文。

## 运行

在仓库根目录启动拓扑。

```sh
sudo p4net examples/quick_start/quick_start.py
```

在 `p4net>` 提示符下，用 `hosts` 查看主机接口，再用 `pingall` 检查两个方向是否连通。

```
p4net> hosts
name  primary_ip   primary_ip6  interfaces
h1    10.0.0.1/24  -           h1-eth0
h2    10.0.0.2/24  -           h2-eth0

p4net> pingall
H \ H  h1   h2
h1     -    1
h2     1    -
2/2 succeeded
```

完成后，在 `p4net>` 提示符下输入 `exit` 停止拓扑。

## 拓扑

`examples/quick_start/quick_start.py`：

```python
--8<-- "examples/quick_start/quick_start.py"
```

网络启动后、进入命令行前会调用 `setup(net)`。它设置静态 ARP 条目，让第一次 ping 可以直接使用对端的 MAC 地址。

## P4 程序

`examples/quick_start/quick_start.p4`：

```p4
--8<-- "examples/quick_start/quick_start.p4"
```

入口处理根据输入端口选择输出端口。ARP 报文也在这两个端口之间转发。

## 可尝试的变体

- 在端口 3 上加一台主机。当前程序会丢弃从端口 3 收到的报文，端口 1 和端口 2 收到的报文仍只发给对方。
- 把入口处理的条件分支换成 `mark_to_drop(std)`，再重新启动拓扑。`pingall` 会显示 `0/2 succeeded`。
- 在连接 `h1` 和 `s1` 的 `add_link()` 调用中加入 `loss_pct=20.0`。用 `h1 ping h2 10 1` 比较修改前后的丢包统计。
