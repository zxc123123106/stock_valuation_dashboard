import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { KeyboardSensor, PointerSensor, TouchSensor, useSensor, useSensors } from "@dnd-kit/core";
import { arrayMove, sortableKeyboardCoordinates } from "@dnd-kit/sortable";

import { reorderStocks } from "../api/stocks";
import { applyDisplayOrder } from "../utils/stocks";


export function useStockSorting({ stocks, setStocks, setError, setMessage, reorderingRef }) {
  const [reordering, setReordering] = useState(false);
  const [activeDragSymbol, setActiveDragSymbol] = useState("");
  const autoScrollFrameRef = useRef(0);
  const autoScrollSpeedRef = useRef(0);
  const stockCardRefs = useRef(new Map());
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 140, tolerance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );
  const orderedStocks = useMemo(() => [...stocks].sort(
    (left, right) => (left.display_order ?? 0) - (right.display_order ?? 0) || left.symbol.localeCompare(right.symbol),
  ), [stocks]);

  const registerStockCard = useCallback((symbol, node) => {
    if (node) stockCardRefs.current.set(symbol, node);
    else stockCardRefs.current.delete(symbol);
  }, []);
  const scrollStockToCenter = useCallback((symbol) => window.requestAnimationFrame(() => {
    stockCardRefs.current.get(symbol)?.scrollIntoView({ block: "center", behavior: "smooth" });
  }), []);

  const persistOrder = useCallback(async (nextStocks, previousStocks, focusSymbol = "") => {
    reorderingRef.current = true;
    setStocks(nextStocks);
    if (focusSymbol) scrollStockToCenter(focusSymbol);
    setReordering(true); setError(""); setMessage("");
    try {
      setStocks(await reorderStocks(nextStocks.map((stock) => stock.symbol)));
      if (focusSymbol) scrollStockToCenter(focusSymbol);
      setMessage("排序已更新");
    } catch (error) {
      setStocks(previousStocks);
      setError(error.message);
    } finally {
      reorderingRef.current = false;
      setReordering(false);
    }
  }, [reorderingRef, scrollStockToCenter, setError, setMessage, setStocks]);

  const moveStock = useCallback((symbol, direction) => {
    if (reordering) return;
    const oldIndex = orderedStocks.findIndex((stock) => stock.symbol === symbol);
    const newIndex = oldIndex + direction;
    if (oldIndex < 0 || newIndex < 0 || newIndex >= orderedStocks.length) return;
    persistOrder(applyDisplayOrder(arrayMove(orderedStocks, oldIndex, newIndex)), orderedStocks, symbol);
  }, [orderedStocks, persistOrder, reordering]);

  const handleDragEnd = useCallback((event) => {
    const { active, over } = event;
    setActiveDragSymbol("");
    if (!over || active.id === over.id || reordering) return;
    const oldIndex = orderedStocks.findIndex((stock) => stock.symbol === active.id);
    const newIndex = orderedStocks.findIndex((stock) => stock.symbol === over.id);
    if (oldIndex < 0 || newIndex < 0) return;
    persistOrder(applyDisplayOrder(arrayMove(orderedStocks, oldIndex, newIndex)), orderedStocks);
  }, [orderedStocks, persistOrder, reordering]);

  useEffect(() => {
    if (!activeDragSymbol) {
      autoScrollSpeedRef.current = 0;
      if (autoScrollFrameRef.current) window.cancelAnimationFrame(autoScrollFrameRef.current);
      autoScrollFrameRef.current = 0;
      return undefined;
    }
    const tick = () => {
      if (!autoScrollSpeedRef.current) { autoScrollFrameRef.current = 0; return; }
      window.scrollBy(0, autoScrollSpeedRef.current);
      autoScrollFrameRef.current = window.requestAnimationFrame(tick);
    };
    const onMove = (event) => {
      const y = event.touches?.[0]?.clientY ?? event.clientY;
      if (typeof y !== "number") return;
      const edge = 96;
      const distanceBottom = window.innerHeight - y;
      autoScrollSpeedRef.current = y < edge
        ? -Math.ceil(((edge - y) / edge) * 22)
        : distanceBottom < edge ? Math.ceil(((edge - distanceBottom) / edge) * 22) : 0;
      if (autoScrollSpeedRef.current && !autoScrollFrameRef.current) {
        autoScrollFrameRef.current = window.requestAnimationFrame(tick);
      }
    };
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("touchmove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("touchmove", onMove);
      if (autoScrollFrameRef.current) window.cancelAnimationFrame(autoScrollFrameRef.current);
      autoScrollFrameRef.current = 0;
      autoScrollSpeedRef.current = 0;
    };
  }, [activeDragSymbol]);

  return {
    sensors, orderedStocks, reordering, activeDragSymbol, setActiveDragSymbol,
    registerStockCard, moveStock, handleDragStart: (event) => setActiveDragSymbol(event.active.id), handleDragEnd,
  };
}
