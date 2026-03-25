"""Neuron summary generation prompt."""

from __future__ import annotations


def build_summary_prompt(
    name:        str,
    neuron_type: str,
    facts:       list[str],
) -> str:
    """Build prompt for generating neuron summary from surrounding synapses."""
    facts_text = "\n".join(f"  - {f}" for f in facts if f)
    return (
        "Generate a concise summary (1-3 sentences) for this knowledge graph neuron.\n"
        "\n"
        f"Neuron: {name}\n"
        f"Type: {neuron_type}\n"
        f"Known facts:\n{facts_text}\n"
        "\n"
        "Respond with ONLY the summary text. No JSON, no markdown, no explanation.\n"
        "Capture the essence of what is known about this neuron."
    )
