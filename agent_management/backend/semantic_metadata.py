from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import aiohttp
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import AzureCliCredential, ClientSecretCredential, DefaultAzureCredential

try:
    from croniter import croniter
except ImportError:
    croniter = None  # type: ignore[assignment]

from .project_store import create_project_store

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_API_ROOT = "https://api.fabric.microsoft.com"
FABRIC_OPERATION_POLL_LIMIT = 12


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def semantic_model_cache_id(workspace_id: str, semantic_model_id: str) -> str:
    return f"{workspace_id}:{semantic_model_id}"


def extract_semantic_model_sources(project: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []

    def add_source(source: dict[str, Any] | None, agent_name: str = "") -> None:
        if not isinstance(source, dict):
            return
        source_type = str(source.get("source_type") or "semantic_model")
        item_id = str(source.get("item_id") or source.get("semantic_model_id") or "")
        workspace_id = str(source.get("workspace_id") or "")
        if source_type != "semantic_model" or not item_id or not workspace_id:
            return
        sources.append({
            "project_id": project.get("id", ""),
            "project_name": project.get("name", ""),
            "agent_name": agent_name,
            "workspace_id": workspace_id,
            "workspace_name": source.get("workspace_name", ""),
            "semantic_model_id": item_id,
            "semantic_model_name": source.get("item_name") or source.get("semantic_model_name") or "",
        })

    mode = project.get("deployment_mode")
    if mode == "standalone":
        add_source((project.get("standalone_agent") or {}).get("semantic_model"), (project.get("standalone_agent") or {}).get("name", ""))
    elif mode == "orchestrator":
        for subagent in (project.get("orchestrator") or {}).get("subagents", []):
            add_source(subagent.get("semantic_model"), subagent.get("name", ""))
    else:
        for source in project.get("data_sources") or (project.get("orchestrator_only") or {}).get("data_sources") or []:
            add_source(source)

    unique: dict[str, dict[str, Any]] = {}
    for source in sources:
        unique[semantic_model_cache_id(source["workspace_id"], source["semantic_model_id"])] = source
    return list(unique.values())


def _credential():
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


def service_principal_fabric_token() -> str:
    return _credential().get_token("https://api.fabric.microsoft.com/.default").token


def _cosmos_container(container_env: str, default_container: str):
    endpoint = os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        raise RuntimeError("AGENT_MGMT_COSMOS_ENDPOINT is required for semantic metadata storage.")
    database_name = os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents")
    container_name = os.environ.get(container_env, default_container)
    client = CosmosClient(endpoint, credential=_credential())
    return client.get_database_client(database_name).get_container_client(container_name)


class SemanticMetadataStore:
    def __init__(self) -> None:
        self.container = _cosmos_container("AGENT_MGMT_METADATA_CONTAINER", "semanticmodelmetadata")

    def get_metadata(self, workspace_id: str, semantic_model_id: str) -> dict[str, Any] | None:
        item_id = semantic_model_cache_id(workspace_id, semantic_model_id)
        try:
            return self.container.read_item(item=item_id, partition_key=item_id)
        except CosmosResourceNotFoundError:
            return None

    def upsert_metadata(self, doc: dict[str, Any]) -> dict[str, Any]:
        doc["id"] = semantic_model_cache_id(doc["workspace_id"], doc["semantic_model_id"])
        doc["metadataid"] = doc["id"]
        doc["type"] = "semantic_model_metadata"
        self.container.upsert_item(doc)
        return doc

    def list_metadata(self) -> list[dict[str, Any]]:
        query = "SELECT c.id, c.workspace_id, c.workspace_name, c.semantic_model_id, c.semantic_model_name, c.status, c.refreshed_at, c.definition_hash, c.last_error FROM c WHERE c.type = 'semantic_model_metadata'"
        return list(self.container.query_items(query=query, enable_cross_partition_query=True))


class MetadataScheduleStore:
    def __init__(self) -> None:
        self.container = _cosmos_container("AGENT_MGMT_METADATA_SCHEDULE_CONTAINER", "metadatarefresh")

    def list_schedules(self) -> list[dict[str, Any]]:
        query = "SELECT * FROM c WHERE c.type = 'metadata_refresh_schedule' ORDER BY c.updated_at DESC"
        return list(self.container.query_items(query=query, enable_cross_partition_query=True))

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        try:
            return self.container.read_item(item=schedule_id, partition_key=schedule_id)
        except CosmosResourceNotFoundError:
            return None

    def upsert_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        schedule.setdefault("id", str(uuid.uuid4()))
        schedule["scheduleid"] = schedule["id"]
        schedule["type"] = "metadata_refresh_schedule"
        schedule.setdefault("enabled", True)
        schedule.setdefault("name", "Semantic metadata refresh")
        schedule.setdefault("cron", "0 2 * * *")
        schedule.setdefault("timezone", "UTC")
        schedule.setdefault("scope", "all_projects")
        schedule.setdefault("created_at", now)
        schedule["updated_at"] = now
        schedule["next_run_at"] = next_run_at(schedule.get("cron", "0 2 * * *"), schedule.get("next_run_at"))
        self.container.upsert_item(schedule)
        return schedule

    def delete_schedule(self, schedule_id: str) -> None:
        self.container.delete_item(item=schedule_id, partition_key=schedule_id)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        query = f"SELECT TOP {max(1, min(int(limit), 200))} * FROM c WHERE c.type = 'metadata_refresh_run' ORDER BY c.started_at DESC"
        return list(self.container.query_items(query=query, enable_cross_partition_query=True))

    def upsert_run(self, run: dict[str, Any]) -> dict[str, Any]:
        run.setdefault("id", f"run-{uuid.uuid4().hex}")
        run["runid"] = run["id"]
        run["type"] = "metadata_refresh_run"
        self.container.upsert_item(run)
        return run

    def try_acquire_lock(self, lock_id: str, owner: str, lease_seconds: int = 900) -> bool:
        now = utc_now()
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        try:
            lock = self.container.read_item(item=lock_id, partition_key=lock_id)
            existing_expiry = _parse_datetime(lock.get("expires_at"))
            if existing_expiry and existing_expiry > now and lock.get("owner") != owner:
                return False
        except CosmosResourceNotFoundError:
            lock = {"id": lock_id, "lockid": lock_id, "type": "metadata_refresh_lock"}
        lock.update({"owner": owner, "expires_at": expires_at, "updated_at": now.isoformat()})
        self.container.upsert_item(lock)
        return True

    def release_lock(self, lock_id: str, owner: str) -> None:
        try:
            lock = self.container.read_item(item=lock_id, partition_key=lock_id)
        except CosmosResourceNotFoundError:
            return
        if lock.get("owner") == owner:
            lock["expires_at"] = utc_now_iso()
            self.container.upsert_item(lock)


def next_run_at(cron: str, previous: str | None = None) -> str:
    if croniter is None:
        return (utc_now() + timedelta(days=1)).isoformat()
    base = max(_parse_datetime(previous) or utc_now(), utc_now())
    return croniter(cron, base).get_next(datetime).astimezone(timezone.utc).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


async def refresh_semantic_model_metadata(source: dict[str, Any], metadata_store: SemanticMetadataStore | None = None) -> dict[str, Any]:
    metadata_store = metadata_store or SemanticMetadataStore()
    token = service_principal_fabric_token()
    async with aiohttp.ClientSession() as session:
        definition = await _get_semantic_model_definition(session, source["workspace_id"], source["semantic_model_id"], token)
    if definition.get("errors"):
        doc = {**source, "id": semantic_model_cache_id(source["workspace_id"], source["semantic_model_id"]), "status": "failed", "source": "fabric_getDefinition", "refreshed_at": utc_now_iso(), "last_error": definition["errors"]}
        metadata_store.upsert_metadata(doc)
        return doc
    normalized = normalize_definition(source, definition)
    metadata_store.upsert_metadata(normalized)
    return normalized


async def refresh_project_metadata(project: dict[str, Any], metadata_store: SemanticMetadataStore | None = None) -> list[dict[str, Any]]:
    metadata_store = metadata_store or SemanticMetadataStore()
    results = []
    for source in extract_semantic_model_sources(project):
        results.append(await refresh_semantic_model_metadata(source, metadata_store))
    return results


async def refresh_all_project_metadata(trigger: str = "manual", schedule_id: str = "") -> dict[str, Any]:
    metadata_store = SemanticMetadataStore()
    schedule_store = MetadataScheduleStore()
    run = {"id": f"run-{uuid.uuid4().hex}", "schedule_id": schedule_id, "trigger": trigger, "started_at": utc_now_iso(), "status": "running", "projects_scanned": 0, "semantic_models_found": 0, "models_refreshed": 0, "models_failed": 0, "errors": []}
    schedule_store.upsert_run(run)
    try:
        projects = create_project_store().list_projects()
        run["projects_scanned"] = len(projects)
        seen_models: set[str] = set()
        for project in projects:
            for source in extract_semantic_model_sources(project):
                model_key = semantic_model_cache_id(source["workspace_id"], source["semantic_model_id"])
                if model_key in seen_models:
                    continue
                seen_models.add(model_key)
                run["semantic_models_found"] += 1
                result = await refresh_semantic_model_metadata(source, metadata_store)
                if result.get("status") == "failed":
                    run["models_failed"] += 1
                    run["errors"].append({"semantic_model_id": source["semantic_model_id"], "error": result.get("last_error")})
                else:
                    run["models_refreshed"] += 1
        run["status"] = "partial" if run["models_failed"] else "succeeded"
    except Exception as exc:
        run["status"] = "failed"
        run["errors"].append({"message": str(exc)})
    finally:
        run["finished_at"] = utc_now_iso()
        schedule_store.upsert_run(run)
    return run


async def run_due_schedules() -> list[dict[str, Any]]:
    schedule_store = MetadataScheduleStore()
    owner = os.environ.get("HOSTNAME") or uuid.uuid4().hex
    now = utc_now()
    runs = []
    for schedule in schedule_store.list_schedules():
        if not schedule.get("enabled", True):
            continue
        due_at = _parse_datetime(schedule.get("next_run_at"))
        if due_at and due_at > now:
            continue
        lock_id = f"metadata-refresh-lock:{schedule['id']}"
        if not schedule_store.try_acquire_lock(lock_id, owner):
            continue
        try:
            run = await refresh_all_project_metadata(trigger="schedule", schedule_id=schedule["id"])
            schedule["last_run_at"] = run.get("finished_at") or utc_now_iso()
            schedule["last_status"] = run.get("status")
            schedule["next_run_at"] = next_run_at(schedule.get("cron", "0 2 * * *"), schedule.get("next_run_at"))
            schedule_store.upsert_schedule(schedule)
            runs.append(run)
        finally:
            schedule_store.release_lock(lock_id, owner)
    return runs


async def _get_semantic_model_definition(session: aiohttp.ClientSession, workspace_id: str, semantic_model_id: str, fabric_token: str) -> dict[str, Any]:
    return await _post_json(session, f"{FABRIC_API}/workspaces/{workspace_id}/items/{semantic_model_id}/getDefinition?format=TMDL", _fabric_headers(fabric_token), {})


def normalize_definition(source: dict[str, Any], definition_payload: dict[str, Any]) -> dict[str, Any]:
    parts = definition_payload.get("definition", {}).get("parts") or definition_payload.get("parts") or []
    tables: list[dict[str, Any]] = []
    relationships: list[dict[str, Any]] = []
    for part in parts:
        path = str(part.get("path") or "")
        if path and not path.endswith(".tmdl"):
            continue
        text = _decode_definition_payload(part)
        if not text:
            continue
        tables.extend(_tables_from_tmdl(text))
        relationships.extend(_relationships_from_tmdl(text))
    return {**source, "id": semantic_model_cache_id(source["workspace_id"], source["semantic_model_id"]), "status": "current", "source": "fabric_getDefinition", "refreshed_at": utc_now_iso(), "definition_hash": hashlib.sha256(json.dumps(parts, sort_keys=True).encode("utf-8", errors="ignore")).hexdigest(), "tables": {"value": _merge_tables(tables), "source": "fabric_getDefinition"}, "relationships": relationships, "ai_instructions": _ai_instructions_from_parts(parts), "last_error": None}


async def _post_json(session: aiohttp.ClientSession, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    async with session.post(url, headers=headers, json=body) as response:
        text = await response.text()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        if response.status == 202 and response.headers.get("Location"):
            return await _poll_fabric_operation(session, response.headers["Location"], headers)
        if response.status >= 400:
            return {"errors": [{"status": response.status, "url": url, "message": payload}]}
        return payload


async def _poll_fabric_operation(session: aiohttp.ClientSession, location: str, headers: dict[str, str]) -> dict[str, Any]:
    operation_url = _fabric_url(location)
    for _ in range(FABRIC_OPERATION_POLL_LIMIT):
        async with session.get(operation_url, headers=headers) as response:
            text = await response.text()
            payload = json.loads(text) if text else {}
            if response.status >= 400:
                return {"errors": [{"status": response.status, "url": operation_url, "message": payload}]}
            if payload.get("definition") or payload.get("parts"):
                return payload
            status = str(payload.get("status") or payload.get("operationStatus") or "").lower()
            if status in {"succeeded", "completed"}:
                result_url = response.headers.get("Location") or response.headers.get("Resource-Location") or payload.get("resultUrl")
                if result_url and _fabric_url(result_url) != operation_url:
                    return await _get_json(session, _fabric_url(result_url), headers)
                return await _get_json(session, operation_url.rstrip("/") + "/result", headers)
            if status in {"failed", "cancelled", "canceled"}:
                return {"errors": [{"status": 202, "url": operation_url, "message": payload}]}
        await asyncio.sleep(1)
    return {"errors": [{"status": 202, "url": operation_url, "message": {"error": "Fabric getDefinition operation did not finish in time."}}]}


async def _get_json(session: aiohttp.ClientSession, url: str, headers: dict[str, str]) -> dict[str, Any]:
    async with session.get(url, headers=headers) as response:
        text = await response.text()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        if response.status >= 400:
            return {"errors": [{"status": response.status, "url": url, "message": payload}]}
        return payload


def _fabric_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _fabric_url(location: str) -> str:
    if location.startswith("http://") or location.startswith("https://"):
        return location
    if location.startswith("/"):
        return urljoin(FABRIC_API_ROOT, location)
    return urljoin(FABRIC_API + "/", location)


def _decode_definition_payload(part: dict[str, Any]) -> str:
    payload = part.get("payload") or part.get("content") or ""
    if not payload:
        return ""
    try:
        if part.get("payloadType") == "InlineBase64" or re.fullmatch(r"[A-Za-z0-9+/=\r\n]+", payload):
            return base64.b64decode(payload).decode("utf-8", errors="ignore")
    except Exception:
        pass
    return str(payload)


def _tables_from_tmdl(text: str) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    current_table: dict[str, Any] | None = None
    current_item: dict[str, Any] | None = None
    item_type = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if stripped.startswith("table "):
            current_table = {"name": _clean_tmdl_name(stripped.removeprefix("table ")), "description": "", "isHidden": False, "columns": [], "measures": []}
            tables.append(current_table)
            current_item = None
            item_type = ""
        elif current_table is not None and stripped.startswith("column "):
            parts = re.split(r"\s*=\s*", stripped.removeprefix("column "), maxsplit=1)
            current_item = {"name": parts[0].strip().strip("'").strip('"'), "dataType": parts[1].strip() if len(parts) > 1 else "", "description": "", "isHidden": False}
            current_table["columns"].append(current_item)
            item_type = "column"
        elif current_table is not None and stripped.startswith("measure "):
            parts = re.split(r"\s*=\s*", stripped.removeprefix("measure "), maxsplit=1)
            current_item = {"name": parts[0].strip().strip("'").strip('"'), "expression": parts[1].strip() if len(parts) > 1 else "", "description": "", "isHidden": False}
            current_table["measures"].append(current_item)
            item_type = "measure"
        elif stripped.startswith("description:") or stripped.startswith("description ="):
            value = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'").strip('"')
            if current_item is not None:
                current_item["description"] = value
            elif current_table is not None:
                current_table["description"] = value
        elif stripped.startswith("isHidden"):
            is_hidden = "true" in stripped.lower()
            if current_item is not None:
                current_item["isHidden"] = is_hidden
            elif current_table is not None:
                current_table["isHidden"] = is_hidden
        elif (stripped.startswith("dataType:") or stripped.startswith("dataType =")) and current_item is not None and item_type == "column":
            current_item["dataType"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip()
        elif (stripped.startswith("expression:") or stripped.startswith("expression =")) and current_item is not None and item_type == "measure":
            current_item["expression"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip()
    return tables


def _relationships_from_tmdl(text: str) -> list[dict[str, Any]]:
    relationships: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        if stripped.startswith("relationship "):
            current = {"name": _clean_tmdl_name(stripped.removeprefix("relationship "))}
            relationships.append(current)
        elif current is not None:
            for key in ["fromColumn", "toColumn", "fromTable", "toTable", "fromCardinality", "toCardinality", "crossFilteringBehavior"]:
                if stripped.startswith(f"{key}:") or stripped.startswith(f"{key} ="):
                    current[key] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'").strip('"')
    return relationships


def _ai_instructions_from_parts(parts: list[dict[str, Any]]) -> str:
    for part in parts:
        path = str(part.get("path") or "")
        if "linguistic" in path.lower():
            text = _decode_definition_payload(part)
            if text:
                return text
    for part in parts:
        path = str(part.get("path") or "")
        if path.endswith("model.tmdl") or path == "model.tmdl":
            text = _decode_definition_payload(part)
            match = re.search(r"annotation\s+__PBI_AIInstructions\s*=\s*```(.*?)```", text, re.DOTALL)
            if match:
                return match.group(1).strip()
    return ""


def _clean_tmdl_name(value: str) -> str:
    name = re.split(r"\s*=\s*|\s*:\s*", value, maxsplit=1)[0].strip()
    return name.strip("'").strip('"')


def _merge_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for table in tables:
        name = table.get("name")
        if not name:
            continue
        target = merged.setdefault(name, {"name": name, "description": table.get("description", ""), "isHidden": table.get("isHidden", False), "columns": [], "measures": []})
        if not target.get("description") and table.get("description"):
            target["description"] = table["description"]
        target["columns"].extend(table.get("columns", []))
        target["measures"].extend(table.get("measures", []))
    return list(merged.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run due semantic metadata refresh schedules.")
    parser.add_argument("--run-now", action="store_true", help="Refresh all semantic metadata immediately instead of checking schedules.")
    args = parser.parse_args()
    result = asyncio.run(refresh_all_project_metadata(trigger="manual") if args.run_now else run_due_schedules())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
