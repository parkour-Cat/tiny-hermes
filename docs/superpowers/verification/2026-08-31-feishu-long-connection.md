# 飞书 WebSocket 长连接接入 — 验收记录

> 产品：spec §19.2（飞书私有部署默认用长连接，不要求公网入站地址）。
> 分支：`feat/feishu-long-connection`，38 个提交，6 个任务 + 4 轮修复。
> 促因：内网穿透隧道四天过期了四次，每次都要手工改飞书后台的 webhook 地址。

**这份记录在 Task 5 落地后重写过一次。**上一版停在 Task 4，断言「消息能被认领但
不会变成回复」——那句话现在是假的，留着会把读它的人引向一个已经不成立的结论。

## 1. 做了什么

1. **适配器**（`channels/infrastructure/feishu_long_connection.py`）：把 lark SDK 的
   WebSocket 客户端包成 `FeishuLongConnection`。SDK 的回调**不在 scheduler 的事件
   循环上**（`lark_oapi/ws/client.py:31-34` 在 import 时就建了一个模块级 loop），
   所以每个回调都经 `asyncio.run_coroutine_threadsafe` 送回 `run()` 捕获的那个 loop，
   并等它完成——不等的话就既没有去重也没有背压。
2. **transport 列**（migration 0052）：`channel_bindings.transport`，`webhook` 或
   `long_connection`，NOT NULL，默认 `webhook`，合法值由 CHECK 约束决定。
3. **scheduler 侧宿主**（`api/cli.py`）：`_long_connections` 在进程启动时读一次活跃的
   长连接绑定，凭据在**读出绑定的同一个 session** 里解析；每条连接包一层
   `_supervised_connection`（指数退避 5s→300s，共用同一个 `stop`），与
   `runtime.run_forever` 一起进 `asyncio.gather`。
4. **开关接到 API 和控制台**：PATCH 接受 `transport`，响应体带上它，控制台能看见能切换，
   并且**没有 app 凭据的绑定切不过去**（服务端按改完之后的状态校验，前端禁用该选项）。
5. **认领之后的那半程**（Task 5）：抽出 `FeishuChannelService._after_claim`，两种
   transport 共用；长连接经 `deliver_verified` 进来。在此之前长连接走到认领就停——
   消息落进 `channel_events`，没有 Run，没有回复。
6. **连接生命周期留痕**：四种 `channel.long_connection.*` 审计行写进已有的
   `audit_events` 表（`/api/v1/audit-events` 已经在读，`AuditPage.tsx` 已经在渲染）。

## 2. 测试过了

```
pytest packages/backend/tests/integration（含 sandbox）   975 passed, 2 failed
pytest packages/backend/tests/unit                        （含在上面的 channels+unit 2401 里）
pnpm --filter @tiny-hermes/web test                       241 passed
pnpm chat:test                                            53 passed
ruff check packages/backend migrations                    All checks passed
pyright                                                   0 errors
```

那 2 条失败是 `model_catalog/test_endpoint_api.py` 的「no boundary」两条。**核实过不是
托辞**：清掉环境里的 `EGRESS_PROXY_URL`（仓库根 `.env` 设了它）再跑，`2 passed in 1.02s`。
与本分支无关，本分支一个字没碰 model_catalog 或 egress。

## 3. 测试自己能不能红——验过

这条分支的判据是「这条路走得通」，不是「测试过了」，所以关键断言都做过变异注入：

| 变异 | 结果 |
| --- | --- |
| `deliver_verified` 认领后直接返回（= Task 5 之前的行为） | 两条端到端测试 FAILED |
| 删掉 `attach_run`（那次真实事故的形状：Run 建了但没挂上） | 两条端到端测试 FAILED |
| `_on_scheduler_loop` 换成裸 `await`（跨 loop 缺陷） | 对应测试 FAILED |
| 删掉 `finally` 里的 drain | 对应测试 FAILED |
| 「一段故障只写一行」改成每次都写 | 对应测试 FAILED（3 行 vs 1 行）|
| 校验读「这次传了什么」而不是「改完之后的状态」 | 对应测试 FAILED（200 vs 400）|

另有一次变异**抓到了实现自己的洞**：drain 超时那条路里，取消会触发 `_forget`，于是
「超时后再数还剩几条」数出来是 0，那行日志压根不发。

## 4. 这一遍没能证明什么

- **没有在真实飞书 socket 上跑过。**测试里的 `_FakeChannel` 复制的是 lark SDK 源码里
  回调的调用方式（哪个线程、哪个 loop、同步还是 await），**不是真实网络**。
  `RECONNECTING`/`RECONNECTED` 在真实 SDK 上到底何时触发、以什么频率成簇出现，未验证。
- **分支唯一真正的验收没做**：切到长连接、把隧道关掉、发一条消息、看它被回答。
  在真机上跑通那一遍之前，这条分支只是「读起来自洽」。
- **§19.2 要求的断线补投语义未验证。**`down_seconds` 现在写得进也读得出，但它存在的
  理由——那次验证本身——没做。这在设计文档 §9 里本来就是明确不做项。
- **Run 是被测试直接 `UPDATE` 成 completed 的**，没有真跑模型。出站那一半证明了，
  Worker 那一半没有（它有自己的套件）。
- **compose-e2e 没跑**，PR 没开。
- 没有证明这些审计行**在浏览器里真的看得见**（没跑 UI）。

## 5. 不声称什么

- **不声称「关掉 egress 代理，这个进程就发不出任何东西」对入站仍然成立。**
  长连接是 scheduler **主动打出去**的一条 WSS，不经过 egress 代理；而且 lark SDK 在
  `ws/client.py:73-80` 主动把 `proxy` 钉成 `None`，所以连环境变量式代理也拦不住它。
  后果具体是：代理关掉之后，消息照样进来、照样触发 Run，只有回复发不出去。
  参照实现 `NousResearch/hermes-agent` @ `3f83297` 把同一件事写在
  `docs/security/network-egress-isolation.md` 的 **Limitations** 一节里
  （「Platform adapters need egress」），它的 gateway 同样是双宿主直连消息平台——
  结构上是同一种设计，区别只在于**它没有声称过「唯一的出口」**。
- **不声称一个进程能带多于一个长连接绑定。**SDK 的 ws loop 是模块级单例，第二个绑定
  的 `start()` 跑不起来，而且 A 的 `finally` 会停掉 B 的 socket 所在的 loop。
  现在多出来的绑定会在启动时被拒绝并写一行 `not_started` 说明原因，**但这是拒绝，
  不是支持。**
- **不声称建连成功之后掉线会被自动恢复。**没有活性检查：socket 死在
  `stop.wait()` 上时，这个进程不会知道。SDK 自己的无限重连是唯一的恢复机制。
- **不声称控制台上「长连接」这一列说的是实话。**它渲染的是**存下来的值**，不是连接
  状态。一个切了 transport 但没重启 scheduler 的绑定、一个凭据失效被跳过的绑定，
  列上都写着「长连接」。原因现在能在审计页上查到，但渠道列表本身不会说。
- **不声称所有角色都读得到这些审计行。**核实过：WORKSPACE_ADMIN 和平台管理员是
  `FULL`，`context` 原样渲染；VIEWER 是 `REDACTED`，行看得见但 `context` 变成 `{}`，
  页面显示「—」，读不到 `down_seconds` 和 `reason`；DEVELOPER 是 `OWN_RESOURCES`，
  而这些行的 `actor_id` 是 `None`，**基本看不到**。做 §19.2 验证的人要用管理员登录。
- **不声称库里已经存在的死配置行会被修好。**这一轮加的是校验，让这种行**建不出来**；
  没有迁移，没有回填。
- **不声称多副本 scheduler 下只会有一条连接。**没处理，没测试。
- **不声称两条 transport 共用一个去重键空间。**设计文档 §3 原来这么要求，真机走查
  证明做不到：SDK 不把飞书的 `event_id` 交给 handler。webhook 按 `event_id` 认领，
  长连接按 `message_id`。飞书的投递方式是单选的，所以两路同时到只可能在切换瞬间。
  §3 已按此修订并写明理由。

## 6. 真机走查：做了，而且它抓到了测试全绿的一个缺陷

2026-09-02，在单机 compose 栈上走了一遍：控制台切 `long_connection` → 重启 scheduler
→ **关掉 cloudflared 隧道** → 在飞书里发消息。

这一遍抓到三件事，**没有一件是测试能抓到的**：

1. **飞书开发者后台的「订阅方式」是另一个开关**，要从「发送至开发者服务器」改成
   「使用长连接接收事件」，**而且改完要发布版本**。spec、实施计划、验收记录三份文档
   一个字都没提过它。用户在我们自己的控制台里做完了全部步骤、看到了「已生效」的提示，
   消息仍然不来。
2. **`_envelope_of` 的 docstring 是假的**，而且是它把生产打挂的：`InboundMessage.raw`
   是消息对象不是事件信封，第一条真实消息的结果是
   `MalformedChannelEvent: no event id in either schema version`。
   **测试之所以全绿，是因为每一条长连接测试递的都是手搭的 webhook 信封**——
   验证的是「我们以为 SDK 会给什么」，不是「SDK 实际给什么」。现在所有长连接测试
   都改用 SDK 自己的 `InboundMessage` 构造。
3. 关闭过程中出现 lark SDK 自己的 `Task was destroyed but it is pending!`
   （`_ping_loop` / `_receive_message_loop`）。**我们的 drain 管不到 SDK 的任务。**
   目前只在关闭时出现，那两个任务不写库，没有观察到数据丢失。
