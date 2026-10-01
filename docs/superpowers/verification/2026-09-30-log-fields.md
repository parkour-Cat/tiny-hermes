# 日志里的 `extra=` 字段，以及 api 进程的 INFO 日志

日期 2026-09-30。分支 `fix/log-fields`，未推送。测试 `c5b6acd`，修复 `8fc9554`。

## 为什么有这一遍

修沙箱准入时发现：controller 启动时记下了预算，Worker 记下了拒绝原因和 run_id，日志里却都看
不到。查下去发现两个问题：

1. **所有进程的日志格式都只有消息本身**（`%(message)s`），代码里 63 处 `extra=` 传的字段全被
   丢掉了。规格 §21.5 要求「结构化日志」；项目虽然配置了 structlog 的 JSON 输出，但应用代码
   用的都是标准库 `logging`，根本不走那条路径。
2. **api 进程根本没有配置日志。**它直接启动 uvicorn，而 uvicorn 只配置它自己的日志器。
   在 api 容器里按 uvicorn 的日志配置复现：`logger.info(...)` 整条消失，`logger.warning(...)`
   打出来了，但 `run_id` 丢了。

## 改了什么

- `FieldsFormatter`：在消息后面把每个 `extra=` 字段写成 `key=value`，全部在同一行。
  - 带空格、引号或 `=` 的字符串，用 JSON 加引号；
  - 列表、字典用紧凑的 JSON；
  - 异常堆栈照常打印在下面几行。
- `configure_logging` 把它装到根日志器上，级别设为 INFO。不管调用几次都只装一次。原来用的
  `basicConfig` 在根日志器已经有处理器时什么都不做，在 pytest 里形同虚设。
- api 的入口在启动 uvicorn 之前调用 `configure_logging()`。uvicorn 的日志配置不碰根日志器
  （`disable_existing_loggers` 为 False，也没有 `root` 这一项），所以两者不冲突。
- 单元测试新增一个自动生效的夹具：每条测试结束后，把根日志器的处理器和级别恢复原样。原有的
  `cli.main()` 测试现在会配置日志，不恢复的话会留下一个指向已关闭输出流的处理器，影响后面的
  测试。

## 审查过 `extra=` 里有什么

字段一直打印不出来，就没人检查过里面放了什么；一旦显示出来，就可能把密钥写进日志。所以用
AST 把每一处 `extra=` 的键和取值表达式都列了出来：
- 大部分是 `run_id`；
- 其余是各种 id（`secret_id` 是密钥记录的 id，`key_id` 是 KEK 的版本号）、计数、配置数值、
  对象存储的对象路径、拒绝原因代码。
- 有一处 `error` 是数据库异常的文本，可能带有 SQL 语句和参数（id、哈希、大小这类值），只在
  罕见的路径上出现。
- **没有密钥、令牌这类敏感值。**

## 测试过了

- 新增 7 条单元测试：
  - 字段追加在消息后面；需要时加引号；没有字段时输出不变；堆栈仍在下一行；
  - 配置之后 INFO 能打出来、带字段；配置两次不会重复输出；
  - api 在 uvicorn 启动之前就配置好了日志。
- 单元测试 2507 条全部通过；全量 pyright 0 errors；ruff 通过。
- 用到 `caplog` 的 42 条集成测试全部通过（它们断言的是日志记录的属性，不受输出格式影响）。

## 走通了什么

重建栈后：
- controller 的启动日志：`sandbox controller started socket=/run/tiny-hermes/controller.sock
  sandbox_memory_mb=1024 sandbox_memory_budget_mb=4002`；
- Worker 的启动日志：`worker started worker_ids=["4ebf610a1b9a-3ccd9c94"]`；
- api 进程：
  - 在 api 容器里，按 `main()` 的顺序（先 `configure_logging()`、再加载 uvicorn 的日志配置）
    复现，INFO 和 WARNING 都打出来了，也都带着 `run_id`；
  - 正在运行的 api 日志里，出现了启动时 alembic 打的 INFO 行，以前这些是被丢掉的。

## 这一遍没能证明什么

- api 里真正由应用代码打的 INFO 日志，这一遍没有在正在运行的进程里看到：api 这一侧几乎不打
  INFO，只能用复现和第三方库的 INFO 行作为间接证据。
- api 启动时多了 7 行 alembic 的插件加载日志，是打开 INFO 之后带出来的噪音，没有处理。
- 没有改成每行一个 JSON 对象。`key=value` 能 grep，人读也方便；如果以后要接日志收集系统，
  可能还要改。

## 不声称什么

- 不声称以后新增的 `extra=` 里不会出现敏感值。这一遍只审查了现有的 63 处。
