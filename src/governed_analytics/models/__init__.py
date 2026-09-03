"""Model-adapter boundaries for the direct Text-to-SQL baseline."""

from governed_analytics.models.protocols import SqlGenerationRequest, SqlGenerator

__all__ = ["SqlGenerationRequest", "SqlGenerator"]
