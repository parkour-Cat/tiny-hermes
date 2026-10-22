# 运维手册

> 产品依据：§1134（健康检查、迁移、备份、恢复、升级回滚说明）、§374、§376、
> §27.3 第 6 条。演练脚本在 `scripts/`。

## 1. 健康检查

```
curl -fsS http://localhost:8000/health/live     # 进程活着
curl -fsS http://localhost:8000/health/ready    # 依赖可用（数据库、Redis、对象存储）
```

Compose 的 `--wait` 用的就是这两个。`live` 通过而 `ready` 不通过，说明进程在但依赖没起来——
**不要因此重启进程**，先看依赖。

## 2. 数据库迁移

```
docker compose -f deploy/compose/compose.yaml run --rm migrate
```

或直接：

```
DATABASE_URL=postgresql+asyncpg://... uv run alembic upgrade head
```

`uv run alembic check` 报告 ORM 与迁移是否已经漂移。**这条在 CI 里是绿的门槛**，
本地改了表结构而没写迁移，它会直接说出来。

## 3. 备份

**KEK 绝不能和数据库备份放在一起**（§374）。这不是建议：
`test_a_database_backup_without_the_kek_cannot_be_decrypted` 断言了只有转储就解不开
Secret，而把 KEK 放进同一个备份会亲手取消这条性质。

```
docker exec tiny-hermes-postgres-1 pg_dump -U tiny_hermes -Fc tiny_hermes > backup.dump
```

对象存储（SeaweedFS；2026-09 之前是 MinIO）里的 artifact、skill 包与会话工作区要单独备份，
它们不在数据库里。

**备份要包含 `alembic_version`**（`pg_dump` 默认会带）。恢复时的第一件事就是确认它的值，
因为它决定了这份备份属于哪个版本的代码。

## 4. 恢复

**`--clean` 不是可选的。** 演练里先试过 `--data-only`：它按字母序恢复且外键是活的，
于是 `agents` 先于 `workspaces` 进去、外键直接失败，`alembic_version` 还会撞主键。
`--clean` 先删后建，顺序就不再是个问题。

```
docker exec -i tiny-hermes-postgres-1 pg_restore -U tiny_hermes -d tiny_hermes --clean --if-exists backup.dump
docker exec tiny-hermes-postgres-1 psql -U tiny_hermes -d tiny_hermes -tAc \
  "select version_num from alembic_version"
```

**恢复之后必须把代码版本对齐到 `alembic_version` 报出来的那一版**，而不是反过来把库
升上来。新代码配旧库会在运行时炸；旧库配新代码，`alembic upgrade head` 才是正确动作。

Secret 的可读性取决于 KEK：恢复出来的库如果配的是另一把 KEK，Secret 全部打不开，
而且**这不会在启动时报错**——它会在第一次用到某个 Secret 时报。
`scripts/backup_restore_drill.py` 实测过这一点（`UnwrapFailed`），所以它不是推测：
**一个配错钥匙的部署看起来是健康的**，健康检查会过，直到某件事需要一个 Secret。
恢复后主动验证一条，别等它自己暴露。

### 备份恢复演练

和回滚演练一样，它自建临时库，**不碰任何现有数据库**：

```
docker run -d --name th-drill-pg -e POSTGRES_USER=tiny_hermes \
  -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=postgres \
  -p 127.0.0.1:55433:5432 postgres:16
uv run --no-sync python scripts/backup_restore_drill.py \
  --admin postgresql://tiny_hermes:local-only@127.0.0.1:55433/postgres \
  --container th-drill-pg
```

它走的是这一节的同一套命令，所以**它验证的是这份手册本身**，不是手册的一个变体。

## 5. 升级回滚

**先看这张表，再决定要不要回滚。** 它是 `scripts/upgrade_rollback_drill.py`
在真实 Postgres 上跑出来的，不是读迁移源码推断的：

| 表 | 回滚四步之后 | 再升级回来 |
|---|---|---|
| `channel_bindings` | 表被删 | 表回来了，**行没回来** |
| `channel_events` | 表被删 | 表回来了，**行没回来** |
| `oidc_providers` | 表被删 | 表回来了，**行没回来** |
| `channel_conversations` | 表被删 | 表回来了（演练时本来就是空的） |

**回滚会不可逆地销毁这些行。** 表结构回得来，内容回不来——重新升级得到的是空表，
而不是回滚前的样子。具体后果：

- `oidc_providers` 没了 → **所有 OIDC 登录立刻不可用**，要重新登记提供方（`client_secret_ref`
  指向的 Secret 还在，但登记本身没了）。
- `channel_bindings` 没了 → 飞书渠道停止工作，且 `encrypt_key_ref` 的对应关系要重建。
- `channel_events` 没了 → **去重记录清空**。飞书在重试窗口（最长 6 小时）内重投的事件
  会被当成新事件，**产生重复的 Run**。这是回滚里最容易被忽略的一个后果。

所以顺序是：

```
# 1. 先备份，不是"回滚失败了再说"
docker exec tiny-hermes-postgres-1 pg_dump -U tiny_hermes -Fc tiny_hermes > pre-rollback.dump

# 2. 回滚 schema
DATABASE_URL=... uv run alembic downgrade -4

# 3. 部署旧版本代码
```

自己跑一遍演练（**不会碰生产库，它自建一个临时库**）：

```
docker run -d --name th-drill-pg -e POSTGRES_USER=tiny_hermes \
  -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=postgres \
  -p 127.0.0.1:55433:5432 postgres:16
uv run --no-sync python scripts/upgrade_rollback_drill.py \
  --admin postgresql://tiny_hermes:local-only@127.0.0.1:55433/postgres
```

### 从 MinIO 迁到 SeaweedFS

MinIO 的镜像 2026-09-11 从 Docker Hub 下架，内置对象存储换成了 SeaweedFS
（`deploy/compose/compose.yaml` 的 `seaweedfs` 服务，S3 端口仍是 9000）。
**SeaweedFS 读不了 MinIO 的磁盘格式**：旧部署的对象留在 `tiny-hermes_minio-data`
卷里，升级后不复制过来就访问不到——artifact 打不开、会话工作区恢复不出来。

新的 Compose 文件不再声明那个卷，所以 `docker compose down -v` **不会**删掉它。

```
# 1. 停掉旧栈，不带 -v：卷要留着
docker compose -f deploy/compose/compose.yaml down

# 2. 升级到新代码，只起新的对象存储
docker compose -f deploy/compose/compose.yaml up -d seaweedfs --wait

# 3. 用旧卷临时起一个 MinIO，换到 19000 端口，免得和新服务的 9000 撞上
docker run -d --name th-old-minio -p 127.0.0.1:19000:9000 \
  -v tiny-hermes_minio-data:/data \
  -e MINIO_ROOT_USER=tiny-hermes-local -e MINIO_ROOT_PASSWORD=tiny-hermes-local-password \
  minio/minio:RELEASE.2025-07-23T15-54-02Z server /data

# 4. 复制，并逐个读回核对 SHA-256；中途断了就重跑，已经一致的会跳过
uv run --no-sync python scripts/migrate_object_store.py \
  --source http://127.0.0.1:19000 \
  --source-access-key tiny-hermes-local --source-secret-key tiny-hermes-local-password \
  --target http://127.0.0.1:9000 \
  --target-access-key tiny-hermes-local --target-secret-key tiny-hermes-local-password

# 5. 起其余服务，确认 artifact 能打开之后再收尾
docker compose -f deploy/compose/compose.yaml up -d --wait
docker rm -f th-old-minio
# 旧卷留一段时间，确认无误后再删：
# docker volume rm tiny-hermes_minio-data
```

第 3 步依赖本机**还缓存着**那个 MinIO 镜像（跑过旧栈的机器都有，但只有那台机器的
架构）。Docker Hub 上已经拉不到，quay.io 在 2026-09-25 对匿名访问也不开放了。没有
缓存的机器，这份手册没有给出办法——如果按第 3 节做过对象存储备份，从备份恢复到
SeaweedFS 是另一条路，但那条路没有演练过。

这几步在 2026-09-25 用一次性的卷和容器走过一遍（20 个对象，复制后另写代码从两边
读出比对，一致；重跑一次，20 个全部跳过）。没有在一台真实的旧部署上走过。

## 6. KEK 轮换

见 `docs/superpowers/verification/2026-08-22-kek-rotation.md`。要点：

```
curl -X POST http://localhost:8000/api/v1/secrets/rewrap \
  -H "X-Workspace-Id: ..." -H "X-CSRF-Token: ..."
```

响应里有四个数：`processed`、`remaining`、`unrecoverable`、`unverifiable`。

**旧 KEK 只能在 `remaining`、`unrecoverable`、`unverifiable` 三个都是 0 的时候销毁**
（§376：重包、校验和审计三件事都完成）。

- `unrecoverable > 0`：这些行旧 KEK 都打不开，销毁旧钥匙也不会让情况更糟，但要先弄清楚
  它们是怎么变成这样的。
- `unverifiable > 0`：重包之后打不开，旧包已经被放回去了。**这时销毁旧 KEK 会丢数据。**
  重跑一次；仍然如此就要人来查。

轮换可以中断后重跑，`key_id` 本身就是进度记录，已经轮换过的行不会被重做。

### KEK 销毁演练

```
docker run -d --name th-drill-pg -e POSTGRES_USER=tiny_hermes \
  -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=postgres \
  -p 127.0.0.1:55433:5432 postgres:16
uv run --no-sync python scripts/kek_destruction_drill.py \
  --admin postgresql://tiny_hermes:local-only@127.0.0.1:55433/postgres
```

它自建临时库，**不碰任何现有数据库**，也**不删除磁盘上的任何钥匙**——销毁是用
「服务只持有新钥匙、没有 `previous` 可退」来模拟的，那正是从部署里删掉一把 KEK
之后剩下的样子。

## 7. 多机部署：Worker 跨主机（B 档）

设计见 [B 档设计](superpowers/specs/2026-10-21-tier-b-networked-controller-design.md)。
一台**沙箱主机**跑 controller 和沙箱（以及看规模放在这里的 Postgres、Redis、对象存储、
Scheduler、API、Web）；任意台 **Worker 主机**只跑 Worker，通过双向 TLS 调用 controller。

**这一档只允许一台沙箱主机。**带工具的并发上限还是那一台的上限，见设计 §2。

### 7.1 签发证书

在一台不跑服务的机器上（运维自己的电脑）签发：

```bash
uv run --no-sync python scripts/controller_certs.py --out certs \
    --host sandbox-1.internal --host 10.0.0.5
```

`--host` 写 Worker 主机拨号时用的名字或地址，可以写多个；脚本会自动加上 `controller`，
那是沙箱主机上 Scheduler 拨号用的 Compose 服务名。输出：

| 文件 | 发给谁 |
|---|---|
| `ca.pem` | 每一台主机 |
| `controller.pem`、`controller.key` | 沙箱主机 |
| `scheduler.pem`、`scheduler.key` | 沙箱主机 |
| `worker.pem`、`worker.key` | 每一台 Worker 主机（沙箱主机上若也跑 Worker，也给它） |
| `ca.key` | **哪台主机都不给。**离线保管，只在续签时用 |

**每台主机只放它自己需要的那几个文件。**Compose 文件逐个挂载文件而不是挂整个目录，
就是为了让一个容器拿不到别的角色的私钥；把多余的私钥拷到主机上，这层保护就没了。

Worker 和 Scheduler 容器以 uid 10001 运行，所以它们的私钥要让 10001 读得到：

```bash
sudo chown 10001:10001 worker.key scheduler.key   # 权限保持 0600
```

controller 以 root 运行，`controller.key` 不用改属主。

### 7.2 沙箱主机

```bash
cd deploy/compose
export CONTROLLER_PRIVATE_ADDRESS=10.0.0.5     # 本机在内网上的地址
export CONTROLLER_CERT_DIR=/srv/tiny-hermes/certs
docker compose -f compose.yaml -f compose.sandbox-host.yaml up -d --scale worker=0
```

controller 只在 `CONTROLLER_PRIVATE_ADDRESS:7443` 上对外。**防火墙只放行 Worker 主机
到这个端口。**它握着 Docker socket，等于这台主机的 root。

Postgres、Redis、对象存储也要让 Worker 主机连得上：默认只发布在 127.0.0.1，设
`DATASTORE_BIND_ADDRESS=<内网地址>` 发布到内网地址，同样只放行 Worker 主机。每路 Worker 并发约占 1.5 条 Postgres 连接，总路数超过 100 时加连接池代理。

### 7.3 Worker 主机

```bash
cd deploy/compose
export DATABASE_URL=postgresql+asyncpg://tiny_hermes:...@10.0.0.5:5432/tiny_hermes
export REDIS_URL=redis://10.0.0.5:6379/0
export S3_ENDPOINT=http://10.0.0.5:9000
export SANDBOX_CONTROLLER_URL=tls://sandbox-1.internal:7443
export CONTROLLER_CERT_DIR=/srv/tiny-hermes/certs
# 其余变量（KEK、对象存储密钥、模型密钥、SANDBOX_IMAGE_DIGEST、容量参数）
# 和沙箱主机上取同样的值
docker compose -f compose.worker-host.yaml up -d --scale worker=2
```

少给任何一个地址，Compose 会直接报错，而不是用单机的默认值去连一个不存在的主机。

**停掉 Worker 主机时，`down` 也要同样的变量。**不设的话 `docker compose -f
compose.worker-host.yaml down` 报错退出，**容器照样在跑**，还会继续领 Run。验收时就这样
撞到过：controller 已经不在那个地址上了，这个 Worker 领到的带工具的 Run 全判了
`sandbox_start_failed`（当时的行为；现在这些 Run 会每 30 秒重试，直到 Run 的预算用完）。
停之前先确认：

```bash
docker ps --filter label=com.docker.compose.project=tiny-hermes-worker
```

Worker 自己的出站（模型、飞书等）走 `EGRESS_PROXY_URL`。**这条链路 B 档没有加密**：
代理在沙箱主机上时，Worker 主机经内网连它，令牌是明文传的。只在可信内网上这样部署；
要跨不可信网络，需要先在代理前面加 TLS。

### 7.4 证书到期

证书默认有效 365 天。剩不到 30 天时，每个进程启动时都会在日志里打一条 `controller
certificate expires`。续签：

```bash
uv run --no-sync python scripts/controller_certs.py --out certs \
    --host sandbox-1.internal --host 10.0.0.5 --force
```

同一个 CA 保留不变，只换各角色的证书和私钥。把新文件发到各主机，然后**逐个重启**
controller、Scheduler 和 Worker；进程只在启动时读证书。

**过期之后**，握手失败，Worker 调 controller 一律得到「联系不上」。后果按 Run 走到哪一步
而不同（M1 沙箱设计 §11）：

- 还没拿到沙箱的 Run：进入 `interrupted`，Scheduler 恢复它，再撞上，再恢复，
  **超过 `MAX_RECOVERY_ATTEMPTS`（默认 3）次就判失败**，前后只有几秒；
- 已经有沙箱、在时间片边界冻结失败的 Run：进入 `interrupted`，等证书换好由 Scheduler 恢复。

为什么前一种不是「稍后重试」：Worker 的证书被拒，TLS 1.3 要等请求写出去之后才告诉它，
它分不出这和「controller 做完就挂了」。这和 controller 停机、网络断开不一样，那两种在
连接阶段就失败，Run 会每 30 秒重试。

所以证书过期是会让用户的 Run 失败的故障，不是只会变慢。**到期警告出现后就续签**，不要等。

### 7.5 出问题时先看什么

| 现象 | 先查 |
|---|---|
| 进程起不来，报 `SANDBOX_CONTROLLER_CERT_FILE=... is a worker certificate; this process is the scheduler` | 证书发错了角色 |
| 进程起不来，报 `... cannot be read` | 文件没挂进去，或私钥属主不是 10001 |
| Worker 日志里 controller `Unreachable`，controller 日志里有 TLS 握手错误 | CA 不一致、证书过期，或 Worker 拨的名字不在 controller 证书的 `--host` 里 |
| Scheduler 调用被拒，原因 `role_not_permitted` | 它拿着 Scheduler 证书在调 `cleanup` 以外的动作：这是缺陷，请报告 |

## 8. 一台主机开几个 Worker

带工具和不带工具的 Run 都要占一条 Worker **通道**。通道不够时，新消息要等前面某个 Run
的时间片结束，最长一个时间片（默认 30 秒）。

### 8.1 两个旋钮

| | 是什么 | 代价 |
|---|---|---|
| `WORKER_CONCURRENCY`（K） | 一个进程里跑几个 Worker，共享事件循环和数据库连接池 | 每个进程固定约 96 MiB，每条通道再加约 1 MiB；一个进程最多用满一个核 |
| `--scale worker=N` | 开几个 Worker 进程（容器） | 每个容器一份固定开销，也多一个连接池 |

**通道数 = N × K**，B 档下把所有 Worker 主机的加在一起。

### 8.2 建议值

来自[单机档验收](superpowers/verification/2026-09-30-tier-a-acceptance.md)，
用的是确定性模型（见 8.6 节）：

- **K = 16。**一个进程跑 32 路带工具的 Run 时，用到了一个核的 82%。
- **N = 核数 ÷ 4，至少 1。**8 核给 2 个，就是 32 条通道。
- **通道数要大于同时在推进的长任务数，再加上交互那一份。**交互那一份约等于「每秒到达的
  消息数 × 每条执行的秒数」。验收第 3 档就是反例：32 个长任务占满 32 条通道，短消息排队
  P95 31.6 秒；多开一个容器（48 条）之后回到 0.04 秒。

```bash
WORKER_CONCURRENCY=16 docker compose up -d --scale worker=2
```

### 8.3 数据库连接：按池的上限算，不按平时用到的算

每个进程的连接池都有上限，它们加起来必须不超过 Postgres 的 `max_connections`
（默认 200，用 `POSTGRES_MAX_CONNECTIONS` 调）：

| 进程 | 每个进程的上限 |
|---|---|
| Worker | `max(5, K) + max(10, K)`，K = 16 时 32 |
| API | `DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW`，默认 80 + 40 = 120 |
| Scheduler | 15 |
| controller | 15 |

再给迁移和人工排查留 10 条左右。

**按建议值，单机就已经超了**：2 个 Worker 容器 × 32 + 120 + 15 + 15 = 214，大于 200。
单机档验收里连接数最多只用到 70，因为池平时填不满；但数据库一慢，每个池都会涨到上限，
那时多出来的连接会被拒绝，报 `too many clients`，**偏偏发生在最忙的时候**。所以：

```bash
# 2 × 32 + 120 + 15 + 15 + 10 = 224，取整
POSTGRES_MAX_CONNECTIONS=250 WORKER_CONCURRENCY=16 docker compose up -d --scale worker=2
```

改 `max_connections` 要重启 Postgres 才生效。B 档下每台 Worker 主机的池都连同一个
Postgres，一起算进去。总数到了几百，就该在前面加一个连接池代理（如 PgBouncer），
而不是一直调大 `max_connections`：这个方案没有测过。

### 8.4 沙箱跟着通道变多

每条通道上带工具的 Run 都可能拿着一个运行中的沙箱；被切片、等下一轮的长任务还各拿着一个
冻结的。沙箱内存预算（`SANDBOX_MEMORY_BUDGET_MB`，默认主机内存的一半）放不下时，Run 会
排队等名额，不会失败。所以**加通道之前先算沙箱内存**，见容量方案 §3 A 档的表。
通道多了、沙箱名额没跟上，带工具的 Run 照样要等。

### 8.5 重启和停止

Worker 收到 SIGTERM 后：

1. 不再领新的 Run；
2. 正在跑的那一轮跑完，时间片就此结束，沙箱冻结保留，Run 回到 `queued`，别的 Worker
   可以马上接着跑；
3. 最多等 `WORKER_SHUTDOWN_GRACE_SECONDS`（默认 20 秒）。还有轮没跑完，进程就不写任何东西
   直接退出，那些 Run 等租约过期（20 秒），由 Scheduler 中断再恢复，每次用掉一次恢复机会
   （`MAX_RECOVERY_ATTEMPTS`，默认 3）。

Compose 给 Worker 的 `stop_grace_period` 是 30 秒，**必须大于**
`WORKER_SHUTDOWN_GRACE_SECONDS`。调大后者时一起调大前者，否则 Compose 会先把进程杀掉。

实测（2026-10-21，K = 4，4 个长任务，`docker compose restart worker`）：

| | 重启用时 | 被中断的 Run | 用掉的恢复次数 |
|---|---:|---:|---:|
| 改之前（Compose 10 秒就杀） | 10.6 s | 3 / 4 | 3 |
| 改之后 | 2.4 s | 0 / 4 | 0 |

一轮比宽限期长的时候（工具里执行 `sleep 60`），重启用了 20.7 秒，日志里有
`workers still busy after the shutdown grace period`，那个 Run 被中断、恢复，最后完成。

### 8.6 改完之后怎么验证

上面的数字都来自确定性模型：不走网络，没有流式解析。真实模型每条通道要多少 CPU 还不知道，
所以**换上真实模型以后，用自己的负载重测一遍**：

```bash
uv run --no-sync python scripts/concurrency_probe.py \
  --rate 2 --seconds 150 --long-tasks 16 --rounds 40 --label my-host
```

同时用 `docker stats` 看 Worker 容器。单机档验收用的标准是：短消息排队 P95 在 10 秒以内，
Worker CPU 按 2 秒采样的 P90 低于 70%。超了就加 N（还有核的话）或者加 Worker 主机（B 档），
不要只加 K：一个进程只能用一个核。

## 9. Redis 停了会怎样

Redis 只用来**提前通知**，平台对不对不靠它。它传两样东西：

- **有活可领**（`tiny-hermes:run-wakeup`）：Worker 被叫醒，不用等空闲轮询。
- **Run 有新事件**（`tiny-hermes:run-events`，容量计划第 5 项）：事件提交之后，写事件的
  进程（Worker、Scheduler、API）发一条通知。API 的事件流平时不查库，收到通知才读；
  另外每 5 秒兜底读一次，防止通知丢了。

Redis 停了，或者 API 连不上它：

- 事件流**立刻**改回每 0.5 秒查一次库，用户看到事件最多晚半秒，和以前一样。
  API 日志有一条 `run event push lost; event streams poll`。
- 写事件的进程每次断线只记一条 `run event publish failed; subscribers will poll`，
  不会每次提交都刷一条。
- Redis 回来后 API 自己重连（约 1 秒），不用重启。

代价要知道：**数据库读得多了**。2026-10-22 在本地栈实测，每个有人在看、但没有新事件的
Run，用 Redis 时每秒约 0.3–0.6 个事务，不用 Redis 时约 3.5–3.7 个。几百个人同时开着
Run 详情页时，Redis 停机会让数据库压力明显上升，不要长时间不修。

只有 API 连得上 Redis、而 Worker 连不上（例如 B 档 Worker 主机到 Redis 的那条内网断了）
时，API 以为通知正常，就按 5 秒兜底读：**这时用户看到事件最多晚 5 秒**，不是半秒。
看 Worker 日志里有没有 `run event publish failed`。

## 10. 上线前要换掉的默认值

`compose.yaml` 在变量没设时用一组写在文件里的默认值，本地栈和 CI 靠它们不带 `.env` 就能起来。
这些值所有人都看得到，所以只能在开发机上用。

- **设 `ENVIRONMENT=production`**（任何不是 `development` 的值都行）。这样会话 cookie 才带
  `Secure`；而且下面任何一项还是默认值时，进程拒绝启动，日志里列出是哪几项。
- 要换的：`SESSION_COOKIE_SECRET`、`BOOTSTRAP_TOKEN`、`TINY_HERMES_KEK`
  （`scripts/generate_local_secrets.py` 生成这三个），`S3_ACCESS_KEY`、`S3_SECRET_KEY`、
  `POSTGRES_PASSWORD`。默认 KEK 是 32 个零字节，用它包的 Secret 等于明文。
- `POSTGRES_PASSWORD` 只在数据卷**第一次初始化**时生效。已经有数据的库，先在库里
  `ALTER ROLE tiny_hermes PASSWORD '...'`，再改变量。
- Postgres、Redis、对象存储默认只发布在 127.0.0.1。Redis 没有密码，不要把
  `DATASTORE_BIND_ADDRESS` 设成公网地址。
- 出站代理和 controller 一样只拿基础环境：没有 KEK、对象存储密钥和模型密钥。它是沙箱唯一
  连得到的进程。

**还没解决的：同一台主机上的沙箱之间网络是通的。**它们都在 `sandbox-egress` 这个网桥上，
`internal` 只去掉了出网，没有去掉容器之间的互访。直接关掉网桥的容器间通信会把代理也切断，
所以要换成每个沙箱一个网络之类的做法，还没做。在那之前，一个 Run 能连到同主机上别的 Run
在沙箱里起的端口。

## 11. 这份手册没有覆盖什么

- **没有在真实生产部署上演练过。** 上面的表和步骤都来自本地容器。
- ~~没有做过「真的销毁旧 KEK 之后」的演练。~~ **已补（2026-08-23）**：
  `scripts/kek_destruction_drill.py` 两个方向都跑过——先重包完再销毁，三条全部
  仍可打开；漏掉一条就销毁，那一条**永久打不开**。第二个方向是**演示**出来的，
  不是断言出来的：一条没人看着它失败过的规则，是一条会被绕过去的规则。
- ~~对象存储的备份恢复没有演练。~~ **已补（2026-08-23）**，见第 3 节。
  仍然没有演练过的是**跨主机**恢复：演练里备份桶和主桶在同一个 MinIO 里，
  所以它证明的是「备份能还原」，不是「备份能搬到另一台机器上还原」。
  对象存储换成 SeaweedFS 之后，这个演练**没有重跑**；它只用 S3 接口，但「应当还能
  跑通」不是跑过。
- **B 档没有在两台真实的机器上部署过。**本地用两个互不相通的 Docker 网络模拟，
  测不出真实的网络延迟和带宽。见 B 档的验收记录。
- **第 9 节的数字只在一台机器上量过**，最多 50 个同时在看的 Run；500 个、多个 API
  进程时没有测。所有 API 进程收同一个频道的全部通知，Run 多到一定程度要改成按 Run 分频道。
- **第 8 节的建议值没有在真实模型下测过**，`POSTGRES_MAX_CONNECTIONS` 调大之后也没有
  在连接真的打满的情况下测过。
- **没有测量过规模**：十万条 Secret 的轮换要多久、大库的 `pg_restore` 要多久，都不知道。
