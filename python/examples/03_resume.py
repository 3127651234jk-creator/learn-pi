# -*- coding: utf-8 -*-
# 第 3 课:重新打开同一个 SQLite 文件,让 harness 继续死掉的进程留下的运行。
# bash 不是重放安全的,所以模型收到一条 interrupted 工具结果,由它决定下一步。
import json

from common import DB_FILE, DATA_DIR, open_agent, print_transcript

harness, root = open_agent()
last = json.loads((DATA_DIR / "last-submission.json").read_text(encoding="utf-8"))

print("reopened {}; resuming unfinished work...".format(DB_FILE))
harness.resume()  # 启动任务调度:待处理的任务从各自检查点继续

submission = harness.submission(last["id"])  # 按 id 重新取得那个 submission
settled = submission.wait()
print("submission {} is now {}\n".format(last["id"], settled.status))

print_transcript(root)
harness.close()
