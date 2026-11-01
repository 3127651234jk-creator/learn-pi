# `submissions`(提交)—— 输入的受理记录

> 五张实体表之一。本文只讲这一张表;全局视角见 [../storage-entities.md](../storage-entities.md)。

一句话定位:**用户每递交一次输入就多一行,它记录"这次输入的受理与推进状态"。调度器的一切判断都围绕它;幂等由"受理前按 `request_id` 查重"实现,`request_id` 的 UNIQUE 约束是最后防线。**

## 1. 建表语句

见 `python/pidurable/storage.py` 的 `SCHEMA` 字符串:

```sql
CREATE TABLE IF NOT EXISTS submissions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT UNIQUE,
  mode       TEXT NOT NULL DEFAULT 'input',
  status     TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

## 2. 字段详解

| 列 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `id` | INTEGER | PK, AUTOINCREMENT | 句柄 `Submission.id`。跨进程也能用 `harness.submission(id)` 重新取得 |
| `request_id` | TEXT | UNIQUE, 可空 | **幂等键**。客户端超时重试时,`find_submission_id_by_request()` 找到旧行直接返回,绝不受理第二次(第 4 课 `04_idempotent.py` 的主题) |
| `mode` | TEXT | DEFAULT 'input' | `input` = 普通输入,走完整「生成 → 工具 → 生成」循环;`steer` = 忙碌时引导,先进收件箱等待并入 |
| `status` | TEXT | NOT NULL | 状态机,见下节 |
| `created_at` | TEXT | DEFAULT datetime('now') | 受理时刻 |

## 3. `status` 状态机

```
        submit()                调度器领取(_step)      正常完成(_finish)
  open ──────────────────► running ────────────────────────────► done
    │                        │
    │ steer 被并入对话        │ _loop 捕获异常
    ▼                        ▼
   done                    failed
```

| 状态 | 含义 | 转入点(代码) |
|---|---|---|
| `open` | 已受理、排队中 | `insert_submission()`(`storage.py:120`) |
| `running` | 正在推进 | `_step()` 领取时(`harness.py:376`) |
| `done` | 完成 | ① `_finish()` 正常收尾;② steer 被 `_drain_inbox()` 并入对话(它没有自己的任务周期);③ `resume()` 时 `finalize_finished_submissions()` 收尾"running 但已无待办任务"的行 |
| `failed` | 出错 | `_loop()` 的异常分支(`harness.py:348-349`)—— 注意这个失败**不发事件**,是迷你实现的简化 |

两条特殊路径:

- **steer 的生命周期很短**:进 inbox 等待;被并入对话时直接 `open → done`;若递交时系统恰好空闲,由 `_promote_oldest_steer()` 把 `mode` 改成 `input`(状态保持 open),按普通输入重新走一遍。
- **resume 收尾为什么成立**:任务完成与状态更新在同一个事务里落盘,所以"status 还是 running、却查不出任何未完成任务"的提交,一定是死在最后一次提交之后 —— 工作其实已经做完,补个 done 就行。

## 4. `mode` 的两种取值

| mode | 提交方式 | 生命周期 |
|---|---|---|
| `input` | `submit(content)` | open → running → done / failed,由 tasks 表里的 `pi.generation` / `pi.tool` 任务推动 |
| `steer` | `submit(content, when_busy="steer")` | open → done(被并入)或 mode 被改为 input(空闲兜底) |

## 5. 幂等:request_id

`request_id` 带 UNIQUE 约束,但本实现不用"撞 UNIQUE 报错再回滚"的笨办法,而是**受理前先查**:

- 普通输入路径:`Conversation.submit()`(`harness.py:143-147`)先 `find_submission_id_by_request()`,查到就直接 `return Submission(harness, existing)`;
- steer 路径同理(`harness.py:127-130`)。

重试者拿到的是**同一个 submission 句柄**,没有第二条记录被创建。这套机制让"客户端超时重试"变成无害操作 —— 第 4 课专门演示这一点。

代码依据(普通输入路径,`harness.py:142-150`,节选):

```python
with harness.storage.commit() as st:
    if request_id:
        # harness 对每个 requestId 只受理一次:重试找到的是同一个 submission
        existing = st.find_submission_id_by_request(request_id)
        if existing:
            return Submission(harness, existing)
    sid = st.insert_submission("input", request_id)
    st.append_entry("pi.user", content)
    st.emit("submission_created", {"submissionId": sid})
```

注意:真正拦住重复受理的是**插入前的查询**;`request_id` 的 `UNIQUE` 约束是兜底。当前实现没有依赖"撞唯一约束再回滚"。steer 路径在 `harness.py:125-139` 用同样的先查后插逻辑。

## 6. 谁写、谁读

| 方向 | 代码 | 说明 |
|---|---|---|
| 写(插入) | `insert_submission(mode, request_id)`(`storage.py:120`) | 状态固定置为 open |
| 写(状态) | `set_submission_status(sid, status)`(`storage.py:141`)、`set_submission_mode()`(`storage.py:144`) | 状态机与 mode 改写 |
| 写(批量收尾) | `finalize_finished_submissions()`(`storage.py:165`) | 仅 `resume()` 调用 |
| 读(调度) | `next_submission_needing_work()`(`storage.py:154`) | 找"open 的 input"或"running 且有未完任务"的 submission —— 前者是新输入,后者是上个进程的半途运行 |
| 读(空闲判断) | `oldest_open_input()`(`storage.py:147`)、`open_steers()`(`storage.py:175`) | `wait_for_idle()` 用它们 + `count_open_tasks_all()` 判断是否真的闲了 |
| 读(幂等) | `find_submission_id_by_request()`(`storage.py:127`) | 按 request_id 找旧受理 |
| 读(句柄) | `get_submission(sid)`(`storage.py:133`) → `harness.submission(id)` / `Submission.status` / `Submission.wait()` | Submission 对象是 `(harness, id)` 的薄包装,status 每次**现查数据库**,不是创建时刻的快照;跨进程也能等同一个 id |

## 7. 在事务时间线中的位置

| 事务 | submissions 的动作 |
|---|---|
| T1 `submit()` | 插入新行(open,带 request_id、mode) |
| T2 `_step()` 领取 | open → running |
| T8 `_finish()` | running → done,同事务发 `submission_done` 事件 |
| T7 `_drain_inbox()` | 被并入的 steer → done |
| 异常分支 `_loop()` | → failed |
| `resume()` | finalize_finished_submissions() 批量收尾 |

## 8. 设计动机

**没有这张表会怎样**:

- 重试会重复执行(没有幂等键);
- 不知道一次输入做到哪一步 —— 排队、进行中、完成、失败全无痕迹;
- 跨进程等待("另一个进程里等这个输入跑完")无从谈起。

它和 tasks 的分工:**submissions 回答"用户要了什么、推进到哪个阶段",tasks 回答"为了做到这件事,还剩哪些具体小步"**。tasks 通过 `submission_id` 挂靠到 submissions,是五张表之间唯一的逻辑外键式关联。注意 SCHEMA 并未声明 `FOREIGN KEY` 约束,`submission_id` 只是 `INTEGER NOT NULL` 列(见 [tasks.md](tasks.md) 的建表语句)。

## 9. 与 TS 原版对应

对应 `@earendil-works/pi-durable` 的 submissions 表。原版的会话实体是 `conversations` 表(见 [conversations.md](conversations.md)),迷你版只保留一条隐式 root 会话,所以没有这张表;submissions 行也没有 session/conversation 列。

---

### 相关文件

- `python/pidurable/storage.py` —— 表结构与全部读写方法
- `python/pidurable/harness.py` —— `submit()`、`_step()`、`_finish()`、`_loop()` 异常分支、`Submission` 句柄类
- `python/examples/04_idempotent.py` —— request_id 幂等演示
- [tasks.md](tasks.md) —— 挂靠在本表上的工作单元
- [documents.md](documents.md) —— steer 在 inbox 文档中的暂存
