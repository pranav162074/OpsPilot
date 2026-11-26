"""The "plan" strategy: a structured investigation, as a LangGraph state machine.

    plan ──▶ gather ──▶ hypothesize ──▶ verify ──▶ answer ──▶ review ──▶ END
    (what to    (all tools     (possible    (one check     (cites      └─unsupported─▶ answer
     look at)    at once)       causes)      per cause,      evidence)
                                             at once)

Unlike "react", which decides one tool call at a time, this collects the evidence a human
would ask for up front and in parallel, then reasons about causes explicitly, and checks
each one before answering. Every stage's output is plain JSON, kept in the state:

- a token budget is checked before each model call; past it the run skips straight to the
  answer, with what it has;
- after each stage the state is handed to `on_checkpoint`, which stores it, so a run whose
  worker died resumes from the last finished stage instead of paying for it all again.
"""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph import START, StateGraph

from opspilot.agent.graph import Trace, cited_ids, text_of
from opspilot.agent.prompts import (
    ANSWER_PROMPT,
    BUDGET_PROMPT,
    HYPOTHESES_PROMPT,
    PLAN_PROMPT,
    REVIEW_PROMPT,
    REVISION_PROMPT,
    SYSTEM_PROMPT,
    VERIFY_PROMPT,
)
from opspilot.agent.tools import Retrieved

Stage = Literal["plan", "gather", "hypothesize", "verify", "answer", "review", "done"]
STAGES: tuple[Stage, ...] = ("plan", "gather", "hypothesize", "verify", "answer", "review")
VERDICTS = ("supported", "refuted", "inconclusive")
LIKELIHOODS = ("high", "medium", "low")
RISKS = ("low", "medium", "high")
ACTIONS_BLOCK = re.compile(r"```actions\s*(.*?)```", re.DOTALL)
# Evidence shown to later stages is cut, so a big log search doesn't crowd out the rest
EVIDENCE_CHARS = 2500


class PlanState(TypedDict):
    stage: Stage
    question: str
    steps: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    hypotheses: list[dict[str, Any]]
    draft: str
    revisions: int
    review_notes: str
    budget_exhausted: bool


def initial_state(question: str) -> PlanState:
    return {
        "stage": "plan",
        "question": question,
        "steps": [],
        "evidence": [],
        "hypotheses": [],
        "draft": "",
        "revisions": 0,
        "review_notes": "",
        "budget_exhausted": False,
    }


def parse_json(text: str) -> Any:
    """The first JSON object in a model's reply: models wrap JSON in prose or code fences."""
    start = text.find("{")
    while start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            return value
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    return None


def split_actions(answer: str) -> tuple[str, list[dict[str, Any]]]:
    """Takes the ```actions block off the end of an answer and validates what's in it."""
    match = ACTIONS_BLOCK.search(answer)
    if match is None:
        return answer, []
    try:
        raw = json.loads(match.group(1))
    except json.JSONDecodeError:
        raw = []
    actions = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        command = item.get("command")
        actions.append(
            {
                "title": str(item["title"]).strip()[:300],
                "command": str(command).strip() if command else None,
                "risk": item.get("risk") if item.get("risk") in RISKS else "high",
                "rationale": str(item.get("rationale") or "").strip() or None,
                "citations": [str(c) for c in item.get("citations") or [] if isinstance(c, str)],
            }
        )
    return (answer[: match.start()] + answer[match.end() :]).strip(), actions


def describe_tools(tools: Sequence[BaseTool]) -> str:
    lines = []
    for tool in tools:
        params = ", ".join(tool.args) if tool.args else "no arguments"
        lines.append(f"- {tool.name}({params}): {tool.description.strip().splitlines()[0]}")
    return "\n".join(lines)


def _valid_call(raw: Any, by_name: dict[str, BaseTool]) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("tool") not in by_name:
        return None
    args = raw.get("args")
    return {"tool": raw["tool"], "args": args if isinstance(args, dict) else {}}


def _evidence_text(evidence: list[dict[str, Any]]) -> str:
    if not evidence:
        return "(none)"
    parts = []
    for item in evidence:
        output = item["output"]
        if len(output) > EVIDENCE_CHARS:
            output = output[:EVIDENCE_CHARS] + " [...]"
        parts.append(f"## {item['tool']}({json.dumps(item['args'])})\n{output}")
    return "\n\n".join(parts)


def _hypotheses_text(hypotheses: list[dict[str, Any]]) -> str:
    if not hypotheses:
        return "(none: answer from the evidence)"
    lines = []
    for i, h in enumerate(hypotheses, 1):
        verdict = h.get("verdict") or "unchecked"
        reason = f": {h['reason']}" if h.get("reason") else ""
        lines.append(f"{i}. {h['statement']} (likelihood {h['likelihood']}) → {verdict}{reason}")
    return "\n".join(lines)


def build_plan_graph(
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    retrieved: Retrieved,
    trace: Trace,
    *,
    max_parallel: int,
    tool_timeout: float,
    max_hypotheses: int,
    token_budget: int,
    reflection: bool,
    on_checkpoint: Callable[[PlanState], Awaitable[None]] | None = None,
) -> Any:
    by_name = {tool.name: tool for tool in tools}
    tool_list = describe_tools(tools)

    def over_budget() -> bool:
        return token_budget > 0 and trace.input_tokens + trace.output_tokens >= token_budget

    async def ask(node: str, prompt: str) -> str:
        started = time.perf_counter()
        response = await llm.ainvoke([HumanMessage(prompt)])
        trace.llm(node, response, started)
        return text_of(response)

    async def run_one(call: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        tool = by_name[call["tool"]]
        try:
            async with asyncio.timeout(tool_timeout):
                output, ok = str(await tool.ainvoke(call["args"])), True
        except TimeoutError:
            output, ok = f"The tool took longer than {tool_timeout:g}s and was stopped.", False
        except Exception as exc:  # a failed tool is evidence too ("Loki is down")
            output, ok = f"The tool failed: {exc}", False
        trace.tool(call["tool"], call["args"], output, started, ok)
        return {**call, "ok": ok, "output": output}

    async def run_all(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # gather keeps the order of the calls, so the trace and evidence read in plan order
        return list(await asyncio.gather(*(run_one(c) for c in calls)))

    def moved(state: PlanState, stage: Stage, **changes: Any) -> dict[str, Any]:
        if over_budget() and stage in ("gather", "hypothesize", "verify"):
            # Out of tokens: no more gathering or reasoning, answer with what there is
            return {**changes, "stage": "answer", "budget_exhausted": True}
        return {**changes, "stage": stage}

    async def plan(state: PlanState) -> dict[str, Any]:
        reply = await ask(
            "plan",
            PLAN_PROMPT.format(system=SYSTEM_PROMPT, max_steps=max_parallel, tools=tool_list)
            + f"\nQuestion:\n{state['question']}",
        )
        parsed = parse_json(reply)
        raw_steps = parsed.get("steps") if isinstance(parsed, dict) else None
        steps = []
        for raw in raw_steps if isinstance(raw_steps, list) else []:
            call = _valid_call(raw, by_name)
            if call is not None and call not in steps:
                steps.append({**call, "why": str(raw.get("why") or "")[:300]})
        if not steps and "search_knowledge_base" in by_name:
            # An unusable plan still gets the one step every investigation needs
            steps = [{"tool": "search_knowledge_base", "args": {"query": state["question"]}}]
        return moved(state, "gather", steps=steps[:max_parallel])

    async def gather(state: PlanState) -> dict[str, Any]:
        evidence = await run_all([{"tool": s["tool"], "args": s["args"]} for s in state["steps"]])
        return moved(state, "hypothesize", evidence=evidence)

    async def hypothesize(state: PlanState) -> dict[str, Any]:
        reply = await ask(
            "hypothesize",
            HYPOTHESES_PROMPT.format(
                question=state["question"],
                evidence=_evidence_text(state["evidence"]),
                max_hypotheses=max_hypotheses,
                tools=tool_list,
            ),
        )
        parsed = parse_json(reply)
        raw_list = parsed.get("hypotheses") if isinstance(parsed, dict) else None
        hypotheses = []
        for raw in (raw_list if isinstance(raw_list, list) else [])[:max_hypotheses]:
            if not isinstance(raw, dict) or not str(raw.get("statement") or "").strip():
                continue
            likelihood = raw.get("likelihood")
            hypotheses.append(
                {
                    "statement": str(raw["statement"]).strip()[:500],
                    "likelihood": likelihood if likelihood in LIKELIHOODS else "medium",
                    "evidence": [str(e).removeprefix("c:") for e in raw.get("evidence") or [] if e],
                    "check": _valid_call(raw.get("check"), by_name),
                    "check_result": None,
                    "verdict": None,
                    "reason": None,
                }
            )
        return moved(state, "verify", hypotheses=hypotheses)

    async def verify(state: PlanState) -> dict[str, Any]:
        hypotheses = [dict(h) for h in state["hypotheses"]]
        to_check = [i for i, h in enumerate(hypotheses) if h["check"]]
        results = await run_all([hypotheses[i]["check"] for i in to_check])
        for i, result in zip(to_check, results, strict=True):
            hypotheses[i]["check_result"] = {"ok": result["ok"], "output": result["output"]}
        if not hypotheses:
            return moved(state, "answer", hypotheses=hypotheses)
        checks = []
        for n, h in enumerate(hypotheses, 1):
            result = h["check_result"]
            shown = result["output"][:EVIDENCE_CHARS] if result else "(not checked)"
            checks.append(f"{n}. {h['statement']}\nCheck: {h['check']}\nResult:\n{shown}")
        reply = await ask(
            "verify", VERIFY_PROMPT.format(question=state["question"], checks="\n\n".join(checks))
        )
        parsed = parse_json(reply)
        verdicts = parsed.get("verdicts") if isinstance(parsed, dict) else None
        for raw in verdicts if isinstance(verdicts, list) else []:
            if not isinstance(raw, dict):
                continue
            number = raw.get("hypothesis")
            if isinstance(number, int) and 1 <= number <= len(hypotheses):
                verdict = raw.get("verdict")
                hypotheses[number - 1]["verdict"] = (
                    verdict if verdict in VERDICTS else "inconclusive"
                )
                hypotheses[number - 1]["reason"] = str(raw.get("reason") or "")[:500] or None
        for h in hypotheses:
            h["verdict"] = h["verdict"] or "inconclusive"
        # Supported causes first, refuted last; the model's ordering within each
        rank = {"supported": 0, "inconclusive": 1, "refuted": 2}
        hypotheses.sort(key=lambda h: rank[h["verdict"]])
        return moved(state, "answer", hypotheses=hypotheses)

    async def answer(state: PlanState) -> dict[str, Any]:
        everything = list(state["evidence"])
        everything += [
            {"tool": h["check"]["tool"], "args": h["check"]["args"], **h["check_result"]}
            for h in state["hypotheses"]
            if h["check"] and h["check_result"]
        ]
        prompt = ANSWER_PROMPT.format(
            question=state["question"],
            evidence=_evidence_text(everything),
            hypotheses=_hypotheses_text(state["hypotheses"]),
        )
        if state["review_notes"]:
            prompt += "\n\n" + REVISION_PROMPT.format(issues=state["review_notes"])
            prompt += f"\n\nYour previous answer:\n{state['draft']}"
        if state["budget_exhausted"] or over_budget():
            prompt += "\n\n" + BUDGET_PROMPT
        draft = (await ask("answer", prompt)).strip()
        skip_review = not reflection or state["revisions"] > 0 or over_budget()
        return {
            "draft": draft,
            "stage": "done" if skip_review else "review",
            "budget_exhausted": state["budget_exhausted"] or over_budget(),
        }

    async def review(state: PlanState) -> dict[str, Any]:
        body, _ = split_actions(state["draft"])
        sources = [retrieved.sources[i] for i in cited_ids(body) if i in retrieved.sources]
        source_text = "\n\n".join(f"[c:{s.chunk_id}] {s.content}" for s in sources) or "(none)"
        line = (
            await ask(
                "review",
                REVIEW_PROMPT.format(question=state["question"], answer=body, sources=source_text),
            )
        ).strip()
        if line.upper().startswith("UNSUPPORTED"):
            issues = line.split(":", 1)[1].strip() if ":" in line else line
            return {"stage": "answer", "review_notes": issues, "revisions": state["revisions"] + 1}
        return {"stage": "done"}

    # Returns Any: LangGraph's node protocol names its parameter ("state"), which a plain
    # Callable type can't express
    def checkpointed(node: Callable[[PlanState], Awaitable[dict[str, Any]]]) -> Any:
        async def run(state: PlanState) -> dict[str, Any]:
            changes = await node(state)
            if on_checkpoint is not None:
                await on_checkpoint({**state, **changes})  # type: ignore[typeddict-item]
            return changes

        return run

    def route(state: PlanState) -> str:
        return "__end__" if state["stage"] == "done" else state["stage"]

    graph = StateGraph(PlanState)
    for name, node in (
        ("plan", plan),
        ("gather", gather),
        ("hypothesize", hypothesize),
        ("verify", verify),
        ("answer", answer),
        ("review", review),
    ):
        graph.add_node(name, checkpointed(node))
        graph.add_conditional_edges(name, route)
    # A resumed run starts at the stage it had reached
    graph.add_conditional_edges(START, route)
    return graph.compile()
