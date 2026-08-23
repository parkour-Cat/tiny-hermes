# 第三方许可证清单

> 由 `scripts/license_inventory.py` 生成，读的是**已安装**的依赖——
> 也就是真正会随发布走的那些，而不是 `pyproject.toml` 声明的那些。
> 两者相差的正是传递依赖，而意外通常出在传递依赖上。
>
> **本文件不构成法律意见**（产品设计 §28 最后一句）。

本项目自身采用 Apache-2.0。Python 依赖 56 个；前端见文末。

## Python 依赖的分布

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

## 全部 Python 依赖

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

## 前端（pnpm，生产依赖）

共 74 个。

| 许可证 | 数量 |
|---|---:|
| MIT | 74 |

| 包 | 版本 | 许可证 |
|---|---|---|
| @ant-design/colors | 8.0.1 | MIT |
| @ant-design/cssinjs | 2.1.2 | MIT |
| @ant-design/cssinjs-utils | 2.1.2 | MIT |
| @ant-design/fast-color | 3.0.1 | MIT |
| @ant-design/icons | 6.3.2 | MIT |
| @ant-design/icons-svg | 4.5.0 | MIT |
| @ant-design/react-slick | 2.0.0 | MIT |
| @babel/runtime | 7.29.7 | MIT |
| @babel/runtime | 8.0.0 | MIT |
| @emotion/hash | 0.8.0 | MIT |
| @emotion/unitless | 0.7.5 | MIT |
| @rc-component/async-validator | 6.0.0 | MIT |
| @rc-component/cascader | 1.17.0 | MIT |
| @rc-component/checkbox | 2.0.0 | MIT |
| @rc-component/collapse | 1.2.0 | MIT |
| @rc-component/color-picker | 3.1.1 | MIT |
| @rc-component/context | 2.0.2 | MIT |
| @rc-component/dialog | 1.10.0 | MIT |
| @rc-component/drawer | 1.4.2 | MIT |
| @rc-component/dropdown | 1.0.3 | MIT |
| @rc-component/form | 1.8.6 | MIT |
| @rc-component/image | 1.9.0 | MIT |
| @rc-component/input | 1.3.1 | MIT |
| @rc-component/input-number | 1.6.2 | MIT |
| @rc-component/mentions | 1.10.0 | MIT |
| @rc-component/menu | 1.4.1 | MIT |
| @rc-component/mini-decimal | 1.1.4 | MIT |
| @rc-component/motion | 1.3.3 | MIT |
| @rc-component/mutate-observer | 2.0.1 | MIT |
| @rc-component/notification | 2.0.7 | MIT |
| @rc-component/overflow | 1.0.1 | MIT |
| @rc-component/pagination | 1.4.0 | MIT |
| @rc-component/picker | 1.12.0 | MIT |
| @rc-component/portal | 2.2.1 | MIT |
| @rc-component/progress | 1.0.2 | MIT |
| @rc-component/qrcode | 2.0.0 | MIT |
| @rc-component/rate | 1.0.1 | MIT |
| @rc-component/resize-observer | 1.1.2 | MIT |
| @rc-component/segmented | 1.3.0 | MIT |
| @rc-component/select | 1.8.2 | MIT |
| @rc-component/slider | 1.1.1 | MIT |
| @rc-component/steps | 1.2.2 | MIT |
| @rc-component/switch | 1.0.3 | MIT |
| @rc-component/table | 1.10.4 | MIT |
| @rc-component/tabs | 1.11.0 | MIT |
| @rc-component/tooltip | 1.4.0 | MIT |
| @rc-component/tour | 2.4.0 | MIT |
| @rc-component/tree | 1.3.2 | MIT |
| @rc-component/tree-select | 1.11.0 | MIT |
| @rc-component/trigger | 3.10.1 | MIT |
| @rc-component/upload | 1.1.1 | MIT |
| @rc-component/util | 1.12.0 | MIT |
| @rc-component/virtual-list | 1.5.1 | MIT |
| @tanstack/query-core | 5.101.4 | MIT |
| @tanstack/react-query | 5.101.4 | MIT |
| antd | 6.5.4 | MIT |
| clsx | 2.1.1 | MIT |
| compute-scroll-into-view | 3.1.1 | MIT |
| cookie | 1.1.1 | MIT |
| csstype | 3.2.3 | MIT |
| dayjs | 1.11.21 | MIT |
| is-mobile | 5.0.0 | MIT |
| json2mq | 0.2.0 | MIT |
| react | 19.2.8 | MIT |
| react-dom | 19.2.8 | MIT |
| react-is | 19.2.8 | MIT |
| react-router | 7.18.2 | MIT |
| react-router-dom | 7.18.2 | MIT |
| scheduler | 0.27.0 | MIT |
| scroll-into-view-if-needed | 3.1.0 | MIT |
| set-cookie-parser | 2.7.2 | MIT |
| string-convert | 0.2.1 | MIT |
| stylis | 4.4.0 | MIT |
| throttle-debounce | 5.0.2 | MIT |
