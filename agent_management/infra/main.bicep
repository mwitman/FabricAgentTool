@description('Azure region for Agent Management resources.')
param location string = resourceGroup().location

@description('Container Apps environment name.')
param containerAppsEnvironmentName string = 'agent-mgmt-env'

@description('Agent Management Container App name.')
param containerAppName string = 'agent-management'

@description('Container image for the Agent Management app.')
param image string

@description('Azure Container Registry login server.')
param acrLoginServer string

@description('Azure Cosmos DB endpoint used by project storage.')
param agentMgmtCosmosEndpoint string = ''

@secure()
@description('Optional app client secret for Foundry/Azure OpenAI access when managed identity is not used.')
param appClientSecret string = ''

@description('Entra tenant ID.')
param azureTenantId string = '72d0cd1a-069b-4eb6-b11f-76cb157bb7b8'

@description('Application client ID used by backend and frontend.')
param appClientId string

@description('Azure OpenAI deployment name used for prompt generation.')
param aoaiDeploymentName string = 'gpt-5.4'

@description('Foundry project endpoint used for hosted-agent deployment.')
param foundryProjectEndpoint string

@description('Reusable runtime image used for generated Foundry Hosted Agents.')
param hostedAgentImage string = '${acrLoginServer}/hosted-agent-runtime:latest'

resource logs 'Microsoft.OperationalInsights/workspaces@2025-02-01' = {
  name: '${containerAppName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource environment 'Microsoft.App/managedEnvironments@2025-02-02-preview' = {
  name: containerAppsEnvironmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource app 'Microsoft.App/containerApps@2025-02-02-preview' = {
  name: containerAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8091
        transport: 'auto'
      }
      registries: [
        {
          server: acrLoginServer
          identity: 'system'
        }
      ]
      secrets: empty(appClientSecret) ? [] : [
        {
          name: 'app-client-secret'
          value: appClientSecret
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'agent-mgmt'
          image: image
          env: concat([
            { name: 'PORT', value: '8091' }
            { name: 'AGENT_MGMT_COSMOS_ENDPOINT', value: agentMgmtCosmosEndpoint }
            { name: 'AGENT_MGMT_COSMOS_DATABASE', value: 'agents' }
            { name: 'AGENT_MGMT_COSMOS_CONTAINER', value: 'agentmetadata' }
            { name: 'AGENT_MGMT_COSMOS_PARTITION_KEY', value: '/projectid' }
            { name: 'AGENT_MGMT_PERMISSIONS_DATABASE', value: 'permissions' }
            { name: 'AGENT_MGMT_PERMISSIONS_CONTAINER', value: 'roles' }
            { name: 'AGENT_MGMT_PERMISSIONS_PARTITION_KEY', value: '/roleid' }
            { name: 'AGENT_MGMT_COSMOS_CREATE_IF_MISSING', value: 'false' }
            { name: 'AGENT_MGMT_COSMOS_AUTH_MODE', value: 'service_principal' }
            { name: 'AZURE_TENANT_ID', value: azureTenantId }
            { name: 'APP_CLIENT_ID', value: appClientId }
            { name: 'AZURE_OPENAI_DEPLOYMENT_NAME', value: aoaiDeploymentName }
            { name: 'AZURE_OPENAI_API_VERSION', value: 'preview' }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryProjectEndpoint }
            { name: 'FOUNDRY_API_VERSION', value: 'v1' }
            { name: 'FOUNDRY_FEATURES', value: 'HostedAgents=V1Preview' }
            { name: 'ACR_LOGIN_SERVER', value: acrLoginServer }
            { name: 'HOSTED_AGENT_IMAGE', value: hostedAgentImage }
          ], empty(appClientSecret) ? [] : [
            { name: 'APP_CLIENT_SECRET', secretRef: 'app-client-secret' }
          ])
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}'
output principalId string = app.identity.principalId
