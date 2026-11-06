"""Configuration options for the Google Workspace MCP Server."""

from __future__ import annotations

import os
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from workspace.config import WorkspaceConfig, load_workspace_config


class MCPConfig(BaseModel):
    """Configuration for Workspace MCP server and runtime security policies."""

    model_config = ConfigDict(extra="ignore")

    enable_write_tools: bool = Field(
        default=True,
        description="Whether write tools (send_email, create_calendar_event, delete_calendar_event) are active",
    )
    require_biometric: bool = Field(
        default=True,
        description="Whether hardware biometric verification (Touch ID / Windows Hello) is strictly required for writes",
    )
    biometric_timeout_seconds: int = Field(
        default=60,
        description="Maximum seconds to wait for user biometric confirmation before timing out",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level for the MCP server process",
    )
    config_path: Optional[str] = Field(
        default=None,
        description="Optional custom path to workspace config.json",
    )
    keyring_service: Optional[str] = Field(
        default=None,
        description="Optional custom keyring service name",
    )

    @classmethod
    def from_env(cls) -> MCPConfig:
        """Create MCPConfig instance populated from environment variables."""
        return cls(
            enable_write_tools=os.environ.get("WORKSPACE_MCP_ENABLE_WRITE_TOOLS", "true").lower() in ("true", "1", "yes"),
            require_biometric=os.environ.get("WORKSPACE_MCP_REQUIRE_BIOMETRIC", "true").lower() in ("true", "1", "yes"),
            biometric_timeout_seconds=int(os.environ.get("WORKSPACE_MCP_BIOMETRIC_TIMEOUT", "60")),
            log_level=os.environ.get("WORKSPACE_MCP_LOG_LEVEL", "INFO").upper(),
            config_path=os.environ.get("WORKSPACE_CONFIG_PATH"),
            keyring_service=os.environ.get("WORKSPACE_KEYRING_SERVICE"),
        )

    def load_sdk_config(self) -> WorkspaceConfig:
        """Load and resolve the underlying Workspace SDK credentials."""
        return load_workspace_config(
            config_path=self.config_path,
            keyring_service=self.keyring_service,
        )
