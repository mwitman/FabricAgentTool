"""List model deployments available in the configured Foundry project."""

from __future__ import annotations

import os
from typing import Any

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


async def list_foundry_model_deployments() -> dict[str, Any]:
    """Query the Foundry project endpoint for available model deployments."""
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        return {"deployments": [], "error": "FOUNDRY_PROJECT_ENDPOINT not configured."}

    try:
        token = _access_token()
    except Exception as exc:
        return {"deployments": [], "error": f"Failed to acquire Foundry token: {str(exc)[:200]}"}

    api_version = os.environ.get("FOUNDRY_API_VERSION", "v1")
    features = os.environ.get("FOUNDRY_FEATURES", "")
    url = f"{endpoint}/deployments?api-version={api_version}"

    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if features:
        headers["Foundry-Features"] = features

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status >= 400:
                text = await response.text()
                return {"deployments": [], "error": f"Foundry API returned {response.status}: {text[:500]}"}
            payload = await response.json()

    raw_deployments = payload.get("value") or payload.get("data") or payload.get("deployments") or []
    deployments = []
    for dep in raw_deployments:
        name = dep.get("name") or dep.get("deployment_name") or dep.get("id") or ""
        model = dep.get("model") if isinstance(dep.get("model"), dict) else {}
        model_name = model.get("name") or dep.get("model_name", "")
        provider = model.get("provider") or model.get("publisher") or dep.get("provider") or dep.get("publisher") or ""
        model_format = model.get("format") or model.get("model_format") or dep.get("format") or dep.get("model_format") or ""
        capabilities = model.get("capabilities") or dep.get("capabilities") or {}
        deployments.append({
            "deployment_name": name,
            "model_display_name": model_name or name,
            "model_name": model_name or "",
            "provider": provider,
            "publisher": model.get("publisher") or dep.get("publisher") or provider,
            "model_format": model_format,
            "capabilities": capabilities if isinstance(capabilities, dict) else {},
        })
    return {"deployments": deployments}
