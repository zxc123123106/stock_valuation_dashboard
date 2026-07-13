import React from "react";
import { RefreshCcw } from "lucide-react";


export function DashboardHeader({ onRefreshAll }) {
  return (
    <header className="topbar">
      <div>
        <p className="eyebrow">Stock Valuation Dashboard</p>
        <h1>股票估值統計看板</h1>
      </div>
      <button
        className="icon-button"
        type="button"
        onClick={onRefreshAll}
        title="更新全部數據"
        aria-label="更新全部數據"
      >
        <RefreshCcw size={18} />
      </button>
    </header>
  );
}
