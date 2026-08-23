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

对象存储（MinIO）里的 artifact 与 skill 包要单独备份，它们不在数据库里。

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

## 7. 这份手册没有覆盖什么

- **没有在真实生产部署上演练过。** 上面的表和步骤都来自本地容器。
- ~~没有做过「真的销毁旧 KEK 之后」的演练。~~ **已补（2026-08-23）**：
  `scripts/kek_destruction_drill.py` 两个方向都跑过——先重包完再销毁，三条全部
  仍可打开；漏掉一条就销毁，那一条**永久打不开**。第二个方向是**演示**出来的，
  不是断言出来的：一条没人看着它失败过的规则，是一条会被绕过去的规则。
- ~~对象存储的备份恢复没有演练。~~ **已补（2026-08-23）**，见第 3 节。
  仍然没有演练过的是**跨主机**恢复：演练里备份桶和主桶在同一个 MinIO 里，
  所以它证明的是「备份能还原」，不是「备份能搬到另一台机器上还原」。
- **没有测量过规模**：十万条 Secret 的轮换要多久、大库的 `pg_restore` 要多久，都不知道。
