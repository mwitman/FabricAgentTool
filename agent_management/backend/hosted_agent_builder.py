from __future__ import annotations

import os
import re
from typing import Any

from .models import AgentProject

_VERSION_TAG_RE = re.compile(r"^v?\d+(?:[._-]\d+)*$", re.IGNORECASE)


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


def build_hosted_agent_deployment(project: AgentProject, agent_name: str | None = None, runtime_version: str | None = None) -> dict[str, Any]:
    validation = validate_project(project)
    if not validation["valid"]:
        return validation

    package_name = _slug(agent_name or project.name)
    image = os.environ.get("HOSTED_AGENT_IMAGE", "").strip()
    if not image:
        acr = os.environ.get("ACR_LOGIN_SERVER", "<acr-login-server>")
        image = f"{acr}/hosted-agent-runtime:latest"
    try:
        image_resolution = _resolve_runtime_image(image, runtime_version=runtime_version)
    except Exception as exc:
        return {
            "valid": False,
            "errors": [str(exc)],
            "image": image,
        }
    return {
        "valid": True,
        "agent_name": agent_name or package_name,
        **image_resolution,
        "project_id": project.id,
        "runtime": "hosted-agent-runtime",
        "configuration_source": "fabric-cosmos",
    }


def build_hosted_agent_package(project: AgentProject, image_tag: str | None = None, agent_name: str | None = None) -> dict[str, Any]:
    return build_hosted_agent_deployment(project, agent_name, runtime_version=image_tag)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9-]+", "-", value.strip().lower()).strip("-")
    return slug or "fabric-agent"


def _resolve_runtime_image(image: str, runtime_version: str | None = None) -> dict[str, str]:
    parsed = _parse_container_image(image)
    selected_version = (runtime_version or "").strip()
    if selected_version:
        if selected_version == "latest":
            raise ValueError("Select a concrete runtime version tag, not latest.")
        pinned_image = f"{parsed['registry']}/{parsed['repository']}:{selected_version}"
        return {
            "image": pinned_image,
            "runtime_image_source": image,
            "runtime_version": selected_version,
        }
    if parsed["tag"] != "latest":
        return {
            "image": image,
            "runtime_image_source": image,
            "runtime_version": parsed["tag"],
        }

    resolved = _resolve_acr_latest_tag(parsed["registry"], parsed["repository"])
    pinned_image = f"{parsed['registry']}/{parsed['repository']}:{resolved['tag']}"
    return {
        "image": pinned_image,
        "runtime_image_source": image,
        "runtime_version": resolved["tag"],
        "runtime_latest_digest": resolved["digest"],
    }


def _parse_container_image(image: str) -> dict[str, str]:
    if "://" in image:
        raise ValueError(f"Hosted runtime image must not include a URL scheme: {image}")
    if "/" not in image:
        raise ValueError(f"Hosted runtime image must include a registry and repository: {image}")
    registry, remainder = image.split("/", 1)
    if not registry or not remainder:
        raise ValueError(f"Hosted runtime image must include a registry and repository: {image}")
    last_segment = remainder.rsplit("/", 1)[-1]
    if ":" in last_segment:
        repository, tag = remainder.rsplit(":", 1)
    else:
        repository, tag = remainder, "latest"
    if "@" in repository or "@" in tag:
        raise ValueError("Hosted runtime image must use a tag, not a digest reference.")
    return {"registry": registry, "repository": repository, "tag": tag or "latest"}


def _resolve_acr_latest_tag(registry: str, repository: str) -> dict[str, str]:
    try:
        from azure.containerregistry import ContainerRegistryClient
    except Exception as exc:
        raise RuntimeError("Install azure-containerregistry to resolve ACR latest tags during deployment.") from exc

    credential = _acr_credential()
    try:
        with ContainerRegistryClient(f"https://{registry}", credential) as client:
            latest = client.get_tag_properties(repository, "latest")
            latest_digest = getattr(latest, "digest", None)
            if not latest_digest:
                raise RuntimeError(f"ACR tag {repository}:latest did not include a digest.")

            matching_tags = [
                tag
                for tag in client.list_tag_properties(repository)
                if tag.name != "latest" and getattr(tag, "digest", None) == latest_digest
            ]
    except Exception as exc:
        raise RuntimeError(f"Could not resolve {registry}/{repository}:latest from ACR: {exc}") from exc
    finally:
        credential.close()

    if not matching_tags:
        raise RuntimeError(
            f"ACR tag {repository}:latest does not have a matching version tag. "
            "Push latest together with a concrete tag, for example -Tags latest,v10."
        )

    version_tags = [tag for tag in matching_tags if _VERSION_TAG_RE.match(tag.name)]
    selected = max(version_tags or matching_tags, key=_tag_sort_key)
    return {"tag": selected.name, "digest": latest_digest}


def _acr_credential():
    from azure.identity import ClientSecretCredential, DefaultAzureCredential

    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def list_runtime_versions(image: str | None = None) -> dict[str, Any]:
    source_image = (image or os.environ.get("HOSTED_AGENT_IMAGE", "").strip())
    if not source_image:
        acr = os.environ.get("ACR_LOGIN_SERVER", "<acr-login-server>")
        source_image = f"{acr}/hosted-agent-runtime:latest"
    parsed = _parse_container_image(source_image)
    try:
        from azure.containerregistry import ContainerRegistryClient
    except Exception as exc:
        raise RuntimeError("Install azure-containerregistry to list ACR runtime tags.") from exc

    credential = _acr_credential()
    try:
        with ContainerRegistryClient(f"https://{parsed['registry']}", credential) as client:
            latest_digest = ""
            try:
                latest_digest = getattr(client.get_tag_properties(parsed["repository"], "latest"), "digest", "") or ""
            except Exception:
                latest_digest = ""
            tags = [tag for tag in client.list_tag_properties(parsed["repository"]) if tag.name != "latest"]
    finally:
        credential.close()

    tags = sorted(tags, key=_tag_sort_key, reverse=True)
    return {
        "image_source": source_image,
        "repository": f"{parsed['registry']}/{parsed['repository']}",
        "latest_version": next((tag.name for tag in tags if latest_digest and getattr(tag, "digest", None) == latest_digest), ""),
        "versions": [
            {
                "version": tag.name,
                "digest": getattr(tag, "digest", "") or "",
                "is_latest": bool(latest_digest and getattr(tag, "digest", None) == latest_digest),
                "updated_at": _tag_timestamp(tag),
            }
            for tag in tags
        ],
    }


def _tag_timestamp(tag: Any) -> str:
    value = getattr(tag, "last_updated_on", None) or getattr(tag, "created_on", None)
    return value.isoformat() if hasattr(value, "isoformat") else ""


def _tag_sort_key(tag: Any) -> tuple[int, tuple[int, ...], str]:
    name = str(tag.name)
    numbers = tuple(int(part) for part in re.findall(r"\d+", name))
    updated = getattr(tag, "last_updated_on", None) or getattr(tag, "created_on", None)
    updated_value = updated.isoformat() if hasattr(updated, "isoformat") else str(updated or "")
    return (1 if _VERSION_TAG_RE.match(name) else 0, numbers, updated_value)
