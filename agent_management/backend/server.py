from __future__ import annotations

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
from .hosted_agent_builder import build_hosted_agent_deployment, validate_project
from .models import AgentRoleBinding, DeploymentRequest, DevChatRequest, PromptGenerationRequest, AgentProject, Role
from .permissions_store import create_permissions_store
from .project_store import create_project_store
from .prompt_generator import dev_chat, generate_prompt

ROOT = Path(__file__).resolve().parent
APP_ROOT = ROOT.parent
load_dotenv(APP_ROOT / ".env")
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


def json_summary(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 600 else text[:600] + "..."


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "agent-management",
        "cosmos_endpoint": os.environ.get("AGENT_MGMT_COSMOS_ENDPOINT", ""),
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
    result = build_hosted_agent_deployment(request.project, request.agent_name)
    if result.get("valid") and request.project.id:
        project = request.project
        project.deployment = result
        _store().save_project(project)
    return result


@app.post("/api/deploy/submit-foundry")
async def deploy_submit_foundry(request: DeploymentRequest):
    try:
        saved_project = _store().save_project(request.project)
        build = build_hosted_agent_deployment(saved_project, request.agent_name)
        if not build.get("valid"):
            return JSONResponse({**build, "error": _deployment_error_message(build)}, status_code=400)
        if not request.submit_to_foundry:
            return {**build, "submitted": False, "message": "Deployment validated. Set submit_to_foundry=true to create the Foundry hosted-agent version."}
        # Resolve the model deployment name from the project config
        project_dict = saved_project.model_dump(mode="json", by_alias=True)
        if saved_project.deployment_mode == "standalone":
            _model_deployment = (project_dict.get("standalone_agent") or {}).get("model_config", {}).get("deployment_name", "")
        elif saved_project.deployment_mode == "orchestrator_only":
            _model_deployment = (project_dict.get("orchestrator_only") or {}).get("model_config", {}).get("deployment_name", "")
        else:
            _model_deployment = (project_dict.get("orchestrator") or {}).get("model_config", {}).get("deployment_name", "")
        env_vars: dict[str, str] = {"MAF_MGMT_PROJECT_ID": saved_project.id}
        if _model_deployment:
            env_vars["AZURE_OPENAI_DEPLOYMENT_NAME"] = _model_deployment
        # Add a deployment nonce so Foundry always creates a new version
        env_vars["project_deployed_at"] = datetime.now(timezone.utc).isoformat()
        result = await create_hosted_agent_version(
            agent_name=build["agent_name"],
            image=build["image"],
            description=saved_project.description or saved_project.name,
            metadata={"source": "agent_management", "project_id": saved_project.id, "mode": saved_project.deployment_mode},
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
        deployment_info = {
            "agent_name": build["agent_name"],
            "agent_endpoint": agent_endpoint_url,
            "version": foundry_version,
            "deployed_at": foundry_deployed_at,
            "foundry_agent_link": result.get("foundry_agent_link") or agent_info.get("foundry_agent_link"),
        }
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
        return {"submitted": True, "build": build, "foundry": result, "info": deployment_info, "binding_id": binding.id}
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(exc)}, status_code=500)


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
