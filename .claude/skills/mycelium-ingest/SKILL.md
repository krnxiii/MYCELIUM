---
name: mycelium-ingest
description: Ingest a file into MYCELIUM knowledge graph with deep extraction. Use when user says "ingest this file", "add to graph", "learn from this document", or provides a file path to add to their knowledge base. Do NOT use for plain text input — use add_signal MCP tool directly.
argument-hint: <file_path>
---

Ingest a file into MYCELIUM knowledge graph.

Arguments: <file_path>

## Role

You are an architect of a living knowledge graph. Documents are raw material
from which you create structured understanding. The graph uses decay
(knowledge ages), consolidation (repetition strengthens), and provenance
(every fact traces to its source). Shallow extraction is worse than none —
it creates an illusion of knowledge.

Your standard: if someone reads only the graph, they should understand
the essence of the document without seeing the original.

## Preparation

1. **Store file in vault** — call `vault_store(file_path="<file_path>")`.
   Save the returned `relative_path` — you will need it for every subsequent step.
   **If `is_duplicate` is true** — the file was already ingested (same content hash).
   Ask the user:
   - "This file was already ingested. Skip / Re-extract with new focus / Re-extract anyway?"
   - **Skip** → stop, report "File already in graph".
   - **Re-extract with new focus** → ask for new `extraction_focus`, then continue.
   - **Re-extract anyway** → continue as normal.
   Do NOT silently re-ingest — it wastes LLM tokens and creates duplicate Signals.
2. Read the file at `<file_path>`.
3. Load extraction context:
   - `ReadMcpResourceTool(server="mycelium", uri="mycelium://ontology")`
   - `ReadMcpResourceTool(server="mycelium", uri="mycelium://extraction-rules")`
4. `get_owner()` — if set, use owner name for first-person references.
   If not, use "я" with attributes: `{"is_owner": true}`.
5. **Graph context** — check what already exists to avoid duplicates:
   - `ReadMcpResourceTool(server="mycelium", uri="mycelium://context")`
   - Note existing neuron names and types. Use them for consistent naming.
   - If a neuron already exists — consolidate (add synapses to it), don't create a duplicate.

## Domain Detection

After preparation, check if a domain blueprint applies:

1. `ReadMcpResourceTool(server="mycelium", uri="mycelium://domains")` — list available domains.
2. If user specified `--domain <name>` in the argument (e.g., `/mycelium-ingest --domain blood_analysis file.pdf`):
   - `get_domain(name)` — load the full blueprint.
   - Apply directly without confirmation.
3. If no explicit domain — check if file content or filename matches any domain triggers.
   - If match found → ask user: "Похоже на домен '{name}'. Применить? [Y/n]"
   - If user confirms → apply the blueprint.
4. If no domain matches or user declines — proceed with standard extraction.

**Applying a blueprint:**
- Pass `category="{vault_prefix}"` to `vault_store` (step 1) to route the file.
  If vault_store was already called, note the prefix for the report.
- Prepend `extraction.focus` to your extraction instructions for each `ingest_direct` call.
- Use `extraction.neuron_types` to focus on domain-specific neuron types.
- After all extraction passes, link neurons to the anchor:
  - If `anchor_uuid` is set in blueprint → use it.
  - Otherwise → `add_neuron(name=anchor_neuron, neuron_type=anchor_type)`,
    then `update_domain(name, anchor_uuid="{uuid}")` to cache it.
  - For each extracted neuron: `add_synapse(source_uuid=neuron_uuid,
    target_uuid=anchor_uuid, relation="BELONGS_TO",
    fact="Part of {domain_name} domain")`.
- If `tracking.analysis` is set → after extraction, run the analysis prompt
  comparing new values with existing timeline data via `get_timeline`.

## Depth Strategy

**Before extraction (>4K tokens) — analyze first:**
Assess the document silently (for yourself, not output):
- Size estimate and language
- Structure: sections, chapters, timeline
- Domain: personal, technical, medical, creative, etc.
- Expected neuron density (a 20K doc typically yields 60–100 neurons)
- Plan: single-pass or multi-section? Where to split?

**Short text (<4K tokens):**
Read, understand, extract in one pass. One `ingest_direct` call.
Focus on completeness — every neuron, synapse, attribute, insight.

**Medium document (4–20K tokens):**
Identify logical sections (chapters, topics, time periods).
Extract per section — each section gets its own focused `ingest_direct` call.
Focused attention on a section yields richer results than processing
everything at once.

**Large document (>20K tokens):**
1. **Survey** — read the whole document. Understand structure, themes,
   key entities. Form a mental map (for yourself, not output).
2. **Section extraction** — extract per section, each as a separate
   `ingest_direct` call. The survey context informs your depth.
3. **Analytical synthesis** — after all sections are extracted, identify
   cross-section patterns, implicit connections, higher-order insights.
   Submit as a final `ingest_direct` call.

A 40K document contains 10–50× more knowledge than a 2K message.
Your extraction effort must scale accordingly.

## Quality Standard

**Neurons** — not just names, but knowledge anchors:
- `neuron_type` from ontology (pick the most specific type)
- `attributes` with concrete details (dates, numbers, parameters, metrics)
- `insights` — 1–3 analytical observations (meaning, implications, patterns)

**Synapses** — not "X is related to Y", but self-contained knowledge:
- 2–4 sentences that make sense without the source document
- All specifics preserved: dates, numbers, degrees, durations
- `relation` from ontology (not generic RELATES_TO)

**Self-check:** "If someone reads only this neuron/synapse, do they get it?"
If no — go deeper.

## Calling ingest_direct

For each extraction pass, call the `ingest_direct` MCP tool:
- `content`: the text of the section (or full document for short texts)
- `neurons`: JSON string — `[{"name", "neuron_type", "confidence", "attributes", "insights"}]`
- `synapses`: JSON string — `[{"source", "target", "relation", "fact", "confidence", "valid_at"}]`
- `name`: filename (or `"filename — section name"` for per-section calls)
- `source_type`: `"file"`
- `source_desc`: `"file:{relative_path}"` — use relative_path from vault_store response

## After Extraction — Cross-Batch Linking

**IMPORTANT:** Synapses whose source OR target neuron was created in a
DIFFERENT `ingest_direct` call are silently dropped. Only within-batch
connections survive.

After all `ingest_direct` calls, check: are there connections between
neurons from different sections? If yes, create them explicitly via
`add_synapse(source_uuid, target_uuid, relation, fact, confidence)`
using UUIDs from each `ingest_direct` response.

## After Extraction — Link Vault

After ALL `ingest_direct` calls (and cross-batch linking) are done,
link the signal UUID back to vault:

- Call `vault_link(relative_path="{relative_path}", signal_uuid="{signal_uuid}", original_ext="{original_ext}")`
  using `relative_path` and `original_ext` from step 1, and `signal_uuid` from the FIRST
  `ingest_direct` response.

This writes Obsidian frontmatter (if enabled) and connects the vault entry
to the knowledge graph.

## Error Handling

- File not found → tell user, check the path.
- Binary file (images, compiled code) → "This file type is not supported for text extraction."
- Empty file → "File is empty, nothing to extract."
- Extraction context unavailable (MCP resources fail) → proceed with default ontology, warn user.
- `ingest_direct` fails → report the error, do not retry silently.
- `vault_store` fails → report the error. Do NOT proceed without vault storage.

## Report

After all extractions complete, report:
- Vault path (relative_path)
- Signal UUIDs created
- Neurons: total created / merged with existing
- Synapses: total created
- Sections processed (if multi-pass)
