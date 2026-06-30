# Agent Management

A management UX for creating Fabric semantic model agents and deploying them as Microsoft Foundry Hosted Agents.

## What It Does

- Lists Fabric data sources the signed-in user can access across all visible Fabric workspaces.
- Creates either a standalone semantic model agent or an orchestrator with semantic-model subagents.
- Uses a Foundry model to generate orchestrator, subagent, and standalone prompts.
- Provides a built-in Dev UI to test agent behavior before deployment.
- Deploys projects to Foundry Hosted Agents using a reusable hosted-agent runtime image.
- Stores agent projects in Azure Cosmos DB.

Supported data-source types include semantic models, GraphQL APIs, SQL endpoints/warehouses, Fabric Data Agents, and Fabric MCP sources. Semantic models have first-class cached metadata support. SQL endpoint query execution is supported through Fabric MCP, but SQL endpoint schema enumeration is not yet cached or enforced as a first-class metadata flow.

## Azure Cosmos DB

The default endpoint is configured in `env.TEMPLATE`:

```text
AGENT_MGMT_COSMOS_ENDPOINT=https://your-cosmos-account.documents.azure.com:443/
```

Authenticates with `APP_CLIENT_ID`/`APP_CLIENT_SECRET` when provided, otherwise uses managed identity/default Azure credentials. The identity must have data-plane access to the Azure Cosmos DB databases/containers.

Projects are stored in the `agents` database, `agentmetadata` container, using `/projectid` as the partition key. Roles and agent role bindings are stored in the `permissions` database, `roles` container, using `/roleid` as the partition key. The databases/containers are not created automatically by default.

Set `AGENT_MGMT_BOOTSTRAP_ADMIN_OBJECT_IDS` and `AGENT_MGMT_BOOTSTRAP_DEVELOPER_OBJECT_IDS` in `.env` to comma- or semicolon-separated Entra user/group object IDs to seed the Admin and Developer roles at startup.

## Local Development

```powershell
cd agent_management
Copy-Item env.TEMPLATE .env
Copy-Item frontend\env.TEMPLATE frontend\.env
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start-dev.ps1
```

Open `http://localhost:5173` while developing the frontend. Vite proxies `/api` requests to the FastAPI backend on `http://localhost:8094`, so React changes hot-reload without running `npm run build`.

Use `npm run build` only when you want to refresh the production-style static bundle served directly by FastAPI on `http://localhost:8094`.

## Container Build

```powershell
docker build --platform linux/amd64 `
  --build-arg VITE_ENTRA_CLIENT_ID=<app-client-id> `
  --build-arg VITE_ENTRA_TENANT_ID=<tenant-id> `
  -t <your-acr>.azurecr.io/agent-management:latest .
docker push <your-acr>.azurecr.io/agent-management:latest
```

## Hosted Agent Runtime Image

Deploys Foundry Hosted Agents by saving the project to Azure Cosmos DB and submitting a reusable runtime image with `AGENT_MGMT_PROJECT_ID`. Users do not need to generate a local package for each project.

Build and push the runtime image with both `latest` and a concrete datetime version tag:

```powershell
cd agent_management
.\deploy-hosted-runtime.ps1
```

By default the script pushes:

```text
latest
dt-yyyyMMdd-HHmmss
```

The script only pushes image tags. When an agent is redeployed from Agent Management, the backend resolves `hosted-agent-runtime:latest` in ACR, finds the matching concrete version tag, and deploys the hosted agent with that pinned tag. A later push to `latest` will not affect existing agents until they are redeployed. Use the runtime-version refresh button in Agent Management after pushing a new runtime image.

The Agent Management backend service principal needs `AcrPull` on the registry or the `hosted-agent-runtime` repository to list available runtime versions. The identity running `deploy-hosted-runtime.ps1` needs `AcrPush`.

At deployment time, the project ID and Cosmos settings are passed to Foundry so the hosted agent loads its project definition directly from Azure Cosmos DB.

## Authorization Flow

Agent Management intentionally uses two authorization contexts.

The signed-in user's delegated Fabric token is used for the data-source dropdown. The frontend acquires a Fabric token with MSAL, sends it to `/api/fabric/items`, and the backend calls Fabric `/workspaces` followed by `/workspaces/{workspace_id}/items`. The dropdown therefore shows supported items from all workspaces visible to that user.

The service principal is used for backend operations: Cosmos DB stores, Foundry management, runtime-version reads from ACR, and semantic metadata refresh with Fabric `getDefinition`. The service principal must be allowed to use Fabric APIs by tenant settings and must also have access to the workspaces/items being refreshed.

Hosted semantic-model agents use a least-privilege runtime path. Runtime schema, relationship, and AI-instruction metadata is read from the service-principal-populated Cosmos cache. The user's delegated credentials are still used for data access through guarded DAX query endpoints, but the hosted runtime does not use the user's token to run semantic-model `getDefinition`, DAX INFO schema discovery, Power BI `/tables`, or workspace/item enumeration for configured semantic-model projects. Runtime DAX always uses dataset-scoped Power BI endpoints, such as `/datasets/{semantic_model_id}/executeDaxQueries` and `/datasets/{semantic_model_id}/executeQueries`, instead of workspace-scoped `/groups/{workspace_id}/datasets/{semantic_model_id}` endpoints. Runtime users need the semantic-model permissions required by Power BI query APIs, typically Build/read access on the semantic model, but do not need Fabric workspace Viewer for configured semantic models.

Use `POWERBI_DAX_EXECUTION_MODE=arrow` to try the dataset-scoped Arrow endpoint first while preserving least privilege. Keep `POWERBI_DAX_ARROW_FALLBACK_JSON=true` so the runtime falls back to dataset-scoped JSON `executeQueries` if Arrow is unavailable or rejected for a model.

Authoring is separate from runtime. Users who select data sources in Agent Management still need enough Fabric access for the data-source dropdown to enumerate visible workspaces/items. Fabric MCP projects can also expose dynamic workspace/item discovery at runtime because that is the purpose of the Fabric MCP source type.

## Semantic Metadata Refresh

Semantic model metadata is cached in Cosmos DB using the `AGENT_MGMT_METADATA_CONTAINER` container, defaulting to `semanticmodelmetadata`. Refresh runs and schedules use `AGENT_MGMT_METADATA_SCHEDULE_CONTAINER`, defaulting to `metadatarefresh`.

The Metadata menu's **Run now** button calls `/api/admin/metadata-refresh/run-now`. It scans all saved projects, extracts configured semantic model sources, deduplicates them by `workspace_id:semantic_model_id`, and refreshes each model by calling Fabric `getDefinition?format=TMDL` with the service principal. The refresh writes normalized tables, columns, measures, relationships, and AI instructions to the shared metadata cache.

Deploying to Foundry also refreshes semantic metadata for the project being deployed before the hosted-agent version is submitted. The metadata is written to the shared cache, not embedded into the project version snapshot. Existing deployed agents can see later cache refreshes because the hosted runtime reads from the shared metadata cache container.

At runtime, `get_semantic_model_metadata` is cache-only. If the cache entry is missing or does not contain table metadata, the hosted agent returns a refresh-required error instead of using the user's token for DAX INFO queries, Power BI `/tables`, or Fabric `getDefinition`. Admins should run metadata refresh before deploying or testing semantic-model agents.

## SQL Endpoint Behavior

SQL endpoints and warehouses can be selected as Fabric data sources. At runtime, `execute_fabric_sql_query` validates that the SQL endpoint is configured for the project, blocks non-read-only SQL, and calls Fabric MCP's configured SQL execution tool, defaulting to `execute_sql_query`.

The runtime also exposes generic `call_fabric_mcp` and `call_fabric_mcp_tool` tools, so the model can discover MCP tools and call schema-related MCP tools if they are available. However, the runtime does not currently force a schema-enumeration step before SQL execution, and SQL endpoint schemas are not cached like semantic model metadata.

## Application Insights KQL

Set `APPLICATIONINSIGHTS_CONNECTION_STRING` or `FABRIC_AGENT_APPINSIGHTS_CONNECTION_STRING` so hosted runtime logs and OpenTelemetry spans are exported to Application Insights. Foundry hosted agents reject `APPLICATIONINSIGHTS_CONNECTION_STRING` as a reserved variable, so Agent Management maps it to `FABRIC_AGENT_APPINSIGHTS_CONNECTION_STRING` for the hosted runtime.

Set `FABRIC_AGENT_DEBUG_TELEMETRY=false` for normal production operation. With the default `false` value, hosted runtime logs keep timings, statuses, counts, and failures, but omit verbose payload-like fields such as tool arguments, tool result previews, DAX query previews, and sensitive agent framework telemetry. Temporarily set `FABRIC_AGENT_DEBUG_TELEMETRY=true` while debugging to restore the richer App Insights logging shape.

After changing hosted runtime telemetry code, rebuild and push the runtime image, then redeploy the hosted agent to the new concrete runtime tag. Existing hosted agents stay pinned to their previously deployed runtime version.

### DAX Tool Call Logs

Workspace-based Application Insights:

```kusto
AppTraces
| where TimeGenerated > ago(24h)
| where Message has "DAX tool call"
| extend event = extract(@"DAX tool call ([^:]+):", 1, Message)
| extend workspace_id = extract(@"workspace_id=""([^""]+)""", 1, Message)
| extend semantic_model_id = extract(@"semantic_model_id=""([^""]+)""", 1, Message)
| extend query_preview = extract(@"query_preview=""(.*?)""(?: endpoint=| query_count=| execution_mode=|$)", 1, Message)
| extend endpoint = extract(@"endpoint=""([^""]+)""", 1, Message)
| extend status = extract(@"status=""([^""]+)""", 1, Message)
| extend query_count = toint(extract(@"query_count=([0-9]+)", 1, Message))
| extend row_count = toint(extract(@"row_count=([0-9]+)", 1, Message))
| extend elapsed_ms = toint(extract(@"elapsed_ms=([0-9]+)", 1, Message))
| project TimeGenerated, event, status, endpoint, elapsed_ms, workspace_id, semantic_model_id, query_count, row_count, query_preview, Message
| order by TimeGenerated desc
```

Classic Application Insights:

```kusto
traces
| where timestamp > ago(24h)
| where message has "DAX tool call"
| extend event = extract(@"DAX tool call ([^:]+):", 1, message)
| extend workspace_id = extract(@"workspace_id=""([^""]+)""", 1, message)
| extend semantic_model_id = extract(@"semantic_model_id=""([^""]+)""", 1, message)
| extend query_preview = extract(@"query_preview=""(.*?)""(?: endpoint=| query_count=| execution_mode=|$)", 1, message)
| extend endpoint = extract(@"endpoint=""([^""]+)""", 1, message)
| extend status = extract(@"status=""([^""]+)""", 1, message)
| extend query_count = toint(extract(@"query_count=([0-9]+)", 1, message))
| extend row_count = toint(extract(@"row_count=([0-9]+)", 1, message))
| extend elapsed_ms = toint(extract(@"elapsed_ms=([0-9]+)", 1, message))
| project timestamp, event, status, endpoint, elapsed_ms, workspace_id, semantic_model_id, query_count, row_count, query_preview, message
| order by timestamp desc
```

### Generic Tool Call Logs

Workspace-based Application Insights:

```kusto
AppTraces
| where TimeGenerated > ago(24h)
| where Message has "LLM tool call"
| extend event = extract(@"LLM tool call ([^:]+):", 1, Message)
| extend conversation_id = extract(@"conversation_id=([^ ]+)", 1, Message)
| extend tool = extract(@"tool=([^ ]+)", 1, Message)
| extend status = extract(@"status=([^ ]+)", 1, Message)
| extend elapsed_ms = toint(extract(@"elapsed_ms=([0-9]+)", 1, Message))
| project TimeGenerated, event, conversation_id, tool, status, elapsed_ms, Message
| order by TimeGenerated desc
```

Classic Application Insights:

```kusto
traces
| where timestamp > ago(24h)
| where message has "LLM tool call"
| extend event = extract(@"LLM tool call ([^:]+):", 1, message)
| extend conversation_id = extract(@"conversation_id=([^ ]+)", 1, message)
| extend tool = extract(@"tool=([^ ]+)", 1, message)
| extend status = extract(@"status=([^ ]+)", 1, message)
| extend elapsed_ms = toint(extract(@"elapsed_ms=([0-9]+)", 1, message))
| project timestamp, event, conversation_id, tool, status, elapsed_ms, message
| order by timestamp desc
```

### DAX Spans

Workspace-based Application Insights:

```kusto
AppDependencies
| where TimeGenerated > ago(24h)
| where Name == "fabric.dax.execute"
| extend workspace_id = tostring(Properties["fabric.workspace_id"])
| extend semantic_model_id = tostring(Properties["fabric.semantic_model_id"])
| extend query_count = toint(Properties["fabric.dax.query_count"])
| project TimeGenerated, Name, Success, ResultCode, DurationMs, workspace_id, semantic_model_id, query_count, Properties
| order by TimeGenerated desc
```

Classic Application Insights:

```kusto
dependencies
| where timestamp > ago(24h)
| where name == "fabric.dax.execute"
| extend workspace_id = tostring(customDimensions["fabric.workspace_id"])
| extend semantic_model_id = tostring(customDimensions["fabric.semantic_model_id"])
| extend query_count = toint(customDimensions["fabric.dax.query_count"])
| project timestamp, name, success, resultCode, duration, workspace_id, semantic_model_id, query_count, customDimensions
| order by timestamp desc
```

### Fabric and Power BI HTTP Calls

Workspace-based Application Insights:

```kusto
AppDependencies
| where TimeGenerated > ago(24h)
| where Target has_any ("api.powerbi.com", "api.fabric.microsoft.com")
  or Data has_any ("executeQueries", "executeDaxQueries", "getDefinition")
  or Name has_any ("executeQueries", "executeDaxQueries", "getDefinition")
| project TimeGenerated, Name, Target, Data, Success, ResultCode, DurationMs
| order by TimeGenerated desc
```

Classic Application Insights:

```kusto
dependencies
| where timestamp > ago(24h)
| where target has_any ("api.powerbi.com", "api.fabric.microsoft.com")
  or data has_any ("executeQueries", "executeDaxQueries", "getDefinition")
  or name has_any ("executeQueries", "executeDaxQueries", "getDefinition")
| project timestamp, name, target, data, success, resultCode, duration
| order by timestamp desc
```

### Runtime Startup and Exceptions

Workspace-based startup traces:

```kusto
AppTraces
| where TimeGenerated > ago(24h)
| where Message has_any ("App Insights telemetry configured for hosted runtime", "Agent created", "Agent response", "DAX tool call", "LLM tool call")
| project TimeGenerated, SeverityLevel, Message, Properties
| order by TimeGenerated desc
```

Classic startup traces:

```kusto
traces
| where timestamp > ago(24h)
| where message has_any ("App Insights telemetry configured for hosted runtime", "Agent created", "Agent response", "DAX tool call", "LLM tool call")
| project timestamp, severityLevel, message, customDimensions
| order by timestamp desc
```

Workspace-based exceptions:

```kusto
AppExceptions
| where TimeGenerated > ago(24h)
| where OuterMessage has_any ("DAX", "tool", "executeQueries", "executeDaxQueries")
  or InnermostMessage has_any ("DAX", "tool", "executeQueries", "executeDaxQueries")
| project TimeGenerated, Type, OuterMessage, InnermostMessage, ProblemId, Properties
| order by TimeGenerated desc
```

Classic exceptions:

```kusto
exceptions
| where timestamp > ago(24h)
| where outerMessage has_any ("DAX", "tool", "executeQueries", "executeDaxQueries")
  or innermostMessage has_any ("DAX", "tool", "executeQueries", "executeDaxQueries")
| project timestamp, type, outerMessage, innermostMessage, problemId, customDimensions
| order by timestamp desc
```

## Azure Container Apps

Bicep is provided in `infra/main.bicep`. Validate with what-if before deployment:

```powershell
az deployment group what-if `
  --resource-group <resource-group> `
  --template-file infra/main.bicep `
  --parameters image=<your-acr>.azurecr.io/agent-management:latest `
               acrLoginServer=<your-acr>.azurecr.io `
               appClientId=<app-client-id> `
               aoaiEndpoint=<your-aoai-endpoint> `
               foundryProjectEndpoint=<your-foundry-project-endpoint>
```

Then deploy with `az deployment group create` after reviewing the what-if output.

### Deploy Metadata Refresh Job To An Existing Container Apps Environment

If Agent Management is deployed separately and the customer already has a Container Apps environment, create only the scheduled metadata refresh job. Use the same image as the Agent Management app and override the command to run the worker:

```text
python -m backend.metadata_refresh_worker
```

The job cron is a poller schedule. A value like `*/15 * * * *` wakes the job every 15 minutes to run any admin-managed metadata schedules whose `next_run_at` is due; it does not force every semantic model to refresh every 15 minutes.

```powershell
$resourceGroup = "<resource-group>"
$environmentName = "<existing-container-apps-env>"
$jobName = "agent-management-metadata-refresh"
$image = "<acr-name>.azurecr.io/agent-management:<tag>"
$acrName = "<acr-name>"
$acrLoginServer = "$acrName.azurecr.io"

$cosmosEndpoint = "https://<cosmos-account>.documents.azure.com:443/"
$tenantId = "<tenant-id>"
$appClientId = "<app-registration-client-id>"
$appClientSecret = $env:APP_CLIENT_SECRET
$pollerCron = "*/15 * * * *"

az containerapp job create `
  --name $jobName `
  --resource-group $resourceGroup `
  --environment $environmentName `
  --trigger-type Schedule `
  --cron-expression "$pollerCron" `
  --replica-timeout 1800 `
  --replica-retry-limit 1 `
  --parallelism 1 `
  --replica-completion-count 1 `
  --image $image `
  --command "python" `
  --args "-m" "backend.metadata_refresh_worker" `
  --mi-system-assigned `
  --registry-server $acrLoginServer `
  --registry-identity system `
  --secrets app-client-secret="$appClientSecret" `
  --env-vars `
    AGENT_MGMT_COSMOS_ENDPOINT="$cosmosEndpoint" `
    AGENT_MGMT_COSMOS_DATABASE="agents" `
    AGENT_MGMT_COSMOS_CONTAINER="agentmetadata" `
    AGENT_MGMT_COSMOS_PARTITION_KEY="/projectid" `
    AGENT_MGMT_METADATA_CONTAINER="semanticmodelmetadata" `
    AGENT_MGMT_METADATA_SCHEDULE_CONTAINER="metadatarefresh" `
    AGENT_MGMT_COSMOS_AUTH_MODE="service_principal" `
    AZURE_TENANT_ID="$tenantId" `
    APP_CLIENT_ID="$appClientId" `
    APP_CLIENT_SECRET="secretref:app-client-secret"
```

Grant the job identity permission to pull the image from ACR:

```powershell
$jobPrincipalId = az containerapp job show `
  --name $jobName `
  --resource-group $resourceGroup `
  --query identity.principalId `
  -o tsv

$acrId = az acr show `
  --name $acrName `
  --resource-group $resourceGroup `
  --query id `
  -o tsv

az role assignment create `
  --assignee $jobPrincipalId `
  --role AcrPull `
  --scope $acrId
```

Start one execution manually to verify the worker can read schedules, refresh due metadata, and write run history:

```powershell
az containerapp job start `
  --name $jobName `
  --resource-group $resourceGroup

az containerapp job execution list `
  --name $jobName `
  --resource-group $resourceGroup `
  -o table
```

For managed identity authentication instead of an app client secret, set `AGENT_MGMT_COSMOS_AUTH_MODE="managed_identity"` and, when using a user-assigned identity, set `AZURE_CLIENT_ID` to that identity's client ID. The selected identity must have Cosmos data-plane access and Fabric access for `getDefinition` on the target workspaces and semantic models.

## Deployment Modes

### Standalone Agent

A single semantic-model-bound agent is packaged into one Foundry Hosted Agent container.

### Orchestrator With Subagents

The orchestrator and all subagents are packaged into one Foundry Hosted Agent container. Each subagent remains bound to its selected semantic model in the project definition.

## Current Implementation Notes

The reusable hosted-agent runtime currently exposes the Foundry responses protocol and loads project configuration from Azure Cosmos DB. Its semantic-model execution path is intentionally isolated in `hosted_agent_runtime/app.py` so it can be expanded with richer DAX execution and routing behavior without changing the project model.
