# 并发探针：同时能执行多少个 Run（扩容第 0 步）

分支 `feat/concurrency-probe`，脚本截至 `53065ab`。未推送、未合并。

## 为什么有这一遍

§24.1 测了创建 Run 的吞吐、SSE 连接数和沙箱启动，但没有一格回答「同时能执行多少个
Run」。而默认 Compose 只起一个 `worker` 容器，`WorkerRuntime.run_once` 一次只领一个
Run、跑一个时间片（`runs/application/worker.py:385`）。按代码推演，整个平台同一时刻
只执行一个 Run——这一遍把推演变成实测。

`scripts/concurrency_probe.py` **不是门槛**：它只输出数字，没有阈值。定阈值是设计变更
（§24.1 末段），要在有了这些数字之后才谈得上。

## 测试过了

| 套件 | 结果 |
|---|---|
| `tests/unit/scripts/test_concurrency_probe.py` | 12 passed（先提交测试 `f18e370` 并看它红，再提交实现） |
| `ruff check` / `ruff format --check`（两个新文件） | 通过 |
| `pyright`（两个新文件） | 0 errors |

单元测试只证明算术：排队等待取 `started_at − created_at`；从未开始的 Run 按
「至少等了这么久」计入而不是被丢掉；同一时刻一个结束一个开始算串行。

## 走通了什么

对一套真实运行的栈，经公开 API 登录、建工作空间、发布 Agent、开 Session、提交 Run，
从 Postgres 读 Run 行的时间戳，从 Docker 读 Worker 内存。三次正式运行和一次冒烟运行
均 `exit 0`，360 个 Run 全部 `completed`。

### 环境

| 项 | 实际 |
|---|---|
| 主机 | macOS，OrbStack 虚拟机 8 vCPU / 7.8 GiB（**不是** §24.1 参考环境） |
| 栈 | 本开发机的 Compose 栈，用 `deploy/compose/redeploy.sh` 重建到 `main` 的 `93e1302` |
| 代码核对 | `redeploy.sh` 打出的 `git=unknown`（本地构建不写 SHA）不够用；改为核对最新新增的后端文件 `runs/domain/skill_review.py`（`d1386b2`）在 api、worker、scheduler、controller 四个容器里都存在 |
| 模型 | 确定性替身，场景 `complete`：一次模型调用、无工具；`DETERMINISTIC_MODEL_DELAY_MS=3000` |
| 负载 | 开环到达，每秒 2 个 Run，持续 60 秒，共 120 个；每个 Run 一个独立 Session |
| 其他 | `.env` 里 `WORKER_LEASE_SECONDS=20`（非默认）；同机还跑着 web、chat-web 等服务 |

### 结果

| Worker 数 | 吞吐 | 排队等待 P50 / P95 / 最大 | 执行时间 P50 | 同时执行 峰值 / 平均 | 最大排队 | 每个 Worker 内存 | 数据库连接 |
|---:|---:|---|---:|---|---:|---|---:|
| 1 | 0.33/s | 153.4 / 291.1 / 306.3 s | 3.05 s | 1 / 0.99 | 100 | 241 MiB | 8 |
| 4 | 1.27/s | 15.7 / 30.2 / 31.5 s | 3.05 s | 4 / 3.85 | 42 | 217 MiB | 11 |
| 8 | 1.89/s | 0.1 / 0.5 / 0.6 s | 3.04 s | 8 / 5.74 | 2 | 217–239 MiB（合计 1773） | 14 |

三次都是 120/120 完成、0 失败；创建 Run 的 P95 在 70–76 ms，0 错误；每个 Worker 的
CPU 峰值 ≤ 7.6%。

读法：

1. **默认部署同一时刻只执行 1 个 Run。** 吞吐 0.33/s 正好是 1 ÷ 3.05 s。到达率 2/s
   时队列线性增长，最后一个 Run 等了 306 秒。
2. **吞吐随 Worker 数线性增长。** 4 个 Worker 1.27/s，理论值 4 ÷ 3.05 = 1.31/s；8 个时
   容量 2.62/s 超过到达率，排队基本消失。8 个以内没看到别的瓶颈：CPU 很低，数据库连接
   每多一个 Worker 约多 1 条，创建 Run 的延迟不变。
3. **利特尔法则对得上。** 8 个 Worker 时平均同时执行 5.74 个，预测值 λ × W = 2 × 3.05 =
   6.1（观测窗口包含收尾阶段，平均值因此偏低）。
4. **并发的成本是内存。** 每个 Worker 进程约 217–241 MiB，而 CPU 峰值 ≤ 7.6%，说明它们
   几乎都在等模型。并发数 = 进程数，所以并发 = 内存。
5. 平台自身每个 Run 的开销约 50 ms（执行时间 3.05 s 减去 3 s 模型延迟）。
6. 空闲 Worker 领取也不是立刻的：8 个 Worker 时仍有 P95 0.5 s 的排队；冒烟运行（1 个
   Worker、50 ms 延迟、3 个 Run）最长也等了 1.0 s。**没有查原因。**

### 推算（不是实测）

按每个 Worker 约 230 MiB 线性外推：要同时执行 120 个 Run，需要约 120 个 Worker 进程，
仅 Worker 就约 27 GiB 内存，这台 7.8 GiB 的虚拟机装不下。数据库连接按每个 Worker 约
1 条外推，Worker 到 190 个左右会碰到 `max_connections=200`。两条都是从 1/4/8 三个点
外推出来的。

## 这一遍没能证明什么

- **没有用真实模型。** 替身不走网络、不产生 token、不做流式解析。真实模型调用下每个 Run
  的 CPU 会更高，「每个 Worker 只占 7% CPU」不能外推到真实负载。
- **没有工具，也没有沙箱。** 每个 Run 只有一次模型调用。「沙箱只能开在单台 Docker
  主机上」这个瓶颈，这一遍完全没有碰到。
- **没有 SSE 订阅者。** 「每个有人在看的 Run 每 0.5 秒查一次库」的成本没有测。
- **只测到 8 个 Worker，没有找到拐点。** 数据库、Redis 唤醒、领取查询在更多 Worker 下
  何时成为瓶颈，不知道。
- 每个配置只跑了一次，没有重复。环境不是 §24.1 的参考环境，同机还有别的服务。
- 没有测多 Worker 下的崩溃恢复是否仍然成立。

## 不声称什么

- 不声称项目支持任何具体数量的并发用户。
- 不声称「加 Worker 就能线性扩到 N 个」。线性只在 1、4、8 三个点上成立。
- 不声称这是一个门槛，或通过了什么门槛。这个脚本没有阈值。
- 不声称上面的内存和连接数推算准确。

## 这一遍顺带做的事与踩到的坑

- **开发机栈从 MinIO 迁到了 SeaweedFS。** 重建到 `main` 时对象存储换了实现，旧 MinIO
  容器占着 9000 端口，应用容器起不来。按 `docs/operations.md` 的「从 MinIO 迁到
  SeaweedFS」操作：两个桶共 79 + 4311 个对象被复制，并逐个读回核对 SHA-256；重跑一遍，
  全部跳过。这是那份手册第一次在真实的旧部署上走。
  - 坑：手册第 1 步是先 `down` 旧栈，而这一遍是先跑了 `redeploy.sh`。SeaweedFS 容器
    在端口冲突时被创建，`HostConfig.PortBindings` 里有 9000，`NetworkSettings.Ports`
    却是空的；之后单独 `up` 它也不会重新绑定端口，要 `--force-recreate` 才正常。
  - 旧卷 `tiny-hermes_minio-data` 和已停止的 `tiny-hermes-minio-1` 容器都保留，没有删。
- **本机的 `127.0.0.1:5432` 被 Homebrew 装的 Postgres 占着。** OrbStack 映射的 5432
  从回环地址上访问不到（报 `role "tiny_hermes" does not exist`）；直连容器 IP 超时，
  推测是代理的 TUN 截住了那个网段。改用本机局域网 IP，经
  `TINY_HERMES_BENCHMARK_DATABASE` 传给脚本。
- 登录用的 bootstrap token 来自 `.env` 的 `BOOTSTRAP_TOKEN`，经
  `TINY_HERMES_E2E_BOOTSTRAP_TOKEN` 传入，没有打印。
- 测完已恢复成 1 个 Worker、50 ms 模型延迟。库里留下了名为 `probe-*` 的工作空间和 363
  个测试 Run（三次正式运行加冒烟运行）。

## 复现

```bash
cd deploy/compose
DETERMINISTIC_MODEL_DELAY_MS=3000 docker compose --env-file ../../.env \
  up -d --no-build --no-deps --scale worker=4 --wait worker
cd ../..
TINY_HERMES_BENCHMARK_DATABASE="postgresql://tiny_hermes:local-only@<能到达 Compose 5432 的地址>:5432/tiny_hermes" \
TINY_HERMES_E2E_BOOTSTRAP_TOKEN="<栈的 BOOTSTRAP_TOKEN>" \
  uv run --no-sync python scripts/concurrency_probe.py --rate 2 --seconds 60 --label workers-4
```
