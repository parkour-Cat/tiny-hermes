# 聊天内命令：/undo 与 /new — 设计

> 日期：2026-08-26
> 状态：待实施
> 产品：v2.5 §916（Web Chat 的「新建会话」入口）、§935（飞书阻塞卡片的同一个入口）。
> 起因：一条飞书会话被它自己的历史锁死——模型在管道真坏时说过 5 次「我看不到图」，
> 管道修好后它把那些当成已确认的事实继续拒绝。当时唯一的出路是我直接删数据库里的
> `channel_conversations` 映射行。**产品没有给用户任何脱身的办法。**

## 1. 问题

渠道层**一条聊天命令都不解析**。`channels/application`、`channels/domain`、
`channels/infrastructure` 里没有任何 slash 处理，每条进来的消息直接变成一个 Run。

后果有两个，都不是假想的：

- 用户无法开始一段新对话。spec §916 和 §935 都要求「新建会话」入口，**一处都没有实现**。
- 历史里一旦有错话（尤其是基础设施故障期间模型说的），没有任何办法取出来。

## 2. 参考实现：Hermes Agent

核对自 `NousResearch/hermes-agent` @ `3f832978`，即 `docs/research/` 钉的那个 commit。
上游事实与本项目的决定分开记，下面标注了哪些抄、哪些不抄。

**存储：两个标志位，三种状态。** `messages` 表上 `active` 与 `compacted`：

| 状态 | 模型上下文 | `session_search` |
|---|---|---|
| `active=1` | 在 | 在 |
| `active=0, compacted=1`（压缩归档） | 不在 | **在** |
| `active=0, compacted=0`（用户撤回） | 不在 | **不在** |

上游注释把区别写死：压缩是 "summarized away"，撤回是 **"user took it back"**。
FTS 索引里这些行**始终存在**——触发器只在 INSERT/DELETE 上动，不看这两个标志，
翻成 `active=0` 只是一次保内容的 UPDATE。所以「搜不到」是查询层过滤，不是删索引。

**`/undo`**（`rewind_to_message`）：软删除 `id >= target` 的全部消息；target 必须是
`user` 消息否则 `ValueError`；target 自己也置 inactive，因为调用方要把它回填成下一条
待发提示；`rewind_count` 每次都自增（计数是操作次数，不是行变化）；对标志位幂等；
`[N]` 默认 1，超界夹到最老的 user 消息；回执回显被撤原文（截断 200 字符）。

**`/new`**（`reset_session`）：轮转 session id，且**不拒绝在飞的工作，而是拆**——bump
run generation、清空 running-agent 槽位、把工具资源拆卸丢到 worker 线程并设 30 秒上限、
按 `parent_session_id` 中断在飞的子 agent 委派、统一漏斗清空所有会话作用域状态。
这段代码上挂着三个 issue（`#35994` 事件循环被拆卸卡死、`#28686` 僵尸槽位静默丢消息、
`#55578` 悬空子 agent 烧 token），**全部来自「重置时在飞的工作」**。

## 3. 本项目的决定

### 3.1 抄上游的

- **软删除，不硬删。** 与 `context_budget.py` 写下的不变量一致：
  「No branch makes a message unreachable」。
- **撤回的消息 `session.search` 也搜不到。** 压缩摘要会主动告诉模型
  「searchable with `session.search`」并附线索词；被撤的内容若还能搜回来，撤回就是漏的
  ——而这正是本功能要解决的中毒问题。
- **`/undo [N]`**，默认 1，超界夹紧不报错。
- **`/undo` 的 target 必须是 user 消息。**
- **回执回显被撤的原文**，方便用户改一改重发。

### 3.2 不抄上游的

- **`/new` 不轮转 session id**，而是在同一个 session 里划一条线：把此前所有消息标为
  不再进上下文。理由：轮转会留下够不着的孤儿 session（这正是本次事故里我手工删映射
  造成的后果），而 `channel_conversations` 的
  `uq_channel_conversations_participant` 唯一约束也要改成部分唯一索引才能容纳历史行。
  同一个持久 id 也正是上游压缩 `in_place: true` 的哲学。
  **代价必须写明：预算、排队、记忆快照都挂在 session 上，`/new` 并不真的重置它们。**
- **真的在飞的 Run 一律拒绝，不拆。** 上游那条路的成本明明白白挂着三个 issue，而
  tiny-hermes 的沙箱所有权和 Session FIFO 语义比它更强，拆得更贵、更容易留下
  历史与世界不一致的状态（工具副作用已经发生，把那一轮从上下文抹掉不会把副作用抹掉）。
- **例外只有一个：队首停住时（`waiting_approval`、`waiting_external`、`paused`），
  `/new` 可以结束这个 Session 的全部未了结工作。** 上游那三个 issue 拆的都是**真的
  在跑**的工作；停着的 Run 没有工具在执行，它等的是一个人、一个外部事件，或者前面
  那个 Run，取消它不会把某个副作用截在半路。排队的 Run 连第一轮都还没开始，这条
  理由对它只有更强。走的是现成的 `RunCoordination.cancel_end_user_run`，合法转移表
  仍归 `RunStateMachine`。
  **结束的是全部，不是队首那一个。** 只结束队首会留下一个洞：队首让开之后，排在
  后面的被提上来，拿着**已经被撤掉的历史**跑完一整轮，然后把答复发进用户以为是全新
  的对话里——出站渠道和模型上下文讲两个不同的故事，正是 §5.1 要消除的那个分裂。
  **全有或全无。** 只要有一个非终态的 Run 收不下 `CANCEL_REQUESTED`（`running`、
  `cancelling`），就一个都不动，按忙拒绝。撤一半比拒绝糟：历史撤了，而那些 Run
  还会答复。这道判断是**事前检查**（`unfinished_work` 里，用状态机那张表推出来的
  可取消状态集），不是回滚——这一层没有 savepoint。
  取消失败（非法转移、状态版本冲突）同样**什么都不撤**，按忙拒绝——照样撤等于给用户
  一段新对话加几个还能醒进来的 Run。
  **`/undo` 对停住的队首照样拒绝。** 不对称是故意的：`/new` 是逃生口——下面那条
  「阻塞卡片里写明 `/new`」的决定正是对着这几种状态做的，不给它这个权限那句话就是
  假的；`/undo` 是对已经落定的历史动刀，没有理由替用户放弃一个他没说要放弃的 Run。
- **不做可点的卡片按钮。** 真做要新建 card-action 回调端点、验签、幂等，是与命令本身
  相当的另一块工程。本版在阻塞卡片的「你可以做什么」里写明 `/new`——
  `feishu_card.py` 现在就是「只命名动作、不渲染按钮」的写法，与之一致。

## 4. 架构

**`channels/domain/commands.py`（纯，无 I/O）**
`parse(text) -> ChatCommand | None`。只认**整条消息精确匹配**的 `/undo`、`/new`
（别名 `/reset`），大小写不敏感，容忍前后空白，`/undo` 接可选正整数参数。
**其它以 `/` 开头的一律不拦**，原样交给模型——飞书里粘一段带 `/` 的路径太常见。
带图的消息不是命令，带附加文字的也不是。与 `blocked.py`、`reply.py` 同层，可纯断言。

**`RunCoordination.withdraw_from_session(session_id, scope, turns=1)`**
`scope ∈ {LAST_EXCHANGE, ALL}`；`ALL` 忽略 `turns`。先判忙（见 §6），忙则抛
`SessionBusy` 且一行不动；
否则置 `withdrawn_at`，返回 `{条数, 轮数, 被撤原文}`。放在这一层而不是渠道层，
是因为 §916 要求 Web Chat 也有同一个入口——那一步应该只剩一个路由加一个按钮。
本版**只交付渠道这一个入口**，Web Chat 的 UI 是独立的后续工作。

**`ChannelIngestion.run_for`**
认出命令 → 调上面的操作 → 写回执文档 → **不提交 Run**。
命令不进队列、不吃预算、不写进历史。

**回执**沿用 `BlockedNotice` 的写法：`channel_events` 上存**结构化文档**，飞书层渲染，
不存渲染好的字符串。扫描器与 `pending_refusals` 同形——一条没产生 Run 但欠人一句话的
入站事件，`binding_target` 取凭证，`replied_at` 标记已答。

## 5. 数据模型

一列：

```sql
ALTER TABLE session_messages ADD COLUMN withdrawn_at timestamptz NULL;
```

用时间戳不用布尔：「什么时候撤的」有用，且天然可空。

**与 `redacted` 严格分开，不复用、不合并。** `redacted` 是 §344 擦除（等于不存在，
搜索也没有），本仓库目前从未被写成 `True`，是为擦除预留的休眠列。

| | `redacted` | `withdrawn_at` |
|---|---|---|
| 语义 | 擦除，等于不存在 | 用户收回，仍在库里 |
| `session.search` | 搜不到 | 搜不到 |
| 转写记录 | 不出现 | **出现，标为已撤回** |

两者搜索行为相同而转写行为不同，这正是它们必须是两列而不是一列的原因。

### 5.1 每一处读 `session_messages` 的判定

这个改动最容易出的错是「撤了但某条路还看得见」——是本项目最常见 bug 的镜像。
**所有读点必须逐个判定，不是改两处就算完。**

| 位置 | 用途 | 判定 |
|---|---|---|
| `sql_store.py:824` `execution_context` | 一个 Run 的历史，喂给模型 | **过滤** |
| `sql_store.py:1258` `_child_result` | 子 Agent Run 的结果消息 | **过滤**——撤了就不该被当作结果引用 |
| `sql_store.py:2581` `list_session_messages` | 转写记录 / API 列出 | **不过滤**，但必须把 `withdrawn_at` 带出去，让界面能标「已撤回」 |
| `sql_store.py:2654` `_copy_checkpoint_messages` | 检查点复制 | **过滤**——撤回的不该被复制进新检查点 |
| `sql_search.py:100` `_base` | 会话搜索 | **过滤**（§3.1 的决定） |
| `sql_channel_store.py` `pending_replies` | 出站回复取 Run 的最后一条 assistant 消息 | **过滤**——见下 |

第六处是实施时（Task 3 Step 5）发现的，原表漏了它，补记于此。

`pending_replies` 用一个 lateral 子查询取 Run 的最后一条 assistant 消息，直接发给飞书。
撤回的判定条件是「没有非终态的 Run」，而一条 Run 终态之后、回复被扫描器派发之前
有一个窗口——用户在这个窗口里 `/undo`，不过滤就会**把刚被撤回的那条答复发出去**。

过滤它的理由不是防泄漏（那条回复本来就是发给同一个人的），而是：不过滤会让**出站渠道
和模型上下文讲两个不同的故事**。用户收到一条答复，而模型的历史里没有它；用户接着追问，
模型不知道在说什么。这正是本功能要消除的分裂。

安全性已核对：该 lateral 是 `outerjoin`，取不到时 `said` 为空字符串，而
`PendingReply.said` 的注释写明「Empty is a real answer — a Run can complete having said
nothing」，`reply_for` 有对应分支。所以过滤不会发出空消息，也不会让扫描器空转。

**同一个查询也没有过滤 `redacted`**，那是本设计之前就存在的缺口，不属于本次改动
——`redacted` 目前全仓库从未被写成 `True`，所以是潜伏的。已单独记录。

## 6. 错误处理与边界

- **忙的判定**必须同时覆盖「队首 Run 未结束」与「后面还有排队的 Run」两种。
  `sessions.head_run_id` 是已知的一半；排队那一半的准确判据由实施时对着
  `RunCoordination` 现有的 FIFO 语义确认，**不得凭 `head_run_id` 一个字段就下结论**。
  回执必须说清是哪一种，不能只说「忙」。判定分三档：
  `running`（队首真的在执行）、`queued`（队首已终态，未了结的全排在它后面）、
  `parked`（队首停在 `waiting_approval`/`waiting_external`/`paused` 上）。
  `/undo` 对三档一律拒绝；`/new` 只对前两档拒绝，第三档把这个 Session 的未了结
  工作**全部**结束再撤（§3.2）。
  **`parked` 这一刀只看队首自己的状态，故意不看后面还排着什么**——阻塞卡片正是在
  「队首停着、你的消息排在后面」时渲染的，把排队的算进来会让卡片上那句 `/new`
  在它唯一要兑现的场景里失效。后面排着的不是拒绝的理由，是**一起被结束**的对象。
  例外：后面站着一个取消不掉的（`running`、`cancelling`）时降级成 `running` 拒绝，
  一个 Run 都不动。
  取消失败时回执说的是 `cancel_failed`，不是「已开始新对话」。
- **回执必须报出结束了几个 Run**（`CommandReceipt.runs_ended`）。被结束的 Run 永远
  不会再答复，不说出来用户只会发现自己有条消息石沉大海，而他刚被告知这是一段全新的
  对话，正好没有理由去找。这个字段之前存下的回执没有这个键，读回来当 0。
- **没有会话行**（这个人从没说过话）：回「没有可撤的」，**不为一条命令创建会话**。
- **`/undo` 找不到 user 消息**：回「没有可撤的」。
- **N 超界**：夹到最老的 user 消息，不报错。
- **幂等**：同一 `channel_event_id` 重放靠现有 claim 挡；即便漏过，已撤的行不再改
  `withdrawn_at`。
- **`sequence` 继续往后长**，不重排、不留空洞——所有行都还在。

## 7. 测试

先写测试，跑它，看它红，再实现。测试与实现分开提交。

**domain**：`/undo`、`/UNDO`、前后空白、`/reset` 别名、`/undo 3` 识别；
`/undoing`、`/undo 顺便帮我看看`、`/usr/bin`、带图的 `/undo` **不**识别。

**application**：撤一轮只动该动的；N 超界夹紧；`/new` 标记全部；
`head_run_id` 非空 → `SessionBusy` **且断言库里一行未变**；无会话行不创建会话；
同一事件两次只撤一次。

**integration**——这一档专抓「够得着」：

- 撤回后**下一轮真实构造的模型 payload 里没有那些消息**（查 payload，不查库）
- 撤回后 `session.search` **搜不到**
- 被撤的行**仍在库里**且 `withdrawn_at` 非空
- `list_session_messages` **仍然返回它们**，并带上 `withdrawn_at`
- 阻塞卡片文案里**出现 `/new`**
- **回执真的被发出去了**

**破坏性验证**：把 `withdrawn_at IS NULL` 从 `execution_context` 里拿掉，必须有测试变红。

## 8. 这份设计替产品做的决定

以下三条**不在 v2.5 里**，实施前应把它们写进产品事实来源：

1. **`/undo` 是新的产品面。** §916/§935 只要求「新建会话」入口，没有提撤回。
2. **`/new` 的语义被定义为「在同一 session 内划线」**，而不是字面意义的「新建会话」。
   §916/§935 的措辞是「新建会话入口」，本设计满足其**意图**（用户能开始一段干净的
   对话）而非其**字面**（创建一个新的 Session 实体）。这个偏差必须由产品确认。
3. **撤回的消息从 `session.search` 中排除。** §14.3 定义了会话搜索，没有说过任何
   消息可以对它不可见。

## 9. 明确不做的

- **可点击的卡片按钮**（需要 card-action 回调基础设施）。
- **Web Chat 的 UI**（§916 的另一半）。逻辑放在 `RunCoordination` 就是为了让它以后
  只需要一个路由加一个按钮。
- **`/retry`、`/status`、`/model`、`/stop`** 等上游的其余命令。
- **权限分级。** 上游有 admin/user 命令白名单；本版所有能给这个绑定发消息的人都能用
  这两条命令。
- **撤回的恢复。** 行还在，`withdrawn_at` 可以被清空，但本版不提供任何入口。
