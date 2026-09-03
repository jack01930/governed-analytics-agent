"""Pinned prompt text for reproducible baseline measurements."""

from __future__ import annotations

from governed_analytics.models.protocols import SqlGenerationRequest

BASELINE_PROMPT_VERSION = "baseline-system-v2"
BASELINE_SYSTEM_PROMPT_V2 = (
    "You generate exactly one PostgreSQL read-only query for the supplied ecommerce question.\n"
    "Use only tables and columns in SCHEMA CONTEXT and metric rules in METRIC CONTEXT.\n"
    "Use half-open UTC time intervals. Do not invent columns or metrics.\n"
    "Return exactly one JSON object matching this example shape:\n"
    '{"sql":"select 1 as value","assumptions":["short business assumption"]}\n'
    'The "assumptions" value must be a JSON array of 0 to 8 strings; use [] when none.\n'
    "Do not provide chain-of-thought or hidden reasoning. "
    "Assumptions may contain only short business assumptions.\n"
    "The SQL must be one SELECT or WITH query. Do not include Markdown fences."
)


def build_baseline_user_prompt(request: SqlGenerationRequest) -> str:
    """Build the byte-stable user message for a single baseline request."""
    return (
        "QUESTION:\n"
        f"{request.question}\n\n"
        "SCHEMA CONTEXT:\n"
        f"{request.schema_context}\n\n"
        "METRIC CONTEXT:\n"
        f"{request.metric_context}\n\n"
        "OUTPUT FORMAT:\n"
        'Return only one JSON object with a "sql" string and an "assumptions" array of strings.'
    )
