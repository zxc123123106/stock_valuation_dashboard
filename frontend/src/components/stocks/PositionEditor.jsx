import React, { useEffect, useState } from "react";


export function PositionEditor({ stock, disabled, onSavePosition, onClearPosition, compact = false }) {
  const [draftBuyPrice, setDraftBuyPrice] = useState(stock.position?.buy_price?.toString() || "");

  useEffect(() => {
    setDraftBuyPrice(stock.position?.buy_price?.toString() || "");
  }, [stock.position?.buy_price]);

  function commitBuyPrice() {
    const trimmed = draftBuyPrice.trim();
    const parsed = Number(trimmed);
    if (!trimmed || Number.isNaN(parsed) || parsed === stock.position?.buy_price) {
      return;
    }
    onSavePosition(trimmed);
  }

  return (
    <div className={`position-row${compact ? " compact" : ""}`}>
      <label className="buy-price-field">
        <span>成交均價</span>
        <input
          inputMode="decimal"
          value={draftBuyPrice}
          onChange={(event) => setDraftBuyPrice(event.target.value)}
          onBlur={commitBuyPrice}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.currentTarget.blur();
            }
          }}
          disabled={disabled}
        />
      </label>
      <button className="text-button sell-button" type="button" onClick={onClearPosition} disabled={disabled || !stock.position}>
        賣出
      </button>
    </div>
  );
}
