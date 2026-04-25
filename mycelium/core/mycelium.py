"""Mycelium orchestrator: ingest pipeline.

P0 — Context-free extraction, batch queries, embedding dedup, summary top-N
L1 — Chunking + rich facts (smart boundaries, table linearization)
L3 — Deep extraction (multi-pass survey + insights + analytical)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ProgressFn = Callable[[str, str], None] | None

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars

from mycelium.config import Settings, load_settings
from mycelium.core.models import (
    Mention,
    Neuron,
    Signal,
    SignalStatus,
    SignalType,
    Synapse,
)
from mycelium.core.types import MyceliumClients
from mycelium.domain import load_all as load_domains, match_domain
from mycelium.exceptions import ExtractionError
from mycelium.llm.session import LLMSession
from mycelium.prompts.ingest import (
    ContradictionResult,
    DedupResult,
    ExtractedNeuron,
    ExtractedQuestion,
    ExtractedSynapse,
    IngestResult,
    build_analytical_prompt,
    build_context_section,
    build_contradiction_prompt,
    build_dedup_prompt,
    build_entity_prompt,
    build_gleaning_prompt,
    build_ingest_prompt,
    build_relation_prompt,
    build_session_entity_user,
    build_session_extract_user,
    build_session_gleaning_user,
    build_session_relation_user,
    build_session_system_prompt,
    build_survey_prompt,
    filter_questions,
)
from mycelium.prompts.summary import build_summary_prompt
from mycelium.search.config import SearchResults
from mycelium.search.search import HybridSearch
from mycelium.utils.chunker import chunk_text
from mycelium.utils.decay import calc_decay_rate, consolidate, cypher_effective_weight
from mycelium.core.skills import load_skills, match_skill
from mycelium.core.telemetry import Telemetry
from mycelium.utils.dedup import cosine_sim
from mycelium.vault.storage import VaultStorage

log = structlog.get_logger()


class Mycelium:
    """Main orchestrator: Signal → Neuron → Synapse pipeline."""

    def __init__(
        self,
        clients:  MyceliumClients,
        settings: Settings | None = None,
    ) -> None:
        self._c          = clients
        self._s          = settings or load_settings()
        self._vault      = VaultStorage(self._s.vault)
        self._search     = HybridSearch(
            self._c.driver, self._c.embedder, self._s, self._c.llm,
        )
        self._owner_name         = self._s.owner.name  # cached, mutable at runtime
        self._owner_uuid         = ""                 # resolved in init_owner
        self._owner_init         = False              # True after first init_owner
        self._last_file_category = ""                 # set by add_episode for add_file

    # ── Public API ──────────────────────────────────────────

    async def add_episode(
        self,
        content:     str,
        *,
        name:        str              = "",
        source_type: SignalType       = SignalType.text,
        source_desc: str              = "",
        domain:      str              = "",
        valid_at:    datetime | None   = None,
        on_progress: ProgressFn        = None,
        extraction_focus: str          = "",
        signal_uuid: str | None        = None,
    ) -> tuple[Signal, list[Neuron], list[Synapse], list[ExtractedQuestion]]:
        """Full ingestion pipeline with adaptive depth (L3).

        ``signal_uuid`` lets callers pre-create a placeholder Signal (e.g. for
        async MCP ingestion) and pass its uuid through — avoids a duplicate
        Signal node being created by this pipeline.
        """
        if not content or not content.strip():
            raise ExtractionError("Signal content cannot be empty")
        clear_contextvars()
        t0 = time.monotonic()

        def _p(step: str, detail: str = "") -> None:
            if on_progress:
                on_progress(step, detail)

        # Auto-detect domain from DomainBlueprint triggers when not provided.
        # Lets daemons/CLI ingests land in CORTEX/{domain}/ without explicit
        # threading from the caller.
        if not domain:
            domain = self._resolve_domain(content, name, source_desc)

        sig_kwargs: dict[str, Any] = dict(
            name        = name or content[:60],
            content     = content,
            source_type = source_type,
            source_desc = source_desc,
            domain      = domain,
            valid_at    = valid_at or datetime.now(UTC),
        )
        if signal_uuid:
            sig_kwargs["uuid"] = signal_uuid
        signal = Signal(**sig_kwargs)
        bind_contextvars(signal_id=signal.uuid)
        log.info("signal_created",
                 source_type=source_type.value, content_len=len(content))

        _p("signal", f"saving ({len(content)} chars)")
        await self._save_signal(signal)
        await self.init_owner()

        try:
            deep = len(content) >= self._s.ingestion.deep_threshold
            chunks = chunk_text(
                content,
                max_chars = self._s.ingestion.max_chunk_chars,
                overlap   = self._s.ingestion.chunk_overlap,
            )
            _p("chunking", f"{len(chunks)} chunk(s)")

            # CC CLI session reuse: system prompt sent once
            extraction_session: LLMSession | None = None
            if self._s.llm.session_enabled:
                extraction_session = LLMSession(
                    system_prompt=build_session_system_prompt(
                        self._s.interaction.level,
                    ),
                )

            # Auto-timeout: deep docs get more time per LLM call
            if deep:
                self._c.llm._s.timeout = self._s.llm.deep_timeout

            # Adaptive depth: survey for large documents (L3)
            survey = ""
            if deep and len(chunks) > 1:
                _p("survey", "analyzing document structure")
                survey = await self._survey(content, on_progress=on_progress)

            all_neurons:   list[Neuron]            = []
            all_synapses:  list[Synapse]           = []
            all_questions: list[ExtractedQuestion]  = []
            chunk_meta:    list[dict[str, int]]    = []
            file_category: str                      = ""

            n = len(chunks)
            if n == 1:
                # Single chunk — no parallelism overhead
                _p("extracting", "LLM extraction")
                neurons, synapses, questions, file_cat = await self._run_pipeline(
                    signal, chunks[0], survey,
                    owner_name=self._owner_name,
                    session=extraction_session,
                    on_progress=on_progress,
                    extraction_focus=extraction_focus,
                )
                file_category = file_cat
                all_neurons.extend(neurons)
                all_synapses.extend(synapses)
                all_questions.extend(questions)
                chunk_meta.append({
                    "idx": 0, "len": len(chunks[0]),
                    "neurons": len(neurons), "synapses": len(synapses),
                })
            else:
                # Multi-chunk: parallel LLM extraction → sequential processing
                _p("extracting", f"{n} chunks (parallel, max {self._s.ingestion.max_parallel_chunks})")
                extract_results = await self._parallel_extract(
                    chunks, survey, session=extraction_session,
                    on_progress=on_progress,
                    extraction_focus=extraction_focus,
                )
                for i, (chunk, result) in enumerate(zip(chunks, extract_results)):
                    if not file_category and result.file_category:
                        file_category = result.file_category
                    if not result.neurons and not result.synapses:
                        all_questions.extend(result.questions)
                        chunk_meta.append({
                            "idx": i, "len": len(chunk),
                            "neurons": 0, "synapses": 0,
                        })
                        continue
                    _p("processing", f"chunk {i + 1}/{n} — dedup + save")
                    neurons, synapses = await self._process_extracted(
                        signal, result, on_progress=on_progress,
                    )
                    all_neurons.extend(neurons)
                    all_synapses.extend(synapses)
                    all_questions.extend(result.questions)
                    chunk_meta.append({
                        "idx": i, "len": len(chunk),
                        "neurons": len(neurons), "synapses": len(synapses),
                    })

            # Gleaning pass: find missed facts (BL-16)
            glean = self._s.ingestion.gleaning_enabled
            glean_min = self._s.ingestion.gleaning_threshold
            if glean and all_neurons and len(content) >= glean_min:
                _p("gleaning", "looking for missed facts")
                # Feed full content so gleaning covers the entire document,
                # not just the first 8K chars (LLM context is 200K tokens).
                glean_text = content
                if extraction_session:
                    glean_prompt = build_session_gleaning_user(
                        glean_text,
                        [f"{n.name} ({n.neuron_type})" for n in all_neurons],
                        [s.fact for s in all_synapses],
                        owner_name=self._owner_name,
                    )
                else:
                    glean_prompt = build_gleaning_prompt(
                        glean_text,
                        [f"{n.name} ({n.neuron_type})" for n in all_neurons],
                        [s.fact for s in all_synapses],
                        owner_name=self._owner_name,
                    )
                gl_n, gl_s, gl_q, _ = await self._run_pipeline(
                    signal, _prompt=glean_prompt, _label="gleaning",
                    session=extraction_session,
                    on_progress=on_progress,
                )
                all_neurons.extend(gl_n)
                all_synapses.extend(gl_s)
                all_questions.extend(gl_q)

            # Analytical pass for deep documents (L3)
            if deep and all_neurons:
                _p("analysis", "deep analytical pass")
                ana_prompt = build_analytical_prompt(
                    [f"{n.name} ({n.neuron_type})" for n in all_neurons],
                    [s.fact for s in all_synapses],
                    owner_name=self._owner_name,
                )
                ana_n, ana_s, ana_q, _ = await self._run_pipeline(
                    signal, _prompt=ana_prompt, _label="analysis",
                    on_progress=on_progress,
                )
                for n in ana_n:
                    n.origin = "derived"
                for s in ana_s:
                    s.origin = "derived"
                all_neurons.extend(ana_n)
                all_synapses.extend(ana_s)
                all_questions.extend(ana_q)

            # Signal embedding (once, not per chunk)
            if self._s.semantic.embed_episode_content and content:
                _p("embedding", "signal content")
                signal.content_embedding = await self._c.embedder.embed(
                    content[:self._s.semantic.max_tokens],
                )
                await self._c.driver.execute_query(
                    "MATCH (e:Signal {uuid: $uuid}) "
                    "SET e.content_embedding = $emb",
                    {"uuid": signal.uuid, "emb": signal.content_embedding},
                )

                # File embedding for similarity links
                if source_type == SignalType.file:
                    if len(chunks) > 1 and survey:
                        file_emb = await self._c.embedder.embed(
                            survey[:self._s.semantic.max_tokens],
                        )
                    else:
                        file_emb = signal.content_embedding
                    await self._c.driver.execute_query(
                        "MATCH (e:Signal {uuid: $uuid}) "
                        "SET e.file_embedding = $emb",
                        {"uuid": signal.uuid, "emb": file_emb},
                    )

            # Chunk metadata (always persist count; meta JSON only for multi-chunk)
            if len(chunks) > 1:
                await self._c.driver.execute_query(
                    "MATCH (e:Signal {uuid: $uuid}) "
                    "SET e.chunk_meta = $meta, e.chunk_count = $count",
                    {
                        "uuid":  signal.uuid,
                        "meta":  json.dumps(chunk_meta),
                        "count": len(chunks),
                    },
                )
            else:
                await self._c.driver.execute_query(
                    "MATCH (e:Signal {uuid: $uuid}) SET e.chunk_count = 1",
                    {"uuid": signal.uuid},
                )

            # S3: filter questions by interaction level
            all_questions = filter_questions(
                all_questions, self._s.interaction.level,
            )

            # Owner auto-detect (S1.4)
            await self._auto_detect_owner(all_neurons)

            signal.status = SignalStatus.saved
            await self._update_status(signal)

            ms = int((time.monotonic() - t0) * 1000)
            _p("done", f"{len(all_neurons)} neurons, {len(all_synapses)} synapses ({ms}ms)")
            log.info("signal_saved",
                     neurons=len(all_neurons), synapses=len(all_synapses),
                     questions=len(all_questions),
                     chunks=len(chunks), duration_ms=ms)
            n_contra = sum(
                1 for s in all_synapses if s.attributes.get("contradiction_of")
            )
            Telemetry().record_ingest(
                neurons=len(all_neurons), synapses=len(all_synapses),
                contradictions=n_contra,
            )
            Telemetry().record_tool("add_episode", ms)
            self._last_file_category = file_category
            return signal, all_neurons, all_synapses, all_questions

        except Exception as e:
            signal.status = SignalStatus.failed
            await self._update_status(signal)
            log.error("signal_failed",
                      error_type=type(e).__name__, error=str(e))
            raise

    async def add_file(
        self,
        path:        Path,
        *,
        category:    str        = "",
        source_desc: str        = "",
        domain:      str        = "",
        subdomain:   str        = "",
        on_progress: ProgressFn = None,
    ) -> tuple[Signal, list[Neuron], list[Synapse], list[ExtractedQuestion]]:
        """Ingest file: vault store → extract text → pipeline.

        ``subdomain`` adds a second path level: vault files land in
        ``CORTEX/{domain}/{subdomain}/{bucket}``. Signal only carries
        ``domain`` — subdomain is a vault routing hint, not a graph property.
        """
        # .txt → .md for Obsidian (Obsidian can't parse frontmatter in .txt)
        store_name   = ""
        original_ext = ""
        if self._s.obsidian.enabled and path.suffix == ".txt":
            store_name   = path.stem + ".md"
            original_ext = "txt"

        # Auto-detect domain BEFORE vault.store so files land in the right
        # CORTEX/{domain}/ bucket from day one. Peek at file head for matching.
        if not domain:
            head = ""
            try:
                if path.suffix in (".md", ".txt", ".json", ".csv"):
                    head = path.read_text(
                        encoding="utf-8", errors="replace",
                    )[:2000]
            except OSError:
                head = ""
            domain = self._resolve_domain(head, path.name, source_desc)

        entry   = self._vault.store(
            path, category=category, name=store_name,
            domain=domain, subdomain=subdomain,
        )
        content = self._vault.extract_text(entry)
        if not content:
            raise ExtractionError(f"No text from {path.name}")

        self._last_file_category = ""
        result = await self.add_episode(
            content,
            name        = path.name,
            source_type = SignalType.file,
            source_desc = source_desc or f"file:{entry.relative_path}",
            domain      = domain,
            on_progress = on_progress,
        )

        # Persist vault content_hash on the Signal for drift detection.
        await self._c.driver.execute_query(
            "MATCH (e:Signal {uuid: $uuid}) SET e.content_hash = $hash",
            {"uuid": result[0].uuid, "hash": entry.content_hash},
        )

        # Link signal UUID back to vault index
        self._vault.update_signal_uuid(entry.relative_path, result[0].uuid)

        # Inject Obsidian frontmatter if enabled
        if self._s.obsidian.enabled:
            from mycelium.obsidian.sync import inject_after_ingest
            await inject_after_ingest(
                self._c.driver, self._vault,
                entry.relative_path, result[0].uuid,
                self._s.obsidian,
                original_ext=original_ext,
            )

        return result

    async def ingest_direct(
        self,
        content:     str,
        neurons:     list[dict[str, Any]],
        synapses:    list[dict[str, Any]],
        *,
        name:        str              = "",
        source_type: SignalType       = SignalType.text,
        source_desc: str              = "",
        domain:      str              = "",
        valid_at:    datetime | None  = None,
    ) -> tuple[Signal, list[Neuron], list[Synapse]]:
        """Ingest pre-extracted neurons/synapses (no LLM subprocess).

        Agent extracts neurons/synapses itself, this method handles
        embed → dedup → consolidation → save pipeline.
        """
        clear_contextvars()
        t0 = time.monotonic()

        # Auto-detect domain from DomainBlueprint triggers when not set —
        # aligns with add_signal so daemons/CLI ingests land in
        # CORTEX/{domain}/ and become queryable via signal.domain.
        if not domain:
            domain = self._resolve_domain(content, name, source_desc)

        signal = Signal(
            name        = name or content[:60],
            content     = content,
            source_type = source_type,
            source_desc = source_desc,
            domain      = domain,
            valid_at    = valid_at or datetime.now(UTC),
        )
        bind_contextvars(signal_id=signal.uuid)
        log.info("ingest_direct_started",
                 source_type=source_type.value, content_len=len(content),
                 neurons=len(neurons), synapses=len(synapses))

        await self._save_signal(signal)
        await self.init_owner()

        try:
            result = IngestResult(
                neurons  = [ExtractedNeuron.model_validate(n) for n in neurons],
                synapses = [ExtractedSynapse.model_validate(s) for s in synapses],
            )

            saved_neurons, saved_synapses = await self._process_extracted(
                signal, result,
            )

            # Signal embedding
            if self._s.semantic.embed_episode_content and content:
                signal.content_embedding = await self._c.embedder.embed(
                    content[:self._s.semantic.max_tokens],
                )
                await self._c.driver.execute_query(
                    "MATCH (e:Signal {uuid: $uuid}) "
                    "SET e.content_embedding = $emb",
                    {"uuid": signal.uuid, "emb": signal.content_embedding},
                )

                # File embedding for similarity links
                if source_type == SignalType.file:
                    await self._c.driver.execute_query(
                        "MATCH (e:Signal {uuid: $uuid}) "
                        "SET e.file_embedding = $emb",
                        {"uuid": signal.uuid, "emb": signal.content_embedding},
                    )

            await self._auto_detect_owner(saved_neurons)

            signal.status = SignalStatus.saved
            await self._update_status(signal)

            ms = int((time.monotonic() - t0) * 1000)
            log.info("ingest_direct_saved",
                     neurons=len(saved_neurons), synapses=len(saved_synapses),
                     duration_ms=ms)
            return signal, saved_neurons, saved_synapses

        except Exception as e:
            signal.status = SignalStatus.failed
            await self._update_status(signal)
            log.error("ingest_direct_failed", error=str(e))
            raise

    async def ingest_batch(
        self,
        items:       list[dict[str, Any]],
        *,
        on_progress: ProgressFn = None,
    ) -> list[tuple[Signal, list[Neuron], list[Synapse]]]:
        """Batch ingest with cross-item dedup. One Signal per item."""
        clear_contextvars()
        t0  = time.monotonic()
        max_items = self._s.ingestion.batch_max_items

        if len(items) > max_items:
            raise ExtractionError(
                f"Batch too large: {len(items)} > {max_items}",
            )

        def _p(step: str, detail: str = "") -> None:
            if on_progress:
                on_progress(step, detail)

        await self.init_owner()

        # [1] Create signals + parse extracted data per item
        signals:     list[Signal]         = []
        per_item:    list[IngestResult]    = []
        item_ranges: list[tuple[int, int]] = []  # (start, end) in flat pool

        flat_neurons:  list[ExtractedNeuron]  = []
        flat_synapses: list[ExtractedSynapse] = []

        for it in items:
            content = it.get("content", "")
            if not content:
                log.warning("batch_item_skip", reason="missing content")
                continue
            it_name     = it.get("name") or content[:60]
            it_src_desc = it.get("source_desc", "")
            # Auto-detect domain per-item when not explicitly set.
            it_domain = it.get("domain", "") or self._resolve_domain(
                content, it_name, it_src_desc,
            )
            # Accept valid_at as datetime or ISO string; fallback to now.
            raw_va = it.get("valid_at")
            if isinstance(raw_va, datetime):
                it_valid_at = raw_va
            elif isinstance(raw_va, str) and raw_va:
                try:
                    it_valid_at = datetime.fromisoformat(
                        raw_va.replace("Z", "+00:00"),
                    )
                    if not it_valid_at.tzinfo:
                        it_valid_at = it_valid_at.replace(tzinfo=UTC)
                except ValueError:
                    it_valid_at = datetime.now(UTC)
            else:
                it_valid_at = datetime.now(UTC)

            sig = Signal(
                name        = it_name,
                content     = content,
                source_type = SignalType(it.get("source_type", "text")),
                source_desc = it_src_desc,
                domain      = it_domain,
                valid_at    = it_valid_at,
            )
            signals.append(sig)
            await self._save_signal(sig)

            result = IngestResult(
                neurons  = [ExtractedNeuron.model_validate(n)
                            for n in it.get("neurons", [])],
                synapses = [ExtractedSynapse.model_validate(s)
                            for s in it.get("synapses", [])],
            )
            per_item.append(result)

            start = len(flat_neurons)
            flat_neurons.extend(result.neurons)
            flat_synapses.extend(result.synapses)
            item_ranges.append((start, len(flat_neurons)))

        _p("embedding", f"{len(flat_neurons)} neuron names (batch)")

        # [2] Batch embed all neuron names
        name_vecs = await self._embed_deduped(
            [n.name for n in flat_neurons],
        )

        # [3] Cross-item dedup within batch
        _p("cross_dedup", f"{len(flat_neurons)} neurons")
        flat_neurons, name_vecs, cross_aliases = self._cross_dedup_neurons(
            flat_neurons, name_vecs,
        )

        # [4] Dedup against DB
        _p("dedup", f"{len(flat_neurons)} neurons vs DB")
        neurons, merged, state = await self._dedup_neurons(
            flat_neurons, name_vecs,
        )

        # [5] Build unified name_map (alias-aware: dedup may rename neurons)
        name_map: dict[str, Neuron] = {}
        for ext, neuron in zip(flat_neurons, neurons):
            name_map[neuron.name] = neuron
            if ext.name != neuron.name:
                name_map.setdefault(ext.name, neuron)
        # Resolve cross-dedup aliases (chain: dropped → surviving → DB final)
        for orig, surviving in cross_aliases.items():
            if orig not in name_map and surviving in name_map:
                name_map[orig] = name_map[surviving]

        # [6] Resolve synapses
        dropped = [
            (es.source, es.target)
            for es in flat_synapses
            if es.source not in name_map or es.target not in name_map
        ]
        if dropped:
            log.warning("synapses_unresolved", count=len(dropped), pairs=dropped[:5])

        candidates = [
            (es, name_map[es.source], name_map[es.target])
            for es in flat_synapses
            if es.source in name_map and es.target in name_map
        ]
        syn_vecs = await self._embed_deduped(
            [es.fact for es, _, _ in candidates],
        ) if candidates else []

        _p("resolving", f"{len(candidates)} synapse candidates")
        synapses, dup_uuids = await self._resolve_synapses(
            candidates, syn_vecs, merged,
        )

        # [7] Consolidation (uses signals[0].valid_at — same as _save_all)
        self._apply_consolidation(neurons, merged, state, signals[0])

        # [8] Fill synapse valid_at fallback from signal
        for syn in synapses:
            if syn.valid_at is None:
                syn.valid_at = signals[0].valid_at

        # [9] Save all neurons + synapses via first signal
        _p("saving", f"{len(neurons)}n + {len(synapses)}s → Neo4j")
        await self._save_all(signals[0], neurons, synapses, dup_uuids)

        # [9] Save mentions for remaining signals
        drv = self._c.driver
        for sig in signals[1:]:
            mentions = [
                Mention(source_uuid=sig.uuid, target_uuid=n.uuid)
                for n in neurons
            ]
            if mentions:
                await drv.execute_query(
                    "UNWIND $batch AS m "
                    "MATCH (sig:Signal {uuid: m.sig}), (nrn:Neuron {uuid: m.nrn}) "
                    "CREATE (sig)-[:MENTIONS {"
                    "  uuid: m.uuid, created_at: datetime(m.created_at)"
                    "}]->(nrn)",
                    {"batch": [
                        {
                            "sig":        m.source_uuid,
                            "nrn":        m.target_uuid,
                            "uuid":       m.uuid,
                            "created_at": m.created_at.isoformat(),
                        }
                        for m in mentions
                    ]},
                )

        # [10] Mark all signals saved
        for sig in signals:
            sig.status = SignalStatus.saved
            await self._update_status(sig)

        ms = int((time.monotonic() - t0) * 1000)
        log.info("ingest_batch_done",
                 items=len(items), neurons=len(neurons),
                 synapses=len(synapses), duration_ms=ms)

        # Build per-item results
        results: list[tuple[Signal, list[Neuron], list[Synapse]]] = []
        for sig in signals:
            results.append((sig, neurons, synapses))
        return results

    def _cross_dedup_neurons(
        self,
        neurons:   list[ExtractedNeuron],
        vecs:      list[list[float]],
    ) -> tuple[list[ExtractedNeuron], list[list[float]], dict[str, str]]:
        """Merge duplicates within batch by exact name + cosine >= threshold.

        Returns (neurons, vecs, aliases) where aliases maps each dropped
        neuron name to the surviving neuron name it was merged into.
        """
        thr  = self._s.dedup.cosine_threshold
        seen: dict[str, int] = {}  # norm_name → index in output
        out_neurons: list[ExtractedNeuron] = []
        out_vecs:    list[list[float]]     = []
        aliases: dict[str, str] = {}

        for n, v in zip(neurons, vecs, strict=True):
            norm = n.name.strip().lower()

            # Exact name match
            if norm in seen:
                idx = seen[norm]
                winner = out_neurons[idx]
                # Merge: keep higher confidence
                if n.confidence > winner.confidence:
                    aliases[winner.name] = n.name
                    out_neurons[idx] = n
                    out_vecs[idx]    = v
                else:
                    aliases[n.name] = winner.name
                continue

            # Cosine match against existing in batch
            dup_idx = None
            for idx, ev in enumerate(out_vecs):
                if v and ev and cosine_sim(v, ev) >= thr:
                    dup_idx = idx
                    break

            if dup_idx is not None:
                seen[norm] = dup_idx
                winner = out_neurons[dup_idx]
                if n.confidence > winner.confidence:
                    aliases[winner.name] = n.name
                    out_neurons[dup_idx] = n
                    out_vecs[dup_idx]    = v
                else:
                    aliases[n.name] = winner.name
                log.info("cross_dedup_merged",
                         merged=n.name, into=out_neurons[dup_idx].name)
                continue

            seen[norm] = len(out_neurons)
            out_neurons.append(n)
            out_vecs.append(v)

        if len(out_neurons) < len(neurons):
            log.info("cross_dedup_done",
                     before=len(neurons), after=len(out_neurons))
        return out_neurons, out_vecs, aliases

    async def search(
        self,
        query:       str,
        *,
        top_k:       int | None = None,
        center_uuid: str | None = None,
    ) -> SearchResults:
        """Hybrid search with decay-weighted reranking."""
        if not query.strip():
            return SearchResults()
        return await self._search.search(
            query, top_k=top_k, center_uuid=center_uuid,
        )

    async def re_extract(
        self, signal_uuid: str,
    ) -> tuple[Signal, list[Neuron], list[Synapse]]:
        """Re-run extraction on existing signal."""
        rows = await self._c.driver.execute_query(
            "MATCH (e:Signal {uuid: $uuid}) "
            "RETURN e.content AS content, e.name AS name, "
            "       e.source_type AS source_type, e.source_desc AS source_desc",
            {"uuid": signal_uuid},
        )
        if not rows:
            raise ExtractionError(f"Signal {signal_uuid} not found")

        r = rows[0]
        return await self.add_episode(
            r["content"],
            name        = r["name"],
            source_type = SignalType(r["source_type"]),
            source_desc = r["source_desc"] or "",
        )

    # ── Owner Identity ─────────────────────────────────────

    @property
    def owner_name(self) -> str:
        return self._owner_name

    async def init_owner(self) -> str:
        """Ensure owner neuron exists (or discover from graph). Call once at startup."""
        if self._owner_init:
            return self._owner_name

        drv = self._c.driver
        if self._owner_name:
            # INSTALL PATH: ensure owner neuron in Neo4j
            await drv.execute_query(
                "MERGE (e:Neuron {name: $name}) "
                "ON CREATE SET e.uuid        = randomUUID(), "
                "              e.neuron_type  = 'person', "
                "              e.importance   = 1.0, "
                "              e.confidence   = 1.0, "
                "              e.decay_rate   = 0.001, "
                "              e.confirmations = 0, "
                "              e.freshness    = datetime(), "
                "              e.created_at   = datetime(), "
                "              e.attributes   = $attrs",
                {"name": self._owner_name,
                 "attrs": json.dumps({"is_owner": True})},
            )
            log.info("owner_ensured", name=self._owner_name)
        else:
            # DEV PATH: look for existing is_owner neuron
            rows = await drv.execute_query(
                "MATCH (e:Neuron) "
                "WHERE e.attributes CONTAINS '\"is_owner\": true' "
                "  AND e.name <> 'я' "
                "RETURN e.name AS name LIMIT 1",
            )
            if rows:
                self._owner_name = rows[0]["name"]
                log.info("owner_detected", name=self._owner_name)

        # Resolve owner uuid for node distance reranking (R2.2)
        if self._owner_name:
            rows = await drv.execute_query(
                "MATCH (e:Neuron {name: $name}) "
                "WHERE e.attributes CONTAINS '\"is_owner\": true' "
                "RETURN e.uuid AS uuid LIMIT 1",
                {"name": self._owner_name},
            )
            if rows:
                self._owner_uuid = rows[0]["uuid"]
                self._search.set_owner_uuid(self._owner_uuid)

        self._owner_init = True
        return self._owner_name

    async def set_owner(self, name: str) -> dict[str, Any]:
        """Set or update owner name. Merges 'я' → name if exists."""
        drv = self._c.driver

        # Merge placeholder "я" → real name
        rows = await drv.execute_query(
            "MATCH (e:Neuron {name: 'я'}) "
            "WHERE e.attributes CONTAINS '\"is_owner\": true' "
            "RETURN e.uuid AS uuid",
        )
        if rows:
            uuid = rows[0]["uuid"]
            await drv.execute_query(
                "MATCH (e:Neuron {uuid: $uuid}) "
                "SET e.name = $name",
                {"uuid": uuid, "name": name},
            )
            # Re-embed with real name
            vec = await self._c.embedder.embed(name)
            await drv.execute_query(
                "MATCH (e:Neuron {uuid: $uuid}) "
                "SET e.name_embedding = $vec",
                {"uuid": uuid, "vec": vec},
            )
            log.info("owner_merged", old="я", new=name, uuid=uuid)
        else:
            # No "я" — ensure owner neuron exists
            await drv.execute_query(
                "MERGE (e:Neuron {name: $name}) "
                "ON CREATE SET e.uuid        = randomUUID(), "
                "              e.neuron_type  = 'person', "
                "              e.importance   = 1.0, "
                "              e.confidence   = 1.0, "
                "              e.decay_rate   = 0.001, "
                "              e.confirmations = 0, "
                "              e.freshness    = datetime(), "
                "              e.created_at   = datetime(), "
                "              e.attributes   = $attrs "
                "ON MATCH SET  e.attributes   = $attrs",
                {"name": name, "attrs": json.dumps({"is_owner": True})},
            )
            log.info("owner_set", name=name)

        self._owner_name = name
        return {"status": "ok", "owner": name}

    async def _auto_detect_owner(self, neurons: list[Neuron]) -> None:
        """After extraction: detect if owner name was learned."""
        if self._owner_name:
            return
        for n in neurons:
            attrs = n.attributes
            if attrs.get("is_owner") and n.name != "я":
                self._owner_name = n.name
                log.info("owner_auto_detected", name=n.name)
                # Merge any "я" placeholder
                await self._c.driver.execute_query(
                    "MATCH (old:Neuron {name: 'я'}) "
                    "WHERE old.attributes CONTAINS '\"is_owner\": true' "
                    "  AND old.uuid <> $uuid "
                    "DETACH DELETE old",
                    {"uuid": n.uuid},
                )
                break

    # ── R4.1: Graph Context for Extraction ──────────────────

    async def _build_graph_context(self) -> str:
        """Build graph context section for extraction prompts (R4.1)."""
        if not self._s.ingestion.context_injection:
            return ""
        drv    = self._c.driver
        top_n  = self._s.ingestion.context_top_n
        rec_n  = self._s.ingestion.context_recent_n

        counts = await drv.execute_query(
            "OPTIONAL MATCH (n:Neuron) WHERE n.expired_at IS NULL "
            "  WITH count(n) AS neurons "
            "OPTIONAL MATCH ()-[f:SYNAPSE]->() WHERE f.expired_at IS NULL "
            "RETURN neurons, count(f) AS synapses"
        )
        c = counts[0] if counts else {}
        if not c.get("neurons"):
            return ""

        ew = cypher_effective_weight("e")
        top = await drv.execute_query(
            "MATCH (e:Neuron) WHERE e.expired_at IS NULL "
            f"WITH e, {ew} AS ew "
            "RETURN e.name AS name, e.neuron_type AS type "
            "ORDER BY ew DESC LIMIT $n",
            {"n": top_n},
        )
        recent = await drv.execute_query(
            "MATCH (e:Neuron) WHERE e.expired_at IS NULL "
            f"WITH e, {ew} AS ew "
            "WHERE ew > 0.1 "
            "RETURN e.name AS name, e.neuron_type AS type "
            "ORDER BY e.freshness DESC LIMIT $n",
            {"n": rec_n},
        )
        return build_context_section(
            c.get("neurons", 0), c.get("synapses", 0), top, recent,
        )

    # ── Pipeline (per-chunk: extract → dedup → save) ───────

    async def _run_pipeline(
        self,
        signal:     Signal,
        text:       str      = "",
        survey:     str      = "",
        *,
        owner_name: str      = "",
        _prompt:    str | None = None,
        _label:     str        = "extraction",
        session:    LLMSession | None = None,
        on_progress: ProgressFn = None,
        extraction_focus: str  = "",
    ) -> tuple[list[Neuron], list[Synapse], list[ExtractedQuestion], str]:
        """Extract → embed → dedup → save. Full single-chunk pipeline.

        Automatically uses two-stage extraction (BL-15) for texts above
        deep_extract_threshold: Pass 1 entities, Pass 2 relations.
        """
        two_stage = (
            _prompt is None
            and len(text) >= self._s.ingestion.deep_extract_threshold
        )

        # R1.4: match extraction skill by signal metadata
        skill_guidance = ""
        if _prompt is None:
            skills = load_skills()
            if skills:
                matched = match_skill(
                    skills,
                    source_type=signal.source_type.value,
                    source_desc=signal.source_desc,
                    name=signal.name,
                )
                if matched:
                    skill_guidance = (
                        f"\n## Extraction Skill: {matched.name}\n"
                        f"{matched.content}\n"
                    )

        # R7: domain trigger auto-detection
        if _prompt is None and not extraction_focus:
            domains = load_domains()
            if domains:
                matched_domain = match_domain(
                    domains,
                    content=text,
                    name=signal.name,
                    source_desc=signal.source_desc,
                )
                if matched_domain and matched_domain.extraction.focus:
                    extraction_focus = matched_domain.extraction.focus
                    if matched_domain.extraction.neuron_types:
                        types_hint = ", ".join(matched_domain.extraction.neuron_types)
                        extraction_focus += f"\nPreferred neuron types: {types_hint}"

        # R4.1: graph context injection
        graph_ctx = ""
        if _prompt is None:
            graph_ctx = await self._build_graph_context()

        if two_stage:
            result = await self._two_stage_extract(
                text, survey=survey, owner_name=owner_name,
                extraction_focus=extraction_focus + skill_guidance,
                graph_context=graph_ctx,
                session=session,
                on_progress=on_progress, label=_label,
            )
        else:
            if session and _prompt is None:
                prompt = build_session_extract_user(
                    text, survey=survey, owner_name=owner_name,
                    reference_time=datetime.now(UTC).strftime("%Y-%m-%d"),
                    extraction_focus=extraction_focus + skill_guidance,
                    graph_context=graph_ctx,
                )
            else:
                prompt = _prompt or build_ingest_prompt(
                    text, survey=survey, owner_name=owner_name,
                    interaction_level=self._s.interaction.level,
                    reference_time=datetime.now(UTC).strftime("%Y-%m-%d"),
                    extraction_focus=extraction_focus + skill_guidance,
                    graph_context=graph_ctx,
                )
            log.info(f"{_label}_started", chars=len(text or prompt))
            result = await self._extract(prompt, session=session, on_progress=on_progress)
            log.info(f"{_label}_done",
                     neurons=len(result.neurons), synapses=len(result.synapses),
                     questions=len(result.questions))

        if not result.neurons and not result.synapses:
            return [], [], result.questions, result.file_category

        neurons, synapses = await self._process_extracted(
            signal, result, on_progress=on_progress,
        )
        return neurons, synapses, result.questions, result.file_category

    async def _two_stage_extract(
        self,
        text:       str,
        *,
        survey:     str        = "",
        owner_name: str        = "",
        extraction_focus: str  = "",
        graph_context:    str  = "",
        session:    LLMSession | None = None,
        on_progress: ProgressFn = None,
        label:      str        = "extraction",
    ) -> IngestResult:
        """Two-stage extraction (BL-15): entities first, then relations."""
        def _p(step: str, detail: str = "") -> None:
            if on_progress:
                on_progress(step, detail)

        # Pass 1: extract entities only
        _p("extracting", "two-stage Pass 1 — neurons")
        if session:
            entity_prompt = build_session_entity_user(
                text, survey=survey, owner_name=owner_name,
                reference_time=datetime.now(UTC).strftime("%Y-%m-%d"),
                extraction_focus=extraction_focus,
                graph_context=graph_context,
            )
        else:
            entity_prompt = build_entity_prompt(
                text, survey=survey, owner_name=owner_name,
                interaction_level=self._s.interaction.level,
                reference_time=datetime.now(UTC).strftime("%Y-%m-%d"),
                extraction_focus=extraction_focus,
                graph_context=graph_context,
            )
        log.info(f"{label}_pass1_started", chars=len(text))
        pass1 = await self._extract(entity_prompt, session=session, on_progress=on_progress)
        log.info(f"{label}_pass1_done", neurons=len(pass1.neurons))

        if not pass1.neurons:
            return pass1

        # Pass 2: extract relations — batch if too many neurons
        max_neurons_per_batch = 20
        all_neurons = pass1.neurons
        neuron_descs = [
            f"{n.name} ({n.neuron_type}, conf={n.confidence})"
            for n in all_neurons
        ]

        all_synapses: list[ExtractedSynapse] = []
        all_questions: list[ExtractedQuestion] = []

        # Split into batches of max_neurons_per_batch
        batches = [
            neuron_descs[i:i + max_neurons_per_batch]
            for i in range(0, len(neuron_descs), max_neurons_per_batch)
        ]
        n_batches = len(batches)

        for bi, batch_descs in enumerate(batches):
            batch_label = (
                f"Pass 2 — relations ({len(batch_descs)} neurons"
                f", batch {bi+1}/{n_batches})"
                if n_batches > 1
                else f"two-stage Pass 2 — relations ({len(batch_descs)} neurons)"
            )
            _p("extracting", batch_label)

            if session:
                relation_prompt = build_session_relation_user(
                    text, batch_descs, owner_name=owner_name,
                )
            else:
                relation_prompt = build_relation_prompt(
                    text, batch_descs, owner_name=owner_name,
                )
            log.info(f"{label}_pass2_batch", batch=bi, neurons=len(batch_descs))
            pass2 = await self._extract(relation_prompt, session=session, on_progress=on_progress)
            all_synapses.extend(pass2.synapses)
            all_questions.extend(pass2.questions)

        log.info(f"{label}_pass2_done", synapses=len(all_synapses))

        # Merge: neurons from Pass 1 + synapses from all Pass 2 batches
        return IngestResult(
            neurons   = all_neurons,
            synapses  = all_synapses,
            questions = pass1.questions + all_questions,
        )

    async def _parallel_extract(
        self,
        chunks:      list[str],
        survey:      str,
        *,
        session:     LLMSession | None = None,
        on_progress: ProgressFn = None,
        extraction_focus: str   = "",
    ) -> list[IngestResult]:
        """Run LLM extraction on all chunks in parallel (semaphore-limited).

        When session_enabled, each chunk gets its OWN LLMSession so they
        can run in parallel (one shared session forces sequential execution).
        """
        n       = len(chunks)
        max_par = self._s.ingestion.max_parallel_chunks
        sem     = asyncio.Semaphore(max_par)

        thresh    = self._s.ingestion.deep_extract_threshold
        graph_ctx = await self._build_graph_context()  # R4.1: build once

        # Per-chunk sessions: each starts fresh but carries system prompt,
        # so Claude gets context on every call without serialising the queue.
        sys_prompt = session.system_prompt if session else None

        async def _one(i: int, chunk: str) -> IngestResult:
            async with sem:
                prefix = f"chunk {i + 1}/{n}"
                chunk_cb: ProgressFn = None
                if on_progress:
                    def chunk_cb(step: str, detail: str = "", _pf: str = prefix) -> None:
                        on_progress(step, f"{_pf} — {detail}" if detail else _pf)

                chunk_session: LLMSession | None = (
                    LLMSession(system_prompt=sys_prompt) if sys_prompt else None
                )
                try:
                    result = await self._extract_chunk(
                        chunk, i, n, thresh, survey, graph_ctx,
                        extraction_focus, chunk_session, chunk_cb,
                    )
                except Exception as exc:
                    log.warning("chunk_session_retry", idx=i,
                                error=type(exc).__name__,
                                reason="extraction failed, retrying without session")
                    if on_progress:
                        on_progress("extracting", f"{prefix} — retry (no session)")
                    result = await self._extract_chunk(
                        chunk, i, n, thresh, survey, graph_ctx,
                        extraction_focus, None, chunk_cb,
                    )

                log.info("chunk_extract_done", idx=i,
                         neurons=len(result.neurons), synapses=len(result.synapses))
                if on_progress:
                    on_progress("extracted", f"{prefix} — "
                                f"{len(result.neurons)}n {len(result.synapses)}s")
                return result

        results = await asyncio.gather(
            *[_one(i, c) for i, c in enumerate(chunks)],
            return_exceptions=True,
        )
        out: list[IngestResult] = []
        for i, r in enumerate(results):
            if isinstance(r, BaseException):
                log.error("chunk_extract_failed", idx=i, error=str(r))
                out.append(IngestResult())
            else:
                out.append(r)
        return out

    async def _extract_chunk(
        self,
        chunk: str, i: int, n: int, thresh: int,
        survey: str, graph_ctx: str, extraction_focus: str,
        session: LLMSession | None, on_progress: ProgressFn,
    ) -> IngestResult:
        """Extract a single chunk (two-stage or single-shot)."""
        prefix = f"chunk {i + 1}/{n}"
        if len(chunk) >= thresh:
            log.info("chunk_twostage_started", idx=i, total=n)
            return await self._two_stage_extract(
                chunk, survey=survey, owner_name=self._owner_name,
                extraction_focus=extraction_focus,
                graph_context=graph_ctx,
                session=session,
                on_progress=on_progress, label=f"chunk_{i}",
            )
        else:
            if session:
                prompt = build_session_extract_user(
                    chunk, survey=survey, owner_name=self._owner_name,
                    reference_time=datetime.now(UTC).strftime("%Y-%m-%d"),
                    extraction_focus=extraction_focus,
                    graph_context=graph_ctx,
                )
            else:
                prompt = build_ingest_prompt(
                    chunk, survey=survey, owner_name=self._owner_name,
                    interaction_level=self._s.interaction.level,
                    reference_time=datetime.now(UTC).strftime("%Y-%m-%d"),
                    extraction_focus=extraction_focus,
                    graph_context=graph_ctx,
                )
            log.info("chunk_extract_started", idx=i, total=n)
            if on_progress:
                pk = len(prompt) // 1024
                on_progress("extracting", f"{prefix} — LLM ({pk}K)")
            return await self._extract(prompt, session=session, on_progress=on_progress)

    async def _process_extracted(
        self,
        signal:      Signal,
        result:      IngestResult,
        *,
        on_progress: ProgressFn = None,
    ) -> tuple[list[Neuron], list[Synapse]]:
        """Embed → dedup → resolve → save. Sequential post-extraction processing."""
        def _p(step: str, detail: str = "") -> None:
            if on_progress:
                on_progress(step, detail)

        # [1] Embed neuron names
        _p("embedding", f"{len(result.neurons)} neuron names")
        name_vecs = await self._embed_deduped(
            [n.name for n in result.neurons],
        )

        # [2] Dedup neurons (exact + vector)
        _p("dedup", f"{len(result.neurons)} neurons")
        neurons, merged, state = await self._dedup_neurons(
            result.neurons, name_vecs,
        )

        # [3] Build synapse candidates (alias-aware: dedup may rename neurons)
        name_map: dict[str, Neuron] = {}
        for ext, neuron in zip(result.neurons, neurons):
            name_map[neuron.name] = neuron
            if ext.name != neuron.name:
                name_map.setdefault(ext.name, neuron)

        dropped = [
            (es.source, es.target)
            for es in result.synapses
            if es.source not in name_map or es.target not in name_map
        ]
        if dropped:
            log.warning("synapses_unresolved", count=len(dropped), pairs=dropped[:5])

        candidates = [
            (es, name_map[es.source], name_map[es.target])
            for es in result.synapses
            if es.source in name_map and es.target in name_map
        ]

        # [4] Embed synapse texts
        if candidates:
            _p("embedding", f"{len(candidates)} synapses")
        syn_vecs = await self._embed_deduped(
            [es.fact for es, _, _ in candidates],
        ) if candidates else []

        # [5] Resolve synapses (vector dedup)
        _p("resolving", f"{len(candidates)} synapse candidates")
        synapses, dup_uuids = await self._resolve_synapses(
            candidates, syn_vecs, merged,
        )

        # [6] Consolidation (freshness/created_at from signal.valid_at)
        self._apply_consolidation(neurons, merged, state, signal)

        # [7] Fill synapse valid_at fallback from signal
        for syn in synapses:
            if syn.valid_at is None:
                syn.valid_at = signal.valid_at

        # [8] Save (batch)
        _p("saving", f"{len(neurons)}n + {len(synapses)}s → Neo4j")
        await self._save_all(signal, neurons, synapses, dup_uuids)

        return neurons, synapses

    async def _extract(
        self, prompt: str, *,
        session:     LLMSession | None = None,
        on_progress: ProgressFn        = None,
    ) -> IngestResult:
        """LLM call → IngestResult."""
        llm_cb = (lambda d: on_progress("extracting", d)) if on_progress else None
        try:
            data = await self._c.llm.generate(
                prompt, session=session, on_progress=llm_cb,
            )
            return IngestResult.model_validate(data)
        except ExtractionError:
            raise
        except Exception as e:
            raise ExtractionError(f"Parse error: {e}") from e

    async def _survey(
        self, content: str, *, on_progress: ProgressFn = None,
    ) -> str:
        """Pass 1: document overview for deep extraction (L3)."""
        prompt = build_survey_prompt(content)
        log.info("survey_started", chars=len(content))
        llm_cb = (lambda d: on_progress("survey", d)) if on_progress else None
        try:
            text = await self._c.llm.generate_text(prompt, on_progress=llm_cb)
            log.info("survey_done", len=len(text))
            return text
        except Exception as e:
            log.warning("survey_failed", error=str(e))
            return ""

    # ── Embedding with dedup (P0.3) ────────────────────────

    async def _embed_deduped(self, texts: list[str]) -> list[list[float]]:
        """Embed batch with text dedup."""
        if not texts:
            return []
        seen: dict[str, int] = {}
        uniq: list[str]      = []
        idxs: list[int]      = []
        for t in texts:
            if t not in seen:
                seen[t] = len(uniq)
                uniq.append(t)
            idxs.append(seen[t])
        vecs = await self._c.embedder.embed_batch(uniq)
        return [vecs[i] for i in idxs]

    # ── Neuron Dedup (P0.1) ────────────────────────────────

    async def _dedup_neurons(
        self,
        extracted: list[Any],
        name_vecs: list[list[float]],
    ) -> tuple[list[Neuron], set[str], list[dict[str, Any]]]:
        """Exact + vector + LLM grey-zone dedup → (neurons, merged_uuids, state)."""
        # Batch exact match via Neo4j
        norms = [n.name.strip().lower() for n in extracted]
        rows  = await self._c.driver.execute_query(
            "MATCH (e:Neuron) "
            "WHERE toLower(trim(e.name)) IN $norms "
            "  AND e.expired_at IS NULL "
            "RETURN e.uuid          AS uuid, "
            "       e.name          AS name, "
            "       e.neuron_type   AS neuron_type, "
            "       coalesce(e.importance, e.confidence) AS importance, "
            "       e.confidence    AS confidence, "
            "       e.decay_rate    AS decay_rate, "
            "       e.confirmations AS confirmations",
            {"norms": list(set(norms))},
        )
        exact = {r["name"].strip().lower(): r for r in rows}

        neurons: list[Neuron]      = []
        merged:  set[str]          = set()
        state:   dict[str, dict[str, Any]] = {}
        # Grey-zone: (index_in_neurons, extracted, match_candidate, vec)
        grey_pending: list[tuple[int, Any, dict[str, Any], list[float]]] = []

        for ext, vec, norm in zip(extracted, name_vecs, norms, strict=True):
            match = exact.get(norm)

            # Vector fallback for unmatched (>= cosine_threshold)
            if not match and vec:
                match = await self._vector_match(vec)

            # Merge insights into attributes (L3)
            attrs = dict(ext.attributes)
            if ext.insights:
                attrs["insights"] = ext.insights

            if match and match["uuid"] not in merged:
                state[match["uuid"]] = match
                n = Neuron(
                    uuid          = match["uuid"],
                    name          = match["name"],
                    neuron_type   = match["neuron_type"] or ext.neuron_type,
                    importance    = match.get("importance") or match.get("confidence", 1.0),
                    confidence    = match.get("importance") or match.get("confidence", 1.0),
                    decay_rate    = match.get("decay_rate", 0.008),
                    confirmations = match.get("confirmations", 0),
                    attributes    = attrs,
                )
                n.name_embedding = vec
                neurons.append(n)
                merged.add(match["uuid"])
                log.info("neuron_deduped",
                         extracted=ext.name, merged_with=match["name"])
            else:
                n = Neuron(
                    name        = ext.name,
                    neuron_type = ext.neuron_type,
                    importance  = ext.confidence,
                    confidence  = ext.confidence,
                    attributes  = attrs,
                    expires_at  = _parse_date(ext.expires_at),
                )
                n.name_embedding = vec
                idx = len(neurons)
                neurons.append(n)

                # Check grey zone for LLM dedup
                if vec and self._s.dedup.llm_enabled:
                    grey_match = await self._vector_match_grey(vec)
                    if grey_match and grey_match["uuid"] not in merged:
                        grey_pending.append((idx, ext, grey_match, vec))

        # Batch LLM dedup for grey-zone candidates
        if grey_pending:
            log.info("llm_dedup_started", pairs=len(grey_pending))
            llm_results = await self._llm_dedup_batch(grey_pending)
            for idx, ext, grey_match, vec in grey_pending:
                if idx in llm_results:
                    match = llm_results[idx]
                    attrs = dict(ext.attributes)
                    if ext.insights:
                        attrs["insights"] = ext.insights
                    state[match["uuid"]] = match
                    neurons[idx] = Neuron(
                        uuid          = match["uuid"],
                        name          = match["name"],
                        neuron_type   = match["neuron_type"] or ext.neuron_type,
                        importance    = match.get("importance") or match.get("confidence", 1.0),
                        confidence    = match.get("importance") or match.get("confidence", 1.0),
                        decay_rate    = match.get("decay_rate", 0.008),
                        confirmations = match.get("confirmations", 0),
                        attributes    = attrs,
                    )
                    merged.add(match["uuid"])

        return neurons, merged, list(state.values())

    async def _vector_match(
        self, vec: list[float],
    ) -> dict[str, Any] | None:
        """Find similar neuron via Neo4j vector index (>= cosine_threshold)."""
        try:
            rows = await self._c.driver.execute_query(
                "CALL db.index.vector.queryNodes('neuron_name_emb', 1, $vec) "
                "YIELD node AS e, score "
                "WHERE score >= $thr AND e.expired_at IS NULL "
                "RETURN e.uuid          AS uuid, "
                "       e.name          AS name, "
                "       e.neuron_type   AS neuron_type, "
                "       coalesce(e.importance, e.confidence) AS importance, "
                "       e.confidence    AS confidence, "
                "       e.decay_rate    AS decay_rate, "
                "       e.confirmations AS confirmations",
                {"vec": vec, "thr": self._s.dedup.cosine_threshold},
            )
            return rows[0] if rows else None
        except Exception as e:
            log.warning("vector_match_failed", error=str(e))
            return None

    async def _vector_match_grey(
        self, vec: list[float],
    ) -> dict[str, Any] | None:
        """Find neuron in grey zone: [llm_threshold, cosine_threshold)."""
        try:
            rows = await self._c.driver.execute_query(
                "CALL db.index.vector.queryNodes('neuron_name_emb', 1, $vec) "
                "YIELD node AS e, score "
                "WHERE score >= $lo AND score < $hi "
                "  AND e.expired_at IS NULL "
                "RETURN e.uuid          AS uuid, "
                "       e.name          AS name, "
                "       e.neuron_type   AS neuron_type, "
                "       coalesce(e.importance, e.confidence) AS importance, "
                "       e.confidence    AS confidence, "
                "       e.decay_rate    AS decay_rate, "
                "       e.confirmations AS confirmations, "
                "       score",
                {
                    "vec": vec,
                    "lo":  self._s.dedup.llm_threshold,
                    "hi":  self._s.dedup.cosine_threshold,
                },
            )
            return rows[0] if rows else None
        except Exception as e:
            log.warning("vector_match_grey_failed", error=str(e))
            return None

    async def _llm_dedup_batch(
        self,
        pending: list[tuple[int, Any, dict[str, Any], list[float]]],
    ) -> dict[int, dict[str, Any]]:
        """LLM dedup for grey-zone pairs. Returns {idx: match} for SAME verdicts."""
        if not pending or not self._s.dedup.llm_enabled:
            return {}

        # Build pairs for prompt: (id, name_a, type_a, name_b, type_b, facts_a, facts_b)
        pairs: list[tuple[int, str, str, str, str, list[str], list[str]]] = []
        # Load facts for existing neurons
        existing_uuids = [m["uuid"] for _, _, m, _ in pending]
        facts_by_uuid: dict[str, list[str]] = {}
        if existing_uuids:
            try:
                rows = await self._c.driver.execute_query(
                    "MATCH (a:Neuron)-[s:SYNAPSE]->(b:Neuron) "
                    "WHERE a.uuid IN $uuids AND s.expired_at IS NULL "
                    "RETURN a.uuid AS uuid, s.fact AS fact "
                    "LIMIT 30",
                    {"uuids": existing_uuids},
                )
                for r in rows:
                    if r["fact"]:
                        facts_by_uuid.setdefault(r["uuid"], []).append(r["fact"])
            except Exception as e:
                log.warning("llm_dedup_facts_load_failed", error=str(e))

        for idx, ext, match, _vec in pending:
            pairs.append((
                idx, ext.name, ext.neuron_type,
                match["name"], match.get("neuron_type", ""),
                [],  # no facts for extracted (new)
                facts_by_uuid.get(match["uuid"], []),
            ))

        # Batch by llm_batch_size
        results: dict[int, dict[str, Any]] = {}
        bs = self._s.dedup.llm_batch_size
        for i in range(0, len(pairs), bs):
            batch = pairs[i:i + bs]
            prompt = build_dedup_prompt(batch)
            try:
                data = await self._c.llm.generate(prompt)
                dr = DedupResult.model_validate(data)
                for p in dr.pairs:
                    if p.verdict.upper() == "SAME":
                        # Find the match dict for this pair id
                        for idx, _ext, match, _vec in pending:
                            if idx == p.id:
                                results[idx] = match
                                log.info("llm_dedup_same",
                                         extracted=_ext.name,
                                         existing=match["name"])
                                break
            except Exception as e:
                log.warning("llm_dedup_failed", error=str(e))

        return results

    # ── Synapse Resolution (P0.1: vector dedup + R1.1: contradiction) ──

    async def _resolve_synapses(
        self,
        candidates: list[tuple[Any, Neuron, Neuron]],
        syn_vecs:   list[list[float]],
        merged:     set[str],
    ) -> tuple[list[Synapse], list[str]]:
        """Vector dedup + contradiction detection. Returns (synapses, dup_uuids)."""
        # Load existing synapse data for merged neurons
        existing: list[dict[str, Any]] = []
        if merged:
            rows = await self._c.driver.execute_query(
                "MATCH (e:Neuron)-[f:SYNAPSE]->() "
                "WHERE e.uuid IN $uuids AND f.expired_at IS NULL "
                "RETURN f.uuid AS uuid, f.fact_embedding AS emb, "
                "       f.fact AS fact, f.confidence AS conf",
                {"uuids": list(merged)},
            )
            existing = [
                {"uuid": r["uuid"], "emb": r["emb"],
                 "fact": r["fact"], "conf": r["conf"]}
                for r in rows if r.get("emb")
            ]

        synapses:  list[Synapse] = []
        dup_uuids: list[str]     = []
        contra_cfg = self._s.contradiction

        # Collect contradiction candidates for batch LLM call
        # (pair_id, extracted_synapse, src, tgt, vec, existing_uuid)
        contra_batch: list[tuple[int, Any, Neuron, Neuron,
                                 list[float], str]] = []

        for (es, src, tgt), vec in zip(candidates, syn_vecs, strict=True):
            dup        = None
            best_match = None          # (uuid, fact, cosine)

            for ex in existing:
                sim = cosine_sim(vec, ex["emb"])
                if sim >= 0.95:
                    dup = ex["uuid"]
                    break
                if (contra_cfg.enabled
                    and sim >= contra_cfg.cosine_threshold
                    and (best_match is None or sim > best_match[2])):
                    best_match = (ex["uuid"], ex["fact"], sim)

            if dup:
                dup_uuids.append(dup)
                continue

            if best_match is not None:
                contra_batch.append((
                    len(contra_batch), es, src, tgt, vec, best_match[0],
                ))
                continue

            synapses.append(_make_synapse(es, src, tgt, vec))

        # ── Contradiction classification (R1.1) ──────────────
        if contra_batch:
            ex_facts = {ex["uuid"]: ex["fact"] for ex in existing}
            prompt_pairs = [
                (pid, es.fact, ex_facts[ex_uuid])
                for pid, es, _, _, _, ex_uuid in contra_batch
            ]
            verdicts = await self._classify_contradictions(prompt_pairs)
            resolved = {pid for pid, _, _ in verdicts}

            for pid, es, src, tgt, vec, ex_uuid in contra_batch:
                if pid not in resolved:
                    # LLM failed for this pair — treat as new synapse
                    synapses.append(_make_synapse(es, src, tgt, vec))
                    continue

                verdict, conf = next(
                    (v, c) for p, v, c in verdicts if p == pid
                )
                if verdict == "CONFIRM":
                    dup_uuids.append(ex_uuid)
                elif (verdict == "SUPERSEDE"
                      and conf >= contra_cfg.auto_expire_confidence):
                    syn = _make_synapse(es, src, tgt, vec)
                    syn.attributes["contradicts"] = ex_uuid
                    synapses.append(syn)
                    log.info("synapse_superseded",
                             new=syn.fact[:80], old_uuid=ex_uuid)
                else:
                    # CONTRADICT or low-confidence SUPERSEDE — keep both
                    syn = _make_synapse(es, src, tgt, vec)
                    syn.attributes["contradiction_of"] = ex_uuid
                    synapses.append(syn)
                    log.info("synapse_contradiction",
                             new=syn.fact[:80], old_uuid=ex_uuid)

            log.info("contradiction_resolved",
                     total=len(contra_batch),
                     confirm=sum(1 for _, v, _ in verdicts if v == "CONFIRM"),
                     supersede=sum(1 for _, v, _ in verdicts
                                   if v == "SUPERSEDE"),
                     contradict=sum(1 for _, v, _ in verdicts
                                    if v == "CONTRADICT"))

        return synapses, dup_uuids

    async def _classify_contradictions(
        self,
        pairs: list[tuple[int, str, str]],
    ) -> list[tuple[int, str, float]]:
        """Classify fact pairs via LLM. Returns [(pair_id, verdict, confidence)]."""
        cfg     = self._s.contradiction
        results: list[tuple[int, str, float]] = []

        for i in range(0, len(pairs), cfg.llm_batch_size):
            batch = pairs[i:i + cfg.llm_batch_size]
            try:
                prompt = build_contradiction_prompt(batch)
                raw    = await self._c.llm.generate(prompt)
                parsed = ContradictionResult.model_validate(raw)
                for p in parsed.pairs:
                    if p.verdict in ("CONFIRM", "SUPERSEDE", "CONTRADICT"):
                        results.append((p.id, p.verdict, p.confidence))
            except Exception as e:
                log.warning("contradiction_classify_failed", error=str(e))

        return results

    # ── Consolidation ──────────────────────────────────────

    def _apply_consolidation(
        self,
        neurons:      list[Neuron],
        merged_uuids: set[str],
        existing:     list[dict[str, Any]],
        signal:       Signal,
    ) -> None:
        """Update decay/freshness for re-mentioned neurons.

        Freshness/created_at come from signal.valid_at — so historical data
        ages correctly (MAX logic in Cypher keeps the most recent mention).
        """
        state_by_uuid = {e["uuid"]: e for e in existing}
        sv            = signal.valid_at

        for neuron in neurons:
            if neuron.uuid in merged_uuids:
                st   = state_by_uuid.get(neuron.uuid, {})
                imp  = st.get("importance") or st.get("confidence", neuron.importance)
                cnt  = st.get("confirmations", neuron.confirmations)

                new_imp, new_rate, new_count = consolidate(
                    imp, cnt, self._s.decay,
                )
                neuron.importance    = new_imp
                neuron.confidence    = new_imp
                neuron.decay_rate    = new_rate
                neuron.confirmations = new_count
            else:
                neuron.decay_rate    = calc_decay_rate(0, self._s.decay)
                neuron.confirmations = 0
                neuron.created_at    = sv

            neuron.freshness = sv

    # ── Domain resolution ─────────────────────────────────

    def _resolve_domain(
        self,
        content:     str,
        name:        str,
        source_desc: str,
    ) -> str:
        """Match a DomainBlueprint by triggers. Returns domain name or ''."""
        try:
            domains = load_domains()
        except Exception:
            return ""
        if not domains:
            return ""
        matched = match_domain(
            domains,
            content     = content or "",
            name        = name or "",
            source_desc = source_desc or "",
        )
        return matched.name if matched else ""

    # ── Neo4j Operations ──────────────────────────────────

    async def _save_signal(self, signal: Signal) -> None:
        # MERGE is idempotent: when a placeholder Signal was pre-created by
        # the async MCP path (_start_bg_extraction), this preserves its uuid +
        # status=extracting while adding the full payload on first save.
        await self._c.driver.execute_query(
            "MERGE (e:Signal {uuid: $uuid}) "
            "ON CREATE SET "
            "  e.name = $name, e.content = $content,"
            "  e.content_embedding = $emb,"
            "  e.source_type = $src_type, e.source_desc = $src_desc,"
            "  e.domain = $domain, e.content_hash = $content_hash,"
            "  e.chunk_count = $chunk_count,"
            "  e.status = $status,"
            "  e.valid_at = datetime($valid_at),"
            "  e.created_at = datetime($created_at) "
            "ON MATCH SET "
            "  e.name = coalesce(e.name, $name),"
            "  e.content = coalesce(e.content, $content),"
            "  e.source_type = coalesce(e.source_type, $src_type),"
            "  e.source_desc = coalesce(e.source_desc, $src_desc),"
            "  e.domain = CASE WHEN coalesce(e.domain, '') = '' "
            "                  THEN $domain ELSE e.domain END,"
            "  e.content_hash = coalesce(e.content_hash, $content_hash),"
            "  e.valid_at = coalesce(e.valid_at, datetime($valid_at))",
            {
                "uuid":         signal.uuid,
                "name":         signal.name,
                "content":      signal.content,
                "emb":          signal.content_embedding or [],
                "src_type":     signal.source_type.value,
                "src_desc":     signal.source_desc,
                "domain":       signal.domain,
                "content_hash": signal.content_hash,
                "chunk_count":  signal.chunk_count,
                "status":       signal.status.value,
                "valid_at":     signal.valid_at.isoformat(),
                "created_at":   signal.created_at.isoformat(),
            },
        )

    async def _update_status(self, signal: Signal) -> None:
        await self._c.driver.execute_query(
            "MATCH (e:Signal {uuid: $uuid}) SET e.status = $status",
            {"uuid": signal.uuid, "status": signal.status.value},
        )

    async def _save_all(
        self,
        signal:         Signal,
        neurons:        list[Neuron],
        synapses:       list[Synapse],
        dup_syn_uuids:  list[str],
    ) -> None:
        """P0.2: batch UNWIND queries (3-5 queries instead of N+1).

        Wraps all writes in a single transaction — partial crashes roll back
        cleanly instead of leaving orphan neurons/synapses/mentions.
        """
        async def _work(run: Any) -> None:
            # ── Neurons batch ──────────────────────────────
            if neurons:
                await run(
                    "UNWIND $batch AS e "
                    "MERGE (n:Neuron {uuid: e.uuid}) "
                    "SET n.name           = e.name, "
                    "    n.neuron_type    = e.neuron_type, "
                    "    n.name_embedding = CASE WHEN size(e.name_emb) > 0 "
                    "                        THEN e.name_emb "
                    "                        ELSE n.name_embedding END, "
                    "    n.importance     = e.importance, "
                    "    n.confidence     = e.importance, "
                    "    n.decay_rate     = e.decay_rate, "
                    "    n.confirmations  = e.confirmations, "
                    "    n.freshness      = CASE "
                    "                        WHEN n.freshness IS NULL "
                    "                          OR datetime(e.freshness) > n.freshness "
                    "                        THEN datetime(e.freshness) "
                    "                        ELSE n.freshness END, "
                    "    n.attributes     = e.attrs, "
                    "    n.origin         = e.origin, "
                    "    n.created_at     = coalesce(n.created_at, datetime(e.created_at)), "
                    "    n.expires_at     = CASE WHEN e.expires_at IS NOT NULL "
                    "                        THEN datetime(e.expires_at) END",
                    {"batch": [
                        {
                            "uuid":          n.uuid,
                            "name":          n.name,
                            "neuron_type":   n.neuron_type,
                            "name_emb":      n.name_embedding,
                            "importance":    n.importance,
                            "decay_rate":    n.decay_rate,
                            "confirmations": n.confirmations,
                            "freshness":     n.freshness.isoformat(),
                            "attrs":         json.dumps(n.attributes),
                            "origin":        n.origin,
                            "created_at":    n.created_at.isoformat(),
                            "expires_at":    n.expires_at.isoformat() if n.expires_at else None,
                        }
                        for n in neurons
                    ]},
                )

            # ── Cascade invalidation (R5.2, opt-in) ────────
            if (neurons and self._s.ingestion.cascade_invalidation):
                merged = [n.uuid for n in neurons if n.confirmations > 0]
                if merged:
                    await run(
                        "UNWIND $uuids AS uuid "
                        "MATCH (n:Neuron {uuid: uuid})-[r:SYNAPSE]->() "
                        "WHERE r.origin = 'derived' "
                        "SET r.needs_recompute = true",
                        {"uuids": merged},
                    )

            # ── Synapses batch ─────────────────────────────
            if synapses:
                await run(
                    "UNWIND $batch AS f "
                    "MATCH (s:Neuron {uuid: f.src}), (t:Neuron {uuid: f.tgt}) "
                    "CREATE (s)-[r:SYNAPSE {"
                    "  uuid: f.uuid, fact: f.fact, fact_embedding: f.emb,"
                    "  relation: f.rel, episodes: f.episodes,"
                    "  confidence: f.conf, origin: f.origin,"
                    "  created_at: datetime(f.created_at)"
                    "}]->(t) "
                    "SET r.valid_at   = CASE WHEN f.valid_at IS NOT NULL "
                    "                     THEN datetime(f.valid_at) END, "
                    "    r.invalid_at = CASE WHEN f.invalid_at IS NOT NULL "
                    "                     THEN datetime(f.invalid_at) END, "
                    "    r.contradiction_of = f.contradiction_of",
                    {"batch": [
                        {
                            "src":        s.source_uuid,
                            "tgt":        s.target_uuid,
                            "uuid":       s.uuid,
                            "fact":       s.fact,
                            "emb":        s.fact_embedding,
                            "rel":        s.relation,
                            "episodes":   [signal.uuid],
                            "conf":       s.confidence,
                            "origin":     s.origin,
                            "created_at": s.created_at.isoformat(),
                            "valid_at":   s.valid_at.isoformat() if s.valid_at else None,
                            "contradiction_of": s.attributes.get("contradiction_of"),
                            "invalid_at": (s.invalid_at.isoformat()
                                           if s.invalid_at else None),
                        }
                        for s in synapses
                    ]},
                )

            # ── Expire contradicted synapses ───────────────
            contradicts = [
                s.attributes["contradicts"]
                for s in synapses if "contradicts" in s.attributes
            ]
            if contradicts:
                await run(
                    "UNWIND $uuids AS uuid "
                    "MATCH ()-[r:SYNAPSE {uuid: uuid}]->() "
                    "SET r.expired_at = datetime()",
                    {"uuids": contradicts},
                )

            # ── Duplicate synapses provenance ──────────────
            if dup_syn_uuids:
                await run(
                    "UNWIND $batch AS d "
                    "MATCH ()-[f:SYNAPSE {uuid: d.uuid}]->() "
                    "WHERE NOT d.ep IN f.episodes "
                    "SET f.episodes   = f.episodes + d.ep, "
                    "    f.confidence = CASE WHEN f.confidence + d.boost <= 1.0 "
                    "                     THEN f.confidence + d.boost ELSE 1.0 END",
                    {"batch": [
                        {
                            "uuid":  u,
                            "ep":    signal.uuid,
                            "boost": self._s.decay.evidence_boost,
                        }
                        for u in dup_syn_uuids
                    ]},
                )

            # ── Mentions batch ─────────────────────────────
            if neurons:
                mentions = [
                    Mention(source_uuid=signal.uuid, target_uuid=n.uuid)
                    for n in neurons
                ]
                await run(
                    "UNWIND $batch AS m "
                    "MATCH (sig:Signal {uuid: m.sig}), (nrn:Neuron {uuid: m.nrn}) "
                    "CREATE (sig)-[:MENTIONS {"
                    "  uuid: m.uuid, created_at: datetime(m.created_at)"
                    "}]->(nrn)",
                    {"batch": [
                        {
                            "sig":        m.source_uuid,
                            "nrn":        m.target_uuid,
                            "uuid":       m.uuid,
                            "created_at": m.created_at.isoformat(),
                        }
                        for m in mentions
                    ]},
                )

        await self._c.driver.run_in_transaction(_work)

    # ── Summary Generation (P0.4: top-N) ──────────────────

    async def refresh_summaries(self) -> int:
        """Generate summaries with top-N synapses by confidence."""
        rows = await self._c.driver.execute_query(
            "MATCH (e:Neuron) "
            "WHERE e.summary IS NULL OR e.summary = '' "
            "MATCH (e)-[f:SYNAPSE]->() "
            "WHERE f.expired_at IS NULL AND f.fact IS NOT NULL "
            "WITH e, f ORDER BY f.confidence DESC "
            "WITH e, collect(f.fact)[..$top_n] AS facts "
            "WHERE size(facts) >= $min "
            "RETURN e.uuid AS uuid, e.name AS name, "
            "       e.neuron_type AS neuron_type, facts",
            {"min": self._s.summary.min_facts, "top_n": self._s.summary.top_n},
        )

        count = 0
        for r in rows:
            try:
                prompt  = build_summary_prompt(r["name"], r["neuron_type"], r["facts"])
                summary = await self._c.llm.generate_text(prompt)
                emb     = await self._c.embedder.embed(summary)

                await self._c.driver.execute_query(
                    "MATCH (e:Neuron {uuid: $uuid}) "
                    "SET e.summary = $summary, e.summary_embedding = $emb",
                    {"uuid": r["uuid"], "summary": summary.strip(), "emb": emb},
                )
                count += 1
                log.info("summary_generated", neuron=r["name"])
            except Exception as e:
                log.warning("summary_failed", neuron=r["name"], error=str(e))

        return count


# ── Helpers ────────────────────────────────────────────────


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        from datetime import date as _date
        d = _date.fromisoformat(s)
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _make_synapse(es: Any, src: Neuron, tgt: Neuron,
                  vec: list[float]) -> Synapse:
    """Build Synapse from extracted data + resolved neurons."""
    syn = Synapse(
        source_uuid = src.uuid,
        target_uuid = tgt.uuid,
        relation    = es.relation,
        fact        = es.fact,
        confidence  = es.confidence,
        valid_at    = _parse_date(es.valid_at),
    )
    syn.fact_embedding = vec
    return syn
