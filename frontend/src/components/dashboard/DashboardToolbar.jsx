import React from "react";
import { ChevronDown, DatabaseBackup, Plus, Search } from "lucide-react";


export function DashboardToolbar({ symbol, onSymbolChange, brokerSetting, onBrokerChange, onSubmit, onOpenDataManagement }) {
  return (
    <form className="toolbar" aria-label="stock controls" onSubmit={(event) => {
      event.preventDefault();
      onSubmit(symbol);
    }}>
      <label className="search-box">
        <Search size={18} />
        <input
          inputMode="numeric"
          value={symbol}
          onChange={(event) => onSymbolChange(event.target.value)}
          placeholder="股票代號"
        />
      </label>
      <button className="text-button primary" type="submit">
        <Plus size={17} />
        加入/更新
      </button>
      <label className="broker-select-field">
        <span>券商</span>
        <select
          value={brokerSetting?.selected_broker || "CATHAY"}
          onChange={(event) => onBrokerChange(event.target.value)}
          aria-label="選擇券商"
        >
          {(brokerSetting?.brokers || []).map((broker) => (
            <option key={broker.broker_id} value={broker.broker_id}>{broker.name}</option>
          ))}
        </select>
        <ChevronDown className="broker-select-icon" size={17} aria-hidden="true" />
      </label>
      <button
        className="icon-button data-management-button"
        type="button"
        onClick={onOpenDataManagement}
        title="資料管理"
        aria-label="開啟資料管理"
      >
        <DatabaseBackup size={19} />
      </button>
    </form>
  );
}
