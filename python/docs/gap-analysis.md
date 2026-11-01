# 迷你版与 TS 原版的差距清单 —— `python/pidurable` 缺了什么

本文回答一个问题:**`python/pidurable` 对照 `@earendil-works/pi-durable` 的源码,缺了哪些内容?**

先说结论:

- **缺很多,但绝大多数是刻意的教学取舍。** 迷你版的目标是让五节课用纯标准库跑出同样的"耐久"效果,不是复刻全部功能。
- 体量对比:迷你版约 **870 行有效代码**(`harness.py` 564 + `storage.py` 245 + `models.py` 192 + `tools.py` 106 + `__init__.py` 27,含空行约 1,130 行);TS 原版 `packages/durable/src` 约 **18,200 行**(不含空行),比例约 **1:21**。
- 另有三处不是"缺失"而是"**语义偏离**":同名的东西在两边的含义不同,对照源码时最容易踩坑,见第 3 节。

## 1. 迷你版保留了什么(主线是完整的)

这些是"耐久"概念的本体,迷你版一行不少:

| 核心语义 | Python 落点 |
|---|---|
| 一次状态变更 = 一个 SQLite 事务,先落盘后可见 | `storage.py:65-76` 的 `commit()`(`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`) |
| 任务检查点 + 崩溃恢复,`interrupted` 语义与 replay policy | `harness.py:434-480`(`_run_tool`)、`tools.py` 的 `replay` 声明 |
| `request_id` 幂等受理 | `harness.py:143-147`(受理前查重,UNIQUE 约束兜底) |
| steer 收件箱与空闲兜底 | `harness.py:482-499`(`_drain_inbox`)、`harness.py:501-519`(`_promote_oldest_steer`) |
| resume 收尾推理("没有待办任务 = 已经做完") | `harness.py:274-284` → `storage.py:165-173` |

对照读源码时,上面五条在 TS 侧的对应物分别是 `storage/sqlite/storage.ts` 的 `commit()`、`harness/tool.ts`、`session/transaction.ts` 的幂等与受理、`harness/inbox.ts`、恢复逻辑 —— 概念一一对应,复杂度不同。

## 2. 分层缺失清单

### 2.1 总览

| 层 | Python 迷你版 | TS 原版 |
|---|---|---|
| 表结构 | 5 张 | 9 张(见 [storage-entities.md](storage-entities.md) 的索引表) |
| 会话 | 隐式 root 一个 | conversations 表:多会话、fork、任务子会话 |
| 事务 | `with commit()` 块里边读边写 | `Transaction` 暂存对象 + 组装/采纳流水线 |
| 任务 | 两种硬编码类型,三态 | 定义式任务,五态机,任务图调度 |
| submissions | open→running→done/failed | queued→placed→done/unanswered |
| documents | 单 key 整值覆盖 | 地址化 + 多化身 + 版本链 |
| 工具 | bash 一个 | read/write/edit/bash + 基础设施 |
| 模型 | faux + OpenRouter 直连 | 独立 provider 体系(`packages/ai`) |
| 观察 | 轮询 events 表 | publication 推送 + 视图翻译 |
| 其他 | — | 压缩、截断、多后端、一致性测试 |

### 2.2 存储层(`storage.py` 245 行 vs `storage/sqlite/` 约 1,100 行 + `jsonl/` + `memory.ts`)

缺的不是"几张表",而是表背后的一套机制:

- **没有全局游标**(缺 `durable_metadata`):每张表各自 `AUTOINCREMENT`,没有跨表统一的提交序号 `seq`,因此无法表达"第 N 次提交时的世界状态";
- **没有全局 ID 空间**(缺 `record_ids`):五张表 ID 互不相干,也就不需要归属登记;
- **没有 schema 迁移**(缺 `durable_schema`):每次 `open()` 直接 `executescript(SCHEMA)`(`storage.py:62`),给已有库加列没有升级路径;
- **entries 没有 `head` 标记、`commit_seq`、`byTaskId`**,也没有 `defineEntry()` 类型化条目;
- **存储后端只有 SQLite 一条路**;TS 侧 `Storage` 是接口,另有 jsonl(777 行)和内存(757 行)两个后端 + 一致性测试。

### 2.3 会话与事务层(完全没有对应物)

这是 TS 侧最精巧的部分,Python 直接用"事务块里边读边写"替代:

- **没有 `Transaction`/`Tx` 暂存对象**(`session/transaction.ts`,954 行):TS 的 commit 回调拿到 `tx`,所有写入先暂存,回调结束后组装成原子批次;写后读被禁止(`ReadAfterWrite`);提交失败 `discard()`,不可判定的失败会把 Session 标记为 poisoned;
- **没有 Session 抽象**(`session/session.ts`,542 行):`subscribeCommits()` publication、close 生命周期、提交采纳(adopt)都不存在;
- **没有 fork**:无 `forkConversation`、无文档副本、无父链读历史 —— 迷你版只有一条隐式 root 时间线(`harness.py:101` 固定 `id = 1`);
- **没有子代理**(foreground/background subagent)。

### 2.4 任务系统(两种硬编码类型 vs 定义式任务框架)

- **没有 `defineTask()`**:TS 的任务由定义描述(initial checkpoint、phase 序列、版本、结果类型),`tasks.ts` 本身只有 8 行薄壳,语义在 `types.ts` 的 `TaskDefinition` 和 `harness/define.ts`;Python 的 `pi.generation`/`pi.tool` 是写死在 `harness.py` 里的;
- **状态机砍到三态**:`pending → running → done`(`storage.py:183-201`);TS 是五态 `pending/running/waiting/completing/terminal`(`migrations.ts:41`),外加 `abort_requested`、`background`;
- **调度器是单线程 FIFO**(`harness.py:334-404`,约 70 行主循环):一个 submission 内按 id 顺序执行,多个 submission 公平排队;TS 的 `scheduler.ts`(1,250 行)处理 ownership、waiting、abort、后台任务、任务图(`task-graph.ts`)。

### 2.5 文档系统(单 key 覆盖 vs 化身 + 版本链)

迷你版的 `documents` 表(`storage.py:27-30`)只有两个 key(`inbox`、`pi.system-state`),语义是"当前值整体覆盖"。TS 的文档体系包括:三种 scope(session/conversation/task)、family/key 寻址、`history: full/latest`、多化身(created_at/retired_at)、base/delta 版本链(`document_revisions`)、定义版本迁移、fork 副本、`checkpointWhen` 快照谓词。逐表讲解见 [entities/documents.md](entities/documents.md)、[entities/document_revisions.md](entities/document_revisions.md)、[entities/conversations.md](entities/conversations.md)。

### 2.6 工具层(106 行 vs `tools/` 目录)

- 只移植了 **bash** 一个工具;TS 的 `CodingTools` 有 read/write/edit/bash(还有 edit-diff、image、路径工具、文件变更队列);
- **没有 `ExecutionEnv` 抽象**:Python 直接 `subprocess.run`(`tools.py:54-73`);TS 把"在哪执行、怎么解码输出"收进 `env/` 与 `tools/env.ts`;
- replay policy 简化成两档(`safe` 与"其余一律 interrupted");TS 工具还有多阶段提交生命周期(`harness/tool.ts`,472 行)。

### 2.7 模型层(192 行 vs `packages/ai` 独立包)

`models.py` 复刻了课程需要的最小面:echo 式 faux(`models.py:68-88`)+ OpenRouter 直连(`models.py:137-176`)。TS 侧的模型体系在独立的 `packages/ai`:多 provider、流式输出、usage 记账、提示词缓存、图像归一化 —— 这些是工程复杂度,不是"耐久"概念的一部分。

### 2.8 观察层(轮询 vs 推送)

- `EventStream`(`harness.py:164-217`)每 100ms 轮询 events 表;TS 是 `subscribeCommits()` 同步 publication + `watchEvents()`(`harness/events.ts`,403 行)把已提交 publication 翻译成 agent 事件,配 `ConversationView`(`harness/view.ts`)做增量视图;
- 注意:**events 表本身是 Python 自加的**,TS 没有这张表(见第 3 节第 1 条)。

### 2.9 其他缺失(不影响五节课)

| 缺失 | TS 落点 | 一句话说明 |
|---|---|---|
| 上下文压缩 | `harness/compaction.ts`(418 行) | 长对话的摘要压缩 |
| 上下文截断 | `truncate.ts`(205 行) | 条目超限时的裁剪 |
| 错误体系 | `errors.ts` | `StorageRejected`(保证没落库)与普通错误(不可判定)的区分 |
| inbox 子系统 | `harness/inbox.ts`(120 行) + submissions 两段式 | Python 用 documents.inbox 数组近似 |
| 一致性测试 | `src/testing/` | 多后端共享的 conformance 套件与基准 |

## 3. 三处"语义偏离"——不是缺失,是同名不同义

对照源码时最容易踩的坑:

### 3.1 events 表是 Python 自加的

TS 原版**没有** events 表:观察者靠 `subscribeCommits()` 的内存 publication,重连重新取快照。Python 把事件持久化成表,换来跨进程、跨重启的增量消费(`events_since(游标)`),游标就是库里的行号。详见 [entities/events.md](entities/events.md) 第 8 节。

### 3.2 同名状态机,取值不同义

| 表 | Python 取值 | TS 取值 |
|---|---|---|
| `submissions.status` | `open / running / done / failed` | `queued / placed / done / unanswered` |
| `tasks.status` | `pending / running / done` | `pending / running / waiting / completing / terminal` |

两边都有"受理"概念,但 TS 把"排队(queued)"和"已落到对话时间线(placed)"分开,Python 把它们合并进 `open`。按名字直接映射会得出错误结论(例如 TS 的 `queued` ≠ Python 的 `running`)。Python 的 `failed` 在 TS 五态机里没有对应 —— 任务失败由 checkpoint 和 terminal outcome 表达,submission 没有 failed 态。

### 3.3 documents 的抽象层级完全不同

Python 的 `set_doc` 是"读出整个值 → 改 → 写回",正确性依赖这三步发生在**同一个事务里**(`harness.py` 的 inbox 处理正是如此);TS 把同一需求做成了带版本冲突检测的 delta 写入:stale delta 会在 `checkDocumentActions()` 被拒(`StorageRejected`),不需要靠事务边界保护读-改-写。换句话说,**Python 用事务纪律解决的问题,TS 用版本纪律解决** —— 这是两版最值得对照思考的设计差异。

## 4. 值得补的与不值得补的

判断标准:能不能撑起"新的一课",以及它是不是"耐久"概念的一部分。

**值得补(按性价比排序):**

1. **第二档 replay 工具**:给 `tools.py` 加一个 `replay="safe"` 的只读工具(如 `read`),一行声明就能让 interrupted 分支有对照面 —— 这是崩溃恢复语义里现成的"另一半"。TS 对应物:`tools/read.ts`。
2. **fork 会话**:引入 `conversations` 表(含 `parent: {conversationId, at}`)+ 读路径父链遍历 + 文档副本。这是把"单时间线"升级成"多时间线"的最小闭环,也顺带回答了 `document_revisions` 为什么存在。实现路径已写在 [entities/conversations.md](entities/conversations.md) 第 7 节。

**不值得补:**

- provider 体系、流式、usage 记账(`packages/ai` 的全部)—— 工程复杂度,与耐久无关;
- compaction / truncate —— 长对话问题,五节课触发不了;
- 任务图调度、waiting/abort/后台任务 —— 单线程 FIFO 已能表达"推进可恢复",引入任务图只会淹没主线;
- 多存储后端 —— SQLite 后端是课程的载体,jsonl/memory 只是同一接口的另两个实现。

## 5. 相关文件

- `python/pidurable/*.py` —— 迷你版全部源码
- `packages/durable/src/` —— TS 原版(`D:\work\pi` 仓库);行数口径为不含空行
- [storage-entities.md](storage-entities.md) —— 五张迷你表的全景与 TS 9 张表的索引
- `entities/` —— 各实体逐表详解(含每篇末尾的"与 Python 迷你版对应"一节)
