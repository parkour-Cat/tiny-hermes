# 上传清理：提交方先清完时，scheduler 不再报失败

日期 2026-10-24。分支 `fix/upload-cleanup-race`。由[两小时压测](2026-10-24-long-task-soak.md)发现。

## 问题

工作区上传提交后，提交方马上自己清暂存区和候选索引，然后清掉 `cleanup_pending`。scheduler 的
`collect_upload_garbage` 也会认领「已提交但还有清理欠账」的行。两边在提交后约 0.1 秒内撞上时，后清完
的一方调用 `finish_cleanup` 会被守卫拒绝（`UploadStateConflict`）：

- 提交方后到：异常被吞掉，本来就没事。
- scheduler 后到：异常一路抛出，**整项扫描记为失败，这一批里剩下的上传也被跳过**。压测里出现过一次。

## 改了什么

`reclaim_upload` 在 `finish_cleanup` 报 `UploadStateConflict` 时当作已完成。依据是这个守卫只拒绝
`cleanup_pending` 已经清掉的行（端口的 docstring 写明了这一点）。两边删的是同一批暂存对象和同一个索引，
删两次和删一次结果一样。行不存在（`UnknownUpload`）仍然抛出。

## 测试过了

- 真 Postgres 和本机对象存储：一个已提交的上传，scheduler 先认领，提交方先清完，scheduler 再执行清理
  → 返回已完成，行仍是 `committed`，欠账已清（**改之前失败，报错和压测里的一模一样**）。
- 会话工作区集成测试 43 passed；scheduler 集成测试 18 passed；后端单元测试 2763 passed。

## 这一遍没能证明什么

- 测试是按顺序构造的（先认领，再让提交方清完，再清理），没有真的让两个进程并发去撞。
- 修好后没有再跑一遍压测，去看这种失败是不是不再出现。它本来就要凑巧才出现，压测里两小时也只出现过一次。

## 不声称什么

- 不声称上传清理没有别的竞争：这次只处理了「提交方先清完」这一种。
