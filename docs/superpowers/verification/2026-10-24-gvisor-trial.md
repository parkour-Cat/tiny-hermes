# gVisor 试装

日期 2026-10-24。本机 OrbStack（Docker 引擎在 aarch64 的 Linux 虚拟机里），gVisor `release-20261019.0`。

## 怎么装的

- 官方发布包 `releases/release/latest/aarch64/gvisor.tar.bz2`，按同目录的 `.sha512` 校验通过。
- 解到 `~/.local/gvisor/`。Docker 引擎的虚拟机里也能看到 `/Users`，所以 `runsc` 直接用这个路径。
- `~/.orbstack/config/docker.json` 里注册 `"runtimes": {"runsc": {"path": ".../runsc"}}`，然后 `orb restart docker`。
  原来的配置备份为 `docker.json.bak-before-gvisor`。
- `docker info` 的运行时列表里出现了 `runsc`。容器里 `uname -r` 显示 `4.19.0-gvisor`。

## 能用的

本机栈上用 `SANDBOX_RUNTIME=runsc` 重建 controller，跑确定性模型：

| 项目 | runc | runsc |
|---|---|---|
| 18 轮长任务（每轮写工作区、存检查点），3 次 | 都完成，约 3.2 秒 | 都完成，约 3.3 秒 |
| 平台记录的单次工具调用中位数 | 24 毫秒 | 约 33 毫秒 |
| 写 2000 个小文件和一个 50 MB 文件 | 约 0.4 秒 | 约 1.7 秒（**约 4 倍**） |
| 这条命令之后到 Run 完成（包括打包检查点） | 约 0.62 秒 | 约 0.81 秒 |
| 建容器加第一次 exec | 约 170 到 280 毫秒 | 约 190 到 230 毫秒 |
| 空闲时的容器内存 | 不到 1 MiB | 约 13 MiB（gVisor 自己的内核） |
| 暂停和恢复（冻结沙箱） | 正常 | 正常 |
| 沙箱没有网络时，连不到外面 | 是 | 是 |

## 不能用的：沙箱集成测试在 runsc 下 7 条失败

把默认运行时临时改成 `runsc`，在本机跑真 Docker 的沙箱集成测试（这个改动没有提交）：208 passed，7 failed。
其中 1 条是预期的（断言运行时是 `runc` 的那条），其他 6 条是三个问题：

1. **冻结（已暂停）的沙箱删不掉。** gVisor 下对已暂停的容器 `docker rm -f` 会失败：`could not kill container: ... is zombie`。
   手动试过，先恢复再删就能删掉。平台回收、驱逐冻结的沙箱时都是直接删，所以会一直删不掉，和这次压测里沙箱卡在
   `isolated` 是同一种后果。4 条失败是这个原因。
2. **沙箱按名字连不到出网代理。** Docker 自定义网络的名字解析（容器里的 `127.0.0.11`）在 gVisor 下不工作，
   `getent hosts` 解析不出任何名字。沙箱的 `HTTP_PROXY` 写的是代理的名字，所以**有出网边界的部署在 gVisor 下
   完全不能出网**。手动试过：按 IP 能连上，用 `--add-host` 写进 `/etc/hosts` 的名字也能连上；内部网络照样连不到
   外面。1 条失败是这个原因。
3. **缓存目录（tmpfs）的 inode 上限不生效。** 第 17 个文件没有被拒绝。大小上限是生效的。gVisor 的 tmpfs 在它自己的
   内存里，总量仍受容器内存上限约束，但「文件数不超过 N」这条保证在 gVisor 下没有了。1 条失败是这个原因。

## 结论

`SANDBOX_RUNTIME=runsc` 现在**不能用在生产上**：第 1 条会让冻结的沙箱永远回收不掉，第 2 条会让需要出网的工具全部
失败。两条都有明确的修法（删除前先恢复暂停的容器；给沙箱写一条指向代理 IP 的 hosts 记录），修完要在 runsc 下把沙箱
集成测试整套跑绿。

本机栈已经改回 `runc`。gVisor 还装着，OrbStack 的 Docker 里留着 `runsc` 这个运行时，不影响默认用 `runc` 的容器。

## 这一遍没能证明什么

- 性能只在一台 Mac 的虚拟机里测过，数据量很小；Linux 服务器上（尤其能用 KVM 平台时）的开销可能不一样。
- 本机栈没有配沙箱网络（`SANDBOX_NETWORK_POOL` 为空），通过代理出网这条路只在集成测试和手动试验里看过，没有走过
  完整的工具调用。
- 没有测 gVisor 下的安全性本身，比如它挡住了哪些系统调用。这次只看功能和开销。

## 不声称什么

- 不声称 gVisor 让沙箱「安全了」：它多了一层隔离，但上面三个问题说明换运行时会改变平台原来依赖的行为。
