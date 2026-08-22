# 参与开发

先读 `CLAUDE.md`。它写的是这个仓库的实际约定，不是客套话——尤其是「注释解释为什么，
不解释做了什么」和「测试先写、看它红、再实现」。

## 起步

```
docker run -d --name th-test-pg -e POSTGRES_USER=tiny_hermes \
  -e POSTGRES_PASSWORD=local-only -e POSTGRES_DB=tiny_hermes_test \
  -p 127.0.0.1:55432:5432 postgres:16

# 两行，必须分开：export A=... B="$A" 会在赋值前求 $A
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"

uv run --no-sync pytest packages/backend/tests/unit -q
uv run ruff check packages/backend && uv run pyright
```

**一次只跑一个 pytest。** 两个套件抢同一个数据库会互相拖垮，症状是长时间没输出。

完整的环境说明与坑在 `docs/development.md`，运维流程在 `docs/operations.md`。

## 提 PR 之前

- `uv run ruff check packages/backend`、`uv run pyright`、单元与集成测试
- 改了表结构就要有迁移，`uv run alembic check` 会告诉你有没有漏
- **合并前必须有一次绿的 `compose-e2e`**，本地跑过的栈不算数

## 这个项目最在意的一件事

**写进去了不等于有人够得着。** 这个仓库已经出过至少六次同一个形状的缺陷：审批消费者
拿不到 id、导出按钮返回空文件、`audit_events` 只写不读、字段停在服务边界到不了线上。
**每一次后端测试都是全绿的。**

所以判据不是「测试过了」，是「这条路走得通」——最好由一个浏览器或一次真实 HTTP 请求
走一遍。写验收记录时，这两句话必须分开写。

## 提交信息

解释**为什么**这样改，以及**不选另一种做法的理由**。看 `git log` 就知道风格。
