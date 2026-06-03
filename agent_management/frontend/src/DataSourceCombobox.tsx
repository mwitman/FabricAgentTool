import { useEffect, useMemo, useRef, useState } from "react";
import { DataSourceRef, dataSourceTypeLabels, FabricItem, fabricMcpDataSource } from "./types";

interface Props {
  items: FabricItem[];
  value: DataSourceRef;
  onChange: (ref: DataSourceRef) => void;
  onOpen?: () => void;
  loading?: boolean;
}

export default function DataSourceCombobox({ items, value, onChange, onOpen, loading }: Props) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const displayValue = useMemo(() => {
    if (value.source_type === "fabric_mcp") return "Fabric MCP";
    if (value.item_name) return `${value.item_name} (${dataSourceTypeLabels[value.source_type]}) — ${value.workspace_name}`;
    return "";
  }, [value]);

  useEffect(() => {
    if (!open) setQuery("");
  }, [open]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    if (!q) return items;
    const terms = q.replace(/[_-]/g, " ").split(/\s+/).filter(Boolean);
    return items.filter((item) => {
      const typeLabel = dataSourceTypeLabels[item.source_type] || item.fabric_type || "";
      const aliases = item.source_type === "data_agent" ? "fabric data agent data agent" : "";
      const haystack = [item.item_name, item.workspace_name, typeLabel, item.fabric_type, item.source_type.replace(/_/g, " "), aliases].join(" ").toLowerCase();
      return terms.every((term) => haystack.includes(term));
    });
  }, [items, query]);

  const select = (item: FabricItem) => {
    if (item.item_id === "fabric_mcp") {
      onChange(fabricMcpDataSource);
    } else {
      onChange({
        source_type: item.source_type,
        workspace_id: item.workspace_id,
        workspace_name: item.workspace_name,
        item_id: item.item_id,
        item_name: item.item_name,
      });
    }
    setOpen(false);
    inputRef.current?.blur();
  };

  return (
    <div className="ds-combobox" ref={wrapperRef}>
      <input
        ref={inputRef}
        className="ds-combobox-input"
        placeholder="Search data source…"
        value={open ? query : displayValue}
        onFocus={() => { setOpen(true); onOpen?.(); }}
        onChange={(e) => { setQuery(e.target.value); if (!open) { setOpen(true); onOpen?.(); } }}
      />
      {open && (
        <ul className="ds-combobox-list" onMouseDown={(e) => e.preventDefault()}>
          <li className={value.source_type === "fabric_mcp" ? "ds-combobox-item selected" : "ds-combobox-item"} onMouseUp={() => select({ source_type: "fabric_mcp", workspace_id: "", workspace_name: "Fabric MCP Server", item_id: "fabric_mcp", item_name: "Fabric MCP", fabric_type: "fabric_mcp" })}>
            <strong>Fabric MCP</strong>
            <span className="ds-combobox-meta">Fabric MCP Server</span>
          </li>
          {filtered.map((item) => (
            <li key={`${item.workspace_id}-${item.item_id}`} className={value.item_id === item.item_id ? "ds-combobox-item selected" : "ds-combobox-item"} onMouseUp={() => select(item)}>
              <strong>{item.item_name}</strong>
              <span className="ds-combobox-meta">{dataSourceTypeLabels[item.source_type]} — {item.workspace_name}</span>
            </li>
          ))}
          {loading && items.length === 0 && <li className="ds-combobox-empty">Loading Fabric items…</li>}
          {!loading && filtered.length === 0 && <li className="ds-combobox-empty">No items match</li>}
        </ul>
      )}
    </div>
  );
}
