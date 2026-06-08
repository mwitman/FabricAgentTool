from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

DeploymentMode = Literal["standalone", "orchestrator", "orchestrator_only"]
DataSourceType = Literal["fabric_mcp", "semantic_model", "graphql", "sql_endpoint", "data_agent"]


class DataSourceRef(BaseModel):
    """Reference to a Fabric item used as the data source for an agent."""
    source_type: DataSourceType = "semantic_model"
    workspace_id: str = ""
    workspace_name: str = ""
    item_id: str = ""
    item_name: str = ""

    @field_validator("source_type", mode="before")
    @classmethod
    def _coerce_empty_source_type(cls, v: Any) -> str:
        if not v:
            return "semantic_model"
        return v


# Keep SemanticModelRef as an alias for backward compatibility with existing Cosmos docs
SemanticModelRef = DataSourceRef


class ModelConfig(BaseModel):
    """Foundry model deployment selection for an agent."""
    model_config = ConfigDict(extra="allow")

    deployment_name: str = ""
    model_display_name: str = ""
    model_name: str = ""
    provider: str = ""
    publisher: str = ""
    model_format: str = ""
    capabilities: dict[str, Any] = Field(default_factory=dict)


class SubagentConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str = ""
    data_source: DataSourceRef = Field(default_factory=DataSourceRef, alias="semantic_model")
    agent_model: ModelConfig = Field(default_factory=ModelConfig, alias="model_config")
    prompt: str = ""
    guardrails: list[str] = Field(default_factory=list)


class StandaloneAgentConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    name: str = "Standalone Fabric Agent"
    description: str = ""
    data_source: DataSourceRef = Field(default_factory=DataSourceRef, alias="semantic_model")
    agent_model: ModelConfig = Field(default_factory=ModelConfig, alias="model_config")
    prompt: str = ""


class ExternalAgentRef(BaseModel):
    """Reference to an existing deployed hosted agent to use as a subagent."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    agent_name: str = ""
    display_name: str = ""
    project_id: str = ""
    description: str = ""


class OrchestratorOnlyConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    name: str = "Orchestrator Only"
    description: str = ""
    prompt: str = ""
    agent_model: ModelConfig = Field(default_factory=ModelConfig, alias="model_config")
    external_agents: list[ExternalAgentRef] = Field(default_factory=list)


class OrchestratorConfig(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)

    name: str = "Fabric Orchestrator"
    description: str = ""
    prompt: str = ""
    agent_model: ModelConfig = Field(default_factory=ModelConfig, alias="model_config")
    subagents: list[SubagentConfig] = Field(default_factory=list)


class AgentProject(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = "agent_project"
    name: str
    description: str = ""
    deployment_mode: DeploymentMode = "orchestrator"
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    orchestrator_only: OrchestratorOnlyConfig = Field(default_factory=OrchestratorOnlyConfig)
    standalone_agent: StandaloneAgentConfig = Field(default_factory=StandaloneAgentConfig)
    deployment: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class PromptGenerationRequest(BaseModel):
    project: AgentProject
    target: Literal["orchestrator", "subagent", "standalone", "orchestrator_only"]
    subagent_id: str | None = None
    instructions: str = ""
    semantic_metadata: dict[str, Any] | None = None


class DevChatRequest(BaseModel):
    project: AgentProject
    message: str
    conversation_id: str
    fabric_token: str | None = None
    powerbi_token: str | None = None


class DeploymentRequest(BaseModel):
    project: AgentProject
    image_tag: str | None = None
    agent_name: str | None = None
    build_and_push: bool = False
    submit_to_foundry: bool = False


# ---------------------------------------------------------------------------
# Permissions / Roles
# ---------------------------------------------------------------------------


class RoleMember(BaseModel):
    """An Entra user or security group assigned to a role."""
    object_id: str
    display_name: str = ""
    member_type: Literal["user", "group"] = "user"


class Role(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = "role"
    name: str
    description: str = ""
    members: list[RoleMember] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()


class AgentRoleBinding(BaseModel):
    """Links a deployed agent to one or more roles. Stored in the Permissions container."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str = "agent_role_binding"
    project_id: str
    agent_name: str
    project_display_name: str = ""
    role_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat()
