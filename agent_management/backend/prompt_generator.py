from __future__ import annotations

import os
from typing import Any

from azure.identity import ClientSecretCredential, DefaultAzureCredential, get_bearer_token_provider
from openai import AsyncOpenAI

from .fabric_semantic import execute_readonly_dax, semantic_model_metadata
from .models import PromptGenerationRequest, SemanticModelRef, AgentProject, SubagentConfig
from .project_store import create_project_store


def _source_id(source: SemanticModelRef) -> str:
    return source.item_id


def _source_name(source: SemanticModelRef) -> str:
    return source.item_name


def _source_label(source: SemanticModelRef) -> str:
    label = source.source_type.replace("_", " ").title()
    name = source.item_name or "No data source selected"
    workspace = f" in workspace {source.workspace_name}" if source.workspace_name else ""
    return f"{name} ({label}){workspace}"


def _subagent_prompt_context(project: AgentProject) -> list[dict[str, str]]:
    return [
        {
            "name": subagent.name,
            "description": subagent.description,
            "source_type": subagent.data_source.source_type,
            "data_source": _source_label(subagent.data_source),
            "prompt": subagent.prompt,
        }
        for subagent in project.orchestrator.subagents
    ]


def _has_data_agent_subagent(project: AgentProject) -> bool:
    return any(subagent.data_source.source_type == "data_agent" for subagent in project.orchestrator.subagents)


def _data_agent_prompt_guidance(project: AgentProject) -> str:
    if not _has_data_agent_subagent(project):
        return ""
    return (
        "\nFabric Data Agent routing rule: If a linked subagent uses a Fabric Data Agent data source, "
        "broad analytical questions about that configured source are in scope for that subagent. "
        "Questions about historical trends, summaries, or 'all my data' should be routed to the Fabric Data Agent subagent instead of refused as too broad. "
        "The hosted runtime can invoke the configured Fabric Data Agent tool, so the orchestrator should ask that subagent/tool first and only refuse after the tool indicates it cannot answer."
    )


def _credential():
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("APP_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("APP_CLIENT_SECRET") or os.environ.get("AZURE_CLIENT_SECRET")
    if tenant_id and client_id and client_secret:
        return ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _foundry_openai_base_url() -> str:
    configured_base_url = os.environ.get("FOUNDRY_OPENAI_BASE_URL")
    if configured_base_url:
        return configured_base_url.rstrip("/") + "/"
    project_endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
    return f"{project_endpoint}/openai/v1/"


def _client() -> AsyncOpenAI:
    api_key = os.environ.get("AOAI_KEY") or os.environ.get("AZURE_OPENAI_API_KEY")
    if api_key:
        return AsyncOpenAI(base_url=_foundry_openai_base_url(), api_key=api_key)
    token_provider = get_bearer_token_provider(_credential(), "https://ai.azure.com/.default")

    async def async_token_provider() -> str:
        return token_provider()

    return AsyncOpenAI(base_url=_foundry_openai_base_url(), api_key=async_token_provider)


async def generate_prompt(request: PromptGenerationRequest) -> dict[str, str]:
    project = request.project
    system = "You generate concise, production-ready Microsoft Foundry hosted-agent prompts for Fabric semantic model agents."
    user = _prompt_request_text(project, request)
    content = await _chat_completion_text([{"role": "system", "content": system}, {"role": "user", "content": user}])
    return {"prompt": content.strip()}


async def dev_chat(
    project: AgentProject,
    message: str,
    history: list[dict[str, str]],
    powerbi_token: str | None = None,
    fabric_token: str | None = None,
) -> dict[str, Any]:
    selected_agent, selected_model = _select_dev_target(project, message)
    trace: list[dict[str, Any]] = [
        {"step": "request", "status": "complete", "detail": message},
        {
            "step": "route",
            "status": "complete",
            "detail": f"Selected {selected_agent} for {_source_name(selected_model) or 'no data source selected'}.",
        },
    ]
    metadata: dict[str, Any] = {}
    query_result: dict[str, Any] = {}
    if selected_model.source_type == "semantic_model" and selected_model.workspace_id and _source_id(selected_model) and powerbi_token:
        metadata = await semantic_model_metadata(powerbi_token, selected_model.workspace_id, _source_id(selected_model), fabric_token)
        metadata_summary = _metadata_summary(metadata)
        metadata_status = "warning" if metadata_summary.get("errors") else "complete"
        trace.append(
            {
                "step": "metadata",
                "status": metadata_status,
                "detail": f"Loaded metadata for {_source_name(selected_model)}.",
                "data": metadata_summary,
            }
        )
        dax_query = _sample_query_for_message(message, metadata)
        if dax_query:
            query_result = await execute_readonly_dax(powerbi_token, selected_model.workspace_id, _source_id(selected_model), dax_query)
            trace.append(
                {
                    "step": "dax",
                    "status": "warning" if query_result.get("errors") else "complete",
                    "detail": "Executed a read-only DAX sample query against the selected semantic model.",
                    "data": _query_result_summary(query_result),
                }
            )
    else:
        trace.append(
            {
                "step": "metadata",
                "status": "skipped",
                "detail": "Semantic metadata is only loaded for semantic model sources with Power BI access.",
            }
        )

    system = _dev_system_prompt(project, selected_agent, selected_model, metadata, query_result)
    messages = [{"role": "system", "content": system}, *_chat_history_messages(history[-12:]), {"role": "user", "content": message}]
    trace.append({"step": "prompt", "status": "complete", "detail": "Built the agent prompt with project configuration and available semantic metadata."})
    answer = await _chat_completion_text(messages)
    trace.append({"step": "model", "status": "complete", "detail": "Foundry model returned the Dev UI response."})
    return {
        "response": answer.strip(),
        "debug": {
            "mode": project.deployment_mode,
            "selected_agent": selected_agent,
            "semantic_model": selected_model.model_dump(),
            "trace": trace,
        },
    }


async def _chat_completion_text(messages: list[dict[str, str]]) -> str:
    response = await _client().chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"],
        messages=messages,
    )
    if not response.choices:
        return ""
    return response.choices[0].message.content or ""


def _chat_history_messages(history: list[dict[str, str]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for item in history:
        role = str(item.get("role") or "user").strip().lower()
        content = str(item.get("content") or "").strip()
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})
    return messages


def _resolve_external_agent_details(project: AgentProject) -> str:
    """Look up each external agent's project and return a rich summary including their prompts."""
    store = create_project_store()
    details: list[str] = []
    for ext in project.orchestrator_only.external_agents:
        section = f"### {ext.display_name or ext.agent_name}\n- Agent name: {ext.agent_name}\n- Description: {ext.description or 'No description'}"
        if ext.project_id:
            try:
                ref_project = store.get_project(ext.project_id)
                if ref_project.deployment_mode == "standalone":
                    agent = ref_project.standalone_agent
                    section += f"\n- Standalone agent name: {agent.name}"
                    section += f"\n- Agent description: {agent.description}" if agent.description else ""
                    if agent.data_source.item_name:
                        section += f"\n- Data source: {agent.data_source.item_name} ({agent.data_source.source_type})"
                    if agent.prompt:
                        prompt_preview = agent.prompt[:2000]
                        section += f"\n- Agent prompt:\n{prompt_preview}"
                elif ref_project.deployment_mode == "orchestrator":
                    orch = ref_project.orchestrator
                    section += f"\n- Orchestrator name: {orch.name}"
                    section += f"\n- Orchestrator description: {orch.description}" if orch.description else ""
                    subagent_names = [s.name for s in orch.subagents]
                    section += f"\n- Subagents: {', '.join(subagent_names)}" if subagent_names else ""
                    if orch.prompt:
                        prompt_preview = orch.prompt[:2000]
                        section += f"\n- Orchestrator prompt:\n{prompt_preview}"
            except Exception:
                section += "\n- (Could not load project details)"
        details.append(section)
    return "\n\n".join(details) if details else "No external agents configured."


def _prompt_request_text(project: AgentProject, request: PromptGenerationRequest) -> str:
    metadata = request.semantic_metadata or {}
    if request.target == "orchestrator_only":
        agent_details = _resolve_external_agent_details(project)
        return f"""
Create an orchestrator prompt for a Fabric agent that delegates to existing deployed standalone agents.
Project: {project.name}
Description: {project.description}

External agents with their full details:
{agent_details}

Additional instructions: {request.instructions}
The orchestrator should route user questions to the appropriate external agent based on each agent's capabilities and prompt details. It should ask clarifying questions when routing is ambiguous, combine results from multiple agents when appropriate, and never invent data.
""".strip()
    if request.target == "orchestrator":
        return f"""
Create an orchestrator prompt for a Fabric agent system.
Project: {project.name}
Description: {project.description}
Linked subagents with names, descriptions, data sources, and associated prompts:
{_subagent_prompt_context(project)}
Additional instructions: {request.instructions}
The orchestrator should route to the linked subagents based on their names, descriptions, data sources, and prompts. It should ask clarifying questions when routing is ambiguous, preserve each subagent's responsibility boundaries, and never invent data.
{_data_agent_prompt_guidance(project)}
""".strip()
    if request.target == "standalone":
        agent = project.standalone_agent
        return f"""
Create a standalone Fabric semantic model agent prompt.
Agent: {agent.name}
Description: {agent.description}
Data source: {_source_label(agent.data_source)}
Metadata: {metadata}
Additional instructions: {request.instructions}
For semantic models, the agent must inspect metadata, write read-only DAX, execute it, and answer only from results. For other Fabric source types, the agent must use the configured Fabric tools and answer only from tool results.
""".strip()
    subagent = next((candidate for candidate in project.orchestrator.subagents if candidate.id == request.subagent_id), None)
    if subagent is None:
        raise ValueError("subagent_id is required for subagent prompt generation.")
    return f"""
Create a subagent prompt for a Fabric semantic model agent.
Project: {project.name}
Subagent: {subagent.name}
Description: {subagent.description}
Data source: {_source_label(subagent.data_source)}
Metadata: {metadata}
Guardrails: {subagent.guardrails}
Additional instructions: {request.instructions}
The subagent must answer only questions appropriate for its selected Fabric data source. If the source is a Fabric Data Agent, broad analytical questions about that configured source, including trends and summaries across the data agent's data, are in scope. Invoke that configured Fabric Data Agent through the available tool before refusing or narrowing the request.
""".strip()


def _select_dev_target(project: AgentProject, message: str) -> tuple[str, SemanticModelRef]:
    if project.deployment_mode == "standalone":
        return project.standalone_agent.name, project.standalone_agent.data_source
    normalized_message = message.lower()
    candidates: list[SubagentConfig] = [
        subagent for subagent in project.orchestrator.subagents if _source_id(subagent.data_source)
    ]
    for subagent in candidates:
        searchable = f"{subagent.name} {subagent.description} {_source_label(subagent.data_source)}".lower()
        if any(term and term in searchable for term in normalized_message.replace("?", " ").split()):
            return subagent.name, subagent.data_source
    if candidates:
        return candidates[0].name, candidates[0].data_source
    return project.orchestrator.name, SemanticModelRef()


def _metadata_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    tables = metadata.get("tables", {}).get("value", [])
    return {
        "dataset": metadata.get("dataset", {}).get("name") or metadata.get("dataset", {}).get("displayName"),
        "source": metadata.get("tables", {}).get("source"),
        "tables": [table.get("name") for table in tables[:12] if table.get("name")],
        "table_count": len(tables),
        "errors": metadata.get("dataset", {}).get("errors") or metadata.get("tables", {}).get("errors"),
    }


def _metadata_prompt_context(metadata: dict[str, Any]) -> dict[str, Any]:
    tables = metadata.get("tables", {}).get("value", [])
    compact_tables = []
    for table in tables[:20]:
        compact_tables.append(
            {
                "name": table.get("name"),
                "columns": [column.get("name") for column in table.get("columns", [])[:30] if column.get("name")],
                "measures": [measure.get("name") for measure in table.get("measures", [])[:30] if measure.get("name")],
            }
        )
    return {**_metadata_summary(metadata), "tables_detail": compact_tables}


def _sample_query_for_message(message: str, metadata: dict[str, Any]) -> str:
    normalized_message = message.lower()
    if not any(term in normalized_message for term in ["customer", "customers", "sample", "records", "details"]):
        return ""
    tables = metadata.get("tables", {}).get("value", [])
    table = _best_table(tables, ["customer", "customers"])
    if not table:
        return ""
    preferred = ["customerID", "customer_id", "first_name", "last_name", "email_address", "email", "city", "country", "phone_number", "state", "gender"]
    columns = _preferred_columns(table.get("columns", []), preferred, 6)
    if not columns:
        return ""
    table_name = _dax_table(table["name"])
    select_args = []
    for column in columns:
        select_args.append(f'        "{column}", {table_name}[{column}]')
    order_column = columns[0]
    return "\n".join(
        [
            "EVALUATE",
            "TOPN(",
            "    5,",
            "    SELECTCOLUMNS(",
            f"        {table_name},",
            ",\n".join(select_args),
            "    ),",
            f"    [{order_column}], ASC",
            ")",
        ]
    )


def _best_table(tables: list[dict[str, Any]], terms: list[str]) -> dict[str, Any] | None:
    visible_tables = [table for table in tables if not table.get("isHidden")]
    for term in terms:
        for table in visible_tables:
            if term in str(table.get("name", "")).lower():
                return table
    return None


def _preferred_columns(columns: list[dict[str, Any]], preferred: list[str], limit: int) -> list[str]:
    available = [str(column.get("name")) for column in columns if column.get("name") and not column.get("isHidden")]
    by_lower = {column.lower(): column for column in available}
    selected = [by_lower[name.lower()] for name in preferred if name.lower() in by_lower]
    if len(selected) < limit:
        selected.extend([column for column in available if column not in selected])
    return selected[:limit]


def _dax_table(name: str) -> str:
    return f"'{name.replace(chr(39), chr(39) + chr(39))}'"


def _query_result_summary(query_result: dict[str, Any]) -> dict[str, Any]:
    rows = query_result.get("rows", [])
    return {
        "query": query_result.get("query"),
        "row_count": len(rows),
        "rows": rows[:5],
        "errors": query_result.get("errors"),
    }


def _dev_system_prompt(project: AgentProject, selected_agent: str, selected_model: SemanticModelRef, metadata: dict[str, Any], query_result: dict[str, Any]) -> str:
    if project.deployment_mode == "standalone":
        agent = project.standalone_agent
        return f"""
You are running Dev UI for a standalone Fabric agent.
Agent: {agent.name}
Data source: {_source_label(agent.data_source)}
Metadata available to the dev run: {_metadata_summary(metadata) if metadata else {}}
Semantic model context: {_metadata_prompt_context(metadata) if metadata else {}}
Executed read-only DAX result: {_query_result_summary(query_result) if query_result else {}}
Prompt under test:
{agent.prompt}
Answer the user as the agent under test. If Executed read-only DAX result contains rows, answer from those rows and include the DAX query that was executed. If metadata is available, refer to the actual tables, columns, or measures that are relevant. Do not claim metadata is missing when Semantic model context contains tables_detail. Do not fabricate data values.
""".strip()
    subagents = "\n".join(
        f"- {subagent.name}: {subagent.description} | source={_source_label(subagent.data_source)}"
        for subagent in project.orchestrator.subagents
    )
    return f"""
You are running Dev UI for an orchestrator with semantic-model subagents.
Orchestrator: {project.orchestrator.name}
Selected agent: {selected_agent}
Selected data source: {_source_label(selected_model)}
Metadata available to the dev run: {_metadata_summary(metadata) if metadata else {}}
Semantic model context: {_metadata_prompt_context(metadata) if metadata else {}}
Executed read-only DAX result: {_query_result_summary(query_result) if query_result else {}}
Prompt under test:
{project.orchestrator.prompt}
Subagents:
{subagents}
Route the user request to the best subagent and answer as the selected agent. If Executed read-only DAX result contains rows, answer from those rows and include the DAX query that was executed. If metadata is available, refer to the actual tables, columns, or measures that are relevant. Do not claim metadata is missing when Semantic model context contains tables_detail. Do not fabricate data values.
""".strip()
