import React, { useState, useRef, useEffect } from "react";

interface Props {
  onSend: (text: string) => void;
  onStop: () => void;
  streaming: boolean;
}

export default function ChatInput({ onSend, onStop, streaming }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea
  useEffect(() => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 200) + "px";
    }
  }, [value]);

  const handleSubmit = () => {
    if (!value.trim() || streaming) return;
    onSend(value.trim());
    setValue("");
    // Reset height
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  return (
    <div className="border-t border-gray-200 dark:border-gray-800 px-4 py-3 shrink-0">
      <div className="max-w-3xl mx-auto">
        <div className="relative flex items-end gap-2 bg-gray-100 dark:bg-gray-800 rounded-2xl px-4 py-2">
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask me..."
            rows={1}
            className="flex-1 bg-transparent outline-none resize-none text-sm py-1.5
                       placeholder:text-gray-400 dark:placeholder:text-gray-500
                       max-h-[200px]"
          />

          {streaming ? (
            <button
              onClick={onStop}
              className="shrink-0 p-2 rounded-xl bg-red-500 hover:bg-red-600 text-white transition-colors"
              title="Stop generating"
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          ) : (
            <button
              onClick={handleSubmit}
              disabled={!value.trim()}
              className="shrink-0 p-2 rounded-xl bg-blue-600 hover:bg-blue-700 disabled:bg-gray-300
                         dark:disabled:bg-gray-600 text-white transition-colors disabled:cursor-not-allowed"
              title="Send message"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 12h14M12 5l7 7-7 7" />
              </svg>
            </button>
          )}
        </div>

        <p className="text-center text-xs text-gray-400 dark:text-gray-500 mt-2">
          Powered by Microsoft Agent Framework
        </p>
      </div>
    </div>
  );
}
