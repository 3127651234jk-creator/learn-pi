# `record_ids`(ID 归属)—— 全局 ID 空间的户口本

> TS 原版表,Python 迷你版未实现(迷你版只有五张表,全景见 [../storage-entities.md](../storage-entities.md))。本文按 TS 源码(`@earendil-works/pi-durable` 的 `packages/durable/src/storage/sqlite/`)讲这张表的结构与事务角色。

一句话定位:**记录每个 ID 属于哪一类记录。五种业务表共享一个 ID 计数器,这张表让"一个 ID 只属于一种类型"成为数据库里可检查的不变量。**

## 1. 建表语句

见 `packages/durable/src/storage/sqlite/migrations.ts:16-19`:

```sql
CREATE TABLE record_ids (
  id          INTEGER PRIMARY KEY,
  record_type TEXT NOT NULL CHECK (record_type IN (
    'conversation', 'entry', 'task', 'submission', 'document'
  ))
) STRICT;
```

注意 `record_ids` 本身不在五种记录类型里 —— 它是登记簿,不是被登记的记录。

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | 与五张业务表同一取值空间(都从 `durable_metadata.next_id` 铸造,见 [durable_metadata.md](durable_metadata.md)) |
| `record_type` | TEXT | CHECK 五选一 | 这个 ID 已经归属哪张表 |

## 3. 写入与校验:一个 ID 的一生

**铸造**:commit 之前,`mintId()` 从内存游标发号(`storage.ts:187-191`);ID 先写进待提交的 `StorageWrite`,此时数据库还不知道它。

**登记**:提交批次里,`applyTableWrite()` / `applyDocumentActions()` 对每个带 ID 的写入调 `claimId()`(`storage.ts:775-777`):

```sql
INSERT OR IGNORE INTO record_ids (id, record_type) VALUES (?, ?);
```

**校验**:登记之前,`checkGlobalIds()`(`storage.ts:588-608`)先逐条检查,规则分两类:

| 记录类型 | 已存在于别的表 | 同批次重复出现 |
|---|---|---|
| `conversation` / `entry` / `document` | 报错(它们是 insert-only,ID 永不换语义) | 报错(同一批次不许铸造两次) |
| `task` / `submission` | 报错 | **允许**(与自身同类型即可)—— 它们走 UPSERT,一个任务/提交的整条记录随状态推进被反复覆写 |

也就是说:conversation/entry/document 的 ID 是"一次诞生,永不复用";task/submission 的 ID 是"一次诞生,原地更新"。

## 4. 它防住什么

- **跨表覆盖**:任务和条目共用计数器,若无登记,"把 entry 的 ID 误铸成 task"会静默写坏两张表之一;有登记则提交直接被拒绝。
- **双重铸造**:同一批次里同一个 ID 出现两次(调用方 bug),立刻报错。
- **insert-only 的第二次诞生**:conversation/entry/document 不允许对已有 ID 再 INSERT;`checkGlobalIds` 在写入前就拦下。

## 5. 在事务中的位置

`record_ids` 的登记和校验都在 `SqliteStorage.commit()` 的原子批次里(`storage.ts:162-185`):先 `checkGlobalIds()`(只读检查),随各表写入逐条 `claimId()`,最后统一 COMMIT 或 ROLLBACK。不会出现"ID 登记了但行没写入"的中间态。

## 6. 设计动机

**没有这张表会怎样**:五种表各自有主键约束,单表内不会重复;但**跨表**的 ID 撞车没有任何机制能发现 —— 类型间误用会静默产生半损坏的库。把归属显式落库,冲突检测才能在提交事务里完成。

## 7. 与 Python 迷你版对应

迷你版没有这张表,也不需要:五张表各用各的 `AUTOINCREMENT`,ID 空间天然隔离,不存在跨表撞号;`tasks`/`submissions` 覆写也只发生在自己的表里。只有当多张表共享同一个计数器时(比如想用 id 直接互相关联),才需要这张登记簿。

---

### 相关文件

- `packages/durable/src/storage/sqlite/migrations.ts` —— 建表语句
- `packages/durable/src/storage/sqlite/storage.ts` —— `checkGlobalIds()` / `claimId()` / `applyTableWrite()`
- [durable_metadata.md](durable_metadata.md) —— ID 从哪个计数器来
- [conversations.md](conversations.md) —— "insert-only 一次诞生"的典型(conversations 从不 UPDATE)
