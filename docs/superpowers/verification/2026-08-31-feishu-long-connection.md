# 长连接住进 scheduler — 2026-08-31

> 产品：spec §19.2（飞书私有部署默认用长连接，不要求公网入站地址）。
> 范围：Task 4（4-task 实施计划的最后一块）——给 Task 3 已经写好的
> `FeishuLongConnection` 找一个跑起来的地方，并让断线留下痕迹。

## 1. 做了什么

1. `feishu_long_connection.py` 新增 `RecordConnectionEvent` 协议、
   `_on_reconnecting`/`_on_reconnected`/`_record_disconnected`/
   `_record_reconnected`，把 SDK 的 `Events.RECONNECTING`/`RECONNECTED`
   （同步、零参数回调，见下文）接到一对写事件的方法上；`run()` 注册这两个
   handler，并在 `finally` 里 `await` 掉还没写完的后台任务，不让进程退出时
   把还没提交的那一行事件丢在半空。
2. `cli.py` 的 `_scheduler()` 把长连接接进主循环：`_long_connections` 在
   进程启动时读一次 `transport = 'long_connection'` 的活跃绑定，凭据在
   读出绑定的**同一个 session** 里用 `CredentialResolver` 解析；每条连接
   包一层 `_supervised_connection`（指数退避重试，共用同一个 `stop`），
   和 `runtime.run_forever` 一起进 `asyncio.gather`。
3. 断线/重连事件写进已有的 `audit_events` 表——不是新表：
   `/api/v1/audit-events` 已经在读它，`AuditPage.tsx` 第 163 行已经在把
   `context` 列渲染出来。`down_seconds` 放在 `context` 里，`disconnected`
   记 `None`（那次中断还没结束）。
4. 新测试文件，逐字复用计划 Step 1 给的三个测试骨架（加了类型标注满足
   pyright，没有改测试逻辑）。

## 2. 测试过了

```
unit          2288 passed
integration    863 passed, 2 failed（见 §3，与本分支无关）
ruff          All checks passed!
pyright       0 errors, 0 warnings, 0 informations
web            236 passed
chat-web        53 passed
```

`channels` 目录单独跑过多轮（Step 4、Step 5 前后各一次）：89 passed，
每次都是干净的。

## 3. 那 2 条失败是环境，不是这条分支

`test_a_check_on_a_deployment_with_no_boundary_says_so` 和
`test_a_check_reaches_nothing_when_there_is_no_boundary` 断言
`refusal == "egress_not_configured"`，实际拿到 `egress_unavailable"`。

原因和 `2026-08-26-feishu-images.md` §2 记录的完全一样：仓库根目录的
`.env` 里有 `EGRESS_PROXY_URL=http://egress-proxy:3128`（这次核对过：
`.env` 第 50 行）。这台机器上「没有出口边界」这个前提不成立——边界配置
好了，只是连不上 `egress-proxy` 这个 Compose 服务名。这是本地部署环境
的已知脆弱点，不是这次改动引入的，也不在这次改动触碰的任何文件附近。

## 4. Step 5 破坏性验证：夹具本身有两个洞，都补上了

计划要求把 `_long_connections` 里「按 transport 过滤」那一行临时删掉，
预期 `test_only_long_connection_bindings_get_a_connection` 变红。第一次
去掉过滤后，这条测试**照样绿**——说明夹具没有验证到它自称验证的东西。

排查发现两处：

1. `seeded_bindings_of_both_transports` 原来给 webhook 绑定的
   `app_id`/`app_secret_ref` 都是 `None`。去掉 transport 过滤后，那条
   webhook 绑定确实进了候选集合，但被**另一条**「凭据解析失败就跳过」
   的逻辑挡住了——测试绿是因为凭据检查而不是 transport 过滤在起作用，
   两个过滤条件在这个夹具下无法区分。修法：给 webhook 绑定也配一份真实、
   可解析的密钥（`app_id="cli_webhook"`，一个真正 seal 过写进 `secrets`
   表的值）。
2. 两个绑定共用同一个密钥 `name` 时撞上 `secrets` 表的
   `uq_secrets_workspace_name`（同一 workspace 下密钥名必须唯一）。修法：
   `name` 按每次调用生成的 `secret_id` 拼后缀，保证工作区内唯一。

修完夹具后重新执行破坏性验证：去掉 transport 过滤，
`test_only_long_connection_bindings_get_a_connection` **真的红了**
（`AssertionError`，候选集合里多出了那条 webhook 绑定的 id）；恢复过滤
后重新绿。`channels` 全套 89 passed，确认这次修复没有连带弄坏别的测试。

## 5. 一个关键的、这次任务范围之外的缺口

**长连接收到的帧，认领之后不会变成 Run。**

`deliver` 接的是 `FeishuWebhookService(store).accept_verified`——这是
Task 3 自己钉死的设计（`accept_verified` 的去重测试
`be4da75`），它的职责只是「验证 + 认领进 `channel_events`」，两种
transport 共用这一半。真正把一条 `channel_event` 变成一次 Run 的是
`FeishuChannelService.deliver()`，而这个方法**只能通过 webhook 的 HTTP
路由触达**——scheduler 进程里没有任何代码轮询 `channel_events` 表找
「已认领但还没转化成 Run」的行。

也就是说：把一个绑定切到 `long_connection`、关掉 cloudflared、发一条
真实消息——按照计划 收尾 一节的说法，这是「这条分支唯一真正的验收」——
此刻会看到消息被 `FeishuChannel` 收到、被 `accept_verified` 认领进
`channel_events`，然后**再也没有下文**。没有 Run，没有回复，用户会觉得
机器人没反应。

这不是这次任务实现错了——Task 4 的 Interfaces 一栏写的是「Consumes:
Task 3 的 `FeishuLongConnection`」「Produces: 无新公开接口」，接线到
Run 创建从未在这次任务的范围里。但它是这条**分支**能不能达成自己所写的
验收标准的前提缺口，必须在这里点出来，而不是留到真机测试才发现。

## 6. 这一遍没能证明什么

- **§19.2 要求的断线补投语义完全没有验证。** 这次任务只证明了「断开、
  重连这两个事件会被写下来、带着非负的 `down_seconds`」——`down_seconds`
  本身存在的理由就是给将来那次验证当输入，但这次没有拔过网线、没有在
  断线期间让对方发过消息、也没有检查过重连后那条消息是否补投。
  §1473/§1515 要求的技术验证记录仍然是空的。
- **没有做过真机部署验收。** 计划的收尾一节写明「唯一真正的验收」是
  `deploy/compose/redeploy.sh` 部署、关掉 cloudflared、真实飞书应用发一条
  消息确认能收到。这次任务在自动化 agent 环境里完成，没有真实的飞书
  应用凭据、没有可关闭的 cloudflared 隧道、也没有触发过 `redeploy.sh`。
  §5 记录的缺口意味着即便走了这一步，大概率也看不到回复。
- **`RECONNECTING`/`RECONNECTED` 回调从未在真实 SDK 事件循环里触发过。**
  单元/集成测试都是直接调用 `_record_disconnected`/`_record_reconnected`
  （`_AdapterThatDrops` 绕开了真实 socket），不是通过一次真实的网络中断
  触发 `FeishuChannel` 自己发出这两个事件。Task 3 的报告已经读过 SDK
  源码确认了回调签名（同步、零参数），但这次任务没有用真实 socket 验证
  过它们确实会被调用到。
- **多副本 scheduler 下的重复建连没有处理，也没有测试覆盖。** 计划本身
  点名了这一条：如果同一个 `long_connection` 绑定被两个 scheduler 副本
  同时读到，会建两条 socket 抢同一个 app 的长连接，行为未知——这次没有
  跑过多副本场景，代码里也没有任何互斥逻辑。
- **`_supervised_connection` 的退避重试没有用真实的间歇性网络故障测过。**
  测试只验证了「一开始就连不上的绑定不会拖垮主循环」（一次性失败），没有
  验证过反复失败时退避是否真的在每次重试间隔变长，也没有验证过 `stop`
  在退避等待期间被设置时是否真的提前退出——这段逻辑目前只被读代码复核过，
  没有专门的测试。
- **没有开 PR，没有拿到 compose-e2e 的绿色结果。** 计划要求的最后一步
  （开 PR、用 `gh run view <id> --log | grep "^compose-e2e"` 确认真的
  跑过测试）不在这次任务的执行范围内。

## 7. 不声称什么

- **不声称这条分支达成了它自己写的验收标准。** §5 的缺口意味着「没有
  公网入口也能收到消息」这句话此刻是假的——消息能被长连接收到、能被
  认领，但不会变成回复。这不是「基本能用，细节没打磨」，是端到端路径
  在最后一步断掉。
- **不声称 `alive` 反映真实连接状态。** 这次任务没有碰 `alive` 或它的
  注释——它现在唯一的保证仍然是「一次投递失败不会把它清空」，不是连接
  健康监控，Task 3 报告里已经写清楚，这次没有让它多做任何事，也没有
  让任何调度逻辑依赖它。
- **不声称断线记录本身是可运营的观测。** `audit_events` 里的一行
  `channel.long_connection.disconnected/reconnected` 是原始数据，没有
  告警、没有面板、没有针对「断线超过 N 分钟」的任何通知——有人要看，
  仍然得去 `/api/v1/audit-events` 或 `AuditPage.tsx` 里手动找。
- **不声称本机全绿等于可以合并。** 判据是 CI 的 compose-e2e 结果，这次
  没有取得。
- **不声称这次找到的两处夹具缺陷（webhook 绑定无凭据、密钥名冲突）是
  这条分支引入的产品缺陷。** 它们只存在于这次新写的测试夹具里，不影响
  任何已发布的行为。
