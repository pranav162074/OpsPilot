"""Answer evaluation: does the agent give correct, grounded answers?

Retrieval evaluation (evals/retrieval.py) checks that search finds the right passage. This
checks the whole agent, end to end, on evals/answer_cases.json:

- **grounded**: the answer cites the document that answers the question
- **citation precision**: share of its citations that are from that document
- **coverage**: share of the case's must-mention facts (a command, a setting) in the answer
- **faithfulness**: share of the answer's claims that its cited sources support, judged by a
  second model call that sees only the answer and those sources (an "LLM as judge")
- tokens, cost and latency per answer

    uv run python -m opspilot.evals.answers [--strategy plan] [--check]

Needs a language model (GEMINI_API_KEY; the free tier is enough for the 8 cases). It's not in
CI: model answers vary between runs and cost quota. Run it before and after changing a prompt,
the agent, or retrieval.
"""

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from opspilot.agent.graph import CITATION, text_of
from opspilot.agent.llm import build_chat_model
from opspilot.agent.planner import parse_json
from opspilot.agent.prompts import FAITHFULNESS_PROMPT
from opspilot.agent.service import InvestigationResult, run_investigation
from opspilot.agent.tools import Retrieved
from opspilot.config import Settings, get_settings
from opspilot.embeddings import Embedder, build_embedder
from opspilot.embeddings.cache import QueryEmbeddingCache
from opspilot.evals.retrieval import EVALS_DIR, _index_corpus
from opspilot.models import Workspace
from opspilot.search.rerank import Reranker, build_reranker


@dataclass
class Faithfulness:
    claims: int
    supported: int
    unsupported: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        # An answer with no claims ("the documents don't cover this") says nothing unsupported
        return self.supported / self.claims if self.claims else 1.0


@dataclass
class CaseResult:
    question: str
    grounded: bool
    citation_precision: float
    coverage: float
    faithfulness: float
    unsupported_claims: list[str]
    invalid_citations: int
    tokens: int
    cost_usd: float
    latency_ms: float
    error: str | None = None


async def judge_faithfulness(
    judge: BaseChatModel, answer: str, retrieved: Retrieved
) -> Faithfulness:
    cited = [retrieved.sources[i] for i in _cited(answer) if i in retrieved.sources]
    sources = "\n\n".join(f"[c:{s.chunk_id}] {s.content}" for s in cited) or "(none)"
    reply = await judge.ainvoke(
        [HumanMessage(FAITHFULNESS_PROMPT.format(answer=answer, sources=sources))]
    )
    parsed = parse_json(text_of(reply))
    claims = parsed.get("claims") if isinstance(parsed, dict) else None
    if not isinstance(claims, list):
        raise ValueError("The judge didn't return claims")
    items = [c for c in claims if isinstance(c, dict)]
    unsupported = [str(c.get("claim", "")) for c in items if c.get("supported") is not True]
    return Faithfulness(
        claims=len(items), supported=len(items) - len(unsupported), unsupported=unsupported
    )


def _cited(answer: str) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []
    for match in CITATION.finditer(answer):
        found = uuid.UUID(match.group(1))
        if found not in seen:
            seen.append(found)
    return seen


def score_case(
    case: dict[str, Any],
    result: InvestigationResult,
    faithfulness: Faithfulness,
    expected_title: str,
) -> CaseResult:
    titles = [c.document_title for c in result.citations]
    answer = result.answer.lower()
    must = case.get("must_mention", [])
    return CaseResult(
        question=case["question"],
        grounded=expected_title in titles,
        citation_precision=(titles.count(expected_title) / len(titles)) if titles else 0.0,
        coverage=(sum(1 for m in must if m.lower() in answer) / len(must)) if must else 1.0,
        faithfulness=faithfulness.score,
        unsupported_claims=faithfulness.unsupported,
        invalid_citations=result.invalid_citations,
        tokens=result.trace.input_tokens + result.trace.output_tokens,
        cost_usd=float(result.cost_usd or 0),
        latency_ms=result.latency_ms,
    )


def summarize(results: list[CaseResult]) -> dict[str, float]:
    ok = [r for r in results if r.error is None]
    n = len(results)

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    return {
        "cases": n,
        "errors": n - len(ok),
        "grounded": sum(1 for r in ok if r.grounded) / n if n else 0.0,
        "citation_precision": mean([r.citation_precision for r in ok]),
        "coverage": mean([r.coverage for r in ok]),
        "faithfulness": mean([r.faithfulness for r in ok]),
        "invalid_citations": sum(r.invalid_citations for r in ok),
        "tokens_per_answer": mean([float(r.tokens) for r in ok]),
        "cost_usd_total": round(sum(r.cost_usd for r in ok), 6),
        "p50_latency_ms": sorted(r.latency_ms for r in ok)[len(ok) // 2] if ok else 0.0,
    }


async def evaluate(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    llm: BaseChatModel,
    judge: BaseChatModel,
    *,
    strategy: str = "react",
    reranker: Reranker | None = None,
    cases: list[dict[str, Any]] | None = None,
    query_cache: QueryEmbeddingCache | None = None,
) -> list[CaseResult]:
    cases = cases or json.loads((EVALS_DIR / "answer_cases.json").read_text(encoding="utf-8"))
    async with sessionmaker() as session:
        workspace = Workspace(name=f"answer-eval-{uuid.uuid4().hex[:8]}")
        session.add(workspace)
        await session.commit()
        workspace_id = workspace.id
    cache = query_cache or _NoCache()
    results: list[CaseResult] = []
    try:
        await _index_corpus(sessionmaker, settings, embedder, workspace_id)
        for case in cases:
            started = time.perf_counter()
            try:
                result = await run_investigation(
                    llm=llm,
                    sessionmaker=sessionmaker,
                    settings=settings,
                    embedder=embedder,
                    query_cache=cache,  # type: ignore[arg-type]
                    workspace_id=workspace_id,
                    question=case["question"],
                    strategy=strategy,
                    reranker=reranker,
                )
                faithfulness = await judge_faithfulness(
                    judge, result.answer, result.retrieved or Retrieved()
                )
                results.append(score_case(case, result, faithfulness, Path(case["expected"]).stem))
            except Exception as exc:  # one failing case shouldn't hide the others
                results.append(
                    CaseResult(
                        question=case["question"],
                        grounded=False,
                        citation_precision=0.0,
                        coverage=0.0,
                        faithfulness=0.0,
                        unsupported_claims=[],
                        invalid_citations=0,
                        tokens=0,
                        cost_usd=0.0,
                        latency_ms=round((time.perf_counter() - started) * 1000, 1),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await session.commit()
    return results


class _NoCache:
    """The eval embeds each question once; no Redis needed."""

    async def embed_query(self, embedder: Embedder, query: str) -> list[float]:
        return await embedder.embed_query(query)


def check(summary: dict[str, float], thresholds: dict[str, float]) -> list[str]:
    return [
        f"{metric} = {summary[metric]:.3f}, below {minimum:.3f}"
        for metric, minimum in thresholds.items()
        if summary[metric] + 1e-9 < minimum
    ]


async def _run(
    settings: Settings, strategy: str, rerank: bool, thresholds: dict[str, float], out: str | None
) -> int:
    llm = build_chat_model(settings)
    if llm is None:
        print("The answer evaluation needs a language model: set GEMINI_API_KEY")
        return 1
    embedder = build_embedder(settings)
    reranker = (
        build_reranker(settings.model_copy(update={"reranker": "cross-encoder"}))
        if rerank
        else None
    )
    engine = create_async_engine(settings.database_url)
    try:
        results = await evaluate(
            async_sessionmaker(engine, expire_on_commit=False),
            settings,
            embedder,
            llm,
            llm,
            strategy=strategy,
            reranker=reranker,
        )
    finally:
        await embedder.aclose()
        await engine.dispose()

    summary = summarize(results)
    print(f"Model: {settings.llm_model}, strategy: {strategy}, re-ranker: {bool(reranker)}\n")
    print(f"{'question':<60} {'grounded':>8} {'prec':>5} {'cover':>5} {'faith':>5}")
    for r in results:
        flag = "ERROR" if r.error else ("yes" if r.grounded else "no")
        print(
            f"{r.question[:60]:<60} {flag:>8} {r.citation_precision:>5.2f} "
            f"{r.coverage:>5.2f} {r.faithfulness:>5.2f}"
        )
        for claim in r.unsupported_claims:
            print(f"    unsupported: {claim}")
        if r.error:
            print(f"    {r.error}")
    print("\n" + "\n".join(f"{k:<20} {v}" for k, v in summary.items()))
    if out:
        report = json.dumps({"summary": summary, "cases": [asdict(r) for r in results]}, indent=2)
        await asyncio.to_thread(Path(out).write_text, report, encoding="utf-8")
    failures = check(summary, thresholds)
    if failures:
        print("\nQuality gate FAILED:\n  " + "\n  ".join(failures))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m opspilot.evals.answers")
    parser.add_argument("--strategy", choices=["react", "plan"], default="react")
    parser.add_argument("--rerank", action="store_true")
    parser.add_argument("--check", action="store_true", help="Fail below evals/thresholds.json")
    parser.add_argument("--json", metavar="PATH", help="Also write the results as JSON")
    args = parser.parse_args()
    thresholds: dict[str, float] = {}
    if args.check:
        all_thresholds = json.loads((EVALS_DIR / "thresholds.json").read_text(encoding="utf-8"))
        thresholds = all_thresholds.get("answers", {})
    return asyncio.run(_run(get_settings(), args.strategy, args.rerank, thresholds, args.json))


if __name__ == "__main__":
    sys.exit(main())
