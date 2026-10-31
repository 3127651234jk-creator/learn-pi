# pi-durable 从零讲起

这个文件夹是 `@earendil-works/pi-durable` 最精简的一份导览,展示的却仍是真实的东西。
五个脚本。一个 SQLite 文件。默认使用免费假模型。没有服务器,没有 Web UI,没有云。

先读本文件,然后按顺序运行五节课程。每节课耗时不到十秒。

## 1. 问题所在

普通的 agent 循环把工作保存在进程内存里:循环调用模型,运行一个工具,再调用模型。
进程一死,工作也随之死掉。

聊天式的 `/resume` 解决不了这个问题。它恢复的只是聊天文本,恢复不了:

- 进程死掉时正在运行的是哪一个工具调用,
- 那个工具已经在磁盘上改了什么,
- 排在它后面、在队列里等待的输入是什么,
- 当时你的应用认定的事实是什么。

所以,启动 agent 的那个窗口就独占了它。合上笔记本电脑,agent 就停了。

## 2. 核心思路

pi-durable 在把每一步展示给任何人**之前**,先把这一步提交到存储。
进程因此变成可丢弃的:进程死了,你重新打开同一份存储,harness 就知道每件工作停在了哪里。

一旦一次运行能活过自己的进程,三件事就成为了可能:

- agent 可以驻留在机器上,而不是终端窗口里。
- 多个客户端可以观察并引导(steer)同一个 agent。
- agent 可以等人好几个小时,然后继续干。

整个前沿一句话概括:**agent 活得比客户端久。**

## 3. 构件

下面的每个术语都是库中真实存在的类型,五节课程里你都会见到它们。

| 构件 | 它是什么 | 它为什么存在 |
|---|---|---|
| **Storage(存储)** | 一个 SQLite 文件、一个 JSONL 目录,或内存 | 持久性的栖身之所。同一时刻,一个文件只归一个进程所有 |
| **Harness(运行框架)** | 打开一份存储、并在其上运行 agent 的对象 | 所有写入都经过同一道原子提交线 |
| **Conversation(会话)** | 一份发言记录(transcript),第一个会话就是 `root` | 每个 agent 就是一个会话 |
| **Entry(条目)** | 记录中一条不可变的记录 | `pi.user`、`pi.assistant`、`pi.tool-result`、`pi.system`、`pi.reset`、`pi.compaction` |
| **Commit(提交)** | 一次原子写入,条目、文档和任务一起写入 | harness 要么全部存下,要么什么都不存 |
| **Document(文档)** | 存放在记录旁边的带类型 JSON | agent 配置、收件箱、直播流状态、用量统计,以及你自己的状态 |
| **Task(任务)** | 一个持久状态机,每个阶段都设有检查点 | 重启后从最后一个检查点继续 |
| **Submission(提交项)** | 你递交给会话的一条输入,可附带 `requestId` | harness 对每个 `requestId` 只受理一次 |
| **Extension(扩展)** | 一组有名字的工具、提示词片段、钩子和任务 | 会话只存这个名字。agent 运行期间,代码可以更换 |
| **Watch(观察)** | 对已提交状态的订阅 | UI 只是一个观察者。迟到的加入者会拿到一份快照 |

一条输入在这些构件之间的流动方式:

```
submit(input)            → pi.user 条目
  pi.generation 任务     → pi.assistant 条目(带工具调用)
    pi.tool 任务 × n     → pi.tool-result 条目 × n
  pi.generation 任务     → pi.assistant 条目(最终回答)
                         → 该 submission 宣告 "done"
```

上面每一行都是一次提交,每个任务都保存一个检查点。这就是为什么在任意一行崩溃都是安全的。

## 4. 让崩溃变安全的那一条规则

每个工具都要声明一个重放策略(replay policy)。

- 标记为 `replay: "safe"` 的工具,崩溃之后会重新运行。读文件这种操作,重复执行是安全的。
- 其他任何工具,崩溃之后返回的是一个 **interrupted(已中断)** 结果。`bash` 就属于这一类。

interrupted 结果告诉模型:"这个工具被中断了,可能已经部分执行。"
然后由模型决定怎么办。它不会在无意间重复某个副作用。

这就是持久性的诚实边界:持久的意思是**已保存检查点**,而不是**恰好执行一次**。
付款或部署这类操作,仍然需要自己的幂等键。

## 5. 运行课程

```bash
npm install
npm run hello     # 第 1 课
npm run hello     # 再跑一遍,对比两次输出
npm run crash     # 第 2 课:进程在工具运行中途自杀
npm run resume    # 第 3 课:同一次运行接着跑完
npm run twice     # 第 4 课
npm run watch     # 第 5 课
npm run clean     # 删除 .data/,从头再来
```

这些脚本使用一个名叫 `faux` 的免费假模型,它会复读你输入的文本。如果文本以 `!` 开头,它就用这一行的其余内容运行一次 `bash` 工具。
要改用真实模型,把 `.env.example` 复制成 `.env`,设置 `PROVIDER=openrouter` 和你的密钥。

### 第 1 课:`npm run hello`

脚本打开 `.data/agent.sqlite`,发送一个问题,等待答案,然后打印所有已存储的条目。

运行两次。第二次会把第一次的条目也打印出来。这个 SQLite 文件**就是**会话本身。

```
  #7   pi.user         What is a durable agent?
  #10  pi.system
  #11  pi.assistant    Echo: What is a durable agent?
```

`pi.system` 条目保存该请求的系统提示词和工具列表。后续请求只保存变化的部分,这有助于让提供商的提示词缓存保持命中。

### 第 2 课:`npm run crash`

脚本发送 `!sleep 4 && echo FINISHED`。模型调用 `bash`,这个工具要跑四秒。
1.5 秒之后,脚本调用 `process.exit(1)`。不调用 close,不做任何清理。

什么都不会丢。用户条目、带工具调用的 assistant 条目、带检查点的工具任务,都已经躺在 SQLite 里了。

### 第 3 课:`npm run resume`

脚本打开同一个文件,调用 `harness.resume()`。调度器找到那个待处理的工具任务。
`bash` 不是重放安全的,于是任务写入一条 interrupted 结果。模型读到它,然后作答。

```
  #16  pi.user         !sleep 4 && echo FINISHED
  #19  pi.assistant    Running it. [tool bash {"command":"sleep 4 && echo FINISHED"}]
  #21  pi.tool-result  <harness> [error] Tool bash was interrupted and may have partially run </harness>
  #23  pi.assistant    Tool finished: ...
```

脚本还演示了 `harness.submission(id)`:你可以在另一个进程里等待某个 submission,而不必是发送它的那个进程。

### 第 4 课:`npm run twice`

脚本把同一个输入带着 `requestId: "greeting-1"` 发送两次。两次调用返回的是同一个 submission id。

```
first.id=25  second.id=25  same=true
```

超时后重试,或者重启后重试,消息都绝不会发两次。

### 第 5 课:`npm run watch`

脚本用 `watchEvents()` 打开一条事件流。然后它发送一个要用工具的输入,并在运行忙碌期间追加一条 **steer(引导)**。
steer 会在当前工具轮次结束后加入。你会看到 `inbox_update`,接着是 `turn_start`,然后是对 steer 的回答。

Durable Pi 的 Web UI 和终端 UI 用的正是这条流。UI 只是已提交状态的观察者,仅此而已。

## 6. 这通向哪里

Durable Pi 仓库(`../../IndyDevDan/Oct122026/durable-pi`)在这个 harness 外面套了一个小型 HTTP 服务器。
那个服务器就是 `dpi-backend`。它运行在你拥有的每台机器上,通过 `/v1` 路径和一个 bearer token 对外提供会话与 agent。

| 你在这里学到的 | 它在那里变成 |
|---|---|
| 一个 SQLite 文件 | 设备上每个会话一个文件 |
| 带 `requestId` 的 `root.submit()` | `POST /v1/sessions/:s/agents/:a/messages` |
| `watchEvents()` | 一条 SSE 流:`hello`、`snapshot`、`deltas` |
| 重新打开时的 `harness.resume()` | 设备重启后恢复每一个会话 |
| 忙碌时的 steer | Web 输入框里的 "Steer" 按钮 |
| 一条被中断的 `bash` 结果 | 同样的结果,设备重启后显示在任何客户端里 |

由此自然产生三种模式:

1. **主动式 agent(Proactive agents)。**由事件、而非人来发起运行。何时启动由廉价的代码决定,持久 agent 负责干完头一个小时的活。
2. **多客户端 agent(Multi-client agents)。**同一个 agent,你可以从浏览器、终端、脚本或另一个 agent 访问它。
3. **人在环旁(Human near the loop)。**一次审批就是一个持久任务:agent 可以等上几个小时,重启之后,你会发现同一个任务仍在等待。

## 7. 要记住的限制

- 持久不等于恰好执行一次。外部副作用需要各自的幂等保障。
- 同一时刻只有一个进程拥有存储文件。没有跨进程锁。
- 像 `bash` 这样的工具,拥有运行该进程的那个用户的全部权限。
- pi-durable 还是实验性项目。本文件夹锁定版本 1.0.3。
