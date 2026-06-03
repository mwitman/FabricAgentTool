import React from "react";
import type { AgentOption, Conversation } from "../App";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  open: boolean;
  onToggle: () => void;
  onCreate: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, name: string) => void;
  onToggleDark: () => void;
  onLogout: () => void;
  userName: string;
  agents: AgentOption[];
  selectedAgent: string;
  onSelectAgent: (agent: string) => void;
  loadingAgents?: boolean;
}

export default function Sidebar({
  conversations,
  activeId,
  open,
  onToggle,
  onCreate,
  onSelect,
  onDelete,
  onRename,
  onToggleDark,
  onLogout,
  userName,
  agents,
  selectedAgent,
  onSelectAgent,
  loadingAgents,
}: Props) {
  const [editingId, setEditingId] = React.useState<string | null>(null);
  const [editValue, setEditValue] = React.useState("");

  const startRename = (conv: Conversation) => {
    setEditingId(conv.id);
    setEditValue(conv.name);
  };

  const commitRename = () => {
    if (editingId && editValue.trim()) {
      onRename(editingId, editValue.trim());
    }
    setEditingId(null);
  };

  return (
    <>
      {/* Overlay for mobile */}
      {open && (
        <div
          className="fixed inset-0 bg-black/30 z-20 md:hidden"
          onClick={onToggle}
        />
      )}

      <aside
        className={`
          fixed md:relative z-30 h-full flex flex-col
          bg-gray-50 dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800
          transition-all duration-200 ease-in-out
          ${open ? "w-72" : "w-0 md:w-0"}
          overflow-hidden
        `}
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-3 py-3 border-b border-gray-200 dark:border-gray-800 shrink-0">
          <button
            onClick={onCreate}
            className="flex-1 flex items-center justify-center gap-2 px-3 py-2 rounded-lg
                       bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium transition-colors"
          >
            <PlusIcon />
            New Chat
          </button>
        </div>

        <section className="border-b border-gray-200 dark:border-gray-800 px-3 py-3 shrink-0">
          <div className="px-1 pb-2 text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
            Agents
          </div>
          <div className="space-y-1">
            {loadingAgents && (
              <div className="flex items-center gap-2 px-3 py-2 text-sm text-gray-400 dark:text-gray-500">
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
                </svg>
                Loading agents…
              </div>
            )}
            {!loadingAgents && agents.length === 0 && (
              <p className="px-3 py-2 text-sm text-gray-400 dark:text-gray-500">No agents available</p>
            )}
            {agents.map((agent) => (
              <button
                key={agent.key}
                onClick={() => onSelectAgent(agent.key)}
                className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm transition-colors
                  ${
                    selectedAgent === agent.key
                      ? "bg-blue-100 text-blue-800 dark:bg-blue-900/40 dark:text-blue-200"
                      : "text-gray-700 hover:bg-gray-200 dark:text-gray-300 dark:hover:bg-gray-800"
                  }`}
                title={agent.label}
              >
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded bg-white text-xs shadow-sm dark:bg-gray-800">
                  {agentIcon(agent.icon)}
                </span>
                <span className="min-w-0 flex-1 truncate">{agent.label}</span>
              </button>
            ))}
          </div>
        </section>

        {/* Conversation list */}
        <nav className="flex-1 overflow-y-auto py-2 chat-scroll">
          <div className="flex items-center justify-between px-4 pb-2">
            <div className="text-xs font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
              Conversations
            </div>
            <button
              onClick={onCreate}
              className="rounded p-1 text-gray-400 transition-colors hover:bg-gray-200 hover:text-gray-700 dark:text-gray-500 dark:hover:bg-gray-800 dark:hover:text-gray-200"
              title="New conversation"
            >
              <PlusIcon />
            </button>
          </div>
          {conversations.length === 0 && (
            <p className="px-4 py-8 text-center text-sm text-gray-400 dark:text-gray-500">
              No conversations yet
            </p>
          )}
          {conversations.map((conv) => (
            <div
              key={conv.id}
              className={`group flex items-center gap-1 mx-2 mb-0.5 px-3 py-2 rounded-lg cursor-pointer text-sm
                ${
                  conv.id === activeId
                    ? "bg-blue-100 dark:bg-blue-900/40 text-blue-800 dark:text-blue-200"
                    : "hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-700 dark:text-gray-300"
                }`}
              onClick={() => onSelect(conv.id)}
            >
              {editingId === conv.id ? (
                <input
                  autoFocus
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onBlur={commitRename}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") commitRename();
                    if (e.key === "Escape") setEditingId(null);
                  }}
                  className="flex-1 bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600
                             rounded px-1.5 py-0.5 text-sm outline-none"
                  onClick={(e) => e.stopPropagation()}
                />
              ) : (
                <span className="flex-1 truncate">{conv.name}</span>
              )}

              {/* Action buttons — visible on hover */}
              {editingId !== conv.id && (
                <span className="hidden group-hover:flex items-center gap-0.5 shrink-0">
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      startRename(conv);
                    }}
                    className="p-1 rounded hover:bg-gray-300 dark:hover:bg-gray-700"
                    title="Rename"
                  >
                    <PencilIcon />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(conv.id);
                    }}
                    className="p-1 rounded hover:bg-red-200 dark:hover:bg-red-900/40 text-red-600 dark:text-red-400"
                    title="Delete"
                  >
                    <TrashIcon />
                  </button>
                </span>
              )}
            </div>
          ))}
        </nav>

        {/* Footer */}
        <div className="border-t border-gray-200 dark:border-gray-800 px-3 py-2 shrink-0 space-y-1">
          {/* User info */}
          <div className="flex items-center gap-2 px-3 py-2 text-sm text-gray-700 dark:text-gray-300">
            <div className="w-6 h-6 rounded-full bg-blue-600 text-white flex items-center justify-center text-xs font-medium shrink-0">
              {userName.charAt(0).toUpperCase()}
            </div>
            <span className="truncate">{userName}</span>
          </div>
          <button
            onClick={onToggleDark}
            className="flex items-center gap-2 w-full px-3 py-2 rounded-lg text-sm
                       hover:bg-gray-200 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-400 transition-colors"
          >
            <MoonIcon />
            Toggle dark mode
          </button>
          <button
            onClick={onLogout}
            className="flex items-center gap-2 w-full px-3 py-2 rounded-lg text-sm
                       hover:bg-red-100 dark:hover:bg-red-900/30 text-red-600 dark:text-red-400 transition-colors"
          >
            <LogoutIcon />
            Sign out
          </button>
        </div>
      </aside>
    </>
  );
}

/* ---- Tiny inline SVG icons ---- */

function PlusIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 3.487a2.1 2.1 0 113.001 2.948L7.5 18.8l-4 1 1-4L16.862 3.487z" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6M1 7h22M8 7V4a1 1 0 011-1h6a1 1 0 011 1v3" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M20.354 15.354A9 9 0 018.646 3.646 9.005 9.005 0 0012 21a9.005 9.005 0 008.354-5.646z" />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
    </svg>
  );
}

function agentIcon(icon?: string) {
  if (icon === "target") return "◎";
  if (icon === "grid") return "▦";
  if (icon === "box") return "□";
  if (icon === "chart") return "↗";
  return "AI";
}
