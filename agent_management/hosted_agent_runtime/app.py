"""Hosted Agent Runtime - agent-framework-powered version.

Reads project configuration from Cosmos DB at startup, builds a ChatAgent
with tools for the configured Fabric data sources, then serves the Foundry
responses protocol so it can be registered as a hosted agent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import base64
import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp
import uvicorn
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import ClientSecretCredential, DefaultAzureCredential, get_bearer_token_provider
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

try:
    import pyarrow.ipc as arrow_ipc
except ImportError:
    arrow_ipc = None  # type: ignore[assignment]

# FoundryChatClient — may live in different submodules
try:
    from agent_framework.foundry import FoundryChatClient
except ImportError:
    from agent_framework import FoundryChatClient

try:
    from agent_framework.foundry import AnthropicFoundryClient
except ImportError:
    AnthropicFoundryClient = None  # type: ignore[assignment,misc]

load_dotenv()

# ── Observability / Tracing ───────────────────────────────────────────────────
_appinsights_conn = os.environ.get("FABRIC_AGENT_APPINSIGHTS_CONNECTION_STRING") or os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
try:
    from agent_framework.observability import OBSERVABILITY_SETTINGS

    additional_exporters = []
    if _appinsights_conn:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter, AzureMonitorLogExporter, AzureMonitorMetricExporter
        additional_exporters.append(AzureMonitorTraceExporter(connection_string=_appinsights_conn))
        additional_exporters.append(AzureMonitorLogExporter(connection_string=_appinsights_conn))
        additional_exporters.append(AzureMonitorMetricExporter(connection_string=_appinsights_conn))

    OBSERVABILITY_SETTINGS.enable_sensitive_data = True
    OBSERVABILITY_SETTINGS._configure(additional_exporters=additional_exporters if additional_exporters else None)

    # Also attach the OTel log handler to the root logger so all app logs go to traces table
    if _appinsights_conn:
        import logging as _logging
        from opentelemetry._logs import get_logger_provider
        from opentelemetry.sdk._logs import LoggingHandler
        _logging.getLogger().setLevel(_logging.INFO)
        _logging.getLogger("hosted_agent_runtime").setLevel(_logging.INFO)
        _logging.getLogger("hosted_agent_runtime.traces").setLevel(_logging.INFO)
        _logging.getLogger("hosted_agent_runtime.dax").setLevel(_logging.INFO)
        _otel_handler = LoggingHandler(logger_provider=get_logger_provider())
        _otel_handler.setLevel(_logging.INFO)
        _logging.getLogger().addHandler(_otel_handler)
        _logging.getLogger("hosted_agent_runtime").info("App Insights telemetry configured for hosted runtime")
except Exception:
    pass

app = FastAPI(title="Hosted Agent Runtime")

# ── Instrument FastAPI + aiohttp for trace propagation ────────────────────────
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)
except Exception:
    pass
try:
    from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
    AioHttpClientInstrumentor().instrument()
except Exception:
    pass

FABRIC_API = "https://api.fabric.microsoft.com/v1"
FABRIC_API_ROOT = "https://api.fabric.microsoft.com"
POWERBI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_OPERATION_POLL_LIMIT = 12
SEMANTIC_SOURCE_TYPE = "semantic_model"
SEMANTIC_MODEL_TYPES = {"SemanticModel", "PowerBIDataset"}
GRAPHQL_TYPES = {"GraphQLApi"}
SQL_ENDPOINT_TYPES = {"SQLEndpoint", "Warehouse"}
DATA_AGENT_TYPES = {"DataAgent", "FabricDataAgent", "DataAgentItem", "FabricDataAgentItem"}
FABRIC_ITEM_TYPE_MAP: dict[str, str] = {}
for _type in SEMANTIC_MODEL_TYPES:
    FABRIC_ITEM_TYPE_MAP[_type] = SEMANTIC_SOURCE_TYPE
for _type in GRAPHQL_TYPES:
    FABRIC_ITEM_TYPE_MAP[_type] = "graphql"
for _type in SQL_ENDPOINT_TYPES:
    FABRIC_ITEM_TYPE_MAP[_type] = "sql_endpoint"
for _type in DATA_AGENT_TYPES:
    FABRIC_ITEM_TYPE_MAP[_type] = "data_agent"
FABRIC_ITEM_TYPE_NORMALIZED_MAP = {re.sub(r"[^a-z0-9]", "", key.lower()): value for key, value in FABRIC_ITEM_TYPE_MAP.items()}
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


def _resolve_model_deployment(model_config: dict[str, Any] | None = None) -> str:
    return (
        (model_config or {}).get("deployment_name")
        or os.environ.get("model_deployment_name")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "")
    )


def _is_claude_model(model_config: dict[str, Any] | None = None) -> bool:
    values = [
        (model_config or {}).get("deployment_name"),
        (model_config or {}).get("model_display_name"),
        (model_config or {}).get("model_name"),
        (model_config or {}).get("provider"),
        (model_config or {}).get("publisher"),
        os.environ.get("model_deployment_name"),
        os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME"),
    ]
    text = " ".join(str(value or "") for value in values).lower()
    return "claude" in text or "anthropic" in text


def _project_model_config(project: dict[str, Any]) -> dict[str, Any] | None:
    mode = project.get("deployment_mode")
    if mode == "standalone":
        return project.get("standalone_agent", {}).get("model_config")
    if mode == "orchestrator_only":
        return project.get("orchestrator_only", {}).get("model_config")
    return project.get("orchestrator", {}).get("model_config")


def _int_env(name: str, default: int | None = None) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _agent_default_options(model_config: dict[str, Any] | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {}
    max_tokens = _int_env("AGENT_MAX_TOKENS")
    if _is_claude_model(model_config):
        max_tokens = _int_env("CLAUDE_AGENT_MAX_TOKENS", max_tokens or 8192)
    if max_tokens:
        options["max_tokens"] = max_tokens
    return options


def _anthropic_foundry_resource_from_endpoint(project_endpoint: str) -> str:
    host = urlparse(project_endpoint).hostname or ""
    suffix = ".services.ai.azure.com"
    if host.endswith(suffix):
        return host[: -len(suffix)].split(".")[0]
    return ""


def _get_chat_client(model_config: dict[str, Any] | None = None) -> Any:
    """Create a FoundryChatClient using the service principal credential."""
    project_endpoint = (os.environ.get("MAF_FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")).rstrip("/")
    deployment_name = _resolve_model_deployment(model_config)
    logger = logging.getLogger("hosted_agent_runtime")
    is_claude = _is_claude_model(model_config)
    logger.info(
        "Runtime model client routing: deployment_name=%s, model_name=%s, model_display_name=%s, provider=%s, publisher=%s, is_claude=%s",
        deployment_name,
        (model_config or {}).get("model_name", ""),
        (model_config or {}).get("model_display_name", ""),
        (model_config or {}).get("provider", ""),
        (model_config or {}).get("publisher", ""),
        is_claude,
    )

    if is_claude:
        if AnthropicFoundryClient is None:
            logger.warning("Claude model detected but AnthropicFoundryClient is unavailable; falling back to FoundryChatClient")
        else:
            base_url = os.environ.get("ANTHROPIC_FOUNDRY_BASE_URL", "").strip() or None
            resource = os.environ.get("ANTHROPIC_FOUNDRY_RESOURCE", "").strip() or _anthropic_foundry_resource_from_endpoint(project_endpoint)
            token_provider = get_bearer_token_provider(_credential(), "https://ai.azure.com/.default")
            logger.info(
                "Using AnthropicFoundryClient for Claude model: model=%s, resource=%s, base_url_configured=%s",
                deployment_name,
                resource or "",
                bool(base_url),
            )
            return AnthropicFoundryClient(
                model=deployment_name or None,
                resource=None if base_url else (resource or None),
                base_url=base_url,
                azure_ad_token_provider=token_provider,
            )

    return FoundryChatClient(
        project_endpoint=project_endpoint or None,
        model=deployment_name or None,
        credential=_credential(),
    )


_project_cache: dict[str, Any] | None = None


def _project() -> dict[str, Any]:
    global _project_cache
    if _project_cache is not None:
        return _project_cache

    logger = logging.getLogger(__name__)
    project_id = (os.environ.get("MAF_MGMT_PROJECT_ID") or os.environ.get("AGENT_MGMT_PROJECT_ID", "")).strip()
    if not project_id:
        raise RuntimeError("MAF_MGMT_PROJECT_ID is required.")
    endpoint = (os.environ.get("MAF_MGMT_COSMOS_ENDPOINT") or os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "")).strip()
    if not endpoint:
        raise RuntimeError("MAF_MGMT_COSMOS_ENDPOINT is required.")
    database_name = os.environ.get("MAF_MGMT_COSMOS_DATABASE") or os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents")
    versions_container = os.environ.get("MAF_MGMT_VERSIONS_CONTAINER") or os.environ.get("AGENT_MGMT_VERSIONS_CONTAINER", "agentversions")
    project_version = (os.environ.get("MAF_MGMT_PROJECT_VERSION") or os.environ.get("AGENT_MGMT_PROJECT_VERSION", "")).strip()
    client = CosmosClient(endpoint, credential=_credential())
    db = client.get_database_client(database_name)

    # Load the pinned version snapshot when one was selected at deployment time; otherwise use newest.
    try:
        container = db.get_container_client(versions_container)
        if project_version:
            results = [container.read_item(item=f"{project_id}:{project_version}", partition_key=project_id)]
        else:
            query = "SELECT TOP 1 * FROM c WHERE c.projectid = @pid ORDER BY c.deployed_at DESC"
            params = [{"name": "@pid", "value": project_id}]
            results = list(container.query_items(query=query, parameters=params, partition_key=project_id, max_item_count=1))
        if results and results[0].get("snapshot"):
            logger.info("_project: loaded snapshot version %s from %s", results[0].get("version"), versions_container)
            _project_cache = results[0]["snapshot"]
            return _project_cache
        else:
            logger.warning("_project: no version snapshots found in %s for project %s", versions_container, project_id)
    except Exception as exc:
        logger.warning("_project: failed to query agentversions: %s", exc)

    # Fallback: live read from agentmetadata (local dev / pre-versioning deployments)
    container_name = os.environ.get("MAF_MGMT_COSMOS_CONTAINER") or os.environ.get("AGENT_MGMT_COSMOS_CONTAINER", "agentmetadata")
    logger.warning("_project: falling back to live read from %s", container_name)
    container = db.get_container_client(container_name)
    return container.read_item(item=project_id, partition_key=project_id)


def _cosmos_db():
    endpoint = (os.environ.get("MAF_MGMT_COSMOS_ENDPOINT") or os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", "")).strip()
    if not endpoint:
        raise RuntimeError("MAF_MGMT_COSMOS_ENDPOINT is required.")
    database_name = os.environ.get("MAF_MGMT_COSMOS_DATABASE") or os.environ.get("AGENT_MGMT_COSMOS_DATABASE", "agents")
    client = CosmosClient(endpoint, credential=_credential())
    return client.get_database_client(database_name)


def _semantic_metadata_from_cache(workspace_id: str, semantic_model_id: str) -> dict[str, Any] | None:
    container_name = os.environ.get("MAF_MGMT_METADATA_CONTAINER") or os.environ.get("AGENT_MGMT_METADATA_CONTAINER", "semanticmodelmetadata")
    item_id = f"{workspace_id}:{semantic_model_id}"
    logger = logging.getLogger("hosted_agent_runtime.metadata")
    try:
        doc = _cosmos_db().get_container_client(container_name).read_item(item=item_id, partition_key=item_id)
    except CosmosResourceNotFoundError:
        logger.info("Semantic metadata cache miss: container=%s semantic_model_id=%s", container_name, semantic_model_id)
        return None
    except Exception as exc:
        logger.warning("Semantic metadata cache read failed: container=%s semantic_model_id=%s error=%s", container_name, semantic_model_id, exc)
        return None
    logger.info("Semantic metadata cache hit: container=%s semantic_model_id=%s refreshed_at=%s", container_name, semantic_model_id, doc.get("refreshed_at"))
    return {
        "workspace_id": workspace_id,
        "semantic_model_id": semantic_model_id,
        "semantic_model_name": doc.get("semantic_model_name"),
        "tables": doc.get("tables") or {"value": []},
        "relationships": doc.get("relationships") or [],
        "ai_instructions": doc.get("ai_instructions") or "",
        "metadata_source": "cosmos_cache",
        "definition_hash": doc.get("definition_hash"),
        "refreshed_at": doc.get("refreshed_at"),
        "status": doc.get("status"),
        "last_error": doc.get("last_error"),
    }


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
            return _with_no_progress_narration_policy(
                f"You are {agent.get('name', 'a Fabric agent')}. "
                f"{agent.get('description', '')} "
                f"Configured data sources: {source_summary}. "
                "Use the available Fabric tools for GraphQL APIs, SQL endpoints, Fabric Data Agents, and Fabric MCP. "
                "For Fabric MCP, discover accessible Fabric items first, then use the matching semantic model, GraphQL, SQL endpoint, Data Agent, or item definition tool."
            )
        model_name = _source_item_name(agent.get("semantic_model", {})) or "the configured semantic model"
        return _with_no_progress_narration_policy(
            f"You are {agent.get('name', 'a Fabric semantic model agent')}. "
            f"{agent.get('description', '')} "
            f"You answer questions using {model_name}. "
            "For any question that asks about data, fields, entities, measures, identity resolution, filtering, counts, totals, trends, or records, call get_semantic_model_metadata first and then call execute_dax_query or execute_dax_queries before answering. "
            "Do not answer with a plan or say you are going to get metadata; call the tool instead. "
            "Answer from tool results, and say what failed if a required tool call fails."
        )
    if mode == "orchestrator_only":
        orch = project.get("orchestrator_only", {})
        if orch.get("prompt"):
            return _with_runtime_tool_policy(orch["prompt"], configured_sources)
        agent_summaries = [
            f"- {a.get('display_name') or a.get('agent_name', 'agent')}: {a.get('description', 'no description')}"
            for a in orch.get("external_agents", [])
        ]
        return _with_no_progress_narration_policy(
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
    return _with_no_progress_narration_policy(
        f"You are {orchestrator.get('name', 'an orchestrator agent')}. "
        f"{orchestrator.get('description', '')} "
        f"You route questions to subagents: {', '.join(subagent_names)}. "
        f"Configured data sources: {source_summary}. "
        "Use the available Fabric tools for each configured source type. "
        "For Fabric MCP, discover accessible Fabric items first, then use semantic model tools for DAX, GraphQL tools for Fabric GraphQL APIs, SQL endpoint tools for SQL, Fabric Data Agent tools for Data Agents, and item definition tools for Fabric metadata."
    )


def _with_no_progress_narration_policy(prompt: str) -> str:
    policy = [
        "Response policy:",
        "Use tools silently. Do not narrate intermediate steps, tool plans, IDs being resolved, peer-selection process, partial findings, or progress updates.",
        "Do not include process narration while working.",
        "Only return the final answer after all needed tool calls are complete.",
    ]
    return prompt.rstrip() + "\n\n" + "\n".join(policy)


def _with_final_answer_streaming_policy(prompt: str) -> str:
    policy = [
        "Streaming response policy:",
        "Do not emit any user-visible text before all required tool calls are complete.",
        "When the final answer is ready, begin it with the exact line FINAL_ANSWER_START, then write only the final answer.",
        "Never use FINAL_ANSWER_START for planning, tool progress, IDs being resolved, partial findings, or intermediate analysis.",
    ]
    return prompt.rstrip() + "\n\n" + "\n".join(policy)


def _strip_final_answer_marker(text: str) -> str:
    marker = "FINAL_ANSWER_START"
    if marker not in text:
        return text
    return text.split(marker, 1)[1].lstrip(" :\r\n\t")


def _strip_progress_narration(text: str) -> str:
    cleaned = _strip_final_answer_marker(text).lstrip()
    if not cleaned:
        return cleaned
    final_start_patterns = [
        r"(?m)^\s*#{1,3}\s+\S",
        r"(?m)^\s*---\s*$",
        r"(?m)^\s*\|.+\|\s*$",
        r"(?m)^\s*\*\*[^*]+\*\*\s*$",
    ]
    for pattern in final_start_patterns:
        match = re.search(pattern, cleaned)
        if match and match.start() > 0:
            prefix = cleaned[:match.start()].strip()
            if _looks_like_progress_prelude(prefix):
                return cleaned[match.start():].lstrip()
    return _strip_leading_progress_sentences(cleaned)


def _looks_like_progress_prelude(text: str) -> bool:
    if not text:
        return False
    sentences = _leading_sentences(text)
    if len(sentences) >= 2:
        return True
    return bool(sentences and _looks_like_progress_sentence(sentences[0]))


def _strip_leading_progress_sentences(text: str) -> str:
    cleaned = text
    previous = None
    while previous != cleaned:
        previous = cleaned
        sentence = _first_sentence(cleaned)
        if not sentence or not _looks_like_progress_sentence(sentence):
            break
        cleaned = cleaned[len(sentence):].lstrip()
    return cleaned


def _leading_sentences(text: str, limit: int = 8) -> list[str]:
    sentences = []
    remaining = text.lstrip()
    while remaining and len(sentences) < limit:
        sentence = _first_sentence(remaining)
        if not sentence:
            break
        sentences.append(sentence)
        remaining = remaining[len(sentence):].lstrip()
    return sentences


def _first_sentence(text: str) -> str:
    match = re.match(r"(?s)^\s*.*?(?:[.?!](?=\s|[A-Z#*|`-])|\n\s*\n|$)", text)
    return match.group(0) if match else ""


def _looks_like_progress_sentence(sentence: str) -> bool:
    stripped = sentence.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    first_word = re.match(r"^[a-z]+", lower)
    if first_word and first_word.group(0).endswith("ing"):
        return True
    if re.match(r"^(?:i\b|now\b|next\b|then\b|wait\b)", lower):
        return True
    if re.search(r"\b(?:id|identifier|key)\b\s*(?:=|:)", lower):
        return True
    return False


def _with_runtime_tool_policy(prompt: str, configured_sources: list[dict[str, Any]]) -> str:
    if not configured_sources:
        return _with_no_progress_narration_policy(prompt)
    data_agent_sources = [source for source in configured_sources if source.get("source_type") == "data_agent"]
    semantic_sources = [source for source in configured_sources if source.get("source_type") == "semantic_model"]
    has_fabric_mcp = any(_source_type(source) == "fabric_mcp" for source in configured_sources)
    policy = [
        "Runtime tool-use policy:",
        f"Configured data sources: {_format_data_source_summary(configured_sources)}.",
        "Use the available Fabric tools for configured sources before deciding a request is out of scope.",
    ]
    if semantic_sources:
        policy.extend([
            "For semantic-model questions about data, fields, entities, measures, identity resolution, filtering, counts, totals, trends, records, or DAX, call get_semantic_model_metadata before answering.",
            "For data-backed answers, call execute_dax_query or execute_dax_queries after metadata is available; do not answer from assumptions or prompt text alone.",
            "When an answer needs multiple independent result sets, comparisons, validations, breakdowns, totals plus details, or more than one EVALUATE statement, prefer execute_dax_queries and pass all queries in dax_queries_json so they can run in one semantic-model operation.",
            "Do not respond with a plan before calling required tools; make the tool call in the same turn instead.",
            "If a required metadata or DAX tool call fails, report the failure and include the relevant error details from the tool result.",
        ])
    if has_fabric_mcp:
        policy.extend([
            "For Fabric MCP, use discover_accessible_fabric_items to find accessible semantic models, GraphQL APIs, SQL endpoints, Warehouses, Data Agents, and other supported Fabric items.",
            "After discovery, use the matching tool for the discovered item type: semantic model metadata and DAX tools for semantic models, GraphQL tools for GraphQL APIs, SQL tools for SQL endpoints and Warehouses, Data Agent tools for Data Agents, and item definition tools for Fabric metadata.",
            "Do not require item IDs to be preconfigured when Fabric MCP is enabled; use the user's Fabric token and the discovered workspace_id and item_id values.",
        ])
    if data_agent_sources:
        policy.extend([
            "For a configured Fabric Data Agent, treat broad analytical questions about that data source, including trends, summaries, and 'all my data' phrasing, as in scope for that Fabric Data Agent.",
            "Invoke invoke_fabric_data_agent with the user's question before refusing or narrowing the request.",
            "Pass the user's analytical question through to the Fabric Data Agent; do not reject it solely because it is broad.",
        ])
    return _with_no_progress_narration_policy(prompt.rstrip() + "\n\n" + "\n".join(policy))


def _get_configured_data_sources(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract all configured data source bindings from the project config."""
    mode = project.get("deployment_mode")
    if mode == "standalone":
        source = project.get("standalone_agent", {}).get("semantic_model", {})
        return [_normalize_data_source(source)] if _has_configured_source(source) else []
    if mode == "orchestrator_only":
        # orchestrator_only projects may have data_sources at the top level or in config
        top_sources = project.get("data_sources") or project.get("orchestrator_only", {}).get("data_sources") or []
        sources = []
        for source in top_sources:
            if _has_configured_source(source):
                sources.append(_normalize_data_source(source))
        return sources
    # orchestrator mode: check subagents
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


def _tool_name(tool_obj: Any) -> str:
    return str(getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", None) or type(tool_obj).__name__)


def _compact_semantic_metadata(metadata: dict[str, Any], max_tables: int = 30, max_fields_per_table: int = 40) -> dict[str, Any]:
    tables_payload = metadata.get("tables") if isinstance(metadata.get("tables"), dict) else {}
    tables = tables_payload.get("value") if isinstance(tables_payload.get("value"), list) else []
    compact_tables: list[dict[str, Any]] = []
    for table in tables[:max_tables]:
        if not isinstance(table, dict):
            continue
        columns = table.get("columns") if isinstance(table.get("columns"), list) else []
        measures = table.get("measures") if isinstance(table.get("measures"), list) else []
        compact_tables.append({
            "name": table.get("name") or table.get("tableName"),
            "columns": [column.get("name") or column.get("columnName") for column in columns[:max_fields_per_table] if isinstance(column, dict)],
            "measures": [measure.get("name") or measure.get("measureName") for measure in measures[:max_fields_per_table] if isinstance(measure, dict)],
        })
    return {
        "workspace_id": metadata.get("workspace_id"),
        "semantic_model_id": metadata.get("semantic_model_id"),
        "semantic_model_name": metadata.get("semantic_model_name"),
        "metadata_source": metadata.get("metadata_source"),
        "refreshed_at": metadata.get("refreshed_at"),
        "table_count": len(tables),
        "tables": compact_tables,
        "relationships": metadata.get("relationships") or [],
        "ai_instructions": metadata.get("ai_instructions") or "",
    }


def _preloaded_semantic_metadata_context(project: dict[str, Any]) -> str:
    semantic_models = _get_semantic_models(project)
    if not semantic_models:
        return ""
    logger = logging.getLogger("hosted_agent_runtime.metadata")
    cache_hits = 0
    cache_misses = 0
    compact_models: list[dict[str, Any]] = []
    missing_models: list[dict[str, str]] = []
    for source in semantic_models:
        workspace_id = str(source.get("workspace_id") or "")
        semantic_model_id = _source_item_id(source)
        if not workspace_id or not semantic_model_id:
            continue
        metadata = _semantic_metadata_from_cache(workspace_id, semantic_model_id)
        if metadata and metadata.get("tables", {}).get("value"):
            cache_hits += 1
            compact_models.append(_compact_semantic_metadata(metadata))
        else:
            cache_misses += 1
            missing_models.append({
                "workspace_id": workspace_id,
                "semantic_model_id": semantic_model_id,
                "semantic_model_name": _source_item_name(source),
            })
    logger.info(
        "Runtime semantic metadata preloaded: semantic_models=%d, cache_hits=%d, cache_misses=%d",
        len(semantic_models),
        cache_hits,
        cache_misses,
    )
    if not compact_models and not missing_models:
        return ""
    payload = {"models": compact_models, "cache_misses": missing_models}
    return (
        "Runtime semantic metadata context was preloaded before this turn. "
        "Use this metadata to choose tables, columns, and measures. "
        "For data-backed answers, call execute_dax_query or execute_dax_queries with the configured workspace_id and semantic_model_id. "
        "If the answer requires multiple independent result sets, comparisons, validations, breakdowns, totals plus details, or more than one EVALUATE statement, use execute_dax_queries and include every query in one dax_queries_json array. "
        "Do not narrate metadata preparation; metadata is already provided here.\n"
        f"```json\n{json.dumps(payload, indent=2)}\n```"
    )


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
    """Invoke an existing deployed hosted agent via the Foundry API using streaming to avoid platform timeout."""
    endpoint = (os.environ.get("MAF_FOUNDRY_PROJECT_ENDPOINT") or os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "")).rstrip("/")
    if not endpoint:
        return {"error": "FOUNDRY_PROJECT_ENDPOINT is required."}
    api_version = os.environ.get("FOUNDRY_API_VERSION", "v1")
    features = os.environ.get("FOUNDRY_FEATURES", "HostedAgents=V1Preview")
    url = f"{endpoint}/agents/{agent_name}/endpoint/protocols/openai/responses?api-version={api_version}"
    body = {
        "input": message,
        "conversation_id": str(uuid.uuid4()),
        "stream": True,
        "fabric_token": fabric_token,
        "powerbi_token": powerbi_token,
    }
    # Use SP credential for Foundry API auth (not the user's Fabric token)
    ai_token = _access_token_str()
    headers = {
        "Authorization": f"Bearer {ai_token}",
        "Foundry-Features": features,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    max_retries = 3
    retry_delay = 5  # seconds

    async with aiohttp.ClientSession() as session:
        for attempt in range(max_retries):
            try:
                async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=300)) as response:
                    # Retry on session_not_ready (agent container still spinning up)
                    if response.status == 409 or response.status == 408:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay * (attempt + 1))
                            ai_token = _access_token_str()
                            headers["Authorization"] = f"Bearer {ai_token}"
                            continue
                        text = await response.text()
                        return {"error": f"Agent {agent_name} not ready after {max_retries} attempts", "detail": text[:500]}

                    if response.status >= 400:
                        text = await response.text()
                        try:
                            payload = json.loads(text) if text else {}
                        except json.JSONDecodeError:
                            payload = {"raw": text}
                        return {"error": f"Agent {agent_name} returned status {response.status}", "detail": payload}

                    content_type = (response.headers.get("Content-Type") or "").lower()

                    # Streaming response — consume SSE and accumulate text
                    if "text/event-stream" in content_type:
                        accumulated_text: list[str] = []
                        final_text = ""
                        buffer = ""
                        async for chunk in response.content.iter_any():
                            buffer += chunk.decode("utf-8")
                            blocks = buffer.split("\n\n")
                            buffer = blocks.pop()
                            for block in blocks:
                                event_data = _parse_sse_data(block)
                                if not event_data:
                                    continue
                                # Accumulate deltas
                                delta = event_data.get("delta") if isinstance(event_data, dict) else None
                                if isinstance(delta, str):
                                    accumulated_text.append(delta)
                                # Check for final text in completed events
                                evt_type = event_data.get("type", "") if isinstance(event_data, dict) else ""
                                if evt_type in ("response.output_text.done", "response.completed"):
                                    text_val = _extract_sse_final_text(event_data)
                                    if text_val:
                                        final_text = text_val

                        result_text = final_text or "".join(accumulated_text)
                        if result_text:
                            return {"agent_name": agent_name, "response": result_text}
                        return {"agent_name": agent_name, "response": "(No response text received)"}

                    # Non-streaming JSON response fallback
                    text = await response.text()
                    try:
                        payload = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        payload = {"raw": text}

                    if isinstance(payload, dict):
                        if isinstance(payload.get("output_text"), str) and payload["output_text"]:
                            return {"agent_name": agent_name, "response": payload["output_text"]}
                        output = payload.get("output")
                        if isinstance(output, list):
                            texts = [item.get("content", [{}])[0].get("text", "") for item in output if item.get("type") == "message"]
                            return {"agent_name": agent_name, "response": "\n".join(texts) or str(payload)}
                        if isinstance(output, str):
                            return {"agent_name": agent_name, "response": output}
                    return {"agent_name": agent_name, "response": str(payload)}

            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    ai_token = _access_token_str()
                    headers["Authorization"] = f"Bearer {ai_token}"
                    continue
                return {"error": f"Agent {agent_name} timed out after {max_retries} attempts"}

    return {"error": f"Agent {agent_name} failed after {max_retries} attempts"}


def _parse_sse_data(block: str) -> dict[str, Any] | None:
    """Parse a single SSE block and return the JSON data payload."""
    data_lines: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("data:"):
            data_lines.append(stripped[5:].strip())
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    if raw == "[DONE]":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _extract_sse_final_text(event_data: dict[str, Any]) -> str:
    """Extract final text from a completed/done SSE event."""
    if isinstance(event_data.get("text"), str):
        return event_data["text"]
    if isinstance(event_data.get("output_text"), str):
        return event_data["output_text"]
    response = event_data.get("response") if isinstance(event_data.get("response"), dict) else {}
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    output = response.get("output") if isinstance(response.get("output"), list) else []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content") if isinstance(item.get("content"), list) else []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "output_text" and isinstance(c.get("text"), str):
                return c["text"]
    return ""


def _agent_stream_keepalive_seconds() -> int:
    try:
        return max(5, int(os.environ.get("AGENT_STREAM_KEEPALIVE_SECONDS", "15")))
    except ValueError:
        return 15


def _agent_stream_idle_timeout_seconds() -> int:
    try:
        return max(_agent_stream_keepalive_seconds(), int(os.environ.get("AGENT_STREAM_IDLE_TIMEOUT_SECONDS", "240")))
    except ValueError:
        return 240


def _access_token_str() -> str:
    """Get an access token string for calling Foundry APIs."""
    credential = _credential()
    scope = os.environ.get("FOUNDRY_TOKEN_SCOPE", "https://ai.azure.com/.default")
    token = credential.get_token(scope)
    return token.token


def _create_agent(project: dict[str, Any], fabric_token: str, powerbi_token: str | None = None, tool_wrapper: Any = None) -> ChatAgent:
    """Create a ChatAgent from project config with source-aware Fabric tools."""
    logger = logging.getLogger("hosted_agent_runtime")
    mode = project.get("deployment_mode")
    model_config = _project_model_config(project)

    client = _get_chat_client(model_config)
    instructions = _build_instructions(project)
    instructions = _with_final_answer_streaming_policy(instructions)
    default_options = _agent_default_options(model_config)

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

        logger.info(
            "Runtime tools prepared: mode=%s, tools=%d, tool_names=%s, external_agents=%d",
            mode,
            len(tools),
            ",".join(_tool_name(tool) for tool in tools),
            len(external_agents),
        )
        agent = ChatAgent(
            client,
            instructions=instructions,
            name=project.get("name", "Orchestrator Agent"),
            description=project.get("description", "An orchestrator that delegates to existing agents."),
            tools=[tool_wrapper(t) for t in tools] if tool_wrapper else tools,
            default_options=default_options or None,
        )
        setattr(agent, "_fabric_runtime_tool_count", len(tools))
        return agent

    data_sources = _get_configured_data_sources(project)
    semantic_models = _get_semantic_models(project)
    graphql_sources = _get_sources_by_type(project, "graphql")
    sql_sources = _get_sources_by_type(project, "sql_endpoint")
    data_agent_sources = _get_sources_by_type(project, "data_agent")
    uses_fabric_mcp = _uses_fabric_mcp(project)
    allow_dynamic_fabric_items = any(_source_type(source) == "fabric_mcp" for source in data_sources)
    has_semantic_model_access = bool(semantic_models) or allow_dynamic_fabric_items
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

    @ai_function(
        name="discover_accessible_fabric_items",
        description=(
            "Search Fabric workspaces for supported items the signed-in user can access. "
            "Use source_type to filter to semantic_model, graphql, sql_endpoint, data_agent, or leave it blank for all. "
            "Use this first for Fabric MCP all-up questions before choosing item IDs for downstream tools."
        ),
    )
    async def discover_accessible_fabric_items(search_text: str = "", source_type: str = "") -> str:
        if not allow_dynamic_fabric_items:
            return json.dumps({"error": "Dynamic Fabric item discovery is only enabled for Fabric MCP projects."})
        payload = await _discover_accessible_fabric_items(fabric_token, search_text, source_type)
        return json.dumps(payload, indent=2)

    def _can_query_semantic_model(workspace_id: str, semantic_model_id: str) -> bool:
        return allow_dynamic_fabric_items or any(
            source.get("workspace_id") == workspace_id and _source_item_id(source) == semantic_model_id
            for source in semantic_models
        )

    def _can_query_graphql(workspace_id: str, graphql_api_id: str) -> bool:
        return allow_dynamic_fabric_items or any(
            source.get("workspace_id") == workspace_id and source.get("item_id") == graphql_api_id
            for source in graphql_sources
        )

    def _can_query_sql_endpoint(workspace_id: str, sql_endpoint_id: str) -> bool:
        return allow_dynamic_fabric_items or any(
            source.get("workspace_id") == workspace_id and source.get("item_id") == sql_endpoint_id
            for source in sql_sources
        )

    def _can_invoke_data_agent(workspace_id: str, data_agent_id: str) -> bool:
        return allow_dynamic_fabric_items or any(
            source.get("workspace_id") == workspace_id and source.get("item_id") == data_agent_id
            for source in data_agent_sources
        )

    def _can_get_fabric_item(workspace_id: str, item_id: str) -> bool:
        return allow_dynamic_fabric_items or any(
            source.get("workspace_id") == workspace_id and source.get("item_id") == item_id
            for source in data_sources
        )

    async def _call_fabric_mcp_jsonrpc(method: str, params: dict[str, Any]) -> str:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        headers = {"Authorization": f"Bearer {fabric_token}", "Content-Type": "application/json", "Accept": "application/json"}
        async with aiohttp.ClientSession() as session:
            async with session.post(_fabric_mcp_endpoint(), headers=headers, json=payload) as response:
                text = await response.text()
                if response.status >= 400:
                    return json.dumps({"status": response.status, "error": text})
                return text

    async def _call_fabric_mcp_jsonrpc_payload(method: str, params: dict[str, Any]) -> dict[str, Any]:
        text = await _call_fabric_mcp_jsonrpc(method, params)
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"raw": text}
        return payload if isinstance(payload, dict) else {"raw": payload}

    def _mcp_payload_error(payload: dict[str, Any]) -> Any:
        return payload.get("error") or payload.get("errors") or (payload if payload.get("status") else None)

    async def _fabric_mcp_tools() -> list[dict[str, Any]]:
        tools_payload = await _call_fabric_mcp_jsonrpc_payload("tools/list", {})
        result = tools_payload.get("result") if isinstance(tools_payload.get("result"), dict) else tools_payload
        tools = result.get("tools") if isinstance(result, dict) else []
        if not isinstance(tools, list):
            return []
        return [tool for tool in tools if isinstance(tool, dict)]

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
        if not _can_query_graphql(workspace_id, graphql_api_id):
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
            "Use this for configured SQL endpoints and broader Fabric operations."
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
        if not _can_query_sql_endpoint(workspace_id, sql_endpoint_id):
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
            "Invoke a configured Fabric Data Agent directly via the Fabric REST API. "
            "Use list_configured_fabric_data_sources first to find workspace_id and item_id."
        ),
    )
    async def invoke_fabric_data_agent(workspace_id: str, data_agent_id: str, prompt: str) -> str:
        if not _can_invoke_data_agent(workspace_id, data_agent_id):
            return json.dumps({"error": "That Fabric Data Agent is not configured for this agent project."})
        # Fabric Data Agent uses the OpenAI Assistants API pattern:
        # 1. Create a thread, 2. Add a message, 3. Create a run, 4. Poll run, 5. Get messages
        api_version = "2024-12-01-preview"
        base_url = f"{FABRIC_API}/workspaces/{workspace_id}/dataagents/{data_agent_id}/aiassistant/openai"
        headers = _fabric_headers(fabric_token)

        def _url(path: str) -> str:
            sep = "&" if "?" in path else "?"
            return f"{base_url}{path}{sep}api-version={api_version}"

        async with aiohttp.ClientSession() as session:
            # Step 0: Create assistant (Fabric requires this before creating a run)
            async with session.post(_url("/assistants"), headers=headers, json={"model": "not used"}) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    return json.dumps({"error": f"Failed to create assistant (HTTP {resp.status})", "detail": text[:500]})
                assistant = await resp.json()
            assistant_id = assistant.get("id")
            if not assistant_id:
                return json.dumps({"error": "No assistant ID returned", "response": assistant})

            # Step 1: Create thread
            async with session.post(_url("/threads"), headers=headers, json={}) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    return json.dumps({"error": f"Failed to create thread (HTTP {resp.status})", "detail": text[:500]})
                thread = await resp.json()
            thread_id = thread.get("id")
            if not thread_id:
                return json.dumps({"error": "No thread ID returned", "response": thread})

            # Step 2: Add user message
            async with session.post(_url(f"/threads/{thread_id}/messages"), headers=headers, json={"role": "user", "content": prompt}) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    return json.dumps({"error": f"Failed to add message (HTTP {resp.status})", "detail": text[:500]})

            # Step 3: Create run using the assistant ID from step 0
            async with session.post(_url(f"/threads/{thread_id}/runs"), headers=headers, json={"assistant_id": assistant_id}) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    return json.dumps({"error": f"Failed to create run (HTTP {resp.status})", "detail": text[:500]})
                run = await resp.json()
            run_id = run.get("id")
            if not run_id:
                return json.dumps({"error": "No run ID returned", "response": run})

            # Step 4: Poll until run completes (max 120s)
            import asyncio
            for _ in range(120):
                await asyncio.sleep(1)
                async with session.get(_url(f"/threads/{thread_id}/runs/{run_id}"), headers=headers) as resp:
                    if resp.status >= 400:
                        break
                    run_status = await resp.json()
                    status = run_status.get("status", "")
                    if status in ("completed", "failed", "cancelled", "expired"):
                        break
            if status == "failed":
                error = run_status.get("last_error") or run_status.get("error") or "Run failed"
                return json.dumps({"error": "Data Agent run failed", "detail": error})

            # Step 5: Get assistant messages
            async with session.get(_url(f"/threads/{thread_id}/messages"), headers=headers) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    return json.dumps({"error": f"Failed to get messages (HTTP {resp.status})", "detail": text[:500]})
                messages_payload = await resp.json()

            # Extract the last assistant message
            messages = messages_payload.get("data") or messages_payload.get("messages") or []
            assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
            if assistant_msgs:
                last_msg = assistant_msgs[-1]
                content = last_msg.get("content")
                if isinstance(content, list):
                    # OpenAI format: content is list of {type, text: {value}}
                    texts = [c.get("text", {}).get("value", "") if isinstance(c, dict) else str(c) for c in content]
                    return "\n".join(texts) or json.dumps(last_msg, indent=2)
                return content if isinstance(content, str) else json.dumps(last_msg, indent=2)
            return json.dumps({"message": "No assistant response received", "raw": messages_payload}, indent=2)

    @ai_function(
        name="get_fabric_item_definition",
        description=(
            "Get the Fabric item definition for a configured source when Fabric supports getDefinition. "
            "Useful for inspecting GraphQL API definitions and other Fabric item metadata."
        ),
    )
    async def get_fabric_item_definition(workspace_id: str, item_id: str, format: str = "") -> str:
        if not _can_get_fabric_item(workspace_id, item_id):
            return json.dumps({"error": "That Fabric item is not configured for this agent project."})
        suffix = f"?format={format}" if format else ""
        async with aiohttp.ClientSession() as session:
            payload = await _post_json(session, f"{FABRIC_API}/workspaces/{workspace_id}/items/{item_id}/getDefinition{suffix}", _fabric_headers(fabric_token), {})
            return json.dumps(payload, indent=2)

    @ai_function(
        name="get_semantic_model_metadata",
        description="Required first step for semantic-model questions. Get table, column, measure, relationship, and AI instruction metadata for a Fabric semantic model before answering or writing DAX.",
    )
    async def get_semantic_model_metadata(workspace_id: str, semantic_model_id: str) -> str:
        if not _can_query_semantic_model(workspace_id, semantic_model_id):
            return json.dumps({"error": "That semantic model is not configured for this agent project."})
        cached_metadata = _semantic_metadata_from_cache(workspace_id, semantic_model_id)
        if cached_metadata and cached_metadata.get("tables", {}).get("value"):
            return json.dumps(cached_metadata, indent=2)
        dataset_url = f"{POWERBI_API}/groups/{workspace_id}/datasets/{semantic_model_id}"
        async with aiohttp.ClientSession() as session:
            # Primary: DAX INFO queries (requires only Build permission, no special item permissions)
            tables = await _semantic_model_tables_from_dax(session, dataset_url, effective_powerbi_token)
            if not tables.get("errors") and tables.get("value"):
                return json.dumps({"workspace_id": workspace_id, "semantic_model_id": semantic_model_id, "tables": tables}, indent=2)

            dax_errors = tables.get("errors", [])

            # Fallback: getDefinition (optional, may require higher item permissions)
            definition_result: dict[str, Any] = {}
            definition_errors: list[dict[str, Any]] = []
            try:
                definition_result = await _semantic_model_tables_from_fabric_definition(session, workspace_id, semantic_model_id, fabric_token)
                if not definition_result.get("errors") and definition_result.get("value"):
                    result: dict[str, Any] = {"workspace_id": workspace_id, "semantic_model_id": semantic_model_id, "tables": definition_result}
                    if definition_result.get("relationships"):
                        result["relationships"] = definition_result.pop("relationships")
                    if definition_result.get("ai_instructions"):
                        result["ai_instructions"] = definition_result.pop("ai_instructions")
                    return json.dumps(result, indent=2)
                definition_errors = definition_result.get("errors", [])
            except Exception as e:
                logging.getLogger("hosted_agent_runtime").debug(f"getDefinition failed (optional): {e}")
                definition_errors = [{"message": str(e)}]

            # Last resort: REST /tables endpoint
            tables = await _get_json(session, f"{dataset_url}/tables", _powerbi_headers(effective_powerbi_token))
            if tables.get("value"):
                return json.dumps({"workspace_id": workspace_id, "semantic_model_id": semantic_model_id, "tables": tables}, indent=2)

            # All methods failed; return diagnostics
            diagnostics: list[dict[str, Any]] = []
            if dax_errors:
                diagnostics.append({"source": "executeQueries_DAX", "errors": dax_errors})
            if definition_errors:
                diagnostics.append({"source": "getDefinition", "errors": definition_errors})
            if tables.get("error") or tables.get("errors"):
                diagnostics.append({"source": "REST_tables", "errors": tables.get("errors") or [tables.get("error")]})
            tables["_diagnostics"] = diagnostics
            return json.dumps({"workspace_id": workspace_id, "semantic_model_id": semantic_model_id, "tables": tables}, indent=2)

    @ai_function(
        name="execute_dax_query",
        description=(
            "Execute one guarded read-only DAX query against a Fabric semantic model to produce a single result set. "
            "Use execute_dax_queries instead when the answer needs multiple result sets, comparisons, validations, breakdowns, totals plus details, or more than one EVALUATE statement. "
            "The query must start with EVALUATE. Write/admin commands are blocked."
        ),
    )
    async def execute_dax_query(workspace_id: str, semantic_model_id: str, dax_query: str) -> str:
        if not _can_query_semantic_model(workspace_id, semantic_model_id):
            return json.dumps({"errors": [{"message": "That semantic model is not configured for this agent project."}]})
        if not _is_readonly_dax(dax_query):
            return json.dumps({"errors": [{"message": "Only read-only DAX queries starting with EVALUATE are allowed."}]})
        result = await _execute_dax_user_query(effective_powerbi_token, workspace_id, semantic_model_id, dax_query)
        return json.dumps(result, indent=2)

    @ai_function(
        name="execute_dax_queries",
        description=(
            "Execute multiple guarded read-only DAX result sets in one semantic-model query operation when possible to produce data-backed answers. "
            "Prefer this tool for comparisons, validations, breakdowns, totals plus details, or any answer that needs more than one EVALUATE statement. "
            "Pass dax_queries_json as a JSON array of {name, query} objects, with each query starting with EVALUATE."
        ),
    )
    async def execute_dax_queries(workspace_id: str, semantic_model_id: str, dax_queries_json: str) -> str:
        if not _can_query_semantic_model(workspace_id, semantic_model_id):
            return json.dumps({"errors": [{"message": "That semantic model is not configured for this agent project."}]})
        try:
            queries = json.loads(dax_queries_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"errors": [{"message": f"dax_queries_json must be valid JSON: {exc}"}]})
        if not isinstance(queries, list) or not queries:
            return json.dumps({"errors": [{"message": "dax_queries_json must be a non-empty array."}]})
        if len(queries) > int(os.environ.get("POWERBI_DAX_MAX_RESULTSETS", "5")):
            return json.dumps({"errors": [{"message": "Too many DAX result sets requested."}]})
        normalized_queries = []
        for index, item in enumerate(queries):
            if not isinstance(item, dict):
                return json.dumps({"errors": [{"message": "Each DAX query item must be an object."}]})
            query = str(item.get("query") or "")
            if not _is_readonly_dax(query):
                return json.dumps({"errors": [{"message": f"Query {index + 1} is not an allowed read-only DAX query."}]})
            normalized_queries.append({"name": str(item.get("name") or f"result_{index + 1}"), "query": query})
        result = await _execute_dax_user_queries(effective_powerbi_token, workspace_id, semantic_model_id, normalized_queries)
        return json.dumps(result, indent=2)

    tools = [list_configured_fabric_data_sources]
    if allow_dynamic_fabric_items:
        tools.append(discover_accessible_fabric_items)
    if has_semantic_model_access:
        tools.extend([list_configured_semantic_models, get_semantic_model_metadata, execute_dax_query, execute_dax_queries])
    if graphql_sources or allow_dynamic_fabric_items:
        tools.extend([query_fabric_graphql, get_fabric_item_definition])
    elif data_sources:
        tools.append(get_fabric_item_definition)
    if sql_sources or allow_dynamic_fabric_items:
        tools.append(execute_fabric_sql_query)
    if data_agent_sources or allow_dynamic_fabric_items:
        tools.append(invoke_fabric_data_agent)
    if uses_fabric_mcp:
        tools.extend([call_fabric_mcp, call_fabric_mcp_tool])

    logger.info(
        "Runtime tools prepared: mode=%s, tools=%d, tool_names=%s, data_sources=%d, semantic_models=%d, graphql_sources=%d, sql_sources=%d, data_agent_sources=%d, uses_fabric_mcp=%s",
        mode,
        len(tools),
        ",".join(_tool_name(tool) for tool in tools),
        len(data_sources),
        len(semantic_models),
        len(graphql_sources),
        len(sql_sources),
        len(data_agent_sources),
        uses_fabric_mcp,
    )
    agent = ChatAgent(
        client,
        instructions=instructions,
        name=project.get("name", "Fabric Agent"),
        description=project.get("description", "A Fabric data agent."),
        tools=[tool_wrapper(t) for t in tools] if tool_wrapper else tools,
        default_options=default_options or None,
    )
    setattr(agent, "_fabric_runtime_tool_count", len(tools))
    return agent


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
        # Fallback: acquire tokens using the service principal credential
        try:
            cred = _credential()
            fabric_token = cred.get_token("https://api.fabric.microsoft.com/.default").token
            if not powerbi_token:
                powerbi_token = cred.get_token("https://analysis.windows.net/powerbi/api/.default").token
        except Exception:
            pass

    if not fabric_token:
        error_text = "A Fabric bearer token is required. The custom UX supplies this token, but the Foundry playground does not, and the service principal fallback failed."
        if body.get("stream") is True:
            return StreamingResponse(_stream_sse(error_text, conversation_id), media_type="text/event-stream")
        return _responses_payload(error_text, conversation_id, project)

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
    metadata_context = _preloaded_semantic_metadata_context(project)
    if metadata_context:
        message = f"{message}\n\n{metadata_context}"
    try:
        agent = _create_agent(project, fabric_token, powerbi_token, tool_wrapper=_make_tool_trace_wrapper(conversation_id))
        logger.info("Agent created: mode=%s, tools=%d", project.get("deployment_mode"), getattr(agent, "_fabric_runtime_tool_count", len(getattr(agent, "tools", []) or [])))
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
            result = _strip_progress_narration(result)
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
        result = _strip_progress_narration("".join(chunks))
        logger.info("Agent stream response length: %d chars", len(result))
        return result
    except Exception as exc:
        logger.exception("Agent run failed")
        return f"[Agent execution error: {exc}]"


async def _run_agent_traced(project: dict[str, Any], message: str, fabric_token: str, conversation_id: str, powerbi_token: str | None = None) -> dict[str, Any]:
    """Run agent locally with full tool-call tracing for Dev UI."""
    import logging
    logger = logging.getLogger("hosted_agent_runtime")
    tool_trace: list[dict[str, Any]] = []

    def _make_wrapper(tool_obj):
        """Wrap a FunctionTool's invoke method to capture calls for tracing."""
        tool_name = getattr(tool_obj, "name", None) or "unknown"
        original_invoke = tool_obj.invoke

        async def _traced_invoke(*args, **kwargs):
            # Extract the actual tool arguments from the SDK's invoke signature
            tool_args = kwargs.get("arguments")
            if hasattr(tool_args, "model_dump"):
                tool_args = tool_args.model_dump()
            elif hasattr(tool_args, "dict"):
                tool_args = tool_args.dict()
            elif not isinstance(tool_args, dict):
                tool_args = dict(tool_args) if tool_args else {}

            start = time.time()
            started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            try:
                result = await original_invoke(*args, **kwargs)
                elapsed = round(time.time() - start, 2)
                # Extract text from Content objects returned by invoke()
                if isinstance(result, list):
                    texts = [getattr(item, "text", None) for item in result if getattr(item, "text", None)]
                    result_str = "\n".join(texts) if texts else str(result)
                else:
                    result_str = str(result) if result is not None else ""
                result_preview = result_str[:2000] + "..." if len(result_str) > 2000 else result_str
                tool_trace.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "result_preview": result_preview,
                    "elapsed_seconds": elapsed,
                    "status": "success",
                    "timestamp": started_at,
                })
                return result
            except Exception as exc:
                elapsed = round(time.time() - start, 2)
                tool_trace.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "error": str(exc),
                    "elapsed_seconds": elapsed,
                    "status": "error",
                    "timestamp": started_at,
                })
                raise

        # Monkey-patch the invoke method on the tool object
        tool_obj.invoke = _traced_invoke
        return tool_obj

    try:
        agent = _create_agent(project, fabric_token, powerbi_token, tool_wrapper=_make_wrapper)
        logger.info("Agent created (traced): mode=%s, tools=%d", project.get("deployment_mode"), getattr(agent, "_fabric_runtime_tool_count", len(getattr(agent, "tools", []) or [])))
    except Exception as exc:
        logger.exception("Failed to create agent")
        return {"response": f"[Agent creation error: {exc}]", "tool_calls": tool_trace, "error": True}

    thread = _get_or_create_thread(conversation_id)
    metadata_context = _preloaded_semantic_metadata_context(project)
    if metadata_context:
        message = f"{message}\n\n{metadata_context}"
    try:
        if thread is not None:
            response = await agent.run(message, session=thread)
        else:
            response = await agent.run(message)
        result = _agent_response_text(response)
        result = _strip_final_answer_marker(result)
        logger.info("Agent traced response length: %d chars, tool_calls: %d", len(result), len(tool_trace))
        return {"response": result, "tool_calls": tool_trace}
    except Exception as exc:
        logger.exception("Agent run failed (traced)")
        return {"response": f"[Agent execution error: {exc}]", "tool_calls": tool_trace, "error": True}


async def _run_agent_sse(project: dict[str, Any], message: str, fabric_token: str, conversation_id: str, powerbi_token: str | None = None):
    import logging
    logger = logging.getLogger("hosted_agent_runtime")
    response_id = f"resp_{uuid.uuid4().hex}"
    yield f"event: response.created\ndata: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'status': 'in_progress'}})}\n\n"

    metadata_context = _preloaded_semantic_metadata_context(project)
    if metadata_context:
        message = f"{message}\n\n{metadata_context}"

    try:
        agent = _create_agent(project, fabric_token, powerbi_token, tool_wrapper=_make_tool_trace_wrapper(conversation_id))
        logger.info("Agent created for stream: mode=%s, tools=%d", project.get("deployment_mode"), getattr(agent, "_fabric_runtime_tool_count", len(getattr(agent, "tools", []) or [])))
    except Exception as exc:
        logger.exception("Failed to create streaming agent")
        async for event in _stream_sse(f"[Agent creation error: {exc}]", conversation_id):
            yield event
        return

    thread = _get_or_create_thread(conversation_id)
    full_response: list[str] = []
    filter_progress = True
    final_answer_started = not filter_progress
    buffered_text = ""
    final_marker = "FINAL_ANSWER_START"

    def _visible_stream_text(text: str) -> str:
        nonlocal buffered_text, final_answer_started
        if not filter_progress:
            return _strip_final_answer_marker(text)
        if final_answer_started:
            return _strip_final_answer_marker(text)
        buffered_text += text
        if final_marker not in buffered_text:
            return ""
        _, after_marker = buffered_text.split(final_marker, 1)
        buffered_text = ""
        final_answer_started = True
        return after_marker.lstrip(" :\r\n\t")

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

        iterator = stream.__aiter__()
        pending_update = asyncio.create_task(iterator.__anext__())
        last_update_at = time.time()
        keepalive_seconds = _agent_stream_keepalive_seconds()
        idle_timeout_seconds = _agent_stream_idle_timeout_seconds()
        timed_out = False

        while True:
            done, _ = await asyncio.wait({pending_update}, timeout=keepalive_seconds)
            if not done:
                idle_seconds = round(time.time() - last_update_at)
                yield f": keepalive idle_seconds={idle_seconds}\n\n"
                if idle_seconds >= idle_timeout_seconds:
                    logger.error("Agent stream idle timeout after %s seconds", idle_seconds)
                    pending_update.cancel()
                    timed_out = True
                    break
                continue
            try:
                update = pending_update.result()
            except StopAsyncIteration:
                break
            last_update_at = time.time()
            pending_update = asyncio.create_task(iterator.__anext__())
            contents = getattr(update, "contents", None) or []
            for content in contents:
                text = ""
                if TextContent is not None and isinstance(content, TextContent) and content.text:
                    text = content.text
                elif hasattr(content, "text") and content.text:
                    text = content.text
                if text:
                    visible_text = _visible_stream_text(text)
                    if visible_text:
                        full_response.append(visible_text)
                        yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': visible_text})}\n\n"

        if timed_out:
            error_text = "[Agent stream timed out waiting for more model output. Try a narrower request or check hosted runtime logs for the last tool call.]"
            yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': error_text})}\n\n"
        elif filter_progress and not final_answer_started and buffered_text.strip():
            fallback_text = _strip_progress_narration(buffered_text)
            logger.warning("Agent stream ended without FINAL_ANSWER_START marker; emitting cleaned buffered text length=%d", len(fallback_text))
            full_response.append(fallback_text)
            yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': fallback_text})}\n\n"

        logger.info("Agent streamed response length: %d chars", len("".join(full_response)))
        yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': {'id': response_id, 'status': 'completed'}})}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as exc:
        logger.exception("Agent stream failed")
        error_text = f"[Agent execution error: {exc}]"
        yield f"event: response.output_text.delta\ndata: {json.dumps({'type': 'response.output_text.delta', 'delta': error_text})}\n\n"
        yield f"event: response.completed\ndata: {json.dumps({'type': 'response.completed', 'response': {'id': response_id, 'status': 'completed'}})}\n\n"
        yield "data: [DONE]\n\n"


def _tool_arguments(kwargs: dict[str, Any]) -> dict[str, Any]:
    tool_args = kwargs.get("arguments")
    if hasattr(tool_args, "model_dump"):
        tool_args = tool_args.model_dump()
    elif hasattr(tool_args, "dict"):
        tool_args = tool_args.dict()
    elif not isinstance(tool_args, dict):
        tool_args = dict(tool_args) if tool_args else {}
    return {key: value for key, value in tool_args.items() if "token" not in key.lower() and "authorization" not in key.lower()}


def _tool_result_preview(result: Any, limit: int = 1000) -> str:
    if isinstance(result, list):
        texts = [getattr(item, "text", None) for item in result if getattr(item, "text", None)]
        result_str = "\n".join(texts) if texts else str(result)
    else:
        result_str = str(result) if result is not None else ""
    return result_str[:limit] + "..." if len(result_str) > limit else result_str


def _make_tool_trace_wrapper(conversation_id: str):
    logger = logging.getLogger("hosted_agent_runtime.traces")

    def _wrap(tool_obj: Any):
        tool_name = getattr(tool_obj, "name", None) or "unknown"
        original_invoke = tool_obj.invoke

        async def _traced_invoke(*args, **kwargs):
            tool_args = _tool_arguments(kwargs)
            start = time.time()
            logger.info(
                "LLM tool call started: conversation_id=%s tool=%s args=%s",
                conversation_id,
                tool_name,
                json.dumps(tool_args, default=str)[:1000],
            )
            try:
                result = await original_invoke(*args, **kwargs)
                elapsed_ms = round((time.time() - start) * 1000)
                logger.info(
                    "LLM tool call completed: conversation_id=%s tool=%s status=success elapsed_ms=%s result_preview=%s",
                    conversation_id,
                    tool_name,
                    elapsed_ms,
                    _tool_result_preview(result),
                )
                return result
            except Exception as exc:
                elapsed_ms = round((time.time() - start) * 1000)
                logger.exception(
                    "LLM tool call failed: conversation_id=%s tool=%s status=error elapsed_ms=%s error=%s",
                    conversation_id,
                    tool_name,
                    elapsed_ms,
                    exc,
                )
                raise

        tool_obj.invoke = _traced_invoke
        return tool_obj

    return _wrap


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
    mode = project.get("deployment_mode")
    if mode == "standalone":
        model_config = project.get("standalone_agent", {}).get("model_config")
    elif mode == "orchestrator_only":
        model_config = project.get("orchestrator_only", {}).get("model_config")
    else:
        model_config = project.get("orchestrator", {}).get("model_config")
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": _resolve_model_deployment(model_config),
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
        if response.status == 202:
            location = response.headers.get("Location") or response.headers.get("Operation-Location") or payload.get("operationUrl") or payload.get("location")
            if location:
                return await _poll_fabric_operation(session, location, headers)
            return {"errors": [{"status": 202, "url": url, "message": "Accepted but no operation URL returned", "body": payload}]}
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
    relationships: list[dict[str, Any]] = []
    for part in parts:
        path = str(part.get("path") or "")
        if not path.endswith(".tmdl"):
            continue
        text = _decode_definition_payload(part)
        if text:
            tables.extend(_tables_from_tmdl(text))
            relationships.extend(_relationships_from_tmdl(text))
    ai_instructions = _ai_instructions_from_parts(parts)
    result: dict[str, Any] = {"value": _merge_tables(tables), "source": "fabricDefinition"}
    if relationships:
        result["relationships"] = relationships
    if ai_instructions:
        result["ai_instructions"] = ai_instructions
    return result


async def _discover_accessible_fabric_items(fabric_token: str, search_text: str = "", source_type: str = "") -> dict[str, Any]:
    search_lower = search_text.lower().strip()
    source_type = source_type.strip().lower()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    async with aiohttp.ClientSession() as session:
        workspaces_payload = await _get_json(session, f"{FABRIC_API}/workspaces", _fabric_headers(fabric_token))
        if "errors" in workspaces_payload:
            return {"items": [], "errors": workspaces_payload["errors"]}
        for workspace in workspaces_payload.get("value", []):
            workspace_id = workspace.get("id")
            if not workspace_id:
                continue
            items_payload = await _get_json(session, f"{FABRIC_API}/workspaces/{workspace_id}/items", _fabric_headers(fabric_token))
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
                score = _score(search_lower, name, workspace_name, item_type, mapped_type.replace("_", " "))
                if search_lower and score == 0:
                    continue
                results.append(
                    {
                        "source_type": mapped_type,
                        "workspace_id": workspace_id,
                        "workspace_name": workspace_name,
                        "item_id": item.get("id"),
                        "item_name": name,
                        "semantic_model_id": item.get("id") if mapped_type == SEMANTIC_SOURCE_TYPE else "",
                        "semantic_model_name": name if mapped_type == SEMANTIC_SOURCE_TYPE else "",
                        "type": item_type,
                        "score": score,
                    }
                )
    results.sort(key=lambda model: model.get("score", 0), reverse=True)
    return {"items": results[:25], "errors": errors}


def _map_fabric_item_type(item_type: str) -> str:
    return FABRIC_ITEM_TYPE_MAP.get(item_type) or FABRIC_ITEM_TYPE_NORMALIZED_MAP.get(re.sub(r"[^a-z0-9]", "", item_type.lower()), "")


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
    async with session.post(f"{dataset_url}/executeQueries", headers=_powerbi_headers(powerbi_token), json=body) as response:
        text = await response.text()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        if response.status >= 400:
            return {"errors": [{"status": response.status, "url": f"{dataset_url}/executeQueries", "message": payload, "query": query}]}
        return payload


async def _execute_dax_user_query(powerbi_token: str, workspace_id: str, semantic_model_id: str, dax_query: str) -> dict[str, Any]:
    result = await _execute_dax_user_queries(powerbi_token, workspace_id, semantic_model_id, [{"name": "result", "query": dax_query}])
    if result.get("errors"):
        return result
    result_set = (result.get("result_sets") or [{}])[0]
    return {"query": dax_query, "endpoint": result.get("endpoint"), "row_count": result_set.get("row_count", 0), "rows": result_set.get("rows", [])}


async def _execute_dax_user_queries(powerbi_token: str, workspace_id: str, semantic_model_id: str, dax_queries: list[dict[str, str]]) -> dict[str, Any]:
    start = time.time()
    query_count = len(dax_queries)
    use_arrow = os.environ.get("POWERBI_DAX_EXECUTION_MODE", "arrow").lower() == "arrow"
    _log_dax_trace(
        "started",
        workspace_id=workspace_id,
        semantic_model_id=semantic_model_id,
        query_count=query_count,
        execution_mode="arrow" if use_arrow and arrow_ipc is not None else "json",
        query_preview=_dax_query_preview(dax_queries),
    )
    with _dax_span(workspace_id, semantic_model_id, query_count):
        try:
            if use_arrow and arrow_ipc is not None:
                arrow_result = await _execute_dax_arrow(powerbi_token, workspace_id, semantic_model_id, dax_queries)
                if not arrow_result.get("errors") or os.environ.get("POWERBI_DAX_ARROW_FALLBACK_JSON", "true").lower() != "true":
                    _log_dax_result("completed", workspace_id, semantic_model_id, arrow_result, query_count, start)
                    return arrow_result
            result_sets = []
            dataset_url = f"{POWERBI_API}/groups/{workspace_id}/datasets/{semantic_model_id}"
            async with aiohttp.ClientSession() as session:
                for item in dax_queries:
                    payload = await _execute_dax(session, dataset_url, powerbi_token, item["query"])
                    if payload.get("errors"):
                        result = {"endpoint": "executeQueries", "errors": payload["errors"], "result_sets": result_sets}
                        _log_dax_result("failed", workspace_id, semantic_model_id, result, query_count, start)
                        return result
                    rows = _query_rows(payload)
                    result_sets.append({"name": item["name"], "query": item["query"], "row_count": len(rows), "rows": rows[:_max_dax_rows()]})
            result = {"endpoint": "executeQueries", "result_sets": result_sets}
            _log_dax_result("completed", workspace_id, semantic_model_id, result, query_count, start)
            return result
        except Exception:
            _log_dax_result("failed", workspace_id, semantic_model_id, {"errors": [{"message": "Unhandled DAX execution exception"}]}, query_count, start)
            raise


def _dax_query_preview(dax_queries: list[dict[str, str]], limit: int = 750) -> str:
    preview = "\n\n".join(str(item.get("query") or "").strip() for item in dax_queries)
    return preview[:limit] + "..." if len(preview) > limit else preview


def _dax_row_count(result: dict[str, Any]) -> int:
    return sum(int(item.get("row_count") or 0) for item in result.get("result_sets") or [] if isinstance(item, dict))


def _log_dax_trace(event: str, **fields: Any) -> None:
    logging.getLogger("hosted_agent_runtime.dax").info(
        "DAX tool call %s: %s",
        event,
        " ".join(f"{key}={json.dumps(value, default=str)}" for key, value in fields.items()),
    )


def _log_dax_result(event: str, workspace_id: str, semantic_model_id: str, result: dict[str, Any], query_count: int, start: float) -> None:
    _log_dax_trace(
        event,
        workspace_id=workspace_id,
        semantic_model_id=semantic_model_id,
        endpoint=result.get("endpoint", ""),
        query_count=query_count,
        row_count=_dax_row_count(result),
        status="error" if result.get("errors") else "success",
        elapsed_ms=round((time.time() - start) * 1000),
        error_count=len(result.get("errors") or []),
    )


def _dax_span(workspace_id: str, semantic_model_id: str, query_count: int):
    try:
        from opentelemetry import trace
        return trace.get_tracer("hosted_agent_runtime.dax").start_as_current_span(
            "fabric.dax.execute",
            attributes={
                "fabric.workspace_id": workspace_id,
                "fabric.semantic_model_id": semantic_model_id,
                "fabric.dax.query_count": query_count,
            },
        )
    except Exception:
        from contextlib import nullcontext
        return nullcontext()


async def _execute_dax_arrow(powerbi_token: str, workspace_id: str, semantic_model_id: str, dax_queries: list[dict[str, str]]) -> dict[str, Any]:
    dax_script = "\n\n".join(item["query"].strip() for item in dax_queries)
    url = f"{POWERBI_API}/groups/{workspace_id}/datasets/{semantic_model_id}/executeDaxQueries"
    body = {"query": dax_script}
    headers = {**_powerbi_headers(powerbi_token), "Accept": "application/vnd.apache.arrow.stream"}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=body) as response:
            content = await response.read()
            if response.status >= 400:
                try:
                    message = json.loads(content.decode("utf-8")) if content else {}
                except Exception:
                    message = {"raw": content[:1000].decode("utf-8", errors="ignore")}
                return {"endpoint": "executeDaxQueries", "errors": [{"status": response.status, "message": message}]}
    try:
        reader = arrow_ipc.open_stream(content)  # type: ignore[union-attr]
        result_sets = []
        for index, batch in enumerate(reader):
            table = batch.to_pydict()
            column_names = list(table.keys())
            rows = [dict(zip(column_names, values)) for values in zip(*table.values())]
            query = dax_queries[min(index, len(dax_queries) - 1)]
            result_sets.append({"name": query["name"], "query": query["query"], "row_count": len(rows), "rows": rows[:_max_dax_rows()]})
        return {"endpoint": "executeDaxQueries", "result_sets": result_sets}
    except Exception as exc:
        return {"endpoint": "executeDaxQueries", "errors": [{"message": f"Failed to decode Arrow response: {exc}"}]}


def _max_dax_rows() -> int:
    return max(1, min(int(os.environ.get("POWERBI_DAX_MAX_ROWS_PER_RESULT", "50")), 500))


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
    """Parse TMDL text extracting tables, columns (with types/descriptions), and measures (with expressions/descriptions)."""
    tables: list[dict[str, Any]] = []
    current_table: dict[str, Any] | None = None
    current_item: dict[str, Any] | None = None  # current column or measure being parsed
    item_type: str = ""  # "column" or "measure"
    indent_stack: int = 0  # track indent level for multi-line expressions

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw_line = lines[i]
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())

        if not stripped or stripped.startswith("//"):
            i += 1
            continue

        # Table declaration
        if stripped.startswith("table "):
            current_table = {
                "name": _clean_tmdl_name(stripped.removeprefix("table ")),
                "description": "",
                "isHidden": False,
                "columns": [],
                "measures": [],
            }
            tables.append(current_table)
            current_item = None
            item_type = ""
            i += 1
            continue

        # Column declaration: "column Name = dataType"
        if current_table is not None and stripped.startswith("column "):
            col_def = stripped.removeprefix("column ")
            # Format: 'Column Name' = DataType  or  ColumnName = DataType
            parts = re.split(r"\s*=\s*", col_def, maxsplit=1)
            col_name = parts[0].strip().strip("'").strip('"')
            data_type = parts[1].strip() if len(parts) > 1 else ""
            current_item = {"name": col_name, "dataType": data_type, "description": "", "isHidden": False}
            current_table["columns"].append(current_item)
            item_type = "column"
            indent_stack = indent
            i += 1
            continue

        # Measure declaration: "measure Name = expression" (may be multi-line with ```)
        if current_table is not None and stripped.startswith("measure "):
            meas_def = stripped.removeprefix("measure ")
            parts = re.split(r"\s*=\s*", meas_def, maxsplit=1)
            meas_name = parts[0].strip().strip("'").strip('"')
            expression = parts[1].strip() if len(parts) > 1 else ""
            # Multi-line expression block
            if expression == "```" or expression.startswith("```"):
                expr_lines: list[str] = []
                if expression != "```":
                    expr_lines.append(expression.removeprefix("```"))
                i += 1
                while i < len(lines):
                    el = lines[i]
                    if el.strip() == "```":
                        i += 1
                        break
                    expr_lines.append(el)
                    i += 1
                expression = "\n".join(expr_lines).strip()
            current_item = {"name": meas_name, "expression": expression, "description": "", "isHidden": False}
            current_table["measures"].append(current_item)
            item_type = "measure"
            indent_stack = indent
            i += 1
            continue

        # Properties of current item or table
        if stripped.startswith("description:") or stripped.startswith("description ="):
            desc_value = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip()
            # Multi-line description block
            if desc_value == "```" or desc_value.startswith("```"):
                desc_lines: list[str] = []
                if desc_value != "```":
                    desc_lines.append(desc_value.removeprefix("```"))
                i += 1
                while i < len(lines):
                    dl = lines[i]
                    if dl.strip() == "```":
                        i += 1
                        break
                    desc_lines.append(dl.strip())
                    i += 1
                desc_value = " ".join(desc_lines).strip()
            else:
                desc_value = desc_value.strip("'\"")
            if current_item is not None:
                current_item["description"] = desc_value
            elif current_table is not None:
                current_table["description"] = desc_value
            i += 1
            continue

        if stripped.startswith("isHidden"):
            is_hidden = "true" in stripped.lower()
            if current_item is not None:
                current_item["isHidden"] = is_hidden
            elif current_table is not None:
                current_table["isHidden"] = is_hidden
            i += 1
            continue

        if stripped.startswith("dataType:") or stripped.startswith("dataType ="):
            dt_value = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip()
            if current_item is not None and item_type == "column":
                current_item["dataType"] = dt_value
            i += 1
            continue

        if stripped.startswith("expression:") or stripped.startswith("expression ="):
            expr_val = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip()
            if expr_val == "```" or expr_val.startswith("```"):
                expr_lines2: list[str] = []
                if expr_val != "```":
                    expr_lines2.append(expr_val.removeprefix("```"))
                i += 1
                while i < len(lines):
                    el2 = lines[i]
                    if el2.strip() == "```":
                        i += 1
                        break
                    expr_lines2.append(el2)
                    i += 1
                expr_val = "\n".join(expr_lines2).strip()
            if current_item is not None and item_type == "measure":
                current_item["expression"] = expr_val
            i += 1
            continue

        i += 1
    return tables


def _relationships_from_tmdl(text: str) -> list[dict[str, Any]]:
    """Parse TMDL relationship definitions."""
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
            if stripped.startswith("fromColumn:") or stripped.startswith("fromColumn ="):
                current["fromColumn"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'\"")
            elif stripped.startswith("toColumn:") or stripped.startswith("toColumn ="):
                current["toColumn"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'\"")
            elif stripped.startswith("fromTable:") or stripped.startswith("fromTable ="):
                current["fromTable"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'\"")
            elif stripped.startswith("toTable:") or stripped.startswith("toTable ="):
                current["toTable"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'\"")
            elif stripped.startswith("fromCardinality:") or stripped.startswith("fromCardinality ="):
                current["fromCardinality"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'\"")
            elif stripped.startswith("toCardinality:") or stripped.startswith("toCardinality ="):
                current["toCardinality"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'\"")
            elif stripped.startswith("crossFilteringBehavior:") or stripped.startswith("crossFilteringBehavior ="):
                current["crossFilteringBehavior"] = re.split(r"[:=]\s*", stripped, maxsplit=1)[-1].strip().strip("'\"")
    return relationships


def _ai_instructions_from_parts(parts: list[dict[str, Any]]) -> str:
    """Extract AI instructions / linguistic schema from definition parts."""
    for part in parts:
        path = str(part.get("path") or "")
        # Look for linguistic schema or AI instructions annotation parts
        if "linguisticSchema" in path.lower() or "linguistic" in path.lower():
            text = _decode_definition_payload(part)
            if text:
                return text
    # Also check model.tmdl for __PBI_AIInstructions annotation
    for part in parts:
        path = str(part.get("path") or "")
        if path.endswith("model.tmdl") or path == "model.tmdl":
            text = _decode_definition_payload(part)
            if text:
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
        if name not in merged:
            merged[name] = {"name": name, "description": table.get("description", ""), "isHidden": table.get("isHidden", False), "columns": [], "measures": []}
        target = merged[name]
        if not target.get("description") and table.get("description"):
            target["description"] = table["description"]
        target["columns"].extend(table.get("columns", []))
        target["measures"].extend(table.get("measures", []))
    return list(merged.values())


def _score(search_text: str, model_name: str, workspace_name: str, *extra_fields: str) -> int:
    if not search_text:
        return 1
    combined = " ".join([model_name, workspace_name, *extra_fields]).lower()
    terms = [term for term in search_text.replace("_", " ").replace("-", " ").split() if len(term) > 2]
    return sum(10 for term in terms if term in combined)


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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8088"))
    uvicorn.run(app, host="0.0.0.0", port=port)
