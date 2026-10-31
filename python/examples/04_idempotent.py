# -*- coding: utf-8 -*-
# 第 4 课:同一个 requestId 只受理一次。重试(或重启)找到的是同一个 submission。
from common import open_agent

harness, root = open_agent()
draft = {"type": "input", "content": "Say hi once.", "requestId": "greeting-1"}

first = root.submit(draft["content"], request_id=draft["requestId"])
second = root.submit(draft["content"], request_id=draft["requestId"])
print("first.id={}  second.id={}  same={}".format(first.id, second.id, first.id == second.id))

settled = first.wait()
print("status: {}".format(settled.status))
harness.close()
