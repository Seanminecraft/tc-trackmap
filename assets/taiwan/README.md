# 臺灣縣市／鄉鎮界（taiwan_towns.json）

- 來源：[taiwan-atlas](https://github.com/dkaoster/taiwan-atlas) v2021.9.20 `towns-10t.json`（1:10,000，MIT 授權）
- 原始資料：內政部國土測繪中心（NLSC）鄉鎮市區界線，[政府資料開放授權條款－第 1 版](https://data.gov.tw/license)
- 範圍：368 個鄉鎮市區、22 縣市（含澎湖、金門、連江〔馬祖〕、綠島、蘭嶼）；縣市名「台」已統一為「臺」
- 處理：`tools/build_taiwan_geo.py` 以共用邊界（arc）方式簡化（容許誤差 0.0008°，約 80 m），點數 45,084 → 18,546，344 KB；
  共用邊界一起簡化，所以相鄰鄉鎮之間不會出現縫隙或重疊。
- 驗證：用 10/23 20:00 薔琵實際四象限風圈比對未簡化版本，受七級（200）、十級（24）影響的鄉鎮名單完全相同。
- 重新產生：
  ```
  curl -sfLO https://cdn.jsdelivr.net/npm/taiwan-atlas@2021.9.20/towns-10t.json
  python3 tools/build_taiwan_geo.py towns-10t.json assets/taiwan/taiwan_towns.json 0.0008
  ```
