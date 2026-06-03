"""Hosted Agent Runtime - agent-framework-powered version.

Reads project configuration from Cosmos DB at startup, builds a ChatAgent
with tools for the configured Fabric data sources, then serves the Foundry
responses protocol so it can be registered as a hosted agent.
"""

from __future__ import annotations

import json
import os
import re
import base64
import asyncio
import time
import uuid
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin

import aiohttp
import uvicorn
from azure.cosmos import CosmosClient
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

# Foundry's agent_framework only exports RawAgent (not ChatAgent)
from agent_framework import RawAgent as ChatAgent

# ai_function decorator — may be named differently across versions
try:
    from agent_framework import ai_function
except ImportError:
    from agent_framework import tool as ai_function

# Optional thread/content types
try:
    from agent_framework._threads import AgentThread
except ImportError:
    try:
        from agent_framework import AgentSession as AgentThread
    except ImportError:
        AgentThread = None  # type: ignore[assignment,misc]

try:
    from agent_framework._types import TextContent
except ImportError:
    try:
        from agent_framework import Content as TextContent
    except ImportError:
        TextContent = None  # type: ignore[assignment,misc]

# FoundryChatClient — may live in different submodules
try:
    from agent_framework.foundry import FoundryChatClient
except ImportError:
    from agent_framework import FoundryChatClient

load_dotenv()

app = FastAPI(title="Hosted Agent Runtime")

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_API_ROOT = "https://api.fabric.microsoft.com"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_OPERATION_POLL_LIMIT = 12
SEMANTIC_SOURCE_TYPE = "semantic_model"
FABRIC_MCP_SOURCE_TYPES = {"fabric_mcp", "sql_endpoint", "data_agent"}
NON_SEMANTIC_SOURCE_TYPES = {"fabric_mcp", "graphql", "sql_endpoint", "data_agent"}

_threads: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Credentials and project loading
# ---------------------------------------------------------------------------


def _credential():
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _get_chat_client(model_config: dict[str, str] | None = None) -> FoundryChatClient:
    """Create a FoundryChatClient using the service principal credential."""
    project_endpoint = (os.environ.get("MAF_FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")).rstrip("/")
    deployment_name = (model_config or {}).get("deployment_name") or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "")

    return FoundryChatClient(
        project_endpoint=project_endpoint or None,
        model=deployment_name or None,
        credential=_credential(),
    )


@lru_cache(maxsize=1)
def _project() -> dict[str, Any]:
    project_id = (os.environ.get("MAF_MGMT_PROJECT_ID") or os.environ.get("AGENT_MGMT_PROJECT_ID", "")).strip()
    if not project_id:
        raise RuntimeError("MAF_MGMT_PROJECT_ID is required.")
    endpoint = (os.environ.get("MAF_MGMT_COSMOS_ENDPOINT") or os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "")).strip()
    if not endpoint:
        raise RuntimeError("MAF_MGMT_COSMOS_ENDPOINT is required.")
    database_name = os.environ.get("MAF_MGMT_COSMOS_DATABASE") or os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents")
    container_name = os.environ.get("MAF_MGMT_COSMOS_CONTAINER") or os.environ.get("AGENT_MGMT_COSMOS_CONTAINER", "agentmetadata")
    client = CosmosClient(endpoint, credential=_credential())
    container = client.get_database_client(database_name).get_container_client(container_name)
    return container.read_item(item=project_id, partition_key=project_id)


# ---------------------------------------------------------------------------
# Agent creation from project config
# ---------------------------------------------------------------------------


def _build_instructions(project: dict[str, Any]) -> str:
    """Build the agent system prompt from saved project configuration."""
    mode = project.get("deployment_mode")
    configured_sources = _get_configured_data_sources(project)
    source_summary = _format_data_source_summary(configured_sources)
    if mode == "standalone":
        agent = project.get("standalone_agent", {})
        if agent.get("prompt"):
            return _with_runtime_tool_policy(agent["prompt"], configured_sources)
        if _uses_non_semantic_fabric(project):
            return (
                f"You are {agent.get('name', 'a Fabric agent')}. "
                f"{agent.get('description', '')} "
                f"Configured data sources: {source_summary}. "
                "Use the available Fabric tools for GraphQL APIs, SQL endpoints, Fabric Data Agents, and Fabric MCP. "
                "For SQL endpoints, Data Agents, and broad Fabric discovery, use the Fabric MCP tools."
            )
        model_name = _source_item_name(agent.get("semantic_model", {})) or "the configured semantic model"
        return (
            f"You are {agent.get('name', 'a Fabric semantic model agent')}. "
            f"{agent.get('description', '')} "
            f"You answer questions using {model_name}. "
            "Use the available tools to get metadata and execute read-only DAX queries."
        )
    if mode == "orchestrator_only":
        orch = project.get("orchestrator_only", {})
        if orch.get("prompt"):
            return _with_runtime_tool_policy(orch["prompt"], configured_sources)
        agent_summaries = [
            f"- {a.get('display_name') or a.get('agent_name', 'agent')}: {a.get('description', 'no description')}"
            for a in orch.get("external_agents", [])
        ]
        return (
            f"You are {orch.get('name', 'an orchestrator agent')}. "
            f"{orch.get('description', '')} "
            f"You delegate tasks to existing deployed agents:\n"
            + "\n".join(agent_summaries) + "\n"
            "Select the most appropriate agent based on the user's question and each agent's description. "
            "Use the invoke tool for each agent to ask it questions and return the combined results."
        )
    orchestrator = project.get("orchestrator", {})
    if orchestrator.get("prompt"):
        return _with_runtime_tool_policy(orchestrator["prompt"], configured_sources)
    subagent_names = [s.get("name", "subagent") for s in orchestrator.get("subagents", [])]
    return (
        f"You are {orchestrator.get('name', 'an orchestrator agent')}. "
        f"{orchestrator.get('description', '')} "
        f"You route questions to subagents: {', '.join(subagent_names)}. "
        f"Configured data sources: {source_summary}. "
        "Use the available Fabric tools for each configured source type. "
        "Use semantic model tools for DAX, GraphQL tools for Fabric GraphQL APIs, and Fabric MCP tools for SQL endpoints, Fabric Data Agents, and broad Fabric operations."
    )


def _with_runtime_tool_policy(prompt: str, configured_sources: list[dict[str, Any]]) -> str:
    if not configured_sources:
        return prompt
    data_agent_sources = [source for source in configured_sources if source.get("source_type") == "data_agent"]
    policy = [
        "Runtime tool-use policy:",
        f"Configured data sources: {_format_data_source_summary(configured_sources)}.",
        "Use the available Fabric tools for configured sources before deciding a request is out of scope.",
    ]
    if data_agent_sources:
        policy.extend([
            "For a configured Fabric Data Agent, treat broad analytical questions about that data source, including trends, summaries, and 'all my data' phrasing, as in scope for that Fabric Data Agent.",
            "Invoke invoke_fabric_data_agent with the user's question before refusing or narrowing the request.",
            "Pass the user's analytical question through to the Fabric Data Agent; do not reject it solely because it is broad.",
        ])
    return prompt.rstrip() + "\n\n" + "\n".join(policy)


def _get_configured_data_sources(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all configured data source bindings from the project config."""
    mode = project.get("deployment_mode")
    if mode == "standalone":
        source = project.get("standalone_agent", {}).get("semantic_model", {})
        return [_normalize_data_source(source)] if _has_configured_source(source) else []
    sources = []
    for subagent in project.get("orchestrator", {}).get("subagents", []):
        source = subagent.get("semantic_model", {})
        if _has_configured_source(source):
            normalized = _normalize_data_source(source)
            normalized["agent_name"] = subagent.get("name", "")
            sources.append(normalized)
    return sources


def _get_semantic_models(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all semantic model bindings from the project config."""
    return [source for source in _get_configured_data_sources(project) if source.get("source_type") == SEMANTIC_SOURCE_TYPE]


def _get_sources_by_type(project: dict[str, Any], source_type: str) -> list[dict[str, Any]]:
    return [source for source in _get_configured_data_sources(project) if source.get("source_type") == source_type]


def _has_configured_source(data_source: dict[str, Any]) -> bool:
    return bool(_source_item_id(data_source) or _source_type(data_source) == "fabric_mcp")


def _normalize_data_source(data_source: dict[str, Any]) -> dict[str, Any]:
    source_type = _source_type(data_source)
    item_id = _source_item_id(data_source)
    item_name = _source_item_name(data_source)
    return {
        "source_type": source_type,
        "workspace_id": data_source.get("workspace_id", ""),
        "workspace_name": data_source.get("workspace_name", ""),
        "item_id": item_id,
        "item_name": item_name,
        "semantic_model_id": item_id if source_type == SEMANTIC_SOURCE_TYPE else "",
        "semantic_model_name": item_name if source_type == SEMANTIC_SOURCE_TYPE else "",
    }


def _source_type(data_source: dict[str, Any]) -> str:
    if data_source.get("source_type"):
        return str(data_source["source_type"])
    if data_source.get("semantic_model_id"):
        return SEMANTIC_SOURCE_TYPE
    return SEMANTIC_SOURCE_TYPE


def _source_item_id(data_source: dict[str, Any]) -> str:
    return str(data_source.get("item_id") or data_source.get("semantic_model_id") or "")


def _source_item_name(data_source: dict[str, Any]) -> str:
    return str(data_source.get("item_name") or data_source.get("semantic_model_name") or "")


def _is_readonly_sql(query: str) -> bool:
    normalized = re.sub(r"--.*?$|/\*.*?\*/", " ", query, flags=re.MULTILINE | re.DOTALL).strip().lower()
    if not (normalized.startswith("select") or normalized.startswith("with")):
        return False
    forbidden = {
        "alter",
        "backup",
        "create",
        "delete",
        "drop",
        "execute",
        "exec",
        "insert",
        "merge",
        "restore",
        "truncate",
        "update",
    }
    return not any(re.search(rf"\b{keyword}\b", normalized) for keyword in forbidden)


def _format_data_source_summary(data_sources: list[dict[str, Any]]) -> str:
    if not data_sources:
        return "none"
    labels = []
    for source in data_sources:
        source_type = source.get("source_type", "unknown")
        name = source.get("item_name") or source.get("item_id") or source_type
        workspace = source.get("workspace_name") or source.get("workspace_id")
        labels.append(f"{name} ({source_type}{', ' + workspace if workspace else ''})")
    return "; ".join(labels)


def _is_fabric_mcp_source(data_source: dict[str, Any]) -> bool:
    return _source_type(data_source) == "fabric_mcp"


def _uses_non_semantic_fabric(project: dict[str, Any]) -> bool:
    return any(source.get("source_type") in NON_SEMANTIC_SOURCE_TYPES for source in _get_configured_data_sources(project))


def _uses_fabric_mcp(project: dict[str, Any]) -> bool:
    return any(source.get("source_type") in FABRIC_MCP_SOURCE_TYPES for source in _get_configured_data_sources(project))


def _fabric_mcp_endpoint() -> str:
    return (os.environ.get("FABRIC_CORE_MCP_ENDPOINT") or "https://api.fabric.microsoft.com/v1/mcp/core").strip()


async def _invoke_external_agent(agent_name: str, message: str, fabric_token: str, powerbi_token: str | None = None) -> dict[str, Any]:
    """Invoke an existing deployed hosted agent via the Foundry API."""
    endpoint = (os.environ.get("MAF_FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")).rstrip("/")
    if not endpoint:
        return {"error": "FOUNDRY_PROJECT_ENDPOINT is required."}
    api_version = os.environ.get("FOUNDRY_API_VERSION", "v1")
    features = os.environ.get("FOUNDRY_FEATURES", "HostedAgents=V1Preview")
    url = f"{endpoint}/agents/{agent_name}/endpoint/protocols/openai/responses?api-version={api_version}"
    body = {
        "input": message,
        "conversation_id": str(uuid.uuid4()),
        "stream": False,
        "fabric_token": fabric_token,
        "powerbi_token": powerbi_token,
    }
    # Use SP credential for Foundry API auth (not the user's Fabric token)
    ai_token = _access_token_str()
    headers = {
        "Authorization": f"Bearer {ai_token}",
        "Foundry-Features": features,
        "Content-Type": "application/json",
    }

    max_retries = 5
    retry_delay = 5  # seconds

    async with aiohttp.ClientSession() as session:
        for attempt in range(max_retries):
            async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=300)) as response:
                text = await response.text()
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = {"raw": text}

                # Retry on session_not_ready (agent container still spinning up)
                if response.status == 409 or (
                    isinstance(payload, dict) and "session_not_ready" in str(payload.get("error", ""))
                ):
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    return {"error": f"Agent {agent_name} session not ready after {max_retries} attempts", "detail": payload}

                if response.status >= 400:
                    return {"error": f"Agent {agent_name} returned status {response.status}", "detail": payload}
                # Extract output text from response
                if isinstance(payload, dict):
                    output = payload.get("output")
                    if isinstance(output, list):
                        texts = [item.get("content", [{}])[0].get("text", "") for item in output if item.get("type") == "message"]
                        return {"agent_name": agent_name, "response": "\n".join(texts) or str(payload)}
                    if isinstance(output, str):
                        return {"agent_name": agent_name, "response": output}
                return {"agent_name": agent_name, "response": str(payload)}

    return {"error": f"Agent {agent_name} failed after {max_retries} attempts"}


def _access_token_str() -> str:
    """Get an access token string for calling Foundry APIs."""
    credential = _credential()
    scope = os.environ.get("FOUNDRY_TOKEN_SCOPE", "https://ai.azure.com/.default")
    token = credential.get_token(scope)
    return token.token


def _create_agent(project: dict[str, Any], fabric_token: str, powerbi_token: str | None = None) -> ChatAgent:
    """Create a ChatAgent from project config with source-aware Fabric tools."""
    # Determine model_config based on deployment mode
    mode = project.get("deployment_mode")
    if mode == "standalone":
        model_config = project.get("standalone_agent", {}).get("model_config")
    elif mode == "orchestrator_only":
        model_config = project.get("orchestrator_only", {}).get("model_config")
    else:
        model_config = project.get("orchestrator", {}).get("model_config")

    client = _get_chat_client(model_config)
    instructions = _build_instructions(project)

    # For orchestrator_only mode, create tools that invoke external agents
    if mode == "orchestrator_only":
        external_agents = project.get("orchestrator_only", {}).get("external_agents", [])
        tools = []
        for ext_agent in external_agents:
            agent_name = ext_agent.get("agent_name", "")
            display_name = ext_agent.get("display_name", agent_name)
            description = ext_agent.get("description", "") or f"Invoke the deployed agent '{display_name}'"

            # Create a closure-based tool for each external agent
            def _make_invoke_tool(target_agent_name: str, target_display_name: str, target_description: str):
                @ai_function(
                    name=f"invoke_{re.sub(r'[^a-zA-Z0-9_]', '_', target_agent_name)}",
                    description=f"Invoke the '{target_display_name}' agent. {target_description}",
                )
                async def invoke_external_agent(message: str) -> str:
                    result = await _invoke_external_agent(target_agent_name, message, fabric_token, powerbi_token)
                    return json.dumps(result, indent=2)
                return invoke_external_agent

            if agent_name:
                tools.append(_make_invoke_tool(agent_name, display_name, description))

        return ChatAgent(
            client,
            instructions=instructions,
            name=project.get("name", "Orchestrator Agent"),
            description=project.get("description", "An orchestrator that delegates to existing agents."),
            tools=tools,
        )

    data_sources = _get_configured_data_sources(project)
    semantic_models = _get_semantic_models(project)
    graphql_sources = _get_sources_by_type(project, "graphql")
    sql_sources = _get_sources_by_type(project, "sql_endpoint")
    data_agent_sources = _get_sources_by_type(project, "data_agent")
    uses_fabric_mcp = _uses_fabric_mcp(project)
    effective_powerbi_token = powerbi_token or fabric_token

    @ai_function(
        name="list_configured_fabric_data_sources",
        description="List all Fabric data sources configured for this hosted agent project.",
    )
    async def list_configured_fabric_data_sources() -> str:
        if not data_sources:
            return json.dumps({"message": "No Fabric data sources configured."})
        return json.dumps(data_sources, indent=2)

    @ai_function(
        name="list_configured_semantic_models",
        description="List the semantic models configured for this agent project.",
    )
    async def list_configured_semantic_models() -> str:
        if not semantic_models:
            return json.dumps({"message": "No semantic models configured."})
        return json.dumps([
            {
                "workspace_id": sm.get("workspace_id"),
                "semantic_model_id": _source_item_id(sm),
                "semantic_model_name": _source_item_name(sm),
                "workspace_name": sm.get("workspace_name"),
            }
            for sm in semantic_models
        ], indent=2)

    async def _call_fabric_mcp_jsonrpc(method: str, params: dict[str, Any]) -> str:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        headers = {"Authorization": f"Bearer {fabric_token}", "Content-Type": "application/json", "Accept": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(_fabric_mcp_endpoint(), headers=headers, json=payload) as response:
                text = await response.text()
                if response.status >= 400:
                    return json.dumps({"status": response.status, "error": text})
                return text

    @ai_function(
        name="call_fabric_mcp",
        description=(
            "Call the Fabric MCP server. Use method 'tools/list' to discover available MCP tools, "
            "then call the appropriate MCP method with params."
        ),
    )
    async def call_fabric_mcp(method: str, params_json: str = "{}") -> str:
        if not uses_fabric_mcp:
            return json.dumps({"error": "Fabric MCP is not enabled for this project's configured data sources."})
        try:
            params = json.loads(params_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"params_json must be valid JSON: {exc}"})
        return await _call_fabric_mcp_jsonrpc(method, params)

    @ai_function(
        name="query_fabric_graphql",
        description=(
            "Execute a query against a configured Fabric GraphQL API. "
            "Use list_configured_fabric_data_sources first to find workspace_id and item_id."
        ),
    )
    async def query_fabric_graphql(workspace_id: str, graphql_api_id: str, query: str, variables_json: str = "{}") -> str:
        if not any(source.get("workspace_id") == workspace_id and source.get("item_id") == graphql_api_id for source in graphql_sources):
            return json.dumps({"error": "That GraphQL API is not configured for this agent project."})
        try:
            variables = json.loads(variables_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"variables_json must be valid JSON: {exc}"})
        if not isinstance(variables, dict):
            return json.dumps({"error": "variables_json must decode to a JSON object."})
        body = {"query": query, "variables": variables}
        url_template = os.environ.get(
            "FABRIC_GRAPHQL_QUERY_URL_TEMPLATE",
            FABRIC_API + "/workspaces/{workspace_id}/graphqlApis/{item_id}/graphql",
        )
        urls = [url_template.format(workspace_id=workspace_id, item_id=graphql_api_id)]
        fallback_url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{graphql_api_id}/graphql"
        if fallback_url not in urls:
            urls.append(fallback_url)
        async with aiohttp.ClientSession() as session:
            last_error: dict[str, Any] | None = None
            for url in urls:
                async with session.post(url, headers=_fabric_headers(fabric_token), json=body) as response:
                    text = await response.text()
                    try:
                        payload = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        payload = {"raw": text}
                    if response.status < 400:
                        return json.dumps(payload, indent=2)
                    last_error = {"status": response.status, "url": url, "message": payload}
                    if response.status not in {404, 405}:
                        break
            return json.dumps({"errors": [last_error] if last_error else [{"message": "GraphQL query failed."}]}, indent=2)

    @ai_function(
        name="call_fabric_mcp_tool",
        description=(
            "Call a Fabric MCP tool by name after discovering MCP tools with call_fabric_mcp('tools/list'). "
            "Use this for configured SQL endpoints, Fabric Data Agents, and broader Fabric operations."
        ),
    )
    async def call_fabric_mcp_tool(tool_name: str, arguments_json: str = "{}") -> str:
        if not uses_fabric_mcp:
            return json.dumps({"error": "Fabric MCP is not enabled for this project's configured data sources."})
        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"arguments_json must be valid JSON: {exc}"})
        if not isinstance(arguments, dict):
            return json.dumps({"error": "arguments_json must decode to a JSON object."})
        return await _call_fabric_mcp_jsonrpc("tools/call", {"name": tool_name, "arguments": arguments})

    @ai_function(
        name="execute_fabric_sql_query",
        description=(
            "Execute a guarded read-only SQL query against a configured Fabric SQL endpoint through Fabric MCP. "
            "Use list_configured_fabric_data_sources first to find workspace_id and item_id."
        ),
    )
    async def execute_fabric_sql_query(workspace_id: str, sql_endpoint_id: str, sql_query: str) -> str:
        if not any(source.get("workspace_id") == workspace_id and source.get("item_id") == sql_endpoint_id for source in sql_sources):
            return json.dumps({"error": "That SQL endpoint is not configured for this agent project."})
        if not _is_readonly_sql(sql_query):
            return json.dumps({"errors": [{"message": "Only read-only SQL queries starting with SELECT or WITH are allowed."}]})
        tool_name = os.environ.get("FABRIC_MCP_SQL_TOOL_NAME", "execute_sql_query")
        return await _call_fabric_mcp_jsonrpc(
            "tools/call",
            {
                "name": tool_name,
                "arguments": {
                    "workspace_id": workspace_id,
                    "workspaceId": workspace_id,
                    "sql_endpoint_id": sql_endpoint_id,
                    "sqlEndpointId": sql_endpoint_id,
                    "item_id": sql_endpoint_id,
                    "itemId": sql_endpoint_id,
                    "query": sql_query,
                    "sql": sql_query,
                },
            },
        )

    @ai_function(
        name="invoke_fabric_data_agent",
        description=(
            "Invoke a configured Fabric Data Agent through Fabric MCP. "
            "Use list_configured_fabric_data_sources first to find workspace_id and item_id."
        ),
    )
    async def invoke_fabric_data_agent(workspace_id: str, data_agent_id: str, prompt: str) -> str:
        if not any(source.get("workspace_id") == workspace_id and source.get("item_id") == data_agent_id for source in data_agent_sources):
            return json.dumps({"error": "That Fabric Data Agent is not configured for this agent project."})
        tool_name = os.environ.get("FABRIC_MCP_DATA_AGENT_TOOL_NAME", "invoke_data_agent")
        return await _call_fabric_mcp_jsonrpc(
            "tools/call",
            {
                "name": tool_name,
                "arguments": {
                    "workspace_id": workspace_id,
                    "workspaceId": workspace_id,
                    "data_agent_id": data_agent_id,
                    "dataAgentId": data_agent_id,
                    "item_id": data_agent_id,
                    "itemId": data_agent_id,
                    "prompt": prompt,
                    "message": prompt,
                },
            },
        )

    @ai_function(
        name="get_fabric_item_definition",
        description=(
            "Get the Fabric item definition for a configured source when Fabric supports getDefinition. "
            "Useful for inspecting GraphQL API definitions and other Fabric item metadata."
        ),
    )
    async def get_fabric_item_definition(workspace_id: str, item_id: str, format: str = "") -> str:
        if not any(source.get("workspace_id") == workspace_id and source.get("item_id") == item_id for source in data_sources):
            return json.dumps({"error": "That Fabric item is not configured for this agent project."})
        suffix = f"?format={format}" if format else ""
        async with aiohttp.ClientSession() as session:
            payload = await _post_json(session, f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/getDefinition{suffix}", _fabric_headers(fabric_token), {})
            return json.dumps(payload, indent=2)

    @ai_function(
        name="get_semantic_model_metadata",
        description="Get table and column metadata for a Fabric semantic model. Call this before writing DAX.",
    )
    async def get_semantic_model_metadata(workspace_id: str, semantic_model_id: str) -> str:
        if not any(source.get("workspace_id") == workspace_id and _source_item_id(source) == semantic_model_id for source in semantic_models):
            return json.dumps({"error": "That semantic model is not configured for this agent project."})
        dataset_url = f"{POWERBI_API}/groups/{workspace_id}/datasets/{semantic_model_id}"
        async with aiohttp.ClientSession() as session:
            tables = await _get_json(session, f"{dataset_url}/tables", _powerbi_headers(effective_powerbi_token))
            if tables.get("errors"):
                tables = await _semantic_model_tables_from_fabric_definition(
                    session, workspace_id, semantic_model_id, fabric_token
                )
        return json.dumps({"workspace_id": workspace_id, "semantic_model_id": semantic_model_id, "tables": tables}, indent=2)

    @ai_function(
        name="execute_dax_query",
        description=(
            "Execute a guarded read-only DAX query against a Fabric semantic model. "
            "The query must start with EVALUATE. Write/admin commands are blocked."
        ),
    )
    async def execute_dax_query(workspace_id: str, semantic_model_id: str, dax_query: str) -> str:
        if not any(source.get("workspace_id") == workspace_id and _source_item_id(source) == semantic_model_id for source in semantic_models):
            return json.dumps({"errors": [{"message": "That semantic model is not configured for this agent project."}]})
        if not _is_readonly_dax(dax_query):
            return json.dumps({"errors": [{"message": "Only read-only DAX queries starting with EVALUATE are allowed."}]})
        dataset_url = f"{POWERBI_API}/groups/{workspace_id}/datasets/{semantic_model_id}"
        body = {"queries": [{"query": dax_query}], "serializerSettings": {"includeNulls": True}}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{dataset_url}/executeQueries", headers=_powerbi_headers(effective_powerbi_token), json=body) as response:
                text = await response.text()
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = {"raw": text}
                if response.status >= 400:
                    return json.dumps({"errors": [{"status": response.status, "message": payload}]})
                rows = _query_rows(payload)
                return json.dumps({"query": dax_query, "row_count": len(rows), "rows": rows[:50]}, indent=2)

    tools = [list_configured_fabric_data_sources]
    if semantic_models:
        tools.extend([list_configured_semantic_models, get_semantic_model_metadata, execute_dax_query])
    if graphql_sources:
        tools.extend([query_fabric_graphql, get_fabric_item_definition])
    if sql_sources:
        tools.append(execute_fabric_sql_query)
    if data_agent_sources:
        tools.append(invoke_fabric_data_agent)
    if uses_fabric_mcp:
        tools.extend([call_fabric_mcp, call_fabric_mcp_tool])

    return ChatAgent(
        client,
        instructions=instructions,
        name=project.get("name", "Fabric Agent"),
        description=project.get("description", "A Fabric data agent."),
        tools=tools,
    )


# ---------------------------------------------------------------------------
# Thread management
# ---------------------------------------------------------------------------


def _get_or_create_thread(conversation_id: str):
    if AgentThread is None:
        return None
    if conversation_id not in _threads:
        _threads[conversation_id] = AgentThread()
    return _threads[conversation_id]


# ---------------------------------------------------------------------------
# FastAPI endpoints
# ---------------------------------------------------------------------------


@app.get("/readiness")
async def readiness():
    project = _project()
    return {"status": "ok", "service": "hosted-agent-runtime", "project": project.get("name")}


@app.post("/responses")
@app.post("/v1/responses")
@app.post("/openai/v1/responses")
async def responses(request: Request):
    body = await request.json()
    project = _project()
    message = _extract_input_text(body)
    conversation_id = _conversation_id(body)
    fabric_token = _extract_fabric_token(request, body)
    powerbi_token = body.get("powerbi_token") or body.get("metadata", {}).get("powerbi_token")

    if not fabric_token:
        error_text = "A Fabric bearer token is required. The custom UX supplies this token, but the Foundry playground does not."
        if body.get("stream") is True:
            return StreamingResponse(_stream_sse(error_text, conversation_id), media_type="text/event-stream")
        return _error_response(error_text, conversation_id)

    if body.get("stream") is True:
        return StreamingResponse(
            _run_agent_sse(project, message, fabric_token, conversation_id, powerbi_token),
            media_type="text/event-stream",
        )

    response_text = await _run_agent(project, message, fabric_token, conversation_id, powerbi_token)
    return _responses_payload(response_text, conversation_id, project)


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------


async def _run_agent(project: dict[str, Any], message: str, fabric_token: str, conversation_id: str, powerbi_token: str | None = None) -> str:
    import logging
    logger = logging.getLogger("hosted_agent_runtime")
    try:
        agent = _create_agent(project, fabric_token, powerbi_token)
        logger.info("Agent created: mode=%s, tools=%d", project.get("deployment_mode"), len(getattr(agent, "tools", []) or []))
    except Exception as exc:
        logger.exception("Failed to create agent")
        return f"[Agent creation error: {exc}]"

    thread = _get_or_create_thread(conversation_id)
    try:
        if not hasattr(agent, "run_stream"):
            if thread is not None:
                response = await agent.run(message, session=thread)
            else:
                response = await agent.run(message)
            result = _agent_response_text(response)
            logger.info("Agent response length: %d chars", len(result))
            if not result.strip():
                logger.warning("Agent returned empty response. Raw response object: %s", repr(response)[:500])
            return result
        chunks: list[str] = []
        if hasattr(agent, "run_stream"):
            if thread is not None:
                stream = agent.run_stream(message, thread=thread)
            else:
                stream = agent.run_stream(message)
        elif thread is not None:
            stream = agent.run(message, stream=True, session=thread)
        else:
            stream = agent.run(message, stream=True)
        async for update in stream:
            contents = getattr(update, "contents", None) or []
            for content in contents:
                if TextContent is not None and isinstance(content, TextContent) and content.text:
                    chunks.append(content.text)
                elif hasattr(content, "text") and content.text:
                    chunks.append(content.text)
        result = "".join(chunks)
        logger.info("Agent stream response length: %d chars", len(result))
        return result
    except Exception as exc:
        logger.exception("Agent run failed")
        return f"[Agent execution error: {exc}]"


async def _run_agent_sse(project: dict[str, Any], message: str, fabric_token: str, conversation_id: str, powerbi_token: str | None = None):
    import logging
    logger = logging.getLogger("hosted_agent_runtime")
    response_id = f"resp_{uuid.uuid4().hex}"
    yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'status': 'in_progress'}})}\n\n"

    try:
        agent = _create_agent(project, fabric_token, powerbi_token)
        logger.info("Agent created for stream: mode=%s, tools=%d", project.get("deployment_mode"), len(getattr(agent, "tools", []) or []))
    except Exception as exc:
        logger.exception("Failed to create streaming agent")
        async for event in _stream_sse(f"[Agent creation error: {exc}]", conversation_id):
            yield event
        return

    thread = _get_or_create_thread(conversation_id)
    full_response: list[str] = []

    try:
        if hasattr(agent, "run_stream"):
            if thread is not None:
                stream = agent.run_stream(message, thread=thread)
            else:
                stream = agent.run_stream(message)
        elif thread is not None:
            stream = agent.run(message, stream=True, session=thread)
        else:
            stream = agent.run(message, stream=True)

        async for update in stream:
            contents = getattr(update, "contents", None) or []
            for content in contents:
                text = ""
                if TextContent is not None and isinstance(content, TextContent) and content.text:
                    text = content.text
                elif hasattr(content, "text") and content.text:
                    text = content.text
                if text:
                    full_response.append(text)
                    yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': text})}\n\n"

        logger.info("Agent streamed response length: %d chars", len("".join(full_response)))
        yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': {'id': response_id, 'status': 'completed'}})}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        logger.exception("Agent stream failed")
        error_text = f"[Agent execution error: {exc}]"
        yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': error_text})}\n\n"
        yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': {'id': response_id, 'status': 'completed'}})}\n\n"
        yield "data: [DONE]\n\n"


def _agent_response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text)
    value = getattr(response, "value", None)
    if isinstance(value, str):
        return value
    if value is not None:
        return str(value)
    return str(response)


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------


def _extract_input_text(body: dict[str, Any]) -> str:
    if isinstance(body.get("input"), str):
        return body["input"]
    if isinstance(body.get("inputText"), str):
        return body["inputText"]
    if isinstance(body.get("message"), str):
        return body["message"]
    messages = body.get("messages")
    if isinstance(messages, list):
        return "\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
    response_input = body.get("input")
    if isinstance(response_input, list):
        parts: list[str] = []
        for item in response_input:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for cp in content:
                        if isinstance(cp, dict):
                            parts.append(str(cp.get("text") or cp.get("content") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return ""


def _conversation_id(body: dict[str, Any]) -> str:
    return str(
        body.get("conversation_id")
        or body.get("conversationId")
        or body.get("thread_id")
        or body.get("threadId")
        or body.get("metadata", {}).get("conversation_id")
        or uuid.uuid4()
    )


def _extract_fabric_token(request: Request, body: dict[str, Any]) -> str | None:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    for header_name in ("x-fabric-token", "x-fabric-authorization"):
        header_value = request.headers.get(header_name, "")
        if header_value.startswith("Bearer "):
            return header_value[len("Bearer "):]
        if header_value:
            return header_value
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    for source in (body, metadata):
        token = source.get("fabric_token") or source.get("fabricToken")
        if token:
            return token.removeprefix("Bearer ").strip()
    return None


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------


def _responses_payload(response_text: str, conversation_id: str, project: dict[str, Any]) -> dict[str, Any]:
    response_id = f"resp_{uuid.uuid4().hex}"
    message_id = f"msg_{uuid.uuid4().hex}"
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", ""),
        "metadata": {"conversation_id": conversation_id, "project_id": project.get("id")},
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": response_text, "annotations": []}],
            }
        ],
        "output_text": response_text,
    }


def _error_response(message: str, conversation_id: str) -> dict[str, Any]:
    return {
        "id": conversation_id,
        "object": "response",
        "status": "failed",
        "output_text": message,
        "metadata": {"conversation_id": conversation_id, "error": True},
    }


async def _stream_sse(text: str, conversation_id: str):
    response_id = f"resp_{uuid.uuid4().hex}"
    yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'status': 'in_progress'}})}\n\n"
    yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': text})}\n\n"
    yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': {'id': response_id, 'status': 'completed'}})}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Fabric / Power BI helpers (retained for tools)
# ---------------------------------------------------------------------------


def _powerbi_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _fabric_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _fabric_url(location: str) -> str:
    if location.startswith("http://") or location.startswith("https://"):
        return location
    if location.startswith("/"):
        return urljoin(FABRIC_API_ROOT, location)
    return urljoin(FABRIC_API + "/", location)


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
                    return await _get_json(session, _fabric_url(result_url), headers)
                return await _get_json(session, operation_url.rstrip("/") + "/result", headers)
            if status in {"failed", "cancelled", "canceled"}:
                return {"errors": [{"status": 202, "url": operation_url, "message": payload}]}
        await asyncio.sleep(1)
    return {"errors": [{"status": 202, "url": operation_url, "message": {"error": "Fabric getDefinition operation did not finish in time.", "last_response": last_payload}}]}


async def _semantic_model_tables_from_fabric_definition(session: aiohttp.ClientSession, workspace_id: str, model_id: str, fabric_token: str) -> dict[str, Any]:
    url = f"{FABRIC_API}/workspaces/{workspace_id}/items/{model_id}/getDefinition?format=TMDL"
    payload = await _post_json(session, url, _fabric_headers(fabric_token), {})
    if "errors" in payload:
        return {"value": [], "errors": payload["errors"], "source": "fabricDefinition"}
    parts = payload.get("definition", {}).get("parts") or payload.get("parts") or []
    tables: list[dict[str, Any]] = []
    for part in parts:
        path = str(part.get("path") or "")
        if path and not path.endswith(".tmdl"):
            continue
        text = _decode_definition_payload(part)
        if text:
            tables.extend(_tables_from_tmdl(text))
    return {"value": _merge_tables(tables), "source": "fabricDefinition"}


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
            current = {"name": _clean_tmdl_name(line.removeprefix("table ")), "columns": [], "measures": []}
            tables.append(current)
        elif current is not None and line.startswith("column "):
            current["columns"].append({"name": _clean_tmdl_name(line.removeprefix("column "))})
        elif current is not None and line.startswith("measure "):
            current["measures"].append({"name": _clean_tmdl_name(line.removeprefix("measure "))})
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


def _is_readonly_dax(query: str) -> bool:
    normalized = re.sub(r"--.*?$|/\*.*?\*/", " ", query, flags=re.MULTILINE | re.DOTALL).strip().lower()
    if not normalized.startswith("evaluate") and not normalized.startswith("define"):
        return False
    forbidden = {"create", "alter", "delete", "insert", "update", "drop", "clear", "refresh", "process"}
    return not any(re.search(rf"\b{keyword}\b", normalized) for keyword in forbidden)


def _query_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        return payload.get("results", [])[0].get("tables", [])[0].get("rows", [])
    except (IndexError, AttributeError):
        return []


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    uvicorn.run(app, host="0.0.0.0", port=port)
