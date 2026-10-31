# -*- coding: utf-8 -*-
# 第 5 课:UI 只是已提交状态的观察者。这里在 steer 加入运行时流式观察事件。
import time

from common import open_agent
from pidurable import watch_events

harness, root = open_agent()

stream = watch_events(harness, root.id)
print("snapshot: {} entries already stored".format(len(stream.snapshot)))


def on_events(events):
    for event in events:
        if event["type"] in ("message_update", "tool_execution_update"):
            continue  # 太啰嗦
        print("  event {}".format(event["type"]), flush=True)


stream.start(on_events)

run = root.submit("!sleep 2 && echo first")
time.sleep(0.5)  # 让运行先明显忙起来
root.submit("Also say thanks.", when_busy="steer")  # 在当前工具轮次之后加入
run.wait()
root.wait_for_idle()

stream.stop()
harness.close()

