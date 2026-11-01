# `conversations`(会话)—— 多会话与 fork 的骨架

> TS 原版表,Python 迷你版未实现(迷你版只有五张表,全景见 [../storage-entities.md](../storage-entities.md))。本文按 TS 源码(`@earendil-works/pi-durable` 的 `packages/durable/src/storage/sqlite/`)讲这张表的结构与事务角色。

一句话定位:**"会话"这个实体本身。root 会话、从历史某一点 fork 出来的分支会话、任务拥有的子会话,每一行是一条会话的元数据。** 迷你版整库只有一条隐式的 root 时间线(entries 的 id 顺序),所以不需要这张表;TS 版支持 fork 与子代理,就必须把"会话"提升为实体。

## 1. 建表语句

见 `packages/durable/src/storage/sqlite/migrations.ts:20-27`:

```sql
CREATE TABLE conversations (
  id                    INTEGER PRIMARY KEY,
  owner_conversation_id INTEGER,
  owner_task_id         INTEGER,
  record                TEXT NOT NULL CHECK (json_valid(record))
) STRICT;
CREATE INDEX conversations_by_owner_conversation ON conversations (owner_conversation_id, id);
CREATE INDEX conversations_by_owner_task ON conversations (owner_task_id, id);
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY | 全局 ID 空间里铸造(见 [record_ids.md](record_ids.md));`1` 保留给 root 会话(`types.ts:39` 的 `ROOT_CONVERSATION_ID`) |
| `owner_conversation_id` | INTEGER | 可空 | 拥有者任务的所属会话;索引列,由 `record.owner` 冗余出来供查询 |
| `owner_task_id` | INTEGER | 可空 | 拥有者任务;任务拥有的子会话(如子代理的独立上下文)在这里挂靠 |
| `record` | TEXT | NOT NULL, json_valid | 完整 `ConversationRecord` JSON:`{ id, parent?, owner? }`,是唯一事实来源;两个 owner 列只是为查询冗余 |

三种形态的行:

| 形态 | record 长什么样 | 谁创建 |
|---|---|---|
| root 会话 | `{ id: 1 }` | `createRootConversation()`(`session/transaction.ts:285-287`),保留身份引导 |
| 普通新会话 | `{ id }` | `Transaction.createConversation()`(`transaction.ts:280-282`) |
| fork 会话 | `{ id, parent: { conversationId, at } }` | `forkConversation(parentId, at)`(`transaction.ts:289-297`)—— 从父会话 entry `at` 之前的历史分叉 |
| 任务子会话 | `{ id, owner: { conversationId, taskId } }` | `createConversation({ ownership: { kind: "task", taskId } })` |

## 3. 这张表如何支撑 fork:读路径上的父链遍历

fork 的关键设计是**复制会话记录,不复制 entries**:子会话的 `record.parent` 指向父会话和分叉点 `at`,读条目时沿父链走。

- `readEntries()`(`storage/sqlite/storage.ts:317-351`):先读本会话的条目;不够就沿 `parent` 链上溯,上限是 `parent.at`(分叉点),把分叉点之后的父会话条目挡在外面;
- `readEntry()`(`storage.ts:258-289`):校验条目确实在这条会话的祖先链视野内,防止借 fork 越权读到分叉点之后的历史;
- `findLatestHeadMarker()`(`storage.ts:291-315`):找"最近一次 head 标记"时同样跨父链回溯。

所以"会话在历史某一点的样子"完全由 `conversations.parent` + entries 的 id 范围推导出来,entries 一行不复制。

## 4. 谁写、谁读

| 方向 | 代码 | 说明 |
|---|---|---|
| 写(仅 INSERT) | `applyTableWrite()` 的 conversation 分支(`storage.ts:716-725`) | 唯一写入口;conversations **从不 UPDATE、从不 DELETE**,会话创建后元数据不变 |
| 创建/分叉(暂存) | `Transaction.createConversation()` / `forkConversation()` / `createRootConversation()`(`transaction.ts:280-341`) | 在 commit 回调里暂存 `StorageWrite`;fork 时同批次还会准备文档副本(`prepareForkDocumentCopies`) |
| 读(单条) | `Storage.conversation(id)`(`storage.ts:193-197`) | 反序列化 `record` |
| 读(扫描) | `scanConversations()`(`storage.ts:199-225`) | 按 `owner_conversation_id` / `owner_task_id` 过滤,游标分页 |
| 读(父链) | `readEntries` / `readEntry` / `findLatestHeadMarker` | 第 3 节的父链遍历 |

两条完整性纪律:

1. **fork 源保护**:同一批次里禁止改动 fork 源会话的 `current` 策略文档、禁止改动被 fork 的源文档(`#rejectForkSourceWrites()`,`transaction.ts:873-892`),否则副本与源在同一次提交里的先后语义会含糊;
2. **拥有者校验**:任务拥有的新会话,其 owner 任务必须存在、不能是 `terminal`/`completing`/abort 标记(`#validateOwners()`,`transaction.ts:853-871`)。

## 5. 在事务中的位置

conversations 只出现在 `SqliteStorage.commit()` 的原子批次里(`storage.ts:162-185`),顺序在 `checkGlobalIds()` 之后、entries 之前。一次 fork 的批次典型包含:

```
conversations: INSERT 子会话(parent 指向父会话与分叉点)
documents:     INSERT 每份 fork 副本(document.copy,源在分叉点 materialize)
entries:       子会话里新追加的条目
```

全部同事务提交:不会出现"会话建了但副本缺失"的中间态。

## 6. 设计动机

**没有这张表会怎样**:

- 只能有一条隐式时间线,无法 fork(在历史分叉点重开分支)或为子代理开独立上下文;
- "从 entry N 之前的历史出发"只能靠复制整个 transcript,存储和一致性成本都高。

它和 entries 的分工:**conversations 回答"有哪些时间线、彼此什么关系",entries 回答"某条时间线上发生过什么"**。

## 7. 与 Python 迷你版对应

迷你版没有这张表:只有一条隐式 root 会话,`entries.id` 顺序即全部历史。若要给迷你库加 fork(第 6 课式的"回到某个点重开"),最小路径就是把会话提升为这张表、给 entries 挂 `conversation_id`,读路径照第 3 节实现父链遍历。

---

### 相关文件

- `packages/durable/src/storage/sqlite/migrations.ts` —— 建表语句与索引
- `packages/durable/src/storage/sqlite/storage.ts` —— `conversation` / `scanConversations` / 父链读路径 / `applyTableWrite`
- `packages/durable/src/session/transaction.ts` —— 创建、fork、拥有者校验、fork 源保护
- [document_revisions.md](document_revisions.md) —— fork 时文档副本的内容来源
- [record_ids.md](record_ids.md) —— 会话 ID 的全局唯一性
