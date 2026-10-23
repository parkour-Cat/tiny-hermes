# 本地密码登录限流

日期 2026-10-23。分支 `feat/login-rate-limit`。产品设计 v2.19（§1.19、§23 第 16 条），运维手册 §13。

| 改了什么 | 测试 | 实现 |
|---|---|---|
| 按账号限流（API）；按来源地址限流（控制台 nginx）；过期的失败记录由 scheduler 清理 | `43694b6`、`da7b622` | `1af2979` |

## 为什么有这一遍

之前一个账号可以被无限次试密码。平台只记一条 `identity.login_failed` 审计，里面连账号都
没有。上一遍加了登录失败的计数和告警，但没有拦截。

## 怎么做的

- **按账号，在 API 里。**同一账号 15 分钟内失败 5 次，之后的登录返回 429 `login_throttled`，
  带 `Retry-After`，密码对了也不放行，直到最早那次失败移出窗口。
  - 判断在核对密码之前，所以被限流时也不会消耗 argon2 的计算。
  - 不存在的账号同样计数。
  - 登录成功清零。
  - 同一账号的尝试用 Postgres 的事务级 advisory lock 串行，两个并发的猜测不会都被算成第 5 次。
  - 失败记录存在新表 `login_failures`，账号按规范化后（去掉首尾空白、转小写）的 SHA-256 记。
- **失败要提交，不能回滚。**`AuthService` 的事务原来只在 `invalid_credentials` 和
  `invalid_bootstrap_token` 时提交；加上了 `login_throttled`，否则限流的审计会被回滚。
  `invalid_credentials` 本来就提交，失败记录跟着它一起留下。
- **按来源地址，在 nginx 里。**API 在控制台的 nginx 后面，看到的来源地址都是 nginx 自己的。
  在 API 里按地址计，一个人连续输错就会把所有人锁在外面。所以按地址的限流放在 nginx 上，
  只限登录这一个接口：每分钟 10 次，可以突发 10 次。
- **`AppError` 能带响应头了**（为了 `Retry-After`）。
- scheduler 的保留期扫描删除 15 分钟以前的失败记录。

## 测试过了

- 后端单元测试 2681 passed；pyright 0 errors；`ruff check` 通过；web 测试 392 passed；新迁移
  `alembic check` 无差异，降级再升级通过。
- 集成测试（含 sandbox 目录）1245 passed、1 skipped、**1 failed**：
  `test_run_events_sse.py::test_a_disconnected_subscriber_leaves_no_open_transaction`。
  - 这条测试在断开连接后固定等 1 秒再查有没有遗留事务。它失败的那次全量运行，是和
    `docker compose build` 全部镜像同时跑的。
  - 之后单独跑 3 次都通过，整个 SSE 文件加 identity 目录一起再跑一次 90 passed。
  - 这个分支没有改任何 SSE 或事件流的代码（`git diff main` 对那两处为空）。
  - 判断是负载下的时序问题，没有修，以 CI 为准。
- 新测试：
  - 5 次失败后，正确密码也是 429，带 `Retry-After`，写一条 `identity.login_throttled`
    审计，指标 `outcome="throttled"` 加一；
  - 失败 4 次时正确密码仍然能登录；
  - 登录成功清零；
  - 窗口外的失败不算；
  - 一个账号的失败不影响另一个账号；
  - 不存在的账号同样被限流；
  - 账号名的大小写和首尾空白算同一个账号；
  - 库里没有账号名明文；
  - scheduler 删除窗口外的记录、保留窗口内的；
  - nginx 配置里登录接口有按地址的限流，并且仍然转发到 API。
- `nginx -t`：用 web 镜像同版本的 `nginx:1.29.1-alpine` 检查配置，语法通过。

### PR 的第一次 CI

两次运行里，一次全绿；另一次红了两处，都和这个分支无关：

- `test_event_push.py::test_an_announced_event_reaches_a_subscriber_at_once`：同一条测试在
  main 的 17:10 那次运行（#125 合并之前）就红过一次，是已有的时序问题。
- compose-e2e 的工作区配额演练：一个 Run 停在 `interrupted` 120 秒，scheduler 的沙箱清理
  每次都被 controller 以 `DockerUnavailable` 拒绝，也就是 CI 机器上的 Docker 守护进程拒绝了
  那次调用。这个分支没有改沙箱的代码。
  - **顺带发现一个排查上的缺口**：controller 只把异常的类名传回给 scheduler，两边都没有记下
    Docker 说的具体内容，所以日志里看不出 Docker 当时为什么拒绝。没有在这一遍修。

推了这条记录，重新跑 CI，看最终提交上的结果。

## 走通了什么

在本机的 Compose 栈上，重建到这个分支，核对过 api、worker、scheduler、controller 四个容器
都有新代码、web 容器的 nginx 配置里有这个限制：

- **按账号**：经控制台的 3000 端口（nginx），同一个账号连续 5 次错误密码都是 401，第 6 次
  是 429，`Retry-After: 900`，响应体 `code` 是 `login_throttled`。
- **按来源地址**：等了一分钟，让 nginx 的令牌桶回满，再从同一个地址快速发 25 次登录，每次
  用不同的账号，避免先撞上按账号的限制。前 11 次到了 API（401），之后 14 次都被 nginx 直接
  挡掉（429）。
- **运维手册里的解锁命令可用**：
  - 手册 §13 里那条 SQL 算出的 key，和 Python 算出的一致：用带大小写和空格的账号名，正好
    查到那 5 条记录；
  - 删掉之后这个账号重新回到 401；
  - 测完清空了 `login_failures`。

## 这一遍没能证明什么

- **并发猜测时不会多算，只靠 advisory lock 的代码，没有并发测试。**
- **直接访问 API 的 8000 端口没有按地址的限流。**本地 Compose 把 8000 发布在所有网卡上。按
  账号的限流仍然有效，按地址的只在 nginx 那一层。运维手册写了生产环境怎么处理，没有改
  Compose 的端口发布。
- **被 nginx 挡掉时，控制台显示的是什么，没有在浏览器里看过。**nginx 的 429 返回的是 HTML，
  不是平台的 problem JSON；登录页显示的是前端对非 JSON 错误的默认文案。按账号的 429 会显示
  英文的 `detail`。
- **没有管理员解锁的界面**，只有运维手册里的 SQL。
- 知道账号名的人可以让这个账号 15 分钟内无法用密码登录。这是按账号限流的代价，写进了产品
  设计，没有缓解措施。
- chat-web 的 nginx 没有加限制：终端用户不用本地密码登录。

## 不声称什么

- 不声称这能防住分布式的撞库。每个地址每分钟 10 次、每个账号 15 分钟 5 次，一个有很多地址的
  攻击者，对很多账号各试几次，仍然试得出来。那要靠强密码、OIDC 和外部 WAF。
- 不声称 5 次 / 15 分钟、10 次 / 分钟是最合适的值。它们是常见的起点。
