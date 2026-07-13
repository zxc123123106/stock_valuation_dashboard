export function formatNumber(value, digits = 2) {
  return new Intl.NumberFormat("zh-TW", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value ?? 0);
}

export function formatOptionalNumber(value, digits = 2) {
  return value === null || value === undefined ? "待更新" : formatNumber(value, digits);
}

export function formatOptionalChartNumber(value, digits = 2) {
  return value === null || value === undefined ? "—" : formatNumber(value, digits);
}

export function formatOptionalPe(value) {
  return value === null || value === undefined ? "不適用" : formatNumber(value);
}

export function formatOptionalSignedPercent(value, digits = 2) {
  if (value === null || value === undefined) return "—";
  return `${formatSignedNumber(value, digits)}%`;
}

export function formatOptionalPercent(value, digits = 2) {
  return value === null || value === undefined ? "—" : `${formatNumber(value, digits)}%`;
}

export function formatPeRange(minValue, maxValue) {
  if (minValue === null || minValue === undefined || maxValue === null || maxValue === undefined) return "待更新";
  return `${formatNumber(minValue)}～${formatNumber(maxValue)}`;
}

export function formatSignedNumber(value, digits = 2) {
  return new Intl.NumberFormat("zh-TW", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
    signDisplay: "exceptZero",
  }).format(value ?? 0);
}

export function valueToneClass(value) {
  if (value === null || value === undefined) return "";
  return value >= 0 ? "positive" : "negative";
}

export function percentageToneClass(value) {
  if (value === null || value === undefined) return "";
  return value >= 0 ? "percentage-positive" : "percentage-negative";
}

export function comparisonPercent(currentPrice, indicatorPrice) {
  if (
    currentPrice === null || currentPrice === undefined ||
    indicatorPrice === null || indicatorPrice === undefined ||
    Number(indicatorPrice) === 0
  ) return null;
  return ((Number(currentPrice) - Number(indicatorPrice)) / Number(indicatorPrice)) * 100;
}

export function comparisonToneClass(value) {
  if (value === null || value === undefined || value === 0) return "percentage-zero";
  return percentageToneClass(value);
}

export function formatTradingDate(value) {
  if (!value) return "待更新";
  const [year, month, day] = String(value).split("-");
  return year && month && day ? `${year}/${month}/${day}` : String(value);
}

export function formatDate(value) {
  if (!value) return "待更新";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "待更新";
  return new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

export function formatTaipeiTime(value) {
  if (value === null || value === undefined) return "";
  const timestamp = typeof value === "number" ? (value > 10_000_000_000 ? value : value * 1000) : new Date(value).getTime();
  return new Intl.DateTimeFormat("zh-TW", {
    timeZone: "Asia/Taipei",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date(timestamp));
}

export function formatCountdown(value, now) {
  if (!value) return "待排程";
  const seconds = Math.max(0, Math.ceil((new Date(value).getTime() - now.getTime()) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes} 分 ${remainder} 秒`;
}

export function formatFundamentalMetric(value, valueType = "number", categoryKey = "") {
  if (value === null || value === undefined) return "待更新";
  if (valueType === "percent") return formatOptionalSignedPercent(value);
  if (categoryKey === "monthly_revenue") return `${formatNumber(Number(value) / 100000000)} 億`;
  return formatNumber(value);
}

export function fundamentalToneClass(value, valueType = "number") {
  if (value === null || value === undefined) return "";
  return valueType === "percent" ? percentageToneClass(value) : "constant-value";
}

export function trendDisplayValue(value, categoryKey) {
  if (value === null || value === undefined) return "待更新";
  if (categoryKey === "monthly_revenue") return `${formatNumber(Number(value) / 100000000)} 億`;
  if (["gross_margin", "operating_margin", "net_margin"].includes(categoryKey)) return `${formatNumber(value)}%`;
  return formatNumber(value);
}

export function trendNumericValue(value, categoryKey) {
  if (value === null || value === undefined) return null;
  return categoryKey === "monthly_revenue" ? Number(value) / 100000000 : Number(value);
}
