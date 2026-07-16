import React, { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, DatabaseBackup, Download, HardDrive, Save, Upload, X } from "lucide-react";

import {
  createDatabaseBackup,
  databaseBackupUrl,
  getDatabaseBackups,
  getDataManagementStatus,
  importUserData,
  previewUserDataImport,
  userDataExportUrl,
} from "../../api/dataManagement";
import { queryKeys } from "../../api/queryKeys";


const formatDateTime = (value) => value
  ? new Intl.DateTimeFormat("zh-TW", { dateStyle: "medium", timeStyle: "short" }).format(new Date(value))
  : "尚無";

const formatSize = (value) => {
  if (!Number.isFinite(value)) return "—";
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
};


export function DataManagementPanel({ open, onClose, onMessage, onError }) {
  const queryClient = useQueryClient();
  const [selectedFile, setSelectedFile] = useState("");
  const [preview, setPreview] = useState(null);
  const statusQuery = useQuery({
    queryKey: queryKeys.dataManagementStatus,
    queryFn: ({ signal }) => getDataManagementStatus({ signal }),
    enabled: open,
  });
  const backupsQuery = useQuery({
    queryKey: queryKeys.databaseBackups,
    queryFn: ({ signal }) => getDatabaseBackups({ signal }),
    enabled: open,
  });
  const backupMutation = useMutation({ mutationFn: createDatabaseBackup });
  const previewMutation = useMutation({ mutationFn: previewUserDataImport });
  const importMutation = useMutation({ mutationFn: importUserData });
  const busy = backupMutation.isPending || previewMutation.isPending || importMutation.isPending;

  useEffect(() => {
    if (!open) {
      setPreview(null);
      setSelectedFile("");
    }
  }, [open]);

  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose, open]);

  if (!open) return null;

  const handleBackup = async () => {
    onError("");
    try {
      await backupMutation.mutateAsync();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.dataManagementStatus }),
        queryClient.invalidateQueries({ queryKey: queryKeys.databaseBackups }),
      ]);
      onMessage("SQLite 一致性備份已完成");
    } catch (error) {
      onError(error.message);
    }
  };

  const handleFile = async (event) => {
    const file = event.target.files?.[0];
    setPreview(null);
    setSelectedFile(file?.name || "");
    if (!file) return;
    onError("");
    try {
      const document = JSON.parse(await file.text());
      setPreview(await previewMutation.mutateAsync(document));
    } catch (error) {
      onError(error instanceof SyntaxError ? "JSON 檔案格式不正確" : error.message);
    }
  };

  const handleImport = async () => {
    if (!preview) return;
    const confirmed = window.confirm(
      `確認用匯入檔案取代目前追蹤清單？\n新增 ${preview.added_symbols.length} 檔、刪除 ${preview.removed_symbols.length} 檔。匯入前會自動建立完整備份。`,
    );
    if (!confirmed) return;
    onError("");
    try {
      const result = await importMutation.mutateAsync({
        document: preview.normalized_document,
        preview_hash: preview.preview_hash,
        expected_revision: preview.current_revision,
        confirm_replace: true,
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.dashboard }),
        queryClient.invalidateQueries({ queryKey: queryKeys.brokerSetting }),
        queryClient.invalidateQueries({ queryKey: queryKeys.dataManagementStatus }),
        queryClient.invalidateQueries({ queryKey: queryKeys.databaseBackups }),
      ]);
      setPreview(null);
      setSelectedFile("");
      onMessage(`使用者資料已匯入，安全備份為 ${result.backup_filename}`);
    } catch (error) {
      onError(error.message);
    }
  };

  const status = statusQuery.data;
  const backups = backupsQuery.data || [];
  return createPortal(
    <div className="data-management-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <section className="data-management-panel" role="dialog" aria-modal="true" aria-label="資料管理">
        <header>
          <div>
            <span className="panel-eyebrow">SQLite</span>
            <h2>資料管理</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} disabled={busy} aria-label="關閉資料管理">
            <X size={19} />
          </button>
        </header>

        <div className="database-health-grid">
          <div><span>Journal</span><strong>{status?.journal_mode?.toUpperCase() || "—"}</strong></div>
          <div><span>Busy timeout</span><strong>{status ? `${status.busy_timeout_ms} ms` : "—"}</strong></div>
          <div><span>外鍵</span><strong>{status?.foreign_keys_enabled ? "已啟用" : "待確認"}</strong></div>
          <div><span>完整性</span><strong>{status?.integrity_status || "—"}</strong></div>
          <div><span>Migration</span><strong>{status?.current_revision || "—"}</strong></div>
          <div><span>每日備份</span><strong>{status ? `${String(status.backup_hour).padStart(2, "0")}:00 · ${status.backup_retention_count} 份` : "—"}</strong></div>
        </div>

        <section className="data-management-section">
          <div className="section-heading">
            <div><HardDrive size={18} /><h3>完整資料庫備份</h3></div>
            <button className="text-button" type="button" onClick={handleBackup} disabled={busy}>
              <DatabaseBackup size={17} />{backupMutation.isPending ? "備份中" : "立即備份"}
            </button>
          </div>
          <p>包含市場快取、分析紀錄與使用者設定。完整還原需先停止後端。</p>
          <div className="backup-list">
            {backups.length === 0 && <span className="empty-line">尚無備份</span>}
            {backups.map((backup) => (
              <div className="backup-row" key={backup.filename}>
                <div>
                  <strong>{formatDateTime(backup.created_at)}</strong>
                  <span>{backup.reason} · {formatSize(backup.size_bytes)}</span>
                </div>
                <a className="icon-button small" href={databaseBackupUrl(backup.filename)} aria-label={`下載 ${backup.filename}`}>
                  <Download size={16} />
                </a>
              </div>
            ))}
          </div>
        </section>

        <section className="data-management-section">
          <div className="section-heading">
            <div><Save size={18} /><h3>使用者資料</h3></div>
            <a className="text-button" href={userDataExportUrl}><Download size={17} />匯出 JSON</a>
          </div>
          <p>只包含券商、追蹤清單、排序與成交均價，不包含 API key 或市場快取。</p>
          <label className="import-file-button">
            <Upload size={17} />
            <span>{selectedFile || "選擇 JSON 並預覽"}</span>
            <input type="file" accept="application/json,.json" onChange={handleFile} disabled={busy} />
          </label>
          {preview && (
            <div className="import-preview">
              <div><span>新增</span><strong>{preview.added_symbols.length}</strong></div>
              <div><span>保留</span><strong>{preview.retained_symbols.length}</strong></div>
              <div><span>刪除</span><strong>{preview.removed_symbols.length}</strong></div>
              <div><span>持倉變更</span><strong>{preview.position_change_count}</strong></div>
              {preview.warnings.map((warning) => <p key={warning}><AlertTriangle size={16} />{warning}</p>)}
              <button className="text-button primary" type="button" onClick={handleImport} disabled={busy}>
                {importMutation.isPending ? "匯入中，請勿關閉" : "確認取代並匯入"}
              </button>
            </div>
          )}
        </section>
      </section>
    </div>,
    document.body,
  );
}
