"""The investigation agent, as a LangGraph state machine.

    agent ──tool calls──▶ tools ──▶ agent          (ReAct: reason, act, observe, repeat)
      │ step limit ──▶ force_answer ──▶ review
      └─answer──▶ review ──supported──▶ END
                     └─unsupported──▶ agent        (one revision with the reviewer's notes)

The reviewer is a second model call that checks the draft against the passages it cites
(self-reflection), so an answer with invented steps gets one chance to be fixed before the
engineer sees it.
"""

import json
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages

from opspilot.agent.prompts import (
    BUDGET_PROMPT,
    REVIEW_PROMPT,
    REVISION_PROMPT,
    STEP_LIMIT_PROMPT,
    SYSTEM_PROMPT,
)
from opspilot.agent.tools import Retrieved

CITATION = re.compile(r"\[c:([0-9a-fA-F-]{36})\]")
# Tool inputs and outputs are cut in the stored trace; the full text lives in the documents
TRACE_TEXT_LIMIT = 2000


class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    steps: int
    revisions: int


@dataclass
class Trace:
    """What happened during one run: every model call and tool call, with timings and tokens."""

    events: list[dict[str, Any]] = field(default_factory=list)
    llm_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Called with each event as it happens: how investigations are streamed (SSE)
    listener: Callable[[dict[str, Any]], None] | None = field(
        default=None, repr=False, compare=False
    )

    def _emit(self) -> None:
        if self.listener is not None:
            self.listener(self.events[-1])

    def llm(self, node: str, message: BaseMessage, started: float) -> None:
        usage = getattr(message, "usage_metadata", None) or {}
        self.llm_calls += 1
        self.input_tokens += int(usage.get("input_tokens", 0))
        self.output_tokens += int(usage.get("output_tokens", 0))
        tool_calls = getattr(message, "tool_calls", None) or []
        self.events.append(
            {
                "type": "llm",
                "node": node,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "input_tokens": int(usage.get("input_tokens", 0)),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "tool_calls": [c["name"] for c in tool_calls],
                "text": text_of(message)[:TRACE_TEXT_LIMIT],
            }
        )
        self._emit()

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_json(self) -> dict[str, Any]:
        return {
            "events": self.events,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Trace":
        return cls(
            events=list(data.get("events", [])),
            llm_calls=int(data.get("llm_calls", 0)),
            tool_calls=int(data.get("tool_calls", 0)),
            input_tokens=int(data.get("input_tokens", 0)),
            output_tokens=int(data.get("output_tokens", 0)),
        )

    def tool(self, name: str, args: dict[str, Any], output: str, started: float, ok: bool) -> None:
        self.tool_calls += 1
        self.events.append(
            {
                "type": "tool",
                "name": name,
                "args": json.loads(json.dumps(args, default=str)),
                "ok": ok,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "output": output[:TRACE_TEXT_LIMIT],
            }
        )
        self._emit()


def text_of(message: BaseMessage) -> str:
    """A message's text, whether the provider returned a string or a list of content blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def cited_ids(text: str) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []
    for match in CITATION.finditer(text):
        chunk_id = uuid.UUID(match.group(1))
        if chunk_id not in seen:
            seen.append(chunk_id)
    return seen


def build_graph(
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    retrieved: Retrieved,
    trace: Trace,
    *,
    question: str,
    max_steps: int,
    reflection: bool,
    token_budget: int = 0,
) -> Any:
    by_name = {tool.name: tool for tool in tools}
    with_tools = llm.bind_tools(list(tools))
    system = SystemMessage(SYSTEM_PROMPT)

    async def agent(state: AgentState) -> dict[str, Any]:
        started = time.perf_counter()
        response = await with_tools.ainvoke([system, *state["messages"]])
        trace.llm("agent", response, started)
        return {"messages": [response], "steps": state["steps"] + 1}

    async def run_tools(state: AgentState) -> dict[str, Any]:
        last = state["messages"][-1]
        calls = last.tool_calls if isinstance(last, AIMessage) else []
        results: list[ToolMessage] = []
        for call in calls:
            started = time.perf_counter()
            tool = by_name.get(call["name"])
            if tool is None:
                output, ok = f"Unknown tool {call['name']!r}.", False
            else:
                try:
                    output, ok = str(await tool.ainvoke(call["args"])), True
                except Exception as exc:  # a failed tool is reported to the model, not raised
                    output, ok = f"The tool failed: {exc}", False
            trace.tool(call["name"], call["args"], output, started, ok)
            results.append(
                ToolMessage(content=output, tool_call_id=call["id"] or "", name=call["name"])
            )
        return {"messages": results}

    def over_budget() -> bool:
        return token_budget > 0 and trace.tokens >= token_budget

    async def force_answer(state: AgentState) -> dict[str, Any]:
        # The model wanted to keep searching (out of steps or tokens); make it answer,
        # without tools this time
        prompt = HumanMessage(BUDGET_PROMPT if over_budget() else STEP_LIMIT_PROMPT)
        started = time.perf_counter()
        response = await llm.ainvoke([system, *state["messages"], prompt])
        trace.llm("force_answer", response, started)
        return {"messages": [prompt, response]}

    async def review(state: AgentState) -> dict[str, Any]:
        draft = text_of(state["messages"][-1])
        sources = [retrieved.sources[i] for i in cited_ids(draft) if i in retrieved.sources]
        source_text = "\n\n".join(f"[c:{s.chunk_id}] {s.content}" for s in sources) or "(none)"
        started = time.perf_counter()
        verdict = await llm.ainvoke(
            REVIEW_PROMPT.format(question=question, answer=draft, sources=source_text)
        )
        trace.llm("review", verdict, started)
        line = text_of(verdict).strip()
        if line.upper().startswith("UNSUPPORTED"):
            issues = line.split(":", 1)[1].strip() if ":" in line else line
            return {
                "messages": [HumanMessage(REVISION_PROMPT.format(issues=issues))],
                "revisions": state["revisions"] + 1,
            }
        return {}

    def after_agent(state: AgentState) -> Literal["tools", "force_answer", "review", "__end__"]:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls:
            return "tools" if state["steps"] < max_steps and not over_budget() else "force_answer"
        return "review" if should_review(state) else "__end__"

    def should_review(state: AgentState) -> bool:
        return reflection and state["revisions"] == 0 and not over_budget()

    def after_review(state: AgentState) -> Literal["agent", "__end__"]:
        # A revision request is the last message only when the reviewer rejected the draft
        return "agent" if isinstance(state["messages"][-1], HumanMessage) else "__end__"

    def after_force(state: AgentState) -> Literal["review", "__end__"]:
        return "review" if should_review(state) else "__end__"

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", run_tools)
    graph.add_node("force_answer", force_answer)
    graph.add_node("review", review)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", after_agent)
    graph.add_edge("tools", "agent")
    graph.add_conditional_edges("force_answer", after_force)
    graph.add_conditional_edges("review", after_review)
    return graph.compile()
