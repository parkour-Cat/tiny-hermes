# 第三方许可证清单

> 由 `scripts/license_inventory.py` 生成，读的是**已安装**的依赖——
> 也就是真正会随发布走的那些，而不是 `pyproject.toml` 声明的那些。
> 两者相差的正是传递依赖，而意外通常出在传递依赖上。
>
> **本文件不构成法律意见**（产品设计 §28 最后一句）。

本项目自身采用 Apache-2.0。依赖共 56 个。

## 分布

| 许可证 | 数量 |
|---|---:|
| MIT | 25 |
| BSD-3-Clause | 11 |
| Apache-2.0 | 7 |
| MPL-2.0 | 1 |
| MIT-0 | 1 |
| Apache-2.0 OR BSD-3-Clause | 1 |
| ISC | 1 |
| Unlicense | 1 |
| MIT AND PSF-2.0 | 1 |
| BSD | 1 |
| Apache-2.0 OR BSD-2-Clause | 1 |
| MIT License | 1 |
| BSD, Public Domain | 1 |
| BSD-2-Clause | 1 |
| MIT OR Apache-2.0 | 1 |
| PSF-2.0 | 1 |

## 需要留意的

以下依赖的许可证带有超出「署名」的义务。**它们都不阻塞发布**，
列在这里是因为「全都是宽松许可」这句话会有人说出口，而它应该被知情地说。

- `certifi`

## 全部依赖

| 包 | 版本 | 许可证 |
|---|---|---|
| alembic | 1.19.1 | MIT |
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.14.2 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 25.1.0 | MIT |
| asyncpg | 0.31.0 | Apache-2.0 |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.4.9 | MIT |
| click | 8.4.2 | BSD-3-Clause |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| dnspython | 2.8.0 | ISC |
| docker | 7.2.0 | Apache-2.0 |
| email-validator | 2.3.0 | Unlicense |
| fastapi | 0.141.1 | MIT |
| greenlet | 3.5.5 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.10.0 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx2 | 2.10.0 | BSD-3-Clause |
| idna | 3.18 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| Mako | 1.4.1 | MIT |
| MarkupSafe | 3.0.3 | BSD-3-Clause |
| minio | 7.2.20 | Apache-2.0 |
| nodeenv | 1.10.0 | BSD |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pluggy | 1.6.0 | MIT |
| pwdlib | 0.3.0 | MIT License |
| pycparser | 3.0 | BSD-3-Clause |
| pycryptodome | 3.23.0 | BSD, Public Domain |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pydantic_core | 2.46.4 | MIT |
| Pygments | 2.20.0 | BSD-2-Clause |
| PyJWT | 2.13.0 | MIT |
| pyright | 1.1.411 | MIT |
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| redis | 8.1.0 | MIT |
| requests | 2.34.2 | Apache-2.0 |
| ruff | 0.16.2 | MIT |
| SQLAlchemy | 2.0.51 | MIT |
| starlette | 1.6.0 | BSD-3-Clause |
| structlog | 26.1.0 | MIT OR Apache-2.0 |
| truststore | 0.10.4 | MIT |
| types-PyYAML | 6.0.12.20260724 | Apache-2.0 |
| typing-inspection | 0.4.2 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| urllib3 | 2.7.0 | MIT |
| uvicorn | 0.52.1 | BSD-3-Clause |
