import { useCallback, useState } from "react";


export function useAIAnalysis() {
  const [pendingBySymbol, setPendingBySymbol] = useState({});
  const setPending = useCallback((symbol, pending) => {
    setPendingBySymbol((current) => {
      if (pending) return { ...current, [symbol]: true };
      if (!current[symbol]) return current;
      const next = { ...current };
      delete next[symbol];
      return next;
    });
  }, []);
  return { pendingBySymbol, setPending };
}
