# MYCELIUM Ontology

## Neuron Types (pick the most specific)

### WHO I AM
| Type         | Description                                         |
|--------------|-----------------------------------------------------|
| person       | Named people ("Alice", "Dr. Smith")                 |
| relationship | Connection roles ("brother", "coach", "partner")    |
| trait         | Personality/characteristics ("introvert", "IQ 130") |
| body          | Body parts, health ("right knee", "eyesight")       |

### WHAT I DO
| Type     | Description                                            |
|----------|--------------------------------------------------------|
| skill    | Competencies ("Python", "public speaking")             |
| practice | Regular activities/rituals ("meditation", "journaling")|
| habit    | Habits good or bad ("coffee on empty stomach")         |
| project  | Projects ("MYCELIUM", "kitchen renovation")            |

### WHAT I THINK / FEEL
| Type     | Description                                            |
|----------|--------------------------------------------------------|
| belief   | Values, convictions ("open source > proprietary")      |
| emotion  | Emotional states ("anxiety before deadlines")          |
| interest | Interests, hobbies ("astrology", "neural networks")   |
| goal     | Goals, aspirations ("learn Go by June")                |

### WHAT'S AROUND ME
| Type   | Description                                              |
|--------|----------------------------------------------------------|
| place  | Locations ("Moscow", "corner cafe")                      |
| event  | Events with time ("interview March 15")                  |
| period | Time spans ("2015-2020", "childhood", "Q1 2026")        |

### WHAT I USE
| Type           | Description                                         |
|----------------|-----------------------------------------------------|
| resource       | Books, tools, meds ("Atomic Habits", "magnesium B6")|
| recommendation | Advice, prescriptions ("drink 2L water")            |

### ABSTRACT
| Type    | Description                                            |
|---------|--------------------------------------------------------|
| concept | Abstract fallback (use only if nothing else fits)      |

**Note:** This ontology is not exhaustive. If no type fits well, use `concept` and add `attributes.suggested_type` with your proposed type name (e.g. `{"suggested_type": "organization"}`).

## Relation Types (prefer specific over generic RELATES_TO)

| Relation      | Pattern                  | Example                               |
|---------------|--------------------------|---------------------------------------|
| INTERESTED_IN | person -> interest/topic | "Alice" -> "astrology"                |
| PURSUES       | person -> goal           | "Alice" -> "learn Go by June"         |
| HAS_SKILL     | person -> skill          | "Alice" -> "Python"                   |
| BELIEVES      | person -> belief         | "Alice" -> "open source > proprietary"|
| PRACTICES     | person -> practice/habit | "Alice" -> "meditation"               |
| FEELS         | person -> emotion        | "Alice" -> "anxiety before deadlines" |
| KNOWS         | person -> person/concept | "Alice" -> "Bob"                      |
| WORKS_ON      | person -> project        | "Alice" -> "MYCELIUM"                 |
| LOCATED_AT    | neuron -> place          | "event" -> "Moscow"                   |
| HAS_TRAIT     | person -> trait/body     | "Alice" -> "introvert"                |
| HAS_PLACEMENT | person -> config/position| "Alice" -> "Sun in Sagittarius 5th"   |
| USES          | neuron -> resource       | "Alice" -> "magnesium B6"             |
| EXPERIENCED   | person -> event          | "Alice" -> "interview March 15"       |
| DURING        | neuron -> period         | "job at Google" -> "2015-2020"        |
| RECOMMENDS    | neuron -> recommendation | "Dr. Smith" -> "sleep by 23:00"       |
| INFLUENCES    | neuron -> neuron (cause) | "meditation" -> "focus"               |
| DEPENDS_ON    | neuron -> neuron (prereq)| "project" -> "funding"                |
| IS_A          | neuron -> concept (taxonomy)| "skateboarding" -> "extreme sports"|
| RELATES_TO    | generic fallback         | use only if nothing above fits        |

### Cross-Concept Relations (no person required)

Use these when connecting non-owner neurons to each other.
These are the relations that give the graph emergent intelligence.

| Relation      | Pattern                      | Example                                        |
|---------------|------------------------------|------------------------------------------------|
| CAUSES        | neuron → neuron (explicit causal mechanism) | "fear of abandonment" → "trust issues" |
| LEADS_TO      | neuron → neuron (sequence)   | "burnout" → "spiritual seeking"                |
| TRIGGERED_BY  | effect → trigger             | "existential crisis" → "Saturn return"         |
| MANIFESTS_AS  | archetype/trait → behavior   | "wounded healer archetype" → "counselling"     |
| AMPLIFIES     | neuron → neuron (intensity)  | "Scorpio moon" → "emotional intensity"         |
| COMPENSATES   | defense mechanism → wound    | "overachievement" → "fear of inadequacy"       |
| CONTRADICTS   | neuron → neuron (tension)    | "fear of betrayal" → "deep loyalty"            |
| PART_OF       | component → whole            | "Mars placement" → "natal chart"               |

### CAUSES Disambiguation
CAUSES requires an **explicit causal mechanism** stated in the text ("X produces Y because…").
- "Person has/displays trait X" → **HAS_TRAIT**, not CAUSES
- "Abstract trait appears as concrete behavior" → **MANIFESTS_AS**, not CAUSES
- "X strengthens/intensifies Y" → **AMPLIFIES**, not CAUSES
- "X happened, then Y followed" → **LEADS_TO**, not CAUSES
- When unsure between CAUSES and another relation, prefer the more specific one.

**Note:** This relation ontology is not exhaustive. If no type fits well, use `RELATES_TO` and add a comment in the fact text suggesting a better type (e.g. "suggested_relation: BORN_IN"). When a suggested relation accumulates across multiple synapses, promote it to the ontology.
