# -*- coding: utf-8 -*-
"""工具层 —— 对应 TS 版的 ``CodingTools``(本示例只用 bash)。

每个工具声明一个重放策略(replay policy),这是崩溃安全的关键:

- ``replay="safe"`` 的工具在崩溃后会被重新执行。读文件这类操作,重复做是安全的。
- 其他工具在崩溃后返回 **interrupted(已中断)** 结果,由模型决定下一步。
  ``bash`` 就属于这一类:它可能已经部分执行,不能盲目重跑。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional


class Tool:
    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        run: Callable[[Dict[str, Any]], str],
        replay: str = "interrupted",
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.run = run
        self.replay = replay

    def spec(self) -> Dict[str, Any]:
        """OpenAI 兼容的 function 描述(给真实模型用)。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _sleep_to_ping(command: str) -> str:
    """cmd 没有 sleep,把 ``sleep N`` 译成等价的 ping 等待(N 秒)。"""
    return re.sub(
        r"(?<![a-z])sleep\s+(\d+)",
        lambda m: "ping -n {} 127.0.0.1 >nul".format(int(m.group(1)) + 1),
        command,
    )


def _run_bash(arguments: Dict[str, Any]) -> str:
    command = arguments.get("command", "")
    bash = shutil.which("bash")
    if bash:
        proc = subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, timeout=120
        )
    else:
        proc = subprocess.run(
            _sleep_to_ping(command),
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        out += "\n[exit code {}]".format(proc.returncode)
    return out.strip()


BASH = Tool(
    name="bash",
    description="Run a bash command and return its combined output.",
    parameters={
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The command to run."}},
        "required": ["command"],
    },
    run=_run_bash,
    replay="interrupted",  # bash 不是重放安全的
)


class ToolRegistry:
    """一组命名的工具 —— 对应 TS 版的 ``createRegistry()`` + ``registry.install()``。"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def install(self, *tools: Tool) -> None:
        for tool in tools:
            self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def specs(self) -> List[Dict[str, Any]]:
        return [tool.spec() for tool in self._tools.values()]

    def names(self) -> List[str]:
        return list(self._tools)
