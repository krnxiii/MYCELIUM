---
name: mycelium-domain
description: Create or manage knowledge domain blueprints. Use when user says "create domain", "new domain", "track blood tests", "set up a knowledge area", or wants to customize how a specific type of knowledge is processed. Interactive constructor for domain blueprints.
argument-hint: [domain_name]
---

Create or manage a domain blueprint for adaptive knowledge processing.

Arguments: [domain_name] (optional — if omitted, ask interactively)

## What is a Domain Blueprint?

A user-defined configuration that adapts ingestion, vault organization,
and graph structure for a specific knowledge area (e.g., blood tests,
finances, reading notes). Stored locally in `~/.mycelium/domains/`.

## Flow

### If managing existing domains

If user says "list domains", "show domains", "delete domain X":
- `list_domains()` — show all available
- `get_domain(name)` — show full config
- `delete_domain(name)` — remove config (anchor neuron stays in graph)
- `update_domain(name, ...)` — modify specific fields

### If creating a new domain

**Step 1: Understand the domain**

If domain name is provided as argument, use it. Otherwise ask:
"What knowledge area do you want to track?"

**Step 2: Ask clarifying questions (3-5 max)**

Based on the domain, ask the most relevant subset of:
1. "What format will the files be in?" (PDF, photos, text, mixed)
2. "What specific data points matter most?"
3. "Do you need to track numeric metrics over time?" (weight, reps, biomarkers)
4. "How often will you add new data?"
5. "What insights do you want to get over time?"
6. "Any specific keywords that identify this type of content?"

Adapt questions to the domain. Do NOT ask about tracking if the answer
is obvious from context ("track my workouts" → clearly tracking).
If user provides an example file (e.g., blood test PDF), parse it to
auto-extract field names, units, and reference ranges.

**Step 3: Generate blueprint**

From the answers, determine:
- `name` — concise domain name
- `description` — one-line purpose
- `vault_prefix` — logical directory path (e.g., "health/blood_tests/")
- `anchor_neuron` — hub neuron name (e.g., "Blood Analysis")
- `anchor_type` — neuron type for anchor (default: "domain")
- `triggers` — 3-7 keywords for auto-detection
- `extraction.focus` — domain-specific extraction instructions
- `extraction.neuron_types` — expected neuron types

**If tracking metrics (numeric data over time):**

Set `tracking.fields` as a structured dict. Each field has:
- `label` — human-readable name with units (e.g., "Bench press (kg)")
- `aliases` — words the parser matches in text, include Russian + English
  (e.g., ["жим", "bench", "жим лёжа"])
- `reference` — optional [min, max] normal range (e.g., [120, 160] for hemoglobin)

Use `tracking_fields_json` parameter (JSON string) in `create_domain`.

Example for fitness domain:
```json
{
  "bench_press": {"label": "Bench press (kg)", "aliases": ["жим", "bench", "жим лёжа"]},
  "squat": {"label": "Squat (kg)", "aliases": ["присед", "squat"]},
  "duration": {"label": "Duration (min)", "aliases": ["время", "минут", "мин"]}
}
```

Set `tracking.analysis` for trend instructions.
Set `tracking.dashboard` to true (default) to auto-generate dashboard.

For large field sets (e.g., blood tests with 100 params):
- Group by category, propose which to track
- Ask user to confirm or filter groups
- Include reference ranges from example data

**If NOT tracking (regular knowledge domain):**

Leave `tracking.fields` empty. The domain will use standard
extraction pipeline via `/mycelium-ingest`.

- `tracking.analysis` — analysis instruction for trends

**Step 4: Confirm with user**

Show the generated YAML and ask for confirmation.
If user wants changes — adjust and re-confirm.

**Step 5: Create**

1. `create_domain(name, ...)` — saves YAML to `~/.mycelium/domains/`
2. `add_neuron(name=anchor_neuron, neuron_type=anchor_type,
     attributes={"is_anchor": true, "domain": name})`
   — create the hub neuron in graph
3. `update_domain(name, anchor_uuid="{uuid}")` — cache UUID in blueprint

**Step 6: Report**

```
Domain "{name}" created:
  Blueprint: ~/.mycelium/domains/{slug}.yaml
  Vault:     {vault_prefix}
  Anchor:    {anchor_neuron} ({anchor_uuid})
  Triggers:  {triggers}
  Tracking:  {fields count} fields (if tracking domain)
```

If tracking domain: "Use /track to log metrics. Use get_metrics to see trends."
If knowledge domain: "Files matching triggers auto-detected during /mycelium-ingest."

## Extraction Skill

If the domain needs specialized extraction (e.g., tabular medical data),
also create an extraction skill:

1. `save_extraction_skill(name="{domain}_parser",
     content="...", keyword="{trigger}")`
2. Set `skill: "{domain}_parser"` in the blueprint via `update_domain`

## Error Handling

- Domain already exists → offer to update or show current config
- Graph unavailable → create blueprint anyway (anchor created on first use)
- User cancels → do nothing
