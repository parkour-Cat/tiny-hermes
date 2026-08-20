# 终端用户入口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**设计：** `docs/superpowers/specs/2026-08-20-end-user-entry-design.md`
**产品：** 产品设计 v2.5 §4.5.1–§4.5.4、§4.6、§278、§282、§344、§348、§928。
**上一步：** M2 全部完成（`44254f3`），§24.1 已在参考主机通过。

**目标：** 明远物流的员工小张在自己公司的 OA 里点开助手就能用，平台不认识他、
明远认识他；他的记忆和会话跨次保留，属于他本人；开发者能读到他的对话来排障，
但每次都留痕。

**架构：** 不新增进程，不新开执行路径。终端用户的 Run 走的是同一个 Worker、同一个
状态机、同一套检查点与事件——`CallerType` 多一个值而已。新增的只有身份换取这一段：
验签、映射、发会话 cookie。

**技术栈：** 沿用现有（Python 3.12、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL、
React/Vite），新增 PyJWT。

## 全局约束

- 迁移从 `20260820_0030` 开始，`down_revision` 接 `20260819_0029`。
- 新增依赖只有 `pyjwt>=2.10`。理由见第 9 节第一条决定。
- 每个阶段先写会失败的测试。
- 已发布 AgentVersion 的内容哈希**不得变化**——`end_user_access` 是第九个可选顶层
  文档，不写就不带这个键，一条测试直接钉住。
- 终端用户**永远**拿不到控制台接口。任何新端点都必须显式声明它服务哪一类主体。

---

## 四条贯穿全篇的红线

- **平台不是身份提供方。** 没有密码、没有邮件、没有找回。任何一步引入「平台自己
  认证终端用户」都是这条红线的反面（§4.5.1）。
- **凭证只能换会话，不能当会话。** 凭证 15 分钟上限由平台强制；换出会话之后不再
  需要它。一个能长期直接调 API 的凭证等于一个平台撤销不了的密码。
- **`MemoryScope` 一行不改。** 主体从 `CallerIdentity` 扩到 `EndUser`，隔离逻辑保持
  原样。如果发现要改它，说明主体建模错了，回头看而不是往下改。
- **读正文必留痕。** §4.6 放开了开发者的「查看」，代价就是这条。没有审计的查看不是
  这次批准的那个查看。

---

## 1. 身份的骨架：三张表与第三种主体

- [ ] 迁移 `20260820_0030`：`end_users`、`external_identities`、`channel_issuers`
      三张表，字段见设计 §3。`external_identities` 上
      `UNIQUE (workspace_id, channel, external_user_id)`——§282 的原话，是这套设计
      的地基，不是索引优化。
- [ ] `CallerType` 增加 `end_user`。带 `caller_type` 列的是 `sessions` 与
      `idempotency_records`——**不是 `runs`**（`runs` 上是 `end_user_id`，没有
      `caller_type`）。两处 CHECK 由 `_in_enum` 从枚举生成，迁移里要显式重建。
- [ ] `end_users` **不含**邮箱、姓名等可识别信息。企业若在凭证里给，存
      `external_identities.profile`（JSON）。**一条测试断言 `end_users` 的列里没有
      任何可识别字段**——这是 §344 抹除能保持廉价的前提，写成测试才不会被后来的人
      顺手加一列。
- [ ] `tests/unit/identity/test_end_user_scope.py`：`MemoryScope` 接受 `EndUser`
      主体后，仍然**无法构造「所有主体」**。M2D 已经把这条钉死过，这里证明换了主体
      种类它依然成立。

**出口检查：** `alembic upgrade head` 后 `alembic check` 干净；降到 `0029` 再升回来
干净；单元测试证明枚举第三值不破坏既有 Run 的读写。

## 2. 验签：把凭证变成一个主体

- [ ] `pyproject.toml` 加 `pyjwt>=2.10`，`uv lock`。
- [ ] `packages/backend/src/tiny_hermes/identity/domain/end_user_credential.py`：
      纯函数 `verify(token, issuer_record, now) -> VerifiedCredential | Refusal`。
      不碰数据库、不发 HTTP。
- [ ] `tests/unit/identity/test_end_user_credential.py`：**先写，且要覆盖每一种失败**
      ——签名不对、`iss` 不匹配、`aud` 不是本工作空间、过期、`nbf` 未到、
      `exp` 超过 15 分钟上限、算法是 `none`、算法被换成 HMAC（alg confusion）。
      每一条单独一个测试，因为这些失败的修法在不同的人手里。
- [ ] **算法白名单只允许 `RS256` 与 `ES256`**，在调用 PyJWT 时显式传
      `algorithms=[...]`。不传或传空是 alg confusion 的经典入口。
- [ ] `exp` 上限 15 分钟由平台强制且**单独回一个可区分的拒绝原因**——这是企业配置
      错误，值得被明确告知（设计 §8）。其余失败一律回同一种 401，不泄露是哪一种。

**出口检查：** 一个用 HMAC 重签的 token 被拒；一个 `alg: none` 的 token 被拒；
一个 `exp` 设成 24 小时的 token 被拒且原因可区分。

## 3. 换取会话：终端用户第一次存在

- [ ] `channel_issuers` 的读写：工作空间管理员登记签发方（公钥或 JWKS URL）与
      `allowed_origins`。JWKS 拉取走 `SafeOutboundClient`——**它是出站请求，必须过
      egress-proxy**，不能因为「只是取个公钥」就开后门。
- [ ] `POST /api/v1/end-user/sessions`：验签 → `upsert external_identities` →
      得到 `end_user_id` → 发会话 cookie（`HttpOnly`、`SameSite=None`、`Secure`）。
- [ ] 会话 cookie 与平台成员的**完全分开**：不同 cookie 名、不同签发路径、不同
      过期（默认 8 小时）。混用一套会让「两个身份体系」这条红线从代码层面消失。
- [ ] `resolve_workspace_caller` 之外新增 `resolve_end_user_caller`。**不要把终端
      用户塞进现有那个函数**——它现在是 Cookie XOR Bearer 的平台成员逻辑，加第三条
      分支会让三种主体的判定混在一处，而这正是最不该猜错的地方。
- [ ] `DELETE /api/v1/end-user/sessions/{end_user_id}`：工作空间管理员立刻踢掉某个
      终端用户的在线会话（设计 §4.3）。停用签发方只挡新凭证，这个接口才是「现在就
      让他下线」——两件事分开，是因为让每个请求回查签发方状态等于把验签成本加到
      每个请求上。
- [ ] **终端用户碰不到控制台接口**（设计 §8 末行）。一条测试拿 end-user 会话
      cookie 去调 `/api/v1/agents`、`/api/v1/sessions`、`/api/v1/memories/pending`，
      每一个都必须 403。这条是 §4.5 首句「不进入管理后台」的可执行形式，**不能靠
      「我们没给他前端入口」来满足**。
- [ ] `tests/integration/identity/test_end_user_sessions.py`：同一个 `sub` 第二次
      进来拿到**同一个** `end_user_id`；不同 `sub` 拿到不同的；已停用的签发方签的
      新凭证被拒；已抹除主体的凭证换不出会话；被踢掉的会话 cookie 立刻失效。

**出口检查：** 一次完整换取；重复换取幂等到同一主体；停用签发方后新凭证立刻失效，
而已在线会话不受影响（设计 §4.3 明说这是权衡，测试要断言这个**已知行为**而不是
断言它被踢掉）；撤销接口调用后该主体的会话立刻失效；终端用户调控制台接口全部 403。

## 4. Agent 分配：两层都要过

- [ ] `AgentSpec` 加可选顶层文档 `end_user_access`（`{"enabled": bool}`）。
      不写就不带这个键——第九次做同一个承诺。
- [ ] `tests/unit/agents/test_end_user_access.py`：**不声明它的 Agent 内容哈希与
      从前一模一样**。前八次都有这条测试，这次也要有。
- [ ] 平台侧闸门 + 企业侧 `agents` 数组，两层都过才放行。
- [ ] 两个方向的拒绝都要说清楚：凭证列了但闸门没开 → 指名是哪个 Agent；闸门开了
      但凭证没列 → 该终端用户看不到它。**修法在不同的人手里，所以不能回同一句话。**
- [ ] 平台校验凭证列出的 Agent 确实属于该工作空间——否则企业可以签发别人的 Agent。

**出口检查：** 四种组合各一条测试（开/关 × 列/不列）。

## 5. Run 与记忆：接上已经预留的电

- [ ] 迁移 `20260820_0031`：`memories.subject_type` 的 CHECK 是**硬编码**的
      `IN ('user', 'service_account')`，不由枚举生成，所以第 1 节没有连带放宽它。
      终端用户成为记忆主体之前必须先放宽，否则第一条私有记忆就写不进去。
- [ ] 终端用户创建的 Session：`caller_type='end_user'`、`caller_id=end_user.id`。
- [ ] `runs.end_user_id` 写入真正的 `EndUser` id（此前只在 `caller_type=user` 时
      写调用者）。
- [ ] `USER_CONFIRMATION` **第一次有生产者**：终端用户发起的 Run 遇到需要本人确认的
      写操作时，开一个 `user_confirmation` 而不是 `governance_approval`。
      §27.2 场景 6 那格「绿得不对」因此变成真的绿——**验收记录要同步更新那句话**。
- [ ] **本人能答自己的确认。** 只上生产者不上消费者比不上更糟——审批路由是
      `_CONSOLE_ONLY` 且会 403 掉终端用户 cookie，于是那个 `user_confirmation`
      属于谁、谁就答不了，Run 一路挂到过期。§4.6 矩阵写的是「用户确认审批 |
      终端用户 | 仅发起人本人」，所以要有一条终端用户自己的审批端点，且**只能**
      答自己发起的、`approval_type=user_confirmation` 的那一条。治理审批对终端
      用户永远 403，这条不能有例外。
- [ ] `tests/integration/runs/test_end_user_memory.py`：M2D 的隔离测试换成
      `EndUser` 主体重跑——A 的私有记忆不出现在 B 的同 Agent 会话里，断言的是
      **发给模型的字节**而不是查询结果。
- [ ] 主体自助接口（导出、更正、遗忘、抹除）接受 `EndUser` 主体，接口形状不变。

**出口检查：** 小张第二次进来，助手记得他；小张和小王互相看不到对方的记忆；
小张能导出自己的数据；抹除后检索不到。

## 6. 读正文留痕

- [ ] `GET /api/v1/sessions/{id}/messages`：会话属于终端用户时，写一条
      `end_user_session.read` 审计，含读取者、被读主体、会话 id、时间。
- [ ] **列表页不写审计**，读正文才写。一次列表返回四十个标题，为它写四十条审计会把
      真正有意义的读取淹掉。
- [ ] 更正、删除、遗忘**不对开发者开放**——§4.6 只放开了「查看」。一条测试断言
      开发者调这三个接口被拒。
- [ ] `tests/integration/runs/test_end_user_session_audit.py`：开发者读一次正文，
      审计表多一行且含双方身份。

**出口检查：** 读正文有审计；列表没有；开发者改不了终端用户的记忆。

## 7. 聊天界面

- [ ] **先做接缝，再换实现。** `apps/chat-web` 的 API 层收到一个模块后面
      （`src/api/session.ts` 之类），先不改行为、测试全绿、提交。研究文档说这是
      「一个文件而不是七十个」的改动——前提正是先有这个接缝。
- [ ] 换掉认证：凭证换会话一次，之后携带 end-user 会话 cookie。
- [ ] 跨域：`SameSite=None; Secure`；`X-Frame-Options` 不能是 `DENY`；允许嵌入的
      来源从 `channel_issuers.allowed_origins` 读。
- [ ] `tests/e2e/end-user.spec.ts`：企业签凭证 → 打开聊天 → 对话 → 关闭重开仍是
      同一个人 → 自助导出拿到自己的数据。

**出口检查：** e2e 全绿；控制台的 Playwright walk 不受影响。

## 8. 验收与记录

- [ ] 后端、ruff、pyright、vitest、tsc、eslint、e2e 全跑。
- [ ] 迁移 `0030` 降级升级往返干净，并降到 `base` 再升回来。
- [ ] 写 `docs/superpowers/verification/2026-08-XX-end-user-entry.md`，结构照 M2E：
      做了什么、走了哪一遍、出口检查逐条对证据、**哪些东西这一遍没能证明**、
      以及不声称什么。
- [ ] 「不声称」至少要写清楚：跨渠道身份没有合并（同一个人从飞书和 Web 进来是两个
      `EndUser`）；平台没有验证企业签发方是否真的认识这个人——**平台信任的是签名，
      不是事实**；停用签发方不踢掉已在线会话。
- [ ] 更新 §27.2 场景 6 的记录：`user_confirmation` 现在有生产者了。
- [ ] `docs/development.md` 加一节：企业怎么接入（登记签发方、签一张凭证、嵌入页面）。

---

## 9. 这份计划替产品做的三个决定

**一、加 PyJWT，不手写验签。**
仓库依赖里只有 `cryptography`，手写 JWT 校验用它做得到。不选它：JWT 的坑几乎全在
校验侧——alg confusion、`none` 算法、`aud` 漏检、时钟偏移，每一个都有过真实 CVE。
一个被审计过的库把这些默认关掉，而我们自己写的第一版一定会漏掉其中一个。代价是多
一个依赖，收益是不用在安全边界上做原创。

**二、终端用户的认证不合并进 `resolve_workspace_caller`。**
另一个合理答案是给它加第三条分支。不选它：那个函数现在是「Cookie XOR Bearer」的平台
成员逻辑，加进来就变成三种主体在一处判定，而判错的后果是终端用户拿到平台成员的权限。
两个函数各自简单，比一个函数聪明要好。

**三、JWKS 拉取走 egress-proxy，不开后门。**
另一个合理答案是「只是取个公钥，直连算了」。不选它：M2C 花了一整个阶段让
`egress-proxy` 成为唯一出站路径，并且有架构测试证明没有旁路。为了取公钥开一个口子，
等于让那条保证变成「除了这一处」。慢一点，但那条线不能有例外。
