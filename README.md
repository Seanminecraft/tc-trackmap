# tc-trackmap 颱風路徑圖

中央氣象署（W-C0034-005）一有新的颱風消息，GitHub Actions 就自動畫出每顆熱帶氣旋的路徑圖（含七級／十級風暴風四象限半徑、70% 機率範圍、臺灣縣市／鄉鎮界，並把落在現在七級／十級風圈內的鄉鎮塗成黃／紅色），
發布在 https://seanminecraft.github.io/tc-trackmap/ ，天氣儀表板的「🌀 活動中熱帶氣旋」區塊讀取 `tc/manifest.json` 顯示。

- `scripts/tc_trackmap.py`：抓資料＋畫圖＋產生網站
- `assets/logo.png`：右下角 logo
- `assets/taiwan/taiwan_towns.json`：臺灣 368 個鄉鎮市區界（含澎湖、金門、馬祖；來源與授權見 `assets/taiwan/README.md`）
- `tools/build_taiwan_geo.py`：重新產生上面那個檔案用（平常不用跑）
- `requirements.txt`：Python 套件清單
- `.github/workflows/tc-trackmap.yml`：每 30 分鐘檢查一次＋Apps Script 即時觸發
- `.github/workflows/keepalive.yml`：每週重新啟用排程（避免 60 天後被 GitHub 自動停用）

氣象資料僅供參考，預報請依氣象署發布為準，並勿作為重大決策使用。
