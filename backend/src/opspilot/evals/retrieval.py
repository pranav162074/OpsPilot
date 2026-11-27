"""Retrieval evaluation: how often does search put the right document near the top?

A fixed corpus of runbooks and a postmortem (backend/evals/corpus) and 22 questions, each with
the one document that answers it. Half the questions quote an exact identifier (an error code,
a command); half are paraphrases that share few words with the answer. Every mode is scored:

- hit@1 / hit@3: share of questions whose answer is the top document / among the top 3
- MRR: mean of 1/rank of the answer (1.0 means always first; 0 for a miss)

    uv run python -m opspilot.evals.retrieval                 # the table
    uv run python -m opspilot.evals.retrieval --rerank        # plus hybrid + cross-encoder
    uv run python -m opspilot.evals.retrieval --check         # fail below evals/thresholds.json

uses EMBEDDING_PROVIDER from your settings. It creates a temporary workspace, indexes the
corpus into it, and deletes it afterwards. --check is the CI quality gate: a change that
makes search worse fails the build, the way a failing test does.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from opspilot.config import Settings, get_settings
from opspilot.embeddings import Embedder, build_embedder
from opspilot.ingestion.service import Outcome, ingest_document
from opspilot.models import Document, Workspace
from opspilot.search.rerank import Reranker, build_reranker
from opspilot.search.service import SearchMode, retrieve

RERANKED = "hybrid+rerank"

EVALS_DIR = Path(__file__).resolve().parents[3] / "evals"


@dataclass
class Score:
    hit_at_1: float
    hit_at_3: float
    mrr: float
    questions: int


async def _index_corpus(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    workspace_id: uuid.UUID,
) -> None:
    async with sessionmaker() as session:
        for path in sorted((EVALS_DIR / "corpus").glob("*.md")):
            content = path.read_text(encoding="utf-8")
            session.add(
                Document(
                    workspace_id=workspace_id,
                    title=path.stem,
                    filename=path.name,
                    source_type="runbook",
                    media_type="text/markdown",
                    content=content,
                    content_hash=hashlib.sha256(content.encode()).hexdigest(),
                    size_bytes=len(content.encode()),
                )
            )
        await session.commit()
        ids = list(
            await session.scalars(select(Document.id).where(Document.workspace_id == workspace_id))
        )
    for document_id in ids:
        outcome = await ingest_document(sessionmaker, settings, document_id, embedder)
        if outcome != Outcome.READY:
            raise RuntimeError(f"Indexing {document_id} ended as {outcome}")


async def evaluate(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    embedder: Embedder,
    reranker: Reranker | None = None,
) -> dict[str, dict[str, Score]]:
    """Scores, by mode, then by question kind ("all", "keyword", "semantic")."""
    questions = json.loads((EVALS_DIR / "queries.json").read_text(encoding="utf-8"))
    async with sessionmaker() as session:
        workspace = Workspace(name=f"retrieval-eval-{uuid.uuid4().hex[:8]}")
        session.add(workspace)
        await session.commit()
        workspace_id = workspace.id

    try:
        await _index_corpus(sessionmaker, settings, embedder, workspace_id)
        runs: list[tuple[str, SearchMode, Reranker | None]] = [
            (mode.value, mode, None) for mode in SearchMode
        ]
        if reranker is not None:
            runs.append((RERANKED, SearchMode.HYBRID, reranker))
        ranks: dict[str, list[tuple[str, int | None]]] = {name: [] for name, _, _ in runs}
        for question in questions:
            query_vector = await embedder.embed_query(question["query"])
            for name, mode, rerank_with in runs:
                async with sessionmaker() as session:
                    found = await retrieve(
                        session,
                        reranker=rerank_with,
                        rerank_candidates=settings.rerank_candidates,
                        limit=settings.search_candidates,
                        workspace_id=workspace_id,
                        query=question["query"],
                        mode=mode,
                        query_vector=query_vector,
                        model=embedder.model,
                        source_types=None,
                        candidates=settings.search_candidates,
                        rrf_k=settings.rrf_k,
                    )
                # Rank documents, not chunks: a document's rank is that of its best chunk
                documents: list[str] = []
                for _, _, document in found.results:
                    if document.filename not in documents:
                        documents.append(document.filename)
                rank = (
                    documents.index(question["expected"]) + 1
                    if question["expected"] in documents
                    else None
                )
                ranks[name].append((question["kind"], rank))
    finally:
        async with sessionmaker() as session:
            await session.execute(delete(Workspace).where(Workspace.id == workspace_id))
            await session.commit()

    def score(results: list[int | None]) -> Score:
        def hits(k: int) -> float:
            return sum(1 for r in results if r is not None and r <= k) / len(results)

        return Score(
            hit_at_1=hits(1),
            hit_at_3=hits(3),
            mrr=sum(1 / r for r in results if r is not None) / len(results),
            questions=len(results),
        )

    return {
        mode: {
            "all": score([r for _, r in results]),
            "keyword": score([r for kind, r in results if kind == "keyword"]),
            "semantic": score([r for kind, r in results if kind == "semantic"]),
        }
        for mode, results in ranks.items()
    }


def check(
    report: dict[str, dict[str, Score]], thresholds: dict[str, dict[str, float]]
) -> list[str]:
    """Every metric below its threshold, as readable lines (empty: the gate passes)."""
    failures = []
    for mode, metrics in thresholds.items():
        if mode not in report:
            failures.append(f"{mode}: not evaluated (is the re-ranker installed?)")
            continue
        scored = report[mode]["all"]
        for metric, minimum in metrics.items():
            value = getattr(scored, metric)
            if value + 1e-9 < minimum:
                failures.append(f"{mode} {metric} = {value:.3f}, below {minimum:.3f}")
    return failures


def print_table(model: str, report: dict[str, dict[str, Score]]) -> None:
    print(f"Embedding model: {model}\n")
    print(f"{'mode':<14} {'questions':>9} {'hit@1':>7} {'hit@3':>7} {'MRR':>6}")
    for mode, by_kind in report.items():
        for kind, s in by_kind.items():
            label = mode if kind == "all" else f"  {kind}"
            print(
                f"{label:<14} {s.questions:>9} {s.hit_at_1:>7.2f} {s.hit_at_3:>7.2f} {s.mrr:>6.2f}"
            )


async def _run(settings: Settings, thresholds: dict[str, dict[str, float]], rerank: bool) -> int:
    reranker = (
        build_reranker(settings.model_copy(update={"reranker": "cross-encoder"}))
        if rerank or RERANKED in thresholds
        else None
    )
    embedder = build_embedder(settings)
    engine = create_async_engine(settings.database_url)
    try:
        report = await evaluate(
            async_sessionmaker(engine, expire_on_commit=False), settings, embedder, reranker
        )
    finally:
        await embedder.aclose()
        await engine.dispose()

    print_table(embedder.model, report)
    if not thresholds:
        return 0
    failures = check(report, thresholds)
    if failures:
        print("\nQuality gate FAILED:\n  " + "\n  ".join(failures))
        return 1
    print(f"\nQuality gate passed ({sum(len(m) for m in thresholds.values())} thresholds)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m opspilot.evals.retrieval")
    parser.add_argument("--rerank", action="store_true", help="Also score hybrid + re-ranker")
    parser.add_argument(
        "--check",
        nargs="?",
        const=str(EVALS_DIR / "thresholds.json"),
        metavar="THRESHOLDS",
        help="Exit 1 if any score is below its threshold (default: evals/thresholds.json)",
    )
    args = parser.parse_args()
    settings = get_settings()
    thresholds: dict[str, dict[str, float]] = {}
    if args.check:
        by_provider = json.loads(Path(args.check).read_text(encoding="utf-8"))
        thresholds = by_provider.get(embedder_key(settings), {})
        if not thresholds:
            print(f"No thresholds for EMBEDDING_PROVIDER={embedder_key(settings)}")
            return 1
    return asyncio.run(_run(settings, thresholds, args.rerank))


def embedder_key(settings: Settings) -> str:
    """Thresholds are per embedding provider: hashing and Gemini score very differently."""
    return settings.embedding_provider


if __name__ == "__main__":
    sys.exit(main())
