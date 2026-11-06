"""Base classes and registry for Workspace MCP tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict, List
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from workspace.calendar import CalendarClient
    from workspace.gmail import GmailClient


class ToolSchema(BaseModel):
    """Base schema for tool input arguments."""

    model_config = ConfigDict(extra="forbid")


class BaseTool(ABC):
    """Abstract base class for all Google Workspace MCP tools."""

    name: str
    description: str
    input_schema: type[ToolSchema] = ToolSchema

    @abstractmethod
    async def execute(
        self,
        gmail_client: GmailClient,
        calendar_client: CalendarClient,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute the tool business logic with the provided arguments."""

    def get_schema(self) -> Dict[str, Any]:
        """Return the JSON schema definition for this tool's parameters."""
        schema = self.input_schema.model_json_schema()
        schema.pop("title", None)
        return schema


class ToolRegistry:
    """In-memory registry of MCP tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool instance."""
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """Retrieve a tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[BaseTool]:
        """List all registered tools."""
        return list(self._tools.values())
