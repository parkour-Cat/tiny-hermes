# OIDC 登录 — 验收记录 2026-08-22

> 计划：`docs/superpowers/plans/2026-08-22-oidc-login.md`。
> 产品：v2.5 §218 第 11 条、§353、§1136、§4.6。对应 §27.3 第 1 条。

## 1. 这一条做了什么

**换的是认证，不是授权。** OIDC 让平台成员用企业 IdP 登录，登录之后拿到的会话与本地
登录**完全一样**，`Membership` 的固定角色一格没动。IdP 说你是谁，平台说你能做什么。

| 做的 | 说明 |
|---|---|
| `oidc_providers` + `oidc_login_states` | 迁移 `20260822_0035`，`down_revision` 接 `0034` |
| Authorization Code + PKCE | 不做 implicit，不做 password grant |
| `state` / `nonce` / `code_verifier` | **存服务端**，不放 cookie；`state` 一次性 |
| `id_token` 校验 | PyJWT，固定 RS256/ES256 白名单，验 `iss`/`aud`/`exp`/`nonce` |
| JIT 建号 | 没见过的 `sub` 建新 User，**永不按邮箱找既有账号** |
| discovery 与 JWKS | 走 `SafeOutboundClient`／egress-proxy，没开旁路 |
| 公开的提供方列表 | `GET /api/v1/auth/oidc/available`，只回 `{id, issuer}` |
| 登录页与空工作空间提示 | 见 §4 |

## 2. 红线一：绝不按邮箱合并账号

这是整件事的重点。IdP 断言的 `email` 只值它自己对邮箱的校验那么多，而平台**没有办法
验证那件事是否真的做过**。自动合并等于把平台账号的安全性外包出去：任何能在那个 IdP
上注册某个地址的人，就能接管平台上同名的账号。

落地方式是**查找逻辑本身**只按 `sub`，不是在按邮箱查之后再加一道判断——
`find_or_create_oidc_identity` 从来不问邮箱。

一条测试直接钉住：库里已有本地用户 `alice@example.com`，OIDC 登录带着**同一个**
`email` 声明进来，断言 `result.user.id != local_user.id`。

代价是可见的：同一个人用本地账号和 OIDC 登录会得到两个 User，要管理员显式绑定。
**这个代价看得见，账号接管看不见。**

## 3. 实现过程中抓到的两个既有缺陷

**一、`SqlAuthStore.find_session` 的 join 里硬编码了 `provider == "local"`。**
这个如果不修，OIDC 登录会**成功**、发出 cookie，然后**下一个请求就是未认证**——登录页
能用，之后什么都不能用。既有测试一条都发现不了，因为既有的会话全是 local 的。

**二、`SafeOutboundClient.request` 只支持 JSON body。** OIDC 的 token 交换按
RFC 6749 §4.1.3 必须是 `application/x-www-form-urlencoded`。加了一个 `data=` 参数，
**没有**绕开出站边界——那条边界有架构测试证明没有旁路。

## 4. 控制台

- 提供方按钮是**真链接不是 fetch**：`/start` 回 302 跳 IdP，用 XHR 会在页面内跟随
  重定向然后失败。按钮上印 issuer 的 host，因为一个只写「用 SSO 登录」的按钮在信任
  两个 IdP 的部署里是抛硬币。
- 提供方查询**故意没有错误分支**：查不到就不显示这一段，本地登录照常。§218 第 11 条
  是「本地账号**和** OIDC」，SSO 挂了不能把密码表单一起带走。
- 回调失败会跳回登录页。**必须说出来**——不说的话，它和正常打开登录页是同一个画面，
  人会对着同一个坏掉的提供方一直重试。
- 新用户没有任何 Membership 时，工作空间页说的是「你已经登录了，但还不属于任何工作
  空间」，不是「还没有工作空间」。后者对**能建工作空间的平台管理员**是对的，对一个
  刚被 OIDC 创建、什么都做不了的人读起来像「这个系统是空的」，会让他去找一个不存在的
  bug。

## 5. 这一遍没能证明什么

- **从没对着真实 IdP 验证过。** 测试里的 IdP 是仓库内的桩——真 socket、真走
  egress-proxy、真 RSA 签名，所以**协议形状和出站边界是真的**。但 Google、Okta、
  Auth0、Keycloak 一个都没连过。**桩测试不是互操作性证据**，任何一家的实现细节
  （discovery 字段、`aud` 是数组还是字符串、额外声明）都可能让它当场失败。
  这一条要在真实环境里跑一次才能说「能用」。
- **没有做账号绑定入口。** 同一个人的本地账号和 OIDC 身份现在是两个 User，
  §353 说「User 可以绑定多个 AuthIdentity」，schema 支持，但**没有绑定的界面或接口**。
- **没有把 IdP 的 group/role 映射到 Membership**，这是有意的（计划 §6 第二条决定）：
  一旦映射，改一个 IdP 的组就能改平台权限，而那次改动不经过平台审计。
- **没有 e2e。** 浏览器走查需要一个跑在 compose 栈里的 IdP，这一遍没有搭。
  登录页的三种情形由组件测试覆盖，**但没有人从浏览器点进去过**。
- **没测过 IdP 不可达时的用户体验**，只测了后端会拒绝。

## 6. 数字

| | |
|---|---|
| backend unit | 1964 passed |
| backend integration | 657 passed（不含 sandbox，见下）|
| ruff / pyright | clean |
| `alembic check` | No new upgrade operations detected |
| 迁移 0035 升降级往返 | clean |
| console vitest | 164 passed |
| console lint / build | clean |

`tests/integration/sandbox/` 在 macOS 上因为一个与本条无关的 socket 路径问题跳过，
CI 上会跑。

合并前按项目惯例必须重新取得 **compose-e2e 绿色**——本地跑过的栈不算数。
