import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { InteractionRequiredAuthError } from "@azure/msal-browser";
import { useMsal } from "@azure/msal-react";
import { AlertTriangle, Bot, Boxes, CheckCircle2, ExternalLink, FlaskConical, FolderOpen, Loader2, Moon, Plus, RefreshCw, Save, Shield, Sun, Trash2, Wand2, X } from "lucide-react";
import { fabricTokenRequest, loginRequest, powerBiTokenRequest } from "./authConfig";
import { AgentRoleBinding, emptyDataSource, emptyModelConfig, emptyRoleMember, ExternalAgentRef, FabricItem, ModelConfig, newProject, newRole, Role, RoleMember, AgentProject, SubagentConfig } from "./types";
import MemberAutocomplete from "./MemberAutocomplete";
import DataSourceCombobox from "./DataSourceCombobox";

type ActiveTab = "projects" | "create" | "dev" | "roles";

type DevTraceStep = {
  step: string;
  status: string;
  detail: string;
  data?: unknown;
};

type DeploymentUiState = "idle" | "working" | "completed" | "failed";

async function readApiResponse(response: Response) {
  const text = await response.text();
  let payload: any = {};
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { error: text };
    }
  }
  if (!response.ok || payload.error || payload.errors) {
    const details = formatApiError(payload, response.status);
    const error = new Error(details);
    (error as any).payload = payload;
    throw error;
  }
  return payload;
}

function formatApiError(payload: any, status: number) {
  if (payload?.error) return String(payload.error);
  if (Array.isArray(payload?.errors)) {
    return payload.errors.map((error: any) => typeof error === "string" ? error : error?.message ? formatMessage(error.message) : formatMessage(error)).join("; ");
  }
  return `Request failed with status ${status}`;
}

function formatMessage(value: any): string {
  if (typeof value === "string") return value;
  if (value?.message) return String(value.message);
  return JSON.stringify(value, null, 2);
}

function cleanDeployment(deployment: any) {
  if (!deployment || typeof deployment !== "object") return null;
  const { package_path: _packagePath, image_repository: _imageRepository, next_steps: _nextSteps, ...cleaned } = deployment;
  return Object.keys(cleaned).length ? cleaned : null;
}

function shouldUsePopupFallback(error: any) {
  return error instanceof InteractionRequiredAuthError || error?.errorCode === "monitor_window_timeout" || String(error?.message ?? "").includes("monitor_window_timeout");
}

const roleName = (name: string) => name.trim().toLowerCase();
const isAdminRoleName = (name: string) => ["admin", "admins"].includes(roleName(name));
const isAgentCreatorRoleName = (name: string) => roleName(name) === "agent creators";

export default function App() {
  const { instance, accounts } = useMsal();
  const account = accounts[0] ?? null;
  const [project, setProject] = useState<AgentProject>(() => newProject());
  const [activeTab, setActiveTab] = useState<ActiveTab>("projects");
  const [projects, setProjects] = useState<AgentProject[]>([]);
  const [devProjectId, setDevProjectId] = useState("");
  const [models, setModels] = useState<FabricItem[]>([]);
  const [status, setStatus] = useState("Ready");
  const [generatingPrompt, setGeneratingPrompt] = useState<string | null>(null);
  const [devMessage, setDevMessage] = useState("");
  const [devResponse, setDevResponse] = useState("");
  const [devTrace, setDevTrace] = useState<DevTraceStep[]>([]);
  const [devDebug, setDevDebug] = useState<any>(null);
  const [isDevRunning, setIsDevRunning] = useState(false);
  const [devRunLocal, setDevRunLocal] = useState(true);
  const [devChatHistory, setDevChatHistory] = useState<Array<{ role: "user" | "assistant"; content: string }>>([]);
  const [isDeploying, setIsDeploying] = useState(false);
  const [deploymentUiState, setDeploymentUiState] = useState<DeploymentUiState>("idle");
  const [deploymentMessage, setDeploymentMessage] = useState("");
  const [openingProjectId, setOpeningProjectId] = useState<string | null>(null);
  const [deletingProjectId, setDeletingProjectId] = useState<string | null>(null);
  const [deployment, setDeployment] = useState<any>(null);
  const [foundryModels, setFoundryModels] = useState<ModelConfig[]>([]);
  const [isLoadingModels, setIsLoadingModels] = useState(false);
  const [isLoadingFoundryModels, setIsLoadingFoundryModels] = useState(false);
  const [roles, setRoles] = useState<Role[]>([]);
  const [editingRole, setEditingRole] = useState<Role | null>(null);
  const [agentBindings, setAgentBindings] = useState<AgentRoleBinding[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [deployedAgents, setDeployedAgents] = useState<ExternalAgentRef[]>([]);
  const [darkMode, setDarkMode] = useState(() => localStorage.getItem("agentManagementTheme") === "dark");
  const [currentUserGroupIds, setCurrentUserGroupIds] = useState<string[]>([]);
  const [subagentAddAcknowledged, setSubagentAddAcknowledged] = useState(false);
  const savedProjectSnapshot = useRef<string>(JSON.stringify(newProject()));
  const subagentAddTimeout = useRef<number | null>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const conversationId = useMemo(() => crypto.randomUUID(), []);
  const currentUserObjectId = useMemo(() => {
    const claims = account?.idTokenClaims as any;
    return claims?.oid || account?.localAccountId || "";
  }, [account]);
  const hasUnsavedChanges = useMemo(() => JSON.stringify(project) !== savedProjectSnapshot.current, [project]);
  const devProject = useMemo(() => projects.find((candidate) => candidate.id === devProjectId) ?? null, [devProjectId, projects]);
  const projectRoleBinding = useMemo(() => project.id ? agentBindings.find((binding) => binding.project_id === project.id) ?? null : null, [agentBindings, project.id]);
  const rolesForCurrentUser = useMemo(() => {
    const allowedIds = new Set([currentUserObjectId, ...currentUserGroupIds].filter(Boolean).map((id) => id.toLowerCase()));
    if (!allowedIds.size) return [];
    return roles.filter((role) => role.members.some((member) => allowedIds.has(member.object_id.toLowerCase())));
  }, [roles, currentUserObjectId, currentUserGroupIds]);
  const currentUserRoleIds = useMemo(() => new Set(rolesForCurrentUser.map((role) => role.id).filter(Boolean) as string[]), [rolesForCurrentUser]);
  const isAdmin = useMemo(() => rolesForCurrentUser.some((role) => isAdminRoleName(role.name)), [rolesForCurrentUser]);
  const isAgentCreator = useMemo(() => rolesForCurrentUser.some((role) => isAgentCreatorRoleName(role.name)), [rolesForCurrentUser]);
  const canCreateProjects = isAdmin || isAgentCreator;
  const assignableRoles = useMemo(() => {
    if (isAdmin) return roles;
    return rolesForCurrentUser.filter((role) => !isAgentCreatorRoleName(role.name));
  }, [isAdmin, roles, rolesForCurrentUser]);
  const visibleProjects = useMemo(() => {
    if (isAdmin) return projects;
    return projects.filter((candidate) => {
      const binding = agentBindings.find((item) => item.project_id === candidate.id);
      return binding?.role_ids.some((roleId) => currentUserRoleIds.has(roleId));
    });
  }, [projects, agentBindings, currentUserRoleIds, isAdmin]);
  const projectRoles = useCallback((candidate: AgentProject) => {
    const binding = agentBindings.find((item) => item.project_id === candidate.id);
    return (binding?.role_ids ?? [])
      .map((roleId) => roles.find((role) => role.id === roleId))
      .filter(Boolean) as Role[];
  }, [agentBindings, roles]);
  const memberLabel = (member: RoleMember) => member.display_name || member.object_id;
  const devProjectAgentCount = useMemo(() => {
    if (!devProject) return 0;
    if (devProject.deployment_mode === "standalone") return 1;
    if (devProject.deployment_mode === "orchestrator_only") return devProject.orchestrator_only?.external_agents?.length ?? 0;
    return devProject.orchestrator?.subagents?.length ?? 0;
  }, [devProject]);


  const login = useCallback(async () => {
    await instance.loginPopup(loginRequest);
  }, [instance]);

  const logout = useCallback(async () => {
    await instance.logoutPopup();
  }, [instance]);

  useEffect(() => {
    localStorage.setItem("agentManagementTheme", darkMode ? "dark" : "light");
  }, [darkMode]);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [devChatHistory, isDevRunning]);

  useEffect(() => () => {
    if (subagentAddTimeout.current !== null) {
      window.clearTimeout(subagentAddTimeout.current);
    }
  }, []);

  useEffect(() => {
    if (!isAdmin && activeTab === "roles") {
      setActiveTab("projects");
    }
    if (!canCreateProjects && activeTab === "create") {
      setActiveTab("projects");
    }
    if (!canCreateProjects && activeTab === "dev") {
      setActiveTab("projects");
    }
  }, [activeTab, canCreateProjects, isAdmin]);

  useEffect(() => {
    const claims = account?.idTokenClaims as any;
    const tokenGroups = Array.isArray(claims?.groups) ? claims.groups : [];
    if (!currentUserObjectId) {
      setCurrentUserGroupIds(tokenGroups);
      return;
    }
    fetch(`/api/directory/member-of/${encodeURIComponent(currentUserObjectId)}`)
      .then((response) => response.ok ? response.json() : { group_ids: tokenGroups })
      .then((payload) => setCurrentUserGroupIds(Array.from(new Set([...(payload.group_ids ?? []), ...tokenGroups]))))
      .catch(() => setCurrentUserGroupIds(tokenGroups));
  }, [account, currentUserObjectId]);

  const loadProjects = useCallback(async () => {
    setProjectsLoading(true);
    try {
      setStatus("Loading projects");
      const response = await fetch("/api/projects");
      const payload = await readApiResponse(response);
      const loadedProjects = payload.projects ?? [];
      setProjects(loadedProjects);
      setDevProjectId((current) => {
        if (current && loadedProjects.some((candidate: AgentProject) => candidate.id === current)) return current;
        return loadedProjects[0]?.id ?? "";
      });
      setStatus(`Loaded ${loadedProjects.length} projects`);
    } catch (error: any) {
      setStatus(`Project load failed: ${error.message}`);
    } finally {
      setProjectsLoading(false);
    }
  }, []);

  const createNewProject = () => {
    if (!canCreateProjects) {
      setStatus("You do not have access to create projects.");
      return;
    }
    const fresh = newProject();
    setProject(fresh);
    savedProjectSnapshot.current = JSON.stringify(fresh);
    setDeployment(null);
    setDeploymentUiState("idle");
    setDeploymentMessage("");
    setDevResponse("");
    setDevTrace([]);
    setDevDebug(null);
    setActiveTab("create");
    setStatus("New project ready");
  };

  const openProject = async (selectedProject: AgentProject) => {
    if (!selectedProject.id) return;
    const selectedProjectId = selectedProject.id;
    if (!isAdmin) {
      const binding = agentBindings.find((item) => item.project_id === selectedProjectId);
      const canOpen = binding?.role_ids.some((roleId) => currentUserRoleIds.has(roleId));
      if (!canOpen) {
        setStatus("You do not have access to this project.");
        return;
      }
    }
    try {
      setOpeningProjectId(selectedProjectId);
      setStatus(`Loading ${selectedProject.name}`);
      const response = await fetch(`/api/projects/${selectedProjectId}`);
      const fullProject = await readApiResponse(response);
      const cleanProject = { ...newProject(), ...fullProject, deployment: cleanDeployment(fullProject.deployment) ?? {} };
      setProject(cleanProject);
      savedProjectSnapshot.current = JSON.stringify(cleanProject);
      setDevProjectId(fullProject.id ?? "");
      setDeployment(cleanDeployment(fullProject.deployment));
      setDeploymentUiState("idle");
      setDeploymentMessage("");
      setDevResponse("");
      setDevTrace([]);
      setDevDebug(null);
      setActiveTab("create");
      setStatus(`Opened ${fullProject.name}`);
    } catch (error: any) {
      setStatus(`Open failed: ${error.message}`);
    } finally {
      setOpeningProjectId(null);
    }
  };

  const projectFoundryLink = (candidate: AgentProject) => {
    const deployment: any = candidate.deployment ?? {};
    const foundry = deployment.foundry ?? {};
    const link = deployment.foundry_agent_link || foundry.foundry_agent_link || "";
    return link.includes(".services.ai.azure.com/api/") ? "https://ai.azure.com/" : link;
  };

  const projectIsDeployed = (candidate: AgentProject) => {
    const deployment: any = candidate.deployment ?? {};
    return Boolean(projectFoundryLink(candidate) || (deployment.foundry && !deployment.foundry.errors));
  };

  const projectDeploymentLabel = (candidate: AgentProject) => {
    if (!projectIsDeployed(candidate)) return "Not deployed";
    const deployment: any = candidate.deployment ?? {};
    const version = deployment.version || deployment.info?.version || deployment.foundry?.version || deployment.foundry?.info?.version;
    return version ? `Deployed v${String(version).replace(/^v/i, "")}` : "Deployed";
  };

  const getFabricToken = useCallback(async (allowPopup = true) => {
    if (!account) throw new Error("Sign in first.");
    try {
      return (await instance.acquireTokenSilent({ ...fabricTokenRequest, account })).accessToken;
    } catch (error) {
      if (allowPopup && shouldUsePopupFallback(error)) {
        return (await instance.acquireTokenPopup({ ...fabricTokenRequest, account })).accessToken;
      }
      throw error;
    }
  }, [account, instance]);

  const getPowerBiToken = useCallback(async () => {
    if (!account) throw new Error("Sign in first.");
    try {
      return (await instance.acquireTokenSilent({ ...powerBiTokenRequest, account })).accessToken;
    } catch (error) {
      if (shouldUsePopupFallback(error)) {
        return (await instance.acquireTokenPopup({ ...powerBiTokenRequest, account })).accessToken;
      }
      throw error;
    }
  }, [account, instance]);

  const loadModels = useCallback(async (options: { allowPopup?: boolean } = {}) => {
    try {
      setIsLoadingModels(true);
      setStatus("Loading Fabric items");
      const token = await getFabricToken(options.allowPopup ?? true);
      const response = await fetch(`/api/fabric/items`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const payload = await response.json();
      const items: FabricItem[] = payload.items ?? [];
      items.sort((a, b) =>
        a.workspace_name.localeCompare(b.workspace_name) ||
        a.source_type.localeCompare(b.source_type) ||
        a.item_name.localeCompare(b.item_name)
      );
      setModels(items);
      setStatus(`Loaded ${items.length} Fabric items`);
    } catch (error: any) {
      setStatus(`Fabric items failed: ${error.message}`);
    } finally {
      setIsLoadingModels(false);
    }
  }, [getFabricToken]);

  const loadFoundryModels = useCallback(async () => {
    try {
      setIsLoadingFoundryModels(true);
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      const response = await fetch("/api/foundry/models", { signal: controller.signal });
      clearTimeout(timeout);
      const payload = await response.json();
      setFoundryModels(payload.deployments ?? []);
      setStatus(`Loaded ${(payload.deployments ?? []).length} Foundry models`);
    } catch (error: any) {
      setFoundryModels([]);
      setStatus(`Foundry models failed: ${error.name === "AbortError" ? "Request timed out" : error.message}`);
    } finally {
      setIsLoadingFoundryModels(false);
    }
  }, []);

  const loadDeployedAgents = useCallback(async () => {
    try {
      const response = await fetch("/api/deployed-agents");
      const payload = await response.json();
      setDeployedAgents(payload.agents ?? []);
    } catch {
      setDeployedAgents([]);
    }
  }, []);

  const loadRoles = useCallback(async () => {
    try {
      const response = await fetch("/api/roles");
      const payload = await response.json();
      setRoles(payload.roles ?? []);
    } catch {
      setRoles([]);
    }
  }, []);

  const loadAgentBindings = useCallback(async () => {
    try {
      const response = await fetch("/api/agent-bindings");
      const payload = await response.json();
      setAgentBindings(payload.bindings ?? []);
    } catch {
      setAgentBindings([]);
    }
  }, []);

  const saveRole = async (role: Role) => {
    const method = role.id ? "PUT" : "POST";
    const url = role.id ? `/api/roles/${role.id}` : "/api/roles";
    const response = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(role) });
    if (response.ok) {
      setEditingRole(null);
      await loadRoles();
      setStatus("Role saved.");
    } else {
      setStatus("Failed to save role.");
    }
  };

  const deleteRole = async (roleId: string) => {
    await fetch(`/api/roles/${roleId}`, { method: "DELETE" });
    await loadRoles();
    setStatus("Role deleted.");
  };

  const saveAgentBinding = async (binding: AgentRoleBinding) => {
    const response = await fetch(`/api/agent-bindings/${binding.project_id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(binding),
    });
    if (response.ok) {
      await loadAgentBindings();
      setStatus("Agent role assignment saved.");
    } else {
      setStatus("Failed to save agent role assignment.");
    }
  };

  const saveProjectRoleAssignment = async (roleIds: string[]) => {
    if (!project.id) {
      setStatus("Save the project before assigning roles.");
      return;
    }
    await saveAgentBinding({
      ...(projectRoleBinding ?? {}),
      project_id: project.id,
      agent_name: String(project.deployment?.agent_name || project.name),
      project_display_name: project.name,
      role_ids: roleIds,
    });
  };

  const addProjectRoleAssignment = async (roleId: string) => {
    if (!roleId) return;
    const currentRoleIds = projectRoleBinding?.role_ids ?? [];
    if (currentRoleIds.includes(roleId)) return;
    await saveProjectRoleAssignment([...currentRoleIds, roleId]);
  };

  const removeProjectRoleAssignment = async (roleId: string) => {
    const currentRoleIds = projectRoleBinding?.role_ids ?? [];
    await saveProjectRoleAssignment(currentRoleIds.filter((currentRoleId) => currentRoleId !== roleId));
  };

  useEffect(() => {
    if (account) loadModels().catch(() => {});
  }, [account, loadModels]);

  useEffect(() => {
    loadFoundryModels();
    loadRoles();
    loadAgentBindings();
  }, [loadFoundryModels, loadRoles, loadAgentBindings]);

  useEffect(() => {
    loadProjects();
  }, [loadProjects]);

  const saveProject = async () => {
    try {
      setStatus("Saving project");
      const response = await fetch(project.id ? `/api/projects/${project.id}` : "/api/projects", {
        method: project.id ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(project),
      });
      const saved = await readApiResponse(response);
      const cleanProject = { ...newProject(), ...saved, deployment: cleanDeployment(saved.deployment) ?? {} };
      setProject(cleanProject);
      savedProjectSnapshot.current = JSON.stringify(cleanProject);
      setDevProjectId(saved.id ?? "");
      setDeployment(cleanDeployment(saved.deployment));
      await loadProjects();
      setStatus("Project saved");
    } catch (error: any) {
      setStatus(`Save failed: ${error.message}`);
    }
  };

  const deleteProject = async (selectedProject: AgentProject) => {
    if (!selectedProject.id) return;
    if (!window.confirm(`Delete ${selectedProject.name}?`)) return;
    try {
      setDeletingProjectId(selectedProject.id);
      setStatus(`Deleting ${selectedProject.name}`);
      const response = await fetch(`/api/projects/${selectedProject.id}`, { method: "DELETE" });
      await readApiResponse(response);
      if (project.id === selectedProject.id) {
        const fresh = newProject();
        setProject(fresh);
        savedProjectSnapshot.current = JSON.stringify(fresh);
        setDeployment(null);
        setDevResponse("");
        setDevTrace([]);
        setDevDebug(null);
      }
      if (devProjectId === selectedProject.id) {
        setDevProjectId("");
      }
      await loadProjects();
      setStatus("Project deleted");
    } catch (error: any) {
      setStatus(`Delete failed: ${error.message}`);
    } finally {
      setDeletingProjectId(null);
    }
  };

  const promptKey = (target: "orchestrator" | "subagent" | "standalone" | "orchestrator_only", subagentId?: string) => `${target}:${subagentId ?? ""}`;
  const isGeneratingPrompt = (target: "orchestrator" | "subagent" | "standalone" | "orchestrator_only", subagentId?: string) =>
    generatingPrompt === promptKey(target, subagentId);

  const generatePrompt = async (target: "orchestrator" | "subagent" | "standalone" | "orchestrator_only", subagentId?: string) => {
    const activePrompt = promptKey(target, subagentId);
    try {
      setGeneratingPrompt(activePrompt);
      setStatus("Generating prompt with Foundry");
      const response = await fetch("/api/prompts/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project, target, subagent_id: subagentId }),
      });
      const payload = await readApiResponse(response);
      if (target === "orchestrator_only") {
        setProject({ ...project, orchestrator_only: { ...project.orchestrator_only, prompt: payload.prompt } });
      } else if (target === "orchestrator") {
        setProject({ ...project, orchestrator: { ...project.orchestrator, prompt: payload.prompt } });
      } else if (target === "standalone") {
        setProject({ ...project, standalone_agent: { ...project.standalone_agent, prompt: payload.prompt } });
      } else {
        setProject({
          ...project,
          orchestrator: {
            ...project.orchestrator,
            subagents: project.orchestrator.subagents.map((subagent) =>
              subagent.id === subagentId ? { ...subagent, prompt: payload.prompt } : subagent
            ),
          },
        });
      }
      setStatus("Prompt generated");
    } catch (error: any) {
      setStatus(`Prompt generation failed: ${error.message}`);
    } finally {
      setGeneratingPrompt(null);
    }
  };

  const runDevChat = async () => {
    const msg = devMessage.trim();
    if (!msg) return;
    try {
      setIsDevRunning(true);
      if (!devProject) throw new Error("Select a project first.");
      if (!visibleProjects.some((candidate) => candidate.id === devProject.id)) throw new Error("You do not have access to this project.");
      setDevChatHistory((prev) => [...prev, { role: "user", content: msg }]);
      setDevMessage("");
      const modeLabel = devRunLocal ? "locally (in-process)" : "against deployed Foundry agent";
      setStatus(`Running Dev UI ${modeLabel} for ${devProject.name}`);
      setDevTrace([{ step: "start", status: "running", detail: `Preparing context. Mode: ${devRunLocal ? "Local (traced)" : "Deployed (Foundry)"}.` }]);
      const projectResponse = await fetch(`/api/projects/${devProject.id}`);
      const fullDevProject = await readApiResponse(projectResponse);
      const fabricToken = account ? await getFabricToken() : undefined;
      const powerbiToken = account ? await getPowerBiToken() : undefined;
      const chatEndpoint = devRunLocal ? "/api/dev/chat-local" : "/api/dev/chat";
      const response = await fetch(chatEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: fullDevProject, message: msg, conversation_id: conversationId, fabric_token: fabricToken, powerbi_token: powerbiToken }),
      });
      const payload = await readApiResponse(response);
      const assistantMsg = payload.response ?? "No response";
      setDevResponse(assistantMsg);
      setDevChatHistory((prev) => [...prev, { role: "assistant", content: assistantMsg }]);
      setDevTrace(payload.debug?.trace ?? []);
      setDevDebug(payload.debug ?? null);
      setStatus("Dev UI run complete");
    } catch (error: any) {
      setStatus(`Dev UI failed: ${error.message}`);
      setDevChatHistory((prev) => [...prev, { role: "assistant", content: `Error: ${error.message}` }]);
      setDevTrace([{ step: "error", status: "failed", detail: error.message }]);
    } finally {
      setIsDevRunning(false);
    }
  };

  const deployToFoundry = async () => {
    try {
      setIsDeploying(true);
      setDeploymentUiState("working");
      setDeploymentMessage("Deploying to Foundry Hosted Agents...");
      setStatus("Submitting project to Foundry Hosted Agents");
      const response = await fetch("/api/deploy/submit-foundry", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project, submit_to_foundry: true }),
      });
      const payload = await readApiResponse(response);
      const nextDeployment = payload.build ? {
        ...payload.build,
        submitted: payload.submitted,
        foundry: payload.foundry,
        foundry_agent_link: payload.foundry?.foundry_agent_link,
        info: payload.info,
      } : payload;
      setDeployment(nextDeployment);
      setProject({ ...project, deployment: nextDeployment });
      await loadProjects();
      if (payload.foundry?.errors) {
        setDeploymentUiState("failed");
        setDeploymentMessage("Foundry deployment returned errors.");
        setStatus("Foundry deployment returned errors");
      } else {
        setDeploymentUiState("completed");
        setDeploymentMessage("Foundry deployment completed.");
        setStatus("Foundry deployment completed");
      }
    } catch (error: any) {
      if (error.payload?.build || error.payload?.foundry) {
        setDeployment(cleanDeployment({ ...(error.payload.build ?? {}), foundry: error.payload.foundry, errors: error.payload.errors }));
      }
      setDeploymentUiState("failed");
      setDeploymentMessage(`Foundry deployment failed: ${error.message}`);
      setStatus(`Foundry deployment failed: ${error.message}`);
    } finally {
      setIsDeploying(false);
    }
  };

  const addSubagent = () => {
    const next: SubagentConfig = {
      id: crypto.randomUUID(),
      name: `Subagent ${project.orchestrator.subagents.length + 1}`,
      description: "Answers questions for a selected data source.",
      semantic_model: emptyDataSource,
      model_config: emptyModelConfig,
      prompt: "",
      guardrails: [],
    };
    setProject({ ...project, orchestrator: { ...project.orchestrator, subagents: [...project.orchestrator.subagents, next] } });
    setSubagentAddAcknowledged(true);
    if (subagentAddTimeout.current !== null) {
      window.clearTimeout(subagentAddTimeout.current);
    }
    subagentAddTimeout.current = window.setTimeout(() => setSubagentAddAcknowledged(false), 1400);
  };

  const removeSubagent = (id: string) => {
    setProject({ ...project, orchestrator: { ...project.orchestrator, subagents: project.orchestrator.subagents.filter((subagent) => subagent.id !== id) } });
  };

  const addExternalAgent = (agent: ExternalAgentRef) => {
    const already = project.orchestrator_only.external_agents.some((ea) => ea.agent_name === agent.agent_name);
    if (already) return;
    const ref: ExternalAgentRef = { ...agent, id: crypto.randomUUID() };
    setProject({ ...project, orchestrator_only: { ...project.orchestrator_only, external_agents: [...project.orchestrator_only.external_agents, ref] } });
  };

  const removeExternalAgent = (id: string) => {
    setProject({ ...project, orchestrator_only: { ...project.orchestrator_only, external_agents: project.orchestrator_only.external_agents.filter((ea) => ea.id !== id) } });
  };

  const DEFAULT_MODEL: ModelConfig = { deployment_name: "gpt-5.4", model_display_name: "gpt-5.4" };

  const foundryModelPicker = (label: string, value: string, onSelect: (selected: ModelConfig) => void, keyPrefix = "model") => (
    <div className="foundry-model-picker">
      <label>{label}<select value={value || DEFAULT_MODEL.deployment_name} onChange={(e) => { const selected = foundryModels.find((m) => m.deployment_name === e.target.value) || (e.target.value === DEFAULT_MODEL.deployment_name ? DEFAULT_MODEL : undefined); onSelect(selected || emptyModelConfig); }}><option value={DEFAULT_MODEL.deployment_name}>{DEFAULT_MODEL.model_display_name}</option>{foundryModels.filter((m) => m.deployment_name !== DEFAULT_MODEL.deployment_name).map((m) => <option key={`${keyPrefix}-${m.deployment_name}`} value={m.deployment_name}>{m.model_display_name || m.deployment_name}</option>)}</select></label>
      <button type="button" className="icon-button" disabled={isLoadingFoundryModels} onClick={loadFoundryModels} title="Refresh Foundry models" aria-label="Refresh Foundry models">
        {isLoadingFoundryModels ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
      </button>
    </div>
  );

  return (
    <div className={darkMode ? "app-shell dark-mode" : "app-shell"}>
      <aside className="sidebar">
        <div className="brand"><Boxes size={24} /><span>Agent Management</span></div>
        <p>Fabric Agent Configuration</p>
        {account ? <div className="account">{account.username}<button onClick={logout} style={{ marginTop: 6, fontSize: 12 }}>Sign out</button></div> : <button onClick={login}>Sign in</button>}
        <nav>
          <button className={activeTab === "projects" ? "nav-tab active" : "nav-tab"} onClick={() => setActiveTab("projects")}><FolderOpen size={16} /> Projects</button>
          {isAdmin ? <button className={activeTab === "roles" ? "nav-tab active" : "nav-tab"} onClick={() => setActiveTab("roles")}><Shield size={16} /> Roles</button> : null}
          {canCreateProjects ? <button className={activeTab === "dev" ? "nav-tab active" : "nav-tab"} onClick={() => setActiveTab("dev")}><FlaskConical size={16} /> Dev UI</button> : null}
        </nav>
        <div className="sidebar-footer">
          <button className="theme-toggle" onClick={() => setDarkMode((current) => !current)}>
            {darkMode ? "Light mode" : "Dark mode"}
          </button>
        </div>
      </aside>

      <main>
        <header className="topbar">
          <div>
            <h1>Agent Management</h1>
            <p>Design, test, and deploy Fabric data agents into Foundry Hosted Agents</p>
          </div>
        </header>

        {activeTab === "projects" ? <section className="panel projects-panel">
          <div className="section-header"><h2>Projects</h2>{canCreateProjects ? <button onClick={createNewProject}><Plus size={16} /> New project</button> : null}</div>
          <div className="project-list">
            {projectsLoading ? <div className="loading-tile">
              <Loader2 className="loading-spinner" size={28} />
              <div>
                <h3>Loading projects</h3>
              </div>
            </div> : visibleProjects.length ? visibleProjects.map((savedProject) => {
              const foundryLink = projectFoundryLink(savedProject);
              const assignedRoles = projectRoles(savedProject);
              const isOpeningProject = openingProjectId === savedProject.id;
              return <div className="project-card" key={savedProject.id ?? savedProject.name}>
                <div>
                  <h3>{savedProject.name}</h3>
                  <p>{savedProject.description || "No description"}</p>
                  {assignedRoles.length ? <div className="project-role-list">
                    {assignedRoles.map((assignedRole) => <span className="member-tile role" key={assignedRole.id ?? assignedRole.name}><span className="member-tile-name">{assignedRole.name}</span></span>)}
                  </div> : null}
                </div>
                <div className="project-actions">
                  <button disabled={isOpeningProject} onClick={() => openProject(savedProject)}>
                    {isOpeningProject ? <Loader2 className="spin" size={16} /> : <FolderOpen size={16} />}
                    {isOpeningProject ? "Loading..." : "Open"}
                  </button>
                  {isAdmin ?
                    <button className="button-danger" disabled={deletingProjectId === savedProject.id} onClick={() => deleteProject(savedProject)}>
                      {deletingProjectId === savedProject.id ? <Loader2 className="spin" size={16} /> : <Trash2 size={16} />}
                      {deletingProjectId === savedProject.id ? "Deleting..." : "Delete"}
                    </button> : null}
                  {isOpeningProject ? <span className="project-loading-note">Loading...</span> : null}
                  <div className="project-meta"><span>{savedProject.deployment_mode}</span><span>{projectDeploymentLabel(savedProject)}</span></div>
                </div>
              </div>;
            }) : <div className="empty-state"><p>No projects available for your roles yet.</p>{canCreateProjects ? <button onClick={createNewProject}><Plus size={16} /> Create your first project</button> : null}</div>}
          </div>
        </section> : activeTab === "create" ? <>
          <section className="panel" id="designer">
            <div className="section-header"><h2>Agent Creation</h2><button onClick={saveProject} style={{ opacity: hasUnsavedChanges ? 1 : 0.6 }}><Save size={16} /> {hasUnsavedChanges ? "Save project" : "Saved"}</button></div>
            <div className="grid two">
              <label>Project name<input value={project.name} onChange={(e) => setProject({ ...project, name: e.target.value })} /></label>
              <label>Deployment mode<select value={project.deployment_mode} onChange={(e) => setProject({ ...project, deployment_mode: e.target.value as any })}><option value="orchestrator">Orchestrator with subagents</option><option value="orchestrator_only">Orchestrator Only</option><option value="standalone">Standalone agent</option></select></label>
            </div>
            <div className="grid two">
              <label className="role-add-picker">Add role{!project.id ? <span className="hint"> (save project first)</span> : null}
                <select disabled={!project.id || !assignableRoles.length} value="" onChange={(e) => { addProjectRoleAssignment(e.target.value); e.currentTarget.value = ""; }}>
                  <option value="">Choose a role...</option>
                  {assignableRoles.filter((role) => role.id && !(projectRoleBinding?.role_ids ?? []).includes(role.id) && !isAdminRoleName(role.name) && !isAgentCreatorRoleName(role.name)).map((role) => <option key={role.id} value={role.id!}>{role.name}</option>)}
                </select>
              </label>
              <label>Agent roles
                <div className="role-detail-list">
                  {(projectRoleBinding?.role_ids ?? []).length ? (projectRoleBinding?.role_ids ?? []).map((roleId) => {
                    const assignedRole = roles.find((role) => role.id === roleId);
                    return <span className="member-tile role" key={roleId}>
                      <span className="member-tile-name">{assignedRole?.name || roleId}</span>
                      <button className="member-tile-remove" onClick={() => removeProjectRoleAssignment(roleId)}>&times;</button>
                    </span>;
                  }) : <span className="role-member-empty">None</span>}
                </div>
              </label>
            </div>
            <label>Description<textarea value={project.description} onChange={(e) => setProject({ ...project, description: e.target.value })} /></label>
          </section>

          {project.deployment_mode === "standalone" ? (
          <section className="panel">
            <h2>Standalone Agent</h2>
            <div className="grid two"><label>Name<input value={project.standalone_agent.name} onChange={(e) => setProject({ ...project, standalone_agent: { ...project.standalone_agent, name: e.target.value } })} /></label><label>Data Source<DataSourceCombobox items={models} value={project.standalone_agent.semantic_model} onChange={(ref) => setProject({ ...project, standalone_agent: { ...project.standalone_agent, semantic_model: ref } })} onOpen={() => { if (!models.length) loadModels(); }} loading={isLoadingModels} /></label></div>
            <div className="grid two">{foundryModelPicker("Foundry Model", project.standalone_agent.model_config?.deployment_name || "", (selected) => setProject({ ...project, standalone_agent: { ...project.standalone_agent, model_config: selected } }), "standalone")}</div>
            <label>Description<textarea value={project.standalone_agent.description} onChange={(e) => setProject({ ...project, standalone_agent: { ...project.standalone_agent, description: e.target.value } })} /></label>
            <button disabled={generatingPrompt !== null} onClick={() => generatePrompt("standalone")}>
              {isGeneratingPrompt("standalone") ? <Loader2 className="spin" size={16} /> : <Wand2 size={16} />}
              {isGeneratingPrompt("standalone") ? "Generating..." : "Generate prompt"}
            </button>
            <textarea className="prompt" value={project.standalone_agent.prompt} onChange={(e) => setProject({ ...project, standalone_agent: { ...project.standalone_agent, prompt: e.target.value } })} />
          </section>
          ) : project.deployment_mode === "orchestrator_only" ? (
          <section className="panel" id="orchestrator-only">
            <div className="section-header"><h2>Orchestrator Only</h2></div>
            <label>Orchestrator name<input value={project.orchestrator_only.name} onChange={(e) => setProject({ ...project, orchestrator_only: { ...project.orchestrator_only, name: e.target.value } })} /></label>
            <div className="grid two">{foundryModelPicker("Orchestrator Foundry Model", project.orchestrator_only.model_config?.deployment_name || "", (selected) => setProject({ ...project, orchestrator_only: { ...project.orchestrator_only, model_config: selected } }), "orchestrator_only")}</div>
            <label>Description<textarea value={project.orchestrator_only.description} onChange={(e) => setProject({ ...project, orchestrator_only: { ...project.orchestrator_only, description: e.target.value } })} /></label>
            <div className="section-header"><h3>External Subagents</h3></div>
            <label>Add deployed agent
              <select value="" onChange={(e) => { const agent = deployedAgents.find((a) => a.agent_name === e.target.value); if (agent) addExternalAgent(agent); }} onFocus={() => { if (!deployedAgents.length) loadDeployedAgents(); }}>
                <option value="">Select a deployed agent...</option>
                {deployedAgents.filter((a) => !project.orchestrator_only.external_agents.some((ea) => ea.agent_name === a.agent_name)).map((a) => <option key={a.agent_name} value={a.agent_name}>{a.display_name} ({a.agent_name})</option>)}
              </select>
            </label>
            {project.orchestrator_only.external_agents.map((ea) => <div className="subagent" key={ea.id} style={{ position: "relative" }}>
              <button className="member-tile-remove" style={{ position: "absolute", top: 8, right: 8 }} onClick={() => removeExternalAgent(ea.id)}>&times;</button>
              <div className="grid two">
                <label>Agent name<input value={ea.agent_name} disabled /></label>
                <label>Display name<input value={ea.display_name} disabled /></label>
              </div>
              <label>Description<textarea value={ea.description} disabled /></label>
            </div>)}
            <div className="section-header"><h3>Orchestrator Prompt</h3></div>
            <span className="hint">Select subagents above before generating — their details will be included in the prompt.</span>
            <button disabled={generatingPrompt !== null} onClick={() => generatePrompt("orchestrator_only")}>
              {isGeneratingPrompt("orchestrator_only") ? <Loader2 className="spin" size={16} /> : <Wand2 size={16} />}
              {isGeneratingPrompt("orchestrator_only") ? "Generating..." : "Generate orchestrator prompt"}
            </button>
            <label>Orchestrator prompt<textarea className="prompt" value={project.orchestrator_only.prompt} onChange={(e) => setProject({ ...project, orchestrator_only: { ...project.orchestrator_only, prompt: e.target.value } })} /></label>
          </section>
          ) : (
          <section className="panel" id="prompts">
            <div className="section-header"><h2>Subagents</h2><button onClick={addSubagent}>{subagentAddAcknowledged ? <CheckCircle2 size={16} /> : <Plus size={16} />}{subagentAddAcknowledged ? "Added..." : "Add subagent"}</button></div>
            {project.orchestrator.subagents.map((subagent) => <div className="subagent" key={subagent.id}>
              <div className="section-header subagent-header">
                <h3>{subagent.name || "Subagent"}</h3>
                <button type="button" className="icon-button button-danger" onClick={() => removeSubagent(subagent.id)} title="Delete subagent" aria-label={`Delete ${subagent.name || "subagent"}`}>
                  <X size={16} />
                </button>
              </div>
              <div className="grid two"><label>Name<input value={subagent.name} onChange={(e) => setProject({ ...project, orchestrator: { ...project.orchestrator, subagents: project.orchestrator.subagents.map((item) => item.id === subagent.id ? { ...item, name: e.target.value } : item) } })} /></label><label>Data Source<DataSourceCombobox items={models} value={subagent.semantic_model} onChange={(ref) => setProject({ ...project, orchestrator: { ...project.orchestrator, subagents: project.orchestrator.subagents.map((item) => item.id === subagent.id ? { ...item, semantic_model: ref } : item) } })} onOpen={() => { if (!models.length) loadModels(); }} loading={isLoadingModels} /></label></div>
              <div className="grid two">{foundryModelPicker("Foundry Model", subagent.model_config?.deployment_name || "", (selected) => setProject({ ...project, orchestrator: { ...project.orchestrator, subagents: project.orchestrator.subagents.map((item) => item.id === subagent.id ? { ...item, model_config: selected } : item) } }), subagent.id)}</div>
              <label>Description<textarea value={subagent.description} onChange={(e) => setProject({ ...project, orchestrator: { ...project.orchestrator, subagents: project.orchestrator.subagents.map((item) => item.id === subagent.id ? { ...item, description: e.target.value } : item) } })} /></label>
              <button disabled={generatingPrompt !== null} onClick={() => generatePrompt("subagent", subagent.id)}>
                {isGeneratingPrompt("subagent", subagent.id) ? <Loader2 className="spin" size={16} /> : <Wand2 size={16} />}
                {isGeneratingPrompt("subagent", subagent.id) ? "Generating..." : "Generate subagent prompt"}
              </button>
              <textarea className="prompt" value={subagent.prompt} onChange={(e) => setProject({ ...project, orchestrator: { ...project.orchestrator, subagents: project.orchestrator.subagents.map((item) => item.id === subagent.id ? { ...item, prompt: e.target.value } : item) } })} />
            </div>)}
            <div className="section-header"><h2>Orchestrator</h2></div>
            <label>Orchestrator name<input value={project.orchestrator.name} onChange={(e) => setProject({ ...project, orchestrator: { ...project.orchestrator, name: e.target.value } })} /></label>
            <div className="grid two">{foundryModelPicker("Orchestrator Foundry Model", project.orchestrator.model_config?.deployment_name || "", (selected) => setProject({ ...project, orchestrator: { ...project.orchestrator, model_config: selected } }), "orchestrator")}</div>
            <button disabled={generatingPrompt !== null} onClick={() => generatePrompt("orchestrator")}>
              {isGeneratingPrompt("orchestrator") ? <Loader2 className="spin" size={16} /> : <Wand2 size={16} />}
              {isGeneratingPrompt("orchestrator") ? "Generating..." : "Generate orchestrator prompt"}
            </button>
            <textarea className="prompt" value={project.orchestrator.prompt} onChange={(e) => setProject({ ...project, orchestrator: { ...project.orchestrator, prompt: e.target.value } })} />
          </section>
          )}

          <section className="panel" id="deploy">
            <h2>Deploy to Foundry Hosted Agents</h2>
            <div className="action-row">
              <button disabled={isDeploying} onClick={deployToFoundry}>
                {isDeploying ? <Loader2 className="spin" size={16} /> : <ExternalLink size={16} />}
                {isDeploying ? "Deploying..." : "Deploy to Foundry"}
              </button>
            </div>
            {deploymentUiState !== "idle" ? (
              <div className={`deployment-state ${deploymentUiState}`} role="status" aria-live="polite">
                {deploymentUiState === "working" ? <Loader2 className="spin" size={18} /> : deploymentUiState === "completed" ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}
                <div>
                  <strong>{deploymentUiState === "working" ? "Deployment in progress" : deploymentUiState === "completed" ? "Deployment complete" : "Deployment failed"}</strong>
                  <span>{deploymentMessage}</span>
                  {deploymentUiState === "completed" && projectFoundryLink(project) ? <a href={projectFoundryLink(project)} target="_blank" rel="noreferrer">Open in Foundry</a> : null}
                </div>
              </div>
            ) : null}
            {(deployment?.info || project.deployment?.info) ? (() => {
              const info = deployment?.info || (project.deployment as any)?.info;
              return (
                <div className="deployment-info-card" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 16, marginTop: 12 }}>
                  <h3 style={{ margin: "0 0 12px 0", fontSize: 14 }}>Deployed Agent Info</h3>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 24px", fontSize: 13 }}>
                    <div><strong>Agent Name</strong><br />{info.agent_name || "—"}</div>
                    <div><strong>Version</strong><br />{info.version || "—"}</div>
                    <div style={{ gridColumn: "1 / -1" }}><strong>Agent Endpoint</strong><br /><code style={{ fontSize: 12, wordBreak: "break-all" }}>{info.agent_endpoint || "—"}</code></div>
                    <div><strong>Deployed At</strong><br />{info.deployed_at ? new Date(info.deployed_at).toLocaleString() : "—"}</div>
                  </div>
                  {info.foundry_agent_link ? <a href={info.foundry_agent_link} target="_blank" rel="noreferrer" style={{ display: "inline-block", marginTop: 8, fontSize: 13 }}>Open in Foundry Portal →</a> : null}
                </div>
              );
            })() : deployment ? <pre style={{ marginTop: 12 }}>{JSON.stringify(deployment, null, 2)}</pre> : null}
          </section>
        </> : activeTab === "roles" ? <section className="panel" id="roles">
          <div className="section-header"><h2>Roles &amp; Permissions</h2><button onClick={() => setEditingRole(newRole())}><Plus size={16} /> New Role</button></div>

          {editingRole ? <div className="role-editor" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 16, marginBottom: 16 }}>
            <h3>{editingRole.id ? "Edit Role" : "Create Role"}</h3>
            <div className="grid two">
              <label>Role name<input value={editingRole.name} onChange={(e) => setEditingRole({ ...editingRole, name: e.target.value })} /></label>
              <label>Description<input value={editingRole.description} onChange={(e) => setEditingRole({ ...editingRole, description: e.target.value })} /></label>
            </div>
            <h4 style={{ marginTop: 12 }}>Members</h4>
            <div className="member-tiles">
              {editingRole.members.map((member, idx) => (
                <span key={idx} className={`member-tile ${member.member_type}`}>
                  <span className="member-tile-name">{member.display_name || member.object_id}</span>
                  <button className="member-tile-remove" onClick={() => { const updated = editingRole.members.filter((_, i) => i !== idx); setEditingRole({ ...editingRole, members: updated }); }}>&times;</button>
                </span>
              ))}
            </div>
            <MemberAutocomplete
              value=""
              placeholder="Search and add members…"
              onChange={() => {}}
              onSelect={(r) => {
                if (!editingRole.members.some(m => m.object_id === r.object_id)) {
                  setEditingRole({ ...editingRole, members: [...editingRole.members, { object_id: r.object_id, display_name: r.display_name, member_type: r.member_type }] });
                }
              }}
              clearOnSelect
            />
            <div className="action-row" style={{ marginTop: 12 }}>
              <button onClick={() => saveRole(editingRole)}><Save size={16} /> Save Role</button>
              <button onClick={() => setEditingRole(null)}>Cancel</button>
            </div>
          </div> : null}

          <div className="role-list">
            {roles.length ? roles.map((role) => (
              <div className="project-card" key={role.id} style={{ marginBottom: 12 }}>
                <div>
                  <h3>{role.name}</h3>
                  <p>{role.description || "No description"}</p>
                  <p style={{ fontSize: 12, opacity: 0.7 }}>{role.members.length} member{role.members.length === 1 ? "" : "s"}</p>
                </div>
                <div className="project-actions">
                  <button onClick={() => setEditingRole({ ...role })}><FolderOpen size={16} /> Edit</button>
                  <button className="button-danger" onClick={() => role.id && deleteRole(role.id)}><Trash2 size={16} /> Delete</button>
                </div>
              </div>
            )) : <div className="empty-state"><p>No roles defined yet.</p><button onClick={() => setEditingRole(newRole())}><Plus size={16} /> Create your first role</button></div>}
          </div>

          <div style={{ marginTop: 24 }}>
            <div className="section-header"><h2>Agent Role Assignments</h2></div>
            {agentBindings.length ? agentBindings.map((binding) => (
              <div className="project-card" key={binding.id ?? binding.project_id} style={{ marginBottom: 12 }}>
                <div>
                  <h3>{binding.agent_name || binding.project_display_name}</h3>
                </div>
                <div className="member-tiles assignment-tags">
                  {binding.role_ids.length ? binding.role_ids.map((roleId) => {
                    const assignedRole = roles.find((role) => role.id === roleId);
                    return <span key={roleId} className="member-tile role">
                      <span className="member-tile-name">{assignedRole?.name || roleId}</span>
                      <button className="member-tile-remove" onClick={() => saveAgentBinding({ ...binding, role_ids: binding.role_ids.filter((currentRoleId) => currentRoleId !== roleId) })}>&times;</button>
                    </span>;
                  }) : <span className="model-pill empty">No roles</span>}
                </div>
              </div>
            )) : <div className="empty-state"><p>No agent role assignments yet. Deploy an agent to create bindings.</p></div>}
          </div>
        </section> : <section className="dev-layout" id="dev">
          <div className="panel dev-console">
            <div className="section-header"><h2>Agent Dev UI</h2>{devProject ? <span className="badge">{devProject.deployment_mode}</span> : null}{devRunLocal ? <span className="badge">local</span> : <span className="badge">deployed</span>}</div>
            <label>Project to test<select value={devProjectId} onChange={(event) => { setDevProjectId(event.target.value); setDevChatHistory([]); setDevTrace([]); setDevDebug(null); setDevResponse(""); }}><option value="">Select project</option>{visibleProjects.map((savedProject) => <option value={savedProject.id ?? ""} key={savedProject.id ?? savedProject.name}>{savedProject.name}</option>)}</select></label>
            <label className="toggle-label"><input type="checkbox" checked={devRunLocal} onChange={(e) => setDevRunLocal(e.target.checked)} /> Run locally (full tool tracing, no Foundry deployment needed)</label>
            {devProject ? <div className="project-test-summary"><strong>{devProject.name}</strong><span>{devProjectAgentCount} agent{devProjectAgentCount === 1 ? "" : "s"}</span><p>{devProject.description || "No description"}</p></div> : null}
            <div className="dev-chat-history">
              {devChatHistory.length === 0 ? <div className="empty-state"><p>Ask the agent a question to start a conversation.</p></div> : devChatHistory.map((msg, i) => (
                <div key={i} className={`dev-chat-msg dev-chat-${msg.role}`}>
                  <strong>{msg.role === "user" ? "You" : "Agent"}</strong>
                  <pre>{msg.content}</pre>
                </div>
              ))}
              {isDevRunning ? <div className="dev-chat-msg dev-chat-assistant"><strong>Agent</strong><p className="typing-indicator">Thinking...</p></div> : null}
              <div ref={chatEndRef} />
            </div>
            <div className="dev-chat-input">
              <input
                type="text"
                value={devMessage}
                onChange={(e) => setDevMessage(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !isDevRunning) { e.preventDefault(); runDevChat(); } }}
                placeholder={devProject ? "Ask the agent a question..." : "Select a project first"}
                disabled={isDevRunning || !devProject}
              />
              <button disabled={isDevRunning || !devProject || !devMessage.trim()} onClick={runDevChat}>
                {isDevRunning ? <Loader2 className="spin" size={16} /> : "Send"}
              </button>
            </div>
          </div>

          <div className="panel trace-panel">
            <h2>Behind The Scenes</h2>
            <div className="trace-list">
              {devTrace.length ? devTrace.map((step, index) => {
                const parseJsonStrings = (obj: any): any => {
                  if (typeof obj === "string") {
                    try { return parseJsonStrings(JSON.parse(obj)); } catch { return obj; }
                  }
                  if (Array.isArray(obj)) return obj.map(parseJsonStrings);
                  if (obj && typeof obj === "object") {
                    const out: any = {};
                    for (const [k, v] of Object.entries(obj)) out[k] = parseJsonStrings(v);
                    return out;
                  }
                  return obj;
                };
                const readable = (data: any) => JSON.stringify(parseJsonStrings(data), null, 2).replace(/\\n/g, "\n").replace(/\\"/g, '"');
                return <div className="trace-step" key={`${step.step}-${index}`}>
                  <div><strong>{step.step}</strong><span>{step.status}</span></div>
                  <p>{step.detail}</p>
                  {step.data ? <pre>{readable(step.data)}</pre> : null}
                </div>;
              }) : <div className="trace-step"><div><strong>idle</strong><span>waiting</span></div><p>Run the Dev UI to inspect routing, metadata lookup, prompt construction, and model response steps.</p></div>}
            </div>
            <pre>{devDebug ? (() => {
              const parseJsonStrings = (obj: any): any => {
                if (typeof obj === "string") {
                  try { return parseJsonStrings(JSON.parse(obj)); } catch { return obj; }
                }
                if (Array.isArray(obj)) return obj.map(parseJsonStrings);
                if (obj && typeof obj === "object") {
                  const out: any = {};
                  for (const [k, v] of Object.entries(obj)) out[k] = parseJsonStrings(v);
                  return out;
                }
                return obj;
              };
              return JSON.stringify(parseJsonStrings(devDebug), null, 2).replace(/\\n/g, "\n").replace(/\\"/g, '"');
            })() : "No debug payload yet."}</pre>
          </div>
        </section>}
        {activeTab === "projects" ? null : <div className="status">{status}</div>}
      </main>
    </div>
  );
}
