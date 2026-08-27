# 用户自己走得出来 — 2026-08-26

> 产品：v2.6 §19.4（聊天内命令）、§14.3（会话搜索）、§916、§935。
> 起因：一条飞书会话被它自己的历史锁死。模型在图片管道故障期间说过五次
> 「我看不到图」，管道修好后它把那些当成已确认的事实继续拒绝。当时唯一的
> 出路是我直接删数据库里的 `channel_conversations` 映射行——**产品没有给
> 用户任何脱身的办法**，而 §916 和 §935 早就要求「新建会话」入口，一处都
> 没有实现。

## 1. 测试过了

```
unit          2233 passed
integration    898 passed, 2 failed（环境，见 §2）
ruff          All checks passed!
pyright       0 errors, 0 warnings
web            222 passed
chat-web        53 passed
compose-e2e     18 passed (2.8m)
```

## 2. 那 2 条失败仍然是环境，不是这条分支

`test_a_check_on_a_deployment_with_no_boundary_says_so` 与
`test_a_check_reaches_nothing_when_there_is_no_boundary`：`shared/config.py`
的 `SettingsConfigDict(env_file=".env")` 会读到仓库根目录那个为本机部署准备的
`.env`，其中有 `EGRESS_PROXY_URL`，于是「没有出口边界」这个前提在本机不成立。
`EGRESS_PROXY_URL=""` 时该文件全过。CI 上没有 `.env`。上一条分支已记录并单独立项。

## 3. 这条路走得通

和 §1 是两句话。**但这一次它只走通了一半，必须说清楚。**

`<FILL：真实飞书里发 /undo 与 /new 的结果>`

## 4. 六个读点，逐个判定

撤回最容易出的错是「撤了但某条路还看得见」。设计 §5.1 列了每一处读
`session_messages` 的地方并逐个判定：

| 位置 | 判定 |
|---|---|
| `execution_context` | 过滤 |
| `_child_result` | 过滤——撤了就不该被当作结果引用 |
| `list_session_messages` | **不过滤**，但带出 `withdrawn_at` |
| `_copy_checkpoint_messages` | 过滤——撤回的不该进新检查点 |
| `sql_search._base` | 过滤——搜得回来就等于没撤 |
| `pending_replies` | 过滤——见下 |

**第六处是实施中发现的，原表漏了它。** `pending_replies` 用一个 lateral 取 Run
的最后一条 assistant 消息直接发给飞书。Run 终态之后、回复派发之前有一个窗口，
用户在这个窗口里 `/undo`，不过滤就会把刚撤回的答复发出去。过滤它的理由不是
防泄漏（那条回复本来就发给同一个人），而是不过滤会让**渠道与模型上下文讲两个
不同的故事**——用户收到一条答复，模型的历史里却没有它。

破坏性验证：把 `withdrawn_at IS NULL` 从 `execution_context` 拿掉，
`test_a_withdrawn_message_is_not_in_the_next_request` 变红，撤回的消息带着
UUID 重新出现在 history 里。装回去，绿。

## 5. 招牌 bug 在专门为它写的分支上又出现了一次

`withdrawn_at` 写进了库、带到了 store 的 DTO，然后**两个 HTTP 响应模型都把它
丢了**。`grep -rn withdrawn apps/` 零结果。而 `sql_store.py` 那句注释写着
「so a caller can render 'withdrawn'」——**声称了一个不存在的可达性**。

所有后端测试都是绿的，因为断言只到 store 为止。

这是 CLAUDE.md 点名的那个 bug 的第七次，出现在专门为它的镜像而写的分支上。
它是最终整分支评审抓到的，不是任务评审抓到的——七次任务评审全部放行。

修了三层：两个响应模型声明字段、控制台与 chat-web 都渲染标记、导出的 Markdown
也标、注释改成描述整条链。新测试断在**此前缺失的那一层**（HTTP 响应），不是 store。

## 6. `/new` 曾经在它唯一有用的场景里失效

阻塞卡片写着「被卡住时，可以发 `/new`」。而忙判定把一切非终态都算忙——包括
`waiting_approval`、`paused`、`waiting_external`，**正是阻塞卡片被渲染的那些状态**。
卡在等审批的人读到卡片、发 `/new`、被拒绝，而这一版没有按钮，取消只能去控制台。
起因那次事故原样复现。

现在按队首 Run 自己的状态分类：真在跑或不可取消则拒绝；停靠则允许，并结束该
Session 全部未了结的 Run。停靠可以安全取消，是因为没有任何工具在执行。

**取消是全有或全无，但没有回滚**：挡住「取消一半」的是动手前那道前置检查，
不是异常处理。代码注释和 §19.4 都这么写，没有声称原子性。

## 7. 这一遍没能证明什么

- **`compose-e2e` 绿了，但它一条命令都没测。** run `33029676349`（PR 触发那次），
  `18 passed (2.8m)`，已用 `gh run view --log | grep "^compose-e2e"` 确认是真的跑了
  测试而不是只返回 success。但 `tests/e2e/` 里没有任何一条涉及 `/undo`、`/new` 或
  撤回标记。**它证明的是这条分支没有弄坏别的东西，不是命令能用。**
- **`<FILL：真实飞书里到底验了什么、没验什么>`**
- **`/new` 结束排队 Run 的行为只在测试里见过。** 真实场景里用户在被卡住时
  连发几条消息再 `/new`，那些消息的 Run 会被取消而用户只看到一句回执，
  这个体验没有对着真人验过。
- **没有并发验证。** 前置检查与取消之间靠 Session 行锁串行，评审对着源码核过
  锁确实被同一路径持有，但**没有任何测试真的并发跑过** `/new` 与提交、审批。
- **新增了一个锁序倒置。** `unfinished_work` 先锁 Session 再锁 Run，而
  `apply_signal`、`claim_head` 是 Run→Session。同一 Run 上并发的 `/new` 与
  「批准」可能死锁，Postgres 中止其一 → webhook 500 → 飞书重试六小时。
  `_terminalize` 早有同样的边，模式非本次引入，**但本次扩大了它的暴露面**。
  没有测试，没有观察过。
- **`cancel_failed` 无法报告已经提交的取消。** 只在 `StateVersionConflict` 下
  可达，而 Session 行锁下几乎不可能——「几乎」不是「不可能」，没有测过。
- **回执的送达没有对着真实飞书验过**，只用 spy sender 验到了 wire 层。
- **`/undo` 的措辞没有对着真人试过。** 回显 200 字符截断是抄上游的数字，
  没有任何依据说它对中文合适。

## 8. 不声称什么

- **不声称历史污染被解决了。** 这条分支给了用户一把手动的铲子。模型仍然会
  被自己说过的话说服，没有任何机制识别「这句话是基础设施故障期间产生的」。
- **不声称 `/new` 等于新建会话。** 它是在同一 Session 内划线。预算、排队、
  记忆快照都挂在 Session 上，**`/new` 不重置它们**。§19.4 写明了这一点，
  但用户界面上没有任何地方告诉用户这件事。
- **不声称撤回可以恢复。** 行还在、`withdrawn_at` 可以被清空，但没有任何入口。
- **不声称 §935 的入口是完整的。** 它是卡片文案里的一句话，不是可点的按钮。
  做按钮要 card-action 回调、验签和幂等，本版没做。
- **不声称 SQL 谓词 `command_receipt IS NOT NULL` 被测试单独钉住。** Python 侧
  的解析守卫也会滤掉空回执，两条都去掉才红。双层是 `pending_blocked_notices`
  已有的模式，但 SQL 那一层至今没有测试单独覆盖。
- **不声称本机跑绿等于可合并。** 判据是 CI 的 compose-e2e。
