# MYCELIUM Extraction Rules

## Task
Extract DENSELY — every neuron, synapse, date, number, practice, detail.
Personal facts > general knowledge. Miss nothing.

## Confidence Levels
- **1.0** — explicitly stated
- **0.5** — inferred from context
- **0.3** — weak hint / speculation

## Synapse Quality
Synapses MUST be RICH and DETAILED (2-4 sentences, self-contained).
Include all specifics: dates, numbers, degrees, durations.

## Attributes
Use attributes for structured details that don't fit in the name:
- `{"degree": "26°04'", "house": 5, "element": "fire", "status": "direct"}`
- `{"start": "2015-01", "end": "2020-06"}`
- `{"dosage": "400mg", "frequency": "daily"}`

When the input contains **tables, lists, or structured data** (columns, rows, key-value pairs),
preserve ALL columns as attributes on the corresponding neuron.
Do NOT flatten tables into narrative fact text — the structure IS the information.
Example: a table row `| Sun | Sagittarius | 26°04' | 5th | Direct |` →
  neuron "Sun in Sagittarius 5th house" with attributes:
  `{"sign": "Sagittarius", "degree": "26°04'", "house": 5, "status": "direct"}`

## Neuron Naming — Canonical English
Neuron names MUST be in **English** regardless of input language.
This ensures cross-language deduplication (e.g. Russian + English inputs merge correctly).

**Shortest unambiguous name.** Specifics go into attributes, not the name.
- "journaling" not "morning pages journaling ritual" (tradition → attribute)
- "freedom need" not "freedom seeking behavior"
- "partial fire grand trine" not "partial grand trine in fire signs"
- If graph context provides existing neuron names, match them exactly.

- Use the standard English term: "snowboarding", "meditation", "anxiety"
- Proper nouns: keep original form ("Москва" → "Moscow", "Wim Hof" stays "Wim Hof")
- Culture-specific concepts with no English equivalent: transliterate ("борщ" → "borscht", "дача" → "dacha")
- If the input is non-English, add original-language name to `attributes.aliases`: `{"aliases": ["сноуборд"]}`
- Synapse `fact` text stays in the **original language** of the input (preserves nuance)

## Coreference Resolution
Before creating neurons, resolve ALL pronouns and references to concrete names:
- "he", "she", "they" → the person's actual name
- "my friend", "the doctor", "his coach" → named neuron (use the name if known, otherwise descriptive: "unnamed photographer", "Alice's coach")
- "it", "this method", "the app" → the specific entity being referenced

NEVER create neurons named "he", "she", "it", "this", "my friend".
If the referent's name is unknown, use the best available description as the neuron name.

## Temporal Resolution
Resolve ALL relative dates to absolute ISO format using the provided reference time:
- "yesterday" → concrete date
- "last week" → approximate date (start of that week)
- "3 months ago" → approximate date
- "in 2 years" → projected date

Use resolved dates in `valid_at` and `attributes` fields.
If no reference time is provided, leave relative dates as-is in attributes.

## Memory TTL (Time-To-Live)
For neurons tied to a specific date that become useless after it passes,
set `expires_at` to an ISO date (e.g. "2026-03-10"):
- "meeting tomorrow at 10" → expires_at = day after the meeting
- "deadline next Friday" → expires_at = the Saturday after
- "sale ends March 15" → expires_at = "2026-03-16"

Do NOT set expires_at for durable knowledge (interests, skills, relationships,
beliefs, ongoing goals). When in doubt, omit it (null = no expiry, soft decay only).

## Rules
1. Extract ALL neurons and synapses — be thorough and dense
2. neuron_type MUST be from the ontology (pick most specific).
   Before assigning `concept`, verify the neuron doesn't fit a specific type:
   behavioral pattern → **trait**, career domain/hobby → **interest**,
   health zone by body part → **body**, regular activity → **practice**,
   dated occurrence → **event**. `concept` = last resort for truly abstract ideas.
3. Synapses MUST be RICH and DETAILED (2-4 sentences, self-contained)
4. Include all specifics: dates, numbers, degrees, durations
5. Use attributes for structured data (dates, metrics, parameters)
6. Create synapses between ALL meaningfully connected neurons — including concept→concept
   connections that reveal causality, hierarchy, temporal flow, or structural belonging.
   Owner→concept connections are necessary but not sufficient.
7. Add insights: 1-3 analytical observations per neuron (meaning, implications, patterns)
8. **No orphan neurons.** Every neuron MUST appear in at least one synapse.
   Non-owner neurons SHOULD have at least one connection to another non-owner neuron.
   A neuron connected only to the owner carries minimal emergent value.
   Owner MUST be directly connected to every trait, placement, interest, emotion,
   skill, and practice that belongs to them. The owner is the hub — if a fact
   describes the owner, there MUST be a synapse owner→neuron (HAS_TRAIT, HAS_PLACEMENT,
   INTERESTED_IN, FEELS, PRACTICES, etc.).
   **Validation:** Before outputting JSON, scan every neuron in your list.
   If a neuron has zero synapses — either create a meaningful synapse for it
   or remove it entirely. A neuron with no connections is noise, not knowledge.
9. **Umbrella neurons.** When 2+ neurons share a common category, create an umbrella neuron
   and connect them via IS_A or PART_OF synapses.
   - Same-domain group: skateboarding, snowboarding, FMX → umbrella "extreme sports" (interest)
   - If the entire document covers a single domain (astrology, medicine, cooking, programming…),
     create a domain umbrella neuron (usually **interest** or **concept**) and connect the owner
     to it (INTERESTED_IN). Sub-neurons connect to it via IS_A/PART_OF — this gives the graph
     a navigable cluster instead of a flat list of unrelated nodes.
   - **Umbrella wiring checklist:** after creating an umbrella, scan ALL neurons in your output.
     Every neuron that belongs to that domain MUST have an IS_A or PART_OF synapse to the umbrella.
     Placements, transits, aspects, techniques, sub-concepts — all connect to the umbrella.
     An umbrella with <5 connections is a red flag — you likely forgot to wire sub-neurons.
10. Neuron names in English (see "Neuron Naming" above). Aliases in attributes for non-English inputs
11. Resolve all coreferences before creating neurons (see "Coreference Resolution" above)
12. Resolve relative dates to absolute ISO format (see "Temporal Resolution" above)
13. Network density: aim for synapses ≥ neurons × 1.5. If most synapses point to owner,
    you missed concept→concept connections — go back and find them.
14. **Consolidation over proliferation.** Prefer fewer attribute-rich neurons over many thin variants.
    - Astrological aspects (conjunctions, squares, trines) → attributes on the placement neuron,
      NOT separate concept neurons. Exception: aspects with explicit behavioral interpretation.
    - Career domains ("psychology and counseling") → **interest** type, not "career in X" concept.
    - Health zones → **body** type by body part ("hips and liver"), not by zodiac sign.
    - If two candidate neurons differ only in phrasing, create one and add the variant as alias.
    - Guideline: a 20K-token document typically yields 60–100 neurons. 150+ signals over-extraction.

## Graph Topology — Network, Not Star

A star graph (every node → owner) is the lowest-quality extraction.
It answers "what does the owner have?" but not "why?", "how?", or "what clusters with what?"

**The target: a network where concepts explain each other.**

After connecting neurons to the owner, ask for each non-owner neuron:
1. **Causes/triggers:** what produces this? what does it produce? (CAUSES, TRIGGERED_BY, LEADS_TO)
2. **Structure:** is this part of a larger concept? does it contain sub-concepts? (PART_OF, IS_A)
3. **Manifestation:** how does this abstract trait appear in concrete behavior? (MANIFESTS_AS)
4. **Tension:** what does this contradict or compensate for? (CONTRADICTS, COMPENSATES)
5. **Time:** what period or event surrounds this? (DURING, TRIGGERED_BY)

**Self-check before finishing:** mentally remove the owner node.
Do the remaining nodes form clusters with internal connections?
If most become isolated — the graph is still a star. Add inter-concept synapses.

## Depth Strategy

**Short text (<4K tokens):** single-pass extraction. Focus on completeness —
every neuron, synapse, attribute, insight. One extraction covers everything.

**Medium document (4–20K tokens):** identify logical sections (chapters, topics,
time periods). Extract per section — focused attention on each part yields
richer results than trying to digest everything at once.

**Large document (>20K tokens):**
1. **Survey** — understand structure, themes, key entities (mental map, not output)
2. **Section extraction** — per section, with survey context informing depth
3. **Analytical synthesis** — cross-section patterns, implicit connections,
   higher-order insights that only emerge from seeing the whole

**Principle:** a 40K document contains 10–50× more knowledge than a 2K message.
Extraction effort must scale accordingly. One pass over a large document
produces the same shallow result as one pass over a short one.

## Depth Contrast

### SHALLOW (bad)

Input: "Я занимаюсь медитацией випассана уже 2 года, каждое утро
по 30 минут. Это помогло мне справиться с тревожностью."

Neuron: "meditation" (interest) — no attributes, no aliases, no insights
Synapse: "Practices meditation" — one sentence, no details, lost original language

**Why bad:** lost tradition (випассана), duration (2 года), frequency (ежедневно),
session length (30 мин), time (утро), therapeutic effect, emotional context.
The graph "knows" almost nothing.

### DEEP (good)

Neuron: **vipassana meditation** (practice, confidence: 1.0)
  attributes: {aliases: ["медитация випассана"], tradition: "vipassana",
               duration: "2 years", frequency: "daily", session: "30 min",
               time: "morning"}
  insights: ["Sustained practice (2 years daily) indicates disciplined
             approach and deep interest in self-awareness"]

Neuron: **anxiety** (emotion, confidence: 0.8)
  attributes: {aliases: ["тревожность"], trend: "decreasing"}
  insights: ["Was significant enough to become the motivation for
             daily practice"]

Synapse: vipassana meditation → anxiety (INFLUENCES, 0.8)
  "Двухлетняя ежедневная практика медитации випассана (30 мин утром)
  привела к ощутимому снижению тревожности. Это указывает на
  устойчивый терапевтический эффект и осознанный выбор практики
  как инструмента управления эмоциональным состоянием."

**Why good:** English neuron names (cross-language dedup), aliases preserve
original language, synapse fact stays in Russian (preserves nuance),
structured attributes, analytical insights. The graph *understands*.

### STAR (bad topology)

Input: natal chart for Nikita — Sun Sagittarius, Moon Scorpio, wounded healer archetype.

Extracted (10 neurons, 10 synapses — all from nikita):
  nikita → Moon in Scorpio (HAS_TRAIT)
  nikita → strong intuition (HAS_TRAIT)
  nikita → fear of betrayal (FEELS)
  nikita → wounded healer archetype (HAS_TRAIT)
  nikita → counselling (INTERESTED_IN)
  nikita → travel (INTERESTED_IN)
  nikita → Sagittarius stellium (HAS_TRAIT)
  ... (all roads lead to nikita)

**Why bad:** remove nikita and every node becomes isolated. The graph can answer
"what does Nikita have?" but cannot answer "why does fear of betrayal coexist
with deep loyalty?", "how does wounded healer manifest in career?",
"what in the chart explains the pull toward counselling?" Zero emergent value.

### NETWORK (good topology)

Same input, 10 neurons, 18 synapses — 8 concept→concept added:
  nikita → Moon in Scorpio (HAS_TRAIT)           ← owner connection
  nikita → wounded healer archetype (HAS_TRAIT)  ← owner connection
  Moon in Scorpio → strong intuition (CAUSES)
  Moon in Scorpio → fear of betrayal (CAUSES)
  fear of betrayal → deep loyalty (COMPENSATES)  ← psychological mechanism
  wounded healer archetype → counselling (MANIFESTS_AS)
  Sagittarius stellium → travel (AMPLIFIES)
  Sagittarius stellium → counselling (LEADS_TO)  ← Sag = teaching/guiding
  Saturn return → spiritual seeking (TRIGGERED_BY)
  spiritual seeking → counselling (LEADS_TO)     ← causal chain

**Why good:** remove nikita — still a connected network. The graph explains
*why* Nikita is drawn to counselling (3 independent causal paths),
*how* fear and loyalty coexist (compensation), *what* drives spiritual development.
This is emergent intelligence.

## Example

Input: "I've been practicing Wim Hof breathing for 3 months, doing 3 rounds
every morning. My cold tolerance improved noticeably."

Neurons:
- **Wim Hof breathing** (practice, confidence: 1.0)
  attributes: {duration: "3 months", rounds: 3, time: "morning"}
  insight: "Systematic breathwork suggesting interest in biohacking"
- **cold tolerance** (trait, confidence: 0.8)
  attributes: {trend: "improved"}
  insight: "Measurable physiological adaptation"

Synapses:
- Wim Hof breathing -> cold tolerance (INFLUENCES, confidence: 0.8)
  "Regular practice of Wim Hof breathing (3 rounds every morning for 3 months)
  has led to a noticeable improvement in cold tolerance."
