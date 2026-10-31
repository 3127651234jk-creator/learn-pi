# -*- coding: utf-8 -*-
"""迷你版 pi-durable —— 对应 TS 版的 Harness / Conversation / Submission / watchEvents。

三条铁律(与原版一致):

1. 每一步先原子提交到 SQLite,再展示给任何人 —— 进程因此是可丢弃的。
2. 每个任务在每个阶段都保存检查点,重启后从最后一个检查点继续。
3. 每个 requestId 只受理一次。

一次输入的流动(每一行都是一次提交,每一行崩溃都是安全的)::

  submit(input)            -> pi.user 条目
    pi.generation 任务     -> pi.assistant 条目(带工具调用)
      pi.tool 任务 x n    -> pi.tool-result 条目 x n
    pi.generation 任务     -> pi.assistant 条目(最终回答)
                           -> submission 宣告 done
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .models import Models, text_of
from .storage import Storage
from .tools import ToolRegistry

SYSTEM_PROMPT = "You are a durable agent demo. Keep answers short."


class Submission:
    """一次提交的句柄。wait() 会等到它宣告 done(或 failed)。"""

    def __init__(self, harness: "Harness", sid: int):
        self.harness = harness
        self.id = sid

    @property
    def status(self) -> str:
        sub = self.harness.storage.get_submission(self.id)
        return sub["status"] if sub else "unknown"

    def wait(self, timeout: Optional[float] = None) -> "Submission":
        deadline = (time.time() + timeout) if timeout else None
        while self.status not in ("done", "failed"):
            if deadline and time.time() > deadline:
                break
            time.sleep(0.05)
        return self


class Conversation:
    """一份发言记录(transcript)。本实现只有一个 root 会话,和示例项目一致。"""

    def __init__(self, harness: "Harness"):
        self.harness = harness
        self.id = 1  # root

    def submit(
        self,
        content: Any,
        request_id: Optional[str] = None,
        when_busy: Optional[str] = None,
    ) -> Submission:
        """递交一条输入,可带 requestId(幂等)与 when_busy="steer"(忙碌时引导)。"""
        harness = self.harness
        if when_busy == "steer":
            with harness.storage.commit() as st:
                if request_id:
                    existing = st.find_submission_id_by_request(request_id)
                    if existing:
                        return Submission(harness, existing)
                sid = st.insert_submission("steer", request_id)
                inbox = st.get_doc("inbox", [])
                inbox.append({"submissionId": sid, "content": content})
                st.set_doc("inbox", inbox)
                st.emit("inbox_update", {"submissionId": sid, "size": len(inbox)})
            harness.kick()
            return Submission(harness, sid)

        with harness.storage.commit() as st:
            if request_id:
                # harness 对每个 requestId 只受理一次:重试找到的是同一个 submission
                existing = st.find_submission_id_by_request(request_id)
                if existing:
                    return Submission(harness, existing)
            sid = st.insert_submission("input", request_id)
            st.append_entry("pi.user", content)
            st.emit("submission_created", {"submissionId": sid})
        harness.kick()
        return Submission(harness, sid)

    def entries(self, limit: int = 200) -> List[Dict[str, Any]]:
        return self.harness.storage.list_entries()[-limit:]

    def wait_for_idle(self, timeout: Optional[float] = None) -> None:
        self.harness.wait_for_idle(timeout)


class EventStream:
    """对已提交状态的订阅:先给快照,再流式推送新事件。UI 只是观察者。"""

    def __init__(self, harness: "Harness"):
        self.harness = harness
        self.snapshot: List[Dict[str, Any]] = harness.storage.list_entries()
        self._last_id = harness.storage.last_event_id()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None

    def start(self, callback: Callable[[List[Dict[str, Any]]], None]) -> None:
        self._callback = callback

        def loop() -> None:
            while not self._stop.wait(0.1):
                events = self.harness.storage.events_since(self._last_id)
                if events:
                    self._last_id = events[-1]["id"]
                    try:
                        callback(events)
                    except Exception:
                        traceback.print_exc()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
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
    """打开一条事件流 —— 对应 TS 版的 ``watchEvents(harness, root.id, ctx)``。"""
    return EventStream(harness)


class Harness:
    """打开一份存储、并在其上运行 agent 的对象。所有写入都经过同一道原子提交线。"""

    def __init__(self, storage: Storage, models: Models, registry: ToolRegistry,
                 model_ref: Dict[str, str], cwd: Path):
        self.storage = storage
        self.models = models
        self.registry = registry
        self.model_ref = model_ref
        self.cwd = Path(cwd)
        self.root = Conversation(self)

        self._wake = threading.Condition()
        self._closed = False
        self._active: Optional[int] = None  # 正在运行的 submission id
        self._thread = threading.Thread(target=self._loop, daemon=True, name="pi-scheduler")
        self._thread.start()

    @classmethod
    def open(cls, db_file, *, models: Models, registry: ToolRegistry,
             model_ref: Dict[str, str], cwd) -> "Harness":
        """打开(或重新打开)SQLite 文件。同一个文件就是同一个会话。"""
        return cls(Storage(db_file), models, registry, model_ref, Path(cwd))

    # ---------- 生命周期 ----------

    def resume(self) -> None:
        """唤醒任务调度:上一个进程遗留的任务从各自检查点继续;
        已经没有待办任务的运行直接宣告完成。"""
        with self.storage.commit() as st:
            st.finalize_finished_submissions()
        self.kick()

    def submission(self, sid: int) -> Submission:
        """按 id 重新取得一个 submission —— 可以在不同于发送者的进程里等待它。"""
        sub = self.storage.get_submission(sid)
        if sub is None:
            raise KeyError("submission {} does not exist".format(sid))
        return Submission(self, sid)

    def kick(self) -> None:
        with self._wake:
            self._wake.notify_all()

    def close(self) -> None:
        self._closed = True
        self.kick()
        self._thread.join(timeout=2)
        self.storage.close()

    def wait_for_idle(self, timeout: Optional[float] = None) -> None:
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
            time.sleep(0.05)

    # ---------- 调度主循环 ----------

    def _loop(self) -> None:
        while not self._closed:
            try:
                worked = self._step()
            except Exception:
                traceback.print_exc()
                if self._active is not None:
                    with self.storage.commit() as st:
                        st.set_submission_status(self._active, "failed")
                self._active = None
                worked = False
            if not worked:
                with self._wake:
                    if self._closed:
                        return
                    self._wake.wait(0.2)

    def _step(self) -> bool:
        """推进一步调度。做了事返回 True。"""
        sid = self._active
        if sid is None:
            sid = self.storage.next_submission_needing_work()
            if sid is None:
                return self._promote_oldest_steer()  # 兜底:空闲时收件箱里的 steer 当新输入
            with self.storage.commit() as st:
                fresh = st.get_submission(sid)["status"] == "open"
                st.set_submission_status(sid, "running")
                if fresh:
                    st.insert_task("pi.generation", sid, {"submissionId": sid})
                st.emit("submission_started", {"submissionId": sid})
            self._active = sid
        return self._advance(sid)

    def _advance(self, sid: int) -> bool:
        task = self.storage.next_open_task(sid)
        if task is None:
            self._finish(sid)
            return True
        if task["type"] == "pi.generation":
            self._run_generation(sid, task)
        elif task["type"] == "pi.tool":
            self._run_tool(sid, task)
        return True

    def _finish(self, sid: int) -> None:
        self._drain_inbox()  # 运行途中到达的 steer,兜底并入
        with self.storage.commit() as st:
            st.set_submission_status(sid, "done")
            st.emit("submission_done", {"submissionId": sid})
        self._active = None

    # ---------- 各阶段的执行(每段都是 原子提交 + 检查点) ----------

    def _run_generation(self, sid: int, task: Dict[str, Any]) -> None:
        with self.storage.commit() as st:
            st.update_task(task["id"], status="running", checkpoint={"phase": "calling-model"})
            st.emit("turn_start", {"submissionId": sid})
        self._ensure_system_entry()
        reply = self.models.generate(self.model_ref, self._messages(), self.registry.specs())
        with self.storage.commit() as st:
            st.append_entry("pi.assistant", reply.blocks(), model=self.model_ref)
            for call in reply.tool_calls:
                st.insert_task("pi.tool", sid, {"toolCall": call})
            st.update_task(task["id"], status="done")
            st.emit("message_update", {"submissionId": sid, "kind": "pi.assistant"})

    def _run_tool(self, sid: int, task: Dict[str, Any]) -> None:
        call = task["payload"].get("toolCall", {})
        name = call.get("name", "")
        tool = self.registry.get(name)
        tool_call_id = call.get("id")

        if task["status"] == "running" and (tool is None or tool.replay != "safe"):
            # 上一个进程死在执行途中,且该工具不是重放安全的:
            # 写入 interrupted 结果,告诉模型"这个工具可能已经部分执行",由模型决定怎么办。
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
                result_text = "[error] {}".format(exc)

        with self.storage.commit() as st:
            st.append_entry(
                "pi.tool-result",
                [{"type": "text", "text": result_text, "toolCallId": tool_call_id}],
            )
            st.update_task(task["id"], status="done")
            st.insert_task("pi.generation", sid, {"submissionId": sid})  # 下一轮:模型读结果后作答
            st.emit("tool_result", {"submissionId": sid, "tool": name})

        self._drain_inbox()  # 当前工具轮次结束:此时等待中的 steer 加入

    def _drain_inbox(self) -> None:
        """把等待中的 steer 并入对话:追加 pi.user 条目,下一轮生成时模型会看到它。"""
        while True:
            with self.storage.commit() as st:
                inbox = st.get_doc("inbox", [])
                if not inbox:
                    return
                item = inbox.pop(0)
                st.set_doc("inbox", inbox)
                st.append_entry("pi.user", item["content"])
                st.set_submission_status(item["submissionId"], "done")

    def _promote_oldest_steer(self) -> bool:
        """兜底:没有任何运行时,把收件箱里的 steer 转成一次普通输入。"""
        with self.storage.commit() as st:
            inbox = st.get_doc("inbox", [])
            if not inbox:
                return False
            item = inbox.pop(0)
            st.set_doc("inbox", inbox)
            st.append_entry("pi.user", item["content"])
            st.set_submission_mode(item["submissionId"], "input")
        return True

    # ---------- 消息组装 ----------

    def _ensure_system_entry(self) -> None:
        """pi.system 条目保存本请求的系统提示词和工具列表;没变化就不重复存,
        这样能让提供商的提示词缓存保持命中(与 TS 版行为一致)。"""
        state = {"system": SYSTEM_PROMPT, "tools": self.registry.names()}
        if self.storage.get_doc("pi.system-state") == state:
            return
        with self.storage.commit() as st:
            if st.get_doc("pi.system-state") == state:
                return
            st.append_entry("pi.system", "")
            st.set_doc("pi.system-state", state)

    def _messages(self) -> List[Dict[str, Any]]:
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
