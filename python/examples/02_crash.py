# -*- coding: utf-8 -*-
# 第 2 课:发起一次要调用 4 秒 bash 工具的运行,然后在 1.5 秒后杀掉进程。
# 这次运行不会丢:它的状态都在 SQLite 里 —— 用户条目、工具调用、工具任务及其检查点。
import json
import os
import threading
import time

from common import DATA_DIR, open_agent

harness, root = open_agent()

submission = root.submit("!sleep 4 && echo FINISHED", request_id="crash-demo-1")
(DATA_DIR / "last-submission.json").write_text(json.dumps({"id": submission.id}))
print("submitted {}; the bash tool is now running for 4 s".format(submission.id), flush=True)


def kill() -> None:
    print("1.5 s passed. Killing the process mid-tool. No close(), no cleanup.", flush=True)
    os._exit(1)  # 不做任何清理,直接死


threading.Timer(1.5, kill).start()
time.sleep(10)  # 主线程保持存活,等定时器来杀
