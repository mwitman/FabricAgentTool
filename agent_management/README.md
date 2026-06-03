# Agent Management

A management UX for creating Fabric semantic model agents and deploying them as Microsoft Foundry Hosted Agents.

## What It Does

- Lists Fabric semantic models the signed-in user can access.
- Creates either a standalone semantic model agent or an orchestrator with semantic-model subagents.
- Uses a Foundry model to generate orchestrator, subagent, and standalone prompts.
- Provides a built-in Dev UI to test agent behavior before deployment.
- Deploys projects to Foundry Hosted Agents using a reusable hosted-agent runtime image.
- Stores agent projects in Azure Cosmos DB.

## Azure Cosmos DB

The default endpoint is configured in `env.TEMPLATE`:

```text
AGENT_MGMT_COSMOS_ENDPOINT=https://your-cosmos-account.documents.azure.com:443/
```

Authenticates with `APP_CLIENT_ID`/`APP_CLIENT_SECRET` when provided, otherwise uses managed identity/default Azure credentials. The identity must have data-plane access to the Azure Cosmos DB databases/containers.

Projects are stored in the `agents` database, `agentmetadata` container, using `/projectid` as the partition key. Roles and agent role bindings are stored in the `permissions` database, `roles` container, using `/roleid` as the partition key. The databases/containers are not created automatically by default.

## Local Development

```powershell
cd agent_management
Copy-Item env.TEMPLATE .env
Copy-Item frontend\env.TEMPLATE frontend\.env
..\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start-dev.ps1
```

Open `http://localhost:5173` while developing the frontend. Vite proxies `/api` requests to the FastAPI backend on `http://localhost:8091`, so React changes hot-reload without running `npm run build`.

Use `npm run build` only when you want to refresh the production-style static bundle served directly by FastAPI on `http://localhost:8091`.

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

Build and push the runtime image once, then keep `HOSTED_AGENT_IMAGE` pointed at it:

```powershell
cd agent_management\hosted_agent_runtime
docker build --platform linux/amd64 -t <your-acr>.azurecr.io/hosted-agent-runtime:v8 .
docker push <your-acr>.azurecr.io/hosted-agent-runtime:v8
```

At deployment time, the project ID and Cosmos settings are passed to Foundry so the hosted agent loads its project definition directly from Azure Cosmos DB.

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

## Deployment Modes

### Standalone Agent

A single semantic-model-bound agent is packaged into one Foundry Hosted Agent container.

### Orchestrator With Subagents

The orchestrator and all subagents are packaged into one Foundry Hosted Agent container. Each subagent remains bound to its selected semantic model in the project definition.

## Current Implementation Notes

The reusable hosted-agent runtime currently exposes the Foundry responses protocol and loads project configuration from Azure Cosmos DB. Its semantic-model execution path is intentionally isolated in `hosted_agent_runtime/app.py` so it can be expanded with richer DAX execution and routing behavior without changing the project model.
