# D 槽本機執行

本專案的 Windows 本機環境已設定為將專案資料、runtime、套件、快取、暫存檔、
PostgreSQL 資料與擷取結果放在 D 槽。

## 主要位置

- 專案：`D:\property-case-radar`
- Python 與 Node：`D:\property-case-radar\.runtime`
- PostgreSQL 17：`D:\PostgreSQL\17`
- Umi-OCR：`D:\Umi-OCR_Paddle_v2.1.5`
- 擷取資料及資料庫備份：`D:\網頁識別認證`
- 專案 log：`D:\property-case-radar\logs`

`scripts\use_d_runtime.ps1` 會設定 `HOME`、`USERPROFILE`、`APPDATA`、
`LOCALAPPDATA`、`TEMP`、pip cache、Python pycache 及 Playwright browser path，
讓這些可控的執行資料留在 `.runtime`。

## 啟動與註冊

手動啟動 API：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_api.ps1
```

一次註冊所有登入／定期排程：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_local_d_tasks.ps1
```

API 位址為 `http://127.0.0.1:8000`，健康檢查為
`http://127.0.0.1:8000/health`。本機 PostgreSQL 使用 15432，避免與既有
Docker 的 5432 衝突。

## 狀態檢查

```powershell
Get-ScheduledTask | Where-Object TaskName -Like "Property Case Radar*" |
    Select-Object TaskName, State

Get-NetTCPConnection -State Listen |
    Where-Object LocalPort -In 8000,1224,15432,18765,18766,18767
```

Windows 排程器仍須使用作業系統內建的
`C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe` 作為啟動器；
這是 Windows 系統元件。排程所啟動的專案 Python、Node、PostgreSQL、OCR、
程式資料、套件、快取和 log 均位於 D 槽。
