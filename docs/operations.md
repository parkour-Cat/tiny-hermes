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

Postgres、Redis、对象存储也要让 Worker 主机连得上：给它们发布到内网地址，同样只放行
Worker 主机。每路 Worker 并发约占 1.5 条 Postgres 连接，总路数超过 100 时加连接池代理。

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
`sandbox_start_failed`。停之前先确认：

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

- 还没拿到沙箱的 Run：判失败，原因 `sandbox_start_failed`，**不会重试**；
- 已经有沙箱、在时间片边界冻结失败的 Run：进入 `interrupted`，等证书换好由 Scheduler 恢复。

所以证书过期是会让用户的 Run 失败的故障，不是只会变慢。**到期警告出现后就续签**，不要等。

### 7.5 出问题时先看什么

| 现象 | 先查 |
|---|---|
| 进程起不来，报 `SANDBOX_CONTROLLER_CERT_FILE=... is a worker certificate; this process is the scheduler` | 证书发错了角色 |
| 进程起不来，报 `... cannot be read` | 文件没挂进去，或私钥属主不是 10001 |
| Worker 日志里 controller `Unreachable`，controller 日志里有 TLS 握手错误 | CA 不一致、证书过期，或 Worker 拨的名字不在 controller 证书的 `--host` 里 |
| Scheduler 调用被拒，原因 `role_not_permitted` | 它拿着 Scheduler 证书在调 `cleanup` 以外的动作：这是缺陷，请报告 |

## 8. 这份手册没有覆盖什么

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
- **没有测量过规模**：十万条 Secret 的轮换要多久、大库的 `pg_restore` 要多久，都不知道。
