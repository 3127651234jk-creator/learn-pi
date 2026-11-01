# `document_revisions`(文档版本链)—— base 快照 + delta 增量

> TS 原版表,Python 迷你版未实现(迷你版只有五张表,全景见 [../storage-entities.md](../storage-entities.md))。本文按 TS 源码(`@earendil-works/pi-durable` 的 `packages/durable/src/storage/sqlite/`)讲这张表的结构与事务角色。

一句话定位:**文档内容随提交序号演化的历史。`documents` 表记"化身"(地址与生卒),这张表记"某个化身的内容是怎么一步步变过来的"** —— 它是"按时间点读文档"和 fork 文档副本的基础。

## 1. 建表语句

见 `packages/durable/src/storage/sqlite/migrations.ts:76-84`:

```sql
CREATE TABLE document_revisions (
  document_id INTEGER NOT NULL,
  seq         INTEGER NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('base', 'delta')),
  version     INTEGER NOT NULL,
  content     TEXT NOT NULL CHECK (json_valid(content)),
  PRIMARY KEY (document_id, seq)
) STRICT;
CREATE INDEX document_revisions_by_kind
  ON document_revisions (document_id, kind, seq DESC);
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `document_id` | INTEGER | 联合主键 | 属于哪个文档**化身**(不是逻辑地址 —— 同一地址退役后重新创建会得到新的 document_id,版本链不跨化身) |
| `seq` | INTEGER | 联合主键 | 写入这次内容时的全局提交序号(来自 `durable_metadata.next_seq`,见 [durable_metadata.md](durable_metadata.md)) |
| `kind` | TEXT | 'base' 或 'delta' | `base` = 完整快照;`delta` = 在上一个版本之上应用的操作(chord ops) |
| `version` | INTEGER | NOT NULL | 文档定义的版本号;`delta` 必须与它所基于的 base 同版本,版本变化必须落新 base(定义迁移) |
| `content` | TEXT | NOT NULL, json_valid | `base` 存 JSON 值;`delta` 存操作列表 |

`(document_id, seq)` 主键意味着**一个提交批次对一个文档最多写一条版本** —— 这由 `prepareDocumentActions()` 强制:同一批次对同一文档只允许一个内容命令(create/copy/change 互斥,`storage.ts:610-655`)。

## 3. 读:从 base 重放到任意时间点

`materializeDocument()`(`storage.ts:542-577`)把版本链重放成值:

1. 找 `seq <= 目标点` 的最新 `base`;
2. 按序应用其后的所有 `delta`;
3. 途中遇到版本与 base 不符的 delta,报错 `crosses a stored version boundary without a base` —— 版本断层的链条不可重放,直接失败而不是猜。

"目标点"可以是 `'current'`(最新)或某个提交序号 `at`,这就是文档的历史读取。因为要连续三次查询(化身记录 + base + deltas),`document()` 把它们包在一个数据库事务里,保证看到同一份已提交状态(`storage.ts:469-473`)。

## 4. 写:什么时候存 base,什么时候存 delta

提交批次里 `applyDocumentActions()` 落版本(`storage.ts:835-848`)。一个 `document.change` 默认存 delta,两种情况升级为 base:

| 情形 | 代码 | 原因 |
|---|---|---|
| 文档定义版本变化(迁移) | `planDocument()`:`loaded.storedVersion < version` 时强制 base(`session/transaction.ts:998-1004`) | 旧版本形状的 delta 无从应用 |
| `checkpointWhen` 谓词要求 | 组装阶段最后判定(`transaction.ts:833-840`) | 定义作者可以按值/操作量决定"到这里就拍快照",控制链长 |

**fork 复制**也走这张表:`document.copy` 在批次里对源文档按分叉点 `at` 做 materialize,结果物化成新化身的第一个 base(`storage.ts:786-806`)—— 历史"到过"的状态就这样被复制成新链的起点。

## 5. 回收:不是所有文档都留全历史

`documents.record.history` 与 scope 决定版本链去留(`isCurrentOnly()`,`storage.ts:110-111`):

| 文档 | 版本链策略 |
|---|---|
| 会话域且 `history: "full"` | 保留完整 base/delta 链 —— 支撑"回到历史某一点"的 fork 与读取 |
| 其余(current-only) | 写入新 base 时**删除**旧版本链(`storage.ts:836-838`);文档退役时再删一次(`storage.ts:860-862`) |

也就是说,这张表只为"需要历史"的文档保留历史;普通键值式文档在存储层不会膨胀。

## 6. 设计动机

**没有这张表会怎样**:

- 文档只有"现在是什么",没有"第 N 次提交时是什么" —— fork 到历史点、审计、按旧版本重放全部不成立;
- 高频小改动只能每次存全量(或完全没有历史)。

base/delta 的组合是标准取舍:delta 让连续小改动只存差量,checkpoint 谓词让链长可控。

## 7. 与 Python 迷你版对应

迷你版没有这张表:`documents.value` 就是当前值的整体覆盖(UPSERT),没有历史、没有 delta、没有定义版本迁移。因此迷你版的 documents 不支持"按时间点读"与 fork —— 这正是迷你实现砍掉 `conversations` fork 能力时一起砍掉的另一半(见 [conversations.md](conversations.md) 第 7 节)。

---

### 相关文件

- `packages/durable/src/storage/sqlite/migrations.ts` —— 建表语句与索引
- `packages/durable/src/storage/sqlite/storage.ts` —— `materializeDocument()` / `applyDocumentActions()` / 版本链回收
- `packages/durable/src/session/transaction.ts` —— base/delta 决策(`planDocument()`、`checkpointWhen`)
- [documents.md](documents.md) —— 迷你版 documents 的对应物(只有"当前值")
- [conversations.md](conversations.md) —— fork 如何消费这张表
