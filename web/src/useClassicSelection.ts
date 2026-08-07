import { useState } from "react";

export interface SelectionModifiers {
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
}

export function useClassicSelection() {
  const [focusedKey, setFocusedKey] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(() => new Set());
  const [anchorKey, setAnchorKey] = useState("");

  function select(
    key: string,
    visibleKeys: string[],
    modifiers: SelectionModifiers,
  ) {
    const additive = modifiers.ctrlKey || modifiers.metaKey;
    if (modifiers.shiftKey && anchorKey) {
      const anchorIndex = visibleKeys.indexOf(anchorKey);
      const targetIndex = visibleKeys.indexOf(key);
      if (anchorIndex >= 0 && targetIndex >= 0) {
        const start = Math.min(anchorIndex, targetIndex);
        const end = Math.max(anchorIndex, targetIndex);
        const range = visibleKeys.slice(start, end + 1);
        setSelectedKeys((current) => (
          new Set(additive ? [...current, ...range] : range)
        ));
        setFocusedKey(key);
        return;
      }
    }
    if (additive) {
      setSelectedKeys((current) => {
        const next = new Set(current);
        if (next.has(key)) next.delete(key);
        else next.add(key);
        return next;
      });
    } else {
      setSelectedKeys(new Set([key]));
    }
    setFocusedKey(key);
    setAnchorKey(key);
  }

  function reset(key = "") {
    setFocusedKey(key);
    setSelectedKeys(new Set(key ? [key] : []));
    setAnchorKey(key);
  }

  return { focusedKey, selectedKeys, select, reset };
}
