# -*- coding: utf-8 -*-
# 删除 .data/,从头再来 —— 对应 npm run clean。
import shutil
from pathlib import Path

data = Path(__file__).resolve().parent.parent / ".data"
if data.exists():
    shutil.rmtree(data)
    print("deleted {}".format(data))
else:
    print("nothing to clean")
