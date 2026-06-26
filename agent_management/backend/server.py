from __future__ import annotations

import asyncio
from contextlib import suppress
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from azure.cosmos.exceptions import CosmosHttpResponseError
from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .fabric_semantic import list_fabric_items, list_semantic_models, list_workspaces, semantic_model_metadata
from .foundry_management import create_hosted_agent_version, foundry_agent_link, get_hosted_agent_info, invoke_hosted_agent
from .hosted_agent_builder import build_hosted_agent_deployment, list_runtime_versions, validate_project
from .models import AgentRoleBinding, BulkRuntimeDeploymentRequest, DeploymentRequest, DevChatRequest, PromptGenerationRequest, AgentProject, Role
from .permissions_store import create_permissions_store
from .project_store import create_project_store, get_version_store
from .prompt_generator import dev_chat, generate_prompt
from .semantic_metadata import MetadataScheduleStore, SemanticMetadataStore, refresh_all_project_metadata, refresh_project_metadata, run_due_schedules

ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT.parent
load_dotenv(APP_ROOT / ".env", override=True)
load_dotenv(APP_ROOT / "env.TEMPLATE")

app = FastAPI(title="Agent Management")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

store = None
_permissions_store = None
_dev_history: dict[str, list[dict[str, str]]] = {}
_metadata_scheduler_task: asyncio.Task | None = None


def _metadata_scheduler_enabled() -> bool:
    return os.environ.get("AGENT_MGMT_ENABLE_METADATA_SCHEDULER", "").strip().lower() in {"1", "true", "yes", "on"}


def _metadata_scheduler_interval_seconds() -> int:
    try:
        return max(10, int(os.environ.get("AGENT_MGMT_METADATA_SCHEDULER_INTERVAL_SECONDS", "60")))
    except ValueError:
        return 60


async def _metadata_scheduler_loop() -> None:
    interval = _metadata_scheduler_interval_seconds()
    while True:
        try:
            await run_due_schedules()
        except Exception as exc:
            print(f"Metadata refresh scheduler failed: {exc}", flush=True)
        await asyncio.sleep(interval)


@app.on_event("startup")
async def start_metadata_scheduler() -> None:
    global _metadata_scheduler_task
    if _metadata_scheduler_enabled() and _metadata_scheduler_task is None:
        _metadata_scheduler_task = asyncio.create_task(_metadata_scheduler_loop())


@app.on_event("shutdown")
async def stop_metadata_scheduler() -> None:
    global _metadata_scheduler_task
    if _metadata_scheduler_task is not None:
        _metadata_scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await _metadata_scheduler_task
        _metadata_scheduler_task = None


def _store():
    global store
    if store is None:
        store = create_project_store()
    return store


def _perms():
    global _permissions_store
    if _permissions_store is None:
        _permissions_store = create_permissions_store()
    return _permissions_store


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise ValueError("Bearer token is required.")
    return authorization[len("Bearer "):].strip()


def _with_foundry_link(project: dict[str, Any] | AgentProject) -> dict[str, Any]:
    if isinstance(project, AgentProject):
        project = project.model_dump(mode="json", by_alias=True)
    deployment = _clean_deployment(project.get("deployment") or {})
    foundry = deployment.get("foundry") if isinstance(deployment.get("foundry"), dict) else {}
    agent_name = deployment.get("agent_name") if foundry else ""
    current_link = deployment.get("foundry_agent_link") or foundry.get("foundry_agent_link")
    if agent_name and foundry and (not current_link or ".services.ai.azure.com/api/" in current_link):
        deployment["foundry_agent_link"] = foundry_agent_link(agent_name)
    project["deployment"] = deployment
    return project


def _clean_deployment(deployment: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(deployment, dict):
        return {}
    legacy_keys = {"package_path", "image_repository", "next_steps"}
    return {key: value for key, value in deployment.items() if key not in legacy_keys}


def _project_store_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, CosmosHttpResponseError):
        status = getattr(exc, "status_code", 500) or 500
        message = str(exc).splitlines()[0]
        operation_match = re.search(r"Operation Type: ([^,\r\n]+)", str(exc))
        resource_match = re.search(r"Resource: ([^,\r\n]+)", str(exc))
        operation = operation_match.group(1).strip() if operation_match else "unknown"
        resource = resource_match.group(1).strip() if resource_match else "unknown"
        return JSONResponse(
            {
                "error": f"Azure Cosmos project store failed: {message}",
                "status": status,
                "operation": operation,
                "resource": resource,
                "hint": f"Verify the service principal has data-plane read/write access to the Azure Cosmos databases and containers. Cosmos denied {operation} on {resource}.",
            },
            status_code=status,
        )
    return JSONResponse({"error": str(exc)}, status_code=500)


def _deployment_error_message(payload: dict[str, Any]) -> str:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if not errors:
        return "Deployment failed."
    messages: list[str] = []
    for error in errors:
        if isinstance(error, str):
            messages.append(error)
        elif isinstance(error, dict):
            message = error.get("message", error)
            if isinstance(message, dict):
                messages.append(message.get("message") or message.get("error", {}).get("message") or json_summary(message))
            else:
                messages.append(str(message))
    return "; ".join(messages) or "Deployment failed."


def _normalize_deployment_timestamp(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if val < 1e12:
            return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
        return datetime.fromtimestamp(val / 1000, tz=timezone.utc).isoformat()
    if isinstance(val, str) and val:
        return val
    return None


def _project_model_deployment(project_dict: dict[str, Any], deployment_mode: str) -> str:
    if deployment_mode == "standalone":
        return (project_dict.get("standalone_agent") or {}).get("model_config", {}).get("deployment_name", "")
    if deployment_mode == "orchestrator_only":
        return (project_dict.get("orchestrator_only") or {}).get("model_config", {}).get("deployment_name", "")
    return (project_dict.get("orchestrator") or {}).get("model_config", {}).get("deployment_name", "")


def _is_admin_user(user_object_id: str, group_ids: list[str]) -> bool:
    allowed_ids = {item.strip().lower() for item in [user_object_id, *group_ids] if item.strip()}
    if not allowed_ids:
        return False
    roles = _perms().list_roles()
    for role in roles:
        if role.get("name", "").strip().lower() not in {"admin", "admins"}:
            continue
        for member in role.get("members", []):
            if str(member.get("object_id", "")).lower() in allowed_ids:
                return True
    return False


def _require_admin(request: Request) -> JSONResponse | None:
    user_object_id = request.headers.get("x-user-object-id", "")
    group_ids = [item.strip() for item in request.headers.get("x-user-group-ids", "").split(",") if item.strip()]
    if _is_admin_user(user_object_id, group_ids):
        return None
    return JSONResponse({"error": "Admin role is required."}, status_code=403)


def json_summary(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 600 else text[:600] + "..."


@app.get("/api/health")
async def health():
    project_store_type = "uninitialized"
    if store is not None:
        project_store_type = type(store).__name__
    return {
        "status": "ok",
        "service": "agent-management",
        "cosmos_endpoint": os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", ""),
        "allow_local_store": os.environ.get("AGENT_MGMT_ALLOW_LOCAL_STORE", ""),
        "project_store_type": project_store_type,
        "foundry_project_endpoint": os.environ.get("FOUNDRY_PROJECT_ENDPOINT", ""),
    }


@app.get("/api/projects")
async def projects():
    try:
        loaded_projects = await run_in_threadpool(lambda: _store().list_projects())
        return {"projects": [_with_foundry_link(project) for project in loaded_projects]}
    except Exception as exc:
        return _project_store_error(exc)


@app.post("/api/projects")
async def create_project(project: AgentProject):
    try:
        saved_project = await run_in_threadpool(lambda: _store().save_project(project))
        return _with_foundry_link(saved_project)
    except Exception as exc:
        return _project_store_error(exc)


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str):
    try:
        project = await run_in_threadpool(lambda: _store().get_project(project_id))
        return _with_foundry_link(project)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.put("/api/projects/{project_id}")
async def save_project(project_id: str, project: AgentProject):
    project.id = project_id
    try:
        saved_project = await run_in_threadpool(lambda: _store().save_project(project))
        return _with_foundry_link(saved_project)
    except Exception as exc:
        return _project_store_error(exc)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    try:
        await run_in_threadpool(lambda: _store().delete_project(project_id))
        # Cascade: remove the agent role binding associated with this project
        binding = await run_in_threadpool(lambda: _perms().get_agent_binding_by_project(project_id))
        if binding:
            await run_in_threadpool(lambda: _perms().delete_agent_binding(binding.id))
        return {"status": "deleted", "project_id": project_id}
    except Exception as exc:
        return _project_store_error(exc)


@app.get("/api/deployed-agents")
async def list_deployed_agents():
    """List agent projects that have been deployed to Foundry."""
    try:
        all_projects = await run_in_threadpool(lambda: _store().list_projects())
        deployed = []
        for p in all_projects:
            deployment = p.get("deployment") or {}
            agent_name = deployment.get("agent_name") or deployment.get("foundry", {}).get("agent_name")
            if agent_name:
                deployed.append({
                    "project_id": p.get("id"),
                    "agent_name": agent_name,
                    "display_name": p.get("name", agent_name),
                    "description": p.get("description", ""),
                    "deployment_mode": p.get("deployment_mode", ""),
                })
        return {"agents": deployed}
    except Exception as exc:
        return _project_store_error(exc)


@app.get("/api/fabric/workspaces")
async def fabric_workspaces(authorization: str | None = Header(default=None)):
    try:
        return await list_workspaces(_bearer_token(authorization))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)


@app.get("/api/fabric/semantic-models")
async def fabric_semantic_models(search: str = "", authorization: str | None = Header(default=None)):
    try:
        return await list_semantic_models(_bearer_token(authorization), search)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)


@app.get("/api/fabric/items")
async def fabric_items(search: str = "", source_type: str = "", authorization: str | None = Header(default=None)):
    """List Fabric workspace items (semantic models, GraphQL APIs, SQL endpoints, data agents)."""
    try:
        return await list_fabric_items(_bearer_token(authorization), search, source_type)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)


@app.get("/api/fabric/semantic-models/{workspace_id}/{model_id}/metadata")
async def fabric_model_metadata(workspace_id: str, model_id: str, authorization: str | None = Header(default=None)):
    try:
        return await semantic_model_metadata(_bearer_token(authorization), workspace_id, model_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)


@app.get("/api/foundry/models")
async def foundry_models():
    """List model deployments available in the configured Foundry project."""
    try:
        from .foundry_models import list_foundry_model_deployments
        return await list_foundry_model_deployments()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/prompts/generate")
async def prompts_generate(request: PromptGenerationRequest):
    try:
        return await generate_prompt(request)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/dev/chat")
async def dev_chat_endpoint(request: DevChatRequest):
    history = _dev_history.setdefault(request.conversation_id, [])
    try:
        result = await _hosted_dev_chat(request, history)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    history.extend([{"role": "user", "content": request.message}, {"role": "assistant", "content": result["response"]}])
    del history[:-12]
    return result


@app.post("/api/dev/chat-local")
async def dev_chat_local_endpoint(request: DevChatRequest):
    """Run the hosted agent runtime logic locally (in-process) with full tool-call tracing."""
    history = _dev_history.setdefault(request.conversation_id, [])
    try:
        result = await _local_dev_chat(request, history)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    history.extend([{"role": "user", "content": request.message}, {"role": "assistant", "content": result["response"]}])
    del history[:-12]
    return result


async def _local_dev_chat(request: DevChatRequest, history: list[dict[str, str]]) -> dict[str, Any]:
    """Execute the agent locally using the hosted_agent_runtime module with tracing."""
    import sys
    import importlib

    project = request.project
    if not request.fabric_token or not request.powerbi_token:
        raise ValueError("Sign in so Dev UI can pass delegated Fabric and Power BI tokens to the hosted agent.")

    # Build enriched message with history
    enriched_message = request.message
    if history:
        lines = []
        for item in history[-12:]:
            role = item.get("role", "message").title()
            content = item.get("content", "").strip()
            if content:
                lines.append(f"{role}: {content}")
        if lines:
            enriched_message = f"[Conversation so far]\n{chr(10).join(lines)}\n\n[Current user message]\n{request.message}"

    trace: list[dict[str, Any]] = [
        {"step": "request", "status": "complete", "detail": request.message},
        {"step": "local_agent", "status": "complete", "detail": "Running agent locally (in-process) with tool-call tracing enabled."},
        {"step": "tokens", "status": "complete", "detail": "Passing signed-in user Fabric and Power BI delegated tokens to the local agent."},
    ]

    # Import and run the traced agent from hosted_agent_runtime
    runtime_path = str(APP_ROOT / "hosted_agent_runtime")
    if runtime_path not in sys.path:
        sys.path.insert(0, runtime_path)
    try:
        from hosted_agent_runtime.app import _run_agent_traced
    except ImportError:
        # Try relative import for different module resolution
        spec = importlib.util.spec_from_file_location("hosted_agent_runtime.app", str(APP_ROOT / "hosted_agent_runtime" / "app.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _run_agent_traced = mod._run_agent_traced

    # Set up environment for the runtime (it reads tokens from env/params)
    # by_alias=True ensures "semantic_model" key is used (not the Python field name "data_source")
    project_dict = project.model_dump(mode="json", by_alias=True) if hasattr(project, "model_dump") else project.dict(by_alias=True) if hasattr(project, "dict") else dict(project)

    result = await _run_agent_traced(
        project=project_dict,
        message=enriched_message,
        fabric_token=request.fabric_token,
        conversation_id=request.conversation_id,
        powerbi_token=request.powerbi_token,
    )

    # Add tool call traces
    for tc in result.get("tool_calls", []):
        status = "complete" if tc.get("status") == "success" else "failed"
        detail = f"Tool: {tc.get('tool')} ({tc.get('elapsed_seconds', '?')}s)"
        data: dict[str, Any] = {"tool": tc.get("tool"), "args": tc.get("args")}
        if tc.get("result_preview"):
            data["result"] = tc["result_preview"]
        if tc.get("error"):
            data["error"] = tc["error"]
        entry: dict[str, Any] = {"step": "tool_call", "status": status, "detail": detail, "data": data}
        if tc.get("timestamp"):
            entry["timestamp"] = tc["timestamp"]
        trace.append(entry)

    trace.append({"step": "response", "status": "complete", "detail": f"Agent returned {len(result.get('response', ''))} chars."})

    return {
        "response": result.get("response") or "No response",
        "debug": {
            "mode": "local",
            "selected_agent": project.name if hasattr(project, "name") else project_dict.get("name", ""),
            "trace": trace,
            "tool_calls": result.get("tool_calls", []),
        },
    }


async def _hosted_dev_chat(request: DevChatRequest, history: list[dict[str, str]]) -> dict[str, Any]:
    project = request.project
    deployment = project.deployment or {}
    agent_name = deployment.get("agent_name")
    if not agent_name:
        raise ValueError("Deploy this project to Foundry before running Dev UI. Dev UI tests the deployed hosted agent container.")
    if deployment.get("foundry", {}).get("errors"):
        raise ValueError("The last Foundry deployment has errors. Redeploy successfully before running Dev UI.")
    if not request.fabric_token or not request.powerbi_token:
        raise ValueError("Sign in so Dev UI can pass delegated Fabric and Power BI tokens to the hosted agent.")

    enriched_message = request.message
    if history:
        lines = []
        for item in history[-12:]:
            role = item.get("role", "message").title()
            content = item.get("content", "").strip()
            if content:
                lines.append(f"{role}: {content}")
        if lines:
            enriched_message = f"[Conversation so far]\n{chr(10).join(lines)}\n\n[Current user message]\n{request.message}"

    trace: list[dict[str, Any]] = [
        {"step": "request", "status": "complete", "detail": request.message},
        {"step": "deployment", "status": "complete", "detail": f"Using deployed Foundry Hosted Agent '{agent_name}'.", "data": {"agent_name": agent_name, "image": deployment.get("image"), "runtime": deployment.get("runtime")}},
        {"step": "tokens", "status": "complete", "detail": "Passing signed-in user Fabric and Power BI delegated tokens to the hosted container."},
    ]
    hosted = await invoke_hosted_agent(
        agent_name=agent_name,
        message=enriched_message,
        conversation_id=request.conversation_id,
        fabric_token=request.fabric_token,
        powerbi_token=request.powerbi_token,
    )
    if hosted.get("errors"):
        trace.append({"step": "foundry", "status": "failed", "detail": "Hosted agent returned an error.", "data": hosted})
        return {"response": f"Hosted agent failed: {_deployment_error_message(hosted)}", "debug": {"mode": project.deployment_mode, "selected_agent": agent_name, "trace": trace}}
    metadata = hosted.get("metadata") if isinstance(hosted.get("metadata"), dict) else {}
    if metadata.get("executed_query"):
        trace.append({
            "step": "query",
            "status": "complete",
            "detail": f"Hosted container executed {metadata.get('query_language', 'query')} against the semantic model.",
            "data": {"language": metadata.get("query_language", "query"), "query": metadata.get("executed_query"), "row_count": metadata.get("row_count")},
        })
    elif metadata.get("query_errors"):
        trace.append({
            "step": "query",
            "status": "failed",
            "detail": f"Hosted container attempted {metadata.get('query_language', 'query')} and received an error.",
            "data": {"language": metadata.get("query_language", "query"), "query": metadata.get("executed_query"), "errors": metadata.get("query_errors")},
        })
    trace.append({"step": "foundry", "status": "complete", "detail": "Hosted agent container returned a response.", "data": {"agent_name": agent_name}})
    return {"response": hosted.get("response") or "No response", "debug": {"mode": project.deployment_mode, "selected_agent": agent_name, "trace": trace, "hosted": hosted}}


@app.post("/api/deploy/validate")
async def deploy_validate(project: AgentProject):
    return validate_project(project)


@app.post("/api/deploy/build")
async def deploy_build(request: DeploymentRequest):
    result = build_hosted_agent_deployment(request.project, request.agent_name, runtime_version=request.runtime_version or request.image_tag)
    if result.get("valid") and request.project.id:
        project = request.project
        project.deployment = result
        _store().save_project(project)
    return result


@app.post("/api/deploy/submit-foundry")
async def deploy_submit_foundry(request: DeploymentRequest):
    try:
        selected_project_version = (request.project_version or "").strip()
        saved_project = _store().get_project(request.project.id) if selected_project_version and request.project.id else _store().save_project(request.project)
        deploy_project = saved_project
        if selected_project_version:
            version_doc = get_version_store().get_version(saved_project.id, selected_project_version)
            if not version_doc or not version_doc.get("snapshot"):
                return JSONResponse({"submitted": False, "error": f"Project version {selected_project_version} was not found."}, status_code=400)
            deploy_project = AgentProject.model_validate({**version_doc["snapshot"], "id": saved_project.id})
        metadata_refresh = await refresh_project_metadata(deploy_project.model_dump(mode="json", by_alias=True))
        build = build_hosted_agent_deployment(deploy_project, request.agent_name, runtime_version=request.runtime_version or request.image_tag)
        if not build.get("valid"):
            return JSONResponse({**build, "error": _deployment_error_message(build)}, status_code=400)
        if not request.submit_to_foundry:
            return {**build, "submitted": False, "metadata_refresh": metadata_refresh, "message": "Deployment validated. Set submit_to_foundry=true to create the Foundry hosted-agent version."}
        # Resolve the model deployment name from the project config
        project_dict = deploy_project.model_dump(mode="json", by_alias=True)
        if deploy_project.deployment_mode == "standalone":
            _model_deployment = (project_dict.get("standalone_agent") or {}).get("model_config", {}).get("deployment_name", "")
        elif deploy_project.deployment_mode == "orchestrator_only":
            _model_deployment = (project_dict.get("orchestrator_only") or {}).get("model_config", {}).get("deployment_name", "")
        else:
            _model_deployment = (project_dict.get("orchestrator") or {}).get("model_config", {}).get("deployment_name", "")
        env_vars: dict[str, str] = {"MAF_MGMT_PROJECT_ID": saved_project.id}
        env_vars["MAF_MGMT_VERSIONS_CONTAINER"] = os.environ.get("AGENT_MGMT_VERSIONS_CONTAINER", "agentversions")
        env_vars["MAF_MGMT_METADATA_CONTAINER"] = os.environ.get("AGENT_MGMT_METADATA_CONTAINER", "semanticmodelmetadata")
        env_vars["MAF_MGMT_AGENT_NAME"] = build["agent_name"]
        if selected_project_version:
            env_vars["MAF_MGMT_PROJECT_VERSION"] = selected_project_version
        if _model_deployment:
            env_vars["model_deployment_name"] = _model_deployment
            env_vars["AZURE_OPENAI_DEPLOYMENT_NAME"] = _model_deployment
        elif os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME"):
            env_vars["model_deployment_name"] = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]
            env_vars["AZURE_OPENAI_DEPLOYMENT_NAME"] = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]
        # Add a deployment nonce so Foundry always creates a new version
        env_vars["project_deployed_at"] = datetime.now(timezone.utc).isoformat()

        result = await create_hosted_agent_version(
            agent_name=build["agent_name"],
            image=build["image"],
            description=saved_project.description or saved_project.name,
            metadata={"source": "agent_management", "project_id": saved_project.id, "mode": deploy_project.deployment_mode},
            environment_variables=env_vars,
        )

        if result.get("errors"):
            saved_project.deployment = {**build, "foundry": result}
            _store().save_project(saved_project)
            return JSONResponse(
                {"submitted": False, "error": _deployment_error_message(result), "build": build, "foundry": result},
                status_code=400,
            )
        # Fetch deployed agent info (endpoint, version, status)
        agent_info = await get_hosted_agent_info(build["agent_name"])
        # Build the agent endpoint URL
        foundry_endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
        agent_endpoint_url = f"{foundry_endpoint}/agents/{build['agent_name']}/endpoint/protocols/openai/responses"
        # Extract version from the Foundry response
        foundry_version = (
            result.get("version")
            or result.get("name")
            or result.get("version_id")
            or agent_info.get("version")
        )

        # Normalize timestamp: Foundry may return Unix seconds, ISO string, or nothing
        def _normalize_timestamp(val):
            if val is None:
                return None
            if isinstance(val, (int, float)):
                # Unix timestamp: if < 1e12, it's seconds; otherwise milliseconds
                if val < 1e12:
                    return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
                else:
                    return datetime.fromtimestamp(val / 1000, tz=timezone.utc).isoformat()
            if isinstance(val, str) and val:
                return val
            return None

        raw_info = agent_info.get("raw") or {}
        foundry_deployed_at = (
            _normalize_timestamp(result.get("created_date_time"))
            or _normalize_timestamp(result.get("createdDateTime"))
            or _normalize_timestamp(result.get("created_at"))
            or _normalize_timestamp(result.get("lastModifiedDateTime"))
            or _normalize_timestamp(result.get("last_modified_date_time"))
            or _normalize_timestamp(raw_info.get("last_modified_date_time"))
            or _normalize_timestamp(raw_info.get("lastModifiedDateTime"))
            or _normalize_timestamp(raw_info.get("created_date_time"))
            or _normalize_timestamp(raw_info.get("createdDateTime"))
            or datetime.now(timezone.utc).isoformat()
        )
        project_version = selected_project_version or foundry_version
        deployment_info = {
            "agent_name": build["agent_name"],
            "agent_endpoint": agent_endpoint_url,
            "foundry_version": foundry_version,
            "runtime_image": build.get("image"),
            "runtime_image_source": build.get("runtime_image_source"),
            "runtime_version": build.get("runtime_version"),
            "runtime_latest_digest": build.get("runtime_latest_digest"),
            "project_version": project_version,
            "project_version_source": selected_project_version or "current_draft",
            "deployed_at": foundry_deployed_at,
            "foundry_agent_link": result.get("foundry_agent_link") or agent_info.get("foundry_agent_link"),
        }
        # --- Save immutable version snapshot to Cosmos using Foundry version number ---
        if foundry_version:
            try:
                snapshot_with_deployment = {
                    **project_dict,
                    "deployment": {**build, "foundry": result, "info": deployment_info},
                }
                get_version_store().save_version(
                    project_id=saved_project.id,
                    version=str(foundry_version),
                    snapshot=snapshot_with_deployment,
                    project_version=str(project_version or ""),
                    runtime_version=str(build.get("runtime_version") or ""),
                )
            except Exception as ver_exc:
                import logging
                logging.getLogger(__name__).warning("Failed to save version snapshot: %s", ver_exc)
        saved_project.deployment = {**build, "foundry": result, "info": deployment_info}
        _store().save_project(saved_project)
        # Ensure an agent role binding exists for the deployed agent
        binding = _perms().get_agent_binding_by_project(saved_project.id)
        if not binding:
            binding = AgentRoleBinding(
                project_id=saved_project.id,
                agent_name=build["agent_name"],
                project_display_name=saved_project.name,
            )
            _perms().save_agent_binding(binding)
        else:
            binding.agent_name = build["agent_name"]
            binding.project_display_name = saved_project.name
            _perms().save_agent_binding(binding)
        return {"submitted": True, "build": build, "foundry": result, "info": deployment_info, "binding_id": binding.id, "metadata_refresh": metadata_refresh}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/runtime/versions")
async def runtime_versions():
    try:
        return list_runtime_versions()
    except Exception as exc:
        return JSONResponse({"error": str(exc), "versions": []}, status_code=500)


@app.post("/api/admin/deploy/runtime")
async def admin_deploy_runtime(request: Request, payload: BulkRuntimeDeploymentRequest):
    admin_error = _require_admin(request)
    if admin_error:
        return admin_error

    runtime_version = payload.runtime_version.strip()
    if not runtime_version or runtime_version == "latest":
        return JSONResponse({"error": "Select a concrete runtime version tag, not latest."}, status_code=400)

    results: list[dict[str, Any]] = []
    for project_id in payload.project_ids:
        item: dict[str, Any] = {"project_id": project_id, "status": "failed"}
        try:
            saved_project = _store().get_project(project_id)
            item["project_name"] = saved_project.name
            deployment = saved_project.deployment or {}
            info = deployment.get("info") or {}
            foundry = deployment.get("foundry") or {}
            agent_name = info.get("agent_name") or deployment.get("agent_name")
            pinned_project_version = str(
                info.get("project_version")
                or deployment.get("project_version")
                or ""
            ).strip()
            if not agent_name:
                raise RuntimeError("Project does not have a deployed agent name.")
            deploy_project = saved_project
            project_version_source = "current_draft"
            if pinned_project_version:
                version_doc = get_version_store().get_version(saved_project.id, pinned_project_version)
                if version_doc and version_doc.get("snapshot"):
                    deploy_project = AgentProject.model_validate({**version_doc["snapshot"], "id": saved_project.id})
                    project_version_source = pinned_project_version
            project_dict = deploy_project.model_dump(mode="json", by_alias=True)
            build = build_hosted_agent_deployment(deploy_project, agent_name, runtime_version=runtime_version)
            if not build.get("valid"):
                raise RuntimeError(_deployment_error_message(build))

            model_deployment = _project_model_deployment(project_dict, deploy_project.deployment_mode)
            env_vars: dict[str, str] = {
                "MAF_MGMT_PROJECT_ID": saved_project.id,
                "MAF_MGMT_VERSIONS_CONTAINER": os.environ.get("AGENT_MGMT_VERSIONS_CONTAINER", "agentversions"),
                "MAF_MGMT_METADATA_CONTAINER": os.environ.get("AGENT_MGMT_METADATA_CONTAINER", "semanticmodelmetadata"),
                "MAF_MGMT_AGENT_NAME": build["agent_name"],
                "project_deployed_at": datetime.now(timezone.utc).isoformat(),
            }
            if pinned_project_version and project_version_source != "current_draft":
                env_vars["MAF_MGMT_PROJECT_VERSION"] = pinned_project_version
            if model_deployment:
                env_vars["model_deployment_name"] = model_deployment
                env_vars["AZURE_OPENAI_DEPLOYMENT_NAME"] = model_deployment
            elif os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME"):
                env_vars["model_deployment_name"] = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]
                env_vars["AZURE_OPENAI_DEPLOYMENT_NAME"] = os.environ["AZURE_OPENAI_DEPLOYMENT_NAME"]

            foundry_result = await create_hosted_agent_version(
                agent_name=build["agent_name"],
                image=build["image"],
                description=saved_project.description or saved_project.name,
                metadata={"source": "agent_management", "project_id": saved_project.id, "mode": deploy_project.deployment_mode, "bulk_runtime_deploy": "true"},
                environment_variables=env_vars,
            )
            if foundry_result.get("errors"):
                raise RuntimeError(_deployment_error_message(foundry_result))

            agent_info = await get_hosted_agent_info(build["agent_name"])
            foundry_endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
            agent_endpoint_url = f"{foundry_endpoint}/agents/{build['agent_name']}/endpoint/protocols/openai/responses"
            foundry_version = foundry_result.get("version") or foundry_result.get("name") or foundry_result.get("version_id") or agent_info.get("version")
            project_version = pinned_project_version if project_version_source != "current_draft" else str(foundry_version or "")
            raw_info = agent_info.get("raw") or {}
            foundry_deployed_at = (
                _normalize_deployment_timestamp(foundry_result.get("created_date_time"))
                or _normalize_deployment_timestamp(foundry_result.get("createdDateTime"))
                or _normalize_deployment_timestamp(foundry_result.get("created_at"))
                or _normalize_deployment_timestamp(foundry_result.get("lastModifiedDateTime"))
                or _normalize_deployment_timestamp(foundry_result.get("last_modified_date_time"))
                or _normalize_deployment_timestamp(raw_info.get("last_modified_date_time"))
                or _normalize_deployment_timestamp(raw_info.get("lastModifiedDateTime"))
                or _normalize_deployment_timestamp(raw_info.get("created_date_time"))
                or _normalize_deployment_timestamp(raw_info.get("createdDateTime"))
                or datetime.now(timezone.utc).isoformat()
            )
            deployment_info = {
                "agent_name": build["agent_name"],
                "agent_endpoint": agent_endpoint_url,
                "foundry_version": foundry_version,
                "runtime_image": build.get("image"),
                "runtime_image_source": build.get("runtime_image_source"),
                "runtime_version": build.get("runtime_version"),
                "runtime_latest_digest": build.get("runtime_latest_digest"),
                "project_version": project_version,
                "project_version_source": project_version_source,
                "deployed_at": foundry_deployed_at,
                "foundry_agent_link": foundry_result.get("foundry_agent_link") or agent_info.get("foundry_agent_link"),
            }
            if foundry_version:
                snapshot_with_deployment = {
                    **project_dict,
                    "deployment": {**build, "foundry": foundry_result, "info": deployment_info},
                }
                get_version_store().save_version(
                    project_id=saved_project.id,
                    version=str(foundry_version),
                    snapshot=snapshot_with_deployment,
                    project_version=project_version,
                    runtime_version=str(build.get("runtime_version") or ""),
                )
            saved_project.deployment = {**build, "foundry": foundry_result, "info": deployment_info}
            _store().save_project(saved_project)
            item.update({
                "status": "succeeded",
                "agent_name": build["agent_name"],
                "foundry_version": foundry_version,
                "project_version": project_version,
                "project_version_source": project_version_source,
                "runtime_version": build.get("runtime_version"),
                "runtime_image": build.get("image"),
            })
        except Exception as exc:
            item["error"] = str(exc)
        results.append(item)

    succeeded = sum(1 for item in results if item.get("status") == "succeeded")
    failed = len(results) - succeeded
    return {"status": "succeeded" if failed == 0 else "partial" if succeeded else "failed", "succeeded": succeeded, "failed": failed, "results": results}


# ---------------------------------------------------------------------------
# Semantic Metadata Cache / Refresh API
# ---------------------------------------------------------------------------


@app.get("/api/admin/semantic-metadata")
async def list_semantic_metadata(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        return {"metadata": await run_in_threadpool(lambda: SemanticMetadataStore().list_metadata())}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/admin/metadata-refresh/schedules")
async def list_metadata_refresh_schedules(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        return {"schedules": await run_in_threadpool(lambda: MetadataScheduleStore().list_schedules())}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/admin/metadata-refresh/schedules")
async def create_metadata_refresh_schedule(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    payload = await request.json()
    try:
        return await run_in_threadpool(lambda: MetadataScheduleStore().upsert_schedule(payload))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.put("/api/admin/metadata-refresh/schedules/{schedule_id}")
async def update_metadata_refresh_schedule(schedule_id: str, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    payload = await request.json()
    payload["id"] = schedule_id
    try:
        return await run_in_threadpool(lambda: MetadataScheduleStore().upsert_schedule(payload))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.delete("/api/admin/metadata-refresh/schedules/{schedule_id}")
async def delete_metadata_refresh_schedule(schedule_id: str, request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        await run_in_threadpool(lambda: MetadataScheduleStore().delete_schedule(schedule_id))
        return {"status": "deleted", "schedule_id": schedule_id}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/admin/metadata-refresh/run-now")
async def run_metadata_refresh_now(request: Request):
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        return await refresh_all_project_metadata(trigger="run_now")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/admin/metadata-refresh/runs")
async def list_metadata_refresh_runs(request: Request, limit: int = 50):
    denied = _require_admin(request)
    if denied:
        return denied
    try:
        return {"runs": await run_in_threadpool(lambda: MetadataScheduleStore().list_runs(limit))}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Project Versions API
# ---------------------------------------------------------------------------


@app.get("/api/projects/{project_id}/versions")
async def list_project_versions(project_id: str):
    try:
        versions = get_version_store().list_versions(project_id)
        return {"versions": versions}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/projects/{project_id}/versions/{version}")
async def get_project_version(project_id: str, version: str):
    doc = get_version_store().get_version(project_id, version)
    if not doc:
        return JSONResponse({"error": "Version not found"}, status_code=404)
    return doc


# ---------------------------------------------------------------------------
# Permissions / Roles API
# ---------------------------------------------------------------------------


@app.get("/api/roles")
async def list_roles():
    return {"roles": await run_in_threadpool(lambda: _perms().list_roles())}


@app.post("/api/roles")
async def create_role(role: Role):
    saved_role = await run_in_threadpool(lambda: _perms().save_role(role))
    return saved_role.model_dump(mode="json")


@app.put("/api/roles/{role_id}")
async def update_role(role_id: str, role: Role):
    role.id = role_id
    saved_role = await run_in_threadpool(lambda: _perms().save_role(role))
    return saved_role.model_dump(mode="json")


@app.delete("/api/roles/{role_id}")
async def delete_role(role_id: str):
    await run_in_threadpool(lambda: _perms().delete_role(role_id))
    return {"status": "deleted", "role_id": role_id}


@app.get("/api/agent-bindings")
async def list_agent_bindings():
    return {"bindings": await run_in_threadpool(lambda: _perms().list_agent_bindings())}


@app.get("/api/agent-bindings/{project_id}")
async def get_agent_binding(project_id: str):
    binding = await run_in_threadpool(lambda: _perms().get_agent_binding_by_project(project_id))
    if not binding:
        return JSONResponse({"error": "No binding found for this project."}, status_code=404)
    return binding.model_dump(mode="json")


@app.put("/api/agent-bindings/{project_id}")
async def save_agent_binding(project_id: str, binding: AgentRoleBinding):
    binding.project_id = project_id
    saved_binding = await run_in_threadpool(lambda: _perms().save_agent_binding(binding))
    return saved_binding.model_dump(mode="json")


@app.delete("/api/agent-bindings/{binding_id}/delete")
async def delete_agent_binding(binding_id: str):
    await run_in_threadpool(lambda: _perms().delete_agent_binding(binding_id))
    return {"status": "deleted", "binding_id": binding_id}


@app.post("/api/agents-for-user")
async def agents_for_user(request: Request):
    """Given a user's object_id and group memberships, return the agents they can access."""
    body = await request.json()
    user_object_id = body.get("user_object_id", "")
    group_ids = body.get("group_ids", [])
    if not user_object_id:
        return JSONResponse({"error": "user_object_id is required."}, status_code=400)
    agents = await run_in_threadpool(lambda: _perms().get_agents_for_user(user_object_id, group_ids))
    return {"agents": agents}


@app.get("/api/directory/member-of/{user_id}")
async def directory_member_of(user_id: str):
    """Return transitive Entra group ids for a user (uses service principal)."""
    try:
        from azure.identity import ClientSecretCredential
        import aiohttp

        tenant_id = os.environ.get("AZURE_TENANT_ID", "")
        client_id = os.environ.get("APP_CLIENT_ID", "")
        client_secret = os.environ.get("APP_CLIENT_SECRET", "")
        if not (tenant_id and client_id and client_secret):
            return JSONResponse({"error": "Service principal not configured"}, status_code=500)

        cred = ClientSecretCredential(tenant_id, client_id, client_secret)
        token = cred.get_token("https://graph.microsoft.com/.default").token
        headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}

        group_ids: list[str] = []
        url = f"https://graph.microsoft.com/v1.0/users/{user_id}/transitiveMemberOf/microsoft.graph.group?$select=id,displayName&$top=999"
        async with aiohttp.ClientSession() as session:
            while url:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        details = await resp.text()
                        return JSONResponse({"error": details}, status_code=resp.status)
                    data = await resp.json()
                    group_ids.extend(group["id"] for group in data.get("value", []) if group.get("id"))
                    url = data.get("@odata.nextLink")

        return {"group_ids": group_ids}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/directory/search")
async def directory_search(q: str = ""):
    """Search Azure AD users and groups by display name (uses service principal)."""
    if not q or len(q) < 2:
        return {"results": []}
    try:
        from azure.identity import ClientSecretCredential
        import aiohttp

        tenant_id = os.environ.get("AZURE_TENANT_ID", "")
        client_id = os.environ.get("APP_CLIENT_ID", "")
        client_secret = os.environ.get("APP_CLIENT_SECRET", "")
        if not (tenant_id and client_id and client_secret):
            return JSONResponse({"error": "Service principal not configured"}, status_code=500)

        cred = ClientSecretCredential(tenant_id, client_id, client_secret)
        token = cred.get_token("https://graph.microsoft.com/.default").token

        headers = {"Authorization": f"Bearer {token}", "ConsistencyLevel": "eventual"}
        safe_q = q.replace("'", "''")

        results: list[dict[str, str]] = []
        async with aiohttp.ClientSession() as session:
            # Search users (using $search for substring/contains matching, case-insensitive)
            user_url = (
                "https://graph.microsoft.com/v1.0/users"
                f"?$search=\"displayName:{safe_q}\" OR \"mail:{safe_q}\" OR \"userPrincipalName:{safe_q}\""
                "&$top=10&$select=id,displayName,mail,userPrincipalName&$count=true"
            )
            async with session.get(user_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for u in data.get("value", []):
                        results.append({
                            "object_id": u["id"],
                            "display_name": u.get("displayName") or u.get("userPrincipalName") or "",
                            "email": u.get("mail") or u.get("userPrincipalName") or "",
                            "member_type": "user",
                        })

            # Search groups (using $search for substring/contains matching, case-insensitive)
            group_url = (
                "https://graph.microsoft.com/v1.0/groups"
                f"?$search=\"displayName:{safe_q}\""
                "&$top=10&$select=id,displayName&$count=true"
            )
            async with session.get(group_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for g in data.get("value", []):
                        results.append({
                            "object_id": g["id"],
                            "display_name": g.get("displayName") or "",
                            "email": "",
                            "member_type": "group",
                        })

        return {"results": results}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


_static_dir = ROOT / "static"
if _static_dir.exists():
    @app.get("/")
    async def root():
        from fastapi.responses import FileResponse
        return FileResponse(_static_dir / "index.html")

    app.mount("/", StaticFiles(directory=str(_static_dir)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8091")), reload=True)
