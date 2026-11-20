"""Retrieval evaluation: how often does search put the right document near the top?

A fixed corpus of runbooks and a postmortem (backend/evals/corpus) and 22 questions, each with
the one document that answers it. Half the questions quote an exact identifier (an error code,
a command); half are paraphrases that share few words with the answer. Every mode is scored:

- hit@1 / hit@3: share of questions whose answer is the top document / among the top 3
- MRR: mean of 1/rank of the answer (1.0 means always first; 0 for a miss)

    uv run python -m opspilot.evals.retrieval

uses EMBEDDING_PROVIDER from your settings. It creates a temporary workspace, indexes the
corpus into it, and deletes it afterwards.
"""

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from opspilot.config import Settings, get_settings
from opspilot.embeddings import Embedder, build_embedder
from opspilot.ingestion.service import Outcome, ingest_document
from opspilot.models import Chunk, Document, Workspace
from opspilot.search.service import SearchMode, search_chunks

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
    sessionmaker: async_sessionmaker[AsyncSession], settings: Settings, embedder: Embedder
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
        ranks: dict[str, list[tuple[str, int | None]]] = {mode.value: [] for mode in SearchMode}
        for question in questions:
            query_vector = await embedder.embed_query(question["query"])
            for mode in SearchMode:
                async with sessionmaker() as session:
                    fused = await search_chunks(
                        session,
                        workspace_id=workspace_id,
                        query=question["query"],
                        mode=mode,
                        query_vector=query_vector,
                        model=embedder.model,
                        source_types=None,
                        candidates=settings.search_candidates,
                        rrf_k=settings.rrf_k,
                    )
                    rows = await session.execute(
                        select(Chunk.id, Document.filename)
                        .join(Document, Document.id == Chunk.document_id)
                        .where(Chunk.id.in_([f.chunk_id for f in fused]))
                    )
                    filenames = {row.id: row.filename for row in rows}
                # Rank documents, not chunks: a document's rank is that of its best chunk
                documents: list[str] = []
                for item in fused:
                    name = filenames[item.chunk_id]
                    if name not in documents:
                        documents.append(name)
                rank = (
                    documents.index(question["expected"]) + 1
                    if question["expected"] in documents
                    else None
                )
                ranks[mode.value].append((question["kind"], rank))
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


async def _main() -> None:
    settings = get_settings()
    embedder = build_embedder(settings)
    engine = create_async_engine(settings.database_url)
    try:
        report = await evaluate(
            async_sessionmaker(engine, expire_on_commit=False), settings, embedder
        )
    finally:
        await embedder.aclose()
        await engine.dispose()

    print(f"Embedding model: {embedder.model}\n")
    print(f"{'mode':<12} {'questions':>9} {'hit@1':>7} {'hit@3':>7} {'MRR':>6}")
    for mode, by_kind in report.items():
        for kind, s in by_kind.items():
            label = mode if kind == "all" else f"  {kind}"
            print(
                f"{label:<12} {s.questions:>9} {s.hit_at_1:>7.2f} {s.hit_at_3:>7.2f} {s.mrr:>6.2f}"
            )


if __name__ == "__main__":
    asyncio.run(_main())
