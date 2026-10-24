# 沙箱的容器运行时可以配置（为 gVisor 准备）

日期 2026-10-24。分支 `feat/sandbox-runtime`。产品设计 v2.25（§1.25）。

## 改了什么

- 新配置 `SANDBOX_RUNTIME`，默认 `runc`，只接受像名字的值（小写字母、数字、`.`、`_`、`-`）。
- 每个沙箱容器都显式带上 `runtime`，不再交给 Docker 的默认值。
- controller 启动时读 `docker info` 的运行时列表，配置的不在里面就退出，报错里列出 Docker 有哪些。
- Compose 透传这个变量；运维手册 §17。

## 测试过了

- 单元：默认 `runc`、可以设为 `runsc`、不像名字的值被拒绝；容器参数集合（字面量比对）里多了
  `runtime`；Docker 没有的运行时让启动检查退出，报错里带配置名和已有的运行时。
- 真 Docker（本机 OrbStack）：新建的沙箱 `HostConfig.Runtime == "runc"`；这台 Docker 的运行时列表里有
  `runc`、没有 `runsc`，启动检查对 `runsc` 退出。
- 后端单元 2763 passed。

## 这一遍没能证明什么

- **没有在 gVisor 上跑过任何沙箱**：本机 Docker 没装 `runsc`。沙箱在 gVisor 下能不能正常执行命令、
  打包和导入工作区、每轮多花多少时间和内存，都没有测。
- 运行时检查只在 controller 启动时做一次；启动之后有人从 Docker 里卸掉运行时，不会被发现。

## 不声称什么

- 不声称支持 gVisor：只是能配置运行时，并且配错了起不来。
