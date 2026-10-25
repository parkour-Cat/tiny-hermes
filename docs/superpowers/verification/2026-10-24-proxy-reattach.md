# 出网代理重建后自动连回沙箱网络；gVisor 下通过真实代理出网

日期 2026-10-24。分支 `fix/proxy-reattach`。

## 问题

出网代理只在新建沙箱网络时连上一次。代理容器一旦被重建（升级、`compose up --force-recreate`），只会回到
它自己的网络上，没有任何东西把它重新连到已有的沙箱网络。重建之前就在的沙箱都出不了网，直到它们被重建。
这个问题和 gVisor 无关，runc 下也一样。

## 改了什么

- controller 的清扫在遇到有记录的沙箱网络时，检查代理是否还连着。不在就按固定地址（子网第二个，和沙箱 hosts
  里写的一致）连回去，并写一条警告日志。
- 清扫在 controller 启动时跑一次，之后每 `SANDBOX_SWEEP_SECONDS` 跑一次（默认 300 秒）。运维手册 §17.1
  写了这段断网的时间，以及想立刻恢复时的做法（重启 controller）。
- 测试夹具里的代理改为按名字收尾，因为测试会删掉它再按同名重建。
- 一条测试改成「先恢复再删」：它模拟运维手动删掉冻结的沙箱，gVisor 下直接删大约四次里失败三次。

## 测试过了

- 真 Docker：删掉代理容器、按同名重建，沙箱连不上代理；清扫之后代理回到 `.2`，沙箱又能按名字连上
  （**改之前两种运行时都失败**）。
- 本机装了 runsc：沙箱集成测试 301 passed、1 skipped（runc 和 runsc 两遍）；单元测试 2765 passed。
  上面那条手动删除的测试改完后单独跑了 5 次，都通过。
- **本机栈，按 CI 的出网设置**（`SANDBOX_NETWORK_POOL`、`EGRESS_PROXY_URL`，平台、工作空间、Agent 三层都放行
  `api`），controller 用这个分支：
  - runc：沙箱通过真实的 `egress-proxy` 请求 `http://api:8000/health/live`，拿到 200。代理日志：
    `egress allowed: caller=sandbox`。
  - runsc：同样拿到 200；请求没授权的 `example.com` 被拒绝（`target_not_in_scope`）；绕过代理直连 `1.1.1.1`
    和 `api` 都连不上。
  - runsc，清扫间隔调成 10 秒：沙箱 `sleep 45` 的过程中重建 `egress-proxy`。controller 日志出现
    `egress proxy put back on a sandbox network`，代理回到 `.2`，`sleep` 之后的请求拿到 200。
- 测完把本机栈恢复成默认设置（runc，没有沙箱网络）。

## 这一遍没能证明什么

- 代理重建后到下一次清扫之前，沙箱确实出不了网，默认最长 5 分钟。没有做「发现代理重建就立刻重连」。
- 只测了一个沙箱、一个网络；几百个沙箱网络时清扫要逐个查询，花多长时间没有测。
- 出网只试了 HTTP 到同一个 Compose 网络里的 API，没有试 HTTPS（`CONNECT`），也没有试外网目标。

## 不声称什么

- 不声称 gVisor 已经能上生产：缓存目录的 inode 上限仍然不生效（运维手册 §17），CI 也不跑 runsc。
