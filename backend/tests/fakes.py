"""A scripted chat model: plays back prepared replies, so agent tests need no API and are exact."""

import asyncio
import re
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict

Step = Callable[[list[BaseMessage]], AIMessage]
USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


class ScriptedChatModel(BaseChatModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[Step]
    calls: list[list[BaseMessage]] = []
    delay: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedChatModel":  # type: ignore[override]
        return self

    def _next(self, messages: list[BaseMessage]) -> ChatResult:
        index = len(self.calls)
        self.calls.append(messages)
        if index >= len(self.script):
            raise AssertionError(
                f"The model was called {index + 1} times; the script has {len(self.script)}"
            )
        return ChatResult(generations=[ChatGeneration(message=self.script[index](messages))])

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._next(messages)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._next(messages)


def call_tool(name: str, **args: Any) -> Step:
    def step(_: list[BaseMessage]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": f"call-{uuid.uuid4().hex[:8]}"}],
            usage_metadata=USAGE,  # type: ignore[arg-type]
        )

    return step


def retrieved_ids(messages: list[BaseMessage]) -> list[str]:
    """Chunk ids the tools have shown the model so far, in order."""
    ids: list[str] = []
    for message in messages:
        if isinstance(message, ToolMessage):
            ids += re.findall(r"\[c:([0-9a-f-]{36})\]", str(message.content))
    return ids


def answer_citing(text: str, which: int = 0) -> Step:
    """Answers with `text`, citing one of the passages the tools returned."""

    def step(messages: list[BaseMessage]) -> AIMessage:
        chunk_id = retrieved_ids(messages)[which]
        return AIMessage(content=f"{text} [c:{chunk_id}]", usage_metadata=USAGE)  # type: ignore[arg-type]

    return step


def reply(text: str) -> Step:
    return lambda _: AIMessage(content=text, usage_metadata=USAGE)  # type: ignore[arg-type]


def fail(_: list[BaseMessage]) -> AIMessage:
    raise RuntimeError("The model API returned 500")


def evidence_ids(messages: list[BaseMessage]) -> list[str]:
    """Ids shown to the model anywhere in its prompt: the "plan" strategy puts the evidence in
    the prompt text instead of in tool messages."""
    ids: list[str] = []
    for message in messages:
        for found in re.findall(r"\[c:([0-9a-f-]{36})\]", str(message.content)):
            if found not in ids:
                ids.append(found)
    return ids


def reply_using(make: Callable[[list[str]], str]) -> Step:
    """Replies with text built from the evidence ids in the prompt (e.g. JSON citing them)."""
    return lambda messages: AIMessage(
        content=make(evidence_ids(messages)),
        usage_metadata=USAGE,  # type: ignore[arg-type]
    )
