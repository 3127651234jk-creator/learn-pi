# pi-durable-hello

学习示例。五个小脚本,演示 `@earendil-works/pi-durable` 能做什么。
默认运行在免费的 faux 假模型上;要换真实模型,请在 `.env` 里设置 `PROVIDER=openrouter`。

- `npm run hello`   一问一答,存入 `.data/agent.sqlite`
- `npm run crash`   启动一个耗时 4 秒的 bash 工具,1.5 秒后杀掉进程
- `npm run resume`  重新打开数据库,把这次运行跑完
- `npm run twice`   把同一个 requestId 发送两次,得到同一个 submission
- `npm run watch`   在 steer 加入运行时,流式观察事件

## 时序图:一次运行如何度过崩溃

第 2、3 课(Node 版与 Python 版行为一致)发生的事情,按时间顺序。
每次"原子提交"都是一个事务 —— 无论进程死在哪一行,已落盘的数据都完整:

```mermaid
sequenceDiagram
    participant C as 客户端脚本
    participant H as Harness
    participant S as SQLite 存储
    participant M as 模型
    participant T as bash 工具

    C->>H: submit("!sleep 4 && echo FINISHED")
    H->>S: 原子提交 1:pi.user 条目 + submission + 生成任务
    H->>M: 调用模型,带上已存储的全部条目
    M-->>H: "Running it." + bash 工具调用
    H->>S: 原子提交 2:pi.assistant 条目 + pi.tool 任务
    H->>S: 原子提交 3:工具任务检查点(executing)
    H->>T: 执行命令,要跑 4 秒
    Note over C,T: 1.5 秒后脚本杀掉自己 —— 不 close,不清理
    Note over S: SQLite 里已经存好:用户条目、工具调用、工具任务和检查点
    C->>H: 重新打开同一个文件,harness.resume()
    H->>S: 找到遗留的工具任务(状态 running)
    H->>S: 原子提交 4:interrupted 工具结果
    H->>M: 调用模型,读到 interrupted 结果
    M-->>H: "Tool finished: ..."
    H->>S: 原子提交 5:最终回答,submission 宣告 done
    C->>H: submission.wait() 返回 done
```

不要 push 到远端。用 `npm run clean` 删除 `.data/`,即可从头再来。
