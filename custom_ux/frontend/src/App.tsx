import React, { useState, useCallback, useEffect, useMemo } from "react";
import {
  useIsAuthenticated,
  useMsal,
  AuthenticatedTemplate,
  UnauthenticatedTemplate,
} from "@azure/msal-react";
import { InteractionRequiredAuthError } from "@azure/msal-browser";
import Sidebar from "./components/Sidebar";
import ChatPanel from "./components/ChatPanel";
import type { Message } from "./components/ChatMessage";
import { loginRequest, fabricTokenRequest, powerBiTokenRequest } from "./authConfig";

export interface Conversation {
  id: string;
  name: string;
  createdAt: number;
  updatedAt?: number;
  agent: string;
  messages: Message[];
}

export interface AgentOption {
  key: string;
  label: string;
  icon?: string;
}

export default function App() {
  const { instance, accounts } = useMsal();
  const isAuthenticated = useIsAuthenticated();

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [agentOptions, setAgentOptions] = useState<AgentOption[]>([]);
  const [selectedAgent, setSelectedAgent] = useState("");
  const [loadingAgents, setLoadingAgents] = useState(true);
  const [loadingConversations, setLoadingConversations] = useState(true);
  const conversationsLoadedRef = React.useRef(false);
  const skipNextConversationSaveRef = React.useRef(false);

  const account = accounts[0] ?? null;
  const selectedAgentOption = useMemo<AgentOption>(
    () => agentOptions.find((agent) => agent.key === selectedAgent) ?? agentOptions[0] ?? { key: selectedAgent, label: selectedAgent || "Agent" },
    [agentOptions, selectedAgent]
  );
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeId) ?? null,
    [conversations, activeId]
  );

  const getGraphToken = useCallback(async (): Promise<string> => {
    if (!account) throw new Error("No active account");
    const graphRequest = { scopes: ["User.Read"], account };
    try {
      const tokenResponse = await instance.acquireTokenSilent(graphRequest);
      return tokenResponse.accessToken;
    } catch {
      const tokenResponse = await instance.acquireTokenPopup(graphRequest);
      return tokenResponse.accessToken;
    }
  }, [account, instance]);

  // Fetch agents the authenticated user can access (role-filtered from Cosmos)
  useEffect(() => {
    if (!account) return;
    let cancelled = false;
    setLoadingAgents(true);

    (async () => {
      try {
        // Acquire a Graph-scoped token to pass to the backend
        const graphToken = await getGraphToken();

        const resp = await fetch("/api/my-agents", {
          headers: { Authorization: `Bearer ${graphToken}` },
        });
        if (!resp.ok) throw new Error(`Failed: ${resp.status}`);
        const payload = await resp.json();
        if (cancelled) return;

        const loaded: AgentOption[] = (payload.agents ?? []).map((a: any) => ({
          key: a.key,
          label: a.label,
          icon: a.icon,
        }));
        if (loaded.length) {
          setAgentOptions(loaded);
          setSelectedAgent((current) =>
            loaded.some((a) => a.key === current) ? current : loaded[0].key
          );
        }
      } catch (err) {
        console.error("Failed to load agents:", err);
        // Fallback: try unauthenticated /api/agents (returns all)
        try {
          if (cancelled) return;
          const resp = await fetch("/api/agents");
          if (!resp.ok) return;
          const payload = await resp.json();
          if (cancelled) return;
          const loaded: AgentOption[] = (payload.agents ?? []).map((a: any) => ({
            key: a.key,
            label: a.label,
            icon: a.icon,
          }));
          if (loaded.length) {
            setAgentOptions(loaded);
            setSelectedAgent((current) =>
              loaded.some((a) => a.key === current) ? current : loaded[0].key
            );
          }
        } catch {}
      } finally {
        if (!cancelled) setLoadingAgents(false);
      }
    })();

    return () => { cancelled = true; };
  }, [account, getGraphToken]);

  useEffect(() => {
    if (!account) {
      conversationsLoadedRef.current = false;
      setConversations([]);
      setActiveId(null);
      setLoadingConversations(false);
      return;
    }

    let cancelled = false;
    conversationsLoadedRef.current = false;
    setLoadingConversations(true);

    (async () => {
      try {
        const graphToken = await getGraphToken();
        const resp = await fetch("/api/conversations", {
          headers: { Authorization: `Bearer ${graphToken}` },
        });
        if (!resp.ok) throw new Error(`Failed: ${resp.status}`);
        const payload = await resp.json();
        if (cancelled) return;
        const loaded: Conversation[] = (payload.conversations ?? []).map((conversation: any) => ({
          id: String(conversation.id),
          name: conversation.name || "New Chat",
          createdAt: Number(conversation.createdAt) || Date.now(),
          updatedAt: Number(conversation.updatedAt) || Number(conversation.createdAt) || Date.now(),
          agent: conversation.agent || "",
          messages: Array.isArray(conversation.messages) ? conversation.messages : [],
        }));
        skipNextConversationSaveRef.current = true;
        setConversations(loaded);
        setActiveId((current) => (current && loaded.some((conversation) => conversation.id === current) ? current : null));
      } catch (err) {
        console.error("Failed to load conversations:", err);
        if (!cancelled) {
          skipNextConversationSaveRef.current = true;
          setConversations([]);
          setActiveId(null);
        }
      } finally {
        if (!cancelled) {
          conversationsLoadedRef.current = true;
          setLoadingConversations(false);
        }
      }
    })();

    return () => { cancelled = true; };
  }, [account, getGraphToken]);

  useEffect(() => {
    if (!account || !conversationsLoadedRef.current) return;
    if (skipNextConversationSaveRef.current) {
      skipNextConversationSaveRef.current = false;
      return;
    }

    const timeout = window.setTimeout(async () => {
      try {
        const graphToken = await getGraphToken();
        await fetch("/api/conversations", {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${graphToken}`,
          },
          body: JSON.stringify({ conversations }),
        });
      } catch (err) {
        console.error("Failed to save conversations:", err);
      }
    }, 500);

    return () => window.clearTimeout(timeout);
  }, [account, conversations, getGraphToken]);

  /** Acquire user-scoped access tokens for Fabric and semantic model queries. */
  const getToken = useCallback(async (): Promise<{ fabricToken: string; powerBiToken: string }> => {
    if (!account) throw new Error("No active account");
    try {
      const fabricResponse = await instance.acquireTokenSilent({
        ...fabricTokenRequest,
        account,
      });
      const powerBiResponse = await instance.acquireTokenSilent({
        ...powerBiTokenRequest,
        account,
      });
      return { fabricToken: fabricResponse.accessToken, powerBiToken: powerBiResponse.accessToken };
    } catch (err) {
      if (err instanceof InteractionRequiredAuthError) {
        const fabricResponse = await instance.acquireTokenPopup(fabricTokenRequest);
        const powerBiResponse = await instance.acquireTokenPopup(powerBiTokenRequest);
        return { fabricToken: fabricResponse.accessToken, powerBiToken: powerBiResponse.accessToken };
      }
      throw err;
    }
  }, [instance, account]);

  const handleLogin = useCallback(async () => {
    try {
      await instance.loginPopup(loginRequest);
    } catch (err) {
      console.error("Login failed:", err);
    }
  }, [instance]);

  const handleLogout = useCallback(async () => {
    try {
      await instance.logoutPopup({ account });
    } catch (err) {
      console.error("Logout failed:", err);
    }
  }, [instance, account]);

  const createConversation = useCallback(() => {
    const conv: Conversation = {
      id: crypto.randomUUID(),
      name: "New Chat",
      createdAt: Date.now(),
      updatedAt: Date.now(),
      agent: selectedAgentOption?.key ?? selectedAgent ?? "",
      messages: [],
    };
    setConversations((prev) => [conv, ...prev]);
    setActiveId(conv.id);
  }, [selectedAgent, selectedAgentOption]);

  const openConversation = useCallback((id: string) => {
    const conversation = conversations.find((candidate) => candidate.id === id);
    if (conversation?.agent) {
      setSelectedAgent(conversation.agent);
    }
    setActiveId(id);
  }, [conversations]);

  const selectAgent = useCallback(
    (agent: string) => {
      if (agent === selectedAgent) return;
      setSelectedAgent(agent);
      const conv: Conversation = {
        id: crypto.randomUUID(),
        name: "New Chat",
        createdAt: Date.now(),
        updatedAt: Date.now(),
        agent,
        messages: [],
      };
      setConversations((prev) => [conv, ...prev]);
      setActiveId(conv.id);
    },
    [selectedAgent]
  );

  const deleteConversation = useCallback(
    (id: string) => {
      setConversations((prev) => prev.filter((c) => c.id !== id));
      if (activeId === id) {
        setActiveId(null);
      }
      (async () => {
        try {
          const graphToken = await getGraphToken();
          await fetch(`/api/conversations/${id}`, {
            method: "DELETE",
            headers: { Authorization: `Bearer ${graphToken}` },
          });
        } catch {}
        fetch(`/api/chat/${id}`, { method: "DELETE" }).catch(() => {});
      })();
    },
    [activeId, getGraphToken]
  );

  const renameConversation = useCallback((id: string, name: string) => {
    setConversations((prev) =>
      prev.map((c) => (c.id === id ? { ...c, name, updatedAt: Date.now() } : c))
    );
  }, []);

  const updateConversationMessages = useCallback((id: string, messages: Message[] | ((previous: Message[]) => Message[])) => {
    setConversations((prev) =>
      prev.map((conversation) => {
        if (conversation.id !== id) return conversation;
        const nextMessages = typeof messages === "function" ? messages(conversation.messages) : messages;
        return { ...conversation, messages: nextMessages, updatedAt: Date.now() };
      })
    );
  }, []);

  const toggleDarkMode = useCallback(() => {
    const html = document.documentElement;
    html.classList.toggle("dark");
    localStorage.setItem(
      "theme",
      html.classList.contains("dark") ? "dark" : "light"
    );
  }, []);

  return (
    <>
      <AuthenticatedTemplate>
        <div className="flex h-screen overflow-hidden">
          <Sidebar
            conversations={conversations}
            activeId={activeId}
            open={sidebarOpen}
            onToggle={() => setSidebarOpen((o) => !o)}
            onCreate={createConversation}
            onSelect={openConversation}
            onDelete={deleteConversation}
            onRename={renameConversation}
            onToggleDark={toggleDarkMode}
            onLogout={handleLogout}
            userName={account?.name ?? account?.username ?? "User"}
            agents={agentOptions}
            selectedAgent={selectedAgent}
            onSelectAgent={selectAgent}
            loadingAgents={loadingAgents}
          />

          <main className="flex-1 flex flex-col min-w-0">
            {activeId && activeConversation ? (
              <ChatPanel
                key={activeId}
                conversationId={activeId}
                onRename={(name) => renameConversation(activeId, name)}
                messages={activeConversation.messages}
                setMessages={(messages) => updateConversationMessages(activeId, messages)}
                onToggleSidebar={() => setSidebarOpen((o) => !o)}
                sidebarOpen={sidebarOpen}
                getToken={getToken}
                userId={account?.localAccountId ?? account?.homeAccountId ?? account?.username ?? "default_user"}
                selectedAgent={selectedAgentOption.key}
                selectedAgentLabel={selectedAgentOption.label}
              />
            ) : (
              <EmptyState onCreate={createConversation} loading={loadingConversations} />
            )}
          </main>
        </div>
      </AuthenticatedTemplate>

      <UnauthenticatedTemplate>
        <LoginScreen onLogin={handleLogin} />
      </UnauthenticatedTemplate>
    </>
  );
}

function EmptyState({ onCreate, loading }: { onCreate: () => void; loading?: boolean }) {
  return (
    <div className="flex-1 flex items-center justify-center">
      <div className="text-center space-y-4">
        <div className="text-5xl">💬</div>
        <h1 className="text-2xl font-semibold">Fabric Agents</h1>
        <p className="text-gray-500 dark:text-gray-400 max-w-md">
          {loading ? "Loading your conversations..." : "Ask questions about data in Fabric."}
        </p>
        <button
          onClick={onCreate}
          className="px-6 py-2.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors font-medium"
        >
          Start a new chat
        </button>
      </div>
    </div>
  );
}

function LoginScreen({ onLogin }: { onLogin: () => void }) {
  return (
    <div className="flex h-screen items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100 dark:from-gray-950 dark:to-gray-900">
      <div className="text-center space-y-6 p-8 bg-white dark:bg-gray-900 rounded-2xl shadow-lg max-w-sm w-full mx-4">
        <div className="text-5xl">🔐</div>
        <h1 className="text-2xl font-semibold">Fabric GraphQL Agents</h1>
        <p className="text-gray-500 dark:text-gray-400 text-sm">
          Sign in with your organizational account to access
          Fabric data through natural language.
        </p>
        <button
          onClick={onLogin}
          className="w-full px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-lg
                     transition-colors font-medium flex items-center justify-center gap-2"
        >
          <svg className="w-5 h-5" viewBox="0 0 21 21" fill="none">
            <rect x="1" y="1" width="9" height="9" fill="#f25022" />
            <rect x="11" y="1" width="9" height="9" fill="#7fba00" />
            <rect x="1" y="11" width="9" height="9" fill="#00a4ef" />
            <rect x="11" y="11" width="9" height="9" fill="#ffb900" />
          </svg>
          Sign in with Microsoft
        </button>
      </div>
    </div>
  );
}
