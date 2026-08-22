# OIDC 登录实施计划 — §27.3 第 1 条

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development
> 或 superpowers:executing-plans。步骤用 `- [ ]`。

**产品：** v2.5 §218 第 11 条、§353、§1129、§1136、§1299、§27.3 第 1 条。
**目标：** 「通过 OIDC 登录并按固定角色访问工作空间资源。」

**这一条是给平台成员的身份，和终端用户入口是两套东西。** 终端用户由企业签 JWT 担保，
平台不认识他；平台成员是平台自己的用户，OIDC 只是换一种**认证**方式，授权仍然是
`Membership` 的固定角色（§4.6），一格都不变。

## 0.2 留下的地基

| 已有 | 这次要做的 |
|---|---|
| `auth_identities` 有 `provider` 列，本地登录查 `provider == "local"` | 加 `provider == "oidc"` 的一条路径 |
| `UNIQUE (provider, subject)` | §353 的「一个 User 可绑多个 AuthIdentity」已经成立，不用改 |
| `Membership` 固定角色 | **一行不改**。OIDC 换的是认证，不是授权 |
| `auth_sessions` + cookie + CSRF | 换出来的会话与本地登录**完全一样**，下游无感 |
| `password_hash` **NOT NULL** | 唯一的 schema 改动：OIDC 身份没有密码，必须可空 |

迁移从 `20260822_0035` 开始，`down_revision` 接 `20260820_0034`。

## 四条红线

- **不自动按邮箱合并账号。** 这是这份计划最重要的一条。IdP 断言的邮箱如果没验证过，
  攻击者在 IdP 上注册同名邮箱就能接管平台账号。§282 已经为终端用户定过同一个调子
  （「跨渠道合并身份必须显式绑定」），这里对平台成员保持一致：**OIDC 的 sub 没见过
  就是新用户，永远不因为邮箱相同而登入既有账号。**
- **授权不经过 IdP。** IdP 说你是谁，平台说你能做什么。任何把 IdP 的 group/role 声明
  直接翻译成 `Membership` 的做法都不在这一条里——那是另一个功能，要单独设计。
- **只做 Authorization Code + PKCE。** 不做 implicit，不做 password grant。
- **`state` 与 `nonce` 必须校验，且一次性。** 这两个不是可选装饰：`state` 挡 CSRF，
  `nonce` 挡 id_token 重放。

## 1. 提供方配置

- [ ] `oidc_providers` 表：`id, issuer, client_id, client_secret_ref, discovery_url,
      scopes, status, created_by, created_at`，`UNIQUE (issuer)`。
- [ ] **client_secret 走既有的 secrets 存储**，不落明文——§4.6「密钥…管理元数据，
      不查看明文」这一行已经定过调子。
- [ ] discovery 文档（`/.well-known/openid-configuration`）与 JWKS **走 egress-proxy**。
      终端用户入口的计划 §9 第三条决定已经立过这个规矩：没有旁路，这里不开例外。
- [ ] 一条测试钉住停用的提供方不能再发起登录。

## 2. 登录流程

- [ ] `GET /api/v1/auth/oidc/{provider}/start` → 生成 `state`、`nonce`、PKCE
      `code_verifier`，存服务端（**不放 cookie 里**），302 到 IdP。
- [ ] `GET /api/v1/auth/oidc/{provider}/callback` → 校验 `state`（一次性，用完即焚）、
      换 token、验 `id_token` 签名与 `iss`/`aud`/`exp`/`nonce`。
- [ ] 验签用 PyJWT，理由同终端用户入口计划 §9 第一条：JWT 的坑几乎全在校验侧。
- [ ] 成功 → 找 `auth_identities where provider='oidc' and subject=<sub>`：
      - 找到 → 发平台自己的会话 cookie，与本地登录**同一条路径**。
      - **没找到 → 建新 User + 新 AuthIdentity。绝不按邮箱去找既有用户。**
- [ ] 一条测试直接钉住红线一：库里已有一个邮箱相同的本地用户，OIDC 登录进来拿到的是
      **另一个** user id。

## 3. 新用户落地之后

- [ ] 新建的 User **没有任何 Membership**，因此看不到任何工作空间——这是安全的默认，
      也是「认证 ≠ 授权」的落地。
- [ ] 控制台要能说清楚这个状态：登录成功但没有工作空间时，页面得说「等管理员分配」，
      而不是显示一个空列表让人以为坏了。
- [ ] 一条测试：OIDC 新用户登录后访问任意工作空间资源被拒。

## 4. 控制台

- [ ] 登录页加「用 OIDC 登录」入口，只在配置了启用的提供方时出现。
- [ ] i18n 两种语言都要有。
- [ ] 本地登录**保持不变**——§218 第 11 条写的是「支持本地账号**和** OIDC」。

## 5. 验收与记录

- [ ] 后端、ruff、pyright、vitest、tsc、eslint 全跑；迁移 `0035` 升降级往返干净。
- [ ] e2e：能不能做取决于有没有可用的 IdP。**如果只能用桩，就在记录里写明
      「这一遍没有对真实 IdP 验证过」**，不要让一个桩测试冒充互操作性证据。
- [ ] 写 `docs/superpowers/verification/2026-08-XX-oidc-login.md`，含「没能证明什么」。

---

## 6. 这份计划替产品做的两个决定

**一、OIDC 的 sub 没见过就是新用户，永远不按邮箱合并。**
另一个合理答案是「邮箱相同且 IdP 声明 email_verified 就合并」。不选它：那把平台账号的
安全性外包给了 IdP 对邮箱的校验，而平台没有办法验证这一点是否真的做了。代价是同一个人
用本地账号和 OIDC 登录会得到两个 User，需要管理员显式绑定——**这个代价是可见的，
而账号接管不是。**

**二、不把 IdP 的 group/role 翻译成 Membership。**
另一个合理答案是自动映射。不选它：§4.6 的固定角色是平台自己的授权模型，一旦它跟着
IdP 的声明走，改一个 IdP 的组就能改平台权限，而那次改动不会经过平台的审计。
认证和授权分开，是这一条能保持简单的原因。
