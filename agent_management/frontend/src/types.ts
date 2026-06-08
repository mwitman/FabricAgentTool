export type DeploymentMode = "standalone" | "orchestrator" | "orchestrator_only";
export type DataSourceType = "fabric_mcp" | "semantic_model" | "graphql" | "sql_endpoint" | "data_agent";

export interface DataSourceRef {
  source_type: DataSourceType;
  workspace_id: string;
  workspace_name: string;
  item_id: string;
  item_name: string;
}

/** @deprecated Use DataSourceRef instead */
export type SemanticModelRef = DataSourceRef;

export interface ModelConfig {
  deployment_name: string;
  model_display_name: string;
  model_name?: string;
  provider?: string;
  publisher?: string;
  model_format?: string;
  capabilities?: Record<string, unknown>;
}

export interface SubagentConfig {
  id: string;
  name: string;
  description: string;
  semantic_model: DataSourceRef;
  model_config: ModelConfig;
  prompt: string;
  guardrails: string[];
}

export interface ExternalAgentRef {
  id: string;
  agent_name: string;
  display_name: string;
  project_id: string;
  description: string;
}

export interface AgentProject {
  id?: string;
  type?: string;
  name: string;
  description: string;
  deployment_mode: DeploymentMode;
  orchestrator: {
    name: string;
    description: string;
    prompt: string;
    model_config: ModelConfig;
    subagents: SubagentConfig[];
  };
  orchestrator_only: {
    name: string;
    description: string;
    prompt: string;
    model_config: ModelConfig;
    external_agents: ExternalAgentRef[];
  };
  standalone_agent: {
    name: string;
    description: string;
    semantic_model: DataSourceRef;
    model_config: ModelConfig;
    prompt: string;
  };
  deployment?: Record<string, unknown>;
}

export interface FabricItem extends DataSourceRef {
  fabric_type: string;
  description?: string;
  score?: number;
}

/** @deprecated Use FabricItem instead */
export type SemanticModelResult = FabricItem;

export const emptyDataSource: DataSourceRef = {
  source_type: "semantic_model",
  workspace_id: "",
  workspace_name: "",
  item_id: "",
  item_name: "",
};

export const fabricMcpDataSource: DataSourceRef = {
  source_type: "fabric_mcp",
  workspace_id: "",
  workspace_name: "",
  item_id: "fabric_mcp",
  item_name: "Fabric MCP",
};

/** @deprecated Use emptyDataSource */
export const emptySemanticModel = emptyDataSource;

export const emptyModelConfig: ModelConfig = {
  deployment_name: "",
  model_display_name: "",
};

export const dataSourceTypeLabels: Record<DataSourceType, string> = {
  fabric_mcp: "Fabric MCP",
  semantic_model: "Semantic Model",
  graphql: "GraphQL API",
  sql_endpoint: "SQL Endpoint",
  data_agent: "Data Agent",
};

export function newProject(): AgentProject {
  return {
    name: "My Fabric Agent",
    description: "A managed Fabric agent.",
    deployment_mode: "orchestrator",
    orchestrator: {
      name: "Fabric Orchestrator",
      description: "Routes business questions to semantic-model subagents.",
      prompt: "",
      model_config: emptyModelConfig,
      subagents: [],
    },
    orchestrator_only: {
      name: "Orchestrator Only",
      description: "Routes to existing deployed standalone agents.",
      prompt: "",
      model_config: emptyModelConfig,
      external_agents: [],
    },
    standalone_agent: {
      name: "Standalone Fabric Agent",
      description: "Answers questions from a selected data source.",
      semantic_model: emptyDataSource,
      model_config: emptyModelConfig,
      prompt: "",
    },
  };
}


// ---------------------------------------------------------------------------
// Permissions / Roles
// ---------------------------------------------------------------------------

export interface RoleMember {
  object_id: string;
  display_name: string;
  member_type: "user" | "group";
}

export interface Role {
  id?: string;
  type?: string;
  name: string;
  description: string;
  members: RoleMember[];
  created_at?: string;
  updated_at?: string;
}

export interface AgentRoleBinding {
  id?: string;
  type?: string;
  project_id: string;
  agent_name: string;
  project_display_name: string;
  role_ids: string[];
  created_at?: string;
  updated_at?: string;
}

export function newRole(): Role {
  return { name: "", description: "", members: [] };
}

export const emptyRoleMember: RoleMember = { object_id: "", display_name: "", member_type: "user" };
