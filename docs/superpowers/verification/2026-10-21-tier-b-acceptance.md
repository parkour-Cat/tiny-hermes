# B 档验收：Worker 在另一台「主机」上，经双向 TLS 调用 controller

日期 2026-10-21。分支 `feat/controller-address`。
设计 [B 档设计](../specs/2026-10-21-tier-b-networked-controller-design.md)（`182969b`，
实现中修订了三处，见文末「设计哪里写错了」）。

测试和实现分开提交，测试在前：

| 步骤 | 测试 | 实现 |
|---|---|---|
| 1 地址写成 URL，调用方和监听方分开 | `e1f5784` | `4b217ba` |
| 2、3 TLS 传输与证书角色 | `58b32f5` | `6dd5920` |
| 设置接到三个进程 | `3b0d17f` | `23e3c42` |
| 4 Compose 文件、证书脚本、运维手册 | `3f98fad`、`1740c09` | `43cd048` |

## 为什么有这一遍

[容量方案](../plans/2026-09-29-enterprise-capacity.md) §3 的 B 档。Worker 只能和 controller
在同一台主机上，因为带工具的 Run 要经过一个只在本机 Compose volume 里的 Unix socket。

## 测试过了

- 后端单元测试全部通过；ruff、pyright 0 errors。
- 传输的集成测试（`test_transport.py`、`test_transport_streaming.py`）改成两种传输各跑
  一遍：同一套协议测试在 TLS 上全部通过，包括多帧导入导出、5000 条的扫描、静默流的
  心跳和超时。只有「socket 文件不是全局可写」那一条在 TLS 下跳过，因为 TLS 没有那个文件。
- `test_transport_tls.py`：没有客户端证书、别的 CA 签的证书、过期证书、controller 证书上的
  名字不对、拿 Worker 证书冒充 controller，都连不上，而且 controller 一个调用都没收到；
  Scheduler 证书调 `execute`、`acquire`、`destroy`、`inspect` 和打开流，都得到
  `role_not_permitted`，同样没有一个调用到达 controller；TCP 连接打开了 keepalive。
- 证书都由 `certs.py` 签发，也就是部署脚本用的那份代码，握手是真的 `ssl` 握手。
- 沙箱集成测试（真实 Docker）全部通过。

## 走通了什么

**环境。**一台 8 核 / 16 GiB 的 macOS，OrbStack VM 8 CPU / 7.8 GiB，确定性模型。
「两台主机」是这样模拟的：

- **沙箱主机**：`compose.yaml` 加 `compose.sandbox-host.yaml`，`--scale worker=0`。
  controller 监听 TLS，端口只绑在 `127.0.0.1:7443`。
- **Worker 主机**：`compose.worker-host.yaml`，单独的 Compose 项目、单独的 Docker 网络，
  **不挂载 socket volume，也不挂载 Docker socket**（`docker inspect` 看到的挂载只有 CA、
  Worker 证书和私钥三个文件）。它经 `host.docker.internal:7443` 连 controller，经 Mac 的
  局域网地址连 Postgres、Redis 和对象存储。
- 证书用 `scripts/controller_certs.py --host host.docker.internal` 签发。

两个环境上的坑，记下来免得下次再踩：

- 这台 Mac 上装着一个原生 Postgres，占着 `127.0.0.1:5432`。容器里连
  `host.docker.internal:5432` 到的是它，报 `role "tiny_hermes" does not exist`。改用
  局域网地址，那里只有 OrbStack 发布的端口。压测探针自己连库也一样，要设
  `TINY_HERMES_BENCHMARK_DATABASE`。
- `docker compose -f compose.worker-host.yaml down` 也要求那些必填变量，不设就报错退出，
  **而容器照样在跑**。见下面第 6 条。

### 1. 带工具的 Run 走通，工作区经 TLS 流传输

同一个会话里两个 Run：第一个在 `/workspace/data` 写 `note.txt`，第二个（新沙箱，工作区
从对象存储导入）读出 `tier-b`。两个 Run 的 `run_lease_acquired` 都是 Worker 主机上的
Worker（`0a93eb142aab`）。这个 Worker 没有别的路能到 controller，所以导入、执行、导出
都是经 TLS 走的。

### 2. 沙箱连不到 controller

在沙箱里逐个尝试 TCP 连接：controller 容器的地址、`host.docker.internal:7443`、Mac 的
局域网地址、沙箱网络的网关、`controller:7443`，**全部连不上**。对照组：同一条命令连
egress-proxy 的 3128 端口，连得上，说明探测方法本身有效。

### 3. 角色在真实的 controller 上生效

拿各个角色的证书直接连运行中的 controller：

| 证书 | 动作 | 结果 |
|---|---|---|
| scheduler | `execute`、`destroy` | `role_not_permitted` |
| scheduler | `cleanup` | `no_reservation`（过了角色检查，被 controller 自己的规则拒绝，这次用的 Run 是编的） |
| worker | `inspect` | `no_reservation` |
| controller（服务端证书当客户端用） | `inspect` | 连接关闭，没有回答 |

### 4. Scheduler 经 TLS 真正回收了一个沙箱

Run 执行 `sleep 60` 时，用 `docker kill` 杀掉 Worker 主机上唯一的 Worker。21 秒后（租约 20 秒），
`sandbox.cleanup` 审计从 34 条变成 35 条，沙箱容器消失。那时没有任何 Worker 活着，能发出
这次调用的只有 Scheduler。重启 Worker 后，这个 Run 被恢复，在新沙箱里跑完，中间经历了
一次时间片结束和恢复（冻结、保温、解冻都是 TLS 调用），状态 `completed`。

### 5. 执行中途杀掉沙箱，照旧中断再恢复

和[第 9 项记录](2026-10-21-sandbox-oom.md)同样的做法：命令执行到第 3 秒时杀掉沙箱容器。
Worker 主机上的 Worker 冻结失败，原因 `ContainerNotRunning`，经 TLS 原样传回；Run 进入
interrupted，然后恢复，reservation 释放、实例 destroyed。

### 6. Worker 联系不上 controller 时，带工具的 Run 失败

这一条不是设计好的测试，是意外撞到的：切回单机栈时，Worker 主机上的 Worker 没有停掉
（`down` 报错退出了），它去连一个已经不再监听 TLS 的 controller，领到的 2 个 Run 都判了
`sandbox_start_failed`。这符合 M1 沙箱设计 §11「启动失败不重试」，但跨网络之后，一次短暂的
网络故障或者证书过期都会这样让用户的 Run 失败。见「发现」第 1 条。

### 7. 每次调用多花多少时间

**单次控制调用**（同一台 Mac，进程内的服务端，一个空操作，各 500 次）：

| | P50 | P95 |
|---|---:|---:|
| Unix socket | 0.114 ms | 0.126 ms |
| TLS | 1.261 ms | 1.298 ms |

每次调用多约 1.15 ms，这是本机回环上的握手成本，没有网络往返。asyncio 没法复用 TLS 会话，
所以每次调用都是完整握手。

**端到端**（每次 15 个带工具的 Run，从领取到完成，各跑两次）：

| | 第一次 P50 / P95 | 第二次 P50 / P95 |
|---|---|---|
| 单机，Unix socket | 579 / 606 ms | 586 / 705 ms |
| Worker 在「另一台主机」，TLS | 669 / 757 ms | 675 / 784 ms |

中位数多约 90 ms，每个 Run 两轮，约合每轮 45 ms。**这 90 ms 不全是 TLS**：模拟出来的 Worker
主机连 controller、数据库、Redis、对象存储，都要先出 VM 到 Mac 再回来，而单机栈全在 VM 内部。
这一遍分不开这两部分。

### 8. 单机档第 1 档负载，Worker 全在「另一台主机」上

和[单机档验收](2026-09-30-tier-a-acceptance.md)第 1 档相同：2 个 Worker 容器 × 16 路，
16 个长任务各 40 轮，之后每秒 2 条短消息、持续 150 秒，模型延迟 2 秒，
`AGENT_MAX_MODEL_CALLS=50`。两个 Worker 都在 Worker 主机上。另外把每个沙箱的上限设成 256 MiB、
预算 4608 MiB，让 16 个长任务的沙箱都放得下：9-30 那一遍还没有准入预算。

```bash
uv run --no-sync python scripts/concurrency_probe.py \
  --rate 2 --seconds 150 --long-tasks 16 --rounds 40 --label tier-b-l1
```

| | 9-30 单机档第 1 档 | 本次 |
|---|---:|---:|
| 同时执行最多 | 21 | 21 |
| 完成 / 失败 | 316 / 0 | 316 / 0 |
| 短消息排队 P50 / P95 / 最大（秒） | 0.01 / 0.02 / 0.25 | 0.01 / 0.03 / 0.06 |
| 短消息端到端 P95（秒） | 2.1 | 2.15 |
| 长任务端到端 P95（秒） | 102 | 105.3 |
| 同一时刻的沙箱最多 | 16 | 16 |
| 数据库连接最多 | 50 | 53 |

四条标准：短消息排队 P95 在 10 秒以内，过；长任务全部完成、没有中断失败，过；Worker CPU
（另外每约 4 秒一次 `docker stats`，39 个样本）两个容器 P90 分别是 17% 和 49%，最大 54%，过；
全栈内存峰值 1755 MiB，约为 VM 的 22%，过。

长任务端到端多了约 3 秒，40 轮平均每轮约 80 ms，和第 7 条量级一致，同样包含出 VM 再回来的那段。

## 发现

1. **获取沙箱时联系不上 controller，Run 直接失败。**见上面第 6 条。连接阶段（TCP 或 TLS
   握手）就失败时，请求确定没有送到 controller，按理可以像「沙箱名额不够」那样结束时间片、
   稍后重试，而不是判失败。没有改：这改的是 M1 沙箱设计 §11 刚修订过的行为，需要单独决定。
2. **Worker 自己的出站代理不加密。**Worker 主机经内网连沙箱主机上的 egress-proxy，代理令牌
   明文传输。运维手册写明只在可信内网上这样部署；没有改。
3. **`compose -f compose.worker-host.yaml down` 也要那些必填变量。**不设就报错退出，容器
   照样在跑，而且会接着领 Run。已写进运维手册 §7.3。

## 设计哪里写错了

实现中发现、已在设计里改正（`2026-10-21-tier-b-networked-controller-design.md`）：

- §4.4 说「复用 `SSLContext` 让 TLS 1.3 的会话恢复生效」。不对：asyncio 不提供把会话交给新
  连接的办法，每次调用都是完整握手。代码注释一开始也照这句话写了，提交前改掉了。
- §4.2 说到期警告来自「启动日志和健康检查」。只做了启动日志。
- §4.4 只举了冻结失败的例子，没写获取沙箱时联系不上会让 Run 失败（发现 1）。

## 这一遍没能证明什么

- **没有用两台真实的机器。**「另一台主机」是同一个 OrbStack VM 里的另一个 Docker 网络，
  流量绕经 Mac。真实内网的延迟、带宽、丢包，以及防火墙规则，都没有测。
- 第 7 条的 90 ms 分不开 TLS 的成本和绕经 Mac 的成本。
- 私钥属主：OrbStack 的绑定挂载下，Worker 和 Scheduler 容器（uid 10001）读得到属主是 Mac
  用户、权限 0600 的私钥。在 Linux 主机上读不到，运维手册要求 `chown 10001`，但这一步没有在
  Linux 上验证过。
- 证书过期的情况只在测试里测过（过期证书握手失败），没有让一个运行中的部署的证书真的过期。
- 负载只跑了单机档第 1 档；第 2 到 4 档没有用 Worker 主机重跑。
- 只有一台沙箱主机，C 档的任何问题都没有碰到。

## 不声称什么

- 不声称 B 档提高了带工具的并发上限：沙箱还在一台主机上，第 8 条的数字和单机档相同，
  本来也应该相同。
- 不声称每轮 45 到 80 ms 的额外开销就是真实网络上的数字。
- 不声称 controller 对外开放之后是安全的：双向 TLS 和只绑内网地址是必要条件。私钥泄露就等于
  那台沙箱主机失守，防火墙要按运维手册配置。
