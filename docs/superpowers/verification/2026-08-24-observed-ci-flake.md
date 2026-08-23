# 两次 compose-e2e 偶发失败,记下来而不是调大超时 — 2026-08-24

> 现场:PR #50,commit `18dbf8c3`。同一个 commit 跑了两次 CI。

## 事实

| 运行 | 触发 | compose-e2e |
|---|---|---|
| 32652990292 | push | **failure** |
| 32652998806 | pull_request | pass |

失败的那次,**Playwright 是 18 条全过**。挂的是它后面的 `Workspace drill` 这一步:

```
== a committed write survives a worker crash ==
  $ docker compose kill worker
  $ docker compose start worker
worker did not become healthy within 90s
```

`scripts/restart_drill.py:84` 的 `await_healthy(timeout=90.0)`;worker 的健康检查是
`process-alive`(`grep -q tiny-hermes /proc/1/cmdline`,interval 5s)。

## 判断

**和 PR #50 无关。** 那条 PR 只动 `apps/web`(一个搜索框和一个对话框),没有碰
worker、compose 文件或任何演练脚本。最近 25 次 CI 运行里,compose-e2e 只失败过
这一次。

## 为什么没有把 90 秒调大

调大超时能让这个红变绿,而且**看不出**是掩盖了什么。如果 worker 在共享 runner 上
偶尔真的要一分半才起来,那是一件值得知道的事;把预算改成 180 秒之后,下一次它花了
两分钟,同样不会有人知道。

所以这一遍只记录,不改。**它再出现一次,就值得真正去查**:先拿到失败那次
`docker compose logs worker` 的输出,确认是进程起得慢,还是健康检查在容器还没
`start` 完成时就开始计时。

## 第二次:另一个原因,同样是偶发

PR #51,commit 同样跑了两次:

| 运行 | compose-e2e | Playwright |
|---|---|---|
| 32657850484 | **failure** | `Running 18` → **17 passed, 1 failed** |
| 32657850892 | pass | 18 passed |

这次不是演练,是一条 e2e 真的红了:

```
[skills] skills.spec.ts:130 upload a skill, bind it, load it in a Run, ...
Error: expect(locator).toBeVisible() failed
  Locator: getByText('propose_once', { exact: true }).first()
  Received: hidden
  121 × locator resolved to <div role="option" aria-label="propose_once">
```

**元素找到了,但一直是 hidden。** 121 次重试都解析到同一个节点。这是 antd Select
的下拉在虚拟列表里没展开(或展开后又收起)的时序问题,不是断言的对象不存在。

**和 PR #51 无关**:它改的是定价、MCP 撤回和渠道签发方,而这条走的是 Agent
构建器里选模型场景那一步。

**也和 #47 那次技能导入 URL 修复无关**,虽然挂的正好是技能那条 walk:#47 改的是
「从 Git 重新导入版本」那个按钮,而这里挂在场景选择器上;而且 #47 合并之后的两次
CI 里 `skills.spec` 都是 ✓。

## 两次放在一起说明什么

短时间内两次,**原因不同**(一次是容器健康检查,一次是浏览器下拉),都在
compose-e2e 这一档,都在重跑后消失。这更像是 runner 争用导致的整体变慢,而不是
某一处代码的问题。

**目前的做法不变:不调超时,不加 retry。** Playwright 的 `retries: 0` 是有意的——
把它调成 1,这两次都会变绿,而「这一档在慢机器上会飘」这个事实就再也没人看得见了。

## 这一遍没能证明什么

- **没有拿到那次失败时 worker 容器自己的日志**。GitHub 的 job 日志里只有演练脚本
  的输出,而演练在超时后直接 `SystemExit`,没有把容器日志打出来。
  这本身是演练的一个缺口——**一次说不清原因的失败,和一次没发生的失败,对读日志
  的人来说差别不大**。
- 因此「进程起得慢」和「健康检查计时起点不对」这两种解释,**这一遍都没有排除**。
- **没有证明这两次是同一个根因**(runner 争用)。这是一个看起来合理的解释,
  而不是一个被测出来的结论——两次的表面原因完全不同,把它们归到一起是推断。
- **没有量过 CI runner 的负载**。「整体变慢」这句话,这一遍没有任何数据支撑。
