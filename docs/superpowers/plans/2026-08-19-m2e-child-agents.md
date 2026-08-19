# M2E 实施计划：一层并行子 Agent

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**产品：** `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` §12.4、§13、§14.1、§27.2.3。
**路线图：** `docs/superpowers/plans/2026-08-17-tiny-hermes-m2-roadmap.md` §8。
**上一步：** `docs/superpowers/plans/2026-08-18-m2d-memory-and-search.md`（M2D，已完成，验证记录
`docs/superpowers/verification/2026-08-19-m2d-memory.md`）。

**开场只有一句：等待已经在了。** M2A 交付了 `waiting_external`、`wait_kind`、
`wait_deadline_at` 和 Scheduler 的到期扫描,M2C 让 `waiting_approval` 第一次
有了生产用法。§13 第 10 条要的东西——父 Run 释放租约、销毁沙箱、挂在一组子 Run
上等——不需要新造状态机,它需要的是**第二种 `wait_kind` 的生产者**,以及一个能
在子 Run 终止时把父唤醒的东西。

**目标：** 主 Agent 并行委派两个以上子 Agent,各自独立 Run、Session、
SessionWorkspace、事件、沙箱与费用记录;父子之间只经 Artifact 授权传文件;
整棵树与其重试链共享同一根预算;子 Agent 权限是父权限与委派策略的交集。

**顺序原则：先立不变量,后开并行。**
第 1 步只做**纯的**委派策略与交集,一个子 Run 都不创建。第 2 步才让子 Run 被
创建出来,而且一开始就带着 `depth` 上限和根预算。第 3 步父 Run 才允许去等。
理由和 M2D 的一样直白:并行最危险的坏法是子 Agent 拿到了父没有的权限,第二危险
的是一棵树把安全阀刷成了每个 Run 一份。前者是交集问题,后者是预算问题,而两者
都必须在第一个子 Run 存在之前就成立——事后再收窄,那个已经跑完的子 Run 已经拿
它不该有的东西做完事了。

---

## 四条贯穿全篇的红线

- **交集只会收窄,没有任何写法能放宽。** 和 M2C-1 的四层出站范围、M2C-2 的
  「不许绑定全部」同一个手法:`intersect` 的空集是**空**而不是全部,未知的面
  是空而不是缺省放行。一个能"补上"父没有的权限的参数,第一天就会被用来给子
  Agent 开一个父自己都没有的口子。
- **`depth` 是硬上限,不是约定。** §13 第 3 条:子 Agent 不能创建孙 Agent。
  这要在**创建路径上**拒绝,而不是靠子 Agent 的 spec 里恰好没绑委派工具——
  一个绑错了的 spec 不该能开出第二层。
- **父子不共享可写目录。** §13 第 8 条。文件只经 Artifact 授权传,而"授权"
  是一行记录,不是一次拷贝。任何让父子挂到同一个 SessionWorkspace 的写法都是
  这条红线的反面。
- **一棵树一个预算。** §12.4 已经把 `budget_root_run_id` 定义好了,M2A 也已经
  让重试链共享它。子 Run **继承根**而不是新建一个;创建子 Run 不重置任何一项
  累计值,这条要有一条测试直接盯着数字看。

---

## 1. 委派策略与交集：纯的那一半

- [x] `packages/backend/tests/unit/agents/test_delegation.py`：先写会失败的测试。
      `DelegationScope` 覆盖六个面:工具、文件、网络、密钥、技能、记忆。
      `intersect(parent, requested)` 只会收窄。
      测试要钉住:空集是空;父没有的面子一定没有;
      **没有任何参数组合能让结果比父宽**——这条用穷举参数化写。
- [x] `memory.read_private` / `memory.propose_private` 是记忆面的两个权限位
      (§13 第 5 条)。子 Agent 读的是 `workspace + 子 Agent + end_user`,
      **不是父 Agent 的 scope**——M2D 的 `MemoryScope` 已经保证了这一点,
      这里只需要不把父的 agent_id 传下去,并加一条测试证明没传。
- [x] `AgentSpec` 加 `delegation`：可委派的子 Agent 别名清单 + 每个的
      `delegation_scope`。发布时检查子 Agent 存在、在同一工作空间、
      且其 scope 不超过本 Agent 自己的权限——**发布期就拒绝**,
      而不是等到运行时才发现交集是空的。

## 2. 子 Run 的创建：depth、根预算与主体继承

- [x] 数据模型与迁移 `0027`：`runs` 加 `parent_run_id`、`depth`、
      `delegation_scope`（快照,不是引用——父 Version 事后改了不该影响
      已经在跑的子 Run,和 §16.3 的审批哈希同一个道理）。
      `depth` 上 CHECK `depth <= 1`。同一份迁移里放宽了事件 CHECK：
      `run_delegated` 和它的生产者一起到,和前四次放宽同一个规矩。
      另加 `ck_runs_delegation_complete`：三个字段一起有或一起无,
      一个「有父无 scope」的行是没人能说出权限的子 Run。
- [x] `agent.delegate` 平台工具：父 Run 提出委派,平台创建子 Run。
      创建路径上拒绝 `depth >= 1` 的调用者——红线二,一条测试直接调
      `SqlChildRuns.delegate`,而且那个子 Agent 的 spec 是**故意绑错的**
      （它自己也带委派策略和工具）,证明拒绝只看 `depth`。
      把代码里那道判断拿掉重跑,`ck_runs_depth` 在数据库层拦住——
      两层都验过。
- [x] 子 Run 继承:`budget_root_run_id`（红线四）、`end_user_id`、
      `CallerIdentity`。**不继承**:Session、SessionWorkspace、沙箱、
      私有记忆内容。各有一条测试。后三条断言的是「没有这条路」:
      SessionWorkspace 按 Session 建,沙箱预留按 `run_id` 唯一,
      私有记忆按 workspace+agent+subject 隔离而子 Run 是另一个 agent。
- [x] 并行上限:`DelegationPolicy.max_parallel`,和 `children` 在一起。
      两个候选位置都没选,理由见第 8 节的更正。
- [x] 集成测试：一次委派两个子 Run,两个都真的跑起来,
      各自有独立 Session 和独立 SessionWorkspace;
      根预算的 `consumed_model_calls` 是三个 Run 的和,不是各自重置。
      用两个 Worker 并发跑,而不是一个 Worker 跑两遍——后者只能证明
      两个 Run 都执行了,前者才证明谁也没在等谁。

## 3. 父 Run 的等待：wait_kind=child_runs

- [ ] `WAIT_CHILD_RUNS` 作为第三种 `wait_kind`（M2A 有 `timer`,M2C 有
      `approval`）。父 Run 进入 `waiting_external`,带子 Run ID 集合、
      `wait_policy=all|any` 和 `wait_deadline_at`。
- [ ] 进入等待时**释放 WorkerLease 并销毁 SandboxReservation 与
      SandboxInstance**（§13 第 10 条的原话）。M2A 的 `_close_sandbox`
      已经做这件事,这一步是接上去并加一条测试证明容器真的没了。
- [ ] 唤醒:`all` 在全部子 Run 进入终态后;`any` 在首个成功后唤醒
      **并默认取消其余**。全部终止而无成功结果时父 Run 仍回 `queued`
      并收到**结构化失败摘要**——不是异常,是一份它能读的东西。
- [ ] 到期:`wait_deadline_at` 过了按 `paused(external_timeout)` 处理。
      Scheduler 的 `_settle_due_waits` 已经这么做了,这一步验证它对
      `child_runs` 同样成立(它现在对非 timer 的 kind 就是这么处理的)。
- [ ] 级联取消:取消父 Run 时,仍在运行或等待的子 Run 一并取消（§13 第 11 条）。

## 4. 结果投递：幂等与保留

- [ ] 子 Run 终止时把结构化结果投给父 Run,用幂等键（§13 第 9 条）。
      父 Run 暂时不可用（正在被别的 Worker 持有、或还没回到可写状态）时
      结果**保留并重试**,不丢。
- [ ] 一条集成测试直接制造这个情况:父 Run 不可用时子 Run 结束,
      恢复后结果**只投一次**。这是路线图的最后一条出口检查。
- [ ] 父 Run 拿到的是**结构化结果 + 明确授权的 Artifact 引用**,
      不是子 Run 的完整上下文（§13 第 7 条）。一条测试断言子 Run 的
      transcript 没有整段进入父 Run。

## 5. 文件：只经 Artifact 授权

- [ ] 父传子:父把选定文件存成 Artifact 并创建**只读授权**;
      子 Run 读的是那份授权,不是父的目录。
- [ ] 子传父:子的产物上传对象存储后,由平台授予父 Run 读取权。
- [ ] 一条测试断言父子**没有共享的可写挂载**——红线三,
      断言的是"没有这条路"而不是"这条路被拒了"。

## 6. 控制台：最小任务树

- [ ] Run 详情显示这个 Run 的父与子（有的话），子 Run 可点进去。
      完整任务树视图是 M3,这里只要**能看出这是一棵树**。
- [ ] `explain.ts` 加 `child_runs` 等待的人话:说清楚它在等谁、等几个、
      `all` 还是 `any`,以及"它自己不会醒"。
- [ ] 两个 locale 的 i18n。

## 7. 验收与记录

- [ ] 后端、ruff、pyright、vitest、tsc、eslint、e2e 全跑。
- [ ] 路线图 §8 的七条出口检查逐条对上证据。
- [ ] `tests/e2e/children.spec.ts`：父 Agent 委派两个子 Agent,
      控制台上看到任务树,父在等,两个子跑完,父醒来完成。
- [ ] 写 `docs/superpowers/verification/2026-08-XX-m2e-child-agents.md`,
      结构照 M2D:做了什么、走了哪一遍、出口检查逐条对证据、
      **哪些东西这一遍没能证明**、以及不声称什么。
      「不声称」至少要写清楚:一层就是一层,不是"暂时只开一层";
      交集是权限的交集,不是能力的保证;
      并行是两个 Run 同时在跑,不是两个 Agent 在协作。
- [ ] `docs/development.md` 加一节:怎么配一个可委派的 Agent,
      `all` 和 `any` 分别意味着什么,子 Run 失败时父看到什么。

---

## 8. 这份计划替产品做的三个决定

**一、`delegation_scope` 在子 Run 上存快照,不存对父 Version 的引用。**
另一个合理答案是运行时去读父 Version 的 spec。不选它,理由和 §16.3 的审批哈希
一样:父 Version 在子 Run 跑到一半时被改了(或被回滚了),那个子 Run 已经拿着
旧交集在做事了,而一个"现在去读"的实现会让它中途换一套权限。快照的代价是
一行 JSON 的重复,换来的是"这个子 Run 是按什么权限跑的"事后可查。

**二、并行上限跟着委派走,既不在 `AgentLimits` 也不在工作空间。**
M2C-2 的货币上限放在了工作空间,理由是「上限是运维的决定」。这条不一样:
并行度是**这个 Agent 的任务形状**——一个做批量核对的 Agent 天然要开五个,
一个做审批摘要的开一个就够。

**这条计划原本写的是放进 `AgentLimits`,并要求实施时确认改哈希可接受。
确认的结果是不可接受,所以按计划的退路走了,但退到了第三个地方。**
`limits` 会被序列化进每一份规范化 spec,加个字段就会给这个平台写过的
每一个内容哈希都换一遍。工作空间方案能避开这点,但会把「任务形状」错放成
「运维决定」。实际做的是第三种:`AgentSpec.delegation` 是一个**可选的顶层
文档**,不写就不带这个键——`skills`、`http_tools`、`mcp_tools` 依次做过同一个
承诺,这是第八次。`max_parallel` 跟着 `children` 一起放在里面,既没动哈希,
也没把它交给运维。一条测试直接钉住:不委派的 Agent 哈希与从前一模一样。

**三、`any` 策略默认取消其余子 Run,不是默认留着跑完。**
§13 第 10 条写的是「默认取消」,这里承接。另一个合理答案是留着跑完并把结果
存起来备用。不选它,是因为那些子 Run 还在花根预算的钱,而父 Run 已经拿到了
它要的答案——继续烧下去是在为一个没人会读的结果付费。代价是一个刚要成功的
子 Run 可能在最后一刻被取消,这一点要在文档里写明,而不是让人从账单上发现。
