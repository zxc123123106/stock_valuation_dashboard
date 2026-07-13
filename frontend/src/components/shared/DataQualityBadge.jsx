const DATA_QUALITY_LABELS = {
  REALTIME: "即時",
  CURRENT: "最新",
  DELAYED: "延遲",
  STALE: "過期",
  MISSING: "待更新",
  NOT_APPLICABLE: "不適用",
};


export function qualityStatusClass(status) {
  return String(status || "MISSING").toLowerCase().replaceAll("_", "-");
}


export function DataQualityBadge({ quality, compact = false }) {
  if (!quality) return null;
  return (
    <span className={`data-quality-badge ${qualityStatusClass(quality.freshness_status)}${compact ? " compact" : ""}`}>
      {DATA_QUALITY_LABELS[quality.freshness_status] || quality.freshness_status}
      {quality.is_cached && <em>使用快取</em>}
    </span>
  );
}
