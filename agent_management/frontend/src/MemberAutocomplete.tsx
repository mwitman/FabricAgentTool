import { useState, useRef, useEffect, useCallback } from "react";

interface DirectoryResult {
  object_id: string;
  display_name: string;
  email: string;
  member_type: "user" | "group";
}

interface Props {
  value: string;
  onSelect: (result: DirectoryResult) => void;
  onChange: (value: string) => void;
  placeholder?: string;
  clearOnSelect?: boolean;
}

export default function MemberAutocomplete({ value, onSelect, onChange, placeholder, clearOnSelect }: Props) {
  const [localValue, setLocalValue] = useState(value);
  const [results, setResults] = useState<DirectoryResult[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef(0);

  const search = useCallback(async (q: string) => {
    const searchId = searchRef.current + 1;
    searchRef.current = searchId;
    if (q.length < 2) {
      setResults([]);
      setOpen(false);
      setLoading(false);
      setHasSearched(false);
      return;
    }
    setOpen(true);
    setLoading(true);
    setHasSearched(false);
    try {
      const resp = await fetch(`/api/directory/search?q=${encodeURIComponent(q)}`);
      if (searchRef.current !== searchId) return;
      if (resp.ok) {
        const data = await resp.json();
        setResults(data.results ?? []);
      } else {
        setResults([]);
      }
    } catch {
      if (searchRef.current === searchId) setResults([]);
    } finally {
      if (searchRef.current === searchId) {
        setLoading(false);
        setHasSearched(true);
      }
    }
  }, []);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const v = e.target.value;
    setLocalValue(v);
    onChange(v);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    setOpen(v.length >= 2);
    setLoading(v.length >= 2);
    setHasSearched(false);
    if (v.length < 2) setResults([]);
    debounceRef.current = setTimeout(() => search(v), 300);
  };

  const handleSelect = (r: DirectoryResult) => {
    onSelect(r);
    setOpen(false);
    setResults([]);
    setHasSearched(false);
    if (clearOnSelect) {
      setLocalValue("");
    } else {
      setLocalValue(r.display_name);
    }
  };

  // Close dropdown on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div ref={containerRef} style={{ position: "relative" }}>
      <input
        placeholder={placeholder ?? "Search by name or email…"}
        value={localValue}
        onChange={handleChange}
        onFocus={() => { if (results.length > 0) setOpen(true); }}
        autoComplete="off"
      />
      {open && (
        <ul className="directory-dropdown">
          {loading ? <li className="directory-item loading"><span className="loading-dots"><span></span><span></span><span></span></span> Loading</li> : results.length ? results.map((r) => (
            <li key={r.object_id} className="directory-item" onClick={() => handleSelect(r)}>
              <span className="directory-name">{r.display_name}</span>
              <span className="directory-detail">
                {r.email ? r.email : r.member_type === "group" ? "Group" : ""}
              </span>
            </li>
          )) : hasSearched ? <li className="directory-item empty">No member found</li> : null}
        </ul>
      )}
    </div>
  );
}
