# M2C-1 出站边界实施计划：egress-proxy 与四层范围

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**产品：** `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` §16.4、§16.5、§26。
**路线图：** `docs/superpowers/plans/2026-08-17-tiny-hermes-m2-roadmap.md` §6。
**上一阶段：** `docs/superpowers/plans/2026-08-17-m2b-skills.md`（M2B，已完成，验收记录
`docs/superpowers/verification/2026-08-18-m2b-skills.md`）。

**为什么这份计划只覆盖 M2C 的前半：** 路线图 §6 把四件事放进一个阶段——出站强制面、
MCP 与 OpenAPI 工具、两类审批、费用安全阀。它们不是同一种东西：第一件是**一条边界**，
后三件是**跑在这条边界上的功能**。§16.5 的原话是「M2 在引入 MCP 和 OpenAPI/HTTP 工具
之前必须上线独立 `egress-proxy`」——先后关系是产品设计写死的，不是排期偏好。所以这份
计划只做边界，做完之后边界可以独立验收：把 proxy 关掉，平台的每一次出站都失败，且没有
任何回退路径。工具、审批与费用写在 M2C-2，它开头第一句就是「边界已经在了」。

**目标：** 让「这个请求可以发出去吗」变成一个**网络级**的问题，而不是一个每个调用点都
要记得问的问题。今天 `SafeOutboundClient` 已经在进程内做了地址校验、重定向复查和 IP
钉定，并有架构测试禁止旁路它；但它是一个库，而库只能约束愿意导入它的代码。M2C-1 把
强制点挪到进程之外：API、Worker、Scheduler、sandbox-controller 和沙箱的动态出站，全部
经过一个独立进程，那个进程按四层范围的交集决定放不放行。

**顺序原则：** 第 1 步是纯函数（四层范围的交集），没有 I/O；第 2 步是进程本身，仍然
没人用它；第 3 步才把平台可信进程接过去；第 4 步是数据与管理面，让范围可以被配置；
第 5 步是沙箱侧，也是唯一需要改容器网络的一步。每一步都能独立跑通。

---

## 四条贯穿全篇的红线

- **强制点在进程外，不在库里。** 一个可以被绕过的检查等于没有检查。`SafeOutboundClient`
  继续存在（它做的 IP 钉定与重定向复查仍然必要），但它的出口从「直接连目标」改成
  「连 proxy」，而 proxy 自己再做一遍同样的校验。两层不是冗余：库那层保证请求在离开
  进程前形状正确，proxy 那层保证形状不正确的请求也出不去。
- **范围只能收窄。** 平台 ∩ 工作空间 ∩ Agent ∩ Run/委派。任何一层都不能打开上一层
  没有批准的目标，这是 §16.5 的原话，也是交集这个词的全部含义。计算发生在连接时，
  不在管理界面。
- **未知不等于允许。** 解析不出 IP、范围表缺失、proxy 不可达——每一个都是拒绝。
  §16.5 要求默认拒绝 loopback、link-local、云元数据地址与未批准的私有网段；「默认」
  的意思是这些判断不需要任何人先配置什么。
- **跨 origin 重定向不带凭据。** M1 已经这样做了；proxy 上线之后这条规则要在两处
  各成立一次，并且两处都有测试。

---

## 1. 纯函数：四层出站范围与交集

- [x] `packages/backend/tests/unit/outbound/test_scope.py`：先写会失败的测试。
      四层交集、收窄、空集、通配与显式条目的关系、以及「工作空间批准了 Agent 没批准」
      被拒的那条。
- [x] `outbound/domain/scope.py`：`OutboundScope` 与 `intersect(*scopes)`。
      一条范围是**主机模式与网段的集合**，不是一串正则：`api.example.com`、
      `*.example.com`、`10.1.0.0/16`。通配只允许出现在最左一段，因为
      `*.example.com` 是一个人能一眼判断的东西，而 `api-*.internal.*` 不是。
- [x] 交集的语义写成测试而不是注释：`*.example.com ∩ api.example.com = api.example.com`，
      `10.0.0.0/8 ∩ 10.1.0.0/16 = 10.1.0.0/16`，`{} ∩ 任何 = {}`。
      空集是合法结果，含义是「这一层什么都没批准」，于是这条链上什么都出不去。
- [x] `PlatformScope` 的默认值：**空**。和 `SANDBOX_IMAGE_DIGEST` 的默认一样，
      一个没有配置过的部署什么都发不出去，而不是什么都发得出去。
      唯一的例外是 M1 已经批准的模型端点地址——它们由端点自己的配置带着走，
      在 §3 里接进来。
- [x] 复用 `outbound/domain/address_policy.py` 的 `verdict()`：范围检查通过之后，
      地址仍然要过 M1 那一关（loopback、link-local、元数据地址、私有网段）。
      两个检查回答不同问题：范围问「这个目标被批准了吗」，地址策略问
      「这个 IP 是不是根本不该被连」。顺序是先范围后地址，因为后者更贵。

## 2. `egress-proxy` 进程

- [x] `packages/backend/src/tiny_hermes/egress/`：`domain/`、`application/`、
      `presentation/`。它是一个 HTTP forward proxy（`CONNECT` 加绝对 URI 两种形式），
      不是一个业务 API。
- [x] `pyproject.toml` 加 `tiny-hermes-egress = "tiny_hermes.egress.cli:main"`；
      `deploy/compose/compose.yaml` 加 `egress-proxy` 服务。它**不挂 Docker socket，
      不拿对象存储凭据，不拿模型密钥**——和 controller 一样，一个进程持有什么就是
      它能被利用成什么，这条在 §14.6 已经是断言了。
- [x] 身份：调用方在 `Proxy-Authorization` 里带一个**进程令牌**，proxy 用它区分
      API / Worker / Scheduler / sandbox 四类调用方。令牌不是密钥管理，它只回答
      「谁在问」；范围仍然来自请求里声明的 `X-Tiny-Hermes-Scope`（workspace、agent、
      run 三个 id），由 proxy 自己去查，不信任调用方声明的范围内容。
- [x] 沙箱是唯一没有令牌的调用方：它的身份由**网络**决定——proxy 为每个 Run 的
      沙箱网络分配一个入口，谁从那个入口来就是那个 Run。容器内的进程不持有任何
      可以冒充别人的东西，这是 §16.4「密钥短期注入」的同一条道理。
- [x] 每一跳重新解析、校验、钉定 IP。**重定向不由 proxy 跟随**：3xx 回给客户端，
      客户端发出的下一跳作为一条新请求再次到达 proxy，从头再查一遍。于是「每一跳
      都被检查」不需要 proxy 记住任何状态，而跨 origin 剥凭据留在凭据所在的那一侧
      （`outbound/client.py`，M1 已有测试）。计划最初写的是 proxy 也剥一遍，
      那是在设计定下来之前——一个从不跟随重定向的进程没有东西可剥。
      proxy 只剥 `Proxy-Authorization`：它是给 proxy 自己的凭据，转发出去就是
      把平台令牌泄给 Run 访问的每一台主机。
- [x] 拒绝要带原因：proxy 用 `403` 加一个结构化 body 回答被范围拒绝的请求，
      调用方把它翻译成 `OutboundRefused`。一个只回 `403 Forbidden` 的 proxy 会让
      每一次误配都变成一次抓包。
- [x] `tests/unit/egress/`：范围拒绝、地址拒绝、重定向剥凭据、未知调用方拒绝。
      `tests/integration/egress/`：真起一个 proxy 进程与一个本地目标服务器，
      走完整条链路。

## 3. 平台可信进程改走 proxy

- [x] `SafeOutboundClient` 增加 `proxy_url` 与调用方身份；配置里给出
      `EGRESS_PROXY_URL`。**没有配置时的行为是拒绝，不是直连**——这是这一步唯一
      需要吵一次的决定，写在 §6。
- [x] 现有三个出站用途逐个接过去：模型调用（`openai_model.py`）、技能 Git 导入
      （`skills/infrastructure/outbound_tarball.py`）、端点连通性检查。三处都已经
      在用 `SafeOutboundClient`，所以这一步改的是构造参数而不是调用点。
- [x] 模型端点的地址进入平台范围：管理员注册端点时，它的 host 自动成为
      `PlatformScope` 的一条。理由是运维现实——否则每注册一个端点都要再去改一遍
      出站范围，而两处不同步的第一个症状是 Run 在运行时失败。
      **这一条推到 §4**：平台范围现在只是一条配置项，没有表也没有写入口，
      而「注册端点时自动写一条」需要先有那张表。勾上的是其余四条。
- [x] `tests/unit/outbound/test_client_ban.py` 升级：现在禁的是「在 outbound 之外
      造 HTTP 客户端」；再加一条**禁止在 outbound 之内绕过 proxy**——
      `httpx.AsyncClient(...)` 不带 `proxy=` 的构造在 `outbound/client.py` 里也失败。
      路线图 §6 的「任何绕过 proxy 直连的代码路径使检查失败」落在这里。
- [x] 集成测试：把 proxy 停掉，模型调用、技能导入、端点检查三条路径全部失败，
      并且失败原因是 `egress_unavailable` 而不是超时。**没有任何一条回退到直连**，
      这是路线图第一条出口检查。

## 4. 范围的数据与管理面

- [x] 迁移 `0016`：`platform_outbound_scopes`、`workspace_outbound_scopes`，
      以及 `agent_versions.spec` 里的 `network` 段（版本化的东西不进表，
      和 `tools`、`skills` 一样跟着 AgentSpec 走）。
- [x] `AgentSpec` 加 `network: {allow: [...]}`；发布时校验它是工作空间范围的子集，
      不是就拒绝并**说出哪一条超出了**。和 `skill_summary_budget_exceeded` 同一个
      形状：refusal 里带足够修好它的信息。
- [x] 平台范围只有平台管理员能写，工作空间范围由工作空间管理员在平台范围内选，
      两处都写审计。这套角色形状 `SkillCatalog` 已经有了，照抄它而不是重新发明。
- [x] Run 级范围：`RunSpec` 暂不开放收窄接口（M2E 的委派才需要），但交集函数
      从第一天就按四层写，并有一条测试用 Run 层收窄证明它是四层而不是三层。
- [x] 控制台：工作空间设置里一个出站范围列表；Agent Builder 里一个 `network` 段，
      同样是「选，不是填」——可选项来自工作空间已批准的集合。

## 5. 沙箱侧出站

- [ ] `container_policy.py`：`network_mode` 从 `none` 变成一个**只能到达 proxy 的
      专用网络**。沙箱仍然没有到互联网的直连，也仍然没有到平台内网其他服务的路。
- [ ] 容器内注入 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`，让常见运行时默认走 proxy；
      但**不依赖它**——网络本身就到不了别处，环境变量只是让工具的错误信息好看一点。
- [ ] `tests/integration/sandbox/`：容器里 `curl` 一个被批准的目标成功，
      一个未批准的目标失败，一个 `169.254.169.254` 失败。这三条是这一步的全部意义。
- [ ] 冻结与销毁路径要一并撤掉网络入口：一个被冻结的沙箱不能再发起新连接
      （§16.4 的原话），所以入口的生命周期跟 SandboxInstance 而不是跟 Run。

## 6. 这份计划替产品做的两个决定

**一、没有配置 `EGRESS_PROXY_URL` 时，平台的出站是拒绝而不是直连。**
另一个合理答案是保留直连作为回退，理由是「本地开发不该被迫起一个 proxy」。不选它，
是因为回退路径的存在会让路线图那条出口检查永远无法真正通过——只要代码里有一条
`if proxy is None: connect_directly()`，「关掉 proxy 后所有出站失败」就变成了一句
需要人去遵守的话。本地开发的代价是 compose 里多一个服务，这个代价是一次性的。

**二、模型端点的地址自动进入平台出站范围，不要求管理员再批准一次。**
另一个合理答案是要求显式批准，理由是「两层批准比一层安全」。不选它，是因为注册一个
端点这个动作本身已经是平台管理员的显式批准，再要求一次不会带来新的判断，只会带来
一个必然被忘记的步骤——而被忘记的后果是 Run 在运行时失败，症状离原因很远。
端点被停用时它的地址随之退出范围，这条要有测试。
