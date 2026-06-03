"""Permissions store backed by a dedicated Cosmos DB container.

Stores Role documents and AgentRoleBinding documents in a single
'roles' container partitioned by /roleid.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import AzureCliCredential, ClientSecretCredential, DefaultAzureCredential

from .models import AgentRoleBinding, Role, RoleMember


class PermissionsStore:
    # -- Roles --
    def list_roles(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_role(self, role_id: str) -> Role:
        raise NotImplementedError

    def save_role(self, role: Role) -> Role:
        raise NotImplementedError

    def delete_role(self, role_id: str) -> None:
        raise NotImplementedError

    # -- Agent role bindings --
    def list_agent_bindings(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_agent_binding_by_project(self, project_id: str) -> AgentRoleBinding | None:
        raise NotImplementedError

    def save_agent_binding(self, binding: AgentRoleBinding) -> AgentRoleBinding:
        raise NotImplementedError

    def delete_agent_binding(self, binding_id: str) -> None:
        raise NotImplementedError

    # -- Resolution: which agents can a user see? --
    def get_agents_for_user(self, user_object_id: str, group_ids: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError


def _is_admin_role(role: dict[str, Any]) -> bool:
    return role.get("name", "").strip().lower() in {"admin", "admins"}


def _configured_bootstrap_admin_ids() -> set[str]:
    raw = os.environ.get("AGENT_MGMT_BOOTSTRAP_ADMIN_OBJECT_IDS", "")
    return {item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()}


def _bootstrap_admin_match(user_object_id: str, group_ids: list[str]) -> tuple[str, str] | None:
    configured_ids = _configured_bootstrap_admin_ids()
    if not configured_ids:
        return None
    user_id = user_object_id.strip().lower()
    if user_id and user_id in configured_ids:
        return user_object_id, "user"
    for group_id in group_ids:
        if group_id.strip().lower() in configured_ids:
            return group_id, "group"
    return None


def _ensure_bootstrap_admin_role(
    roles: list[dict[str, Any]],
    user_object_id: str,
    group_ids: list[str],
    save_role,
) -> list[dict[str, Any]]:
    match = _bootstrap_admin_match(user_object_id, group_ids)
    if match is None:
        return roles

    object_id, member_type = match
    admin_role_index = next((index for index, role in enumerate(roles) if _is_admin_role(role)), None)
    member = RoleMember(object_id=object_id, display_name="Bootstrap admin", member_type=member_type)

    if admin_role_index is None:
        saved = save_role(Role(name="Admin", description="Bootstrap administrator role.", members=[member]))
        return [*roles, saved.model_dump(mode="json")]

    role = Role.model_validate(roles[admin_role_index])
    if any(existing.object_id.lower() == object_id.lower() for existing in role.members):
        return roles
    role.members.append(member)
    saved = save_role(role)
    updated_roles = [*roles]
    updated_roles[admin_role_index] = saved.model_dump(mode="json")
    return updated_roles


class CosmosPermissionsStore(PermissionsStore):
    def __init__(self) -> None:
        endpoint = os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "").strip()
        if not endpoint:
            raise RuntimeError("AGENT_MGMT_COSMOS_ENDPOINT is required.")

        database_name = os.environ.get("AGENT_MGMT_PERMISSIONS_DATABASE") or os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "permissions")
        container_name = os.environ.get("AGENT_MGMT_PERMISSIONS_CONTAINER", "roles")
        self.partition_field = os.environ.get("AGENT_MGMT_PERMISSIONS_PARTITION_KEY", "/roleid").strip("/") or "roleid"
        credential = _cosmos_credential()
        self.client = CosmosClient(endpoint, credential=credential)
        self.database = self.client.get_database_client(database_name)
        self.container = self.database.get_container_client(container_name)
        # Verify the container exists by reading its properties
        self.container.read()

    # -- Roles --
    def list_roles(self) -> list[dict[str, Any]]:
        query = "SELECT * FROM c WHERE c.type = 'role'"
        roles = list(self.container.query_items(query=query, enable_cross_partition_query=True))
        return sorted(roles, key=lambda role: role.get("name", ""))

    def get_role(self, role_id: str) -> Role:
        item = self.container.read_item(item=role_id, partition_key=role_id)
        return Role.model_validate(item)

    def save_role(self, role: Role) -> Role:
        role.touch()
        item = role.model_dump(mode="json")
        self._save_item(item)
        return role

    def delete_role(self, role_id: str) -> None:
        self.container.delete_item(item=role_id, partition_key=role_id)

    # -- Agent role bindings --
    def list_agent_bindings(self) -> list[dict[str, Any]]:
        query = "SELECT * FROM c WHERE c.type = 'agent_role_binding'"
        bindings = list(self.container.query_items(query=query, enable_cross_partition_query=True))
        return sorted(bindings, key=lambda binding: binding.get("project_display_name", ""))

    def get_agent_binding_by_project(self, project_id: str) -> AgentRoleBinding | None:
        query = "SELECT * FROM c WHERE c.type = 'agent_role_binding' AND c.project_id = @pid"
        params = [{"name": "@pid", "value": project_id}]
        items = list(self.container.query_items(query=query, parameters=params, enable_cross_partition_query=True))
        if not items:
            return None
        return AgentRoleBinding.model_validate(items[0])

    def save_agent_binding(self, binding: AgentRoleBinding) -> AgentRoleBinding:
        binding.touch()
        item = binding.model_dump(mode="json")
        self._save_item(item)
        return binding

    def _save_item(self, item: dict[str, Any]) -> None:
        item[self.partition_field] = item["id"]
        try:
            self.container.replace_item(item=item["id"], body=item)
        except CosmosResourceNotFoundError:
            self.container.create_item(item)

    def delete_agent_binding(self, binding_id: str) -> None:
        self.container.delete_item(item=binding_id, partition_key=binding_id)

    # -- Resolution --
    def get_agents_for_user(self, user_object_id: str, group_ids: list[str]) -> list[dict[str, Any]]:
        """Return agent bindings where the user or any of their groups is a member of an assigned role."""
        roles = self.list_roles()
        roles = _ensure_bootstrap_admin_role(roles, user_object_id, group_ids, self.save_role)
        all_member_ids = {user_object_id} | set(group_ids)

        # Find which role IDs this user belongs to
        user_role_ids: set[str] = set()
        is_admin = False
        for role in roles:
            for member in role.get("members", []):
                if member.get("object_id") in all_member_ids:
                    user_role_ids.add(role["id"])
                    if _is_admin_role(role):
                        is_admin = True
                    break

        if is_admin:
            return self.list_agent_bindings()

        if not user_role_ids:
            return []

        # Find agent bindings that include at least one of the user's roles
        bindings = self.list_agent_bindings()
        result = []
        for binding in bindings:
            binding_role_ids = set(binding.get("role_ids", []))
            if binding_role_ids & user_role_ids:
                result.append(binding)
        return result


class LocalPermissionsStore(PermissionsStore):
    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parent / "data" / "permissions"
        self.root.mkdir(parents=True, exist_ok=True)

    def list_roles(self) -> list[dict[str, Any]]:
        results = []
        for path in self.root.glob("role_*.json"):
            results.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(results, key=lambda r: r.get("name", ""))

    def get_role(self, role_id: str) -> Role:
        path = self.root / f"role_{role_id}.json"
        if not path.exists():
            raise KeyError(role_id)
        return Role.model_validate_json(path.read_text(encoding="utf-8"))

    def save_role(self, role: Role) -> Role:
        role.touch()
        path = self.root / f"role_{role.id}.json"
        path.write_text(role.model_dump_json(indent=2), encoding="utf-8")
        return role

    def delete_role(self, role_id: str) -> None:
        path = self.root / f"role_{role_id}.json"
        if path.exists():
            path.unlink()

    def list_agent_bindings(self) -> list[dict[str, Any]]:
        results = []
        for path in self.root.glob("binding_*.json"):
            results.append(json.loads(path.read_text(encoding="utf-8")))
        return sorted(results, key=lambda b: b.get("project_display_name", ""))

    def get_agent_binding_by_project(self, project_id: str) -> AgentRoleBinding | None:
        for path in self.root.glob("binding_*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("project_id") == project_id:
                return AgentRoleBinding.model_validate(data)
        return None

    def save_agent_binding(self, binding: AgentRoleBinding) -> AgentRoleBinding:
        binding.touch()
        path = self.root / f"binding_{binding.id}.json"
        path.write_text(binding.model_dump_json(indent=2), encoding="utf-8")
        return binding

    def delete_agent_binding(self, binding_id: str) -> None:
        path = self.root / f"binding_{binding_id}.json"
        if path.exists():
            path.unlink()

    def get_agents_for_user(self, user_object_id: str, group_ids: list[str]) -> list[dict[str, Any]]:
        roles = self.list_roles()
        roles = _ensure_bootstrap_admin_role(roles, user_object_id, group_ids, self.save_role)
        all_member_ids = {user_object_id} | set(group_ids)
        user_role_ids: set[str] = set()
        is_admin = False
        for role in roles:
            for member in role.get("members", []):
                if member.get("object_id") in all_member_ids:
                    user_role_ids.add(role["id"])
                    if _is_admin_role(role):
                        is_admin = True
                    break
        if is_admin:
            return self.list_agent_bindings()
        if not user_role_ids:
            return []
        bindings = self.list_agent_bindings()
        return [b for b in bindings if set(b.get("role_ids", [])) & user_role_ids]


def create_permissions_store() -> PermissionsStore:
    """Create a permissions store, preferring Cosmos, falling back to local if allowed."""
    allow_local = os.environ.get("AGENT_MGMT_ALLOW_LOCAL_STORE", "false").lower() == "true"
    try:
        return CosmosPermissionsStore()
    except Exception as exc:
        if allow_local:
            print(f"[permissions_store] Cosmos Permissions container unavailable ({type(exc).__name__}), using local file store.")
            return LocalPermissionsStore()
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
