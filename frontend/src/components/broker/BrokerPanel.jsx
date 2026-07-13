import React from "react";
import { ChevronDown, ChevronRight, Wifi } from "lucide-react";

import { DataQualityBadge } from "../shared/DataQualityBadge";
import {
  formatDate,
  formatNumber,
  formatSignedNumber,
  percentageToneClass,
} from "../../utils/formatters";


export function BrokerTradingDisclosure({ brokerTrading, quality, expanded, onToggle }) {
  return (
    <div className="broker-trading">
      <button className="broker-toggle" type="button" onClick={onToggle} aria-expanded={expanded}>
        <span>
          {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
          主力進出
        </span>
        <span className="disclosure-meta"><small>{brokerTrading?.trade_date || "待更新"}</small><DataQualityBadge quality={quality} compact /></span>
      </button>
      {expanded && (
        <div className="broker-panel">
          {brokerTrading ? (
            <>
              <div className="broker-summary">
                <BrokerSummaryItem label="主力買賣" value={brokerTrading.main_net_volume} tone="net" />
                <BrokerSummaryItem label="主力買" value={brokerTrading.main_buy_volume} tone="buy" />
                <BrokerSummaryItem label="主力賣" value={brokerTrading.main_sell_volume} tone="sell" />
              </div>
              <div className="broker-rankings">
                <BrokerRanking title="買超券商" rows={brokerTrading.buy_brokers} />
                <BrokerRanking title="賣超券商" rows={brokerTrading.sell_brokers} />
              </div>
              <small className="broker-source">
                {brokerTrading.source} · {formatDate(brokerTrading.fetched_at)}
              </small>
            </>
          ) : (
            <div className="valuation-empty">
              <Wifi size={15} />
              主力進出待更新
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function BrokerSummaryItem({ label, value, tone }) {
  const toneClass = tone === "buy" ? "broker-buy" : tone === "sell" ? "broker-sell" : percentageToneClass(value);
  return (
    <div>
      <span>{label}</span>
      <strong className={toneClass}>{formatSignedNumber(value, 0)}</strong>
      <small>張</small>
    </div>
  );
}

function BrokerRanking({ title, rows = [] }) {
  const netLabel = title === "買超券商" ? "買超張數" : "賣超張數";
  return (
    <div className="broker-ranking">
      <strong>{title}</strong>
      {rows.length ? (
        <>
          <div className="broker-ranking-row broker-ranking-head">
            <span>排名</span>
            <span>券商</span>
            <span>買進</span>
            <span>賣出</span>
            <span>{netLabel}</span>
          </div>
          {rows.map((row) => (
            <div className="broker-ranking-row" key={`${title}-${row.rank}-${row.broker_name}`}>
              <span>{row.rank}</span>
              <span>{row.broker_name}</span>
              <span className="broker-trade-volume">{formatNumber(row.buy_volume, 0)}</span>
              <span className="broker-trade-volume">{formatNumber(row.sell_volume, 0)}</span>
              <span className={percentageToneClass(row.net_volume)}>{formatSignedNumber(row.net_volume, 0)}</span>
            </div>
          ))}
        </>
      ) : (
        <small>待更新</small>
      )}
    </div>
  );
}
