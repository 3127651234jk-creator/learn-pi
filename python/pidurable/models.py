# -*- coding: utf-8 -*-
"""模型层 —— 对应 TS 版的 ``@earendil-works/pi-ai``。

faux(免费假模型)与 TS 版 ``installFaux`` 逐条对应:

- 上一条消息是工具结果 → 回复 "Tool finished: ..."
- 最近一条用户文本以 ``!`` 开头 → 回复 "Running it." 并调用一次 bash 工具
- 其余 → 原样复读 "Echo: ..."

OpenRouter 是可选的真实模型(OpenAI 兼容接口),用标准库 urllib 直连,无第三方依赖。
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def text_of(content: Any) -> str:
    """把条目内容压成一行文本 —— 对应 setup.ts 的 ``textOf()``。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text") or "")
        elif kind == "toolCall":
            args = json.dumps(block.get("arguments", {}), ensure_ascii=False, separators=(",", ":"))
            parts.append("[tool {} {}]".format(block.get("name"), args))
    return " ".join(p for p in parts if p)


@dataclass
class Reply:
    """一次模型回复:文本 + 可选的工具调用。"""

    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def stop_reason(self) -> str:
        return "toolUse" if self.tool_calls else "stop"

    def blocks(self) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        for call in self.tool_calls:
            blocks.append(
                {
                    "type": "toolCall",
                    "name": call["name"],
                    "arguments": call.get("arguments", {}),
                    "id": call.get("id"),
                }
            )
        return blocks


class FauxProvider:
    """免费、确定性的假模型:echo 模式 + ``!<cmd>`` 触发一次 bash。"""

    def generate(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Reply:
        last = messages[-1] if messages else None
        if last is not None and last.get("role") == "toolResult":
            return Reply(text="Tool finished: " + text_of(last.get("content"))[:200])

        prompt = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                prompt = text_of(message.get("content"))
                break
        if prompt.startswith("!"):
            return Reply(
                text="Running it.",
                tool_calls=[
                    {"name": "bash", "arguments": {"command": prompt[1:].strip()}, "id": None}
                ],
            )
        return Reply(text="Echo: " + prompt)


def _to_openai_message(message: Dict[str, Any]) -> Dict[str, Any]:
    role = message.get("role")
    content = message.get("content")
    blocks = content if isinstance(content, list) else []

    if role == "user":
        return {"role": "user", "content": text_of(content)}

    if role == "assistant":
        texts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
            elif block.get("type") == "toolCall":
                call: Dict[str, Any] = {
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("arguments", {}), ensure_ascii=False),
                    },
                }
                if block.get("id"):
                    call["id"] = block["id"]
                tool_calls.append(call)
        out: Dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
        if tool_calls:
            out["tool_calls"] = tool_calls
        return out

    if role == "toolResult":
        call_id = None
        for block in blocks:
            if isinstance(block, dict) and block.get("toolCallId"):
                call_id = block["toolCallId"]
        return {
            "role": "tool",
            "tool_call_id": call_id or "call_0",
            "content": text_of(content),
        }

    return {"role": "system", "content": text_of(content)}


class OpenRouterProvider:
    """真实模型(OpenAI 兼容的 chat/completions 接口)。"""

    API_URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model_id: str):
        self.api_key = api_key
        self.model_id = model_id
        if not api_key:
            raise RuntimeError("PROVIDER=openrouter 时需要在 .env 里设置 OPENROUTER_API_KEY")

    def generate(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Reply:
        body: Dict[str, Any] = {
            "model": self.model_id,
            "messages": [_to_openai_message(m) for m in messages],
        }
        if tools:
            body["tools"] = tools
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        message = data["choices"][0]["message"]
        tool_calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args)
            except ValueError:
                arguments = {"command": raw_args}
            tool_calls.append({"name": fn.get("name"), "arguments": arguments, "id": call.get("id")})
        return Reply(text=message.get("content") or "", tool_calls=tool_calls)


class Models:
    """模型注册表 —— 对应 TS 版的 ``createModels()``。"""

    def __init__(self) -> None:
        self._providers: Dict[str, Any] = {}

    def set_provider(self, name: str, provider: Any) -> None:
        self._providers[name] = provider

    def generate(self, model_ref: Dict[str, str], messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Reply:
        provider = self._providers.get(model_ref.get("provider", ""))
        if provider is None:
            raise RuntimeError("provider {!r} 没有安装".format(model_ref.get("provider")))
        return provider.generate(messages, tools)
