from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from azure.cosmos import CosmosClient
from azure.identity import AzureCliCredential, ClientSecretCredential, DefaultAzureCredential

from .models import AgentProject

# Keys whose values must never be persisted to Cosmos
_SENSITIVE_ENV_KEYS = {"APP_CLIENT_SECRET", "AZURE_CLIENT_SECRET", "CLIENT_SECRET", "SECRET"}


def _strip_secrets(obj: Any) -> None:
    """Recursively remove sensitive keys from environment_variables dicts."""
    if isinstance(obj, dict):
        if "environment_variables" in obj and isinstance(obj["environment_variables"], dict):
            for key in list(obj["environment_variables"].keys()):
                if key.upper() in _SENSITIVE_ENV_KEYS or "SECRET" in key.upper():
                    del obj["environment_variables"][key]
        for value in obj.values():
            _strip_secrets(value)
    elif isinstance(obj, list):
        for item in obj:
            _strip_secrets(item)


class ProjectStore:
    def list_projects(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_project(self, project_id: str) -> AgentProject:
        raise NotImplementedError

    def save_project(self, project: AgentProject) -> AgentProject:
        raise NotImplementedError

    def delete_project(self, project_id: str) -> None:
        raise NotImplementedError


class CosmosProjectStore(ProjectStore):
    def __init__(self) -> None:
        endpoint = os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "").strip()
        if not endpoint:
            raise RuntimeError("AGENT_MGMT_COSMOS_ENDPOINT is required for Cosmos project storage.")

        database_name = os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents")
        container_name = os.environ.get("AGENT_MGMT_COSMOS_CONTAINER", "agentmetadata")
        self.partition_field = os.environ.get("AGENT_MGMT_COSMOS_PARTITION_KEY", "/projectid").strip("/") or "projectid"
        credential = _cosmos_credential()
        self.client = CosmosClient(endpoint, credential=credential)
        self.database = self.client.get_database_client(database_name)
        self.container = self.database.get_container_client(container_name)

    def list_projects(self) -> list[dict[str, Any]]:
        query = "SELECT * FROM c WHERE c.type = 'agent_project'"
        projects = list(self.container.query_items(query=query, enable_cross_partition_query=True))
        return sorted(projects, key=lambda project: project.get("updated_at") or "", reverse=True)

    def get_project(self, project_id: str) -> AgentProject:
        item = self.container.read_item(item=project_id, partition_key=project_id)
        return AgentProject.model_validate(item)

    def save_project(self, project: AgentProject) -> AgentProject:
        project.touch()
        item = project.model_dump(mode="json", by_alias=True)
        item[self.partition_field] = project.id
        _strip_secrets(item)
        self.container.upsert_item(item)
        return project

    def delete_project(self, project_id: str) -> None:
        self.container.delete_item(item=project_id, partition_key=project_id)


class LocalProjectStore(ProjectStore):
    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parent / "data" / "projects"
        self.root.mkdir(parents=True, exist_ok=True)

    def list_projects(self) -> list[dict[str, Any]]:
        projects = []
        for path in self.root.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            projects.append(data)
        return sorted(projects, key=lambda project: project.get("updated_at") or "", reverse=True)

    def get_project(self, project_id: str) -> AgentProject:
        path = self.root / f"{project_id}.json"
        if not path.exists():
            raise KeyError(project_id)
        return AgentProject.model_validate_json(path.read_text(encoding="utf-8"))

    def save_project(self, project: AgentProject) -> AgentProject:
        project.touch()
        path = self.root / f"{project.id}.json"
        path.write_text(project.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        return project

    def delete_project(self, project_id: str) -> None:
        path = self.root / f"{project_id}.json"
        if path.exists():
            path.unlink()


def create_project_store() -> ProjectStore:
    log = logging.getLogger(__name__)
    allow_local = os.environ.get("AGENT_MGMT_ALLOW_LOCAL_STORE", "false").lower() == "true"
    try:
        store = CosmosProjectStore()
        log.info("[project_store] Using CosmosProjectStore")
        return store
    except Exception as exc:
        if allow_local:
            log.warning("[project_store] Cosmos unavailable (%s), using LocalProjectStore", exc)
            return LocalProjectStore()
        raise


def _cosmos_credential():
    auth_mode = os.environ.get("AGENT_MGMT_COSMOS_AUTH_MODE", "service_principal").lower()
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
    if auth_mode in {"azure_cli", "user"}:
        return AzureCliCredential(tenant_id=tenant_id)
    if auth_mode in {"default", "managed_identity"}:
        return DefaultAzureCredential(exclude_interactive_browser_credential=True)
    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


# ---------------------------------------------------------------------------
# Version Store — immutable deployment snapshots
# ---------------------------------------------------------------------------


class VersionStore:
    """Stores immutable version snapshots of projects keyed by projectid + version."""

    def __init__(self) -> None:
        endpoint = os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "").strip()
        if not endpoint:
            raise RuntimeError("AGENT_MGMT_COSMOS_ENDPOINT is required for VersionStore.")
        database_name = os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents")
        container_name = os.environ.get("AGENT_MGMT_VERSIONS_CONTAINER", "agentversions")
        credential = _cosmos_credential()
        client = CosmosClient(endpoint, credential=credential)
        self.database = client.get_database_client(database_name)
        self.container = self.database.get_container_client(container_name)

    def save_version(self, project_id: str, version: str, snapshot: dict[str, Any], deployed_by: str = "") -> dict[str, Any]:
        """Write an immutable version snapshot (secrets stripped)."""
        from datetime import datetime, timezone
        import copy
        clean_snapshot = copy.deepcopy(snapshot)
        _strip_secrets(clean_snapshot)
        doc = {
            "id": f"{project_id}:{version}",
            "projectid": project_id,
            "version": version,
            "snapshot": clean_snapshot,
            "deployed_at": datetime.now(timezone.utc).isoformat(),
            "deployed_by": deployed_by,
        }
        self.container.upsert_item(doc)
        return doc

    def get_version(self, project_id: str, version: str) -> dict[str, Any] | None:
        """Read a specific version snapshot."""
        try:
            return self.container.read_item(item=f"{project_id}:{version}", partition_key=project_id)
        except Exception:
            return None

    def list_versions(self, project_id: str) -> list[dict[str, Any]]:
        """List all versions for a project, newest first."""
        query = "SELECT c.id, c.version, c.deployed_at, c.deployed_by FROM c WHERE c.projectid = @pid ORDER BY c.deployed_at DESC"
        params = [{"name": "@pid", "value": project_id}]
        return list(self.container.query_items(query=query, parameters=params, partition_key=project_id))

    def delete_version(self, project_id: str, version: str) -> None:
        """Delete a version snapshot."""
        try:
            self.container.delete_item(item=f"{project_id}:{version}", partition_key=project_id)
        except Exception:
            pass


_version_store: VersionStore | None = None


def get_version_store() -> VersionStore:
    global _version_store
    if _version_store is None:
        _version_store = VersionStore()
    return _version_store
