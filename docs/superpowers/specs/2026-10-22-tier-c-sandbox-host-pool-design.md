# C 档：沙箱主机池

日期 2026-10-22。[容量方案](../plans/2026-09-29-enterprise-capacity.md) §3 C 档的设计，接着
[B 档设计](2026-10-21-tier-b-networked-controller-design.md) §9 留下的事。
状态：设计，未实现。2026-10-23 按当时的代码修订一次，改动见文末「修订记录」。

## 1. 要解决什么

B 档之后，Worker 可以在任意台机器上，但**沙箱只能在一台主机上**：配置只放得下一个
controller 地址，代码里也有六处默认只有一台（§3）。带工具的并发因此被一台主机封顶。

C 档要做到：**多台沙箱主机，每台一个 controller；新沙箱放到有空的那台；冻结的沙箱下一个
时间片回到原主机；一台主机坏了，运维确认后它上面的 Run 换一台冷恢复。**

## 2. 换来什么，换不来什么

**换来的：**

- 带工具的并发随沙箱主机数线性增长。单台的两个上限都是按主机算的：Docker exec 每秒约
  140 次（[每轮开销记录](../verification/2026-09-29-round-overhead.md)），沙箱内存按
  主机的准入预算算。
- 一台沙箱主机停机只影响它上面的 Run，不再是全部带工具的 Run。

**换不来的：**

- **Postgres 还是一个。**单机档 40 路时 Postgres CPU 峰值已经 224%。主机池让更多 Run
  同时执行，压力全落在数据库上。6000 人要不要读写分离、连接池代理，是另一份设计。
- **冷恢复不会变快。**换主机就是冷启动：从对象存储下载工作区，流给新 controller。
- **API 的事件流还是所有进程收全部通知**（第 5 项验收记录）。

## 3. 代码里「只有一台」的地方

B 档 §9 列了六条，这次读代码又找到一条。

1. `sandbox_instances` 不记录在哪台主机上，Worker 和 Scheduler 也没有地方按主机找 controller。
2. 准入：`committed_memory_mb()` 对**全库**的活跃 reservation 求和；`lock_admission()` 是
   全库一把锁（`_ADMISSION_LOCK_KEY`）。
3. 腾内存：`evictable_keeps()` 返回**全库**的冻结沙箱，A 主机缺内存会去驱逐 B 主机上的沙箱。
4. 出站：`sandbox_egress_addresses` 以地址为主键。每台主机各有自己的 Docker 网络，两台可以
   发出同一个地址；代理按地址认 Run，认错就是把一个 Run 的出站额度和审计记到另一个 Run 上。
   （修订：改成要求地址池互不重叠，见 §4.6。）
5. Scheduler 的 `_reclaim_sandboxes` 把所有 reservation 发给同一个 controller。
6. 每个 Run 的 Docker volume 只在建它的那台主机上。
7. ~~没有孤儿卷清扫。~~ **已补上**（2026-10-23 修订时确认）：controller 的 `sweep()` 每
   `SANDBOX_SWEEP_SECONDS` 在准入锁下删掉本机上没有实例行指着的容器、没有 reservation 也没有
   租约的 Run 卷、没有记录的沙箱网络。主机池要改的只是「有记录」的判断要加上「在本机名下」。

## 4. 设计

### 4.1 主机登记：`sandbox_hosts`

新表，每台沙箱主机一行，由那台的 controller 自己写：

| 列 | 说明 |
|---|---|
| `name` | 主键。运维起的名字，例如 `sbx-a`。不是地址，也不是宿主机路径 |
| `url` | Worker 和 Scheduler 拨的地址，必须是 `tls://` |
| `memory_budget_mb` | 这台的沙箱内存预算（§21.3） |
| `state` | `active`、`draining`、`retired` |
| `seen_at` | controller 每 10 秒更新一次 |
| `network_pool` | 这台的 `SANDBOX_NETWORK_POOL`，可空（不联网的部署）。和别的主机重叠时拒绝启动（§4.6） |

controller 启动时按 `SANDBOX_CONTROLLER_NAME`、`SANDBOX_CONTROLLER_ADVERTISE_URL` 和自己的
预算写入（已存在就更新 `url`、预算、`seen_at`，**不改 `state`**：一台被下线的主机重启后
不会自己回来）。

为什么放数据库而不是让每个 Worker 配一张列表：加一台主机不用改所有 Worker 和 Scheduler 的
配置再重启。地址被写坏的后果有限：Worker 拨过去还要过双向 TLS，对面必须拿着平台 CA 签给
controller 的证书，而且证书里的名字要和 `url` 的主机名对上（B 档 §4.2 已经这样校验）。

### 4.2 沙箱记住自己在哪台

- `sandbox_instances` 加 `controller`（主机名，非空）。reservation 经 instance 找到主机，
  不另加列。
- `sandbox_egress_addresses` 不改（§4.6：各台的地址池互不重叠，地址本身就唯一）。
- controller 只认自己名下的行：调用里的 `sandbox_id` 属于别的主机时，拒绝
  `reservation_elsewhere`，什么也不做。这条是兜底，正常路由不会走到这里。

### 4.3 放置：Worker 挑主机，controller 说了算

Worker 调 `acquire` 之前：

1. **这个 Run 有活着的 reservation**（kept、active、isolated）：只去它所在的那台。冻结的
   沙箱只能在原主机解冻；isolated 的照旧拒绝，等 Scheduler 回收。那台连不上就按 B 档的规则
   每 30 秒重试——**不会**因为原主机连不上就在别处再建一个，那正是 isolated 状态要防的
   「一个 Run 两个沙箱」。
2. **没有**：从 `state = active` 且 `seen_at` 在 30 秒内的主机里，按剩余内存
   （预算减去这台上活跃 reservation 的上限之和）从大到小排，同样多的随机打散。
3. 按顺序试。被拒 `memory_budget_exhausted`，或者 `NotConnected`（请求没发出去），就试下一台。
   每台每次最多试一次。全都不行，按现在的规则结束时间片、稍后重试。
4. 请求已经发出去才断（`sent=True`）的，**不换主机**，按 B 档规则中断，交给恢复流程。
   那台可能已经建了沙箱。

剩余内存是 Worker 读出来的估计，不是保证。两个 Worker 同时看中同一台时，由那台 controller
的准入锁决定谁进，输的那个被拒后换下一台。所以**准入的权威仍然只在 controller**，
放置只是挑一个大概率能进的。

不做跨主机迁移：一台满了不会把别处冻结的沙箱搬过来。

### 4.4 其余调用怎么找到主机

`execute`、`freeze`、`thaw`、工作区流、`destroy`、`cleanup`、`volume_remove` 都带着
`sandbox_id`。新的 `SandboxPool` 和现在的 `SandboxClient` 是同一个接口，按
`sandbox_instances.controller` 找到 `sandbox_hosts.url`，转给那台的客户端。`acquire` 的结果里
带上主机名，Worker 一个时间片里不用再查库；Scheduler 按行里的主机名分组。

每台主机一个长期的 `ControllerClient`（它本来就是每次调用新开连接，没有连接状态）。

### 4.5 准入和腾内存按主机分开

- `lock_admission(controller)`：`pg_advisory_xact_lock(KEY, hashtext(name))`，两台主机的
  acquire 互不等待。
- `committed_memory_mb(controller)`、`evictable_keeps(controller)` 都加上
  `sandbox_instances.controller = :name`。A 主机缺内存只会驱逐 A 上的冻结沙箱。

### 4.6 出站代理每台一个，地址池互不重叠

每台沙箱主机跑自己的 egress-proxy：v2.21 起每个沙箱有自己的 `internal` 网络，代理要加入这个
网络，所以只能在同一个 Docker 上。Worker 自己的出站仍然走 `EGRESS_PROXY_URL`，可以指向任意
一台的代理，也可以单独部署。

**地址怎么不冲突**：初稿打算把 `sandbox_egress_addresses` 的主键改成 `(controller, address)`，
代理再按 `(自己的名字, 来源地址)` 认 Run。修订后改成**要求每台主机的 `SANDBOX_NETWORK_POOL`
互不重叠**：沙箱地址都从本机的池里分，池不重叠，地址就在全平台唯一，主键和代理的查法都不用改。

- 每台 controller 登记时把自己的池写进 `sandbox_hosts.network_pool`。和另一台 `active` 或
  `draining` 主机的池重叠时拒绝启动，日志里写出是哪一台。
- 代价：运维要给每台主机分一段不重叠的池（例如 `10.240.0.0/16`、`10.241.0.0/16`）。这比改主键
  和代理查法简单，也更容易在出问题时看懂：一个地址只可能属于一台主机。

### 4.7 Scheduler 回收，和孤儿卷清扫

- `_reclaim_sandboxes` 按主机分组，各发各的。一台连不上，只是它那组留到下一轮，不挡别的主机。
- **清扫已经有了**（`SandboxController.sweep`，每台 controller 自己扫本机）。主机池里要改的是：
  - 容器：「有一行活着的实例指着它」改成「有一行活着的、**在本机名下**的实例指着它」。被 `retire`
    又连回来的主机（§4.8），本机的容器对应的行已经是 `destroyed`，会被清掉。
  - 卷：「这个 Run 有 reservation 或租约」改成「这个 Run 在**本机**有 reservation，或者有租约」。
    Run 换到别的主机冷恢复后，老主机上的卷就会被清掉。租约那一条仍然不分主机：Run 正在别处
    执行时，老主机不该删它的卷吗？可以删，但留着无害，清扫宁可少删。
  - 准入锁按主机分（§4.5），清扫拿本机那把。

### 4.8 排空和下线

两个运维操作，新脚本 `scripts/sandbox_hosts.py`：

- **`drain NAME`**：`state = draining`。不再放新沙箱；已有的冻结沙箱照常解冻、到期、被回收。
  脚本打印这台还剩多少活跃 reservation，到 0 就可以停机维护，`activate NAME` 放回来。
- **`retire NAME --confirm-host-is-gone`**：用于主机已经彻底死掉、不会回来的情况。
  在一个事务里：`state = retired`；这台的全部 reservation 改为 `released`，instance 改为
  `destroyed`；清掉这台的出站地址；对等待「清理已确认」的 Run 走现有的确认流程（和
  `_confirm_cleanup_intent` 同一套）。之后这些 Run 的下一次 acquire 落到别的主机，冷恢复。

**`retire` 不会自动发生。**`seen_at` 过期只让放置跳过这台（§4.3 第 2 步），不释放任何
reservation：分不清「主机死了」和「网络断了而沙箱还在跑」，后一种情况下自动释放就是一个 Run
两个沙箱。这一点和 B 档对 isolated 的处理一致：不确定时宁可卡住，等人确认。

被 `retire` 的主机如果其实还活着、又连回来，它的 controller 会发现自己名下的行已经是
`released`/`destroyed`，拒绝对它们的任何调用；它本机残留的容器和卷由 §4.7 的清扫删掉。
**现在没有启动时的对账**，所以 §4.7 的清扫要连容器一起扫，这里要补一条测试（§8）。

### 4.9 单机不变

- `SANDBOX_CONTROLLER_NAME` 默认 `default`；迁移把现有行都写成 `default`。
- Worker、Scheduler 仍然读 `SANDBOX_CONTROLLER_URL`。写成 `pool` 时才走 §4.3、§4.4；写成
  `unix://` 或 `tls://` 时和现在一样只有一台。
- 只有一台主机时，`sandbox_hosts` 里一行 `default`。Unix socket 的部署不往表里写
  `url`（socket 路径是宿主机路径，不存）。

## 5. 配置

| 设置 | 谁读 | 默认 | 说明 |
|---|---|---|---|
| `SANDBOX_CONTROLLER_URL` | Worker、Scheduler | 不变 | 新增取值 `pool` |
| `SANDBOX_CONTROLLER_NAME` | controller | `default` | §4.1 |
| `SANDBOX_CONTROLLER_ADVERTISE_URL` | controller | 空 | 主机池里必填，`tls://` |
| `SANDBOX_HOST_STALE_SECONDS` | Worker | 30 | 放置时跳过多久没心跳的主机 |
| `SANDBOX_SWEEP_SECONDS` | controller | 300 | 已有，§4.7 |

启动时校验：`pool` 模式下 controller 没有 `ADVERTISE_URL`，或它不是 `tls://`，拒绝启动；
同一个名字被两台不同的 controller 同时心跳（`url` 来回变），controller 记错误日志。

## 6. 数据库迁移

一个迁移，可以在线做：

- 建 `sandbox_hosts`。
- `sandbox_instances.controller`：先加可空列，回填 `default`，再设非空和默认值。
- `sandbox_instances (controller, status)` 加索引，给按主机求和用。

降级：删列、删表。只在只有 `default` 一台时可以降级，迁移里检查并拒绝。

## 7. 规格要改的地方

- 产品设计 §21.3：准入预算是「每台沙箱主机」的，不是全平台的。版本号加一。
- 产品设计 §22：部署形态加主机池，指向本文。
- 技术设计 §11.1、M1 沙箱设计 §5.1：controller 只对自己名下的沙箱负责。
- 会话工作区设计 §9 第 3 条：清扫由 controller 自己做，不是 Scheduler。
- 容量方案 §3 C 档：链接到本文。

## 8. 测试与验收

按项目约定：测试先写，单独提交。跨进程、跨网络的边界用真的 TLS、真的 Docker、真的 Postgres。

**单元测试**

- 放置：有 reservation 只去原主机；按剩余内存排序；跳过 `draining`、`retired` 和心跳过期的；
  `memory_budget_exhausted`、`NotConnected` 换下一台，`sent=True` 不换；全都不行时结束时间片。
- `pool` 以外的取值行为不变。

**集成测试**

- 两个 controller（两个名字、同一个 Docker）：各自的准入只数自己的；A 缺内存不驱逐 B 的；
  同一个地址在两台各登记一次不冲突，代理按名字认对 Run。
- controller 拒绝别的主机名下的 `sandbox_id`。
- 孤儿卷、孤儿容器清扫：删掉没人引用的；不删有活 reservation 的、有活租约的；和并发的
  acquire 不抢。
- `retire`：reservation 释放、地址清掉、等待清理确认的 Run 走到原定的状态；被 retire 的
  主机连回来后拒绝调用。
- Scheduler：一台连不上时，另一台的回收照常进行。

**现场验收**

1. 本机用三个互不相通的 Docker 网络模拟两台沙箱主机加一台 Worker 主机，只经 TLS 互通。
2. 40 个带工具的 Run，看沙箱是否大致平分到两台；每台的内存占用都在各自预算内。
3. 长任务在 A 上冻结，下一个时间片仍在 A 上解冻（`cache_state = reused`）。
4. 把 A 整个停掉：A 上的 Run 卡住、重试，**不**在 B 上另建沙箱；`retire A` 之后它们在 B
   上冷恢复并完成。
5. `drain B`：新 Run 都去 A，B 上的冻结沙箱到期后清零。
6. 单机 compose-e2e 不改，照样要绿。

本机模拟测不出真实网络下冷恢复的带宽成本，验收记录要写明。

## 9. 实施顺序

每一步单独合并，每一步之后单机行为不变。

1. ~~孤儿卷和孤儿容器清扫~~：已经有了（§3 第 7 条）。
2. 迁移和 `controller` 列；准入、腾内存、清扫按主机算（单机下只有 `default`，结果不变）。
3. `sandbox_hosts` 登记和心跳；地址池不重叠的检查。
4. `SandboxPool`：放置和路由；Scheduler 按主机回收。
5. `scripts/sandbox_hosts.py`：drain、activate、retire；运维手册。
6. 现场验收和规格修订。

## 10. 这份设计替产品做的决定

需要确认，不同意的话改起来都不难：

- **放置按剩余内存分散**，而不是先塞满一台。分散让单台故障影响的 Run 少，代价是每台都要
  一直开着。
- **主机死了要人确认才释放**（§4.8）。自动释放会在网络分区时造成一个 Run 两个沙箱。代价是
  主机死掉到有人 `retire` 之间，它上面的 Run 一直在等，直到预算耗尽暂停。
- **不做冻结沙箱的跨主机迁移。**满了就等，或者换主机冷启动。
- **主机登记放在数据库里**，而不是每个 Worker 配列表（§4.1）。

## 11. 风险和未决

- ~~同一台主机上重建沙箱时卷是旧的。~~ 已处理：`acquire` 新建实例前调 `_clear_leftovers`，
  先删掉这个 Run 在本机留下的容器和卷（2026-10-23 修订时确认）。
- **Postgres 是下一个瓶颈**（§2），这份设计不解决。
- **一台主机的 controller 等于那台的 root**，B 档 §10 的话对每一台都成立；主机越多，要保管的
  私钥越多。
- 剩余内存是按上限算的（§21.3），不是按实际用量；主机上实际很空时也可能被判满。这是
  现在的准入规则，C 档不改。
- 没有考虑沙箱主机规格不同（CPU 多少不一）：放置只看内存。Docker exec 的每秒上限是按主机的，
  CPU 少的那台会先慢下来。

## 修订记录

**2026-10-23**，按合并了 #124 到 #136 之后的代码：

- §3 第 7 条和 §11 第一条风险已经被后来的改动解决，§9 第 1 步删掉。
- §4.6：出站地址不再改主键，改成要求各台主机的地址池互不重叠（v2.21 起每个沙箱一个网络，
  地址都从本机的池里分）。§6 的迁移因此不再动 `sandbox_egress_addresses`。
- §4.7：清扫改成在现有 `sweep()` 上加「在本机名下」的判断。
- 准入按 Agent 声明的内存计（v2.22），放置时的「剩余内存」照此计算，不用另外改。
