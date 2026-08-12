"""Tool abstraction - a capability unit an Agent can mount.

The design mirrors `connectors/`: a "tool" describes itself (name + JSON-Schema parameters) and
registers with the registry; the Agent loop hands the tool schemas to the LLM (native function-calling),
then dispatches the LLM's tool_call to the matching tool. Retrieval is just one of the built-in tools,
and an external MCP tool adapted to the same interface is indistinguishable from a built-in one to the Agent.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sag_api.db.models import Agent, Source
    from sag_api.sag import EngineManager


@dataclass
class ToolMeta:
    """Tool self-description. `parameters` is a JSON-Schema (object) for the model's function-calling."""

    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolContext:
    """Runtime context a tool needs (injected by the Agent loop, mirroring how a job handler receives singletons)."""

    engine_manager: EngineManager
    sources: list[Source] = field(default_factory=list)
    persona: dict[str, Any] = field(default_factory=dict)
    agent: Agent | None = None
    language: str = "en"
    # Global evidence numbering offset: the loop sets it before each dispatch so [n] keeps increasing across rounds without reuse
    citation_offset: int = 0


@dataclass
class ToolResult:
    """Tool execution result. `content` goes back to the model; `citations` give the UI provenance; `data` carries structured extras."""

    content: str
    citations: list[dict] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """Base class of every tool. Adding a tool = subclass + implement invoke + register it in the registry."""

    meta: ToolMeta

    @abstractmethod
    async def invoke(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute the tool. Arguments come from the model (or are auto-seeded); returns a result to feed back."""
        raise NotImplementedError
