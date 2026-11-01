# `durable_metadata`(全局游标)—— ID 与提交序号的唯一来源

> TS 原版表,Python 迷你版未实现(迷你版只有五张表,全景见 [../storage-entities.md](../storage-entities.md))。本文按 TS 源码(`@earendil-works/pi-durable` 的 `packages/durable/src/storage/sqlite/`)讲这张表的结构与事务角色。

一句话定位:**两个全局计数器的家 —— `next_id`(记录 ID 分配游标)和 `next_seq`(提交序号)。整库所有"跨表单调性"都从这一行读出来。**

## 1. 建表语句

见 `packages/durable/src/storage/sqlite/migrations.ts:10-15`:

```sql
CREATE TABLE durable_metadata (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  next_id   TEXT    NOT NULL,
  next_seq  INTEGER NOT NULL
) STRICT;
INSERT INTO durable_metadata (singleton, next_id, next_seq) VALUES (1, '2', 1);
```

初始值有讲究:`next_id = '2'` 是因为 ID `1` 保留给 root 会话(`types.ts:39` 的 `ROOT_CONVERSATION_ID`);`next_seq = 1` 表示第一次提交拿到的序号是 1。

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `singleton` | INTEGER | PRIMARY KEY, CHECK (=1) | 强制单行:这张表只允许一行,没有"第二份元数据" |
| `next_id` | TEXT | NOT NULL | 下一个可铸造的记录 ID。**用 TEXT 存数字**:`node:sqlite` 会拒绝超出 JavaScript 安全整数范围的 INTEGER 结果,文本编码保住 2^53 之后的 ID 空间(`migrations.ts:8` 的注释) |
| `next_seq` | INTEGER | NOT NULL | 下一次提交将获得的提交序号;也是"到目前为止共有多少次提交" |

## 3. 谁写、谁读

| 方向 | 代码 | 说明 |
|---|---|---|
| 读(启动) | `SqliteStorage.open()`(`storage.ts:147-151`) | 读一次,把 `next_id` 装进内存,之后的 `mintId()` 在内存里自增(`storage.ts:187-191`) |
| 读(每次提交) | `commit()` 事务第一句(`storage.ts:166-171`) | 读出 `next_seq`,这个值就是本批次的提交序号 |
| 写(每次提交) | `commit()` 事务最后一句(`storage.ts:176-180`) | `next_seq += 1`;`next_id = max(现值, 本批次用到的最大 ID + 1)` |

`mintId()` 在 commit 之前就于内存推进,失败的提交会留下 ID 空洞(无害);但**元数据只在成功提交时推进**,所以崩溃重启后游标不会倒退到已用过的号以下(配合 [record_ids.md](record_ids.md) 的归属检查,ID 复用会被显式拒绝)。

## 4. `next_seq` 是整库时间戳

一个提交批次里的所有行共用同一个 `seq`,它被写进:

| 落点 | 表 | 语义 |
|---|---|---|
| `commit_seq` | entries | 这条条目是第几次提交时落库的 |
| `created_at` / `retired_at` | documents | 文档化身的生卒"提交序号";`findDocument(address, at)` 按 `at` 做**时间点寻址** |
| `seq` | document_revisions | 版本链的排序键(见 [document_revisions.md](document_revisions.md)) |

这正是"读 `at = seq N` 时的文档内容"这类历史查询能成立的前提:时间轴不是墙上时钟,而是**提交计数**。

## 5. 设计动机

**没有这张表会怎样**:

- 计数器只放内存,崩溃后 ID 与 seq 双双回退:新条目 id 撞旧条目、文档的历史时间点寻址错乱;
- 各表自增(如迷你版)虽然不撞行,但没有跨表统一序号,无法表达"第 N 次提交时的世界状态"。

把两个游标和业务写入放进**同一个事务**推进,是"崩溃后既不丢号、也不重号"的最简实现。

## 6. 与 Python 迷你版对应

迷你版没有这张表:五张表各用 `AUTOINCREMENT` 自增,`created_at` 用 `datetime('now')` 字符串。行内不冲突,但代价是没有全局提交序号 —— 无法按"第 N 次提交"寻址历史,各表之间也只能靠 `submission_id` 这类逻辑外键关联,没有统一时间轴。

---

### 相关文件

- `packages/durable/src/storage/sqlite/migrations.ts` —— 建表语句、初始值、TEXT 编码原因
- `packages/durable/src/storage/sqlite/storage.ts` —— `open()` / `mintId()` / `commit()` 对两个游标的读写
- [record_ids.md](record_ids.md) —— ID 铸造之后的归属登记
- [document_revisions.md](document_revisions.md) —— seq 作为版本链排序键
- [conversations.md](conversations.md) —— ID `1` 保留给 root 会话
