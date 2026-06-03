from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import aiohttp
from azure.identity import ClientSecretCredential, DefaultAzureCredential


def _credential():
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _access_token() -> str:
    return _credential().get_token("https://ai.azure.com/.default").token


def foundry_agent_link(agent_name: str) -> str:
    if not agent_name:
        return ""

    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    project_name = endpoint.rsplit("/api/projects/", 1)[-1] if "/api/projects/" in endpoint else ""
    template = os.environ.get("FOUNDRY_AGENT_PORTAL_URL_TEMPLATE", "").strip()
    if template:
        return template.format(agent_name=quote(agent_name, safe=""), project_name=quote(project_name, safe=""))

    portal_url = os.environ.get("FOUNDRY_PORTAL_URL", "").strip().rstrip("/")
    if portal_url:
        return portal_url

    return "https://ai.azure.com/"


async def create_hosted_agent_version(
    agent_name: str,
    image: str,
    description: str,
    metadata: dict[str, Any] | None = None,
    environment_variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is required.")
    api_version = os.environ.get("FOUNDRY_API_VERSION", "v1")
    features = os.environ.get("FOUNDRY_FEATURES", "HostedAgents=V1Preview")
    url = f"{endpoint}/agents/{agent_name}/versions?api-version={api_version}"
    body = {
        "definition": {
            "kind": "hosted",
            "image": image,
            "cpu": "1",
            "memory": "2Gi",
            "container_protocol_versions": [{"protocol": "responses", "version": "1.0.0"}],
            "environment_variables": {**_agent_environment(), **(environment_variables or {})},
        },
        "description": description,
        "metadata": metadata or {"source": "agent_management"},
    }
    headers = {
        "Authorization": f"Bearer {_access_token()}",
        "Foundry-Features": features,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body, timeout=120) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"raw": text}
            if response.status >= 400:
                return {"errors": [{"status": response.status, "message": _redact_sensitive(payload)}]}
            return {**payload, "foundry_agent_link": foundry_agent_link(agent_name)}


async def get_hosted_agent_info(agent_name: str) -> dict[str, Any]:
    """Fetch the latest agent details (endpoint, version, status) from Foundry."""
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return {"error": "FOUNDRY_PROJECT_ENDPOINT is required."}
    api_version = os.environ.get("FOUNDRY_API_VERSION", "v1")
    features = os.environ.get("FOUNDRY_FEATURES", "HostedAgents=V1Preview")
    url = f"{endpoint}/agents/{agent_name}?api-version={api_version}"
    headers = {
        "Authorization": f"Bearer {_access_token()}",
        "Foundry-Features": features,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=30) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"raw": text}
            if response.status >= 400:
                return {"error": f"Failed to fetch agent info: {response.status}", "detail": _redact_sensitive(payload)}
            # Build the agent's responses endpoint URL
            agent_endpoint = f"{endpoint}/agents/{agent_name}/endpoint/protocols/openai/responses"
            return {
                "agent_name": agent_name,
                "agent_endpoint": agent_endpoint,
                "version": payload.get("version") or payload.get("latest_version") or payload.get("id"),
                "status": payload.get("status") or payload.get("provisioning_state"),
                "foundry_agent_link": foundry_agent_link(agent_name),
                "raw": _redact_sensitive(payload),
            }


async def invoke_hosted_agent(
    agent_name: str,
    message: str,
    conversation_id: str,
    fabric_token: str | None = None,
    powerbi_token: str | None = None,
) -> dict[str, Any]:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is required.")
    api_version = os.environ.get("FOUNDRY_API_VERSION", "v1")
    features = os.environ.get("FOUNDRY_FEATURES", "HostedAgents=V1Preview")
    url = f"{endpoint}/agents/{agent_name}/endpoint/protocols/openai/responses?api-version={api_version}"
    body = {
        "input": message,
        "conversation_id": conversation_id,
        "stream": False,
        "fabric_token": fabric_token,
        "powerbi_token": powerbi_token,
    }
    headers = {
        "Authorization": f"Bearer {_access_token()}",
        "Foundry-Features": features,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body, timeout=300) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"raw": text}
            if response.status >= 400:
                return {"errors": [{"status": response.status, "message": _redact_sensitive(payload)}], "agent_name": agent_name}
            if isinstance(payload, dict) and payload.get("error"):
                return {"errors": [{"status": response.status, "message": _redact_sensitive(payload)}], "agent_name": agent_name}
            metadata = payload.get("metadata") if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict) else {}
            return {"agent_name": agent_name, "response": _output_text(payload), "metadata": _redact_sensitive(metadata), "raw": _redact_sensitive(payload)}


def _output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload or "")
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    output = payload.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text") or part.get("content")
                        if isinstance(text, str):
                            parts.append(text)
        if parts:
            return "".join(parts)
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("output_text"), str):
        return response["output_text"]
    return ""


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = key.lower()
            if any(term in normalized for term in ["secret", "password", "token", "key", "authorization"]):
                redacted[key] = "[redacted]"
            else:
                redacted[key] = _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value


def _agent_environment() -> dict[str, str]:
    names = [
        "AZURE_OPENAI_DEPLOYMENT_NAME",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_TENANT_ID",
        "APP_CLIENT_ID",
        "APP_CLIENT_SECRET",
        "FABRIC_CORE_MCP_ENDPOINT",
    ]
    environment = {name: os.environ[name] for name in names if os.environ.get(name)}
    aliases = {
        "FOUNDRY_PROJECT_ENDPOINT": "MAF_FOUNDRY_PROJECT_ENDPOINT",
        "AGENT_MGMT_COSMOS_ENDPOINT": "MAF_MGMT_COSMOS_ENDPOINT",
        "AGENT_MGMT_COSMOS_DATABASE": "MAF_MGMT_COSMOS_DATABASE",
        "AGENT_MGMT_COSMOS_CONTAINER": "MAF_MGMT_COSMOS_CONTAINER",
        "AGENT_MGMT_COSMOS_PARTITION_KEY": "MAF_MGMT_COSMOS_PARTITION_KEY",
    }
    for source, target in aliases.items():
        if os.environ.get(source):
            environment[target] = os.environ[source]
    return environment
