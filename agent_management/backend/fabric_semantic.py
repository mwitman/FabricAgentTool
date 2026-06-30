from __future__ import annotations

import base64
import asyncio
import json
import re
from typing import Any
from urllib.parse import urljoin

import aiohttp

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_API_ROOT = "https://api.fabric.microsoft.com"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
SEMANTIC_MODEL_TYPES = {"SemanticModel", "PowerBIDataset"}
GRAPHQL_TYPES = {"GraphQLApi"}
SQL_ENDPOINT_TYPES = {"SQLEndpoint", "Warehouse"}
DATA_AGENT_TYPES = {"DataAgent", "FabricDataAgent", "DataAgentItem", "FabricDataAgentItem"}
FABRIC_ITEM_TYPE_MAP: dict[str, str] = {}
for _t in SEMANTIC_MODEL_TYPES:
    FABRIC_ITEM_TYPE_MAP[_t] = "semantic_model"
for _t in GRAPHQL_TYPES:
    FABRIC_ITEM_TYPE_MAP[_t] = "graphql"
for _t in SQL_ENDPOINT_TYPES:
    FABRIC_ITEM_TYPE_MAP[_t] = "sql_endpoint"
for _t in DATA_AGENT_TYPES:
    FABRIC_ITEM_TYPE_MAP[_t] = "data_agent"
ALL_SUPPORTED_TYPES = set(FABRIC_ITEM_TYPE_MAP.keys())
FABRIC_OPERATION_POLL_LIMIT = 12


def _normalize_fabric_type(item_type: str) -> str:
    return re.sub(r"[^a-z0-9]", "", item_type.lower())


FABRIC_ITEM_TYPE_NORMALIZED_MAP = {_normalize_fabric_type(key): value for key, value in FABRIC_ITEM_TYPE_MAP.items()}


def _map_fabric_item_type(item_type: str) -> str:
    return FABRIC_ITEM_TYPE_MAP.get(item_type) or FABRIC_ITEM_TYPE_NORMALIZED_MAP.get(_normalize_fabric_type(item_type), "")


def fabric_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def powerbi_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


async def list_workspaces(fabric_token: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as session:
        return await _get_json(session, f"{FABRIC_API}/workspaces", fabric_headers(fabric_token))


async def list_semantic_models(fabric_token: str, search_text: str = "") -> dict[str, Any]:
    search_lower = search_text.lower().strip()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        workspaces_payload = await _get_json(session, f"{FABRIC_API}/workspaces", fabric_headers(fabric_token))
        if "errors" in workspaces_payload:
            return {"semantic_models": [], "errors": workspaces_payload["errors"]}
        for workspace in workspaces_payload.get("value", []):
            workspace_id = workspace.get("id")
            if not workspace_id:
                continue
            items_payload = await _get_json(session, f"{FABRIC_API}/workspaces/{workspace_id}/items", fabric_headers(fabric_token))
            if "errors" in items_payload:
                errors.extend(items_payload["errors"])
                continue
            for item in items_payload.get("value", []):
                item_type = item.get("type") or item.get("itemType")
                name = item.get("displayName") or item.get("name") or ""
                workspace_name = workspace.get("displayName") or workspace.get("name") or ""
                if item_type not in SEMANTIC_MODEL_TYPES:
                    continue
                score = _score(search_lower, name, workspace_name)
                if search_lower and score == 0:
                    continue
                results.append(
                    {
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "semantic_model_id": item.get("id"),
                        "semantic_model_name": name,
                        "type": item_type,
                        "description": item.get("description"),
                        "score": score,
                    }
                )
    results.sort(key=lambda model: model.get("score", 0), reverse=True)
    return {"semantic_models": results, "errors": errors}


async def list_fabric_items(
    fabric_token: str,
    search_text: str = "",
    source_type: str = "",
) -> dict[str, Any]:
    """List Fabric workspace items filtered by data source type.

    source_type can be: semantic_model, graphql, sql_endpoint, data_agent, or empty for all.
    """
    search_lower = search_text.lower().strip()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        workspaces_payload = await _get_json(session, f"{FABRIC_API}/workspaces", fabric_headers(fabric_token))
        if "errors" in workspaces_payload:
            return {"items": [], "errors": workspaces_payload["errors"]}
        for workspace in workspaces_payload.get("value", []):
            workspace_id = workspace.get("id")
            if not workspace_id:
                continue
            items_payload = await _get_json(
                session, f"{FABRIC_API}/workspaces/{workspace_id}/items", fabric_headers(fabric_token)
            )
            if "errors" in items_payload:
                errors.extend(items_payload["errors"])
                continue
            for item in items_payload.get("value", []):
                item_type = str(item.get("type") or item.get("itemType") or "")
                mapped_type = _map_fabric_item_type(item_type)
                if not mapped_type:
                    continue
                if source_type and mapped_type != source_type:
                    continue
                name = item.get("displayName") or item.get("name") or ""
                workspace_name = workspace.get("displayName") or workspace.get("name") or ""
                type_label = mapped_type.replace("_", " ")
                score = _score(search_lower, name, workspace_name, item_type, type_label)
                if search_lower and score == 0:
                    continue
                results.append(
                    {
                        "source_type": mapped_type,
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "item_id": item.get("id"),
                        "item_name": name,
                        "fabric_type": item_type,
                        "description": item.get("description"),
                        "score": score,
                    }
                )
    results.sort(key=lambda i: i.get("score", 0), reverse=True)
    return {"items": results, "errors": errors}


async def semantic_model_metadata(powerbi_token: str, workspace_id: str, model_id: str, fabric_token: str | None = None) -> dict[str, Any]:
    dataset_url = f"{POWERBI_API}/groups/{workspace_id}/datasets/{model_id}"
    tables_url = f"{dataset_url}/tables"
    async with aiohttp.ClientSession() as session:
        metadata = {
            "dataset": await _get_json(session, dataset_url, powerbi_headers(powerbi_token)),
            "tables": await _get_json(session, tables_url, powerbi_headers(powerbi_token)),
        }
        tables_errors = metadata.get("tables", {}).get("errors") or []
        if any("not Push API dataset" in json.dumps(error) for error in tables_errors):
            metadata["tables"] = await _semantic_model_tables_from_dax(session, f"{POWERBI_API}/datasets/{model_id}", powerbi_token)
        if fabric_token and metadata.get("tables", {}).get("errors"):
            definition_tables = await _semantic_model_tables_from_fabric_definition(session, workspace_id, model_id, fabric_token)
            if definition_tables.get("value") or definition_tables.get("errors"):
                metadata["tables"] = definition_tables
        return metadata


async def execute_readonly_dax(powerbi_token: str, workspace_id: str, model_id: str, query: str) -> dict[str, Any]:
    if not _is_readonly_dax(query):
        return {"errors": [{"message": "Only read-only DAX queries that start with EVALUATE are allowed."}], "query": query}
    dataset_url = f"{POWERBI_API}/datasets/{model_id}"
    async with aiohttp.ClientSession() as session:
        payload = await _execute_dax(session, dataset_url, powerbi_token, query)
    if payload.get("errors"):
        return {"errors": payload["errors"], "query": query}
    return {"query": query, "rows": _query_rows(payload), "raw": payload}


def _is_readonly_dax(query: str) -> bool:
    normalized = re.sub(r"--.*?$|/\*.*?\*/", " ", query, flags=re.MULTILINE | re.DOTALL).strip().lower()
    if not normalized.startswith("evaluate"):
        return False
    forbidden = {"create", "alter", "delete", "insert", "update", "drop", "clear", "refresh", "process"}
    return not any(re.search(rf"\b{keyword}\b", normalized) for keyword in forbidden)


async def _semantic_model_tables_from_dax(session: aiohttp.ClientSession, dataset_url: str, powerbi_token: str) -> dict[str, Any]:
    tables_payload = await _execute_dax(session, dataset_url, powerbi_token, "EVALUATE INFO.TABLES()")
    columns_payload = await _execute_dax(session, dataset_url, powerbi_token, "EVALUATE INFO.COLUMNS()")
    measures_payload = await _execute_dax(session, dataset_url, powerbi_token, "EVALUATE INFO.MEASURES()")
    errors = []
    for payload in [tables_payload, columns_payload, measures_payload]:
        errors.extend(payload.get("errors", []))
    if errors:
        return {"value": [], "errors": errors, "source": "executeQueries"}

    table_rows = _query_rows(tables_payload)
    column_rows = _query_rows(columns_payload)
    measure_rows = _query_rows(measures_payload)
    table_by_id: dict[str, dict[str, Any]] = {}
    table_by_name: dict[str, dict[str, Any]] = {}
    for row in table_rows:
        table_id = str(_row_value(row, "ID", "TableID") or "")
        name = _row_value(row, "Name", "ExplicitName")
        if not name:
            continue
        table = {
            "id": table_id,
            "name": name,
            "description": _row_value(row, "Description") or "",
            "isHidden": _row_value(row, "IsHidden") or False,
            "columns": [],
            "measures": [],
        }
        if table_id:
            table_by_id[table_id] = table
        table_by_name[str(name)] = table

    for row in column_rows:
        table = _table_for_row(row, table_by_id, table_by_name)
        if table is None:
            continue
        name = _row_value(row, "ExplicitName", "Name")
        if name:
            table["columns"].append({"name": name, "dataType": _row_value(row, "InferredDataType", "DataType"), "isHidden": _row_value(row, "IsHidden") or False})

    for row in measure_rows:
        table = _table_for_row(row, table_by_id, table_by_name)
        if table is None:
            continue
        name = _row_value(row, "Name", "ExplicitName")
        if name:
            table["measures"].append({"name": name, "expression": _row_value(row, "Expression"), "isHidden": _row_value(row, "IsHidden") or False})

    return {"value": list(table_by_name.values()), "source": "executeQueries"}


async def _execute_dax(session: aiohttp.ClientSession, dataset_url: str, powerbi_token: str, query: str) -> dict[str, Any]:
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
    async with session.post(f"{dataset_url}/executeQueries", headers=powerbi_headers(powerbi_token), json=body) as response:
        text = await response.text()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        if response.status >= 400:
            return {"errors": [{"status": response.status, "url": f"{dataset_url}/executeQueries", "message": payload, "query": query}]}
        return payload


async def _semantic_model_tables_from_fabric_definition(
    session: aiohttp.ClientSession,
    workspace_id: str,
    model_id: str,
    fabric_token: str,
) -> dict[str, Any]:
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{model_id}/getDefinition?format=TMDL"
    payload = await _post_json(session, url, fabric_headers(fabric_token), {})
    if "errors" in payload:
        return {"value": [], "errors": payload["errors"], "source": "fabricDefinition"}
    parts = payload.get("definition", {}).get("parts") or payload.get("parts") or []
    tables: list[dict[str, Any]] = []
    for part in parts:
        path = str(part.get("path") or "")
        if path and not path.endswith(".tmdl"):
            continue
        text = _decode_definition_payload(part)
        if not text:
            continue
        tables.extend(_tables_from_tmdl(text))
    return {"value": _merge_tables(tables), "source": "fabricDefinition"}


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
    last_payload: dict[str, Any] = {}
    for _ in range(FABRIC_OPERATION_POLL_LIMIT):
        async with session.get(operation_url, headers=headers) as response:
            text = await response.text()
            try:
                payload = json.loads(text) if text else {}
            except json.JSONDecodeError:
                payload = {"raw": text}
            if response.status >= 400:
                return {"errors": [{"status": response.status, "url": operation_url, "message": payload}]}
            last_payload = payload
            if payload.get("definition") or payload.get("parts"):
                return payload
            status = str(payload.get("status") or payload.get("operationStatus") or "").lower()
            if status in {"succeeded", "completed"}:
                result_url = response.headers.get("Location") or response.headers.get("Resource-Location") or payload.get("resultUrl")
                if result_url and _fabric_url(result_url) != operation_url:
                    result_payload = await _get_json(session, _fabric_url(result_url), headers)
                    if result_payload.get("definition") or result_payload.get("parts") or result_payload.get("errors"):
                        return result_payload
                return await _get_json(session, operation_url.rstrip("/") + "/result", headers)
            if status in {"failed", "cancelled", "canceled"}:
                return {"errors": [{"status": 202, "url": operation_url, "message": payload}]}
        await asyncio.sleep(1)
    return {"errors": [{"status": 202, "url": operation_url, "message": {"error": "Fabric getDefinition operation did not finish in time.", "last_response": last_payload}}]}


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
    current: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("table "):
            name = _clean_tmdl_name(line.removeprefix("table "))
            current = {"name": name, "columns": [], "measures": []}
            tables.append(current)
        elif current is not None and line.startswith("column "):
            name = _clean_tmdl_name(line.removeprefix("column "))
            current["columns"].append({"name": name})
        elif current is not None and line.startswith("measure "):
            name = _clean_tmdl_name(line.removeprefix("measure "))
            current["measures"].append({"name": name})
    return tables


def _clean_tmdl_name(value: str) -> str:
    name = re.split(r"\s*=\s*|\s*:\s*", value, maxsplit=1)[0].strip()
    return name.strip("'").strip('"')


def _merge_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for table in tables:
        name = table.get("name")
        if not name:
            continue
        target = merged.setdefault(name, {"name": name, "columns": [], "measures": []})
        target["columns"].extend(table.get("columns", []))
        target["measures"].extend(table.get("measures", []))
    return list(merged.values())


def _query_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return payload.get("results", [])[0].get("tables", [])[0].get("rows", [])
    except (IndexError, AttributeError):
        return []


def _row_value(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        for key, value in row.items():
            if key.split("[")[-1].rstrip("]").lower() == name.lower():
                return value
    return None


def _table_for_row(row: dict[str, Any], table_by_id: dict[str, dict[str, Any]], table_by_name: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    table_id = _row_value(row, "TableID")
    if table_id is not None and str(table_id) in table_by_id:
        return table_by_id[str(table_id)]
    table_name = _row_value(row, "Table", "TableName")
    if table_name is not None:
        return table_by_name.get(str(table_name))
    return None


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


def _score(search_text: str, model_name: str, workspace_name: str, *extra_fields: str) -> int:
    if not search_text:
        return 1
    combined = " ".join([model_name, workspace_name, *extra_fields]).lower()
    terms = [term for term in search_text.replace("_", " ").replace("-", " ").split() if len(term) > 2]
    return sum(10 for term in terms if term in combined)
