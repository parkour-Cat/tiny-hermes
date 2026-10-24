# C 档第 2 步：准入、驱逐、调用都按沙箱主机分开

日期 2026-10-23。分支 `feat/sandbox-host-admission`。[C 档设计](../specs/2026-10-22-tier-c-sandbox-host-pool-design.md)
§4.2、§4.5、§9 第 2 步；产品设计 v2.23（§1.23、§21.3、§22）。

这一步之后**仍然只能有一台沙箱主机**：Worker 和 Scheduler 还不知道有多台，主机登记、放置、
路由是后面几步。这一步做的是让 controller 只对「自己名下」的东西负责，并且单机行为不变。

## 做了什么

| 部分 | 内容 |
|---|---|
| 迁移 0078 | `sandbox_instances.controller`，非空，默认 `default`；已有的行都是 `default`。索引 `(controller, status)`。降级时如果有别的主机名的行就拒绝 |
| 配置 | `SANDBOX_CONTROLLER_NAME`，默认 `default`，只能是小写字母、数字和连字符（最长 63），Compose 已透传 |
| 准入 | 每台主机一把锁（`pg_advisory_xact_lock(key, hashtext(name))`），只数本机的实例 |
| 驱逐 | 只挑本机的冻结实例 |
| 拒绝 | 对别的主机名下的沙箱的任何调用，以及 Run 的活 reservation 在别的主机时的 acquire，都拒绝为 `reservation_elsewhere`，什么也不做 |
| 清扫 | 不用改：判断「有没有记录」看的是容器 id，共用一个 Docker 时也不会删掉别的主机的容器 |

## 测试过了

- 集成测试（真 Docker、真 Postgres，两个名字的 controller 共用一个 Docker 和一个数据库）：
  - 实例记录了建它的主机名；
  - 预算 512 时 A 上已有两个 256 被判满，B 上照样能建；
  - A 缺内存时不驱逐 B 上冻结的沙箱，B 的容器仍是 `paused`；
  - B 收到 A 名下沙箱的 freeze，拒绝为 `reservation_elsewhere`，A 的容器仍在运行；
  - Run 在 A 上有冻结的沙箱，它的 acquire 到了 B，被拒绝，B 上什么也没建；
  - A 的准入锁被一个打开的事务拿着时，B 的 acquire 20 秒内正常完成，没有被挡住。
- 配置：默认名字是 `default`；`SBX`、带空格、带斜杠、以连字符开头、64 个字符的名字都被拒绝。
- `alembic upgrade head`、`downgrade -1`、再 `upgrade head` 都通过；`alembic check` 没有差异。
- 先写测试、看它失败、再实现，提交顺序是测试在前。
- 已有的两处测试按新列和新参数改了：`sandbox_instances` 的列白名单加了 `controller`（这一列存的是
  主机名，不是路径，配置的格式不允许出现斜杠）；单元测试里的假 store 接受新的参数。

全量（本地）：单元 2719 passed；集成 1301 passed、1 skipped、**1 failed**。失败的是
`test_run_events_sse.py::test_a_disconnected_subscriber_leaves_no_open_transaction`：它统计**整个测试库**
里 idle in transaction 的连接，数到 1 个。这条测试走 SSE，不经过沙箱；setup 阶段的日志里有「垃圾回收
在清理一个没归还的连接」，像是前面某条测试留下的连接被数了进来。单独连跑这个文件 5 次都是
11/11。之前的记录里它也在本机高负载时偶发失败过。没有改它，CI 的结果见 PR。

## 这一遍没能证明什么

- **没有两台真的主机。**两个 controller 共用一个 Docker，测的是数据库这一侧按主机分开，不是网络
  上的两台机器。
- 新旧 controller 混跑时，旧的用单参数的锁，新的用双参数的锁，两者互不排斥。一台主机只跑一个
  controller，升级时先停旧的再起新的就没有问题；同时跑两个会让两个 acquire 同时数到同一块空间。
  运维手册里还没写这一条，放到第 5 步一起写。
- Worker 收到 `reservation_elsewhere` 会让 Run 失败（`sandbox_reservation_elsewhere`）。单机下走不到，
  第 4 步做路由时要改成按原主机重试。
- 沙箱内存的监控指标仍然是整个池的合计，没有按主机分。

## 不声称什么

- 不声称现在能部署多台沙箱主机。
