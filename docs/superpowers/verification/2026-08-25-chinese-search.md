# 中文搜不到 — 2026-08-25

> 产品：v2.5 §14.3（会话搜索）、§14.1（记忆）、§7.4.2（上下文分段预算）。
> 起因：在讨论「压缩摘要要不要带语义内容」时，去核对 `session_search` 能不能
> 兜底，结果发现它对中文根本不工作。

## 1. 一条 SQL 就能看见

```
to_tsvector('simple', '上周的订单统计和客户偏好')
  → '上周的订单统计和客户偏好':1          ← 整句一个 token

to_tsvector('simple', 'order stats and customer prefs')
  → 'and':3 'customer':4 'order':1 ...    ← 英文正常分词
```

`simple` 按空格和标点切。中文没有空格，所以**一整句变成一个 token**，查询只有
在和原文逐字一致时才命中。

```
搜「用户偏好」 → 匹配「用户偏好用中文回复,项目叫 tiny-hermes」?  f
搜「prefers」  → 匹配「user prefers Chinese replies」?            t
```

## 2. 两处依赖它，两处都坏

**§14.3 的 `session.search`。** 模型「想不起来就去搜」这条退路，在中文对话里
是断的。这也正是我原本打算用来兜底压缩失忆的那条路。

**`memories.relevant_in` 的排序，这条更隐蔽。** 它的 docstring 写着：

> ordered by **keyword relevance**... It decides which memories the planner
> sees first, and therefore **which ones survive when the segment is over
> budget**.

中文下这个相关性恒为零，于是排序**静默退化成「按时间倒序」**。预算不够时丢掉
哪几条记忆，实际上是任意的。docstring 描述的机制，对这个平台的真实用户从来
没有运行过。

## 3. 为什么没被发现

英文测试全绿，而且会一直全绿——`simple` 对英文工作得很好。这个缺陷只在中文
输入下出现，而**整个测试套件当时只说英文**。

原注释是这么写的：

> `simple` for the reason the memory index uses it: this platform **serves
> Chinese and English side by side**, and a stemmer for one mangles the other.

推理正确（一种语言的词干器会毁掉另一种），结论跳了一步：**`simple` 对中文根本
不分词**。这条注释又一次声称了代码没有的能力。

## 4. 修法：加一层，不换配置

保留 `simple`，**额外**加一个 Han 字符二元组索引，查询时和词索引 OR 起来。

```sql
to_tsvector('simple', body) || to_tsvector('simple', th_cjk_bigrams(body))
```

- 英文继续按词匹配，**完全不受影响**
- 中文按相邻字符对匹配

**为什么是二元组不是单字**：单字 token 几乎匹配一切，而一个「返回所有会话」的
搜索和「返回零个」一样没用，还更难发现。代价是**单字查询无法命中**——写进迁移
注释了，因为它是静默失败。

**`matching()` 是共享函数**，不是四个调用点各写一遍。漏掉一个的后果是沉默：
一个静静地什么都匹配不到的搜索，和一个确实没有结果的搜索，看起来一模一样。

它只在查询真的含 Han 字符时才加二元组分支——`th_cjk_bigrams` 对纯 ASCII 返回
空串，而 `plainto_tsquery` 对空串**每次调用都发一条 NOTICE**。

## 5. 测试里踩到两次「假设 ASCII」

同一个文件，同一个根因，都是**英文测试永远看不到**的：

**`Idempotency-Key` 直接拼输入文本。** HTTP header 是 latin-1，中文直接
`UnicodeEncodeError`——在一个测试搜索功能的套件里，为一个用户写中文的平台。
改成了摘要哈希。

**`_transcript` 读 `content::text`。** PostgreSQL 把中文转义成 `\uXXXX`，所以
中文断言永远匹配不上，英文断言永远匹配得上。**我一度以为搜索没生效，实际上它
早就成功了**，只是断言在和转义后的字符串比。

## 6. 破坏性验证

| 打掉什么 | 中文测试 |
|---|---|
| 查询侧的二元组分支 | 挂 |
| 索引侧的二元组列 | 挂 |
| 记忆排序的查询侧 | 挂 |

两半缺一不可。

迁移可逆性也验了：`HAS BIGRAM → words only → HAS BIGRAM`。

（第一次做这个验证时，我手动 `DROP COLUMN` 连带删掉了索引，导致后续 downgrade
中途失败，而我把错误重定向掉了没看见。干净重做后才确认迁移本身没问题。）

## 7. 这一遍没能证明什么

- **compose-e2e 没有重新取得绿色。** 本地跑过的栈不算数。
- **没有对着真实的长对话验过。** 测试里的中文是两三句话；一个几十天、几千条
  消息的会话里，二元组索引的体积和查询延迟**没有测量过**。GIN 索引会明显变大，
  大多少不知道。
- **单字查询不能匹配，没有做任何补偿。** 「搜『猫』」会静默返回空。
- **只覆盖 Han 字符。** 日文假名、韩文谚文没有处理——`simple` 对它们的切分
  是什么行为，没有验证。
- **`th_cjk_bigrams` 的正则用的是字符区间 `[㐀-鿿]`**，对扩展区（CJK Ext B 及
  以上，超出 BMP 的字）行为没有验证。
## 8. 既有数据的重建成本：量过了

原本这条写着「不知道」。量了：

| | |
|---|---|
| 数据量 | 50 万行 / 248 MB |
| `ALTER TABLE`（**持 ACCESS EXCLUSIVE 锁**） | **17.6 秒** |
| 重建 GIN 索引 | 5.0 秒 |
| 表大小 | 248 MB → **426 MB（+72%）** |

（本机 Docker 里的 postgres:16，中文消息，每条约一句话。）

线性外推 **500 万行 ≈ 3 分钟全表锁**——期间这张表完全不可读写，也就是整个
平台不可用。

**所以这个迁移不能直接跑在有历史数据的生产库上。** 需要在线方案：新列 →
后台分批回填 → 建索引用 `CONCURRENTLY` → 切换。那是独立的一块工作，这一版
没有做。

对一个刚部署、`session_messages` 还很小的环境，直接跑没问题——17.6 秒是
50 万行的数字，几千行是瞬间。

**索引体积 +72% 也要算进容量规划。** 二元组比原文 token 多得多，这是它能
工作的原因，也是它的成本。

## 9. 这一遍还是没能证明什么
