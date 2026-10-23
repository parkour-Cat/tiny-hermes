# 工作空间的长任务上限与并发上限

日期 2026-10-23。产品设计 v2.21 §1.21 第 1 条的实现设计，并给第 3 条的并发上限补上设置界面
（[领取顺序与并发上限](2026-10-23-claim-limits-design.md) §5 第 3 条）。

## 现在

- Agent 的 `limits.max_execution_seconds` 写死最多 900 秒：这个上限写在 Agent 规格的模型字段
  里（`le=900`）。
- `limits.max_model_calls` 不写死在规格里，在保存草稿和发布时对照 `PlatformCeilings`
  （`AGENT_MAX_MODEL_CALLS`，默认 20）检查。这个做法的好处写在代码注释里：管理员调低上限，
  不会让已经发布的版本读不出来。
- 并发上限（v2.21 第 3 条）只能用 SQL 设。

## 做法

- 执行时间改成和模型调用次数一样：去掉规格里的 `le=900`，改在写入时检查。去掉字段约束不改变
  任何已发布版本的内容哈希。
- 每个工作空间可以设自己的上限：

  | 工作空间设置 | 不设时 | 最多 |
  |---|---|---|
  | `agent_max_execution_seconds` | `AGENT_MAX_EXECUTION_SECONDS`（900） | `AGENT_CEILING_EXECUTION_SECONDS`（7200） |
  | `agent_max_model_calls` | `AGENT_MAX_MODEL_CALLS`（20） | `AGENT_CEILING_MODEL_CALLS`（200） |
  | `max_running_runs_per_caller` | `RUN_LIMIT_PER_CALLER`（3） | 1000 |
  | `max_running_runs` | 不限 | 10000 |

  不设时和现在一模一样，管理员调高之后才变。
- 接口 `GET/PUT /api/v1/workspaces/{id}/limits`。成员都能读：Agent 作者要知道自己能写多少；
  只有工作空间管理员和平台管理员能改。返回当前值、不设时的默认值和最多能设到多少；改动写审计
  `workspace.limits_changed`。传 `null` 表示恢复默认。
- 控制台：「设置」里加一段「运行上限」。Agent 编辑页里执行时间和模型调用次数的输入框，最大值
  改成按这个工作空间的上限，不再写死 900。

## 不做

- 不改已经发布的版本：调低上限之后，已发布的版本照常运行，下次保存或发布时才按新上限检查
  （和 `max_model_calls` 现在的做法一致）。
- 工具调用次数上限这一遍不做成工作空间设置。
