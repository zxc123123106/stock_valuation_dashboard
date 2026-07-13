import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { AlertCircle, Loader2, X } from "lucide-react";

import { useDataQuality } from "../../hooks/useDataQuality";
import { DataQualityBadge } from "../shared/DataQualityBadge";
import { formatDate, formatTradingDate } from "../../utils/formatters";


function qualityDataLabel(item) {
  if (item?.data_period) {
    return item.data_period;
  }
  return item?.data_date ? formatTradingDate(item.data_date) : "尚無資料";
}

export function DataQualityPopover({ stock, open, anchorRef, onClose }) {
  const panelRef = useRef(null);
  const { quality, loading, error } = useDataQuality(stock.symbol, open);
  const [panelStyle, setPanelStyle] = useState({});

  useEffect(() => {
    if (!open) return undefined;
    function positionPanel() {
      const anchor = anchorRef.current;
      if (!anchor) return;
      const rect = anchor.getBoundingClientRect();
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;
      const mobile = viewportWidth <= 520;
      const width = mobile ? viewportWidth - 20 : Math.min(540, viewportWidth - 24);
      const left = mobile ? 10 : Math.min(Math.max(12, rect.right - width), viewportWidth - width - 12);
      let top = mobile ? 10 : rect.bottom + 8;
      if (!mobile && viewportHeight - top < 360 && rect.top > 360) {
        top = Math.max(12, rect.top - Math.min(650, viewportHeight - 24) - 8);
      }
      setPanelStyle({ left, top, width, maxHeight: Math.max(180, viewportHeight - top - 10) });
    }
    function handlePointerDown(event) {
      if (!panelRef.current?.contains(event.target) && !anchorRef.current?.contains(event.target)) onClose();
    }
    function handleKeyDown(event) {
      if (event.key === "Escape") onClose();
    }
    positionPanel();
    window.addEventListener("resize", positionPanel);
    window.addEventListener("scroll", positionPanel, true);
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("resize", positionPanel);
      window.removeEventListener("scroll", positionPanel, true);
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [anchorRef, onClose, open]);

  if (!open) return null;
  return createPortal(
    <section ref={panelRef} className="data-quality-popover" style={panelStyle} role="dialog" aria-label={`${stock.symbol} 資料可信度`}>
      <header className="ai-popover-header">
        <div>
          <span>資料可信度</span>
          <strong>{stock.symbol} {stock.name}</strong>
        </div>
        <button className="icon-button small" type="button" onClick={onClose} aria-label="關閉資料可信度">
          <X size={16} />
        </button>
      </header>
      <div className="data-quality-panel">
        {loading && !quality ? (
          <div className="ai-analysis-empty"><Loader2 className="spin" size={16} />讀取資料狀態</div>
        ) : error && !quality ? (
          <div className="ai-analysis-error"><AlertCircle size={16} />{error}</div>
        ) : quality ? (
          <>
            <div className={`data-quality-overall ${quality.overall_status.toLowerCase()}`}>
              <span>整體狀態</span>
              <strong>{quality.overall_status === "HEALTHY" ? "可信" : quality.overall_status === "WARNING" ? "需要留意" : "資料異常"}</strong>
              <small>{formatDate(quality.checked_at)}</small>
            </div>
            <div className="data-quality-list">
              {quality.items.map((item) => (
                <article className="data-quality-item" key={item.category}>
                  <div className="data-quality-item-head">
                    <strong>{item.label}</strong>
                    <DataQualityBadge quality={item} />
                  </div>
                  <div className="data-quality-facts">
                    <span><small>資料日期／期別</small><b>{qualityDataLabel(item)}</b></span>
                    <span>
                      <small>{item.category === "AI_ANALYSIS" ? "成功分析時間" : "取得時間"}</small>
                      <b>{item.fetched_at ? formatDate(item.fetched_at) : "尚無資料"}</b>
                    </span>
                    <span><small>來源</small><b>{item.source || "尚無資料"}</b></span>
                  </div>
                  {item.components?.length > 1 && (
                    <div className="data-quality-components">
                      {item.components.map((component) => (
                        <span key={component.category}>
                          <b>{component.label}</b>
                          <DataQualityBadge quality={component} compact />
                          <small>
                            {component.category.startsWith("AI_") && component.fetched_at
                              ? formatDate(component.fetched_at)
                              : qualityDataLabel(component)}
                          </small>
                        </span>
                      ))}
                    </div>
                  )}
                  {item.last_error_summary && (
                    <details className="data-quality-error">
                      <summary><AlertCircle size={14} />{item.last_error_summary}</summary>
                      <p>{item.last_error_detail}</p>
                      <small>
                        失敗時間 {formatDate(item.last_error_at)}
                        {item.next_retry_at ? ` · 下次重試 ${formatDate(item.next_retry_at)}` : ""}
                      </small>
                    </details>
                  )}
                </article>
              ))}
            </div>
          </>
        ) : null}
      </div>
    </section>,
    document.body,
  );
}
