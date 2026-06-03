from __future__ import annotations

import os
import re
from typing import Any

from .models import AgentProject


def _requires_fabric_item(source_type: str) -> bool:
    return source_type != "fabric_mcp"


def validate_project(project: AgentProject) -> dict[str, Any]:
    errors: list[str] = []
    if project.deployment_mode == "standalone":
        agent = project.standalone_agent
        if not agent.name.strip():
            errors.append("Standalone agent name is required.")
        if _requires_fabric_item(agent.data_source.source_type) and not agent.data_source.item_id:
            errors.append("Standalone agent must be bound to a Fabric item.")
        if not agent.prompt.strip():
            errors.append("Standalone agent prompt is required.")
    elif project.deployment_mode == "orchestrator_only":
        orch = project.orchestrator_only
        if not orch.name.strip():
            errors.append("Orchestrator name is required.")
        if not orch.external_agents:
            errors.append("At least one external agent is required.")
        for ext in orch.external_agents:
            if not ext.agent_name.strip():
                errors.append(f"External agent '{ext.display_name}' must have an agent_name.")
    else:
        if not project.orchestrator.name.strip():
            errors.append("Orchestrator name is required.")
        if not project.orchestrator.prompt.strip():
            errors.append("Orchestrator prompt is required.")
        if not project.orchestrator.subagents:
            errors.append("At least one subagent is required.")
        for subagent in project.orchestrator.subagents:
            if _requires_fabric_item(subagent.data_source.source_type) and not subagent.data_source.item_id:
                errors.append(f"Subagent '{subagent.name}' must be bound to a Fabric item.")
            if not subagent.prompt.strip():
                errors.append(f"Subagent '{subagent.name}' prompt is required.")
    return {"valid": not errors, "errors": errors}


def build_hosted_agent_deployment(project: AgentProject, agent_name: str | None = None) -> dict[str, Any]:
    validation = validate_project(project)
    if not validation["valid"]:
        return validation

    package_name = _slug(agent_name or project.name)
    image = os.environ.get("HOSTED_AGENT_IMAGE", "").strip()
    if not image:
        acr = os.environ.get("ACR_LOGIN_SERVER", "<acr-login-server>")
        image = f"{acr}/hosted-agent-runtime:latest"
    return {
        "valid": True,
        "agent_name": agent_name or package_name,
        "image": image,
        "project_id": project.id,
        "runtime": "hosted-agent-runtime",
        "configuration_source": "fabric-cosmos",
    }


def build_hosted_agent_package(project: AgentProject, image_tag: str | None = None, agent_name: str | None = None) -> dict[str, Any]:
    return build_hosted_agent_deployment(project, agent_name)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip().lower()).strip("-")
    return slug or "fabric-agent"
