# 每轮少 5 次 Docker 调用：正确，但端到端看不出来

分支 `perf/fewer-docker-calls`，建在 `perf/round-overhead-investigation` 之上。未推送、未合并。

## 做了什么

`DockerEngine` 里有 8 个方法：`execute`、`execute_streamed`、`import_tree`、
`export_tree`、`scan_tree`、`pause`、`unpause`、`remove`。它们以前都先用
`containers.get` 取一次容器，也就是一次 `inspect_container`，而容器 id 本来就是传进来的。
docker SDK 的 `Container.pause()` 这些方法，本身就是拿 `self.id` 去调 `client.api` 的
同名方法，所以现在直接调 `client.api`，是同一个请求，只是少了前面那次 inspect。

`address_of` 保留了 inspect，因为它要读的就是 inspect 返回的网络信息。

## 测试过了

- 新测试 `test_a_round_does_not_inspect_the_container_before_each_call`，在真实 Docker
  上跑：依次执行工具、流式执行、冻结、扫描、导出、解冻，并统计 `inspect_container`
  的调用次数。改动前是 6 次，这条红（`6d59180`）；改动后是 0 次。
- `tests/integration/sandbox` 全部 90 条通过（真实 Docker，69 秒）。
- 后端单元全部 2469 条通过；`ruff` 和 `pyright` 通过。

## 端到端测量

条件：N=32，每个 10 轮，每轮模型延迟 1 秒，`WORKER_CONCURRENCY=32`。先用旧 controller 跑
两次，只重建 controller，再跑两次；在容器里核对过，重建后 `containers.get` 从 9 处变成 2 处。

| | 整轮 P50 | 工具 P50 | 其余 P50 |
|---|---|---|---|
| 改动前，第 1、2 次 | 2605、2818 ms | 259、360 ms | 1216、1388 ms |
| 改动后，第 1、2 次 | 2551、2973 ms | 260、318 ms | 1217、1651 ms |

**看不出可测量的改善。** 这和事先的预测一致：[逐项计时](2026-09-29-round-overhead.md)
里，32 路时一次 inspect 的 P50 约 7 ms，一轮 5 次合计约 35 ms，只占一轮约 2.7 秒的
1.3%；而同样条件下，两次运行之间就会差 200 ms 左右。

## 为什么还是保留这个改动

它让每轮对 Docker 守护进程的调用从约 12 次降到约 7 次，也少占 controller 线程池里的线程，
行为不变，90 条真实 Docker 测试都通过。只是它的收益小于这台机器上的测量噪声。**不声称
它让轮次变快了。**

## 这一遍没能证明什么

- 在更高并发下（比如 64 路，或者原生 Linux 上 inspect 并不便宜的时候），少掉的这些调用
  会不会显出差别，没有测。
- 每边只跑了两次，不足以测出小于约 10% 的差别。

## 下一步真正值得做的（按实测开销）

32 路时，一轮里 Docker 上的时间合计约 440 ms：

1. **每轮冻结再解冻**：`pause` 98 ms 加 `unpause` 76 ms，共约 174 ms，占四成。冻结是
   扫描和导出的前提（controller 的 `_require_frozen`），用来保证快照一致：工具如果在后台
   留下了还在写文件的进程，不冻结就会导出一个写到一半的状态。要省掉它，就得先证明
   「这一刻沙箱里没有我们之外的进程」，这会改动一条安全不变量，需要先改规格。
2. **扫描和导出各打一次整个目录的 tar**：各约 36–40 ms，合成一次能省一次。
3. 更大的一块不在 Docker 上：一次工作区提交里，数据库和对象存储还有约 10 次串行往返，
   32 路时合计约 400 ms。
