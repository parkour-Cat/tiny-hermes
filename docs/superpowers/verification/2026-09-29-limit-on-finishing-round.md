# 在最后一次允许的调用里完成的 Run，不再被记成 paused(limit)

分支 `fix/limit-on-finishing-round`，建在 `fix/lease-lock-order` 之上。未推送、未合并。
规格改动：v2.13 §12.3（`866e398`）。

## 现象

2026-09-29 做长任务探针时，19 轮的长任务全部停在 `paused(limit)`。它们的事件历史里，
第 20 次、也就是最后一次允许的调用，已经是 `goal_verdict: done`。

暂停的 Run 仍然占着 Session 队首，所以这个人在同一个 Session 里的下一条消息，会一直
排在一个已经完成的任务后面，直到有人为它放宽一个根本用不着的预算。

## 原因

- `decide_after_round` 在看 Goal 判定之前，先检查 `budget_allows`。
- `budget_allows` 来自 `_budget_after`，它的 docstring 写的是「这一轮之后，预算还允许
  再跑一轮吗」。判为 `done` 的一轮没有下一轮，却被这个「下一轮」的问题改写了结局。
- 同一个函数里的 `rounds_exhausted`（最大连续轮数）早就排在 done 和 failed 之后，注释
  写的是「上限挡的是下一轮，不是已经解决了目标的这一轮」。两个阀门的规则不一致。
- 规格 §12.3 只规定「达到限制时不得开始新的调用」，没有说已经完成的这一轮怎么办；
  这一遍补上了。

## 修复

- 新增 `_ends_the_run(outcome)`：判定是 `done` 或 `failed`，且没有待审批、没有委派。
  满足时，预算不再改写结局。
- `continue`、`wait`、待审批、以及人主动取消或暂停，都保持原样。
- `decide_after_round` 的 docstring 原来说安全阀优先于已达成的目标；改成写明它优先于
  「还需要下一轮的轮」，不优先于「已经结束这个 Run 的轮」。

## 两处原来钉住旧行为的测试，这次是有意改的

1. **`test_a_met_goal_does_not_outrank_the_three_things_above_it`** 把预算和取消、暂停
   并列为「优先于 done」的三件事。它来自 `56e859d`，那是一次提交说明写着「没有任何
   可观察变化」的重构：它钉住的是当时的顺序，并不是针对这种情况做的决定。现在它只保留
   取消和暂停，改名为 `…_does_not_outrank_a_person_stopping_the_run`，docstring 写了
   预算为什么被拿掉。
2. **`test_budget_expansion.py` 的 `stopped_at_the_limit`**。它把
   `consumed_model_calls` 调到「上限减一」来制造停在阀门上的 Run。但模型收到的轮次号
   `round_index` 就是从这个计数算出来的，于是 `continue_once` 以为自己在第 20 轮，直接
   答了 `done`。**这 7 条测试以为自己在测「还要下一轮，但预算用完了」，实际测的是
   「已经完成，但预算用完了」，而它们能通过，正是依赖这个 bug。** 现在改为把上限降到 1，
   于是 `continue_once` 在第 1 轮就答 `continue`，暂停是真的因为还需要下一轮。

另外，`test_a_summary_call_counts_against_the_max_model_calls_ceiling` 的核心主张没变：
摘要器没有被调用，总共只用了一次调用。只是这一轮用唯一的一次调用答完了，所以结局从
`paused` 改为 `completed`。

## 测试过了

新测试先写，并看它红（`641bcbf`）：

- 单元：`done` 和 `failed` 在预算用尽时按判定结束，这 2 条是红的；`continue`、`wait`、
  待审批、取消照旧，这 4 条从一开始就是绿的，钉住这次改动不外溢。
- 集成：`test_a_run_finished_on_its_last_allowed_call_completes_and_frees_the_session`。
  一个 Agent 只允许 2 次调用，跑 `continue_once`（正好需要 2 次）。修复前 Run 停在
  `paused`，同一个 Session 的下一条消息领不到。

| 套件 | 结果 |
|---|---|
| 后端单元（全部） | 2469 passed |
| 后端集成（不含 sandbox，全部），在 `c5a70ee` 上 | 1012 passed，1 failed（见下） |
| 后端集成 `runs`（全部），修正第三个夹具后，在 `1e8b4c7` 上 | 527 passed |
| `ruff` / `pyright`（改动文件） | 通过 / 0 errors |

第一遍跑 `runs` 集成测试时，我把失败列表截在了 8 行，漏看了第 9 条：
`test_worker_execution.py::test_the_safety_valve_stops_a_run_before_another_model_call`。
它用了同一种「把已用计数调到上限减一」的夹具，是在全量集成测试里才发现的，修法相同
（`1e8b4c7`）。修完之后 `runs` 目录 527 条全部通过；全量里 `runs` 之外的部分，在
`c5a70ee` 上就已全部通过。

## 走通了什么

本机栈重建到 `1e8b4c7`（第一次构建时，`uv sync` 从 PyPI 取构建依赖超时，重试一次就成功了），
并在容器里核对过 `_ends_the_run` 已经在里面。`WORKER_CONCURRENCY=4`，模型延迟 300 ms。

| 步骤 | 结果 |
|---|---|
| Agent 的模型调用上限是 20（演练控制台的默认值），跑 19 轮的 `long_task`，正好需要 20 次调用 | `completed`，调用 **20/20** |
| 它结束之后，在同一个 Session 里再发一条 | 被领取，`completed` |

修复前的对照：同一天的公平实验里，16 个 19 轮长任务都停在 `paused(limit)`，已用调用都是
20。

第一次现场尝试不算数：那次在第一个 Run 还在跑的时候就发了第二条消息，触发了 §12.1 的
「中途来消息就让位」，第一个 Run 只跑了 2 轮就结束了。重做时改成等第一个结束之后再发。

## 这一遍没能证明什么

- 没有用真实模型：`done` 都来自确定性模型。
- 费用阀门：§12.4 允许 Token 和费用被最后一次调用越过，这一轮做完就完成，越过的用量
  照常记账。**没有专门测「最后一轮越过了费用上限，同时判了 done」**，只测了调用次数这个
  阀门。
- **没有取得 compose-e2e 结果。**

## 不声称什么

- 不声称以前的 `paused(limit)` 记录现在会自己变成 `completed`。已经暂停的 Run 还是暂停的，
  这一遍没有回填数据。
