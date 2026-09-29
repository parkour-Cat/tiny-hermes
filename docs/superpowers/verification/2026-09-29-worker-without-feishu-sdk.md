# Worker 与 api 不再加载飞书 SDK

分支 `perf/worker-no-feishu-sdk`，代码截至 `fa7276a`。建在 `feat/concurrency-probe`
之上。未推送、未合并。

## 为什么有这一遍

[并发探针](2026-09-29-concurrency-probe.md)量出每个 Worker 进程约 220 MiB，而 CPU 峰值
不到 8%。在 worker 容器里拆开看：

| 情况 | 内存 | 模块数 |
|---|---:|---:|
| 空的 Python 解释器 | 12 MiB | — |
| 加载 `tiny_hermes.api.cli` | 237 MiB | 11,745 |
| 单独加载 `lark_oapi` | +152 MiB | 10,697 |
| 用替身顶掉 `lark_oapi` 后再加载 `cli` | 102 MiB | 875 |

`cli.py` 里同时放着 api、Worker、scheduler 三个入口，并在文件顶部 import 了
`feishu_long_connection`。于是 api 和每个 Worker 都加载了整个飞书 SDK，而长连接只有
scheduler 会开。

## 测试过了

| 套件 | 结果 |
|---|---|
| `tests/unit/api/test_cli.py` | 4 passed |
| `tests/integration/channels/test_long_connection_lifecycle.py` + `tests/unit/api` | 36 passed |
| `ruff check` / `pyright`（改动文件） | 通过 / 0 errors |

先写测试并看它红（`14df155`）：在一个新的解释器里 import `cli`，断言 `lark_oapi` 不在
`sys.modules` 里。另有一条对照测试断言 import 长连接模块**会**加载 SDK，免得在没装 SDK
的机器上，前一条也能通过而什么都没证明。

## 走通了什么，以及第一次没走通

**第一次实现（`e7d21b4`）测试全绿，但真实的栈坏了。** 用 `redeploy.sh` 重建后，
scheduler 以退出码 1 退出：

```
lark_oapi.channel.errors.FeishuChannelError: WebSocket connect failed: This event loop is already running
```

原因：`lark_oapi/ws/client.py:32` 在 import 时执行 `loop = asyncio.get_event_loop()`，
之后在线程里对这个 loop 调 `run_until_complete`。import 原来在模块顶部，发生在
`asyncio.run` 之前，拿到的是一个闲置的 loop；挪进 `_long_connections` 之后，import 发生
在 scheduler 的 loop 已经在跑的时候，拿到的就是正在跑的那个 loop。

长连接的生命周期测试没有发现它，因为那些测试用 fake 替换了 SDK 的 channel——正是
CLAUDE.md 里「夹具必须用被集成那一方自己的类型构造」那一条。

修法（`c3994c4` 测试，`fa7276a` 实现）：`scheduler_main` 在 `asyncio.run` 之前先
import 长连接模块。新测试在子进程里走 `scheduler_main` 的顺序，断言 SDK 绑定的 loop
不是 scheduler 正在跑的那个。修复前它是红的（`'True' == 'False'`）。

修复后在本机栈上：

| 进程 | 之前 | 之后 |
|---|---:|---:|
| Worker（`/proc/1/status` 的 VmRSS） | 220 MiB | **96 MiB** |
| api（Python 进程的 VmRSS） | 没有单独测 | 112 MiB |
| scheduler | — | 237 MiB（仍然加载 SDK，它需要） |

scheduler 保持 healthy，日志里有 `connected to wss://msg-frontier.feishu.cn/...`：
这个开发栈上唯一的长连接绑定真的连上了飞书。

## 这一遍没能证明什么

- **没有通过长连接收发过一条真实消息。** 只看到连接建立，没有在飞书里发消息走一遍。
- **api 改动前的内存没有单独测过。** 只推断它也加载了 SDK（它 import 的是同一个
  `cli.py`），没有测出改动前后的差值。
- 内存数字都是空闲时的常驻内存，没有在负载下测。
- **没有取得 compose-e2e 结果。** 分支没有推送，CI 没有跑。

## 不声称什么

- 不声称 SDK 的 loop 问题只有这一处。`_long_connections` 的 docstring 记录了它的另一个
  限制（每个进程至多一条长连接），这次没有碰。
- 不声称以后在别处 import 这个模块是安全的。任何在运行中的 loop 里第一次 import SDK 的
  代码，都会重现这个问题；现在守住它的只是 `scheduler_main` 里的顺序和那条子进程测试。

## 顺带看到的

SDK 自己的 INFO 日志会把连接 URL 整行打出来，其中带着 `access_key` 和 `ticket`。这是
改动之前就有的行为，这一遍没有改。
