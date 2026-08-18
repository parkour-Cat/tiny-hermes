# M2C-2 实施计划：外部工具、两类审批与费用安全阀

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**产品：** `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` §12.4、§16.1、§16.2、§16.3、§17、§20。
**路线图：** `docs/superpowers/plans/2026-08-17-tiny-hermes-m2-roadmap.md` §6。
**上一步：** `docs/superpowers/plans/2026-08-18-m2c-egress-proxy.md`（M2C-1，已完成）。

**开场只有一句：边界已经在了。** M2C-1 上线之后，这个平台的每一次出站都要经过
egress-proxy，没配 proxy 就一个字节也发不出去，沙箱挂在一张哪儿也去不了的网络上。
§16.5 要求的前置条件因此已经满足，这份计划做的是跑在那条边界上的东西。

**目标：** Agent 第一次能调用平台之外的业务能力——从 OpenAPI 描述生成的 HTTP 工具，
和管理员绑定的 MCP 工具子集；高风险的调用先停下来等人，而不是先做完再报告；
花掉的钱有上限，而未知的价格不被当成零。

**顺序原则：** 先只读，后写入；先「拒绝」，后「等待」。
第 1 步给 HTTP 工具建起注册与执行，但**只放行只读方法**——写方法在这一步是直接拒绝，
不是排队。第 2 步交付审批，写方法才第一次有资格进入 `waiting_approval`。
这个顺序不是审美：§16.3 写死了「M2 在引入共享资源、联网工具和外部副作用之前必须交付
本节审批能力」，而「审批到位之前写操作是拒绝」本身就是一条可以写成测试的安全性质，
比一句承诺可靠。第 3 步是 MCP，它比 OpenAPI 多的是 schema 预算这一层，
所以放在工具执行路径已经稳定之后。第 4 步是费用。每一步都能独立跑通。

---

## 四条贯穿全篇的红线

- **两次权限检查，对每一个新工具都成立。** §16.2 的第一次决定模型**被告知**什么，
  第二次决定**真正跑**什么。MCP 尤其：必须显式绑定允许的工具子集，
  绝不把 Server 发现的所有工具交给模型。第二次检查针对的是实际参数，
  不是模型说出的名字。
- **审批绑定的是规范化之后的真实参数。** 工具名、参数、工作目录、目标资源或权限
  任何一处变化，原批准失效（§16.3）。这条要有一个纯函数来算哈希，
  而不是散落在几个比较里。
- **终端用户不能批准治理操作，管理员也不能代替用户确认。** §16.3 的两类审批
  是两种主体的两种权力。确有必要的管理覆盖只能是**新建**一条治理审批并写明理由，
  永远不是改写原来那条用户确认记录。
- **未知不等于零。** 未知 usage 不记为 0，未知价格不记为 0（§12.4）。
  `usage_quality=unavailable` 的端点上货币硬上限被禁用，
  而时间、调用次数与单次最大输出仍然强制。

---

## 1. HTTP / OpenAPI 工具：注册、绑定、只读执行

- [x] `packages/backend/tests/unit/tools/test_openapi_document.py`：先写会失败的测试。
      纯函数解析一份 OpenAPI 文档，得出**可绑定的操作列表**：
      `operationId`、方法、路径模板、参数 schema。拒绝没有 `operationId` 的操作
      （名字是绑定的键，一个没有名字的操作没法被稳定地绑定），
      拒绝路径里带模板注入的写法。
- [x] `tools/domain/openapi.py`：上面那个解析器，纯的。文档从哪来是别人的事。
      同时给出**工具 schema 的估算尺寸**，第 3 步的预算要用同一个函数，
      否则两处会对「这个工具多大」给出不同答案。
- [x] 数据模型与迁移 `0018`：`http_tools`（工作空间级，名称、base_url、
      OpenAPI 文档内容哈希、可绑定操作、凭据引用、创建人）。
      文档本身按 §1 的内容哈希存一份不可变副本——**绑定的是版本，不是 URL**，
      理由和技能一样：远端文档改了不该悄悄改变已发布 Agent 的行为。
- [x] 注册时把 `base_url` 的 host 当作出站目标校验：不在工作空间已批准范围内就拒绝，
      并说出缺哪一条。这是 M2C-1 那两级范围的第一个真实消费者。
- [x] `AgentSpec` 加 `http_tools: tuple[HttpToolBinding, ...]`，绑的是
      `http_tool_version_id` + 允许的 `operation_id` 子集。发布时校验：
      版本可见、操作存在、host 在 Agent 的 `network.allow` 内。
      第六次「不写就不进规范化文档」的加宽承诺，照旧要有测试钉住内容哈希不变。
- [x] `tools/domain/registry.py`：HTTP 工具不是一个固定名字的工具，
      而是**一族**由绑定生成的工具名（`http.<tool>.<operation>`）。
      `schemas_for` 因此要能接受绑定信息，而不只是一串名字——
      这是 registry 第一次需要知道「这个 Agent 绑了什么」，
      改动要小心，并且 `authorize` 的第二次检查仍然只认绑定，不认名字。
- [x] 执行：请求由**平台**发出（不是沙箱内），走 `SafeOutboundClient`，
      于是自动经过 proxy 并被四层范围检查一遍。凭据从 Secret 取，
      注入到请求头，**永不进提示词、永不进事件、永不进日志**。
- [x] **只读方法先行**：`GET`/`HEAD`/`OPTIONS` 直接执行；
      `POST`/`PUT`/`PATCH`/`DELETE` 在这一步返回具名拒绝
      `approval_required`，并写一条事件说明「审批尚未交付」。
      测试要断言这一条——它是第 2 步之前唯一正确的行为。
- [x] 集成测试：真起一个 stand-in HTTP 服务与真 proxy，
      绑定 → 发布 → 跑一轮 → 工具结果回到会话；未绑定的 operation 被拒；
      host 不在范围内的注册被拒。

## 2. 两类审批

- [x] `runs/domain/approval.py`：纯函数。`normalize_call(tool, arguments, target)`
      给出**规范化参数哈希**；`is_still_valid(approval, call)` 判断一条既有批准
      是否仍然覆盖这次调用。§16.3 的「参数变化使原批准失效」全部落在这两个函数里，
      并且穷举测试：改工具名、改一个参数、改工作目录、改目标资源、
      调换参数顺序（不应失效）、改一个数值的精度（应失效）。
- [x] 数据模型与迁移 `0020`：`approvals`。（原写作 `0019`；那个号被第 1 步的 `http_call_refused` 事件类型用掉了。）字段照 §16.3 逐条来：
      `approval_type`（`user_confirmation` / `governance_approval`）、
      `required_permission`、`requested_by`、`expires_at`、规范化参数哈希、
      `decided_by`、`decided_at`、`decision_reason`、以及它属于哪个 Run 与哪次调用。
      默认有效期 24 小时，工作空间可在 5 分钟到 7 天之间配置。
- [x] `Run.end_user_id`：迁移 `0020` 一并加。§16.3 的 `user_confirmation`
      只能由**发起该 Run 的 EndUser 本人**决定，所以 Run 必须记住那是谁。
      这一阶段的落法写在 §9 的决定里。
- [x] Worker 侧：一次需要审批的调用不执行，写 `RUN_APPROVAL_REQUESTED`，
      Run 进入 `waiting_approval`（状态机早就支持，这是它第一次有生产用法），
      释放 lease 并销毁沙箱保留——等待中的 Run 不占 Worker 槽位也不占容器，
      这是 §12.3 已经承诺过的行为，这里第一次被真正走到。
- [x] 批准 → Run 回到 `queued`；拒绝 → `paused(approval_rejected)`；
      过期 → `paused(approval_expired)`；运行时找不到可确认主体 →
      `paused(approval_unavailable)`，**不静默升级也不代批**。
      Scheduler 加一条过期扫描，和等待超时同一处。
- [x] 权限：`user_confirmation` 只能由 Run 的 EndUser 本人决定，
      工作空间管理员和开发者都不行；`governance_approval` 只能由工作空间管理员或
      平台管理员批准，终端用户身份绝不能批准治理操作。
      两条各有一条测试，且反向的那条（管理员代批用户确认）必须失败。
- [x] 发布时的选择：AgentVersion 为每个需要用户确认的工具显式选
      「禁用」/「工作空间管理员批准的狭范围预授权」/`governance_approval`，
      否则**发布失败**（§16.3）。ServiceAccount 发起的 `noninteractive` Run
      因此永远不会需要一个不存在的 EndUser。
- [x] 第 1 步的写方法在这里解锁：不再直接拒绝，而是请求审批。
      §1 那条「审批未交付时写方法被拒」的测试改成「写方法进入 `waiting_approval`」，
      改动本身就是这一步完成的证据。
- [x] 集成测试：写操作 → `waiting_approval` → 批准 → 执行 → 完成；
      批准后修改参数 → 原批准失效并重新排队；拒绝 → `paused(approval_rejected)`；
      过期 → `paused(approval_expired)`。
      **「EndUser 之外的人尝试确认 → 403」只有单元测试，没有集成测试。**
      集成套件里只有一个引导出来的平台管理员，再造一个能登录的普通成员需要一条
      这个阶段还不存在的用户创建路径。`may_decide` 的两个方向在
      `tests/unit/runs/test_approval.py` 里是穷举的，这条要在 §6 的验证记录里
      写明「这一遍没能证明什么」。

## 3. MCP：绑定子集与 schema 预算

- [x] 数据模型与迁移 `0021`：`mcp_servers`（工作空间级，地址、凭据引用、
      能力与 schema 哈希缓存、上次校验时间）。地址同样要在已批准出站范围内。
      **实际做成了两张表**：`mcp_servers` 和 `mcp_server_versions`。
      因为本节下一条要求「绑的是 server 版本」，而一个 server 的能力不是谁上传的
      文档、是它今天怎么答——所以「版本」在这里是平台把**被审过的那份名单**
      写下来。快照定住可以被提供的名字，schema 仍然每次运行前重读。
      迁移 `0022` 一并加两个事件类型（`tool_schema_budget_exceeded`、
      `mcp_tools_revalidated`），第 4 步的价格表因此顺延到 `0023`。
- [x] 拉取与缓存：平台缓存 Server 能力和 schema 哈希，
      **每次运行前重新校验已绑定子集**（§16.2 的原话）。
      落法是**每个执行时间片一次**，不是每一轮：每轮一次会把远端的抖动变成
      整条 Run 的抖动，而只在 Run 创建时校验一次会让一周后恢复的 Run
      按一周前的形状跑。时间片是 Worker 本来就在推理的单位。
- [x] `AgentSpec` 加 `mcp_tools`，绑的是 server 版本 + 明确的工具名子集。
      **不允许绑定「全部」**：§16.2 说不得默认把 Server 发现的所有工具交给模型，
      而一个能写「全部」的字段，第一天就会被写成全部。
- [x] 预算：绑定子集的 schema 合计尺寸用 §1 的同一个估算函数算，
      对照 `TOOL_SCHEMAS` 段的 `max_tokens`。超了就写
      `tool_schema_budget_exceeded` 事件并进入 `paused(tool_budget_exceeded)`，
      **不截断 schema、不继续调用**。
- [x] 恢复后不重复扣预算：这是路线图明写的出口检查。
      预算是按 Run 计的一次性判断，恢复的 Run 重新校验一次 schema，
      如果远端没变就照旧运行，而不是把上一次的消耗再算一遍。
- [x] 执行：MCP 调用与 HTTP 工具走同一条审批与出站路径，
      写类调用同样要审批。凭据处理与 §1 相同。
- [x] 集成测试：一个 stand-in MCP server。绑定子集 → 模型只被告知这几个；
      未绑定的工具名 → `tool_not_authorized`；
      远端 schema 变大到超预算 → `paused(tool_budget_exceeded)`；
      恢复 → 不重复扣。

## 4. 费用安全阀与价格版本

- [ ] 数据模型与迁移 `0023`：`model_pricing_versions`（币种、输入/输出/缓存单价、
      生效时间、维护人）。**未配置价格 ≠ 价格为零**：
      未配置时显示「费用未知」，管理员可以显式把本地模型价格设为 0，
      这与「未知」是两个不同状态，两者各有一条测试。
- [ ] `Run` 创建时固定引用当时的 `model_pricing_version_id`（§12.4）。
      运行中改别名或改价格不会重算已经开始的 Run——
      安全阀和用量记录因此可以追溯。
- [ ] 预检与修正：每次模型调用前按预计输入与最大输出做预检，
      调用后按 Provider usage 或已标记的本地估算修正累计值。
      流式调用的最终 usage 只在结束时可得，所以**允许上限被单次调用越过一次**，
      这条要写进文档与 UI 文案，而不是留给用户自己发现。
- [ ] `usage_quality=unavailable` 的端点：禁用 Token 与货币硬上限，
      仍然强制时间、调用次数与单次最大输出。
      一条测试断言这四件事同时成立——只验前一半会让人以为那种端点不受任何限制。
- [ ] 控制台：Run 详情显示费用与它的口径（`provider` / `estimated` / `unknown`），
      估算值明确标注。已有的 `usage_quality` 展示照抄，不新发明一套词。

## 5. 控制台

- [ ] `apps/web/src/pages/ApprovalsPage.tsx` + 测试：待办审批列表、
      两类分开、显示规范化后的参数、批准与拒绝各带原因。
      **完整的治理审批队列是 M3**，这里只做够用的一页：
      看得见、批得了、拒得掉、知道为什么。
- [ ] `apps/web/src/pages/HttpToolsPage.tsx` 与 MCP 的注册页面 + 测试：
      注册、看可绑定操作、停用。地址不在出站范围内时的拒绝，
      要把缺的那一条显示出来，形状照 `outbound_entry_outside_platform`。
- [ ] Agent Builder 加工具绑定区：选 HTTP 工具版本与操作子集、选 MCP 工具子集、
      为每个需要用户确认的工具选那三个选项之一。**发布失败时**要说清是哪个工具
      没选。
- [ ] `runs/explain.ts` 加 `approval_requested` / `approval_approved` /
      `tool_schema_budget_exceeded` 的人话；Run 详情在 `waiting_approval` 时
      给出「等谁批」而不只是一个状态词。
- [ ] i18n 两个 locale 补齐。

## 6. 验收与记录

- [ ] 后端、ruff、pyright、vitest、tsc、eslint、e2e 全跑。
- [ ] 路线图 §6 的六条出口检查逐条对上：
      - 关闭 `egress-proxy` 后所有出站工具与模型调用失败，没有回退到直连
        （M2C-1 已证，这里要在**工具**上再证一次）。
      - 工作空间允许、Agent 未允许的目标被拒；Run 级范围只能收窄不能放宽。
      - MCP schema 超预算的 Run 进入 `paused(tool_budget_exceeded)`，
        恢复后不重复扣预算。
      - 终端用户身份不能批准治理操作。
      - 审批通过后修改工具参数，原审批失效并重新排队。
      - `usage_quality=unavailable` 的端点上货币硬上限被禁用，
        而时间、调用次数与单次最大输出仍强制；未知费用不记为 0。
- [ ] `tests/e2e/tools.spec.ts`：注册一个 HTTP 工具 → 绑给 Agent → 发布 →
      跑一轮只读调用 → 再跑一轮写调用 → 在控制台看到待审 →
      批准 → Run 继续并完成。
- [ ] 写 `docs/superpowers/verification/2026-08-XX-m2c-tools-approvals.md`，
      结构照 M2B/M2C-1：做了什么、走了哪一遍、出口检查逐条对证据、
      **哪些东西这一遍没能证明**、以及不声称什么。
      「不声称」一节至少要写清楚：预授权不是自动批准；
      schema 预算是估算而不是计量；未知费用不是零；
      MCP server 的返回内容与技能正文一样是不可信文本。
- [ ] `docs/development.md` 加一节：管理员怎么注册一个 HTTP 工具与 MCP server，
      怎么读一条待审批，以及为什么写操作会停下来。

## 7. 这份计划替产品做的三个决定

**一、这一阶段的 EndUser 就是发起 Run 的那个登录用户。**
§16.3 的 `user_confirmation` 需要一个「经验证的 EndUser」，而独立的终端用户
`chat-web` 要到 M3 才有。另一个合理答案是等 M3 再交付用户确认、
M2 只做治理审批。不选它，是因为那会让「外部写操作」在整个 M2 里要么全部禁止、
要么全部升级成治理审批，前者砍掉了工具的一半用处，后者把管理员变成人肉队列。
所以：`caller_type=user` 的 Run 记下 `end_user_id = caller_id`，
`service_account` 的 Run 没有 EndUser，因而只能用发布时预授权或治理审批——
这正是 §16.3 对 ServiceAccount 的要求，不是绕过它。

**二、HTTP 工具绑的是文档版本，不是 URL。**
另一个合理答案是每次运行时拉取最新的 OpenAPI 文档。不选它，理由和技能版本一样：
远端把一个参数改成必填，已发布 Agent 的行为就在没人发布任何东西的情况下变了。
代价是管理员要在文档变化后显式更新一次；这个代价是可见的，而前者的代价不是。

**三、写类调用在审批交付之前是拒绝，不是排队。**
另一个合理答案是先把请求排进队列，等审批能力上线后再一起放行。不选它，
是因为一个「排着队但没人能批」的 Run 与一个坏掉的 Run 无法区分，
而一条具名拒绝是可以读、可以测、可以在下一步被改成等待的。
这条决定的寿命只有一步长，但它让第 1 步能独立验收。
