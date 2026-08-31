# 飞书长连接接入 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 飞书事件可以经 WebSocket 长连接进入平台，不再要求公网入站地址；Webhook 保留为并存的另一种模式。

**Architecture:** 把 `FeishuWebhookService.accept()` 拆成「验签解密」与「归一化认领」两段，两种 transport 共用后半段——去重只有一份。长连接适配器住在 scheduler 进程里，与既有的 `run_forever` 并排跑、共用同一个 `stop`。

**Tech Stack:** Python 3.12、飞书官方 `lark-oapi` SDK、SQLAlchemy 2 async、Alembic、pytest。

## Global Constraints

- 设计事实来源：`docs/superpowers/specs/2026-08-31-feishu-long-connection-design.md`。产品事实来源：`docs/superpowers/specs/2026-08-09-tiny-hermes-product-design.md` v2.9.2 §19.2、§929。
- **测试先写，跑它，看它红，再实现。提交分开：先 test 再 impl。**
- 注释和 docstring 解释「为什么」，不解释「做了什么」。
- **一条注释不得声称代码没有的保护。**
- **断言按 id 找行，不要按下标。**
- **本计划是少数需要修改 `pyproject.toml` 的工作之一。** CLAUDE.md 那条「不要改
  `pyproject.toml`」针对的是 macOS 上 greenlet 的平台标记问题——不要为绕过平台标记去改
  它。**新增一个真实的生产依赖不是那种情况**，Task 3 必须改。除此之外不要碰这个文件。
- 迁移 head 当前是 `20260830_0051`。
- 跑测试前两行必须分开写：
  ```
  export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
  export DATABASE_URL="$TEST_DATABASE_URL"
  ```
- **永远只跑一个 pytest。**
- 部署用 `deploy/compose/redeploy.sh`，它会逐个容器验证代码真的换了。

---

### Task 1: 把 accept() 拆成两段

**Files:**
- Modify: `packages/backend/src/tiny_hermes/channels/application/webhook_service.py`
- Test: `packages/backend/tests/unit/channels/test_accept_verified.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  ```python
  # FeishuWebhookService 新增公开方法
  async def accept_verified(
      self, *, binding_id: UUID, envelope: dict[str, Any]
  ) -> Claimed | Unreadable
  ```
  归一化 + 认领，不做验签也不做解密。`accept()` 在解密之后调用它。

**拆分点已经定位好：** `accept()` 里 `event_from_envelope(envelope)` 那一行及其之后（含
`Unreadable` 分支与两处 `claim_delivery`）属于后半段；它上面的签名检查与
`decrypt_payload` 属于 Webhook 独有。

**签名里没有 `encrypt_key`，这是有意的。** `BindingSecrets` 目前是
`(binding_id, encrypt_key)`，而后半段一个字节都不解密。把 `encrypt_key` 传进一个不需要
它的函数，会让下一个人以为那里还会解密。所以新方法只收 `binding_id`。

**这一任务成功的判据是：Webhook 现有的全部测试一条不改地继续通过。** 任何一条需要修改
的既有测试，都说明拆的位置不对——先回头看拆点，不要改测试。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/channels/test_accept_verified.py
"""已经验过签、解过密的事件，从这里进来。

长连接的帧由飞书 SDK 验签与解密，所以它不能走 `accept()` 的前半段；两种
transport 共用的是后半段——归一化与认领。去重就在认领里，只有这一份。
"""

from uuid import uuid4

import pytest

from tiny_hermes.channels.application.webhook_service import (
    Claimed,
    FeishuWebhookService,
    Unreadable,
)


async def test_a_verified_event_is_normalized_and_claimed(claims_spy) -> None:
    service = FeishuWebhookService(claims_spy)
    binding = uuid4()

    answer = await service.accept_verified(
        binding_id=binding, envelope=_text_message_envelope("hello")
    )

    assert isinstance(answer, Claimed)
    assert answer.event.text == "hello"
    assert claims_spy.claimed == [(binding, answer.event.channel_event_id)]


async def test_an_unreadable_event_still_gets_claimed(claims_spy) -> None:
    # 认领是去重，不是「这条能不能处理」。一条读不懂的消息也必须占住它的
    # `channel_event_id`，否则飞书重投时会被当成新消息再走一遍。
    service = FeishuWebhookService(claims_spy)

    answer = await service.accept_verified(
        binding_id=uuid4(), envelope=_unsupported_envelope()
    )

    assert isinstance(answer, Unreadable)
    assert len(claims_spy.claimed) == 1


async def test_it_does_not_decrypt(claims_spy) -> None:
    # 传进来的信封已经是明文。这条测试钉住签名里没有 `encrypt_key`：
    # 若将来有人把解密挪回这一层，它会因为拿不到密钥而失败。
    import inspect

    signature = inspect.signature(FeishuWebhookService.accept_verified)
    assert "encrypt_key" not in signature.parameters
    assert "secrets" not in signature.parameters
```

> `claims_spy` 与两个 `_..._envelope()` 是本任务要写的夹具，放在同一文件里；
> 照 `packages/backend/tests/unit/channels/` 现有构造信封与假 store 的方式写。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_accept_verified.py -q
```
Expected: FAIL — `AttributeError: 'FeishuWebhookService' object has no attribute 'accept_verified'`

- [ ] **Step 3: 拆**

把 `accept()` 中 `event_from_envelope(envelope)` 起至结尾的部分整体移入
`accept_verified(binding_id, envelope)`，`accept()` 在 `decrypt_payload` 之后改为
`return await self.accept_verified(binding_id=secrets.binding_id, envelope=envelope)`。

`accept()` 现有的 docstring 说的是「Verify, decrypt, normalize, claim — in that order,
and no other」，拆完之后它只做前两件。**把这句改成真话**，并说明后两件去了哪里、为什么
——两种 transport 共用它。

- [ ] **Step 4: 跑它，确认绿，并确认既有测试一条没改**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels packages/backend/tests/integration/channels -q
git diff --stat -- packages/backend/tests
```
Expected: PASS，且 `git diff --stat` **只显示新增的那个测试文件**。若有既有测试文件被
改动，说明拆点不对——回到 Step 3。

- [ ] **Step 5: 提交**

```bash
git add packages/backend/tests/unit/channels/test_accept_verified.py
git commit -m "test(channels): 已验签的事件从后半段进来"
git add packages/backend/src/tiny_hermes/channels/application/webhook_service.py
git commit -m "feat(channels): 拆出两种 transport 共用的那一半"
```

---

### Task 2: 绑定上多一个 transport

**Files:**
- Create: `migrations/versions/20260831_0052_binding_transport.py`
- Modify: `packages/backend/src/tiny_hermes/channels/infrastructure/tables.py`
- Modify: `packages/backend/src/tiny_hermes/channels/application/binding_service.py`
- Test: `packages/backend/tests/integration/channels/test_binding_transport.py`

**Interfaces:**
- Consumes: 无。
- Produces: `ChannelBindingRow.transport: Mapped[str]`，取值 `webhook` / `long_connection`，
  默认 `webhook`，附 CHECK 约束。绑定的读写接口带上它。

**默认必须是 `webhook`。** 既有绑定的行为一个字不能变——它们是靠公网地址在收消息的，
改成长连接会让它们立刻收不到任何东西。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/channels/test_binding_transport.py
"""绑定说明自己用哪种方式收事件。

默认 `webhook`：既有绑定靠公网地址收消息，默认值若是长连接，它们会在升级的
那一刻集体失聪。
"""

import pytest


async def test_an_existing_binding_reads_back_as_webhook(store, seeded_binding) -> None:
    binding_id, workspace_id = seeded_binding

    binding = await store.binding(workspace_id, binding_id)

    assert binding.transport == "webhook"


async def test_a_binding_can_declare_long_connection(store, seeded_binding) -> None:
    binding_id, workspace_id = seeded_binding

    await store.set_transport(workspace_id, binding_id, "long_connection")

    binding = await store.binding(workspace_id, binding_id)
    assert binding.transport == "long_connection"


async def test_an_invented_transport_is_refused(store, seeded_binding) -> None:
    binding_id, workspace_id = seeded_binding

    with pytest.raises(Exception):
        await store.set_transport(workspace_id, binding_id, "carrier_pigeon")
```

> `seeded_binding` 照 `packages/backend/tests/integration/channels/` 现有建绑定的夹具写，
> 返回 `(binding_id, workspace_id)`。`set_transport` 是本任务要加的 store 方法；若绑定
> 已有通用的更新入口，用那个而不是新开一个，并在报告里说明。

- [ ] **Step 2: 跑它，确认它红**

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"
uv run --no-sync pytest packages/backend/tests/integration/channels/test_binding_transport.py -q
```
Expected: FAIL — 绑定视图上没有 `transport`

- [ ] **Step 3: 加列与迁移**

迁移 `20260831_0052_binding_transport.py`，`down_revision = "20260830_0051"`：

```python
def upgrade() -> None:
    op.add_column(
        "channel_bindings",
        sa.Column(
            "transport",
            sa.String(32),
            nullable=False,
            server_default="webhook",
        ),
    )
    op.create_check_constraint(
        "ck_channel_bindings_transport",
        "channel_bindings",
        "transport IN ('webhook', 'long_connection')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_channel_bindings_transport", "channel_bindings", type_="check")
    op.drop_column("channel_bindings", "transport")
```

`tables.py` 的 `ChannelBindingRow` 加：

```python
    #: 这个绑定用哪种方式收事件。默认 `webhook` 不是偏好，是升级安全：既有绑定
    #: 靠公网地址在收消息，默认值若是长连接，它们会在升级的那一刻集体失聪。
    transport: Mapped[str] = mapped_column(
        String(32), default="webhook", server_default="webhook"
    )
```

migration 的 docstring 要写明**为什么默认是 `webhook` 而不是设计里说的「私有部署默认
长连接」**：spec 说的是新部署的推荐，而这一列的默认值决定的是**升级时既有行的取值**，
两者不是同一件事。

- [ ] **Step 4: 跑它，确认绿，并确认 alembic 干净**

```bash
uv run --no-sync pytest packages/backend/tests/integration/channels/test_binding_transport.py -q
uv run alembic check
```
Expected: PASS；`alembic check` 输出 `No new upgrade operations detected.`

- [ ] **Step 5: 提交**

```bash
git add packages/backend/tests/integration/channels/test_binding_transport.py
git commit -m "test(channels): 绑定说明自己用哪种方式收事件"
git add migrations/versions/20260831_0052_binding_transport.py \
        packages/backend/src/tiny_hermes/channels/infrastructure/tables.py \
        packages/backend/src/tiny_hermes/channels/application/binding_service.py
git commit -m "feat(channels): 绑定上多一个 transport，默认 webhook"
```

---

### Task 3: SDK 与长连接适配器

**Files:**
- Modify: `pyproject.toml`（**见全局约束：本任务是允许改它的那一个**）
- Create: `packages/backend/src/tiny_hermes/channels/infrastructure/feishu_long_connection.py`
- Test: `packages/backend/tests/unit/channels/test_long_connection_adapter.py`
- Test: `packages/backend/tests/integration/channels/test_transport_dedup.py`

**Interfaces:**
- Consumes: Task 1 的 `accept_verified(binding_id=..., envelope=...)`。
- Produces:
  ```python
  @dataclass(frozen=True)
  class LongConnectionBinding:
      binding_id: UUID
      app_id: str
      app_secret: str

  class FeishuLongConnection:
      def __init__(self, binding: LongConnectionBinding, deliver: DeliverFrame) -> None
      async def on_frame(self, frame: Any) -> None
      async def run(self, stop: asyncio.Event) -> None
      alive: bool          # 连接是否还活着；一条帧处理失败不得把它变成 False
  ```
  `DeliverFrame` 是一个 `Protocol`，签名与 `accept_verified` 一致——**适配器不认识
  `FeishuWebhookService`，只认识这个协议**，所以它可以在不碰数据库的情况下被单元测试。

**开始之前先读 SDK 的一手资料**，spec §19.2 钉了具体 commit：
`https://github.com/larksuite/oapi-sdk-python/blob/8d6402635d0a9314ddae765ae64931aabca30f79/doc/channel/quickstart.md`
**不要凭印象写 SDK 的调用方式。** 读完把「建连、注册事件处理器、关闭」三件事的真实 API
写进报告；若与本计划下面的示意不符，**以 SDK 为准并说明差异**。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/unit/channels/test_long_connection_adapter.py
"""适配器只做一件事：把 SDK 交给它的帧，原样交给共用的那一半。

它不认识 `FeishuWebhookService`，只认识 `DeliverFrame` 协议——所以这一层的
测试不需要数据库，也不该需要。
"""

from uuid import uuid4


async def test_a_frame_is_handed_to_the_shared_half(deliver_spy) -> None:
    binding = LongConnectionBinding(
        binding_id=uuid4(), app_id="cli_x", app_secret="s"
    )
    adapter = FeishuLongConnection(binding, deliver_spy)

    await adapter.on_frame(_frame({"schema": "2.0", "header": {"event_id": "e1"}}))

    assert deliver_spy.calls == [
        (binding.binding_id, {"schema": "2.0", "header": {"event_id": "e1"}})
    ]


async def test_a_failing_frame_does_not_kill_the_connection(deliver_boom) -> None:
    # 一条读不懂或处理失败的消息，不能让整条连接断掉——断了之后所有后续
    # 消息都收不到，代价远大于丢这一条。
    binding = LongConnectionBinding(binding_id=uuid4(), app_id="cli_x", app_secret="s")
    adapter = FeishuLongConnection(binding, deliver_boom)

    await adapter.on_frame(_frame({"header": {"event_id": "e2"}}))

    assert adapter.alive is True
```

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_long_connection_adapter.py -q
```
Expected: FAIL — `ModuleNotFoundError` 或 `NameError: FeishuLongConnection`

- [ ] **Step 3: 引 SDK 并实现适配器**

在根 `pyproject.toml` 的 `dependencies` 里加 `lark-oapi`（版本以 SDK 文档为准），
**并在那一行上方写一句注释说明它为什么是生产依赖**——那个文件里每个非显然的依赖都有
一句，照着写。

`on_frame` 与 SDK 无关，照抄：

```python
    async def on_frame(self, frame: Any) -> None:
        """把一帧交给两种 transport 共用的那一半。

        异常在这里被吃掉而不是冒泡：一条读不懂或处理失败的消息若把连接带下去，
        之后所有消息都收不到，代价远大于丢这一条。日志是这条路径上唯一的痕迹，
        所以它必须带上 `binding_id` 和事件 id。
        """
        try:
            await self._deliver(self._binding.binding_id, _envelope_of(frame))
        except Exception:
            logger.exception(
                "long connection frame not handled binding=%s",
                self._binding.binding_id,
            )
```

`run(stop)` 依赖 SDK 的真实 API——按 quickstart 文档写，建连、注册 `on_frame` 作为事件
处理器、跑到 `stop` 被设置为止，并在报告里写下你实际用到的三个调用。
`_envelope_of(frame)` 把 SDK 的帧对象转成 `accept_verified` 期望的 `dict`；它的形状同样
以 SDK 为准。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/unit/channels/test_long_connection_adapter.py -q
```
Expected: PASS

- [ ] **Step 5: 证明去重是共用的那一份**

这是设计里点名「最容易在实现中被绕开」的一条。

```python
# packages/backend/tests/integration/channels/test_transport_dedup.py
"""同一条事件经两条路到达，只产生一个 Run。

两种 transport 共用同一个认领，所以第二条会被 `(binding_id, channel_event_id)`
挡掉。若将来有人给长连接另写一份去重，这条测试会红——那正是它存在的理由。
"""


async def test_the_same_event_over_both_transports_makes_one_run(
    service, store, seeded_binding
) -> None:
    binding_id, workspace_id = seeded_binding
    envelope = _text_message_envelope("hello", event_id="dup-1")

    first = await service.accept_verified(binding_id=binding_id, envelope=envelope)
    second = await service.accept_verified(binding_id=binding_id, envelope=envelope)

    assert first.claim_id is not None
    assert second.claim_id is None
    assert await _events_for(store, binding_id, "dup-1") == 1
```

> `service` 用真实的 `FeishuWebhookService` 接真实的 store；`_events_for` 直接数
> `channel_events` 里该 `channel_event_id` 的行数。**第二次返回 `claim_id is None`
> 的确切形状以 `claim_delivery` 的真实契约为准**，若不同，用真实的并在报告里说明。

- [ ] **Step 6: 跑绿并提交**

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"
uv run --no-sync pytest packages/backend/tests/unit/channels packages/backend/tests/integration/channels -q
```

```bash
git add packages/backend/tests/unit/channels/test_long_connection_adapter.py \
        packages/backend/tests/integration/channels/test_transport_dedup.py
git commit -m "test(channels): 帧交给共用的那一半，两条路只产生一个 Run"
git add pyproject.toml packages/backend/src/tiny_hermes/channels/infrastructure/feishu_long_connection.py
git commit -m "feat(channels): 飞书长连接适配器"
```

---

### Task 4: 住进 scheduler，并把断线留下痕迹

**Files:**
- Modify: `packages/backend/src/tiny_hermes/api/cli.py`（scheduler 入口，约 290-307 行）
- Modify: `packages/backend/src/tiny_hermes/channels/infrastructure/feishu_long_connection.py`
- Test: `packages/backend/tests/integration/channels/test_long_connection_lifecycle.py`

**Interfaces:**
- Consumes: Task 2 的 `transport` 列；Task 3 的 `FeishuLongConnection`、`LongConnectionBinding`。
- Produces: 无新公开接口；scheduler 进程行为改变。

scheduler 入口现在是：建 runtime → `stop = _stop_on_termination()` → `run_forever(stop, interval)`
→ `finally` 关资源。长连接与 `run_forever` **并排跑，共用同一个 `stop`**。

**断线必须留痕，这是硬性要求。** 每次断开与重连记一条事件，**带上断开时长**。理由不是
可观测性好看：§19.2 要求验证断线期间的补投语义，而没有断开时间窗，那次验证只能靠猜
——需要知道断了多久、以及那段时间里本该有哪些消息。

**不做热重载。** 新增绑定或改 `transport` 需要重启 scheduler。这一条必须写进 docstring
**和**控制台提示，否则用户改完发现没生效会以为坏了。

- [ ] **Step 1: 写失败的测试**

```python
# packages/backend/tests/integration/channels/test_long_connection_lifecycle.py
"""连接的生死要留下痕迹。

断线时长是将来验证补投语义的唯一依据——拔了网线之后，要知道断了多久，
才能判断那段时间的消息有没有被补发。
"""


async def test_only_long_connection_bindings_get_a_connection(
    scheduler_connections, seeded_bindings_of_both_transports
) -> None:
    webhook_id, long_id = seeded_bindings_of_both_transports

    started = await scheduler_connections()

    assert [b.binding_id for b in started] == [long_id]


async def test_a_disconnect_is_recorded_with_how_long_it_lasted(
    store, adapter_that_drops
) -> None:
    await adapter_that_drops.run_until_reconnected()

    events = await _connection_events(store)
    assert [e["kind"] for e in events] == ["disconnected", "reconnected"]
    assert events[1]["down_seconds"] >= 0


async def test_a_failed_connect_does_not_stop_the_scheduler(
    scheduler_connections, binding_with_bad_credentials
) -> None:
    # 一个连不上的绑定不能拖垮主循环——它还负责发回复、发卡片、回执。
    started = await scheduler_connections()

    assert started is not None
```

- [ ] **Step 2: 跑它，确认它红**

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://tiny_hermes:local-only@127.0.0.1:55432/tiny_hermes_test"
export DATABASE_URL="$TEST_DATABASE_URL"
uv run --no-sync pytest packages/backend/tests/integration/channels/test_long_connection_lifecycle.py -q
```
Expected: FAIL

- [ ] **Step 3: 接进 scheduler**

在 `cli.py` 的 scheduler 入口，把现在这一行

```python
    await runtime.run_forever(stop, settings.scheduler_interval_seconds)
```

换成并排跑：

```python
    connections = await _long_connections(settings, senders)
    await asyncio.gather(
        runtime.run_forever(stop, settings.scheduler_interval_seconds),
        # 与主循环共用同一个 `stop`：进程收到终止信号时两边一起停，
        # 而不是主循环退出后留着一堆连接等超时。
        *(connection.run(stop) for connection in connections),
    )
```

`_long_connections` 读出 `transport = 'long_connection'` 的活跃绑定，为每个构造
`LongConnectionBinding` 与 `FeishuLongConnection`。它的 docstring 要写明**绑定变更不会
被这里看到**——scheduler 只在启动时读一次，改了 transport 需要重启。

凭据用现有的 `CredentialResolver` 解析——**在读出绑定的那个 session 里解**，这是本仓库
踩过的坑（`fix(channels): resolve the image secret through a real session`）。

- [ ] **Step 4: 跑它，确认绿**

```bash
uv run --no-sync pytest packages/backend/tests/integration/channels -q
```
Expected: PASS

- [ ] **Step 5: 破坏性验证**

把「按 transport 过滤」那个条件临时去掉（让所有活跃绑定都建连），重跑：
Expected: `test_only_long_connection_bindings_get_a_connection` **FAIL**。
不红说明夹具里根本没有 webhook 绑定，**先修夹具**。恢复后确认绿。

- [ ] **Step 6: 提交**

```bash
git add packages/backend/tests/integration/channels/test_long_connection_lifecycle.py
git commit -m "test(channels): 只给声明了长连接的绑定建连，断线要留痕"
git add packages/backend/src/tiny_hermes/api/cli.py \
        packages/backend/src/tiny_hermes/channels/infrastructure/feishu_long_connection.py
git commit -m "feat(channels): 长连接住进 scheduler，断开与重连留下时长"
```

---

### Task 5: 让长连接收到的消息真的变成 Run

**这个任务是计划的缺陷补丁，不是原计划的一部分。** Task 1 把 `accept()` 拆成
「验签」+「归一化并认领」两段，并声称后一段是**两种 transport 共用的那一半**。
它不是。Webhook 那条路在拿到 `Claimed` 之后还要做六件事，全在
`FeishuChannelService.deliver`（`feishu_service.py:119-175`）里：

- `Unreadable` → `record_unsupported`（否则发消息的人只等到静默）
- `claim_id is None` → 去重命中，什么也不做
- `ingestion.run_for(...)` → **这一步才产生 Run**
- 命令而非 Run → `record_command_receipt`
- `attach_run(claim_id, run_id)` → 出站队列的键，缺了就没人收到回复
- `delivered.blocked` → `record_blocked_notice`

长连接现在走到 `accept_verified` 就停了。结果是：消息落进 `channel_events`，
**没有 Run，没有回复**。这正是这个项目最常见的那个 bug——写进去了不等于有人
够得着，第十一次。分支唯一真正的验收（关掉隧道发一条消息、看它被回复）现在
会在最后一步失败。

**Files:**
- Modify: `packages/backend/src/tiny_hermes/channels/application/feishu_service.py`
- Modify: `packages/backend/src/tiny_hermes/api/cli.py`（`_deliver_via`，约 381-398 行）
- Modify: `packages/backend/src/tiny_hermes/channels/infrastructure/feishu_long_connection.py`（`DeliverFrame` 的返回类型）
- Test: `packages/backend/tests/integration/channels/test_long_connection_lifecycle.py`

**Interfaces:**
- Produces: `FeishuChannelService.deliver_verified(*, binding_id: UUID, envelope: dict[str, Any], request_id: str) -> Accepted`
- Changes: `DeliverFrame.__call__` 的返回类型从 `Claimed | Unreadable` 改成 `Accepted`

**接缝画在哪：** 把 `deliver()` 里 `outcome = await self._webhooks.accept(...)` **之后**
的全部逻辑原样搬进 `_after_claim(binding, outcome, request_id) -> Accepted`，`deliver()`
和新的 `deliver_verified()` 都调它。**搬，不要重写**——那一段里的每条注释都记着一次
真实事故（`attach_run` 那条记的是「一次线上部署留下两个 `run_id` NULL 的认领，什么
都没失败，因为没人读那一列」），重写会把它们弄丢。

**`request_id` 用什么：** 长连接没有 HTTP 请求。用**飞书的事件 id**——它是这条路上
唯一天然的关联键，而且 `_event_id_of` 已经能从信封里取出来。它只当字符串关联键用
（进审计文本和 `/new` 的 escape hatch），不要求是 UUID。

**同一个 session：** `_deliver_via` 里那个 per-frame session 现在要同时装下
`SqlChannelStore`、`CredentialResolver`、`FeishuWebhookService`、`ChannelIngestion`
——照 `resources.py:188` 的 `feishu_channel_service` 抄那张依赖图，它的 docstring 讲了
为什么必须是一个 session：认领和它引出的 Run 必须一起提交或一起回滚。`cli.py` 已经
import 了需要的每一样东西（`CredentialResolver`、`optional_kek`、`SqlSecretStore`、
`SqlChannelStore`），只缺 `SqlEndUserStore`、`RunCoordination`、`SqlRunStore`、
`ChannelIngestion`、`FeishuChannelService`。

- [ ] **Step 1: 先写会红的测试**

在 `test_long_connection_lifecycle.py` 里加一条。它必须断言**库里真的多了一个 Run，
并且这个 Run 和那条认领连上了**——不要只断言 `deliver` 的返回值，那正是让前十次
漏掉的写法：

```python
async def test_a_frame_over_the_long_connection_becomes_a_run(
    engine: AsyncEngine, seeded_bindings_of_both_transports: ...
) -> None:
    """判据不是「deliver 返回了 Accepted」，是「有人够得着」：
    `runs` 里多了一行，且 `channel_events.run_id` 指向它。少任何一半，
    发消息的人都等不到回复。"""
```

用真实的飞书消息信封（文本消息）走 `_deliver_via(...)` 返回的那个 `deliver`。
断言三件事，**按 id 找行，不要按下标**：
1. `channel_events` 里那条认领的 `run_id` **不是 NULL**；
2. `runs` 里存在该 id 的行；
3. 它的 `session_id` 属于这个绑定对应的会话。

- [ ] **Step 2: 跑它，确认它红**

```bash
uv run --no-sync pytest packages/backend/tests/integration/channels/test_long_connection_lifecycle.py::test_a_frame_over_the_long_connection_becomes_a_run -q
```
Expected: FAIL，`run_id` 是 `None`（不是报错，是断言失败）。**如果它直接绿了，
说明你的信封没走到该走的地方，先查信封再往下。**

- [ ] **Step 3: 提交这条红测试**

```bash
git add packages/backend/tests/integration/channels/test_long_connection_lifecycle.py
git commit -m "test(channels): 长连接进来的一帧要变成 Run，不是只落一条认领"
```

- [ ] **Step 4: 抽出 `_after_claim`，加 `deliver_verified`**

`feishu_service.py`：`deliver()` 保持行为不变（既有测试一条都不该改）。

- [ ] **Step 5: `_deliver_via` 改成建整个 `FeishuChannelService` 并调 `deliver_verified`**

顺带把它的 docstring 里那句「`accept_verified` only normalizes and claims (the half both
transports share); it does not create a Run」改掉——**改完它就成假话了**，而这个项目有
一条硬规矩：一条注释不得声称代码没有的保护，反过来也一样，注释不得描述代码已经不
再做的事。

- [ ] **Step 6: 跑测试，确认绿；再确认既有的 webhook 测试一条没坏**

```bash
uv run --no-sync pytest packages/backend/tests/integration/channels -q
```

- [ ] **Step 7: 提交**

```bash
git add packages/backend/src/tiny_hermes/channels/application/feishu_service.py \
        packages/backend/src/tiny_hermes/api/cli.py \
        packages/backend/src/tiny_hermes/channels/infrastructure/feishu_long_connection.py
git commit -m "feat(channels): 长连接和 webhook 共用认领之后的那一整段"
```

---

## 收尾

- [ ] **本地全套**：`alembic check`、unit、integration、ruff、pyright、web、chat-web。
      `tests/integration/model_catalog/test_endpoint_api.py` 那两条在有仓库根 `.env` 的
      机器上会失败，是已知环境问题。
- [ ] **部署并真机走一遍**：`deploy/compose/redeploy.sh`（它会逐个容器验证代码真的换了
      ——本仓库已经因为「没换成」白测过一整轮）。把飞书绑定切到 `long_connection`，
      **关掉 cloudflared 隧道**，然后发一条消息，确认它照样到达。**这是这条分支唯一
      真正的验收：没有公网入口也能收到消息。**
- [ ] **写验收记录** `docs/superpowers/verification/2026-08-31-feishu-long-connection.md`，
      必须有「这一遍没能证明什么」与「不声称什么」两节。至少写明：**断线补投语义没有
      验证**（拔网线那一步没做），以及多副本 scheduler 下会重复建连这件事没有处理。
- [ ] **开 PR，取得真正的 compose-e2e 绿色**，用
      `gh run view <id> --log | grep "^compose-e2e" | grep -E "passed|✘"` 确认。
