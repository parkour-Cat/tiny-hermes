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

被中断的那一轮里如果有一个调用正发往沙箱外面（`http.*`、`mcp.*`、配了网络的 `shell.exec`），
恢复时不会让模型重想一遍，而是把那一轮写进会话记录，那个调用的结果写成「中断，结果未知」，
由模型自己去查（设计 §17.2，v2.17）。对方有没有收到，平台不知道，对话里能看到模型怎么处理的。
这种 Run 的恢复事件（`run_recovery_approved`；预算或恢复次数用完时是 `run_recovery_failed`）
带 `interrupted_call_id`。工作区版本对不上（这一轮的
改动可能已经提交）的 Run 不写这段记录，照旧失败。

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

### 8.7 沙箱主机上残留的容器和卷

controller 启动时清扫一遍，之后每 `SANDBOX_SWEEP_SECONDS`（默认 300 秒）清扫一遍：

- 带 `tiny-hermes.run` 标签、但数据库里没有一条未销毁的实例记录指向它的容器，删掉。
  这些通常是 controller 在 `docker run` 之后、记录提交之前崩溃留下的；
- 带同样标签的数据卷，它的 Run 既没有占用记录也没有租约，删掉。还被挂载的卷留着，记一条
  `sandbox volume left in place`。

删掉的东西写进审计（`sandbox.sweep`），日志里有 `sandbox host swept containers=N volumes=M`。
平时 N、M 应该是 0；经常不是 0，说明 controller 在频繁崩溃或者提交失败，先查那个。

清扫在准入锁下进行，所以清扫期间新沙箱的创建会排队，一般在一秒以内。

另外，同一个 Run 下一次建新实例时，会先删掉它自己留下的容器和卷，新实例总是从空卷开始恢复。

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
- 起了 `observability` profile 的话：设 `GRAFANA_ADMIN_PASSWORD`（默认 `admin`），设
  `METRICS_TOKEN`（不设时出站代理不提供指标）。见第 12 节。

**沙箱之间的网络隔离（v2.21 起）：**每个沙箱一个 `internal` 网桥，上面只有它和出站代理
（设计：[每个沙箱一个网络](superpowers/specs/2026-10-23-sandbox-networks-design.md)）。之前
所有沙箱共用一个 `sandbox-egress` 网桥，一个 Run 能连到同主机上别的 Run 在沙箱里起的端口。

- 要联网的部署设 `SANDBOX_NETWORK_POOL`（比如 `10.240.0.0/16`），不能和主机的网络、Docker
  默认的 172.17–31.x、192.168.x 重叠。每个沙箱从里面取一个 /28，`/16` 能分 4096 个。
- `SANDBOX_EGRESS_PROXY_CONTAINER` 是出站代理的容器名，Compose 默认 `tiny-hermes-egress-proxy-1`；
  改了 Compose 项目名就要跟着改。
- 还设着旧的 `SANDBOX_EGRESS_NETWORK`、没设新变量的部署，controller 启动时记一条警告，沙箱
  不联网。这是有意的：不悄悄退回共享网桥。
- 删不掉的沙箱网桥由 controller 的主机巡检按标签收走。手工清理：
  `docker network ls --filter label=tiny-hermes.run`。

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

## 12. 监控：指标与告警

设计见[指标与链路追踪](superpowers/specs/2026-10-22-observability-design.md)。

### 12.1 起监控栈

```bash
cd deploy/compose
# .env 里先设 METRICS_TOKEN（.env.example 有生成命令）
docker compose --env-file ../../.env --profile observability up -d --wait
```

| 服务 | 地址 | 说明 |
|---|---|---|
| Grafana | http://127.0.0.1:3002 | 用户 `admin`，密码 `GRAFANA_ADMIN_PASSWORD`；预置仪表盘在 tiny-hermes 文件夹 |
| Prometheus | http://127.0.0.1:9090 | Status → Targets 看抓取是否都是 up |
| Jaeger | http://127.0.0.1:16686 | 链路追踪，见 12.4 |

不加 `--profile observability` 时这些服务都不启动，平台本身照常运行。

### 12.2 指标端口

- api、worker、scheduler、controller、egress-proxy 各自在 **9464** 端口提供 `/metrics`。
  Compose 不把它发布到宿主机；对外的 8000 端口**没有** `/metrics`。
- 设了 `METRICS_TOKEN` 时，端口只回应 `Authorization: Bearer <token>`。Prometheus 从这个
  变量读 token。
- **出站代理没有 token 就不提供指标**，日志里有一条 `metrics not served: METRICS_TOKEN is
  unset`。原因：沙箱连得到出站代理监听的每个端口，指标里有全平台的出站放行和拒绝计数。
- 端口被占用时进程照常运行，只记一条 `metrics not served`，Prometheus 里这个目标显示 down。
- `--scale worker=M` 之后不用改配置：Prometheus 按服务名解析出全部 M 个副本。
- B 档（Worker 在别的机器上）：Compose 里的 Prometheus 只抓得到本机的服务。别的机器上的
  Worker 要在企业自己的 Prometheus 里加目标，并在防火墙里只对它开放 9464。

### 12.3 先看什么

| 现象 | 看哪个面板 | 通常是 |
|---|---|---|
| 用户说回复慢 | 「最久的等待」「Worker 通道」 | 通道满了：加 Worker 或调 `WORKER_CONCURRENCY`（第 8 节） |
| 排队不多但一轮很慢 | 「模型调用 P95」「工具调用 P95」「工作区提交 P95」 | 哪一段高就查哪一段 |
| 带工具的 Run 卡在排队 | 「准入拒绝」「沙箱内存」 | 沙箱内存预算满了（第 8.4 节） |
| 恢复、审批过期、清理不动了 | 「每项扫描距上次成功」 | 某一项扫描每轮都失败：日志里搜 `scheduler scan failed scan=<名字>` |
| 出站拒绝突然变多 | 「出口放行与拒绝」 | 按 reason 看；具体主机在出站代理日志里 |

告警规则在 `deploy/compose/observability/alerts.yml`，只有规则，没有通知渠道。接企业自己的
Alertmanager。阈值都是初值，要在试点之后定。

### 12.4 指标里没有什么

- **没有工作空间、用户、Run 的 id，也没有工具名、模型端点名、出站目标主机。**这些标签的取值
  没有上限，每多一个值就多一组时间序列。要查某一个 Run，看它的事件、日志和链路追踪。
- 模型调用按提供方类型（`deterministic`、`openai_compatible`）分，不按端点分。

## 13. 登录限流

规则在产品设计 §23 第 16 条（v2.19）。

- **按账号**（API 里）：同一账号 15 分钟内本地密码登录失败 5 次，之后的登录返回 429
  `login_throttled`，带 `Retry-After`，**密码对了也不放行**，直到最早那次失败移出 15 分钟窗口。
  不存在的账号同样计数。登录成功清零。OIDC 登录不受影响。
- **按来源地址**（控制台的 nginx 里）：同一地址对登录接口每分钟 10 次，可以突发 10 次，超过的
  直接由 nginx 返回 429，不到 API。**直接访问 API 的 8000 端口不经过这一层**，所以生产环境不要
  把 8000 暴露给浏览器以外的网络，或者在你自己的网关上做同样的限制。
- 指标：`tiny_hermes_login_attempts_total{outcome="throttled"}`；告警规则 `LoginFailuresSpiking`。
  审计：`identity.login_throttled`。

**有人被锁在外面了，等不了 15 分钟：**现在没有管理员解锁的界面。失败记录按账号名规范化后
（去掉首尾空白、转小写）的 SHA-256 存储，在数据库里删掉就解锁：

```bash
docker compose exec postgres psql -U tiny_hermes -d tiny_hermes -c \
  "DELETE FROM login_failures WHERE subject_key = encode(sha256(convert_to(lower(trim('someone@example.com')), 'UTF8')), 'hex')"
```

知道账号名的人可以故意输错，让这个账号的密码登录 15 分钟内用不了。这是按账号限流的代价。
如果某个账号反复被这样针对，查审计里的 `identity.login_throttled`，在网关上按来源地址封掉。

## 14. 数据库行级安全

规则在产品设计 §9 第 3 条、§23 第 17 条（v2.20）。

- 迁移 `20261023_0075` 创建角色 `tiny_hermes_app`（不能登录、不是超级用户、没有 BYPASSRLS、
  不拥有任何表），给它增删改查权限，并在 31 张带 `workspace_id` 的表上开行级安全。以后的迁移
  建的表自动授权，但**要自己写策略**；`test_row_level_security` 在缺策略时会失败。
- 应用的每个连接打开后执行 `SET ROLE tiny_hermes_app`（`DATABASE_ROLE`，默认就是它）。
  Compose 用的登录 `tiny_hermes` 是超级用户，行级安全对它不生效，所以必须降下来。迁移仍然用
  `tiny_hermes` 跑。
- **生产环境更好的做法**：给 `tiny_hermes_app` 一个密码和 `LOGIN`，应用直接用它登录，
  `DATABASE_ROLE` 设为空。这样即使有 SQL 注入执行了 `RESET ROLE`，也回不到超级用户。
  ```sql
  ALTER ROLE tiny_hermes_app WITH LOGIN PASSWORD '...';
  ```
  然后 `DATABASE_URL` 换成这个角色，迁移任务单独保留所有者的连接串。
- 核对：每个进程连上之后 `SELECT current_user` 应该是 `tiny_hermes_app`。

**它管什么、不管什么：**API 在请求确定工作空间之后开始的事务受限，代码漏了工作空间条件时由
数据库兜底。Worker、Scheduler、controller、出站代理、登录、平台管理的事务不受限，隔离仍然
只靠代码。

## 15. 领取顺序与并发上限

规则在产品设计 §1.21 第 2、3 条（v2.21），设计：[领取顺序与并发上限](superpowers/specs/2026-10-23-claim-limits-design.md)。

- **交互的先领。**同步 Chat Completions，以及终端用户从 Web Chat、飞书等渠道发起的 Run，排在
  控制台和异步 API 的 Run 前面；委派出去的子 Run 不算交互。后台 Run 排队超过
  `RUN_INTERACTIVE_PROMOTION_SECONDS`（默认 120）后按交互排，不会被一直挤在后面。
- **每人同时执行的上限**：`RUN_LIMIT_PER_CALLER`（默认 3），工作空间可以单独设
  `workspaces.max_running_runs_per_caller`。只管用户和终端用户，服务账号（API Key）不受它约束。
- **工作空间同时执行的上限**：`workspaces.max_running_runs`，默认不限。
- 只算正在执行时间片的 Run；等审批、等外部事件、暂停、排队的不算。超出上限的 Run 排队等待，
  不报错，状态仍是「排队中」。
- 工作空间管理员在控制台「设置 → 运行上限」里改，或者调接口
  `PUT /api/v1/workspaces/{id}/limits`。留空表示用平台默认值。

### 15.1 长任务的上限（v2.21 §1.21 第 1 条）

同一个页面上还有两项：单个任务最长执行时间、最多调用模型次数。它们是这个工作空间里的 Agent
**最多能申请到多少**：

| 设置 | 不设时 | 最多 | 平台变量 |
|---|---|---|---|
| 单个任务最长执行时间 | 900 秒 | 7200 秒 | `AGENT_MAX_EXECUTION_SECONDS`、`AGENT_CEILING_EXECUTION_SECONDS` |
| 单个任务最多调用模型次数 | 20 次 | 200 次 | `AGENT_MAX_MODEL_CALLS`、`AGENT_CEILING_MODEL_CALLS` |

- 调高之后，Agent 作者在 Agent 编辑页里把自己的上限改大，保存、发布才生效。保存和发布时按
  工作空间的上限检查，超了会说出两个数字。
- 调低不影响已经发布的版本：它们照常运行，下次保存或发布时才按新上限检查。

- 注意：排队数和「最久的等待」指标会把因为上限而等待的 Run 也算进去。某人一次提交很多任务时，
  这两个数会变大，这是上限在起作用，不一定是容量不够。

