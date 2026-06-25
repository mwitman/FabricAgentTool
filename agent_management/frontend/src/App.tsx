import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { InteractionRequiredAuthError } from "@azure/msal-browser";
import { useMsal } from "@azure/msal-react";
import { AlertTriangle, Bot, Boxes, CalendarClock, CheckCircle2, ExternalLink, FlaskConical, FolderOpen, Loader2, Moon, Plus, RefreshCw, Save, Shield, Sun, Trash2, Wand2, X } from "lucide-react";
import { fabricTokenRequest, loginRequest, powerBiTokenRequest } from "./authConfig";
import { AgentRoleBinding, emptyDataSource, emptyModelConfig, emptyRoleMember, ExternalAgentRef, FabricItem, ModelConfig, newProject, newRole, Role, RoleMember, AgentProject, SubagentConfig } from "./types";
import MemberAutocomplete from "./MemberAutocomplete";
import DataSourceCombobox from "./DataSourceCombobox";

type ActiveTab = "projects" | "create" | "dev" | "roles" | "metadata" | "runtime";

type DevTraceStep = {
  step: string;
  status: string;
  detail: string;
  data?: unknown;
};

type DeploymentUiState = "idle" | "working" | "completed" | "failed";

type MetadataRefreshSchedule = {
  id?: string;
  name: string;
  enabled: boolean;
  cron: string;
  timezone: string;
  next_run_at?: string;
  last_run_at?: string;
  last_status?: string;
};

type MetadataRefreshRun = {
  id: string;
  trigger?: string;
  started_at?: string;
  finished_at?: string;
  status?: string;
  semantic_models_found?: number;
  models_refreshed?: number;
  models_failed?: number;
  errors?: unknown[];
};

type SemanticMetadataSummary = {
  id: string;
  semantic_model_name?: string;
  workspace_name?: string;
  status?: string;
  refreshed_at?: string;
  last_error?: unknown;
};

type RuntimeVersion = {
  version: string;
  digest?: string;
  is_latest?: boolean;
  updated_at?: string;
};

type ProjectVersionSummary = {
  version: string;
  foundry_version?: string;
  project_version?: string;
  runtime_version?: string;
  deployed_at: string;
  deployed_by?: string;
};

const weekDays = [
  { value: "1", short: "Mon", label: "Monday" },
  { value: "2", short: "Tue", label: "Tuesday" },
  { value: "3", short: "Wed", label: "Wednesday" },
  { value: "4", short: "Thu", label: "Thursday" },
  { value: "5", short: "Fri", label: "Friday" },
  { value: "6", short: "Sat", label: "Saturday" },
  { value: "0", short: "Sun", label: "Sunday" },
];

const allWeekDayValues = weekDays.map((day) => day.value);

const timezoneOptions = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Phoenix",
  "America/Los_Angeles",
  "America/Anchorage",
  "Pacific/Honolulu",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
];

function scheduleTimezoneOptions(currentTimezone: string) {
  return currentTimezone && !timezoneOptions.includes(currentTimezone)
    ? [currentTimezone, ...timezoneOptions]
    : timezoneOptions;
}

function parseScheduleCron(cron: string) {
  const parts = cron.trim().split(/\s+/);
  if (parts.length < 5) return { time: "02:00", days: allWeekDayValues };
  const minute = Number(parts[0]);
  const hour = Number(parts[1]);
  const time = Number.isInteger(hour) && Number.isInteger(minute)
    ? `${String(Math.min(Math.max(hour, 0), 23)).padStart(2, "0")}:${String(Math.min(Math.max(minute, 0), 59)).padStart(2, "0")}`
    : "02:00";
  const days = parts[4] === "*"
    ? allWeekDayValues
    : parts[4].split(",").map((day) => day.trim()).filter((day) => allWeekDayValues.includes(day));
  return { time, days: days.length ? days : allWeekDayValues };
}

function buildScheduleCron(time: string, days: string[]) {
  const [hour = "2", minute = "0"] = time.split(":");
  const selectedDays = days.length === allWeekDayValues.length ? "*" : days.join(",");
  return `${Number(minute) || 0} ${Number(hour) || 0} * * ${selectedDays || "*"}`;
}

function describeSchedule(schedule: MetadataRefreshSchedule) {
  const { time, days } = parseScheduleCron(schedule.cron);
  const [hour, minute] = time.split(":").map(Number);
  const date = new Date();
  date.setHours(hour || 0, minute || 0, 0, 0);
  const timeLabel = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const dayLabel = days.length === allWeekDayValues.length
    ? "Every day"
    : weekDays.filter((day) => days.includes(day.value)).map((day) => day.short).join(", ");
  return `${dayLabel} at ${timeLabel}`;
}

function metadataRunExecutionType(trigger?: string) {
  return trigger === "run_now" ? "Run now" : "Schedule";
}

function metadataRunMessage(run: MetadataRefreshRun) {
  if (!run.errors?.length) return run.status === "succeeded" ? "Succeeded" : run.status || "Completed";
  const firstError = run.errors[0] as any;
  const message = firstError?.error?.[0]?.message?.error?.message
    || firstError?.error?.message?.error?.message
    || firstError?.message
    || firstError?.error
    || "Metadata refresh failed.";
  return typeof message === "string" ? message : JSON.stringify(message);
}

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
const isDeveloperRoleName = (name: string) => roleName(name) === "developer";

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
  const [metadataSchedules, setMetadataSchedules] = useState<MetadataRefreshSchedule[]>([]);
  const [metadataRuns, setMetadataRuns] = useState<MetadataRefreshRun[]>([]);
  const [semanticMetadata, setSemanticMetadata] = useState<SemanticMetadataSummary[]>([]);
  const [editingSchedule, setEditingSchedule] = useState<MetadataRefreshSchedule | null>(null);
  const [isMetadataLoading, setIsMetadataLoading] = useState(false);
  const [editingRole, setEditingRole] = useState<Role | null>(null);
  const [agentBindings, setAgentBindings] = useState<AgentRoleBinding[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [deployedAgents, setDeployedAgents] = useState<ExternalAgentRef[]>([]);
  const [darkMode, setDarkMode] = useState(() => localStorage.getItem("agentManagementTheme") === "dark");
  const [currentUserGroupIds, setCurrentUserGroupIds] = useState<string[]>([]);
  const [projectVersions, setProjectVersions] = useState<ProjectVersionSummary[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null);
  const [selectedDeploymentProjectVersion, setSelectedDeploymentProjectVersion] = useState("");
  const [runtimeVersions, setRuntimeVersions] = useState<RuntimeVersion[]>([]);
  const [selectedRuntimeVersion, setSelectedRuntimeVersion] = useState("");
  const [selectedBulkRuntimeVersion, setSelectedBulkRuntimeVersion] = useState("");
  const [selectedBulkProjectIds, setSelectedBulkProjectIds] = useState<string[]>([]);
  const [isBulkDeploying, setIsBulkDeploying] = useState(false);
  const [bulkDeploymentResult, setBulkDeploymentResult] = useState<any>(null);
  const [runtimeVersionMessage, setRuntimeVersionMessage] = useState("");
  const [viewingVersionSnapshot, setViewingVersionSnapshot] = useState(false);
  const [subagentAddAcknowledged, setSubagentAddAcknowledged] = useState(false);
  const savedProjectSnapshot = useRef<string>(JSON.stringify(newProject()));
  const subagentAddTimeout = useRef<number | null>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const [conversationId, setConversationId] = useState(() => crypto.randomUUID());
  const currentUserCandidateIds = useMemo(() => {
    const claims = account?.idTokenClaims as any;
    const homeAccountObjectId = account?.homeAccountId?.split(".")[0] || "";
    return Array.from(new Set([
      claims?.oid,
      claims?.objectId,
      claims?.sub,
      account?.localAccountId,
      homeAccountObjectId,
    ].filter(Boolean).map((id) => String(id))));
  }, [account]);
  const currentUserObjectId = currentUserCandidateIds[0] || "";
  const hasUnsavedChanges = useMemo(() => JSON.stringify(project) !== savedProjectSnapshot.current, [project]);
  const devProject = useMemo(() => projects.find((candidate) => candidate.id === devProjectId) ?? null, [devProjectId, projects]);
  const projectRoleBinding = useMemo(() => project.id ? agentBindings.find((binding) => binding.project_id === project.id) ?? null : null, [agentBindings, project.id]);
  const rolesForCurrentUser = useMemo(() => {
    const allowedIds = new Set([...currentUserCandidateIds, ...currentUserGroupIds].filter(Boolean).map((id) => id.toLowerCase()));
    if (!allowedIds.size) return [];
    return roles.filter((role) => role.members.some((member) => allowedIds.has(member.object_id.toLowerCase())));
  }, [roles, currentUserCandidateIds, currentUserGroupIds]);
  const currentUserRoleIds = useMemo(() => new Set(rolesForCurrentUser.map((role) => role.id).filter(Boolean) as string[]), [rolesForCurrentUser]);
  const isAdmin = useMemo(() => rolesForCurrentUser.some((role) => isAdminRoleName(role.name)), [rolesForCurrentUser]);
  const isDeveloper = useMemo(() => rolesForCurrentUser.some((role) => isDeveloperRoleName(role.name)), [rolesForCurrentUser]);
  const canCreateProjects = isAdmin || isDeveloper;
  const adminHeaders = useMemo(() => ({
    "x-user-object-id": currentUserObjectId,
    "x-user-group-ids": currentUserGroupIds.join(","),
  }), [currentUserObjectId, currentUserGroupIds]);
  const assignableRoles = useMemo(() => {
    if (isAdmin) return roles;
    return rolesForCurrentUser.filter((role) => !isDeveloperRoleName(role.name));
  }, [isAdmin, roles, rolesForCurrentUser]);
  const visibleProjects = useMemo(() => {
    if (isAdmin) return projects;
    return projects.filter((candidate) => {
      const binding = agentBindings.find((item) => item.project_id === candidate.id);
      return binding?.role_ids.some((roleId) => currentUserRoleIds.has(roleId));
    });
  }, [projects, agentBindings, currentUserRoleIds, isAdmin]);
  const projectVersionOptions = useMemo(() => {
    const seen = new Set<string>();
    return projectVersions.filter((item) => {
      const projectVersion = String(item.project_version || item.version);
      if (seen.has(projectVersion)) return false;
      seen.add(projectVersion);
      return true;
    });
  }, [projectVersions]);
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
    if (!isAdmin && (activeTab === "roles" || activeTab === "metadata" || activeTab === "runtime")) {
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
    loadRuntimeVersions();
  }, []);

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
        return "";
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
      setConversationId(crypto.randomUUID());
      setDevChatHistory([]);
      setDeployment(cleanDeployment(fullProject.deployment));
      setDeploymentUiState("idle");
      setDeploymentMessage("");
      setDevResponse("");
      setDevTrace([]);
      setDevDebug(null);
      setActiveTab("create");
      setSelectedVersion(null);
      setSelectedDeploymentProjectVersion("");
      setViewingVersionSnapshot(false);
      setStatus(`Opened ${fullProject.name}`);
      // Load version history
      loadVersions(selectedProjectId);
    } catch (error: any) {
      setStatus(`Open failed: ${error.message}`);
    } finally {
      setOpeningProjectId(null);
    }
  };

  const loadVersions = async (projectId: string) => {
    try {
      const response = await fetch(`/api/projects/${projectId}/versions`);
      if (response.ok) {
        const data = await response.json();
        setProjectVersions(data.versions || []);
      } else {
        setProjectVersions([]);
      }
    } catch {
      setProjectVersions([]);
    }
  };

  const loadRuntimeVersions = async () => {
    try {
      setRuntimeVersionMessage("Loading runtime versions...");
      const response = await fetch("/api/runtime/versions");
      const data = await readApiResponse(response);
      const versions = data.versions || [];
      setRuntimeVersions(versions);
      const defaultVersion = data.latest_version || versions[0]?.version || "";
      setSelectedRuntimeVersion((current) => current || defaultVersion);
      setSelectedBulkRuntimeVersion((current) => current || defaultVersion);
      setRuntimeVersionMessage(versions.length ? "" : "No ACR runtime versions found");
    } catch (error: any) {
      setRuntimeVersions([]);
      setRuntimeVersionMessage(`Runtime versions unavailable: ${error.message}`);
    }
  };

  const switchToVersion = async (version: string) => {
    if (!project.id) return;
    try {
      setStatus(`Loading version ${version}...`);
      const response = await fetch(`/api/projects/${project.id}/versions/${version}`);
      const data = await readApiResponse(response);
      if (data.snapshot) {
        const versionProject = { ...newProject(), ...data.snapshot, deployment: cleanDeployment(data.snapshot.deployment) ?? {} };
        const projectVersion = data.project_version || data.snapshot?.deployment?.info?.project_version || version;
        setProject(versionProject);
        setSelectedVersion(version);
        setSelectedDeploymentProjectVersion(version);
        setViewingVersionSnapshot(true);
        setStatus(`Viewing project version ${projectVersion}`);
      }
    } catch (error: any) {
      setStatus(`Failed to load version: ${error.message}`);
    }
  };

  const selectProjectVersion = (version: string) => {
    if (!version) {
      switchToLive();
      return;
    }
    switchToVersion(version);
  };

  const switchToLive = () => {
    if (!project.id) return;
    // Re-open the live project from Cosmos
    const liveProject = projects.find((p) => p.id === project.id);
    if (liveProject) openProject(liveProject);
    else {
      setSelectedVersion(null);
      setSelectedDeploymentProjectVersion("");
      setViewingVersionSnapshot(false);
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
    const version = deployment.info?.foundry_version || deployment.info?.version || deployment.foundry_version || deployment.version || deployment.foundry?.version || deployment.foundry?.name || deployment.foundry?.info?.version;
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

  const loadMetadataAdmin = useCallback(async () => {
    if (!isAdmin) return;
    setIsMetadataLoading(true);
    try {
      const [schedulesResponse, runsResponse, metadataResponse] = await Promise.all([
        fetch("/api/admin/metadata-refresh/schedules", { headers: adminHeaders }),
        fetch("/api/admin/metadata-refresh/runs", { headers: adminHeaders }),
        fetch("/api/admin/semantic-metadata", { headers: adminHeaders }),
      ]);
      const schedulesPayload = await readApiResponse(schedulesResponse);
      const runsPayload = await readApiResponse(runsResponse);
      const metadataPayload = await readApiResponse(metadataResponse);
      setMetadataSchedules(schedulesPayload.schedules ?? []);
      setMetadataRuns(runsPayload.runs ?? []);
      setSemanticMetadata(metadataPayload.metadata ?? []);
    } catch (error: any) {
      setStatus(`Metadata refresh load failed: ${error.message}`);
    } finally {
      setIsMetadataLoading(false);
    }
  }, [adminHeaders, isAdmin]);

  const saveMetadataSchedule = async (schedule: MetadataRefreshSchedule) => {
    const method = schedule.id ? "PUT" : "POST";
    const url = schedule.id ? `/api/admin/metadata-refresh/schedules/${schedule.id}` : "/api/admin/metadata-refresh/schedules";
    const response = await fetch(url, { method, headers: { "Content-Type": "application/json", ...adminHeaders }, body: JSON.stringify(schedule) });
    await readApiResponse(response);
    setEditingSchedule(null);
    await loadMetadataAdmin();
    setStatus("Metadata refresh schedule saved.");
  };

  const deleteMetadataSchedule = async (scheduleId: string) => {
    const response = await fetch(`/api/admin/metadata-refresh/schedules/${scheduleId}`, { method: "DELETE", headers: adminHeaders });
    await readApiResponse(response);
    await loadMetadataAdmin();
    setStatus("Metadata refresh schedule deleted.");
  };

  const runMetadataRefreshNow = async () => {
    setIsMetadataLoading(true);
    try {
      const response = await fetch("/api/admin/metadata-refresh/run-now", { method: "POST", headers: adminHeaders });
      const payload = await readApiResponse(response);
      await loadMetadataAdmin();
      setStatus(`Metadata refresh ${payload.status ?? "completed"}: ${payload.models_refreshed ?? 0} refreshed, ${payload.models_failed ?? 0} failed.`);
    } catch (error: any) {
      setStatus(`Metadata refresh failed: ${error.message}`);
    } finally {
      setIsMetadataLoading(false);
    }
  };

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

  useEffect(() => {
    if (activeTab === "metadata") {
      loadMetadataAdmin();
    }
  }, [activeTab, loadMetadataAdmin]);

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
      setConversationId(crypto.randomUUID());
      setDevChatHistory([]);
      setDeployment(cleanDeployment(saved.deployment));
      setSelectedVersion(null);
      setSelectedDeploymentProjectVersion("");
      setViewingVersionSnapshot(false);
      await loadProjects();
      if (saved.id) await loadVersions(saved.id);
      setStatus("Draft saved");
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
        body: JSON.stringify({
          project,
          submit_to_foundry: true,
          project_version: selectedDeploymentProjectVersion || (viewingVersionSnapshot ? selectedVersion : ""),
          runtime_version: selectedRuntimeVersion,
        }),
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
      if (project.id) loadVersions(project.id);
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

  const deployBulkRuntime = async () => {
    if (!selectedBulkRuntimeVersion || selectedBulkProjectIds.length === 0) return;
    setIsBulkDeploying(true);
    setBulkDeploymentResult(null);
    try {
      const response = await fetch("/api/admin/deploy/runtime", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...adminHeaders },
        body: JSON.stringify({ project_ids: selectedBulkProjectIds, runtime_version: selectedBulkRuntimeVersion }),
      });
      const payload = await readApiResponse(response);
      setBulkDeploymentResult(payload);
      await loadProjects();
      setStatus(`Runtime deployment ${payload.status}: ${payload.succeeded ?? 0} succeeded, ${payload.failed ?? 0} failed.`);
    } catch (error: any) {
      setBulkDeploymentResult({ status: "failed", error: error.message, results: [] });
      setStatus(`Runtime deployment failed: ${error.message}`);
    } finally {
      setIsBulkDeploying(false);
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
          {isAdmin ? <button className={activeTab === "metadata" ? "nav-tab active" : "nav-tab"} onClick={() => setActiveTab("metadata")}><CalendarClock size={16} /> Metadata</button> : null}
          {isAdmin ? <button className={activeTab === "runtime" ? "nav-tab active" : "nav-tab"} onClick={() => setActiveTab("runtime")}><ExternalLink size={16} /> Runtime Deploy</button> : null}
          {canCreateProjects ? <button className={activeTab === "dev" ? "nav-tab active" : "nav-tab"} onClick={() => { setDevProjectId(""); setDevChatHistory([]); setDevTrace([]); setDevDebug(null); setDevResponse(""); setConversationId(crypto.randomUUID()); setActiveTab("dev"); }}><FlaskConical size={16} /> Dev UI</button> : null}
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
            }) : <div className="empty-state">
              <p>No projects available for your roles yet.</p>
              {projects.length ? <p className="muted">Loaded {projects.length} project{projects.length === 1 ? "" : "s"}, but your current session matched {rolesForCurrentUser.length} role{rolesForCurrentUser.length === 1 ? "" : "s"}{rolesForCurrentUser.length ? `: ${rolesForCurrentUser.map((role) => role.name).join(", ")}` : "."}</p> : null}
              {currentUserCandidateIds.length ? <p className="muted">Signed-in ID candidates: {currentUserCandidateIds.join(", ")}</p> : null}
              {canCreateProjects ? <button onClick={createNewProject}><Plus size={16} /> Create your first project</button> : null}
            </div>}
          </div>
        </section> : activeTab === "create" ? <>
          <section className="panel" id="designer">
            <div className="section-header"><h2>Agent Creation</h2><button onClick={saveProject} style={{ opacity: hasUnsavedChanges ? 1 : 0.6 }}><Save size={16} /> {hasUnsavedChanges ? "Save project" : "Saved"}</button></div>
            <div className="grid two">
              <label>Project name<input value={project.name} onChange={(e) => setProject({ ...project, name: e.target.value })} /></label>
              <label>Deployment mode<select value={project.deployment_mode} onChange={(e) => setProject({ ...project, deployment_mode: e.target.value as any })}><option value="orchestrator">Orchestrator with subagents</option><option value="orchestrator_only">Orchestrator Only</option><option value="standalone">Standalone agent</option></select></label>
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
              <button disabled={isDeploying || !selectedRuntimeVersion} onClick={deployToFoundry}>
                {isDeploying ? <Loader2 className="spin" size={16} /> : <ExternalLink size={16} />}
                {isDeploying ? "Deploying..." : "Deploy to Foundry"}
              </button>
              <button disabled={isDeploying} onClick={loadRuntimeVersions}><RefreshCw size={16} /> Refresh runtime versions</button>
            </div>
            <div className="grid two" style={{ marginTop: 12 }}>
              <label>Project Version
                <select value={selectedDeploymentProjectVersion} onChange={(e) => selectProjectVersion(e.target.value)}>
                  <option value="">Current Draft</option>
                  {projectVersionOptions.map((v) => (
                    <option key={`deploy-project-${v.version}`} value={v.version}>
                      v{String(v.project_version || v.version).replace(/^v/i, "")}
                    </option>
                  ))}
                </select>
              </label>
              <label>Runtime Version
                <select value={selectedRuntimeVersion} onChange={(e) => setSelectedRuntimeVersion(e.target.value)}>
                  <option value="">Select runtime version</option>
                  {runtimeVersions.map((v) => (
                    <option key={`runtime-${v.version}`} value={v.version}>
                      {v.version}{v.is_latest ? " — current latest" : ""}{v.updated_at ? ` — ${new Date(v.updated_at).toLocaleString()}` : ""}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {runtimeVersionMessage ? <p className="muted" style={{ marginTop: 8 }}>{runtimeVersionMessage}</p> : null}
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
            {(deployment?.info || project.deployment?.info || project.deployment) ? (() => {
              const info = deployment?.info || (project.deployment as any)?.info;
              const dep = deployment || project.deployment as any;
              const agentName = info?.agent_name || dep?.agent_name || "—";
              const foundryVersion = info?.foundry_version || info?.version || dep?.foundry_version || dep?.foundry?.version || dep?.version || "—";
              const runtimeImage = info?.runtime_image || dep?.image || "—";
              const runtimeVersion = info?.runtime_version || dep?.runtime_version || "";
              const runtimeImageSource = info?.runtime_image_source || dep?.runtime_image_source || "";
              const projectVersion = info?.project_version || dep?.project_version || "";
              const agentEndpoint = info?.agent_endpoint || dep?.agent_endpoint || dep?.foundry_agent_link || "—";
              const deployedAt = info?.deployed_at || dep?.deployed_at || dep?.foundry?.created_at;
              const foundryLink = info?.foundry_agent_link || dep?.foundry_agent_link || dep?.foundry?.foundry_agent_link;
              return (
                <div className="deployment-info-card" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 16, marginTop: 12 }}>
                  <h3 style={{ margin: "0 0 12px 0", fontSize: 14 }}>Current Deployed Agent Info</h3>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "8px 24px", fontSize: 13 }}>
                    <div><strong>Agent Name</strong><br />{agentName}</div>
                    <div><strong>Foundry Version</strong><br />{foundryVersion}</div>
                    <div><strong>Project Version</strong><br />{projectVersion ? `v${String(projectVersion).replace(/^v/i, "")}` : "—"}</div>
                    <div><strong>Runtime Version</strong><br />{runtimeVersion || "—"}</div>
                    <div style={{ gridColumn: "1 / -1" }}><strong>Runtime Image</strong><br /><code style={{ fontSize: 12, wordBreak: "break-all" }}>{runtimeImage}</code></div>
                    {runtimeImageSource && runtimeImageSource !== runtimeImage ? <div style={{ gridColumn: "1 / -1" }}><strong>Resolved From</strong><br /><code style={{ fontSize: 12, wordBreak: "break-all" }}>{runtimeImageSource}</code></div> : null}
                    <div style={{ gridColumn: "1 / -1" }}><strong>Agent Endpoint</strong><br /><code style={{ fontSize: 12, wordBreak: "break-all" }}>{agentEndpoint}</code></div>
                    <div><strong>Deployed At</strong><br />{deployedAt ? new Date(deployedAt).toLocaleString() : "—"}</div>
                  </div>
                  {foundryLink ? <a href={foundryLink} target="_blank" rel="noreferrer" style={{ display: "inline-block", marginTop: 8, fontSize: 13 }}>Open in Foundry Portal →</a> : null}
                </div>
              );
            })() : null}
          </section>
        </> : activeTab === "runtime" ? (() => {
          const deployedProjects = projects.filter(projectIsDeployed);
          const allSelected = deployedProjects.length > 0 && deployedProjects.every((item) => selectedBulkProjectIds.includes(item.id || ""));
          const toggleProject = (projectId: string) => setSelectedBulkProjectIds((current) => current.includes(projectId) ? current.filter((item) => item !== projectId) : [...current, projectId]);
          return <section className="panel" id="runtime-deployments">
            <div className="section-header">
              <h2>Runtime Deployments</h2>
              <div className="action-row">
                <button disabled={isBulkDeploying || !selectedBulkRuntimeVersion || selectedBulkProjectIds.length === 0} onClick={deployBulkRuntime}>
                  {isBulkDeploying ? <Loader2 className="spin" size={16} /> : <ExternalLink size={16} />}
                  {isBulkDeploying ? "Deploying..." : "Deploy selected"}
                </button>
                <button disabled={isBulkDeploying} onClick={loadRuntimeVersions}><RefreshCw size={16} /> Refresh versions</button>
              </div>
            </div>
            <div className="grid two">
              <label>Runtime Version
                <select value={selectedBulkRuntimeVersion} onChange={(e) => setSelectedBulkRuntimeVersion(e.target.value)}>
                  <option value="">Select runtime version</option>
                  {runtimeVersions.map((version) => (
                    <option key={`bulk-runtime-${version.version}`} value={version.version}>
                      {version.version}{version.is_latest ? " — current latest" : ""}{version.updated_at ? ` — ${new Date(version.updated_at).toLocaleString()}` : ""}
                    </option>
                  ))}
                </select>
              </label>
              <label>Agents
                <select value={allSelected ? "all" : "custom"} onChange={(e) => setSelectedBulkProjectIds(e.target.value === "all" ? deployedProjects.map((item) => item.id || "").filter(Boolean) : [])}>
                  <option value="custom">Custom selection ({selectedBulkProjectIds.length})</option>
                  <option value="all">All deployed agents ({deployedProjects.length})</option>
                </select>
              </label>
            </div>
            {runtimeVersionMessage ? <p className="muted" style={{ marginTop: 8 }}>{runtimeVersionMessage}</p> : null}
            <div style={{ marginTop: 16, display: "grid", gap: 8 }}>
              {deployedProjects.length ? deployedProjects.map((item) => {
                const dep: any = item.deployment || {};
                const info = dep.info || {};
                const projectId = item.id || "";
                const isSelected = selectedBulkProjectIds.includes(projectId);
                return <label key={`bulk-project-${projectId}`} style={{ display: "grid", gridTemplateColumns: "auto 1fr auto auto", gap: 12, alignItems: "center", border: "1px solid var(--border)", borderRadius: 8, padding: 12 }}>
                  <input type="checkbox" checked={isSelected} onChange={() => toggleProject(projectId)} />
                  <span><strong>{item.name}</strong><br /><span className="muted">{info.agent_name || dep.agent_name || "Deployed agent"}</span></span>
                  <span><strong>Project</strong><br />{info.project_version ? `v${String(info.project_version).replace(/^v/i, "")}` : "—"}</span>
                  <span><strong>Runtime</strong><br />{info.runtime_version || dep.runtime_version || "—"}</span>
                </label>;
              }) : <p className="muted">No deployed agents found.</p>}
            </div>
            {bulkDeploymentResult ? <div className={`deployment-state ${bulkDeploymentResult.status === "succeeded" ? "completed" : bulkDeploymentResult.status === "failed" ? "failed" : "working"}`} role="status" style={{ marginTop: 16 }}>
              {bulkDeploymentResult.status === "succeeded" ? <CheckCircle2 size={18} /> : <AlertTriangle size={18} />}
              <div>
                <strong>Runtime deployment {bulkDeploymentResult.status}</strong>
                <span>{bulkDeploymentResult.succeeded ?? 0} succeeded, {bulkDeploymentResult.failed ?? 0} failed.</span>
                {bulkDeploymentResult.results?.length ? <div style={{ marginTop: 8, display: "grid", gap: 4 }}>
                  {bulkDeploymentResult.results.map((result: any) => <span key={`bulk-result-${result.project_id}`} style={{ fontSize: 12 }}>
                    {result.project_name || result.project_id}: {result.status}{result.runtime_version ? ` (${result.runtime_version})` : ""}{result.error ? ` — ${result.error}` : ""}
                  </span>)}
                </div> : null}
              </div>
            </div> : null}
          </section>;
        })() : activeTab === "metadata" ? <section className="panel" id="metadata-refresh">
          <div className="section-header">
            <h2>Metadata Refresh</h2>
            <div className="action-row">
              <button disabled={isMetadataLoading} onClick={loadMetadataAdmin}>{isMetadataLoading ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />} Refresh</button>
              <button disabled={isMetadataLoading} onClick={runMetadataRefreshNow}><RefreshCw size={16} /> Run now</button>
              <button onClick={() => setEditingSchedule({ name: "Nightly semantic metadata refresh", enabled: true, cron: "0 2 * * *", timezone: "UTC" })}><Plus size={16} /> New schedule</button>
            </div>
          </div>

          {editingSchedule ? <div className="role-editor" style={{ border: "1px solid var(--border)", borderRadius: 8, padding: 16, marginBottom: 16 }}>
            <h3>{editingSchedule.id ? "Edit Schedule" : "Create Schedule"}</h3>
            <div className="grid two">
              <label>Name<input value={editingSchedule.name} onChange={(e) => setEditingSchedule({ ...editingSchedule, name: e.target.value })} /></label>
              <label>Time<input type="time" value={parseScheduleCron(editingSchedule.cron).time} onChange={(e) => setEditingSchedule({ ...editingSchedule, cron: buildScheduleCron(e.target.value, parseScheduleCron(editingSchedule.cron).days) })} /></label>
              <label>Timezone<select value={editingSchedule.timezone} onChange={(e) => setEditingSchedule({ ...editingSchedule, timezone: e.target.value })}>{scheduleTimezoneOptions(editingSchedule.timezone).map((timezone) => <option key={timezone} value={timezone}>{timezone}</option>)}</select></label>
              <label className="toggle-label"><input type="checkbox" checked={editingSchedule.enabled} onChange={(e) => setEditingSchedule({ ...editingSchedule, enabled: e.target.checked })} /> Enabled</label>
            </div>
            <div className="schedule-days" aria-label="Refresh days">
              {weekDays.map((day) => {
                const schedule = parseScheduleCron(editingSchedule.cron);
                const selected = schedule.days.includes(day.value);
                const nextDays = selected ? schedule.days.filter((value) => value !== day.value) : [...schedule.days, day.value];
                return <button key={day.value} type="button" className={selected ? "selected" : ""} aria-pressed={selected} onClick={() => setEditingSchedule({ ...editingSchedule, cron: buildScheduleCron(schedule.time, nextDays) })}>{day.short}</button>;
              })}
            </div>
            <div className="action-row" style={{ marginTop: 12 }}>
              <button onClick={() => saveMetadataSchedule(editingSchedule)}><Save size={16} /> Save Schedule</button>
              <button onClick={() => setEditingSchedule(null)}>Cancel</button>
            </div>
          </div> : null}

          <div className="grid two">
            <div>
              <h3>Schedules</h3>
              {metadataSchedules.length ? metadataSchedules.map((schedule) => (
                <div className="project-card" key={schedule.id} style={{ marginBottom: 12 }}>
                  <div>
                    <h3>{schedule.name}</h3>
                    <p>{schedule.enabled ? "Enabled" : "Disabled"} · {describeSchedule(schedule)} · {schedule.timezone}</p>
                    <p style={{ fontSize: 12, opacity: 0.7 }}>Next: {schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString() : "not scheduled"} · Last: {schedule.last_status || "none"}</p>
                  </div>
                  <div className="project-actions">
                    <button onClick={() => setEditingSchedule({ ...schedule })}><FolderOpen size={16} /> Edit</button>
                    {schedule.id ? <button className="button-danger" onClick={() => deleteMetadataSchedule(schedule.id!)}><Trash2 size={16} /> Delete</button> : null}
                  </div>
                </div>
              )) : <div className="empty-state"><p>No metadata refresh schedules yet.</p></div>}
            </div>
            <div>
              <h3>Cached Semantic Metadata</h3>
              {semanticMetadata.length ? semanticMetadata.map((item) => (
                <div className="project-card" key={item.id} style={{ marginBottom: 12 }}>
                  <div>
                    <h3>{item.semantic_model_name || item.id}</h3>
                    <p>{item.workspace_name || "Workspace"} · {item.status || "unknown"}</p>
                    <p style={{ fontSize: 12, opacity: 0.7 }}>Refreshed: {item.refreshed_at ? new Date(item.refreshed_at).toLocaleString() : "never"}</p>
                    {item.last_error ? <pre>{JSON.stringify(item.last_error, null, 2)}</pre> : null}
                  </div>
                </div>
              )) : <div className="empty-state"><p>No cached semantic metadata yet.</p></div>}
            </div>
          </div>

          <h3 style={{ marginTop: 18 }}>Run History</h3>
          <div className="role-list">
            {metadataRuns.length ? metadataRuns.map((run) => (
              <div className="project-card" key={run.id} style={{ marginBottom: 12 }}>
                <div>
                  <h3>{metadataRunExecutionType(run.trigger)}</h3>
                  <p>{run.started_at ? new Date(run.started_at).toLocaleString() : "Not started"}</p>
                  <p style={{ fontSize: 12, opacity: 0.7 }}>{metadataRunMessage(run)}</p>
                </div>
              </div>
            )) : <div className="empty-state"><p>No metadata refresh runs yet.</p></div>}
          </div>
        </section> : activeTab === "roles" ? <section className="panel" id="roles">
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
            {editingRole.id && !isAdminRoleName(editingRole.name) && !isDeveloperRoleName(editingRole.name) ? (() => {
              const roleAgents = agentBindings.filter((binding) => binding.role_ids.includes(editingRole.id!));
              const unassignedAgents = agentBindings.filter((binding) => !binding.role_ids.includes(editingRole.id!));
              return (<>
                <h4 style={{ marginTop: 12 }}>Assigned Agents</h4>
                <div className="member-tiles assignment-tags">
                  {roleAgents.length ? roleAgents.map((binding) => (
                    <span key={binding.project_id} className="member-tile role">
                      <span className="member-tile-name">{binding.agent_name || binding.project_display_name}</span>
                      <button className="member-tile-remove" onClick={() => saveAgentBinding({ ...binding, role_ids: binding.role_ids.filter((rid) => rid !== editingRole.id) })}>&times;</button>
                    </span>
                  )) : <span className="model-pill empty" style={{ fontSize: 12 }}>No agents assigned</span>}
                </div>
                {unassignedAgents.length ? (
                  <select style={{ marginTop: 6, fontSize: 12, padding: "3px 6px" }} value="" onChange={(e) => {
                    const binding = agentBindings.find((b) => b.project_id === e.target.value);
                    if (binding) saveAgentBinding({ ...binding, role_ids: [...binding.role_ids, editingRole.id!] });
                  }}>
                    <option value="">Add agent...</option>
                    {unassignedAgents.map((binding) => <option key={binding.project_id} value={binding.project_id}>{binding.agent_name || binding.project_display_name}</option>)}
                  </select>
                ) : null}
              </>);
            })() : null}
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
                  <p style={{ fontSize: 12, opacity: 0.7 }}>{role.members.length} member{role.members.length === 1 ? "" : "s"}{!isAdminRoleName(role.name) && !isDeveloperRoleName(role.name) ? ` · ${agentBindings.filter((b) => b.role_ids.includes(role.id!)).length} agent${agentBindings.filter((b) => b.role_ids.includes(role.id!)).length === 1 ? "" : "s"}` : ""}</p>
                </div>
                <div className="project-actions">
                  <button onClick={() => setEditingRole({ ...role })}><FolderOpen size={16} /> Edit</button>
                  <button className="button-danger" onClick={() => role.id && deleteRole(role.id)}><Trash2 size={16} /> Delete</button>
                </div>
              </div>
            )) : <div className="empty-state"><p>No roles defined yet.</p><button onClick={() => setEditingRole(newRole())}><Plus size={16} /> Create your first role</button></div>}
          </div>
        </section> : <section className="dev-layout" id="dev">
          <div className="panel dev-console">
            <div className="section-header"><h2>Agent Dev UI</h2>{devProject ? <span className="badge">{devProject.deployment_mode}</span> : null}{devRunLocal ? <span className="badge">local</span> : <span className="badge">deployed</span>}</div>
            <label>Project to test<select value={devProjectId} onChange={(event) => { setDevProjectId(event.target.value); setDevChatHistory([]); setDevTrace([]); setDevDebug(null); setDevResponse(""); setConversationId(crypto.randomUUID()); }}><option value="">Select project</option>{visibleProjects.map((savedProject) => <option value={savedProject.id ?? ""} key={savedProject.id ?? savedProject.name}>{savedProject.name}</option>)}</select></label>
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
                  <div><strong>{step.step}</strong><span>{step.status}</span>{(step as any).timestamp ? <span className="trace-ts">{new Date((step as any).timestamp).toLocaleTimeString()}</span> : null}</div>
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
