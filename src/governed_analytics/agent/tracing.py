"""Safe in-memory trace side channel for one agent run."""

from __future__ import annotations

from governed_analytics.agent.contracts import (
    ModelCallTrace,
    NodeTrace,
    SafeTrace,
    ToolCallTrace,
)


class InMemoryTraceRecorder:
    """Append-only recorder that never accepts State, prompts, payloads, or rows."""

    def __init__(self) -> None:
        self._nodes: list[NodeTrace] = []
        self._model_calls: list[ModelCallTrace] = []
        self._tool_calls: list[ToolCallTrace] = []

    def append_node(self, trace: NodeTrace) -> None:
        self._nodes.append(trace)

    def append_model(self, traces: tuple[ModelCallTrace, ...]) -> None:
        self._model_calls.extend(traces)

    def append_tool(self, trace: ToolCallTrace) -> None:
        self._tool_calls.append(trace)

    def snapshot(self) -> SafeTrace:
        return SafeTrace(
            nodes=tuple(self._nodes),
            model_calls=tuple(self._model_calls),
            tool_calls=tuple(self._tool_calls),
        )


__all__ = ["InMemoryTraceRecorder"]
