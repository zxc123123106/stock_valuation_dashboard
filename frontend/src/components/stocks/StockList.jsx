import React, { useMemo } from "react";
import { DndContext, DragOverlay, closestCenter } from "@dnd-kit/core";
import { SortableContext, verticalListSortingStrategy } from "@dnd-kit/sortable";
import { Database } from "lucide-react";

import { isPendingRefresh, isVisibleRefreshState } from "../../utils/stocks";
import { SortableStockCard, StockCard } from "./StockCard";


export function StockList({
  loading, stocks, refreshStatus, now, sensors, reordering, activeDragSymbol,
  setActiveDragSymbol, registerStockCard, moveStock, handleDragStart, handleDragEnd,
  queueRefreshSymbol, deleteStock, savePosition, clearPosition,
}) {
  const refreshStateBySymbol = useMemo(
    () => new Map((refreshStatus?.symbols || []).map((state) => [state.symbol, state])),
    [refreshStatus],
  );
  const activeStock = stocks.find((stock) => stock.symbol === activeDragSymbol);

  return (
    <section className="stock-grid" aria-label="stocks">
      {loading ? <Empty label="載入中" /> : stocks.length ? (
        <DndContext
          sensors={sensors}
          collisionDetection={closestCenter}
          autoScroll
          onDragStart={handleDragStart}
          onDragCancel={() => setActiveDragSymbol("")}
          onDragEnd={handleDragEnd}
        >
          <SortableContext items={stocks.map((stock) => stock.symbol)} strategy={verticalListSortingStrategy}>
            {stocks.map((stock, index) => {
              const state = refreshStateBySymbol.get(stock.symbol);
              return (
                <SortableStockCard
                  key={stock.symbol}
                  stock={stock}
                  index={index}
                  total={stocks.length}
                  refreshState={state}
                  showRefreshState={isVisibleRefreshState(state, now)}
                  sortingDisabled={reordering || isPendingRefresh(state)}
                  actionDisabled={reordering}
                  onRegisterRef={registerStockCard}
                  onMoveUp={() => moveStock(stock.symbol, -1)}
                  onMoveDown={() => moveStock(stock.symbol, 1)}
                  onRefresh={() => queueRefreshSymbol(stock.symbol)}
                  onDelete={() => deleteStock(stock.symbol)}
                  onSavePosition={(buyPrice) => savePosition(stock.symbol, buyPrice)}
                  onClearPosition={() => clearPosition(stock.symbol)}
                />
              );
            })}
          </SortableContext>
          <DragOverlay>
            {activeStock ? (
              <StockCard
                stock={activeStock}
                refreshState={refreshStateBySymbol.get(activeStock.symbol)}
                showRefreshState={false}
                overlay
                sortingDisabled
                actionDisabled
                canMoveUp={false}
                canMoveDown={false}
                onMoveUp={() => {}}
                onMoveDown={() => {}}
                onRefresh={() => {}}
                onDelete={() => {}}
                onSavePosition={() => {}}
                onClearPosition={() => {}}
              />
            ) : null}
          </DragOverlay>
        </DndContext>
      ) : <Empty label="無資料" />}
    </section>
  );
}


function Empty({ label }) {
  return <div className="empty"><Database size={18} />{label}</div>;
}
