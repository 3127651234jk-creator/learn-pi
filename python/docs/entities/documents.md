# `documents`(文档)—— 少量键值状态

> 五张实体表之一。本文只讲这一张表;全局视角见 [../storage-entities.md](../storage-entities.md)。

一句话定位:**key-value 存储,放不适合按时间线追加的"当前状态"**。与 entries(只追加)相反,documents 是"整体覆盖"语义 —— 同一个 key 反复写,读到的永远是最新值。本迷你实现真实用到的只有两个 key。

## 1. 建表语句

见 `python/pidurable/storage.py` 的 `SCHEMA` 字符串:

```sql
CREATE TABLE IF NOT EXISTS documents (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `key` | TEXT | PRIMARY KEY | 文档名,全局唯一 |
| `value` | TEXT | NOT NULL | JSON 序列化的文档值 |

写入用的是 UPSERT(`ON CONFLICT(key) DO UPDATE SET value = excluded.value`),所以写同一个 key 就是整值覆盖,不存在"追加"或"部分更新" —— 要改收件箱就得"读出整个数组 → 改 → 写回",而这三步必须发生在**同一个事务里**才算安全(本实现确实如此,见下文)。

## 3. 本实现用到的两个 key

### 3.1 `inbox` —— steer 的暂存收件箱

值结构:

```json
[{"submissionId": 3, "content": ...}, ...]
```

用户以 `submit(when_busy="steer")` 递交"引导"时,输入不当成新任务,而是先塞进这个数组排队,等 agent 空隙时并入对话。

| 方向 | 代码 | 动作 |
|---|---|---|
| 追加 | `Conversation.submit()` 的 steer 分支(`harness.py:132-135`) | 事务内:`get_doc("inbox", [])` → `append` → `set_doc`;同事务还插入一条 mode=`steer` 的 submission 并发 `inbox_update` 事件 |
| 弹出(运行途中) | `_drain_inbox()`(`harness.py:492-496`) | 每条 steer 单独一个事务:`pop(0)` 按到达顺序取最早的一条 → 写回数组 → 追加 `pi.user` 条目 → 该 steer 的 submission 标记 done |
| 弹出(空闲兜底) | `_promote_oldest_steer()`(`harness.py:510-514`) | 同样弹出并写入 `pi.user`,但只把该 submission 的 `mode` 改成 `input`、状态保持 open,让它按普通输入走完整生命周期 |

### 3.2 `pi.system-state` —— 系统提示词去重基准

值结构:

```json
{"system": "You are a durable agent demo. Keep answers short.", "tools": ["bash"]}
```

由 `_ensure_system_entry()`(`harness.py:523`)维护:

1. 事务外先比对一次:状态没变就直接返回,零开销;
2. 事务内再比对一次(双检,防并发重复插入);
3. 确实变了才写入:同事务里追加一条**空**的 `pi.system` entries 条目 + 更新 `pi.system-state` 文档。

设计动机:entries 表是只追加的,如果每次生成前都把系统提示词写成一条新条目,对话历史会迅速膨胀,而且提供商的提示词缓存(prompt cache)永远不命中。把"当前提示词状态"放进可覆盖的 documents,entries 里只留一条占位空条目 —— 状态没变就什么都不写,缓存得以命中。

## 4. 谁写、谁读

| 方法 | 代码位置 | 语义 |
|---|---|---|
| `get_doc(key, default)` | `storage.py:107` | 按名读取,反序列化 JSON;不存在返回 default |
| `set_doc(key, value)` | `storage.py:111` | UPSERT 覆盖写,JSON 序列化 |

所有对 inbox 的读-改-写都包在 `with storage.commit()` 事务里,所以并发或崩溃都不会把数组写坏。

## 5. 在事务时间线中的位置

| 事务 | documents 的动作 |
|---|---|
| steer 递交(`submit(when_busy="steer")`) | inbox 追加一条 |
| T3 首次运行前 | `pi.system-state` 首次写入 |
| T7 `_drain_inbox()` | 每弹一条 steer,写回一次 inbox |
| 空闲兜底 `_promote_oldest_steer()` | 同上 |

## 6. 设计动机

**没有这张表会怎样**:

- steer 消息没有暂存处,忙碌时递交的引导会直接丢失;
- 系统提示词反复以新条目入库,缓存失效、历史膨胀。

它和 entries 的分工可以概括为:**"发生过什么"按时间线追加(entries),"现在是什么状态"按键值覆盖(documents)**。

## 7. 与 TS 原版对应

| 本库 key | TS 原版语义 |
|---|---|
| `documents.inbox` | steer 收件箱 |
| `documents.pi.system-state` | 系统提示词去重 / 缓存命中语义 |

---

### 相关文件

- `python/pidurable/storage.py` —— 表结构与 `get_doc` / `set_doc`
- `python/pidurable/harness.py` —— `submit()` steer 分支、`_drain_inbox()`、`_promote_oldest_steer()`、`_ensure_system_entry()`
- [entries.md](entries.md) —— `pi.system` 占位条目与 `pi.system-state` 的配合
- [submissions.md](submissions.md) —— steer submission 的生命周期
