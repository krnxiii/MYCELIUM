---
source: file
format: medical
keyword: анализ
---

Extract the following from medical reports:
- All numeric values with units and normal ranges
- Date of the test/procedure
- Doctor or clinic name if mentioned
- Any out-of-range flags (HIGH/LOW/CRITICAL)
- Recommended follow-up actions

Use neuron types: metric, condition, doctor, clinic, recommendation
For synapses use: HAS_RESULT, ORDERED_BY, PERFORMED_AT, INDICATES, RECOMMENDS