# tiny-hermes — 给 AI 协作者的约定

产品事实来源是 `docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md`（当前
v2.9.2）。实现与它冲突时以它为准，除非你先改它并写明理由。

## 怎么写代码

- **测试先写，跑它，看它红，再实现。** 提交分开：先 test 再 impl，让 git 顺序看得出来。
- **注释和 docstring 解释「为什么」，不解释「做了什么」。** 代码说得清「做了什么」，
  说不清「为什么是这样而不是另一样」。
- **一条注释不得声称代码没有的保护。** 这个项目已经因此出过两次事故：抹除的注释说
  「两个指针都会先清掉」而没有任何代码清它们；`nginx.conf` 的注释说来源白名单顶替了
  `X-Frame-Options`，而它对点击劫持是结构性看不见的。
- **响应模型按主体收窄**，不要一个模型加一堆条件。先例：`19b91e3`、`b97ddb3`、`a463b74`。
- **断言按 id 找行，不要按下标。** 多张表按 `created_at` 排序且无 tiebreaker。
- 已发布 AgentVersion 的**内容哈希不得变化**。可选顶层文档不写就不带那个键。

## 这个项目最常见的 bug

**写进去了不等于有人够得着。** 已经抓到至少五次：审批消费者拿不到 id、导出按钮返回空、
`forgetAllSessionIds` 没有调用方、`audit_events` 只写不读、`failure_reason` 从没渲染过。
**每一次后端测试都是全绿的。**

判据不是「测试过了」，是「这条路走得通」。写验收记录时这两句话必须分开。

## 跑测试

```bash
docker run -d --name th-test-pg -e POSTGRES_USER=tiny_hermes \
  -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=tiny_hermes_test \
  -p 127.0.0.1:55432:5432 postgres:16

# 两行，必须分开写：export A=... B="$A" 会在赋值前求 $A
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"

uv run --no-sync pytest packages/backend/tests/unit -q
uv run --no-sync pytest packages/backend/tests/integration --ignore=packages/backend/tests/integration/sandbox -q
uv run ruff check packages/backend migrations && uv run pyright
pnpm --filter @tiny-hermes/web test && pnpm chat:test
```

`--ignore` 掉 sandbox 是为了快（那一档约 70 秒，而且要一个能连上的 Docker）。
**它现在在 macOS 上跑得起来**——去掉 `--ignore` 是 790 条全绿。以前不行：那 17 条
的 socket 路径是用 `tmp_path` 拼的，pytest 拿测试名当目录名，在 macOS 上超过
`sockaddr_un.sun_path` 的 104 字节，**在 setup 阶段就 ERROR**。这种失败最难看见——
pytest 报的是 error 不是 failure，一整个目录就那么静悄悄地没跑。

**永远只跑一个 pytest。** 两个套件抢同一个数据库会互相拖垮，症状是长时间没输出。
诊断 `select pid,state,now()-state_change from pg_stat_activity where state like '%idle in transaction%'`，
有孤儿就 `pg_terminate_backend`，别等。

macOS 上 `greenlet` 要手动装进 venv（SQLAlchemy 的平台标记覆盖 `aarch64` 而非
macOS 的 `arm64`）——**不要改 `pyproject.toml`**。

Unix socket 的路径不要用 `tmp_path` 拼——用
`tests/integration/sandbox/conftest.py` 的 `socket_dir`。

## 合并前

**必须重新取得 compose-e2e 绿色结果**，本地跑过的栈不算数。

开 PR 会触发第二次 CI，PR 会显示 `UNSTABLE` 直到跑完——**等它**，别看 push 那次的绿。
而且 `success` 不等于真的跑过测试：用
`gh run view <id> --log | grep "^compose-e2e" | grep -E "passed|✘"` 确认。

**要合并的分支：先提交，再推送。** 推送后才提交的东西不会进 PR，分支删掉后就悬空了。

## 文档

- `docs/superpowers/plans/` — 实施计划，含「这份计划替产品做的决定」
- `docs/superpowers/verification/` — 验收记录，**必须有**「这一遍没能证明什么」与
  「不声称什么」两节
- `.superpowers/` 是 gitignored，别把该留下的东西写在那里
