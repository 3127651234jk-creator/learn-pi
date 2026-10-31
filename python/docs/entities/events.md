# `events`(事件)—— 观察者事件流

> 五张实体表之一。本文只讲这一张表;全局视角见 [../storage-entities.md](../storage-entities.md)。

一句话定位:**只追加的通知日志,专供 UI 等观察者订阅(第 5 课的 `watch_events()`)。核心纪律是"先落盘,再被读到" —— 观察者只是已提交状态的旁观者,永远看不到未提交的数据。**

## 1. 建表语句

见 `python/pidurable/storage.py` 的 `SCHEMA` 字符串:

```sql
CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  type    TEXT NOT NULL,
  payload TEXT NOT NULL
);
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | 观察者拿它当游标:`events_since(last_id)` 只取增量 |
| `type` | TEXT | NOT NULL | 事件种类,八种,见下节 |
| `payload` | TEXT | NOT NULL | JSON 载荷,主要内容是 submissionId 及相关上下文 |

与 entries 一样是**只追加**表:全程只有 `emit()` 一个写入口,从不改、从不删。但和 entries 的分工完全不同 —— entries 是"对话事实"(供模型消费),events 是"通知"(仅供观察者消费),事件不参与任何控制流决策。

## 3. 八种事件及产生点

| type | 产生点(代码) | payload 要点 |
|---|---|---|
| `submission_created` | `submit()` 普通路径(`harness.py:150`) | submissionId |
| `inbox_update` | `submit()` 的 steer 分支(`harness.py:135`) | submissionId、收件箱大小 |
| `submission_started` | `_step()` 领取 submission(`harness.py:382`) | submissionId |
| `turn_start` | `_run_generation()` 的事前检查点提交(`harness.py:422`) | submissionId |
| `message_update` | `_run_generation()` 的回复落盘提交(`harness.py:432`) | submissionId、kind=`pi.assistant` |
| `tool_execution_update` | `_run_tool()` 的事前检查点提交(`harness.py:459`) | submissionId、工具名 |
| `tool_result` | `_run_tool()` 的结果落盘提交(`harness.py:478`) | submissionId、工具名 |
| `submission_done` | `_finish()`(`harness.py:403`) | submissionId |

一个简化之处:`_loop()` 的异常分支把 submission 置为 `failed` 时**不发事件** —— 等待者通过轮询 status 才能观察到失败,这是迷你实现留的口子。

## 4. 观察协议:快照 + 增量

`EventStream`(`harness.py:164`)实现了"UI 只是观察者"的思想:

1. **先给快照**:创建 `EventStream` 这一刻,拿 `list_entries()` 的完整对话记录(`stream.snapshot`),观察者先渲染它;
2. **再流增量**:游标 `last_event_id` 初始化为 `last_event_id()`(即"从现在开始听"),后台线程每 100ms 查一次 `events_since(游标)`,新事件分批投递给 callback;
3. **投递前先推进游标**,再调用回调;回调抛异常只打印不上抛 —— 观察者崩溃不能连累 harness 本身;
4. `stop()` 退出前排空最后一批已提交未推送的事件,保证看到完整收尾。

重连也很简单:观察者崩溃后再连一次,重新拿一份快照即可,不需要任何"错过的事件补发"机制。

## 5. 谁写、谁读

| 方向 | 代码 | 说明 |
|---|---|---|
| 写 | `emit(type, payload)`(`storage.py:227`) | 唯一写入口;**总是与对应的状态变更在同一个事务里**,所以"状态变了但事件没发"不可能发生 |
| 读(游标基准) | `last_event_id()`(`storage.py:233`) | 当前最大事件 id |
| 读(增量) | `events_since(last_id)`(`storage.py:237`) | 取 id 大于游标的所有事件,按序返回 |

## 6. 在事务时间线中的位置

每个事务里,事件和它描述的状态变更一起 COMMIT:

| 事务 | 同事务发出的事件 |
|---|---|
| T1 `submit()` | `submission_created`(普通)或 `inbox_update`(steer) |
| T2 `_step()` 领取 | `submission_started` |
| T3 `_run_generation()` 前 | `turn_start` |
| T4 `_run_generation()` 后 | `message_update` |
| T5 `_run_tool()` 前 | `tool_execution_update` |
| T6 `_run_tool()` 后 | `tool_result` |
| T7 `_drain_inbox()` | (不发事件) |
| T8 `_finish()` | `submission_done` |

这保证了观察者视角的一致性:**看到 `tool_result` 事件时,那条 `pi.tool-result` 条目一定已经可读**。

## 7. 设计动机

**没有这张表会怎样**:UI / 测试脚本无法在不碰 harness 内部状态的前提下跟随进度 —— 只能去轮询 entries 或自己解析数据库,而"谁在动状态"的边界就破了。

它体现的是 pi-durable 的分层原则:**状态在库,UI 旁观**;harness 对观察者的全部承诺只有两条 —— 先提交、后可见;只追加、可增量消费。

## 8. 与 TS 原版对应

对应 `@earendil-works/pi-durable` 的 events 表与 `watchEvents()` API。

---

### 相关文件

- `python/pidurable/storage.py` —— 表结构与 `emit` / `last_event_id` / `events_since`
- `python/pidurable/harness.py` —— `EventStream` / `watch_events()`,以及各 `emit()` 调用点
- `python/examples/05_watch.py` —— 订阅事件的课程脚本
- [entries.md](entries.md) —— 事件描述的事实主体
- [tasks.md](tasks.md) —— 检查点提交与事件同事务落盘
