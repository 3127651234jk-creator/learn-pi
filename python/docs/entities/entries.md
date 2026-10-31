# `entries`(条目)—— 会话正文

> 五张实体表之一。本文只讲这一张表;全局视角见 [../storage-entities.md](../storage-entities.md)。

一句话定位:**会话 transcript 的本体。每行一条消息,`ORDER BY id` 就是时间线;崩溃后模型"记得之前说过什么"全靠它。**

## 1. 建表语句

见 `python/pidurable/storage.py` 的 `SCHEMA` 字符串:

```sql
CREATE TABLE IF NOT EXISTS entries (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  kind    TEXT NOT NULL,
  content TEXT NOT NULL,
  model   TEXT
);
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | 全局单调递增。恢复与展示都按它排序 —— id 顺序即对话时间线 |
| `kind` | TEXT | NOT NULL | 条目种类:谁发的、什么性质,四种取值见下节 |
| `content` | TEXT | NOT NULL | JSON 序列化:要么是纯字符串,要么是块(block)列表 |
| `model` | TEXT | 可空 | JSON 模型引用 `{"provider": ..., "modelId": ...}`,**只有 `pi.assistant` 条目会写**,其余三种恒为 NULL |

这张表是**只追加**的:全程只有 `append_entry()` 一个写入口,从不 UPDATE、从不 DELETE。对话历史因此是不可篡改的事实记录,这也是重放恢复能成立的前提。

## 3. `kind` 的四种取值

| kind | 谁写入(函数) | content 长什么样 |
|---|---|---|
| `pi.user` | `Conversation.submit()` 普通路径、`_drain_inbox()`、`_promote_oldest_steer()` | 用户输入的原文(字符串或块) |
| `pi.assistant` | `_run_generation()` 的第二次提交 | `reply.blocks()`:文本块 + 0..n 个 `toolCall` 块(块里带 `name` / `arguments` / `id`) |
| `pi.tool-result` | `_run_tool()` 的收尾提交 | `[{"type": "text", "text": 结果文本, "toolCallId": 调用 id}]`;崩溃恢复分支写的是 `interrupted` 提示文本 |
| `pi.system` | `_ensure_system_entry()` | **恒为空字符串 `""`**。真系统提示词在 `documents` 表的 `pi.system-state` 里;这条空条目只负责"对话历史中此处有一条系统段"的占位 |

两个容易忽略的细节:

1. **`toolCall` 与 `toolCallId` 的配对**:assistant 条目里的 `toolCall` 块带一个 `id`;紧接着落库的 `pi.tool-result` 条目带着同一个 `toolCallId`。模型下一轮读消息时就是靠这组 id 把"我发起的调用"和"它的结果"对上的。
2. **`pi.system` 不进模型消息**。组装消息时(`harness.py` 的 `_messages()`)只有另外三种被转换;系统提示词由 `pi.system-state` 文档单独维护,这样提示词没变化时就不重复写入,保住提供商的提示词缓存命中。

## 4. 谁写、谁读

| 方向 | 代码 | 说明 |
|---|---|---|
| 写 | `Storage.append_entry(kind, content, model)`(`storage.py:80`) | 唯一写入口,把 content / model 做 JSON 序列化后插入 |
| 读(全量) | `Storage.list_entries()`(`storage.py:91`) → `Conversation.entries(limit)`(`harness.py:155`) | 按 id 升序返回,供重放、展示 |
| 读(组消息) | `Harness._messages()`(`harness.py:542`) | 每次生成前全量重建模型上下文:`pi.user → role="user"`,`pi.assistant → role="assistant"`,`pi.tool-result → role="toolResult"`,跳过 `pi.system` |
| 读(课程脚本) | `python/examples/common.py` 的重放循环 | `03_resume.py` 恢复时把条目重放给模型看 |

## 5. 在事务时间线中的位置

以第 2/3 课的运行(编号对应 `storage-entities.md` 第 3 节的 T1–T8)为例,entries 出现在:

| 事务 | 写入的条目 |
|---|---|
| T1 `submit()` | `pi.user`(用户输入) |
| T3 前的首次运行 | 空 `pi.system`(占位) |
| T4 `_run_generation()` 后 | `pi.assistant`(模型回复,model 列写入模型引用) |
| T6 `_run_tool()` 后 | `pi.tool-result`(带 toolCallId) |
| T7 `_drain_inbox()` | `pi.user`(并入的 steer) |

关键设计:每个条目都是和它对应的状态变更(任务完成、事件通知)**在同一个事务里**落盘的。比如 T4 里 `pi.assistant` 条目、n 个新 `pi.tool` 任务、generation 任务标记 done、`message_update` 事件,四笔写入一起 COMMIT —— 不会出现"回复有了但任务没记上"的中间态。

## 6. 崩溃恢复中的角色

进程死掉重启后:

1. `resume()` 接手遗留任务;
2. 调度器执行任务前,`_messages()` 从 entries 全量重放出完整的模型上下文 —— 模型看到的历史和崩溃前完全一致;
3. 崩溃时"跑了一半"的工具,其 `pi.tool-result` 由恢复逻辑补写为 `interrupted` 提示,而不是凭空消失(详见 [tasks.md](tasks.md) 的决策表)。

**没有这张表会怎样**:崩溃后模型不知道之前说过什么,只能从零开始。

## 7. 与 TS 原版对应

对应 `@earendil-works/pi-durable` 的 `storage/sqlite` 的 entries 表;`root.entries()` API 也一一对应(见 `python/README.md` 的映射表)。

---

### 相关文件

- `python/pidurable/storage.py` —— 表结构与 `append_entry` / `list_entries`
- `python/pidurable/harness.py` —— `_messages()`(条目 → 模型消息)、`_run_generation` / `_run_tool`(条目落盘点)
- `python/docs/storage-entities.md` —— 五表总览与完整时间线
