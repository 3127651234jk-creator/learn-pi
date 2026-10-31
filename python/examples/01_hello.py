# -*- coding: utf-8 -*-
# 第 1 课:一问一答,并且已经落盘。
# 运行两次:第二次也会打印第一次的条目,因为 SQLite 文件就是会话本身。
from common import DB_FILE, open_agent, print_transcript

harness, root = open_agent()

submission = root.submit("What is a durable agent?")
settled = submission.wait()
print("submission {} is {}".format(submission.id, settled.status))

print("\nEverything stored in {}:".format(DB_FILE))
print_transcript(root)
harness.close()
