# `tasks`(任务)—— 工作单元 + 崩溃检查点

> 五张实体表之一。本文只讲这一张表;全局视角见 [../storage-entities.md](../storage-entities.md)。

一句话定位:**真正"干活"的单位。一次运行的推进被切成一串小任务,每个任务的状态与检查点本身就是崩溃恢复的依据。** 这是五张表里"耐久(durable)"属性体现得最彻底的一张。

## 1. 建表语句

见 `python/pidurable/storage.py` 的 `SCHEMA` 字符串:

```sql
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  type          TEXT NOT NULL,
  status        TEXT NOT NULL,
  submission_id INTEGER NOT NULL,
  payload       TEXT NOT NULL,
  checkpoint    TEXT
);
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | 任务编号;同一 submission 内按 id 顺序执行 |
| `type` | TEXT | NOT NULL | `pi.generation`(调一次模型)或 `pi.tool`(执行一次工具调用) |
| `status` | TEXT | NOT NULL | `pending → running → done`,**只进不退** |
| `submission_id` | INTEGER | NOT NULL | 挂靠的 submission —— 五张表之间唯一的显式外键关系 |
| `payload` | TEXT | NOT NULL | JSON,任务参数,结构随 type 而定,见下节 |
| `checkpoint` | TEXT | 可空 | 阶段检查点 JSON:`{"phase": "calling-model"}` 或 `{"phase": "executing"}` |

`status` 只进不退,意味着重启后读到的每种状态都有明确含义:

- `pending`:还没轮到它;
- `running`:**上一个进程死在半路**(活着的进程会把它推到 done);
- `done`:已彻底完成。

## 3. `type` 与 `payload` 的对应

| type | payload 结构 | 干什么 |
|---|---|---|
| `pi.generation` | `{"submissionId": n}` | 把对话记录全量重放成 messages,调一次模型,把回答落库 |
| `pi.tool` | `{"toolCall": {"name": ..., "arguments": ..., "id": ...}}` | 执行一次工具调用,把结果落库 |

## 4. 任务流水线:一次输入怎么被切开

一次 input 的推进被切成一串任务,顺序执行:

```
pi.generation  →  pi.tool × n  →  pi.generation  →  ...  →  (不再调工具)
```

规则(`harness.py` 文件头与 `_run_generation` / `_run_tool`):

1. 每个 `pi.generation` 完成时,若模型回答里带了工具调用,就为每个调用插一个 `pi.tool` 任务(先记账,后执行);
2. 每个 `pi.tool` 完成时(结果已落库),插一个新的 `pi.generation` 任务,让模型带着结果继续作答;
3. 如此循环,直到某次回复不再要求调工具 —— 队列空了,submission 收尾宣告 done。

## 5. 检查点(checkpoint):崩溃安全的关键一笔

`checkpoint` 记录"正要做什么"这个事实,**在真正执行外部动作之前先提交**。以工具执行为例(`_run_tool()`,节选):

```python
with self.storage.commit() as st:
    st.update_task(task["id"], status="running", checkpoint={"phase": "executing"})
    st.emit("tool_execution_update", ...)
# ↑ 这笔提交落盘后才开始跑 bash
#   若进程死在下面这一行,重启后任务仍是 running → 走 interrupted 分支
result_text = tool.run(arguments)
```

调模型前同理,先提交 `{"phase": "calling-model"}`。而模型调用、bash 执行这些"外部世界"动作**不在任何事务里** —— 它们无法回滚,所以绝不会放进事务;崩溃后能恢复的全部信息,都来自检查点提交前后的表状态。

## 6. 崩溃后重启:两种任务各自的恢复语义

### 6.1 `pi.tool` —— 分"没跑过"和"跑了一半"

重启后按任务的 status 与工具的重放声明(`replay` policy,见 `tools.py`)分辨:

| 重启时读到的状态 | 结论 | 处理 |
|---|---|---|
| `status == "pending"` | 工具**没跑过** | 照常执行 |
| `status == "running"` 且工具 `replay != "safe"`(bash 就是) | 工具**可能跑了一半** | **不重跑**;写 `interrupted` 结果 `<harness> [error] Tool bash was interrupted and may have partially run </harness>`,让模型自己决定下一步(比如 bash 里的 mkdir 半路死掉,目录可能建了也可能没建,盲目重跑有风险) |
| `status == "running"` 且工具 `replay == "safe"` | 重复执行无害 | 直接重新执行 |

> 迷你库里唯一的工具 `bash` 是 `replay="interrupted"`;`replay="safe"` 的语义(如读文件)在 `tools.py` 注释里有说明,但示例未安装这类工具。未知工具名同样按"不可重放"处理。

### 6.2 `pi.generation` —— 重跑是安全的

死在模型调用期间:重启后任务还是 running,恢复逻辑**原样重跑整个 generation**。调模型本身没有副作用,重跑只是多花点算力。

### 6.3 fresh 检查:别给半途运行重复派活

`_step()` 领取 submission 时(`harness.py:373-382`)区分两种情况:

- `fresh = True`(状态是 open 的全新输入):插入第一个 `pi.generation` 任务;
- `fresh = False`(running 的半途运行):**不加新任务** —— 未完成的任务还在库里,`_advance()` 会从第一个未完成处继续。

## 7. 谁写、谁读

| 方向 | 代码 | 说明 |
|---|---|---|
| 插入 | `insert_task(type, submission_id, payload)`(`storage.py:183`) | 状态固定置为 pending |
| 更新 | `update_task(task_id, status, checkpoint)`(`storage.py:190`) | 状态与检查点可分开或一起改 |
| 读(调度) | `next_open_task(sid)`(`storage.py:203`) | 取该 submission 最早的一个未完成任务,驱动 `_advance()` |
| 读(空闲判断) | `count_open_tasks_all()`(`storage.py:219`) | 全库未完成任务数,`wait_for_idle()` 的条件之一 |
| 读(收尾判断) | (由 `finalize_finished_submissions()` 的子查询引用) | "有未完成任务"是 submission 能否收尾的判据 |

## 8. 在事务时间线中的位置

以第 2/3 课的运行(编号对应 `storage-entities.md` 第 3 节的 T1–T8)为例:

| 事务 | tasks 的动作 |
|---|---|
| T2 `_step()` 领取 | 插入 `pi.generation`(pending) |
| T3 `_run_generation()` 前 | 该 generation → running,checkpoint=calling-model |
| — 模型调用 | **不在事务里**;死在这里则任务保持 running,重启重跑 |
| T4 `_run_generation()` 后 | 插入 n 个 `pi.tool`(pending),generation → done |
| T5 `_run_tool()` 前 | 该 tool → running,checkpoint=executing |
| — 工具执行 | **不在事务里**;死在这里就是第 2 课的场景 |
| T6 `_run_tool()` 后 | tool → done,插入下一轮 `pi.generation` |

注意 T4 和 T6 的关键设计:**"结果落库 + 任务标记 done + 派生下一批任务"在同一个事务里**,三个事实要么同时存在,要么都不存在。

## 9. 设计动机

**没有这张表会怎样**:

- 崩溃后只能整段重来(不知道推进到哪一步),或根本无法续跑;
- 工具是否执行过完全无法判断,重跑有副作用风险,不跑又丢结果。

它和 submissions 的分工:**submissions 是"需求"与阶段,tasks 是把需求兑现成的一个个可恢复小步**;tasks 通过 `submission_id` 挂靠到 submissions。

## 10. 与 TS 原版对应

对应 `@earendil-works/pi-durable` 的 tasks 表。

---

### 相关文件

- `python/pidurable/storage.py` —— 表结构与 `insert_task` / `update_task` / `next_open_task` / `count_open_tasks_all`
- `python/pidurable/harness.py` —— `_run_generation()` / `_run_tool()`(检查点样板)、`_step()`(fresh 检查)、`_advance()`
- `python/pidurable/tools.py` —— 工具的 `replay` 重放声明
- `python/examples/02_crash.py` / `03_resume.py` —— 崩溃与续跑演示
- [entries.md](entries.md) —— interrupted 结果如何写进对话
- [submissions.md](submissions.md) —— 任务挂靠的父实体
