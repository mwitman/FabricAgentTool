"""Custom UX Backend — FastAPI server with SSO that proxies requests to Foundry Hosted Agents.

Exposes:
    POST /api/chat          — SSE stream (proxied from Foundry Hosted Agents)
    DELETE /api/chat/{id}   — delete a conversation thread
    GET  /api/health        — healthcheck
    GET  /                  — serves the built React frontend (static files)

Authentication:
    The frontend acquires a Graph-scoped token (User.Read) for identity and sends
    it as ``Authorization: Bearer <token>``. Fabric and Power BI tokens are passed
    in the request body so the hosted agent can call Fabric APIs as the signed-in
    user. The backend uses its own service principal credential to call Foundry.

Configuration:
    FOUNDRY_PROJECT_ENDPOINT — Foundry project endpoint ending in /api/projects/<project>.
"""

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import uvicorn
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Path & env setup
# ---------------------------------------------------------------------------
_backend_dir = Path(__file__).resolve().parent
_custom_ux_dir = _backend_dir.parent
load_dotenv(_custom_ux_dir / ".env")

# ---------------------------------------------------------------------------
# Mem0 memory layer (optional — stays in the UX layer for cross-session recall)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(_backend_dir))
from memory import create_memory  # noqa: E402

import logging as _logging
_mem0_logger = _logging.getLogger("mem0")
try:
    memory = create_memory()
    _mem0_logger.info("Mem0 memory layer initialised")
except Exception as _e:
    _mem0_logger.warning("Mem0 unavailable — running without memory: %s", _e)
    memory = None

# ---------------------------------------------------------------------------
# Foundry Hosted Agent routing
# ---------------------------------------------------------------------------
FOUNDRY_PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
FOUNDRY_API_VERSION = os.environ.get("FOUNDRY_API_VERSION", "v1")
FOUNDRY_FEATURES = os.environ.get("FOUNDRY_FEATURES", "HostedAgents=V1Preview")

_azure_credential: DefaultAzureCredential | None = None
_conversation_history: dict[str, list[dict[str, str]]] = {}
_history_turn_limit = 8
_conversation_store = None
_local_history_file = _backend_dir / "data" / "conversation_history.json"


# ---------------------------------------------------------------------------
# Permissions store (Cosmos) — determines which agents a user can see
# ---------------------------------------------------------------------------
_permissions_store = None


def _cosmos_credential():
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id, client_id, client_secret)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _perms():
    """Lazily create a Cosmos-backed permissions store."""
    global _permissions_store
    if _permissions_store is not None:
        return _permissions_store

    from azure.cosmos import CosmosClient
    endpoint = os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        return None
    database_name = os.environ.get("AGENT_MGMT_PERMISSIONS_DATABASE", "permissions")
    container_name = os.environ.get("AGENT_MGMT_PERMISSIONS_CONTAINER", "roles")
    try:
        client = CosmosClient(endpoint, credential=_cosmos_credential())
        db = client.get_database_client(database_name)
        container = db.get_container_client(container_name)
        container.read()  # verify access
        roles = list(container.query_items(query="SELECT * FROM c WHERE c.type = 'role'", enable_cross_partition_query=True))
        _ensure_configured_admin_role(container, roles)
        _permissions_store = container
        return _permissions_store
    except Exception as exc:
        import logging
        logging.getLogger("custom_ux").warning("Permissions store unavailable: %s", exc)
        return None


def _configured_bootstrap_admin_ids() -> set[str]:
    raw = os.environ.get("AGENT_MGMT_BOOTSTRAP_ADMIN_OBJECT_IDS", "")
    return {item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()}


def _configured_bootstrap_admin_members() -> list[dict[str, str]]:
    raw = os.environ.get("AGENT_MGMT_BOOTSTRAP_ADMIN_OBJECT_IDS", "")
    members: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw.replace(";", ",").split(","):
        object_id = item.strip()
        if not object_id or object_id.lower() in seen:
            continue
        seen.add(object_id.lower())
        members.append({"object_id": object_id, "display_name": "Bootstrap admin", "member_type": "user"})
    return members


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


def _ensure_bootstrap_admin_role(container, roles: list[dict[str, Any]], user_object_id: str, group_ids: list[str]) -> list[dict[str, Any]]:
    roles = _ensure_configured_admin_role(container, roles)
    match = _bootstrap_admin_match(user_object_id, group_ids)
    if match is None:
        return roles

    object_id, member_type = match
    admin_index = next((index for index, role in enumerate(roles) if role.get("name", "").strip().lower() in {"admin", "admins"}), None)
    member = {"object_id": object_id, "display_name": "Bootstrap admin", "member_type": member_type}
    now = datetime.now(timezone.utc).isoformat()
    partition_field = os.environ.get("AGENT_MGMT_PERMISSIONS_PARTITION_KEY", "/roleid").strip("/") or "roleid"

    if admin_index is None:
        role = {
            "id": str(uuid.uuid4()),
            "type": "role",
            "name": "Admin",
            "description": "Bootstrap administrator role.",
            "members": [member],
            "created_at": now,
            "updated_at": now,
        }
        role[partition_field] = role["id"]
        container.upsert_item(role)
        return [*roles, role]

    role = dict(roles[admin_index])
    members = list(role.get("members", []))
    if any(existing.get("object_id", "").lower() == object_id.lower() for existing in members):
        return roles
    members.append(member)
    role["members"] = members
    role["updated_at"] = now
    role[partition_field] = role["id"]
    container.upsert_item(role)
    updated_roles = [*roles]
    updated_roles[admin_index] = role
    return updated_roles


def _ensure_configured_admin_role(container, roles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    configured_members = _configured_bootstrap_admin_members()
    if not configured_members:
        return roles

    admin_index = next((index for index, role in enumerate(roles) if role.get("name", "").strip().lower() in {"admin", "admins"}), None)
    now = datetime.now(timezone.utc).isoformat()
    partition_field = os.environ.get("AGENT_MGMT_PERMISSIONS_PARTITION_KEY", "/roleid").strip("/") or "roleid"

    if admin_index is None:
        role = {
            "id": str(uuid.uuid4()),
            "type": "role",
            "name": "Admin",
            "description": "Bootstrap administrator role.",
            "members": configured_members,
            "created_at": now,
            "updated_at": now,
        }
        role[partition_field] = role["id"]
        container.upsert_item(role)
        return [*roles, role]

    role = dict(roles[admin_index])
    members = list(role.get("members", []))
    existing_ids = {member.get("object_id", "").lower() for member in members}
    changed = False
    for member in configured_members:
        if member["object_id"].lower() not in existing_ids:
            members.append(member)
            existing_ids.add(member["object_id"].lower())
            changed = True
    if not changed:
        return roles

    role["members"] = members
    role["updated_at"] = now
    role[partition_field] = role["id"]
    container.upsert_item(role)
    updated_roles = [*roles]
    updated_roles[admin_index] = role
    return updated_roles


def _get_agents_for_user(user_object_id: str, group_ids: list[str]) -> list[dict[str, Any]]:
    """Query Cosmos for agent bindings accessible by this user."""
    container = _perms()
    if container is None:
        return []

    # Load all roles
    roles = list(container.query_items(
        query="SELECT * FROM c WHERE c.type = 'role'",
        enable_cross_partition_query=True,
    ))
    roles = _ensure_bootstrap_admin_role(container, roles, user_object_id, group_ids)

    all_member_ids = {user_object_id} | set(group_ids)
    user_role_ids: set[str] = set()
    is_admin = False
    for role in roles:
        for member in role.get("members", []):
            if member.get("object_id") in all_member_ids:
                user_role_ids.add(role["id"])
                if role.get("name", "").strip().lower() in {"admin", "admins"}:
                    is_admin = True
                break

    # Load all agent bindings
    bindings = list(container.query_items(
        query="SELECT * FROM c WHERE c.type = 'agent_role_binding'",
        enable_cross_partition_query=True,
    ))

    if is_admin:
        return bindings
    if not user_role_ids:
        return []

    return [b for b in bindings if set(b.get("role_ids", [])) & user_role_ids]


def _get_all_agent_bindings() -> list[dict[str, Any]]:
    """Return all agent bindings (no role filtering)."""
    container = _perms()
    if container is None:
        return []
    return list(container.query_items(
        query="SELECT * FROM c WHERE c.type = 'agent_role_binding'",
        enable_cross_partition_query=True,
    ))


async def _resolve_user_from_graph_token(request: Request) -> dict[str, str] | JSONResponse:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "Bearer token required."}, status_code=401)
    user_token = auth_header[len("Bearer "):]

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://graph.microsoft.com/v1.0/me?$select=id,displayName,userPrincipalName,mail",
            headers={"Authorization": f"Bearer {user_token}"},
        ) as resp:
            if resp.status != 200:
                return JSONResponse({"error": "Failed to resolve user identity from Graph."}, status_code=401)
            me = await resp.json()

    user_id = me.get("id", "")
    if not user_id:
        return JSONResponse({"error": "Could not determine user object ID."}, status_code=401)

    return {
        "id": user_id,
        "display_name": me.get("displayName") or me.get("userPrincipalName") or "User",
        "user_principal_name": me.get("userPrincipalName") or me.get("mail") or "",
    }


def _conversation_history_key(user_id: str, conversation_id: str) -> str:
    return f"{user_id}:{conversation_id}"


def _history_partition_key_path() -> str:
    value = os.environ.get("CUSTOM_UX_HISTORY_PARTITION_KEY", "/userid").strip()
    if not value:
        return "/userid"
    return value if value.startswith("/") else f"/{value}"


def _history_partition_field() -> str:
    return _history_partition_key_path().strip("/") or "userid"


def _sanitize_messages(messages: Any) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        return []
    sanitized: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        sanitized.append({"role": role, "content": content})
    return sanitized


def _sanitize_conversation(user_id: str, raw: dict[str, Any], conversation_id: str | None = None) -> dict[str, Any]:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    conv_id = str(conversation_id or raw.get("id") or uuid.uuid4())
    created_at = raw.get("createdAt") if isinstance(raw.get("createdAt"), (int, float)) else now_ms
    updated_at = raw.get("updatedAt") if isinstance(raw.get("updatedAt"), (int, float)) else now_ms
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name").strip() else "New Chat"
    doc = {
        "id": conv_id,
        "type": "user_conversation",
        "userid": user_id,
        "name": name.strip(),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "agent": str(raw.get("agent") or ""),
        "messages": _sanitize_messages(raw.get("messages")),
    }
    doc[_history_partition_field()] = user_id
    return doc


def _conversation_response(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": doc.get("id", ""),
        "name": doc.get("name") or "New Chat",
        "createdAt": doc.get("createdAt") or doc.get("updatedAt") or 0,
        "updatedAt": doc.get("updatedAt") or doc.get("createdAt") or 0,
        "agent": doc.get("agent") or "",
        "messages": _sanitize_messages(doc.get("messages")),
    }


def _history_container():
    global _conversation_store
    if _conversation_store is not None:
        return _conversation_store

    endpoint = os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "").strip()
    if not endpoint:
        return None

    try:
        from azure.cosmos import CosmosClient, PartitionKey

        client = CosmosClient(endpoint, credential=_cosmos_credential())
        database_name = os.environ.get("CUSTOM_UX_HISTORY_DATABASE", "customux")
        container_name = os.environ.get("CUSTOM_UX_HISTORY_CONTAINER", "conversationhistory")
        partition_key_path = _history_partition_key_path()
        db = client.create_database_if_not_exists(database_name)
        _conversation_store = db.create_container_if_not_exists(
            id=container_name,
            partition_key=PartitionKey(path=partition_key_path),
        )
        return _conversation_store
    except Exception as exc:
        import logging
        logging.getLogger("custom_ux").warning("Conversation history store unavailable: %s", exc)
        return None


def _read_local_history() -> dict[str, list[dict[str, Any]]]:
    if not _local_history_file.exists():
        return {}
    try:
        with _local_history_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_local_history(data: dict[str, list[dict[str, Any]]]) -> None:
    _local_history_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = _local_history_file.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as handle:
        json.dump(data, handle)
    temp_file.replace(_local_history_file)


def _list_user_conversations(user_id: str) -> list[dict[str, Any]]:
    container = _history_container()
    if container is not None:
        docs = list(container.query_items(
            query="SELECT * FROM c WHERE c.userid = @userid AND c.type = 'user_conversation'",
            parameters=[{"name": "@userid", "value": user_id}],
            partition_key=user_id,
        ))
        return sorted((_conversation_response(doc) for doc in docs), key=lambda doc: doc.get("updatedAt") or 0, reverse=True)

    data = _read_local_history()
    return sorted((_conversation_response(doc) for doc in data.get(user_id, [])), key=lambda doc: doc.get("updatedAt") or 0, reverse=True)


def _replace_user_conversations(user_id: str, conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = [_sanitize_conversation(user_id, conversation) for conversation in conversations if isinstance(conversation, dict)]
    container = _history_container()
    if container is not None:
        existing = list(container.query_items(
            query="SELECT c.id FROM c WHERE c.userid = @userid AND c.type = 'user_conversation'",
            parameters=[{"name": "@userid", "value": user_id}],
            partition_key=user_id,
        ))
        next_ids = {conversation["id"] for conversation in sanitized}
        for doc in existing:
            doc_id = doc.get("id")
            if doc_id and doc_id not in next_ids:
                container.delete_item(item=doc_id, partition_key=user_id)
        for conversation in sanitized:
            container.upsert_item(conversation)
        return sorted((_conversation_response(doc) for doc in sanitized), key=lambda doc: doc.get("updatedAt") or 0, reverse=True)

    data = _read_local_history()
    data[user_id] = sanitized
    _write_local_history(data)
    return sorted((_conversation_response(doc) for doc in sanitized), key=lambda doc: doc.get("updatedAt") or 0, reverse=True)


def _delete_user_conversation(user_id: str, conversation_id: str) -> None:
    container = _history_container()
    if container is not None:
        try:
            container.delete_item(item=conversation_id, partition_key=user_id)
        except Exception:
            pass
    else:
        data = _read_local_history()
        data[user_id] = [doc for doc in data.get(user_id, []) if doc.get("id") != conversation_id]
        _write_local_history(data)

    _conversation_history.pop(_conversation_history_key(user_id, conversation_id), None)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Fabric GraphQL Agents - Custom UX")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------
def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _normalize_agent(agent: str) -> str:
    value = (agent or "orchestrator").strip().lower()
    if value in _agent_names():
        return value
    return AGENT_ALIASES.get(value, "orchestrator")


# ---------------------------------------------------------------------------
# Agent resolution — dynamic from Cosmos bindings
# ---------------------------------------------------------------------------
def _normalize_agent(agent: str) -> str:
    """Return the agent_name as-is (it's now a Foundry agent name from Cosmos)."""
    return (agent or "").strip()


def _foundry_responses_url(agent_name: str) -> str:
    if not FOUNDRY_PROJECT_ENDPOINT:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is required.")
    return (
        f"{FOUNDRY_PROJECT_ENDPOINT}/agents/{agent_name}"
        f"/endpoint/protocols/openai/responses?api-version={FOUNDRY_API_VERSION}"
    )


def _format_history_context(user_id: str, conversation_id: str) -> str:
    history = _conversation_history.get(_conversation_history_key(user_id, conversation_id), [])[-(_history_turn_limit * 2):]
    if not history:
        return ""

    lines = []
    for message in history:
        role = message.get("role", "message").title()
        content = message.get("content", "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _append_history(user_id: str, conversation_id: str, user_message: str, assistant_message: str) -> None:
    history = _conversation_history.setdefault(_conversation_history_key(user_id, conversation_id), [])
    history.extend(
        [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]
    )
    max_messages = _history_turn_limit * 2
    if len(history) > max_messages:
        del history[:-max_messages]


def _azure_ai_access_token() -> str:
    static_token = os.environ.get("FOUNDRY_ACCESS_TOKEN", "").strip()
    if static_token:
        return static_token.removeprefix("Bearer ").strip()

    global _azure_credential
    if _azure_credential is None:
        tenant_id = os.environ.get("AZURE_TENANT_ID")
        client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
        client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
        if tenant_id and client_id and client_secret:
            _azure_credential = ClientSecretCredential(tenant_id, client_id, client_secret)
        else:
            _azure_credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return _azure_credential.get_token("https://ai.azure.com/.default").token


def _parse_sse_block(block: str) -> tuple[str | None, dict | str | None]:
    event_type: str | None = None
    data_lines: list[str] = []

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())

    if not data_lines:
        return event_type, None

    data = "\n".join(data_lines)
    if data == "[DONE]":
        return event_type or "done", "[DONE]"

    try:
        return event_type, json.loads(data)
    except json.JSONDecodeError:
        return event_type, data


def _foundry_event_to_ux(event_type: str | None, payload: dict | str | None) -> dict | None:
    if payload == "[DONE]":
        return None
    if not isinstance(payload, dict):
        return None

    payload_type = payload.get("type") or event_type
    if payload_type == "response.output_text.delta":
        return {"type": "text", "content": payload.get("delta", "")}
    if payload_type == "response.failed":
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        return {"type": "error", "content": error.get("message") or "Foundry hosted agent failed."}
    if payload_type == "error":
        return {"type": "error", "content": payload.get("message") or payload.get("error") or "Foundry error."}

    return None


async def _proxy_foundry_agent(
    fabric_token: str,
    powerbi_token: str | None,
    enriched_message: str,
    conversation_id: str,
    agent: str,
    full_response: list[str],
):
    token = _azure_ai_access_token()
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _foundry_responses_url(agent),
            headers={
                "Authorization": f"Bearer {token}",
                "Foundry-Features": FOUNDRY_FEATURES,
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={
                "input": enriched_message,
                "conversation_id": conversation_id,
                "stream": True,
                "fabric_token": fabric_token,
                "powerbi_token": powerbi_token,
            },
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                yield _sse({"type": "error", "content": f"Foundry agent error ({resp.status}): {error_text}"})
                return

            buffer = ""
            async for chunk in resp.content.iter_any():
                buffer += chunk.decode("utf-8")
                blocks = buffer.split("\n\n")
                buffer = blocks.pop()

                for block in blocks:
                    event_type, payload = _parse_sse_block(block)
                    ux_event = _foundry_event_to_ux(event_type, payload)
                    if not ux_event:
                        continue
                    if ux_event.get("type") == "text":
                        full_response.append(ux_event.get("content", ""))
                    yield _sse(ux_event)


# ---------------------------------------------------------------------------
# POST /api/chat — SSE streaming (proxied to Foundry Hosted Agents)
# ---------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(request: Request):
    body = await request.json()
    message: str = body.get("message", "")
    conversation_id: str = body.get("conversation_id") or str(uuid.uuid4())
    user_id: str = body.get("user_id") or "default_user"
    agent: str = _normalize_agent(body.get("agent", ""))
    fabric_token: str = body.get("fabric_token") or body.get("fabricToken") or ""
    powerbi_token: str | None = body.get("powerbi_token") or body.get("powerbiToken")

    if not fabric_token:
        return JSONResponse({"error": "fabric_token is required in the request body."}, status_code=400)

    if not message.strip():
        return JSONResponse({"error": "message is required"}, status_code=400)
    if not agent:
        return JSONResponse({"error": "agent is required (pass the agent_name from Cosmos)"}, status_code=400)

    # --- Retrieve conversation and long-term memory context ---
    enriched_message = message
    context_blocks: list[str] = []

    history_context = _format_history_context(user_id, conversation_id)
    if history_context:
        context_blocks.append(f"[Conversation so far]\n{history_context}")

    if memory:
        try:
            results = memory.search(query=message, filters={"user_id": user_id}, limit=5)
            memories_list = results.get("results", []) if isinstance(results, dict) else results
            if memories_list:
                mem_lines = "\n".join(f"- {m['memory']}" for m in memories_list)
                context_blocks.append(f"[Relevant context from previous conversations]\n{mem_lines}")
        except Exception as exc:
            _mem0_logger.warning("Mem0 search failed: %s", exc)

    if context_blocks:
        enriched_message = f"{'\n\n'.join(context_blocks)}\n\n[Current user message]\n{message}"

    # --- Proxy to Foundry Hosted Agent (SSE streaming) ---
    async def event_stream():
        yield _sse({"type": "meta", "conversation_id": conversation_id, "agent": agent})

        full_response: list[str] = []
        try:
            async for event in _proxy_foundry_agent(
                fabric_token=fabric_token,
                powerbi_token=powerbi_token,
                enriched_message=enriched_message,
                conversation_id=conversation_id,
                agent=agent,
                full_response=full_response,
            ):
                yield event

        except Exception as exc:
            yield _sse({"type": "error", "content": str(exc)})

        yield _sse({"type": "done"})

        # --- Mem0: store the exchange for future recall ---
        if memory and full_response:
            try:
                assistant_message = "".join(full_response)
                exchange = [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": assistant_message},
                ]
                memory.add(exchange, user_id=user_id)
            except Exception as exc:
                _mem0_logger.warning("Mem0 add failed: %s", exc)

        if full_response:
            _append_history(user_id, conversation_id, message, "".join(full_response))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# DELETE /api/chat/{conversation_id}
# ---------------------------------------------------------------------------
@app.delete("/api/chat/{conversation_id}")
async def delete_conversation(conversation_id: str):
    stale_keys = [key for key in _conversation_history if key.endswith(f":{conversation_id}")]
    for key in stale_keys:
        _conversation_history.pop(key, None)
    return {"status": "deleted", "conversation_id": conversation_id}


@app.get("/api/conversations")
async def list_conversations(request: Request):
    user = await _resolve_user_from_graph_token(request)
    if isinstance(user, JSONResponse):
        return user
    return {"user": user, "conversations": _list_user_conversations(user["id"])}


@app.put("/api/conversations")
async def save_conversations(request: Request):
    user = await _resolve_user_from_graph_token(request)
    if isinstance(user, JSONResponse):
        return user
    body = await request.json()
    conversations = body.get("conversations", [])
    if not isinstance(conversations, list):
        return JSONResponse({"error": "conversations must be a list."}, status_code=400)
    saved = _replace_user_conversations(user["id"], conversations)
    return {"user": user, "conversations": saved}


@app.delete("/api/conversations/{conversation_id}")
async def delete_saved_conversation(conversation_id: str, request: Request):
    user = await _resolve_user_from_graph_token(request)
    if isinstance(user, JSONResponse):
        return user
    _delete_user_conversation(user["id"], conversation_id)
    return {"status": "deleted", "conversation_id": conversation_id}


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "custom-ux",
        "foundry_project_endpoint": FOUNDRY_PROJECT_ENDPOINT,
        "permissions_store": "connected" if _perms() is not None else "unavailable",
    }


@app.get("/api/agents")
async def agents_list():
    """Return all agent bindings (no user filtering). Used for admin views."""
    bindings = _get_all_agent_bindings()
    return {"agents": [
        {
            "key": b.get("agent_name", ""),
            "label": b.get("project_display_name") or b.get("agent_name", ""),
            "agent_name": b.get("agent_name", ""),
            "icon": "bot",
        }
        for b in bindings if b.get("agent_name")
    ]}


@app.post("/api/agents-for-user")
async def agents_for_user(request: Request):
    """Return agent options filtered by the user's roles in Cosmos."""
    body = await request.json()
    user_object_id = body.get("user_object_id", "")
    group_ids = body.get("group_ids", [])
    if not user_object_id:
        return JSONResponse({"error": "user_object_id is required."}, status_code=400)
    bindings = _get_agents_for_user(user_object_id, group_ids)
    return {"agents": [
        {
            "key": b.get("agent_name", ""),
            "label": b.get("project_display_name") or b.get("agent_name", ""),
            "agent_name": b.get("agent_name", ""),
            "icon": "bot",
        }
        for b in bindings if b.get("agent_name")
    ]}


@app.get("/api/my-agents")
async def my_agents(request: Request):
    """Return agents the authenticated user can access, based on their roles.

    Reads the user's OID from the Authorization token (via MS Graph /me),
    resolves their group memberships, then filters agent bindings by role.
    """
    user = await _resolve_user_from_graph_token(request)
    if isinstance(user, JSONResponse):
        return user
    user_object_id = user["id"]

    # Resolve group memberships using service principal (transitiveMemberOf)
    group_ids: list[str] = []
    try:
        cred = _cosmos_credential()
        sp_token = cred.get_token("https://graph.microsoft.com/.default").token
        headers = {"Authorization": f"Bearer {sp_token}", "ConsistencyLevel": "eventual"}
        url: str | None = (
            f"https://graph.microsoft.com/v1.0/users/{user_object_id}"
            f"/transitiveMemberOf/microsoft.graph.group?$select=id&$top=999"
        )
        async with aiohttp.ClientSession() as session:
            while url:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()
                    group_ids.extend(g["id"] for g in data.get("value", []) if g.get("id"))
                    url = data.get("@odata.nextLink")
    except Exception:
        pass  # Continue with just the user OID if group resolution fails

    bindings = _get_agents_for_user(user_object_id, group_ids)
    return {"agents": [
        {
            "key": b.get("agent_name", ""),
            "label": b.get("project_display_name") or b.get("agent_name", ""),
            "agent_name": b.get("agent_name", ""),
            "icon": "bot",
        }
        for b in bindings if b.get("agent_name")
    ]}


# ---------------------------------------------------------------------------
# Static file serving (built React frontend)
# ---------------------------------------------------------------------------
_static_dir = _backend_dir / "static"
if _static_dir.exists():
    @app.get("/")
    async def root():
        from fastapi.responses import FileResponse
        return FileResponse(_static_dir / "index.html")

    app.mount("/", StaticFiles(directory=str(_static_dir)), name="static")


# ---------------------------------------------------------------------------
# Run directly
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)
