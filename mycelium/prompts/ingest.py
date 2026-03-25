"""Ingest prompt: context-aware extraction (P0.1 + R4.1)."""

from __future__ import annotations

from pathlib import Path
from typing  import Any

from pydantic import BaseModel, Field

# ── Knowledge loader (single source of truth) ─────────

_KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"


def _load_knowledge(name: str) -> str:
    return (_KNOWLEDGE_DIR / name).read_text()


# ── Response Models ──────────────────────────────────────


class ExtractedNeuron(BaseModel):
    name:        str
    neuron_type: str
    confidence:  float          = 1.0
    attributes:  dict[str, Any] = Field(default_factory=dict)
    insights:    list[str]      = Field(default_factory=list)
    expires_at:  str | None     = None   # ISO date — hard TTL for ephemeral facts


class ExtractedSynapse(BaseModel):
    source:     str                     # neuron name
    target:     str                     # neuron name
    relation:   str
    fact:       str                     # natural language
    confidence: float          = 1.0
    valid_at:   str | None     = None   # ISO date


class ExtractedQuestion(BaseModel):
    text:     str              # question for the user
    category: str              # conflict | incomplete | dedup | identity
    context:  str = ""         # related neuron/synapse name


class IngestResult(BaseModel):
    """Parsed LLM response."""
    neurons:       list[ExtractedNeuron]   = Field(default_factory=list)
    synapses:      list[ExtractedSynapse]  = Field(default_factory=list)
    questions:     list[ExtractedQuestion] = Field(default_factory=list)
    file_category: str                     = ""


# ── Context Model (used by dedup utils) ──────────────────


class NeuronContext(BaseModel):
    """Existing neuron context for dedup utilities."""
    uuid:        str
    name:        str
    neuron_type: str
    facts:       list[str] = Field(default_factory=list)


# ── Interaction Levels (S3) ─────────────────────────────────

_QUESTION_INSTRUCTIONS: dict[str, str] = {
    "silent": (
        "## Questions\n"
        "Do NOT generate questions. Resolve all ambiguity yourself.\n"
        "Conflicts: expire old synapse. Dedup: merge if high similarity.\n"
        "Missing data: skip, do not ask."
    ),
    "minimal": (
        "## Questions (optional)\n"
        "Generate questions ONLY for:\n"
        "  - conflict:  direct contradictions with existing synapses\n"
        "  - identity:  speaker identity when owner unknown\n"
        "Maximum 1 question. Include in \"questions\" array."
    ),
    "balanced": (
        "## Questions (optional)\n"
        "If you encounter ambiguity, generate clarifying questions (0-3):\n"
        "  - conflict:   existing synapse contradicts new information\n"
        "  - incomplete: important attribute missing (dosage, date, name)\n"
        "  - dedup:      unclear if two neurons are the same\n"
        "  - identity:   speaker identity unclear\n"
        "Include in \"questions\" array. Omit or leave empty if no ambiguity."
    ),
    "curious": (
        "## Questions & Observations\n"
        "Generate questions AND observations (0-5):\n"
        "  - conflict:    existing synapse contradicts new information\n"
        "  - incomplete:  important attribute missing (dosage, date, name)\n"
        "  - dedup:       unclear if two neurons are the same\n"
        "  - identity:    speaker identity unclear\n"
        "  - observation: patterns noticed, connections suggested, interesting findings\n"
        "Include in \"questions\" array."
    ),
}

_LEVEL_CATEGORIES: dict[str, set[str]] = {
    "silent":   set(),
    "minimal":  {"conflict", "identity"},
    "balanced": {"conflict", "incomplete", "dedup", "identity"},
    "curious":  {"conflict", "incomplete", "dedup", "identity", "observation"},
}


def filter_questions(
    questions: list[ExtractedQuestion],
    level:     str = "balanced",
) -> list[ExtractedQuestion]:
    """Double control: prompt instructs level, pipeline enforces it."""
    allowed = _LEVEL_CATEGORIES.get(level, _LEVEL_CATEGORIES["balanced"])
    return [q for q in questions if q.category in allowed]


# ── Prompt Template (P0.1: no existing context) ─────────

_SYSTEM = """\
You are MYCELIUM — a personal knowledge graph extraction engine.

<extraction_rules>
{extraction_rules}
</extraction_rules>

<ontology>
{ontology}
</ontology>

<output_format>
## Output Format
Respond with ONLY a valid JSON object. No markdown, no explanation.

{{
  "file_category": "one-word topic label (e.g. astrology, health, cooking, work, travel, finance)",
  "neurons": [
    {{"name": "...", "neuron_type": "...", "confidence": 0.0-1.0,
      "attributes": {{"key": "value"}},
      "insights": ["analytical observation (1-2 sentences each)"],
      "expires_at": "YYYY-MM-DD or null"}}
  ],
  "synapses": [
    {{"source": "neuron_name", "target": "neuron_name",
      "relation": "CAUSES", "fact": "rich detailed synapse (2-4 sentences)",
      "confidence": 0.0-1.0, "valid_at": "YYYY-MM-DD or null"}}
  ],
  "questions": [
    {{"text": "clarifying question", "category": "conflict|incomplete|dedup|identity",
      "context": "related neuron or synapse name"}}
  ]
}}

CRITICAL: "relation" MUST be a specific type from the ontology above
(HAS_TRAIT, INTERESTED_IN, CAUSES, MANIFESTS_AS, AMPLIFIES, PART_OF, etc.).
NEVER use RELATES_TO when a more specific type applies.
</output_format>

<example>
Input: "I've been doing Wim Hof breathing for 3 months, 3 rounds every morning. My cold tolerance improved."
Output:
{{
  "file_category": "health",
  "neurons": [
    {{"name": "Wim Hof breathing", "neuron_type": "practice", "confidence": 1.0,
      "attributes": {{"duration": "3 months", "rounds": 3, "time": "morning"}},
      "insights": ["Systematic breathwork suggesting interest in biohacking"]}},
    {{"name": "cold tolerance", "neuron_type": "trait", "confidence": 0.8,
      "attributes": {{"trend": "improved"}},
      "insights": ["Measurable physiological adaptation"]}}
  ],
  "synapses": [
    {{"source": "Wim Hof breathing", "target": "cold tolerance",
      "relation": "INFLUENCES", "fact": "Regular practice of Wim Hof breathing (3 rounds every morning for 3 months) has led to a noticeable improvement in cold tolerance.",
      "confidence": 0.8, "valid_at": null}}
  ],
  "questions": []
}}
</example>

{questions_instruction}
"""


_OWNER_KNOWN = """\

## Owner Context
Owner of this knowledge graph: {name}.
First-person pronouns (I, me, my, mine, я, мне, мой, меня) refer to person neuron '{name}'.
ALWAYS create the '{name}' person neuron and link extracted facts to it.
Even for short texts (1-2 sentences), extract at least one interest/practice/trait neuron
and connect it to '{name}' via a synapse.
After owner connections, build connections BETWEEN concepts: causal, categorical,
structural, and temporal links between non-owner neurons are equally important.
If text reveals alternative names/nicknames for any person, add them to attributes.aliases list.\
"""

_OWNER_UNKNOWN = """\

## Owner Context
Text is from the graph owner (first-person).
Create a person neuron for the speaker.
If speaker identifies themselves by name — use that name and set attributes.is_owner = true.
If no name given — use neuron name 'я' as temporary anchor with attributes.is_owner = true.
If text reveals alternative names/nicknames for any person, add them to attributes.aliases list.\
"""


def build_context_section(
    neuron_count:   int,
    synapse_count:  int,
    top_neurons:    list[dict[str, Any]],
    recent_neurons: list[dict[str, Any]],
) -> str:
    """R4.1: Build graph context section for extraction prompt."""
    if not neuron_count:
        return ""
    lines = [
        "\n## Graph Context (existing knowledge — avoid duplicates, use consistent naming)",
        f"Neurons: {neuron_count}, Active synapses: {synapse_count}",
    ]
    if top_neurons:
        top = ", ".join(f"{n['name']} ({n['type']})" for n in top_neurons[:10])
        lines.append(f"Top entities: {top}")
    if recent_neurons:
        rec = ", ".join(f"{n['name']} ({n['type']})" for n in recent_neurons[:10])
        lines.append(f"Recent: {rec}")
    return "\n".join(lines) + "\n"


def build_ingest_prompt(
    text:              str,
    survey:            str = "",
    owner_name:        str = "",
    interaction_level: str = "balanced",
    reference_time:    str = "",
    extraction_focus:  str = "",
    graph_context:     str = "",
) -> str:
    """Build extraction prompt. Optional survey context + owner identity."""
    q_inst = _QUESTION_INSTRUCTIONS.get(
        interaction_level, _QUESTION_INSTRUCTIONS["balanced"],
    )
    system = _SYSTEM.format(
        extraction_rules      = _load_knowledge("extraction.md"),
        ontology              = _load_knowledge("ontology.md"),
        questions_instruction = q_inst,
    )
    owner = _OWNER_KNOWN.format(name=owner_name) if owner_name else _OWNER_UNKNOWN
    ctx   = f"\n## Document Context\n{survey}\n" if survey else ""
    ref   = f"\n## Reference Time\nCurrent date: {reference_time}.\n" if reference_time else ""
    focus = (f"\n## Extraction Focus\n{extraction_focus}\n"
             "Extract ONLY information relevant to this focus. Ignore everything else.\n"
             ) if extraction_focus else ""
    return f"{system}{owner}{graph_context}{ctx}{ref}{focus}\n<signal>\n{text}\n</signal>"


# ── Two-Stage Extraction (BL-15) ────────────────────────

_ENTITY_ONLY = """\
You are MYCELIUM — a personal knowledge graph extraction engine.

<extraction_rules>
{extraction_rules}
</extraction_rules>

<ontology>
{ontology}
</ontology>

## Task
Extract ONLY NEURONS (entities) from the text below. Do NOT extract synapses yet.
List every person, concept, skill, interest, event, practice, trait, emotion, goal, etc.

## Output Format
Respond with ONLY a valid JSON object. No markdown, no explanation.

{{
  "file_category": "one-word topic label (e.g. astrology, health, cooking, work, travel, finance)",
  "neurons": [
    {{"name": "...", "neuron_type": "...", "confidence": 0.0-1.0,
      "attributes": {{"key": "value"}},
      "insights": ["analytical observation"],
      "expires_at": "YYYY-MM-DD or null"}}
  ]
}}

{questions_instruction}
"""

_RELATION_ONLY = """\
You are MYCELIUM — a personal knowledge graph extraction engine.

<extraction_rules>
{extraction_rules}
</extraction_rules>

<ontology>
{ontology}
</ontology>

## Task
Given the text and a COMPLETE list of extracted neurons, extract ALL SYNAPSES
(relationships) between them. You see every entity — now connect them.

## Extracted Neurons
{neuron_list}

## Output Format
Respond with ONLY a valid JSON object. No markdown, no explanation.

{{
  "synapses": [
    {{"source": "neuron_name", "target": "neuron_name",
      "relation": "CAUSES", "fact": "rich detailed synapse (2-4 sentences)",
      "confidence": 0.0-1.0, "valid_at": "YYYY-MM-DD or null"}}
  ]
}}

CRITICAL: "relation" MUST be a specific type from the ontology above
(HAS_TRAIT, INTERESTED_IN, CAUSES, MANIFESTS_AS, AMPLIFIES, PART_OF, etc.).
NEVER use RELATES_TO when a more specific type applies.

## Rules
1. source and target MUST be names from the neuron list above
2. Synapses MUST be RICH and DETAILED (2-4 sentences, self-contained)
3. Connect every neuron to every other neuron it causally, categorically,
   temporally, or structurally relates to — not just to the owner
4. Every neuron MUST appear in at least one synapse; non-owner neurons
   SHOULD appear in at least one synapse with another non-owner neuron
5. Target: 40%+ of synapses must be concept→concept (non-owner source AND target)
6. For each non-owner neuron ask: what causes it? what does it lead to?
   what is it part of? what does it manifest as? what does it contradict?
"""


def build_entity_prompt(
    text:              str,
    survey:            str = "",
    owner_name:        str = "",
    interaction_level: str = "balanced",
    reference_time:    str = "",
    extraction_focus:  str = "",
    graph_context:     str = "",
) -> str:
    """Build entity-only extraction prompt (two-stage Pass 1)."""
    q_inst = _QUESTION_INSTRUCTIONS.get(
        interaction_level, _QUESTION_INSTRUCTIONS["balanced"],
    )
    system = _ENTITY_ONLY.format(
        extraction_rules      = _load_knowledge("extraction.md"),
        ontology              = _load_knowledge("ontology.md"),
        questions_instruction = q_inst,
    )
    owner = _OWNER_KNOWN.format(name=owner_name) if owner_name else _OWNER_UNKNOWN
    ctx   = f"\n## Document Context\n{survey}\n" if survey else ""
    ref   = f"\n## Reference Time\nCurrent date: {reference_time}.\n" if reference_time else ""
    focus = (f"\n## Extraction Focus\n{extraction_focus}\n"
             "Extract ONLY information relevant to this focus. Ignore everything else.\n"
             ) if extraction_focus else ""
    return f"{system}{owner}{graph_context}{ctx}{ref}{focus}\n## Input Text\n{text}"


def build_relation_prompt(
    text:         str,
    neuron_descs: list[str],
    owner_name:   str = "",
) -> str:
    """Build relation-only extraction prompt (two-stage Pass 2)."""
    nrns = "\n".join(f"  - {n}" for n in neuron_descs)
    system = _RELATION_ONLY.format(
        extraction_rules = _load_knowledge("extraction.md"),
        ontology         = _load_knowledge("ontology.md"),
        neuron_list      = nrns,
    )
    owner = _OWNER_KNOWN.format(name=owner_name) if owner_name else _OWNER_UNKNOWN
    return f"{system}{owner}\n\n<signal>\n{text}\n</signal>"


# ── Gleaning Prompt (BL-16) ──────────────────────────────

_GLEANING = """\
You are MYCELIUM — a personal knowledge graph extraction engine.

<extraction_rules>
{extraction_rules}
</extraction_rules>

<ontology>
{ontology}
</ontology>

## Task
Review the original text and the ALREADY EXTRACTED neurons and synapses.
Find what was MISSED. Return ONLY NEW neurons and synapses not yet captured.

Look for:
- Entities mentioned but not extracted
- Relationships between existing neurons that were overlooked
- Implicit facts, dates, attributes that were skipped
- Secondary details (locations, durations, frequencies)

## Already Extracted Neurons
{neurons}

## Already Extracted Synapses
{synapses}

## Output Format
Respond with ONLY a valid JSON object. No markdown, no explanation.
Return ONLY items that are genuinely NEW — not already in the lists above.
If nothing was missed, return {{"neurons": [], "synapses": []}}.

{{
  "neurons": [
    {{"name": "...", "neuron_type": "...", "confidence": 0.0-1.0,
      "attributes": {{"key": "value"}},
      "insights": ["analytical observation"],
      "expires_at": "YYYY-MM-DD or null"}}
  ],
  "synapses": [
    {{"source": "neuron_name", "target": "neuron_name",
      "relation": "CAUSES", "fact": "rich detailed synapse (2-4 sentences)",
      "confidence": 0.0-1.0, "valid_at": "YYYY-MM-DD or null"}}
  ]
}}
"""


def build_gleaning_prompt(
    text:         str,
    neuron_descs: list[str],
    synapse_texts: list[str],
    owner_name:   str = "",
) -> str:
    """Build gleaning prompt: find what extraction missed."""
    nrns = "\n".join(f"  - {n}" for n in neuron_descs)
    syns = "\n".join(f"  - {s}" for s in synapse_texts[:100])
    system = _GLEANING.format(
        extraction_rules = _load_knowledge("extraction.md"),
        ontology         = _load_knowledge("ontology.md"),
        neurons          = nrns,
        synapses         = syns,
    )
    owner = (_OWNER_KNOWN.format(name=owner_name)
             if owner_name else _OWNER_UNKNOWN)
    return f"{system}{owner}\n\n<signal>\n{text}\n</signal>"


# ── Survey Prompt (L3: Pass 1) ──────────────────────────

_SURVEY = """\
Analyze this document and provide a concise structural overview:
1. Main sections and topics covered
2. Key neurons mentioned (people, concepts, practices)
3. Core themes and relationships between topics

200-500 words. Plain text, no JSON or markdown.\
"""


def build_survey_prompt(text: str) -> str:
    """Build survey prompt (Pass 1: document overview)."""
    return f"{_SURVEY}\n\n<document>\n{text}\n</document>"


# ── Analytical Prompt (L3: Pass 3) ──────────────────────

_ANALYTICAL = """\
You are MYCELIUM — a knowledge graph analysis engine.

## Task
Given extracted neurons and synapses, provide DEEPER ANALYSIS:
- Cross-neuron patterns and themes
- Implicit connections not directly stated
- Analytical conclusions and implications
Only produce NEW insights not already captured below.

{ontology}

## Extracted Neurons
{neurons}

## Extracted Synapses
{synapses}

## Output Format
Respond with ONLY a valid JSON object.
{{
  "neurons": [
    {{"name": "...", "neuron_type": "...", "confidence": 0.7,
      "attributes": {{}}, "insights": ["..."]}}
  ],
  "synapses": [
    {{"source": "neuron_name", "target": "neuron_name",
      "relation": "MANIFESTS_AS", "fact": "analytical conclusion (2-4 sentences)",
      "confidence": 0.7, "valid_at": null}}
  ]
}}

## Rules
1. Only NEW neurons/synapses — no repetitions
2. Use existing neuron names as source/target
3. confidence: 0.7 for inferred analysis
4. Focus on patterns, themes, cross-neuron connections
"""


def build_analytical_prompt(
    neuron_descs:  list[str],
    synapse_texts: list[str],
    owner_name:    str = "",
) -> str:
    """Build analytical prompt (Pass 3: cross-neuron analysis)."""
    nrns = "\n".join(f"  - {n}" for n in neuron_descs)
    syns = "\n".join(f"  - {s}" for s in synapse_texts[:100])
    owner = _OWNER_KNOWN.format(name=owner_name) if owner_name else _OWNER_UNKNOWN
    return _ANALYTICAL.format(
        ontology = _load_knowledge("ontology.md"),
        neurons  = nrns,
        synapses = syns,
    ) + owner


# ── Session-Aware Prompts (system once, user per-call) ───


_SESSION_SYSTEM = """\
You are MYCELIUM — a personal knowledge graph extraction engine.

<extraction_rules>
{extraction_rules}
</extraction_rules>

<ontology>
{ontology}
</ontology>

## Session Protocol
This is a multi-turn extraction session. Each message specifies a TASK MODE:
- EXTRACT: full extraction (neurons + synapses + questions)
- ENTITIES_ONLY: extract only neurons, no synapses
- RELATIONS_ONLY: extract synapses given a neuron list
- GLEANING: find missed neurons/synapses from a previous extraction

Always respond with ONLY a valid JSON object matching the requested schema.
No markdown, no explanation.

## Output Schemas

### EXTRACT / ENTITIES_ONLY schema:
{{
  "file_category": "one-word topic label",
  "neurons": [
    {{"name": "...", "neuron_type": "...", "confidence": 0.0-1.0,
      "attributes": {{"key": "value"}},
      "insights": ["analytical observation (1-2 sentences each)"],
      "expires_at": "YYYY-MM-DD or null"}}
  ],
  "synapses": [
    {{"source": "neuron_name", "target": "neuron_name",
      "relation": "CAUSES", "fact": "rich detailed synapse (2-4 sentences)",
      "confidence": 0.0-1.0, "valid_at": "YYYY-MM-DD or null"}}
  ],
  "questions": [
    {{"text": "clarifying question", "category": "conflict|incomplete|dedup|identity",
      "context": "related neuron or synapse name"}}
  ]
}}

### RELATIONS_ONLY schema:
{{
  "synapses": [
    {{"source": "neuron_name", "target": "neuron_name",
      "relation": "CAUSES", "fact": "rich detailed synapse (2-4 sentences)",
      "confidence": 0.0-1.0, "valid_at": "YYYY-MM-DD or null"}}
  ]
}}

### GLEANING schema:
{{
  "neurons": [...],
  "synapses": [...]
}}
(Return ONLY genuinely new items not in the provided lists.)

CRITICAL: "relation" MUST be a specific type from the ontology above
(HAS_TRAIT, INTERESTED_IN, CAUSES, MANIFESTS_AS, AMPLIFIES, PART_OF, etc.).
NEVER use RELATES_TO when a more specific type applies.

<example>
Input: "Я занимаюсь медитацией випассана 2 года, каждое утро по 30 мин. Помогает с тревожностью."
Output:
{{
  "file_category": "practice",
  "neurons": [
    {{"name": "vipassana meditation", "neuron_type": "practice", "confidence": 1.0,
      "attributes": {{"aliases": ["медитация випассана"], "tradition": "vipassana", "duration": "2 years", "frequency": "daily", "session": "30 min", "time": "morning"}},
      "insights": ["Sustained 2-year daily practice indicates deep commitment to self-awareness"]}},
    {{"name": "anxiety", "neuron_type": "emotion", "confidence": 0.8,
      "attributes": {{"aliases": ["тревожность"], "trend": "decreasing"}},
      "insights": ["Significant enough to motivate daily practice adoption"]}}
  ],
  "synapses": [
    {{"source": "vipassana meditation", "target": "anxiety",
      "relation": "INFLUENCES", "fact": "Двухлетняя ежедневная практика медитации випассана (30 мин утром) привела к снижению тревожности. Устойчивый терапевтический эффект указывает на осознанный выбор практики как инструмента управления эмоциональным состоянием.",
      "confidence": 0.8, "valid_at": null}}
  ],
  "questions": []
}}
</example>

{questions_instruction}
"""


def build_session_system_prompt(
    interaction_level: str = "balanced",
) -> str:
    """Unified system prompt for extraction session (~15KB).

    Sent once via --append-system-prompt on session creation.
    Covers all extraction task modes (extract, entity, relation, gleaning).
    """
    q_inst = _QUESTION_INSTRUCTIONS.get(
        interaction_level, _QUESTION_INSTRUCTIONS["balanced"],
    )
    return _SESSION_SYSTEM.format(
        extraction_rules      = _load_knowledge("extraction.md"),
        ontology              = _load_knowledge("ontology.md"),
        questions_instruction = q_inst,
    )


def build_session_extract_user(
    text:             str,
    survey:           str = "",
    owner_name:       str = "",
    reference_time:   str = "",
    extraction_focus: str = "",
    graph_context:    str = "",
) -> str:
    """User-turn for full extraction (neurons + synapses) in session mode."""
    owner = _OWNER_KNOWN.format(name=owner_name) if owner_name else _OWNER_UNKNOWN
    ctx   = f"\n## Document Context\n{survey}\n" if survey else ""
    ref   = f"\n## Reference Time\nCurrent date: {reference_time}.\n" if reference_time else ""
    focus = (f"\n## Extraction Focus\n{extraction_focus}\n"
             "Extract ONLY information relevant to this focus. Ignore everything else.\n"
             ) if extraction_focus else ""
    return (
        f"## Task Mode: EXTRACT\n"
        f"Extract ALL neurons AND synapses from the text below.\n"
        f"{owner}{graph_context}{ctx}{ref}{focus}\n<signal>\n{text}\n</signal>"
    )


def build_session_entity_user(
    text:             str,
    survey:           str = "",
    owner_name:       str = "",
    reference_time:   str = "",
    extraction_focus: str = "",
    graph_context:    str = "",
) -> str:
    """User-turn for entity-only extraction (two-stage Pass 1) in session mode."""
    owner = _OWNER_KNOWN.format(name=owner_name) if owner_name else _OWNER_UNKNOWN
    ctx   = f"\n## Document Context\n{survey}\n" if survey else ""
    ref   = f"\n## Reference Time\nCurrent date: {reference_time}.\n" if reference_time else ""
    focus = (f"\n## Extraction Focus\n{extraction_focus}\n"
             "Extract ONLY information relevant to this focus. Ignore everything else.\n"
             ) if extraction_focus else ""
    return (
        f"## Task Mode: ENTITIES_ONLY\n"
        f"Extract ONLY NEURONS (entities). Do NOT extract synapses yet.\n"
        f"List every person, concept, skill, interest, event, practice, "
        f"trait, emotion, goal, etc.\n"
        f"{owner}{graph_context}{ctx}{ref}{focus}\n<signal>\n{text}\n</signal>"
    )


def build_session_relation_user(
    text:         str,
    neuron_descs: list[str],
    owner_name:   str = "",
) -> str:
    """User-turn for relation extraction (two-stage Pass 2) in session mode."""
    nrns  = "\n".join(f"  - {n}" for n in neuron_descs)
    owner = _OWNER_KNOWN.format(name=owner_name) if owner_name else _OWNER_UNKNOWN
    return (
        f"## Task Mode: RELATIONS_ONLY\n"
        f"Given the text and extracted neurons, extract ALL SYNAPSES.\n\n"
        f"## Extracted Neurons\n{nrns}\n\n"
        f"## Rules\n"
        f"1. source and target MUST be names from the neuron list above\n"
        f"2. Synapses MUST be RICH and DETAILED (2-4 sentences)\n"
        f"3. Connect every neuron to related neurons — not just to the owner\n"
        f"4. Every neuron MUST appear in at least one synapse\n"
        f"5. Target: 40%+ synapses should be concept→concept (non-owner)\n"
        f"6. For each non-owner neuron ask: what causes it? what does it lead to?\n"
        f"   what is it part of? what does it manifest as? what does it contradict?\n"
        f"{owner}\n\n## Original Text\n{text}"
    )


def build_session_gleaning_user(
    text:          str,
    neuron_descs:  list[str],
    synapse_texts: list[str],
    owner_name:    str = "",
) -> str:
    """User-turn for gleaning (find missed facts) in session mode."""
    nrns = "\n".join(f"  - {n}" for n in neuron_descs)
    syns = "\n".join(f"  - {s}" for s in synapse_texts[:100])
    owner = (_OWNER_KNOWN.format(name=owner_name)
             if owner_name else _OWNER_UNKNOWN)
    return (
        f"## Task Mode: GLEANING\n"
        f"Review the text and find what was MISSED.\n"
        f"Return ONLY NEW neurons and synapses not yet captured.\n\n"
        f"## Already Extracted Neurons\n{nrns}\n\n"
        f"## Already Extracted Synapses\n{syns}\n"
        f"{owner}\n\n## Original Text\n{text}"
    )


# ── LLM Dedup Prompt (BL-17) ─────────────────────────────

class DedupPair(BaseModel):
    id:       int
    verdict:  str   # SAME | DIFFERENT


class DedupResult(BaseModel):
    pairs: list[DedupPair] = Field(default_factory=list)


_LLM_DEDUP = """\
You are a deduplication engine for a personal knowledge graph.

For each pair, decide: are these the SAME real-world entity?

Consider:
- Name similarity (abbreviations, synonyms, translations)
- Type compatibility (skill vs interest may still be same entity)
- Context from facts (if provided)

Respond with ONLY a valid JSON object:
{{"pairs": [{{"id": 1, "verdict": "SAME"}}, {{"id": 2, "verdict": "DIFFERENT"}}]}}

## Pairs
{pairs}
"""


def build_dedup_prompt(
    pairs: list[tuple[int, str, str, str, str, list[str], list[str]]],
) -> str:
    """Build LLM dedup prompt for grey-zone pairs.

    Each pair: (id, name_a, type_a, name_b, type_b, facts_a, facts_b)
    """
    lines: list[str] = []
    for pid, na, ta, nb, tb, fa, fb in pairs:
        entry = f"Pair {pid}: \"{na}\" ({ta}) vs \"{nb}\" ({tb})"
        if fa:
            entry += f"\n  Facts A: {'; '.join(f for f in fa[:3] if f)}"
        if fb:
            entry += f"\n  Facts B: {'; '.join(f for f in fb[:3] if f)}"
        lines.append(entry)
    return _LLM_DEDUP.format(pairs="\n".join(lines))


# ── Contradiction Classification (R1.1) ─────────────────


class ContradictionPair(BaseModel):
    id:         int
    verdict:    str    # CONFIRM | SUPERSEDE | CONTRADICT
    confidence: float


class ContradictionResult(BaseModel):
    pairs: list[ContradictionPair] = Field(default_factory=list)


_CONTRADICTION = """\
You are a fact consistency analyzer for a personal knowledge graph.

For each pair, classify how the NEW fact relates to the OLD fact:

- CONFIRM: new fact agrees with or reinforces old fact (same meaning)
- SUPERSEDE: new fact updates/replaces old fact (temporal change, correction, more specific)
- CONTRADICT: facts genuinely conflict (both may be valid perspectives)

Consider:
- Temporal context (dates, "now", "recently" — newer may supersede older)
- Specificity (more specific fact supersedes generic)
- Perspective (subjective opinions can coexist → CONTRADICT, not SUPERSEDE)

Respond with ONLY a valid JSON object:
{{"pairs": [{{"id": 1, "verdict": "CONFIRM", "confidence": 0.9}}]}}

## Pairs
{pairs}
"""


def build_contradiction_prompt(
    pairs: list[tuple[int, str, str]],
) -> str:
    """Build contradiction classification prompt.

    Each pair: (id, new_fact, old_fact)
    """
    lines: list[str] = []
    for pid, new_fact, old_fact in pairs:
        lines.append(
            f"Pair {pid}:\n"
            f"  OLD: {old_fact}\n"
            f"  NEW: {new_fact}"
        )
    return _CONTRADICTION.format(pairs="\n\n".join(lines))
