# 唤醒要在建 Run 的事务提交之后发

分支 `feat/enterprise-capacity`（合并分支）。未推送、未合并。测试 `164f7b9`，修复 `a0eed4c`。

## 怎么发现的

推送之前，在本机照着 CI 跑 compose-e2e 的几个步骤。重启演练失败了：

```
FAILED: no Run was picked up in under the 2s idle poll (['2.06', '2.05', '2.13']),
so the wake-up channel never recovered and the Worker is polling for everything
```

第 1 个场景（还没碰 Redis）领取就用了 2.08 秒，所以问题跟 Redis 重启无关，唤醒从一开始就
没起作用。

## 复现

空闲的栈、1 个 Worker，每 5 秒提交 1 个 Run，一共 10 个，看每个 Run 的
`started_at − created_at`：**10 个都是 2.02 秒**，正好是一个空闲轮询间隔。

排查过程：

- 在 Redis 上挂一个旁听订阅者，能收到 API 发出的唤醒；Worker 也在订阅（订阅数为 2）。
  所以 API 发了，Worker 也收着。
- 建 Run 的路由在函数体里调用 `_announce`，也就是 publish；而提交是在 `run_coordination`
  这个依赖的 `else: await session.commit()` 里做的，要等路由函数返回之后才执行。
  `submit_run` 和 `SqlRunStore` 自己都不提交。

所以顺序是：先发唤醒，Worker 立刻用自己的连接去领，Run 还没提交，领不到，睡满 2 秒，
然后事务才提交。

`_announce` 的 docstring 写的是「在创建工作的事务提交之后通知 Worker」，代码并不是这样做
的。已有的 `test_run_creation_publishes_after_the_transaction_commits` 是在 HTTP 响应回来
之后才去看通知，那时事务早就提交了，所以它**从来没有检验过先后顺序**，一直是绿的。

这个 bug 从加唤醒（`c3ff582`，2026-08-10）那天起就在，本质是一次竞争：Worker 的领取查询
和 API 的提交谁先到，相差只有几毫秒。

- 多个 Worker 时，总有一个查得晚，能看到已提交的 Run，所以演练以前能过。
- 这条分支上，只有 1 个 Worker 时每次都输。一个说得通的解释是 `ix_runs_claim_order` 让领取
  查询变快了，这一点没有证实。

## 修复

- 路由不再自己发唤醒，改成 `RunCoordination.announce(workspace_id, run_id)`，只登记。
- `run_coordination` 在 `session.commit()` 之后，把登记的逐个发出去。
- 被覆盖的路径：`create_run`、`retry_run`、终端用户建 Run。
- 其余发唤醒的地方本来就没问题：
  - `completions.admit_chat` 在函数里自己提交之后才返回；
  - scheduler 的 `_announce` 是在它改状态的事务之外、另开只读会话之后才发。

## 测试过了

- 新测试 `test_a_new_run_is_committed_before_its_wake_up_is_published`：在 publish 的那一刻，
  用另一条连接查这个 Run 是否可见。修复前 3/3 是 `[False]`，修复后 3/3 通过。
- 后端单元 2473 条通过；集成 `runs` + `channels` + `identity` 728 条通过。
- 在 `a0eed4c` 上照 CI 的步骤在本机完整跑了一遍（不含 compose-e2e）：

  | 步骤 | 结果 |
  |---|---|
  | ruff、pyright | 通过；pyright 0 errors |
  | 后端单元 | 2473 通过 |
  | 后端集成（含 sandbox） | 1104 通过 |
  | 并发相关测试 ×10 | 通过 |
  | 集成，Redis 不可达 | 1099 通过，5 跳过 |
  | alembic check、迁移链 | 通过 |
  | web lint、build；chat lint、test（88）、build | 通过 |
  | web test | 见下 |

- web test 连跑了三次整套：第一次 2 条失败（`AgentDetailPage`、`AgentModelPolicy`），第二次
  1 条失败（`SkillsPage`），第三次 391 条全过。三次挂的不是同一批，报错都是「等不到某个元素」，
  单独跑这几个文件都通过。这条分支对 `apps/` 的改动只有 `types.ts` 里给场景的联合类型加了
  一个字面量，不会改变任何组件的行为。所以把它记成本机负载下的不稳定，**不是**证明了它和
  这条分支无关——那要在 main 上跑出同样的失败才算，这一遍没有做。

## 走通了什么

镜像重建连续三次失败：构建里 `uv sync` 从 `files.pythonhosted.org` 取 hatchling 超时。同一个
地址，从宿主机和普通容器都能在 1–2.5 秒内取到。构建步骤里的 DNS 解析到的是代理的假地址
`::ffff:198.18.0.239`（CLAUDE.md「跑不动的时候」那一节记过这种现象）。

所以这一遍的现场验证，是把改动的 4 个文件拷进正在运行的 api 容器，再重启它，并在容器里
核对过修复代码确实在：

| 检查 | 修复前 | 修复后 |
|---|---|---|
| 空闲时 10 个 Run 的领取等待 P50 / 最大 | 2.02 / 2.02 s | **0.02 / 0.03 s** |
| 重启演练 | 失败（领取 2.06、2.05、2.13 s） | **四个场景全部通过**，领取都在 0.12 s |

这也解释了 [并发探针](2026-09-29-concurrency-probe.md) 里记的「空闲 Worker 领取也不是立刻的
……没查原因」。

## 同一次本机 e2e 里，另一个失败是环境造成的

`tools.spec.ts` 的「注册 HTTP 工具并调用」失败，出口代理的日志先是 `port_not_allowed`，放开
端口之后是 `private`：

- CI 给这个 job 设了 `EGRESS_ALLOWED_PORTS=80,443,8000` 和 `OUTBOUND_ALLOWED_CIDRS=172.16.0.0/12`；
- GitHub 机器上 compose 网络是 172.x，本机 OrbStack 上是 `192.168.107.0/24`。

把 `OUTBOUND_ALLOWED_CIDRS` 换成本机的网段后，这条测试通过了（19 秒）；测完已恢复成空。

## 这一遍没能证明什么

- **修复后的镜像没有在本机构建成功过。** 现场验证用的是拷文件，不是镜像。CI 会从头构建镜像，
  那才是第一次真正构建。
- 「是索引让领取变快，才让竞争每次都输」只是一个说得通的解释，没有测过。
- 终端用户建 Run 这条路径的改动，没有单独的测试；它和控制台路径共用同一个依赖和同一种写法。

## 不声称什么

- 不声称 API 里再也没有「在事务里发外部通知」的地方。这一遍只查了唤醒的 publish。
