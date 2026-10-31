# -*- coding: utf-8 -*-
"""迷你版 pi-durable —— 对应 TS 版的 Harness / Conversation / Submission / watchEvents。

这是整个示例的核心:一个"随时可被杀掉、重启后无损继续"的 agent 运行时。
它不实现任何聪明的东西,只实现纪律 —— 所有状态变更都先落库,再对外可见。

【库里的四种数据,各司其职】(表结构见 storage.py)

- entry(条目)     :对话记录本身。kind 有 pi.user / pi.assistant /
                    pi.tool-result / pi.system 四种,只追加、不修改。
- submission(提交):一次"递交进来的输入"的生命周期:
                    open(排队) -> running(进行中) -> done / failed。
- task(任务)      :推进一个 submission 的工作单元,也就是"检查点":
                    pi.generation(调一次模型)/ pi.tool(执行一次工具)。
- event(事件)     :只增不改的通知流,专供 UI 等观察者订阅(watch_events)。

三条铁律(与原版一致):

1. 每一步先原子提交到 SQLite,再展示给任何人 —— 进程因此是可丢弃的。
   (SQLite 事务保证:要么全部写入,要么什么都不写,不存在"写了一半"。)
2. 每个任务在每个阶段都保存检查点,重启后从最后一个检查点继续。
3. 每个 requestId 只受理一次 —— 客户端超时重试不会产生重复输入。

一次输入的流动(每一行都是一次提交,每一行崩溃都是安全的)::

  submit(input)            -> pi.user 条目
    pi.generation 任务     -> pi.assistant 条目(带工具调用)
      pi.tool 任务 x n    -> pi.tool-result 条目 x n
    pi.generation 任务     -> pi.assistant 条目(最终回答)
                           -> submission 宣告 done

解读:模型若回复"我要调工具",就插入 n 个 pi.tool 任务;每个工具结果
落库后,再插一个新的 pi.generation 任务,让模型带着结果继续作答。
如此循环,直到某次回复不再要求调工具,submission 才宣告 done。
本文件里的调度线程(scheduler)做的正是这件事:不停地推动这条流水线。
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# 三个兄弟模块:storage(SQLite 原子提交)/ models(假模型与真实模型)/ tools(工具注册表)
from .models import Models, text_of
from .storage import Storage
from .tools import ToolRegistry

# 发给模型的系统提示词(演示用:要求回答简短)
SYSTEM_PROMPT = "You are a durable agent demo. Keep answers short."


class Submission:
    """一次提交的句柄。wait() 会等到它宣告 done(或 failed)。

    注意:Submission 对象本身不保存任何状态 —— 它只是 (harness, id) 的
    薄包装。真正的状态机(open/running/done/failed)永远以 SQLite 为准,
    因此:
    - 在任何线程、甚至另一个进程里构造出的 Submission,只要 id 相同,
      指向的就是同一条数据库记录;
    - status 每次都现查数据库,读到的是"最新事实",不是创建时刻的快照。
    """

    def __init__(self, harness: "Harness", sid: int):
        self.harness = harness
        self.id = sid

    @property
    def status(self) -> str:
        """当前状态:open(排队)/ running(进行中)/ done / failed。现查现读。"""
        sub = self.harness.storage.get_submission(self.id)
        return sub["status"] if sub else "unknown"

    def wait(self, timeout: Optional[float] = None) -> "Submission":
        """阻塞直到状态变为 done/failed(或超时)。返回自身,便于链式调用。

        实现是朴素的轮询(每 50ms 查一次数据库)。对演示代码来说,这比
        条件变量 + 回调更直观,而且天然支持跨进程等待 —— 状态在库里,
        谁来查都能查到。超时后直接返回,调用方再用 status 自行判断结果。
        """
        deadline = (time.time() + timeout) if timeout else None  # None 表示无限等
        while self.status not in ("done", "failed"):
            if deadline and time.time() > deadline:
                break
            time.sleep(0.05)
        return self


class Conversation:
    """一份发言记录(transcript)。本实现只有一个 root 会话,和示例项目一致。

    会话自身没有可变状态:所谓"对话内容"就是存储里的全部条目。
    Conversation 只是暴露三个动作:submit(写入并启动)、entries(读取)、
    wait_for_idle(等待全部做完)。
    """

    def __init__(self, harness: "Harness"):
        self.harness = harness
        self.id = 1  # root —— 固定为 1;要支持多会话,在这里分配不同的 id 即可

    def submit(
        self,
        content: Any,
        request_id: Optional[str] = None,
        when_busy: Optional[str] = None,
    ) -> Submission:
        """递交一条输入,可带 requestId(幂等)与 when_busy="steer"(忙碌时引导)。

        两种递交模式:

        - 普通输入(默认):写一条 pi.user 条目,并创建 mode="input" 的
          submission,调度器会为它走完整的「生成 -> 工具 -> 生成」循环。
        - steer(引导):不当成新任务,而是塞进收件箱(inbox 文档),
          等当前 submission 的下一轮生成时让模型"顺便"看到 ——
          相当于对正在干活的 agent 插一句"对了,还要……"。

        两者都支持 request_id 幂等:同一 id 再次提交(典型场景是客户端
        超时重试),直接返回第一次创建的那个 submission,绝不产生第二条。
        """
        harness = self.harness
        if when_busy == "steer":
            # ---- steer 路径:先进收件箱,此刻不写对话条目(写入时机在消费端) ----
            with harness.storage.commit() as st:
                if request_id:
                    existing = st.find_submission_id_by_request(request_id)
                    if existing:
                        # 幂等:受理过就不再受理,重试者拿到的是同一个句柄
                        return Submission(harness, existing)
                sid = st.insert_submission("steer", request_id)
                inbox = st.get_doc("inbox", [])  # 收件箱存在 key-value 文档 "inbox" 里
                inbox.append({"submissionId": sid, "content": content})
                st.set_doc("inbox", inbox)
                st.emit("inbox_update", {"submissionId": sid, "size": len(inbox)})
                # 在 with 事务块里 return 是安全的:contextmanager 正常退出
                # (没抛异常)时照样 COMMIT,上面的写入会一起落盘生效。
            harness.kick()  # 叫醒调度线程:空闲的话它马上来收件箱兜底
            return Submission(harness, sid)

        # ---- 普通输入路径:直接成为对话的一条 pi.user 条目 ----
        with harness.storage.commit() as st:
            if request_id:
                # harness 对每个 requestId 只受理一次:重试找到的是同一个 submission
                existing = st.find_submission_id_by_request(request_id)
                if existing:
                    return Submission(harness, existing)
            sid = st.insert_submission("input", request_id)       # 状态置为 open,进入排队
            st.append_entry("pi.user", content)                   # 用户的话落进对话记录
            st.emit("submission_created", {"submissionId": sid})  # 通知观察者
            # 这三步(submission + 条目 + 事件)在同一个事务里,崩溃也不会"半有半无"
        harness.kick()  # 让调度线程立刻看到新工作,而不是等下一轮轮询
        return Submission(harness, sid)

    def entries(self, limit: int = 200) -> List[Dict[str, Any]]:
        """读取对话记录的最后 limit 条(默认 200),按写入顺序(id)排列。"""
        return self.harness.storage.list_entries()[-limit:]

    def wait_for_idle(self, timeout: Optional[float] = None) -> None:
        """等到 harness 完全空闲(没有任何在跑/排队的工作)。转发给 harness。"""
        self.harness.wait_for_idle(timeout)


class EventStream:
    """对已提交状态的订阅:先给快照,再流式推送新事件。UI 只是观察者。

    这是 pi-durable "状态在库、UI 旁观" 思想的体现:UI 不直接修改状态,
    它拿一份快照(list_entries 的结果)起家,之后只消费 events 表里追加的
    通知,就能在本地拼出与数据库一致的最新画面。观察者崩溃也不影响
    agent 本身 —— 重连后再拿一次快照即可。

    实现是最朴素的轮询:后台线程每 100ms 查一次 events_since(游标)。
    """

    def __init__(self, harness: "Harness"):
        self.harness = harness
        # 快照:创建这一刻的完整对话记录。观察者先渲染它,再等增量事件。
        self.snapshot: List[Dict[str, Any]] = harness.storage.list_entries()
        self._last_id = harness.storage.last_event_id()  # 游标:从"现在"开始往后听
        self._stop = threading.Event()                   # 置位后后台线程退出
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None

    def start(self, callback: Callable[[List[Dict[str, Any]]], None]) -> None:
        """启动后台线程,把新到的事件分批投递给 callback(参数是事件列表)。"""
        self._callback = callback

        def loop() -> None:
            # self._stop.wait(0.1) 一箭双雕:既当"关闭信号"又当"100ms 定时器"——
            # 置位时立即返回 True 退出循环;否则最多等 0.1 秒醒来轮询一次。
            while not self._stop.wait(0.1):
                events = self.harness.storage.events_since(self._last_id)
                if events:
                    self._last_id = events[-1]["id"]  # 先推进游标,再投递
                    try:
                        callback(events)
                    except Exception:
                        # 观察者的崩溃不能连累 harness 本身:吞掉异常,打印了事
                        traceback.print_exc()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止推送。退出前把最后一批事件排空,保证观察者看到完整收尾。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        # 排空停止前已提交但还没推送的事件,保证观察者看到完整的收尾
        if self._callback is not None:
            events = self.harness.storage.events_since(self._last_id)
            if events:
                self._last_id = events[-1]["id"]
                try:
                    self._callback(events)
                except Exception:
                    traceback.print_exc()


def watch_events(harness: "Harness", conversation_id: int = 1) -> EventStream:
    """打开一条事件流 —— 对应 TS 版的 ``watchEvents(harness, root.id, ctx)``。

    用法:stream = watch_events(h); stream.start(callback); ...; stream.stop()
    conversation_id 参数仅为对齐 TS 版的签名;本实现只有 root 一个会话。
    """
    return EventStream(harness)


class Harness:
    """打开一份存储、并在其上运行 agent 的对象。所有写入都经过同一道原子提交线。

    它是整个系统的心脏,由三部分组成:

    - storage  :SQLite 里的全部状态(条目/提交/任务/事件),唯一"事实来源";
    - 调度线程 :后台单线程,不断从库里挑待办任务来执行(见 _loop/_step);
    - 唤醒机制 :threading.Condition 充当"闹钟"。有新工作时 kick() 立刻
                 叫醒调度线程;没叫醒也不要紧,它最多每 0.2 秒自己醒来看一眼。

    为什么用单线程调度?agent 的工作天然是串行推进的:一个 submission 内的
    任务必须按序执行,多个 submission 也按 id 顺序公平排队。单线程让
    "同一时刻只有一处改动执行状态"成为不变量,再配合 storage.commit()
    内部的锁,彻底避免竞态条件。
    """

    def __init__(self, storage: Storage, models: Models, registry: ToolRegistry,
                 model_ref: Dict[str, str], cwd: Path):
        self.storage = storage      # 唯一事实来源(SQLite)
        self.models = models        # 模型注册表:faux(假模型)或 openrouter(真模型)
        self.registry = registry    # 可用工具集(本示例只装了 bash)
        self.model_ref = model_ref  # 用哪个模型,如 {"provider": "faux", "id": "faux"}
        self.cwd = Path(cwd)        # 工作目录(工具在此目录下执行)
        self.root = Conversation(self)  # 唯一的 root 会话

        self._wake = threading.Condition()  # 调度线程的"闹钟"
        self._closed = False                # 置位后调度线程退出
        self._active: Optional[int] = None  # 正在运行的 submission id(内存缓存)
        # 调度线程:daemon=True,主进程退出时它跟着死 —— 这没关系,
        # 因为所有进度都已落库,下个进程 resume() 就能接手。
        self._thread = threading.Thread(target=self._loop, daemon=True, name="pi-scheduler")
        self._thread.start()

    @classmethod
    def open(cls, db_file, *, models: Models, registry: ToolRegistry,
             model_ref: Dict[str, str], cwd) -> "Harness":
        """打开(或重新打开)SQLite 文件。同一个文件就是同一个会话。

        这正是"durable"的入口:进程崩溃后,用同一个 db_file 再 open 一次,
        所有条目、未完成的任务都原样躺在库里,接着跑 resume() 即可续命。
        """
        return cls(Storage(db_file), models, registry, model_ref, Path(cwd))

    # ---------- 生命周期 ----------

    def resume(self) -> None:
        """唤醒任务调度:上一个进程遗留的任务从各自检查点继续;
        已经没有待办任务的运行直接宣告完成。

        后半句为什么成立?因为"任务做完"和"状态改成 done"在同一个事务里
        落盘。所以凡是"状态还是 running、却查不出任何未完成任务"的提交,
        一定是死在了最后一次提交之后 —— 工作其实已经做完,补个 done 就行。
        """
        with self.storage.commit() as st:
            st.finalize_finished_submissions()
        self.kick()  # 叫醒调度线程,让它立刻开始接手遗留任务

    def submission(self, sid: int) -> Submission:
        """按 id 重新取得一个 submission —— 可以在不同于发送者的进程里等待它。"""
        sub = self.storage.get_submission(sid)
        if sub is None:
            raise KeyError("submission {} does not exist".format(sid))
        return Submission(self, sid)

    def kick(self) -> None:
        """叫醒调度线程(它可能正睡在 _wake 上)。

        每次产生新工作(submit、派生任务等)后都应调用;即使忘了,
        调度线程 0.2 秒的轮询兜底也能发现。
        """
        with self._wake:
            self._wake.notify_all()

    def close(self) -> None:
        """关停:置退出标志、叫醒线程、等它退出(最多 2 秒)、关闭数据库。"""
        self._closed = True
        self.kick()  # 若调度线程正睡着,立刻叫醒它,让它看到 _closed 后退出
        self._thread.join(timeout=2)
        self.storage.close()

    def wait_for_idle(self, timeout: Optional[float] = None) -> None:
        """阻塞等待,直到没有任何工作在跑或排队(或超时返回)。

        "没有工作"要同时满足四个条件:
        1. 当前没有正在执行的 submission(_active 为空);
        2. 没有排着队的普通输入(状态 open 的 input submission);
        3. 收件箱里没有等待合并的 steer;
        4. 所有 submission 都没有未完成的任务(pi.generation / pi.tool)。
        """
        deadline = (time.time() + timeout) if timeout else None
        while not self._closed:
            busy = (
                self._active is not None
                or self.storage.oldest_open_input() is not None
                or self.storage.open_steers()
                or self.storage.count_open_tasks_all() > 0
            )
            if not busy:
                return
            if deadline and time.time() > deadline:
                return
            time.sleep(0.05)  # 轮询节奏:50ms,演示代码够用且响应够快

    # ---------- 调度主循环 ----------

    def _loop(self) -> None:
        """调度线程的主循环:不停地"_step 推一步",没活干就睡一会儿。

        异常处理值得注意:_step 抛错不会杀死线程 —— 当前 submission 被标记为
        failed(这本身也是一次原子提交),调度器继续服务后续工作。
        这正是铁律 1 的好处:连"失败"也是落库的状态,而不是悬在内存里。
        """
        while not self._closed:
            try:
                worked = self._step()
            except Exception:
                traceback.print_exc()
                if self._active is not None:
                    # 出错时把正在做的 submission 标记为 failed,让等待者能观察到
                    with self.storage.commit() as st:
                        st.set_submission_status(self._active, "failed")
                self._active = None
                worked = False
            if not worked:
                # 这一步没做事:睡在条件变量上,最多 0.2 秒(留作轮询兜底)。
                # kick() 会提前叫醒我们,新工作因此能被及时处理。
                with self._wake:
                    if self._closed:
                        return
                    self._wake.wait(0.2)

    def _step(self) -> bool:
        """推进一步调度。做了事返回 True。

        每次调用做三选一:
        1. 当前有活跃 submission(_active 非空)—— 继续推进它;
        2. 否则从库里挑下一个需要干活的 submission —— 开始处理它;
        3. 连需要干活的都没有 —— 尝试把收件箱里的 steer 转成新输入(兜底)。
        """
        sid = self._active
        if sid is None:
            sid = self.storage.next_submission_needing_work()
            if sid is None:
                return self._promote_oldest_steer()  # 兜底:空闲时收件箱里的 steer 当新输入
            with self.storage.commit() as st:
                # fresh:这是刚排队的全新输入,还是上个进程留下的半途任务?
                fresh = st.get_submission(sid)["status"] == "open"
                st.set_submission_status(sid, "running")  # open -> running,占住这个提交
                if fresh:
                    # 全新输入的第一个任务:让模型看一遍对话并作答。
                    # 半途恢复(fresh=False)则不加新任务 —— 未完成的任务还在
                    # 库里,_advance 会从它们中间的第一个继续执行。
                    st.insert_task("pi.generation", sid, {"submissionId": sid})
                st.emit("submission_started", {"submissionId": sid})
            self._active = sid
        return self._advance(sid)

    def _advance(self, sid: int) -> bool:
        """推进 sid 一步:找到它下一个未完成任务并执行;没有任务了就收尾。"""
        task = self.storage.next_open_task(sid)  # 按 id 取最早的一个未完成任务
        if task is None:
            self._finish(sid)  # 任务队列空了:整个 submission 完成
            return True
        if task["type"] == "pi.generation":
            self._run_generation(sid, task)  # 调一次模型
        elif task["type"] == "pi.tool":
            self._run_tool(sid, task)        # 执行一次工具调用
        return True

    def _finish(self, sid: int) -> None:
        """收尾一个 submission:宣告 done,并释放调度器去接下一位。"""
        self._drain_inbox()  # 运行途中到达的 steer,兜底并入
        with self.storage.commit() as st:
            st.set_submission_status(sid, "done")
            st.emit("submission_done", {"submissionId": sid})
        self._active = None  # 腾出手来,_step 下一轮会挑选下一个 submission

    # ---------- 各阶段的执行(每段都是 原子提交 + 检查点) ----------

    def _run_generation(self, sid: int, task: Dict[str, Any]) -> None:
        """执行一次 pi.generation:调模型 -> 把回答(和工具调用)落库。

        注意结构:模型调用夹在两次原子提交之间。
        - 提交 1(事前检查点):记下"正要调模型",并发出 turn_start 事件。
          若进程死在这之后、回复落库之前,重启后该任务仍是 running,
          会被原样重跑 —— 调模型本身没有副作用,重跑是安全的(多花点算力)。
        - models.generate(...):唯一的"外部世界"动作,无法回滚,所以
          绝不放进事务里。
        - 提交 2(事后落盘):回答、派生的工具任务、任务完成标记,
          一次全部落库 —— 这三个事实要么同时存在,要么都不存在。
        """
        with self.storage.commit() as st:
            st.update_task(task["id"], status="running", checkpoint={"phase": "calling-model"})
            st.emit("turn_start", {"submissionId": sid})
        self._ensure_system_entry()  # 保证系统提示词条目存在且是最新的
        reply = self.models.generate(self.model_ref, self._messages(), self.registry.specs())
        with self.storage.commit() as st:
            # 模型这次说了什么,原样追加进对话记录(文本块 + 工具调用块)
            st.append_entry("pi.assistant", reply.blocks(), model=self.model_ref)
            # 模型要求调用的每个工具,各生成一个 pi.tool 任务(先记账,后执行)
            for call in reply.tool_calls:
                st.insert_task("pi.tool", sid, {"toolCall": call})
            st.update_task(task["id"], status="done")  # 本任务完成
            st.emit("message_update", {"submissionId": sid, "kind": "pi.assistant"})

    def _run_tool(self, sid: int, task: Dict[str, Any]) -> None:
        """执行一次 pi.tool。这是整个文件里崩溃恢复学问最大的地方。

        关键在任务此刻的 status:
        - pending:从没开始跑过(上一次连检查点都没提交)—— 正常执行;
        - running:上个进程死在"已提交检查点、还没落结果"之间 ——
          此时工具可能已经(部分)执行过了,能不能重跑要看工具自己的
          重放声明(replay policy,见 tools.py 的说明)。
        """
        call = task["payload"].get("toolCall", {})
        name = call.get("name", "")
        tool = self.registry.get(name)
        tool_call_id = call.get("id")

        if task["status"] == "running" and (tool is None or tool.replay != "safe"):
            # 上一个进程死在执行途中,且该工具不是重放安全的:
            # 写入 interrupted 结果,告诉模型"这个工具可能已经部分执行",由模型决定怎么办。
            # (比如 bash 里的 mkdir 半路死掉:目录可能建了也可能没建,盲目重跑有风险,
            #   所以把判断权交还给模型。未知工具名同样按"不可重放"处理。)
            result_text = "<harness> [error] Tool {} was interrupted and may have partially run </harness>".format(name)
        else:
            # 检查点:即将执行工具。这是崩溃安全的关键提交 ——
            # 执行中途死掉,重启后任务的状态是 running,恢复逻辑据此分辨"没跑过"和"跑了一半"。
            with self.storage.commit() as st:
                st.update_task(task["id"], status="running", checkpoint={"phase": "executing"})
                st.emit("tool_execution_update", {"submissionId": sid, "tool": name})
            try:
                if tool is None:
                    result_text = "[unknown tool {}]".format(name)
                else:
                    result_text = tool.run(call.get("arguments", {}))
            except Exception as exc:
                # 工具报错不炸调度器:错误文本当作工具结果交给模型,让它自行处置
                result_text = "[error] {}".format(exc)

        with self.storage.commit() as st:
            # 无论走哪条分支,结果都写成一条 pi.tool-result 条目,并用
            # toolCallId 与发起调用的 assistant 块配对 —— 模型下一轮看到的就是它。
            st.append_entry(
                "pi.tool-result",
                [{"type": "text", "text": result_text, "toolCallId": tool_call_id}],
            )
            st.update_task(task["id"], status="done")
            st.insert_task("pi.generation", sid, {"submissionId": sid})  # 下一轮:模型读结果后作答
            st.emit("tool_result", {"submissionId": sid, "tool": name})

        self._drain_inbox()  # 当前工具轮次结束:此时等待中的 steer 加入

    def _drain_inbox(self) -> None:
        """把等待中的 steer 并入对话:追加 pi.user 条目,下一轮生成时模型会看到它。

        在两处被调用:_run_tool 落完工具结果之后(一轮工具刚结束),以及
        _finish 里(submission 收尾前)。每个 steer 单独用一个事务处理:
        "弹出收件箱 + 写入对话 + 标记其 submission 为 done" 三步原子完成。
        循环到收件箱空为止,这样运行途中陆续到达的 steer 一个都不会漏。
        """
        while True:
            with self.storage.commit() as st:
                inbox = st.get_doc("inbox", [])
                if not inbox:
                    return
                item = inbox.pop(0)                          # 按到达顺序取最早的一条
                st.set_doc("inbox", inbox)                   # 先从收件箱移除
                st.append_entry("pi.user", item["content"])  # 再写进对话记录
                # steer 没有自己的任务周期:它的内容并入正在进行的对话即可
                st.set_submission_status(item["submissionId"], "done")

    def _promote_oldest_steer(self) -> bool:
        """兜底:没有任何运行时,把收件箱里的 steer 转成一次普通输入。

        steer 的语义是"在当前工作运行途中插话"。但如果递交 steer 时
        agent 根本没在干活,它就会一直躺在收件箱里没人消费 —— 所以
        调度器空闲时(_step 三选一的第 3 条)把它升级为普通输入,
        让它获得完整的「生成 -> (工具) -> 生成」生命周期。做了事返回 True。
        """
        with self.storage.commit() as st:
            inbox = st.get_doc("inbox", [])
            if not inbox:
                return False  # 收件箱也空着:真没事干
            item = inbox.pop(0)
            st.set_doc("inbox", inbox)
            st.append_entry("pi.user", item["content"])
            # 只把 mode 改成 input,状态保持 open —— _step 下一轮会像普通
            # 输入一样选中它,置为 running 并插入 pi.generation 任务。
            st.set_submission_mode(item["submissionId"], "input")
        return True

    # ---------- 消息组装 ----------

    def _ensure_system_entry(self) -> None:
        """pi.system 条目保存本请求的系统提示词和工具列表;没变化就不重复存,
        这样能让提供商的提示词缓存保持命中(与 TS 版行为一致)。

        实现是"双检":先在事务外比对(大多数时候直接返回,零开销),
        再在事务内比对一次(防止并发时重复插入)。pi.system-state 文档
        记住上一次写入的内容,作为比对基准。
        本迷你版只把系统提示词记录在案,并未拼进真正的模型请求
        (假模型用不上;真实模型层也未使用它 —— 见 models.py)。
        """
        state = {"system": SYSTEM_PROMPT, "tools": self.registry.names()}
        if self.storage.get_doc("pi.system-state") == state:
            return
        with self.storage.commit() as st:
            if st.get_doc("pi.system-state") == state:  # 事务内复查,防止并发重复写
                return
            st.append_entry("pi.system", "")  # 条目做记录;具体内容由 state 文档承载
            st.set_doc("pi.system-state", state)

    def _messages(self) -> List[Dict[str, Any]]:
        """把对话记录(全部条目)翻译成模型能理解的 messages 列表。

        映射规则:
          pi.user        -> role="user"
          pi.assistant   -> role="assistant"
          pi.tool-result -> role="toolResult"

        每次生成都是全量重建 —— 不在内存里维护对话状态,条目表就是
        对话本身。这正是"崩溃即恢复"能成立的原因:任何时刻都能从库里
        重放出同样的模型上下文,不依赖任何易失的内存状态。
        """
        msgs: List[Dict[str, Any]] = []
        for entry in self.storage.list_entries():
            kind = entry["kind"]
            if kind == "pi.user":
                msgs.append({"role": "user", "content": entry["content"]})
            elif kind == "pi.assistant":
                msgs.append({"role": "assistant", "content": entry["content"]})
            elif kind == "pi.tool-result":
                msgs.append({"role": "toolResult", "content": entry["content"]})
            # pi.system 不进消息列表:系统提示词由 pi.system-state 文档维护
        return msgs
