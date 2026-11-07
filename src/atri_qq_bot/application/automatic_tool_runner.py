from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .contracts import AgentRunResult, ToolReceipt
from ..llm_tools.registry import ToolRegistry
from ..llm_tools.tool_loop import (
    append_tool_results,
    has_document_promise_without_receipt,
    tool_calls_from_message,
)


ModelRequest = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ToolExecutor = Callable[[str, dict[str, Any], Any], Awaitable[str | None]]
ReplyNormalizer = Callable[[str], str]

# 反谎言守卫的纠正指令：承诺发文档但没有工具回执时，把虚假承诺
# 退回给模型重答——要么真正调用 send_document 生成文件，要么给出
# 不承诺发送文件的诚实回复。只纠正一轮，仍不调用就按原样发出。
_DOCUMENT_PROMISE_REPAIR_PROMPT = (
    "上一条回复承诺向用户发送文档，但本轮并没有调用 send_document "
    "或 compile_paper_digest 生成文件，这是虚假承诺。现在立即调用 "
    "send_document 工具，把承诺的文档内容（基于你已掌握的真实信息）"
    "真正生成出来，然后基于工具回执重新给出真实回复；如果生成不了，"
    "就重新给出不承诺发送任何文件的诚实回复。"
)


class AutomaticToolRunner:
    """Run the legacy automatic tool loop behind one small, testable interface.

    This deliberately models only the non-agent-protocol path. The stricter
    final-answer protocol has additional grounding invariants and remains in
    its legacy adapter until it has equivalent replay coverage.

    The runner owns the loop ordering: model response -> bounded tool calls ->
    follow-up response. Its caller retains provider transport, persona prompt
    construction and context-specific tools such as voice delivery.
    """

    def __init__(
        self,
        config: Any,
        request_model: ModelRequest,
        normalize_reply: ReplyNormalizer,
        *,
        tool_executor: ToolExecutor | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._config = config
        self._request_model = request_model
        self._normalize_reply = normalize_reply
        self._tool_executor = tool_executor
        self._registry = registry

    async def run(
        self,
        messages: list[dict[str, Any]],
        payload: dict[str, Any],
        *,
        context_id: str = "",
    ) -> AgentRunResult:
        # 这是一个有上限的 Agent 状态机：模型先判断是否需要工具，工具执行
        # 后把真实回执追加回上下文，再让模型基于证据完成回复。
        message = self._message_from_response(await self._request_model(payload))
        receipts: list[ToolReceipt] = []
        remaining_tool_calls = max(
            1,
            int(getattr(self._config, "llm_tool_max_calls", 2) or 2),
        )
        message, receipts, remaining_tool_calls = await self._drive_tool_loop(
            messages, payload, message, receipts, remaining_tool_calls, context_id
        )
        content = self._normalize_reply(str(message.get("content") or "")).strip()
        if has_document_promise_without_receipt(
            content, {receipt.name for receipt in receipts if receipt.ok}
        ):
            # 反谎言守卫：agent 协议路径早有「稍后执行承诺」拦截，自动
            # 工具路径一直没有——deepseek-flash 实测连续三轮口头承诺
            # 「文档随后就到」却从不调用工具，这里补上同等的拦截。
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {"role": "system", "content": _DOCUMENT_PROMISE_REPAIR_PROMPT}
            )
            repair_payload = dict(payload)
            repair_payload["messages"] = messages
            remaining_tool_calls = max(remaining_tool_calls, 1)
            message = self._message_from_response(
                await self._request_model(repair_payload)
            )
            message, receipts, remaining_tool_calls = await self._drive_tool_loop(
                messages,
                repair_payload,
                message,
                receipts,
                remaining_tool_calls,
                context_id,
            )
            content = self._normalize_reply(str(message.get("content") or "")).strip()
        return AgentRunResult(
            reply_text=content[:1200],
            tool_receipts=tuple(receipts),
        )

    async def _drive_tool_loop(
        self,
        messages: list[dict[str, Any]],
        payload: dict[str, Any],
        message: dict[str, Any],
        receipts: list[ToolReceipt],
        remaining_tool_calls: int,
        context_id: str,
    ) -> tuple[dict[str, Any], list[ToolReceipt], int]:
        # 每轮执行后都重新请求模型，让它看到成功/失败回执，而不是猜测结果。
        while remaining_tool_calls > 0:
            tool_calls = tool_calls_from_message(message)
            if not tool_calls:
                break
            executed = await append_tool_results(
                messages,
                message,
                tool_calls,
                self._config,
                executor=self._tool_executor,
                max_calls=remaining_tool_calls,
                context_id=context_id,
                receipts=receipts,
                registry=self._registry,
            )
            remaining_tool_calls -= executed
            followup_payload = dict(payload)
            followup_payload["messages"] = messages
            if remaining_tool_calls <= 0:
                followup_payload.pop("tools", None)
                followup_payload.pop("tool_choice", None)
            message = self._message_from_response(
                await self._request_model(followup_payload)
            )
        return message, receipts, remaining_tool_calls

    @staticmethod
    def _message_from_response(data: dict[str, Any]) -> dict[str, Any]:
        # Preserve the legacy adapter's strict provider response contract:
        # malformed provider payloads still surface to its existing fallback.
        return data["choices"][0]["message"]
