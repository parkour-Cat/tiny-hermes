# B 档：Worker 跨机器，controller 走网络

日期 2026-10-21。[容量方案](../plans/2026-09-29-enterprise-capacity.md) §3 B 档的设计。
状态：已实现（§8 的 5 步），验收见 [B 档验收记录](../verification/2026-10-21-tier-b-acceptance.md)。

## 1. 要解决什么

Worker 现在只能和 sandbox-controller 跑在同一台机器上：带工具的 Run 要经过一个 Unix socket
找 controller，而这个 socket 放在只有 Worker、Scheduler、controller 才挂载的 Compose
volume 里（`deploy/compose/compose.yaml` 的 `controller-socket`）。Worker 本身已经无状态：Run
的真相在 Postgres 里，领取用 `SKIP LOCKED` 加队首校验。所以唯一的阻塞就是这条 socket。

B 档要做到：**Worker 可以跑在任意台机器上，通过网络调用一台沙箱主机上的 controller。**

## 2. B 档换来什么，换不来什么

先把期望说清楚，免得把 B 档当成「6000 人」的答案。

**换来的：**

- Worker 侧的 CPU 不再受一台机器限制。单机档验收里，2 个 Worker 容器在 40 路时
  CPU 峰值 85% 到 88%（[单机档验收](../verification/2026-09-30-tier-a-acceptance.md)）；
  真实模型每路要多少 CPU 还不知道，很可能更高。
- 不带工具的 Run（纯对话）只用 Worker、Postgres 和模型，B 档之后可以靠加 Worker 主机扩容。
- 沙箱主机上不再跑 Worker，省出的 CPU 和内存都给沙箱。

**换不来的：**

- **带工具的并发上限不变。** 沙箱仍然只在一台主机上：Docker exec 每秒约 140 次封顶
  （[每轮开销记录](../verification/2026-09-29-round-overhead.md)），沙箱内存按准入预算算。
  要突破，得做 C 档（多台沙箱主机）。
- Postgres 的上限不变。40 路时 Postgres CPU 峰值已经 224%，B 档只会让它更忙。

## 3. 代码里的「只有一台主机」假设

读代码找到的。B 档只处理第 1、2 条，其余的留给 C 档（§9），并且 B 档用配置保证
只有一个 controller，让那些假设继续成立。

1. **传输是 Unix socket。** `ControllerClient` 用 `asyncio.open_unix_connection`，
   `ControllerServer` 用 `asyncio.start_unix_server`。
2. **「连得上 socket」就是全部的调用者身份。** `transport/server.py` 的模块注释写明了：
   能连上的只可能是 Worker 或 Scheduler 之一。技术设计 §11.1 说 Scheduler 用「独立的
   `sandbox.cleanup` 内部权限」，但这份权限**不是凭证**：Scheduler 什么也不出示，
   controller 也分不出谁在调用。`cleanup` 真正的保护是「这个 Run 没有活着的租约」
   （`controller.py` 的 `cleanup`）。Worker 也会调 `cleanup`（`worker.py`）。
3. 准入预算 `committed_memory_mb()` 对**全库**的活跃 reservation 求和，不分主机。
4. 腾内存时的 `evictable_keeps()` 也是全库的。
5. `sandbox_egress_addresses` 以地址为主键。两台主机上的沙箱网络可以发出同一个地址。
6. Scheduler 的 `_reclaim_sandboxes` 把所有 reservation 发给同一个 controller。
7. 每个 Run 的 Docker volume 只在建它的那台主机上。

## 4. 设计

### 4.1 传输：同一个协议，跑在 TLS 上

**协议不变。**每行一个 JSON 的控制调用、流式动作的帧格式、64 KiB 行上限、帧上限、
credit、30 秒空闲规则、PROGRESS 心跳，全部照旧。只换承载它的连接：

- 服务端：`asyncio.start_server(..., ssl=server_context)`，与 Unix socket 二选一。
- 客户端：`asyncio.open_connection(host, port, ssl=client_context)`。

不改成 HTTP 或 gRPC：现有协议当初选 JSON 行，是为了不给 phase-3A「不自建 HTTP 客户端」
的禁令开第二个例外（`server.py` 的模块注释）。这个理由到网络上依然成立，
而且流式、背压和校验和已经在帧层做好了，换协议等于重写一遍再测一遍。

**地址写法。**一个配置项 `SANDBOX_CONTROLLER_URL`，两种形式：

| 写法 | 含义 |
|---|---|
| `unix:///run/tiny-hermes/controller.sock` | 现在的样子，单机 Compose 的默认值 |
| `tls://sandbox-1.internal:7443` | B 档 |

controller 一侧用 `SANDBOX_CONTROLLER_LISTEN`，写法相同。只接受这两种 scheme，
不接受明文 `tcp://`：controller 握着 Docker socket，等于宿主机 root，不能有一条
不认证的路通向它。

现有的 `SANDBOX_CONTROLLER_SOCKET` 保留一个版本，作为
`unix://<它的值>` 的旧写法；两个都填就拒绝启动。

### 4.2 身份：双向 TLS，证书带角色

- 部署时生成一个**私有 CA**，只用于 controller 这一条链路，不复用对外 HTTPS 的证书。
- controller 有服务端证书，SAN 写它的主机名；客户端用这个 CA 校验它（`check_hostname`
  打开）。
- Worker 和 Scheduler 各有客户端证书，controller 用 `CERT_REQUIRED` 校验，CA 同上。
- **证书带角色。**客户端证书的 Subject `OU` 是 `worker` 或 `scheduler`。controller
  在握手后读出 OU，**Scheduler 证书只允许 `cleanup`**（它今天调用的唯一动作），其余动作
  以 `role_not_permitted` 拒绝。Worker 证书允许全部动作。

  这让技术设计 §11.1 那句「Scheduler 使用独立 `sandbox.cleanup` 内部权限」第一次有了
  对应的凭证。走 Unix socket 时没有证书，行为和现在一样：不分角色，只靠租约检查。
- **租约检查一条不减。**证书证明的只是「这是平台自己的 Worker 或 Scheduler」，和今天
  「连得上 socket」是同一类事实。技术设计 §11.1「仅能连接 socket 不能替代这些校验」
  照样适用：每次调用照旧核对 reservation 归属和 `worker_lease_id`。
- 证书和私钥用文件挂载给容器，路径通过配置传入（§5）。私钥文件权限 0600，属主是进程的
  运行用户。

不做：证书吊销列表、自动轮换。轮换就是换文件再重启进程，写进运维文档。证书有效期
建议 1 年，剩不到 30 天时，每个进程**启动时**在日志里警告。（初稿写的是「启动日志和健康
检查」；健康检查只看进程在不在，没有加这一项。进程只在启动时读证书，所以长期不重启的进程
不会再次警告。）

### 4.3 controller 的网络暴露面

- 只监听沙箱主机的**内网地址**，不监听 `0.0.0.0`。`SANDBOX_CONTROLLER_LISTEN` 里写
  具体地址；写 `0.0.0.0` 或 `::` 时拒绝启动，除非显式设置
  `SANDBOX_CONTROLLER_LISTEN_ANY=true`。
  - Compose 里正是这种情况：controller 在自己的容器里监听 `0.0.0.0`（那只是容器自己的
    接口），主机对外暴露什么由端口绑定决定，`compose.sandbox-host.yaml` 把它绑在
    `CONTROLLER_PRIVATE_ADDRESS` 上。
- 运维文档要求防火墙只放行 Worker 主机和 Scheduler 所在主机到这个端口。
- 沙箱容器所在的 `sandbox-egress` 网络是 `internal` 的，沙箱本来就到不了宿主机的内网
  地址。这一点要在验收时实测，不能只靠推理（§7）。

### 4.4 超时、断线和「结果未知」

`ControllerClient.Unreachable` 的含义不变：**平台没能问到，结果未知**，不是「被拒绝」。
Worker 遇到它时的处理照旧，例如冻结失败就进 `interrupted`，绝不悄悄重新排队
（M1 沙箱设计 §11）。

网络让这种情况更常见，所以有几条要写清楚：

- **获取沙箱时联系不上。**（2026-10-21 实现后改过，原先是判失败。）按请求有没有可能送到
  controller 分两种：
  - **确定没送出**：在 `connect()` 里就失败，包括连接被拒、socket 不存在、连接超时、
    客户端不信任 controller 的证书。controller 不可能做了什么，Worker 结束时间片，
    Run 30 秒后可以再被领取。每次尝试都要花掉要工具的那一次模型调用，所以等多久受 Run
    自己的预算约束，预算用完就按预算暂停。
  - **可能送到了**：连上之后没有回答，包括超时。这次 acquire 可能已经建好了 reservation，
    只是回答丢了。直接重新排队的话，下一次 acquire 会一直撞上 `already_reserved`：
    Scheduler 只隔离并回收 interrupted Run 的 reservation。所以走 `interrupted`，由
    Scheduler 隔离、回收，然后恢复（最多 `MAX_RECOVERY_ATTEMPTS` 次）。
  **Worker 自己的证书过期属于第二种**：TLS 1.3 里，服务端检查客户端证书发生在客户端
  握手完成之后，客户端只看到请求写出去后连接被关掉，分不出这和「controller 做完就挂了」。
  所以证书过期时，带工具的 Run 会在几次恢复之后失败，到期警告出现后就要续签。
- **执行中途断线。**controller 那边的 `execute_streamed` 会在下一次 `sink.deliver` 时
  写失败；命令本身在容器里继续跑，直到超时或容器被销毁。这和今天 Worker 进程被杀时的
  行为一样，B 档不改。Worker 得到 `Unreachable`，这一轮的结果按未知处理。
- **TCP keepalive。**客户端和服务端都打开，空闲 30 秒开始探测，免得半开的连接一直挂着。
  流式动作已经有 PROGRESS 心跳和 30 秒空闲规则，控制调用有 120 秒超时，keepalive
  只是兜底。
- **连接还是每次调用新开一条。**今天就是这样。每轮开销记录里一次工作区提交要
  约 15 次串行调用，所以握手成本会被放大 15 倍。**要实测**（§7），超过每轮 50 ms 再考虑
  连接池。
  - 初稿写「复用 `SSLContext` 让 TLS 1.3 的会话恢复生效」，**这是错的**：asyncio 不提供把
    上一次的 TLS 会话交给新连接的办法，复用 context 不会触发会话恢复，每次调用都是完整握手。
    实测见验收记录。

### 4.5 Worker 主机还要连什么

controller 之外，Worker 本来就要连这些。单机 Compose 里它们在同一个 Docker 网络上，
跨机器之后都要走内网，部署文档要写：

| 依赖 | 说明 |
|---|---|
| Postgres | 每路约 1.5 条连接。总路数超过 100 就要加连接池代理（容量方案 §3 A 档） |
| Redis | |
| 对象存储（SeaweedFS） | 工作区按版本存在这里，冷恢复时 Worker 下载再流给 controller |
| 模型厂商、飞书等外部服务 | Worker 自己的出站，走 `SafeOutboundClient`，受 Worker 主机的网络策略约束 |

**Worker 自己的出站代理没有加密。**Worker 的模型调用等出站请求走 `EGRESS_PROXY_URL`。
代理在沙箱主机上时，Worker 主机经内网连它，代理令牌是明文传的。B 档没有处理，运维手册
要求只在可信内网上这样部署。

**工作区流量会穿过网络。**冷恢复时，Worker 从对象存储下载工作区，再通过
`workspace_import` 流给 controller；工作区上限 2 GiB。内网带宽要按「同时冷恢复的 Run 数 ×
工作区大小」估算。这件事 B 档不优化。

### 4.6 只允许一个 controller

B 档的配置是**一个地址，不是列表**。§3 的第 3 到 7 条都默认只有一台沙箱主机，
多 controller 要 C 档才安全。所以：

- Worker 和 Scheduler 只读一个 `SANDBOX_CONTROLLER_URL`。
- 这不是靠「不小心」保证的，而是配置的形状本身就放不下第二个。

### 4.7 部署形态

Compose 增加一个文件，单机的 `compose.yaml` 保持不变。

- **沙箱主机：**controller、egress-proxy，以及（看规模）Postgres、Redis、对象存储、
  Scheduler、API、Web。controller 改用 `tls://` 监听。
- **Worker 主机：**新文件 `deploy/compose/compose.worker-host.yaml`，只有一个 `worker`
  服务，连接串全部指向沙箱主机或独立的数据库主机，挂载自己的客户端证书。
  `--scale worker=N` 照旧可用。

证书用新脚本 `scripts/controller_certs.py` 生成。项目已经依赖 `cryptography`，
不需要新依赖。输出：CA、controller 服务端证书（SAN 由参数给出）、一张 `worker` 证书、
一张 `scheduler` 证书。

## 5. 配置

| 设置 | 谁读 | 默认 | 说明 |
|---|---|---|---|
| `SANDBOX_CONTROLLER_URL` | Worker、Scheduler | `unix:///run/tiny-hermes/controller.sock` | §4.1 |
| `SANDBOX_CONTROLLER_LISTEN` | controller | 同上 | §4.1、§4.3 |
| `SANDBOX_CONTROLLER_LISTEN_ANY` | controller | `false` | §4.3 |
| `SANDBOX_CONTROLLER_CA_FILE` | 三者 | 空 | `tls://` 时必填 |
| `SANDBOX_CONTROLLER_CERT_FILE` | 三者 | 空 | 自己的证书，`tls://` 时必填 |
| `SANDBOX_CONTROLLER_KEY_FILE` | 三者 | 空 | 自己的私钥，`tls://` 时必填 |
| `SANDBOX_CONTROLLER_SOCKET` | 三者 | — | 旧写法，保留一个版本，见 §4.1 |

启动时校验：`tls://` 缺任何一个文件、文件读不出、证书过期、Worker 或 Scheduler 拿到的
证书 OU 和自己的角色不符，都拒绝启动，并在错误信息里写出该改哪一项。

**没有数据库迁移。**

## 6. 规格要改的地方

- 技术设计 §11.1：「M1 单机 Compose 使用……Unix domain socket」后面补一句：多机部署时
  改为双向 TLS，见本文；Scheduler 的 `sandbox.cleanup` 权限在 TLS 下由证书角色承载。
  「仅能连接 Unix socket 不能替代这些校验」改成「仅能连接 controller（socket 或通过 TLS
  认证）不能替代这些校验」。
- M1 沙箱设计 §5.1：同样的补充。
- 产品设计 §22：部署清单后补一段多机形态，指向本文。版本号加一。
- 容量方案 §3 B 档：链接到本文。

## 7. 测试与验收

按项目的约定：测试先写，单独提交。跨进程、跨网络的边界，夹具必须用对方自己的类型：
**测试用真的 TLS 连接和 `cryptography` 生成的真证书，不手搭握手结果。**

**单元测试**

- 地址解析：两种 scheme、拒绝 `tcp://`、新旧写法同时出现时拒绝。
- 监听地址：拒绝 `0.0.0.0`，除非显式放行。
- 角色到动作的映射：`scheduler` 只能 `cleanup`。

**集成测试**（`tests/integration/sandbox/`，需要 Docker）

- 现有的 controller 测试用 TLS 再跑一遍：参数化传输方式，而不是复制测试。
- 错误的 CA、过期证书、没有客户端证书：握手失败，客户端得到 `Unreachable`。
- Scheduler 证书调 `execute`：`role_not_permitted`，而且容器里没有执行任何东西。
- 流式动作在 TLS 上：2 GiB 上限附近的导入、导出，以及校验和不符时的拒绝。
- 执行中途断开 TLS 连接：Worker 得到 `Unreachable`；controller 不崩，下一次调用正常。

**现场验收**（写进 `docs/superpowers/verification/`）

1. 本机模拟两台主机：controller 和 Worker 放在两个不共享 volume 的 Docker 网络上，
   只能经 TLS 互通。端到端跑一个带工具的 Run，再跑一次冷恢复。
2. 在沙箱里尝试连接 controller 的内网地址和端口，必须连不上。
3. 每轮开销对比：同样的负载，Unix socket 和 TLS 各跑一次，比较每轮 P50、P95。
4. 单机档验收的第 1 档负载，Worker 放在「另一台主机」上重跑，四条标准都要过。
5. 现有的 compose-e2e 不改，照样要绿：单机默认还是 Unix socket。

本机模拟拿不到真实的网络延迟和带宽。真正的两台机器测试要在试点环境里做，验收记录里
要写明这一点。

## 8. 实施顺序

每一步都可以单独合并，单机行为在每一步之后都不变。

1. 配置：`SANDBOX_CONTROLLER_URL`、`SANDBOX_CONTROLLER_LISTEN` 和兼容旧写法，仍然只支持 `unix://`。
2. 传输：服务端和客户端支持 `tls://`；测试参数化。
3. 证书角色：OU 读取和 `scheduler` 只能 `cleanup`。
4. 证书脚本、`compose.worker-host.yaml`、运维文档。
5. 现场验收和规格修订。

## 9. 留给 C 档的事

B 档用「只有一个 controller」让下面这些假设继续成立。C 档要逐条处理：

- `sandbox_instances` 记录所属的 controller（一个名字，不是地址，也不是宿主机路径，
  所以不违反「不保存任意宿主机路径」那条规则）；Worker 和 Scheduler 按它路由。
- 准入预算、腾内存的候选，都按 controller 分开算。
- `sandbox_egress_addresses` 的主键加上 controller；每台沙箱主机一个 egress-proxy，
  只查自己那台的地址。
- Scheduler 的回收按 controller 分组发送。
- 新沙箱放到哪台主机（放置策略），以及名额满时换一台再试。
- 一台沙箱主机彻底死掉时：它上面的 reservation 一直是 isolated，Run 永远拿不到新沙箱。
  要有一个由运维确认的「这台主机已下线」操作，把它们释放掉。

## 10. 风险和未决

- **controller 从「本机才够得着」变成「内网可达」。**它等于宿主机 root。双向 TLS 和
  内网监听是必要条件，不是充分条件：私钥泄露就等于这台主机失守。这一点要写进运维文档的
  安全一节，不能只留在设计里。
- **握手成本**是推算，没测过（§4.4）。
- ~~**获取沙箱时的网络故障会让 Run 失败**~~ 已改，见 §4.4。剩下的是：Worker 证书过期
  仍然会让带工具的 Run 失败；controller 宕机超过 Run 的预算能撑的时间，Run 按预算暂停。
- **Worker 出站代理的链路没有加密**（§4.5）。
- **B 档单独上线的收益取决于真实模型的 CPU 开销。**如果试点测出来 Worker CPU 不是瓶颈，
  B 档的价值主要是给 C 档铺路。
