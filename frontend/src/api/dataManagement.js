import { API_BASE_URL, requestJson } from "./client";


export const getDataManagementStatus = ({ signal } = {}) => requestJson("/api/data-management/status", { signal });
export const getDatabaseBackups = ({ signal } = {}) => requestJson("/api/data-management/backups", { signal });
export const createDatabaseBackup = () => requestJson("/api/data-management/backups", { method: "POST" });
export const previewUserDataImport = (document) => requestJson("/api/data-management/import/preview", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ document }),
});
export const importUserData = (payload) => requestJson("/api/data-management/import", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload),
});
export const userDataExportUrl = `${API_BASE_URL}/api/data-management/export`;
export const databaseBackupUrl = (filename) => `${API_BASE_URL}/api/data-management/backups/${encodeURIComponent(filename)}`;
