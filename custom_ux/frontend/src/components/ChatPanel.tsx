import React, { useState, useRef, useEffect, useCallback } from "react";
import ChatMessage, { Message } from "./ChatMessage";
import ChatInput from "./ChatInput";

export type AgentTarget = string;

interface Props {
  conversationId: string;
  onRename: (name: string) => void;
  messages: Message[];
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>;
  onToggleSidebar: () => void;
  sidebarOpen: boolean;
  getToken: () => Promise<{ fabricToken: string; powerBiToken: string }>;
  userId: string;
  selectedAgent: AgentTarget;
  selectedAgentLabel: string;
}

interface ToolCallState {
  name: string;
  done: boolean;
}

export default function ChatPanel({
  conversationId,
  onRename,
  messages,
  setMessages,
  onToggleSidebar,
  sidebarOpen,
  getToken,
  userId,
  selectedAgent,
  selectedAgentLabel,
}: Props) {
  const [streaming, setStreaming] = useState(false);
  const [toolCalls, setToolCalls] = useState<Map<string, ToolCallState>>(
    new Map()
  );
  const scrollRef = useRef<HTMLDivElement>(null);
  const hasNamedRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    hasNamedRef.current = messages.length > 0;
  }, [conversationId, messages.length]);

  // Auto-scroll on new content
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, toolCalls]);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!text.trim() || streaming) return;

      // Auto-name conversation from first message
      if (!hasNamedRef.current) {
        hasNamedRef.current = true;
        const short = text.length > 40 ? text.slice(0, 40) + "..." : text;
        onRename(short);
      }

      const userMsg: Message = { role: "user", content: text };
      setMessages((prev) => [...prev, userMsg]);
      setStreaming(true);
      setToolCalls(new Map());

      const assistantIndex = messages.length + 1; // position of the new assistant msg
      setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        // Acquire a Fabric-scoped token before calling the API
        const { fabricToken, powerBiToken } = await getToken();

        const resp = await fetch("/api/chat", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${fabricToken}`,
          },
          body: JSON.stringify({
            message: text,
            conversation_id: conversationId,
            user_id: userId,
            agent: selectedAgent,
            powerbi_token: powerBiToken,
          }),
          signal: controller.signal,
        });

        if (!resp.ok || !resp.body) {
          throw new Error(`Server error: ${resp.status}`);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          // Parse SSE lines
          const lines = buffer.split("\n\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.replace(/^data: /, "").trim();
            if (!trimmed) continue;

            let event: any;
            try {
              event = JSON.parse(trimmed);
            } catch {
              continue;
            }

            if (event.type === "text") {
              setMessages((prev) => {
                const updated = [...prev];
                const msg = updated[assistantIndex];
                if (msg) {
                  updated[assistantIndex] = {
                    ...msg,
                    content: msg.content + event.content,
                  };
                }
                return updated;
              });
            } else if (event.type === "tool_call") {
              setToolCalls((prev) => {
                const next = new Map(prev);
                next.set(event.call_id, { name: event.name, done: false });
                return next;
              });
            } else if (event.type === "tool_result") {
              setToolCalls((prev) => {
                const next = new Map(prev);
                const existing = next.get(event.call_id);
                if (existing) next.set(event.call_id, { ...existing, done: true });
                return next;
              });
            } else if (event.type === "error") {
              setMessages((prev) => {
                const updated = [...prev];
                updated[assistantIndex] = {
                  role: "assistant",
                  content: `**Error:** ${event.content}`,
                };
                return updated;
              });
            }
          }
        }
      } catch (err: any) {
        if (err.name !== "AbortError") {
          setMessages((prev) => {
            const updated = [...prev];
            updated[assistantIndex] = {
              role: "assistant",
              content: `**Error:** ${err.message}`,
            };
            return updated;
          });
        }
      } finally {
        setStreaming(false);
        abortRef.current = null;
      }
    },
    [conversationId, messages.length, onRename, streaming, getToken, selectedAgent, userId]
  );

  const handleStop = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
  }, []);

  // Active tool calls (not yet done)
  const activeTools = [...toolCalls.values()].filter((t) => !t.done);

  return (
    <div className="flex-1 flex flex-col h-full">
      {/* Top bar */}
      <header className="flex items-center gap-3 px-4 py-3 border-b border-gray-200 dark:border-gray-800 shrink-0">
        {!sidebarOpen && (
          <button
            onClick={onToggleSidebar}
            className="p-1.5 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-800 transition-colors"
            title="Open sidebar"
          >
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
            </svg>
          </button>
        )}
        <div className="min-w-0">
          <h1 className="truncate text-xl font-semibold text-gray-900 dark:text-gray-100">
            {selectedAgentLabel}
          </h1>
        </div>
      </header>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto chat-scroll">
        <div className="max-w-3xl mx-auto px-4 py-6 space-y-6">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center py-20 text-center space-y-3">
              <div className="text-4xl">🔍</div>
              <p className="text-gray-400 dark:text-gray-500 text-sm max-w-sm">
                How can I help?
              </p>
            </div>
          )}

          {messages.map((msg, i) => (
            <ChatMessage key={i} message={msg} />
          ))}

          {/* Tool call indicator */}
          {streaming && activeTools.length > 0 && (
            <div className="flex items-start gap-3">
              <div className="w-8 h-8 rounded-full bg-purple-100 dark:bg-purple-900/40 flex items-center justify-center text-purple-600 dark:text-purple-400 shrink-0 text-sm">
                ⚙
              </div>
              <div className="space-y-1 pt-1">
                {activeTools.map((t, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-2 text-sm text-purple-600 dark:text-purple-400"
                  >
                    <Spinner />
                    <span>
                      Calling <strong>{formatToolName(t.name)}</strong>...
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Typing indicator when streaming but no active tool calls */}
          {streaming && activeTools.length === 0 && messages[messages.length - 1]?.role === "assistant" && (
            <div className="flex items-center gap-2 text-gray-500 dark:text-gray-400 text-sm pl-11">
              <Spinner />
              <span>Asking <strong>{selectedAgentLabel}</strong>...</span>
            </div>
          )}
        </div>
      </div>

      {/* Input */}
      <ChatInput onSend={sendMessage} onStop={handleStop} streaming={streaming} />
    </div>
  );
}

function Spinner() {
  return (
    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  );
}

function formatToolName(name: string): string {
  return name
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
