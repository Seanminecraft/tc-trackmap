#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tc_trackmap.py — 氣象署颱風路徑圖（每顆颱風一張 PNG）＋ manifest.json，給 GitHub Pages 用

資料：中央氣象署 W-C0034-005「颱風消息與警報－熱帶氣旋路徑」
  - 平常抓氣象署 S3 公開鏡像（不用金鑰、不吃配額）
  - S3 失敗（或還沒更新到 Apps Script 說的那一報）才用 CWA_API_KEY 打 1 次 datastore（金鑰絕不印出）
風場：現在位置用氣象署的四象限半徑（Circle15ms／Circle25ms 的 QuadrantRadii：NE/SE/SW/NW），
      預報點氣象署只給單一半徑 → 畫圓（虛線），並在圖例註明。全部只用氣象署資料，不混用其他機構。

用法：
  python3 scripts/tc_trackmap.py --check            # 只用標準函式庫：有沒有新的一報？（寫 skip=true/false 到 GITHUB_OUTPUT）
  python scripts/tc_trackmap.py --out site           # 抓資料、畫圖、輸出 site/tc/…
  python scripts/tc_trackmap.py --input x.json --out preview --no-history   # 用本機檔案測試
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

SCRIPT_VERSION = "tc-trackmap 1.1"
TPE = dt.timezone(dt.timedelta(hours=8))
S3_URL = "https://cwaopendata.s3.ap-northeast-1.amazonaws.com/Warning/W-C0034-005.json"
DATASTORE_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0034-005?format=JSON&Authorization="
HISTORY_KEEP = 10                         # 每顆颱風保留最近幾報（含最新）
UA = {"User-Agent": "tc-trackmap (GitHub Actions)"}
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def log(*a):
    print(*a, flush=True)


def mask(s):
    """錯誤訊息裡如果有金鑰，一律遮掉"""
    k = os.environ.get("CWA_API_KEY", "")
    s = str(s)
    return s.replace(k, "***") if k else s


# ------------------------------------------------------------------ 抓資料
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_raw(expect=None, wait_s=240):
    """回傳 (raw_json, 來源說明)。expect＝Apps Script 傳來的「應該要有的報」，S3 還沒更新就等一下、再不行才用金鑰。"""
    deadline = time.time() + (wait_s if expect else 0)
    last_err = None
    while True:
        try:
            raw = json.loads(http_get(S3_URL))
            storms, meta = normalize(raw)
            if not expect or expect_satisfied(storms, expect):
                return raw, "S3"
            log("⏳ S3 鏡像還是舊的一報（Apps Script 說應該有：%s），等 60 秒再看" % expect)
        except Exception as e:  # noqa
            last_err = e
            log("⚠️ S3 鏡像讀取失敗：%s" % mask(e))
        if time.time() + 60 > deadline:
            break
        time.sleep(60)
    key = os.environ.get("CWA_API_KEY", "").strip()
    if key:
        try:
            raw = json.loads(http_get(DATASTORE_URL + key))
            log("ℹ️ 改用氣象署 datastore（用了 1 次金鑰呼叫）")
            return raw, "datastore"
        except Exception as e:
            log("⚠️ datastore 也失敗：%s" % mask(e))
            last_err = e
    try:  # 沒金鑰／datastore 失敗：退回 S3 現有版本
        return json.loads(http_get(S3_URL)), "S3"
    except Exception as e:
        raise RuntimeError("抓不到 W-C0034-005：%s" % mask(last_err or e))


def expect_satisfied(storms, expect):
    want = set(x for x in re.split(r"[,\s]+", expect or "") if x and x != "none")
    have = set("%s@%s" % (s["id"], s["issueKey"]) for s in storms)
    if not want:                      # Apps Script 說「沒有颱風」
        return not storms
    return want.issubset(have)


# ------------------------------------------------------------------ 整理資料（datastore 與 S3 兩種格式都吃）
def _list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def _num(v):
    try:
        if v in (None, "", "-"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _txt(v, lang):
    for m in _list(v):
        if isinstance(m, dict) and (m.get("lang") or m.get("@lang")) == lang:
            return m.get("value") or m.get("#text") or ""
    return ""


def _circle(c):
    if not isinstance(c, dict):
        return None, None
    r = _num(c.get("Radius"))
    q = {}
    for it in _list((c.get("QuadrantRadii") or {}).get("Radius") if isinstance(c.get("QuadrantRadii"), dict) else None):
        d = it.get("dir") or it.get("@dir")
        v = _num(it.get("value") if "value" in it else it.get("#text"))
        if d and v is not None:
            q[d.upper()] = v
    return r, (q if len(q) == 4 else None)


def _time(s):
    try:
        return dt.datetime.fromisoformat(s).astimezone(TPE)
    except Exception:
        return None


def _pt(f):
    r7, q7 = _circle(f.get("Circle15ms"))
    r10, q10 = _circle(f.get("Circle25ms"))
    return {
        "lat": _num(f.get("CoordinateLatitude")), "lon": _num(f.get("CoordinateLongitude")),
        "wind": _num(f.get("MaxWindSpeed")), "gust": _num(f.get("MaxGustSpeed")), "p": _num(f.get("Pressure")),
        "mvSpd": _num(f.get("MovingSpeed")), "mvDir": f.get("MovingDirection") or "",
        "r7": r7, "r7q": q7, "r10": r10, "r10q": q10,
        "state": _txt(f.get("StateTransfer"), "zh-hant"),
    }


def normalize(raw):
    """→ (storms, meta)。storms 依編號排序；每顆含 past（含現在位置，最後一筆）、forecast、issue 等。"""
    meta = {}
    if "cwaopendata" in raw:
        doc = raw["cwaopendata"]
        meta["sent"] = doc.get("Sent", "")
        meta["identifier"] = doc.get("Identifier", "")
        root = (doc.get("Dataset") or {}).get("TropicalCyclones") or {}
    else:
        root = (raw.get("records") or {}).get("TropicalCyclones") or {}
    storms = []
    for tc in _list((root or {}).get("TropicalCyclone")):
        if not isinstance(tc, dict):
            continue
        past = []
        for f in _list((tc.get("AnalysisData") or {}).get("Fix")):
            p = _pt(f)
            p["t"] = _time(f.get("DateTime") or "")
            p["move"] = _txt(f.get("MovingPrediction"), "zh-hant")
            if p["lat"] is not None and p["lon"] is not None and p["t"]:
                past.append(p)
        if not past:
            continue
        past.sort(key=lambda p: p["t"])
        fc = []
        for f in _list((tc.get("ForecastData") or {}).get("Fix")):
            p = _pt(f)
            p["init"] = _time(f.get("InitialTime") or "")
            p["tau"] = _num(f.get("ForecastHour"))
            p["r70"] = _num(f.get("Radius70PercentProbability"))
            if p["lat"] is None or p["lon"] is None or p["tau"] is None or not p["init"]:
                continue
            p["t"] = p["init"] + dt.timedelta(hours=p["tau"])
            fc.append(p)
        fc.sort(key=lambda p: p["tau"])
        year = str(tc.get("Year") or past[-1]["t"].year)
        ty = str(tc.get("CwaTyNo") or "").strip()
        td = str(tc.get("CwaTdNo") or "").strip()
        sid = "%s-ty%02d" % (year, int(ty)) if ty.isdigit() else ("%s-td%02d" % (year, int(td)) if td.isdigit() else
                                                                     "%s-%s" % (year, re.sub(r"[^a-z0-9]+", "", (tc.get("TyphoonName") or "tc").lower()) or "tc"))
        issue = fc[0]["init"] if fc else past[-1]["t"]
        if past[-1]["t"] > issue:
            issue = past[-1]["t"]
        s = {
            "id": sid, "year": year, "tyNo": ty, "tdNo": td,
            "name": (tc.get("CwaTyphoonName") or "").strip(), "enName": (tc.get("TyphoonName") or "").strip(),
            "past": past, "forecast": fc, "issue": issue, "issueKey": issue.strftime("%Y%m%d%H%M"),
        }
        s["sig"] = storm_sig(s)
        storms.append(s)
    storms.sort(key=lambda s: (s["year"], int(s["tyNo"]) if s["tyNo"].isdigit() else 999, s["tdNo"]))
    return storms, meta


def storm_sig(s):
    def pt(p):
        return [p.get(k) for k in ("lat", "lon", "wind", "gust", "p", "r7", "r10", "r70", "tau")] + \
            [sorted((p.get("r7q") or {}).items()), sorted((p.get("r10q") or {}).items()), p["t"].isoformat()]
    blob = json.dumps([s["id"], s["name"], s["enName"], [pt(p) for p in s["past"]], [pt(p) for p in s["forecast"]]],
                      ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def all_sig(storms):
    return hashlib.sha1((SCRIPT_VERSION + "|" + "|".join(s["id"] + ":" + s["sig"] for s in storms)).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ 分類與單位
def cwa_category(w):
    """氣象署強度分類（近中心最大風速 m/s）"""
    if w is None:
        return "未提供"
    if w >= 51.0:
        return "強烈颱風"
    if w >= 32.7:
        return "中度颱風"
    if w >= 17.2:
        return "輕度颱風"
    return "熱帶性低氣壓"


CAT_COLOR = {"熱帶性低氣壓": "#38bdf8", "輕度颱風": "#facc15", "中度颱風": "#f97316", "強烈颱風": "#dc2626",
             "溫帶氣旋": "#94a3b8", "未提供": "#94a3b8"}
BEAUFORT = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7, 37.0, 41.5, 46.2, 51.0, 56.1, 61.3]


def beaufort(w):
    if w is None:
        return None
    for i, th in enumerate(BEAUFORT):
        if w < th:
            return i
    return 18          # ≥ 61.3 m/s：17 級以上


def bf_text(w):
    b = beaufort(w)
    if b is None:
        return ""
    return "17 級以上" if b >= 18 else "%d 級" % b


def pt_category(p):
    if p.get("state") and "溫帶" in p["state"]:
        return "溫帶氣旋"
    return cwa_category(p.get("wind"))


def fmt_t(t, with_year=False):
    return t.strftime("%Y-%m-%d %H:%M" if with_year else "%m/%d %H時")


def storm_title(s):
    cur = s["past"][-1]
    cat = cwa_category(cur["wind"])
    nm = s["name"] or s["enName"] or ""
    if cat == "熱帶性低氣壓" and not s["tyNo"]:
        head = "熱帶性低氣壓" + ((" TD%s" % s["tdNo"]) if s["tdNo"] else "") + ((" " + nm) if nm else "")
    else:
        head = ("%s %s" % ("颱風" if cat != "熱帶性低氣壓" else "熱帶性低氣壓", s["name"] or "")).strip()
        if s["enName"]:
            head += " " + s["enName"]
    nos = []
    if s["tyNo"]:
        nos.append("%s 年第 %s 號颱風" % (s["year"], int(s["tyNo"])))
    if s["tdNo"] and s["tyNo"]:
        nos.append("TD%s" % s["tdNo"])
    if not nos:
        nos.append("%s 年" % s["year"])
    return head, "（" + "，".join(nos) + "）" if nos else ""


# ------------------------------------------------------------------ 幾何
R_EARTH = 6371.0


def dest(lat, lon, brg, km):
    la1, lo1, b = math.radians(lat), math.radians(lon), math.radians(brg)
    d = km / R_EARTH
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


def gc_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1, math.sqrt(a)))


def circle_ll(lat, lon, km, n=72):
    return [dest(lat, lon, 360.0 * i / n, km)[::-1] for i in range(n + 1)]


def quad_ll(lat, lon, q, n=24):
    """四象限風圈：NE 0–90°、SE 90–180°、SW 180–270°、NW 270–360°，每象限用各自半徑畫弧（象限交界成階梯）"""
    pts = []
    for d, a0 in (("NE", 0), ("SE", 90), ("SW", 180), ("NW", 270)):
        r = q.get(d) or 0
        for i in range(n + 1):
            la, lo = dest(lat, lon, a0 + 90.0 * i / n, r) if r > 0 else (lat, lon)
            pts.append((lo, la))
    pts.append(pts[0])
    return pts


def cone_polygon(center_pts):
    """70% 機率範圍：各預報點的 70% 半徑圓，相鄰兩點取凸包再聯集（與氣象署潛勢圖相同概念）"""
    from shapely.geometry import Polygon, MultiPoint
    from shapely.ops import unary_union
    circles = [Polygon(circle_ll(la, lo, max(r, 1.0), 48)) for la, lo, r in center_pts]
    parts = [MultiPoint(list(a.exterior.coords) + list(b.exterior.coords)).convex_hull for a, b in zip(circles, circles[1:])]
    return unary_union(parts or circles)


# ------------------------------------------------------------------ 臺灣縣市／鄉鎮界與受風影響鄉鎮
# 資料：assets/taiwan/taiwan_towns.json（由 tools/build_taiwan_geo.py 從 taiwan-atlas towns-10t 轉出，隨 repo 一起，不必線上下載）
TW_GEO_PATH = os.path.join(ROOT, "assets", "taiwan", "taiwan_towns.json")
TW_BOX = (118.1, 122.1, 21.85, 26.4)          # 含金門、馬祖、澎湖、蘭嶼綠島
_TW_CACHE = {}
COUNTY_ORDER = ["基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "苗栗縣", "臺中市", "彰化縣", "南投縣", "雲林縣",
                "嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣", "宜蘭縣", "花蓮縣", "臺東縣", "澎湖縣", "金門縣", "連江縣"]


def load_taiwan(path=None):
    """讀鄉鎮多邊形與邊界線（同一個程式執行只讀一次，多顆颱風共用）"""
    path = path or TW_GEO_PATH
    if path in _TW_CACHE:
        return _TW_CACHE[path]
    from shapely.geometry import Polygon, MultiPolygon, MultiLineString
    from shapely.validation import make_valid
    from shapely.ops import unary_union
    import shapely
    if not os.path.exists(path):
        log("⚠️ 找不到鄉鎮界資料 %s，略過縣市／鄉鎮界" % path)
        _TW_CACHE[path] = None
        return None
    d = json.load(open(path, encoding="utf-8"))
    sc = d["scale"]
    arcs = [[(x * sc, y * sc) for x, y in a] for a in d["arcs"]]

    def ring(idx):
        pts = []
        for i in idx:
            a = arcs[i] if i >= 0 else arcs[~i][::-1]
            pts.extend(a if not pts else a[1:])
        return pts
    towns = []
    for t in d["towns"]:
        polys = []
        for poly in t["p"]:
            rings = [r for r in (ring(x) for x in poly) if len(r) >= 4]
            if rings:
                polys.append(Polygon(rings[0], rings[1:]))
        def fix(g):
            if g.is_valid:
                return g
            g = make_valid(g)
            if g.geom_type == "GeometryCollection":
                g = unary_union([x for x in g.geoms if x.geom_type in ("Polygon", "MultiPolygon")])
            return g
        polys = [fix(x) for x in polys]
        g = fix(unary_union(polys)) if len(polys) > 1 else polys[0]
        towns.append({"c": t["c"], "t": t["t"], "code": t["code"], "geom": g})
    lines = {}
    for k in (0, 1, 2):
        segs = [arcs[i] for i, kk in enumerate(d["kind"]) if kk == k and len(arcs[i]) >= 2]
        lines[k] = MultiLineString(segs)
    shapely.prepare(lines[0])
    tw = {"towns": towns, "lines": lines, "source": d.get("source", "")}
    _TW_CACHE[path] = tw
    return tw


def _aeqd(lat, lon):
    """以颱風中心為原點的球面等距方位投影（R＝6371 km，與 dest() 相同的球）：
    在這個平面上「到中心的距離＝大圓距離、方位角＝真方位」，所以四象限扇形就是精確的平面扇形。"""
    from pyproj import Transformer
    crs = "+proj=aeqd +lat_0=%.6f +lon_0=%.6f +R=%d +units=m +no_defs" % (lat, lon, int(R_EARTH * 1000))
    fwd = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    inv = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    return fwd, inv


def wind_field_xy(q, r, n=90):
    """風圈在 AEQD 平面（公尺）的多邊形：有四象限就是四個扇形的聯集（NE 0–90°、SE 90–180°…），沒有就用單一半徑圓"""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    ring = []
    if q and any((q.get(k) or 0) > 0 for k in ("NE", "SE", "SW", "NW")):
        # 一個環走完四個象限：象限交界處沿半徑方向「階梯」接起來＝四個扇形的聯集
        for d, a0 in (("NE", 0), ("SE", 90), ("SW", 180), ("NW", 270)):
            rd = (q.get(d) or 0) * 1000.0
            if rd <= 0:
                ring.append((0.0, 0.0))
                continue
            ring += [(rd * math.sin(math.radians(a0 + 90.0 * i / n)), rd * math.cos(math.radians(a0 + 90.0 * i / n)))
                     for i in range(n + 1)]
    elif r and r > 0:
        rr = r * 1000.0
        ring = [(rr * math.sin(2 * math.pi * i / (4 * n)), rr * math.cos(2 * math.pi * i / (4 * n))) for i in range(4 * n)]
    if len(ring) < 3:
        return None
    g = Polygon(ring)
    return g if g.is_valid else g.buffer(0)


def wind_field_ll(lat, lon, q, r, n=90):
    """畫圖用：把同一個 AEQD 風圈多邊形轉回經緯度（著色判斷與畫出來的形狀完全相同）；回傳環的 list"""
    poly = wind_field_xy(q, r, n)
    if poly is None:
        return None
    _, inv = _aeqd(lat, lon)
    out = []
    for g in getattr(poly, "geoms", [poly]):
        xs, ys = zip(*g.exterior.coords)
        lo, la = inv.transform(xs, ys)
        if lon > 90:
            lo = [x + 360 if x < 0 else x for x in lo]      # 換日線附近保持連續
        out.append(list(zip(lo, la)))
    return out


def towns_in_field(lat, lon, q, r, tw):
    """回傳與風圈有任何重疊（intersects，不是中心點）的鄉鎮 index 集合；距離用球面大圓距離（AEQD）"""
    import numpy as np
    import shapely
    if not tw:
        return set()
    poly = wind_field_xy(q, r)
    if poly is None:
        return set()
    rmax_km = max([r or 0] + [v or 0 for v in (q or {}).values()])
    # 先用大圓距離粗篩：鄉鎮外接框中心離颱風中心 > 半徑＋外接框對角線 → 不可能重疊
    fwd, _ = _aeqd(lat, lon)
    shapely.prepare(poly)
    hit = set()
    for i, t in enumerate(tw["towns"]):
        x0, y0, x1, y1 = t["geom"].bounds
        diag = gc_km(y0, x0, y1, x1)
        if gc_km(lat, lon, (y0 + y1) / 2, (x0 + x1) / 2) > rmax_km + diag + 5:
            continue
        g = shapely.transform(t["geom"], lambda xy: np.column_stack(fwd.transform(xy[:, 0], xy[:, 1])))
        if poly.intersects(g):
            hit.add(i)
    return hit


def affected_towns(s, tw=None):
    """現在位置的七級／十級風圈影響哪些鄉鎮。回傳 dict（給圖與 manifest 用）"""
    tw = tw if tw is not None else load_taiwan()
    cur = s["past"][-1]
    res = {"r10": [], "r7": [], "r10Counties": [], "r7Counties": []}
    if not tw:
        return res
    # 颱風太遠（離臺灣框 > 最大半徑＋50 km）直接略過
    rmax = max([cur.get("r7") or 0, cur.get("r10") or 0] + [v or 0 for v in (cur.get("r7q") or {}).values()]
               + [v or 0 for v in (cur.get("r10q") or {}).values()])
    clat = min(max(cur["lat"], TW_BOX[2]), TW_BOX[3]); clon = min(max(cur["lon"], TW_BOX[0]), TW_BOX[1])
    if rmax <= 0 or gc_km(cur["lat"], cur["lon"], clat, clon) > rmax + 50:
        return res
    h10 = towns_in_field(cur["lat"], cur["lon"], cur.get("r10q"), cur.get("r10"), tw)
    h7 = towns_in_field(cur["lat"], cur["lon"], cur.get("r7q"), cur.get("r7"), tw) | h10
    order = lambda idx: sorted(idx, key=lambda i: tw["towns"][i]["code"])

    def counties(idx):
        cs = {tw["towns"][i]["c"] for i in idx}
        return sorted(cs, key=lambda c: COUNTY_ORDER.index(c) if c in COUNTY_ORDER else 99)
    res["r10"] = [tw["towns"][i]["c"] + tw["towns"][i]["t"] for i in order(h10)]
    res["r7"] = [tw["towns"][i]["c"] + tw["towns"][i]["t"] for i in order(h7)]
    res["r10Counties"] = counties(h10)
    res["r7Counties"] = counties(h7)
    res["_idx10"], res["_idx7only"] = h10, h7 - h10
    return res


# ------------------------------------------------------------------ 繪圖
TAIWAN_BOX = (118.0, 124.0, 20.5, 27.0)
TAIPEI = (25.04, 121.56)
TOWN7_FILL = (1.0, 0.82, 0.0, 0.50)      # 鄉鎮受七級風：黃（半透明，界線看得到）
TOWN10_FILL = (0.86, 0.12, 0.12, 0.55)   # 鄉鎮受十級風：紅（紅蓋黃）


def setup_fonts():
    from matplotlib import font_manager, rcParams
    cands = []
    for d in (os.environ.get("TC_FONT_DIR", ""), os.path.join(ROOT, "fonts"), os.path.expanduser("~/.fonts")):
        if d and os.path.isdir(d):
            cands += [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.lower().endswith((".otf", ".ttf", ".ttc"))]
    fam = None
    for f in cands:
        try:
            font_manager.fontManager.addfont(f)
            nm = font_manager.FontProperties(fname=f).get_name()
            if fam is None or "TC" in nm:
                fam = nm
        except Exception:
            pass
    names = [fam] if fam else []
    names += ["Noto Sans CJK TC", "Noto Sans TC", "Noto Sans CJK JP", "DejaVu Sans"]
    rcParams["font.family"] = "sans-serif"
    rcParams["font.sans-serif"] = names
    rcParams["axes.unicode_minus"] = False
    return names[0]


def compute_extent(s):
    cur = s["past"][-1]
    lons, lats = [], []
    recent = [p for p in s["past"] if p["t"] >= cur["t"] - dt.timedelta(hours=72)]
    for p in recent:
        lons.append(p["lon"]); lats.append(p["lat"])
    for p in s["forecast"]:
        r = p.get("r70") or 0
        dlat = r / 111.0
        dlon = r / (111.0 * max(0.2, math.cos(math.radians(p["lat"]))))
        lons += [p["lon"] - dlon, p["lon"] + dlon]; lats += [p["lat"] - dlat, p["lat"] + dlat]
    r7 = max([cur.get("r7") or 0] + list((cur.get("r7q") or {}).values()))
    lats += [cur["lat"] - r7 / 111.0, cur["lat"] + r7 / 111.0]
    lons += [cur["lon"] - r7 / 100.0, cur["lon"] + r7 / 100.0]
    x0, x1 = min(lons + [TAIWAN_BOX[0]]) - 2.5, max(lons + [TAIWAN_BOX[1]]) + 2.5
    y0, y1 = min(lats + [TAIWAN_BOX[2]]) - 2.0, max(lats + [TAIWAN_BOX[3]]) + 2.0
    y0, y1 = max(y0, -5.0), min(y1, 55.0)
    # 長寬比限制在 1.25～1.9（太扁就往南北補、太高就往東西補）
    mx = lambda y: math.log(math.tan(math.pi / 4 + math.radians(y) / 2))
    for _ in range(3):
        w = math.radians(x1 - x0)
        h = mx(y1) - mx(y0)
        asp = w / h
        if asp > 1.9:
            need = w / 1.9 - h
            y0 -= math.degrees(need / 2) * 0.9; y1 += math.degrees(need / 2) * 0.9
            y0, y1 = max(y0, -5.0), min(y1, 55.0)
        elif asp < 1.25:
            need = h * 1.25 - w
            x0 -= math.degrees(need / 2); x1 += math.degrees(need / 2)
    return x0, x1, y0, y1


def render_storm(s, meta, out_png, logo_path=None, generated=None, source_label="S3", aff=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature

    setup_fonts()
    generated = generated or dt.datetime.now(TPE)
    cur = s["past"][-1]
    fc = s["forecast"]
    x0, x1, y0, y1 = compute_extent(s)
    if os.environ.get("TC_EXTENT"):            # 只給測試／檢查用：強制範圍「西,東,南,北」
        x0, x1, y0, y1 = [float(v) for v in os.environ["TC_EXTENT"].split(",")]
    clon = 180.0 if x1 > 178 else 0.0
    proj = ccrs.Mercator(central_longitude=clon)
    pc = ccrs.PlateCarree()

    W = 13.0                     # 吋；dpi 125 → 1625 px 寬
    mx = lambda y: math.log(math.tan(math.pi / 4 + math.radians(y) / 2))
    asp = math.radians(x1 - x0) / (mx(y1) - mx(y0))
    lm, rm = 0.035, 0.035
    map_w = W * (1 - lm - rm)
    map_h = map_w / asp
    title_h, foot_h = 1.05, 1.08
    H = title_h + map_h + foot_h
    fig = plt.figure(figsize=(W, H), dpi=125, facecolor="white")
    ax = fig.add_axes([lm, foot_h / H, 1 - lm - rm, map_h / H], projection=proj)
    ax.set_extent([x0, x1, y0, y1], crs=pc)
    ax.set_facecolor("#dbeafe")
    ax.add_feature(cfeature.LAND.with_scale("50m"), facecolor="#f5f1e6", edgecolor="none", zorder=1)
    ax.add_feature(cfeature.COASTLINE.with_scale("50m"), edgecolor="#64748b", linewidth=0.6, zorder=2)
    ax.add_feature(cfeature.BORDERS.with_scale("50m"), edgecolor="#a8a29e", linewidth=0.4, linestyle=":", zorder=2)
    gl = ax.gridlines(crs=pc, draw_labels=True, linewidth=0.4, color="#94a3b8", alpha=0.6, linestyle="--")
    gl.top_labels = gl.right_labels = False
    gl.xlabel_style = gl.ylabel_style = {"size": 8.5, "color": "#475569"}
    halo = [pe.withStroke(linewidth=2.6, foreground="white")]

    # ---- 臺灣縣市／鄉鎮界＋受七級／十級風影響的鄉鎮著色（臺灣在圖內才畫）
    tw = load_taiwan()
    if aff is None:
        aff = affected_towns(s, tw)
    ppd = W * (1 - lm - rm) * 125 / (x1 - x0)          # 每經度幾個像素（決定線寬）
    tw_in = tw and not (TW_BOX[1] < x0 or TW_BOX[0] > x1 or TW_BOX[3] < y0 or TW_BOX[2] > y1)
    n10, n7only = len(aff.get("_idx10") or ()), len(aff.get("_idx7only") or ())
    if tw_in:
        towns = tw["towns"]
        if n7only:
            ax.add_geometries([towns[i]["geom"] for i in aff["_idx7only"]], crs=pc, facecolor=TOWN7_FILL,
                              edgecolor="none", zorder=6.4)
        if n10:
            ax.add_geometries([towns[i]["geom"] for i in aff["_idx10"]], crs=pc, facecolor=TOWN10_FILL,
                              edgecolor="none", zorder=6.5)
        k = min(1.0, max(0.35, ppd / 90.0))
        ax.add_geometries([tw["lines"][2]], crs=pc, facecolor="none", edgecolor="#94a3b8", linewidth=0.35 * k,
                          alpha=0.85, zorder=6.6)
        ax.add_geometries([tw["lines"][1], tw["lines"][0]], crs=pc, facecolor="none", edgecolor="#475569",
                          linewidth=0.75 * k, zorder=6.7)

    # 臺灣標示與距離參考
    ax.text(121.0, 23.6, "臺灣", transform=pc, fontsize=10, color="#334155", ha="center", va="center", zorder=3,
            path_effects=halo, fontweight="bold")

    # 70% 機率範圍（預報）
    if fc and any(p.get("r70") for p in fc):
        try:
            pts = [(cur["lat"], cur["lon"], 1.0)] + [(p["lat"], p["lon"], p.get("r70") or 1.0) for p in fc]
            cone = cone_polygon(pts)
            ax.add_geometries([cone], crs=pc, facecolor="#ffffff", alpha=0.55, edgecolor="#475569",
                              linewidth=1.0, linestyle=(0, (4, 3)), zorder=3)
        except Exception as e:
            log("⚠️ 70% 機率範圍繪製失敗：%s" % e)

    # 預報點風圈（氣象署只給單一半徑）→ 虛線圓
    for j, p in enumerate(fc):
        if not (int(p["tau"]) % 24 == 0 or j == len(fc) - 1):
            continue      # 只畫每 24 小時與最後一點，避免圈圈疊太多
        for key, col in (("r7", "#d97706"), ("r10", "#dc2626")):
            r = p.get(key)
            if r and r > 0:
                xy = circle_ll(p["lat"], p["lon"], r, 60)
                ax.plot([a for a, b in xy], [b for a, b in xy], transform=pc, color=col, linewidth=0.7,
                        linestyle=(0, (2, 2)), alpha=0.8, zorder=4)

    # 現在位置的四象限風圈（氣象署 QuadrantRadii；沒有才用單一半徑）
    def draw_wind(q, r, face, edge, z):
        rings = wind_field_ll(cur["lat"], cur["lon"], q, r)     # 與鄉鎮著色判斷用的是同一個多邊形
        if not rings:
            return False
        for xy in rings:
            ax.fill([a for a, b in xy], [b for a, b in xy], transform=pc, facecolor=face, edgecolor=edge,
                    linewidth=1.6, zorder=z)
        return True
    has7 = draw_wind(cur.get("r7q"), cur.get("r7"), (1.0, 0.84, 0.2, 0.32), "#d97706", 5)
    has10 = draw_wind(cur.get("r10q"), cur.get("r10"), (0.94, 0.27, 0.27, 0.38), "#b91c1c", 6)

    # 過去路徑
    past = s["past"]
    ax.plot([p["lon"] for p in past], [p["lat"] for p in past], transform=pc, color="#334155", linewidth=1.6, zorder=7)
    for p in past[:-1]:
        ax.scatter(p["lon"], p["lat"], transform=pc, s=26, color=CAT_COLOR[pt_category(p)], edgecolor="#1e293b",
                   linewidth=0.6, zorder=8)
    lab_days = set()
    for p in past[:-1]:
        if p["t"].hour == 8 and p["t"].date() not in lab_days and p["t"] >= cur["t"] - dt.timedelta(days=10):
            lab_days.add(p["t"].date())
            ax.text(p["lon"], p["lat"], " " + p["t"].strftime("%m/%d"), transform=pc, fontsize=8, color="#334155",
                    ha="left", va="top", zorder=9, path_effects=halo, clip_on=True)

    # 預報路徑
    if fc:
        ax.plot([cur["lon"]] + [p["lon"] for p in fc], [cur["lat"]] + [p["lat"] for p in fc], transform=pc,
                color="#334155", linewidth=1.5, linestyle=(0, (5, 3)), zorder=7)
        prev = (cur["lon"], cur["lat"])

        def disp(lon, lat):
            x, y = proj.transform_point(lon, lat, pc)
            return ax.transData.transform((x, y))
        placed = [disp(cur["lon"], cur["lat"])]
        nlab = 0
        for i, p in enumerate(fc):
            ax.scatter(p["lon"], p["lat"], transform=pc, s=46, facecolor=CAT_COLOR[pt_category(p)], edgecolor="#1e293b",
                       linewidth=1.0, zorder=8, marker="o")
            # 標籤放在前進方向的右側（交錯避免重疊）
            dx, dy = p["lon"] - prev[0], p["lat"] - prev[1]
            prev = (p["lon"], p["lat"])
            nx, ny = (dy, -dx) if (dx or dy) else (1, 0)
            nrm = math.hypot(nx, ny) or 1
            txt = fmt_t(p["t"])
            if p.get("wind"):
                txt += "\n%d m/s" % round(p["wind"])
            elif p.get("state"):
                txt += "\n" + p["state"]
            d = disp(p["lon"], p["lat"])
            if i != len(fc) - 1 and any(math.hypot(d[0] - q[0], d[1] - q[1]) < 62 for q in placed):
                continue      # 太擠就不標（點還是會畫），最後一點一定標
            placed.append(d)
            nlab += 1
            side = 1 if nlab % 2 else -1
            ox, oy = 15 * side * nx / nrm, 15 * side * ny / nrm
            ax.annotate(txt, (p["lon"], p["lat"]), xycoords=pc._as_mpl_transform(ax), xytext=(ox, oy),
                        textcoords="offset points", fontsize=8.2, color="#0f172a", ha="center", va="center",
                        zorder=10, path_effects=halo, annotation_clip=True)

    # 現在位置符號
    ccol = CAT_COLOR[pt_category(cur)]
    ax.scatter(cur["lon"], cur["lat"], transform=pc, s=260, marker="o", facecolor=ccol, edgecolor="#0f172a",
               linewidth=1.6, zorder=11)
    ax.scatter(cur["lon"], cur["lat"], transform=pc, s=40, marker="x", color="#0f172a", linewidth=1.4, zorder=12)
    nm = s["name"] or s["enName"] or ("TD%s" % s["tdNo"])
    ax.annotate("%s\n%s" % (nm, fmt_t(cur["t"])), (cur["lon"], cur["lat"]), xycoords=pc._as_mpl_transform(ax),
                xytext=(0, 20), textcoords="offset points", ha="center", va="bottom", fontsize=10.5,
                fontweight="bold", color="#0f172a", zorder=12, path_effects=halo)

    # ---- 角落擺放：資訊框放在路徑最少的角落，圖例放第二少的角落
    def frac(px, py):
        return (px - x0) / (x1 - x0), (mx(py) - mx(y0)) / (mx(y1) - mx(y0))
    allp = [frac(p["lon"], p["lat"]) for p in past[-30:] + fc] + [frac(cur["lon"], cur["lat"])] * 5
    if tw_in:   # 臺灣（含著色鄉鎮）也不要被資訊框、圖例蓋住
        allp += [frac(lo, la) for lo, la in ((121.0, 23.7), (121.5, 25.0), (120.7, 22.5), (119.6, 23.5))] * 3
    corners = {"upper left": (0, 1), "upper right": (1, 1), "lower left": (0, 0), "lower right": (1, 0)}
    score = {k: sum(1 for (fx, fy) in allp if abs(fx - cx) < 0.42 and abs(fy - cy) < 0.45) for k, (cx, cy) in corners.items()}
    order = sorted(corners, key=lambda k: (score[k], k != "upper left"))
    info_loc, leg_loc = order[0], order[1]

    # 資訊框
    lines = ["▍現在（%s）" % cur["t"].strftime("%m/%d %H:%M"),
             "中心位置　北緯 %.1f°・東經 %.1f°" % (cur["lat"], cur["lon"]),
             "中心氣壓　%s hPa" % (int(cur["p"]) if cur["p"] else "—"),
             "最大風速　%s m/s（%s）" % (int(cur["wind"]) if cur["wind"] is not None else "—", bf_text(cur["wind"]) or "—"),
             "瞬間陣風　%s m/s（%s）" % (int(cur["gust"]) if cur["gust"] is not None else "—", bf_text(cur["gust"]) or "—")]

    def rad_txt(q, r):
        if q:
            return "%d km（NE %d／SE %d／SW %d／NW %d）" % (r or max(q.values()), q["NE"], q["SE"], q["SW"], q["NW"])
        return "%d km" % r if r else "—"
    lines.append("七級風半徑　" + rad_txt(cur.get("r7q"), cur.get("r7")))
    lines.append("十級風半徑　" + rad_txt(cur.get("r10q"), cur.get("r10")))
    if cur.get("mvDir") or cur.get("mvSpd"):
        lines.append("移動　%s %s km/h" % (cur.get("mvDir") or "", int(cur["mvSpd"]) if cur.get("mvSpd") else ""))
    if cur.get("move"):
        mv = cur["move"]
        import textwrap as _tw
        lines.append("預測　" + "\n　　　".join(_tw.wrap(mv, 18)))
    import textwrap as _tw2
    if n10:
        lines.append("受十級風影響：%d 個鄉鎮" % len(aff["r10"]))
        lines.append("　" + "\n　".join(_tw2.wrap("、".join(aff["r10Counties"]), 20)))
    if aff.get("r7"):
        lines.append("受七級風影響：%d 個鄉鎮%s" % (len(aff["r7"]), "（含十級）" if n10 else ""))
        lines.append("　" + "\n　".join(_tw2.wrap("、".join(aff["r7Counties"]), 20)))
    lines.append("距臺北約 %s km" % format(int(round(gc_km(cur["lat"], cur["lon"], *TAIPEI), -1)), ","))
    ha = "left" if "left" in info_loc else "right"
    va = "top" if "upper" in info_loc else "bottom"
    ax.text(0.012 if ha == "left" else 0.988, 0.985 if va == "top" else 0.015, "\n".join(lines),
            transform=ax.transAxes, ha=ha, va=va, fontsize=9.2, color="#0f172a", linespacing=1.45, zorder=20,
            bbox=dict(boxstyle="round,pad=0.55", facecolor="white", edgecolor="#cbd5e1", alpha=0.93))

    # 圖例
    hs = [Line2D([], [], color="#334155", lw=1.6, label="過去路徑")]
    if fc:
        hs.append(Line2D([], [], color="#334155", lw=1.5, ls=(0, (5, 3)), label="預報路徑（標籤＝臺灣時間）"))
    for c in ("熱帶性低氣壓", "輕度颱風", "中度颱風", "強烈颱風"):
        hs.append(Line2D([], [], marker="o", ls="", markerfacecolor=CAT_COLOR[c], markeredgecolor="#1e293b", markersize=7, label=c))
    if any(pt_category(p) == "溫帶氣旋" for p in fc):
        hs.append(Line2D([], [], marker="o", ls="", markerfacecolor=CAT_COLOR["溫帶氣旋"], markeredgecolor="#1e293b", markersize=7, label="變性為溫帶氣旋"))
    q7 = "四象限" if cur.get("r7q") else "單一半徑"
    if has7:
        hs.append(Patch(facecolor=(1.0, 0.84, 0.2, 0.32), edgecolor="#d97706", label="七級風暴風範圍（現在，%s）" % q7))
    if has10:
        hs.append(Patch(facecolor=(0.94, 0.27, 0.27, 0.38), edgecolor="#b91c1c",
                        label="十級風暴風範圍（現在，%s）" % ("四象限" if cur.get("r10q") else "單一半徑")))
    if tw_in and n7only:
        hs.append(Patch(facecolor=TOWN7_FILL, edgecolor="#475569", lw=0.6, label="鄉鎮受七級風影響"))
    if tw_in and n10:
        hs.append(Patch(facecolor=TOWN10_FILL, edgecolor="#475569", lw=0.6, label="鄉鎮受十級風影響"))
    if any(p.get("r7") or p.get("r10") for p in fc):
        hs.append(Line2D([], [], color="#d97706", lw=0.9, ls=(0, (2, 2)), label="預報暴風半徑（七級／十級紅）"))
    if any(p.get("r70") for p in fc):
        hs.append(Patch(facecolor="white", edgecolor="#475569", ls="--", label="70% 機率範圍"))
    leg = ax.legend(handles=hs, loc=leg_loc, fontsize=8.4, frameon=True, framealpha=0.93, edgecolor="#cbd5e1",
                    borderpad=0.6, labelspacing=0.45, handlelength=2.2)
    leg.set_zorder(20)

    # ---- 標題
    head, nos = storm_title(s)
    fig.text(lm, 1 - 0.20 / H, head + nos, ha="left", va="top", fontsize=19, fontweight="bold", color="#0f172a")
    cat = pt_category(cur)
    sub = "氣象署分析時間 %s（臺灣時間）｜現在強度：%s，近中心最大風速 %s m/s（%s）" % (
        cur["t"].strftime("%Y-%m-%d %H:%M"), cat, int(cur["wind"]) if cur["wind"] is not None else "—", bf_text(cur["wind"]) or "—")
    if cur["p"]:
        sub += "，中心氣壓 %d hPa" % cur["p"]
    fig.text(lm, 1 - 0.66 / H, sub, ha="left", va="top", fontsize=11.5, color="#334155")
    fig.add_artist(Line2D([lm, 1 - rm], [1 - (title_h - 0.06) / H] * 2, color=CAT_COLOR.get(cat, "#94a3b8"), lw=3.2,
                          transform=fig.transFigure))

    # ---- 頁尾（左：來源與免責；右：logo，不蓋到文字）
    sent = ""
    if meta.get("sent"):
        try:
            sent = "，檔案發布 " + dt.datetime.fromisoformat(meta["sent"]).astimezone(TPE).strftime("%m/%d %H:%M")
        except Exception:
            pass
    fc_note = "預報點暴風半徑氣象署只提供單一半徑" if fc else "本報無預報點"
    if tw_in and (n10 or n7only):
        fc_note += "；鄉鎮著色＝與現在位置七級／十級風圈有任何重疊（球面距離）"
    foot = ["資料來源：中央氣象署（颱風路徑、七級／十級風暴風半徑、70%% 機率半徑%s）；底圖：Natural Earth；縣市／鄉鎮界：內政部國土測繪中心（taiwan-atlas 2021）" % sent,
            "風圈：現在位置使用氣象署四象限半徑（NE／SE／SW／NW）；%s，以圓表示" % fc_note,
            "氣象資料僅供參考，預報請依氣象署發布為準，並勿作為重大決策使用。",
            "繪圖時間 %s（臺灣時間）｜所有時間皆為臺灣時間（UTC+8）" % generated.strftime("%Y-%m-%d %H:%M")]
    logo_w_in = 0.0
    if logo_path and os.path.exists(logo_path):
        try:
            from PIL import Image
            img = Image.open(logo_path).convert("RGBA")
            side = foot_h * 0.86
            lax = fig.add_axes([1 - rm - side / W, (foot_h - side) / 2 / H, side / W, side / H])
            lax.imshow(img)
            lax.axis("off")
            logo_w_in = side + 0.2
        except Exception as e:
            log("⚠️ logo 讀取失敗：%s" % e)
    import textwrap
    maxchars = int((W * (1 - lm - rm) - logo_w_in) / (8.6 / 72 * 0.98))
    ftxt = []
    for i, l in enumerate(foot):
        ftxt += textwrap.wrap(l, maxchars) or [""]
    fig.text(lm, (foot_h - 0.14) / H, "\n".join(ftxt), ha="left", va="top", fontsize=8.6, color="#475569", linespacing=1.5)
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    tmp = out_png + ".tmp.png"
    fig.savefig(tmp, dpi=125, facecolor="white")
    plt.close(fig)
    # 壓縮：調色盤 256 色（地圖顏色少，肉眼看不出差別，檔案約小一半以上）
    try:
        from PIL import Image
        im = Image.open(tmp).convert("RGB")
        im.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.Dither.NONE).save(out_png, optimize=True)
        os.remove(tmp)
    except Exception:
        os.replace(tmp, out_png)
    return out_png


# ------------------------------------------------------------------ 網站輸出
def load_prev_manifest(base):
    if not base:
        return None
    try:
        return json.loads(http_get(base.rstrip("/") + "/tc/manifest.json?t=%d" % time.time(), timeout=20))
    except Exception as e:
        log("ℹ️ 讀不到線上 manifest（第一次部署屬正常）：%s" % e)
        return None


def build_site(storms, meta, out, base, prev, source, logo, history=True):
    tcdir = os.path.join(out, "tc")
    os.makedirs(tcdir, exist_ok=True)
    now = dt.datetime.now(TPE)
    prev_storms = {}
    for ps in (prev or {}).get("storms", []):
        prev_storms[ps.get("id")] = ps
        if ps.get("cwaTdNo"):
            prev_storms["td:" + ps["cwaTdNo"] + ":" + str(ps.get("year"))] = ps
    items = []
    for s in storms:
        v = s["issueKey"] + "-" + s["sig"][:6]
        img_rel = "tc/%s.png" % s["id"]
        arch_rel = "tc/archive/%s/%s.png" % (s["id"], s["issueKey"])
        log("🖌️ 繪製 %s %s %s（分析 %s）" % (s["id"], s["name"], s["enName"], s["issue"].strftime("%m/%d %H:%M")))
        aff = affected_towns(s)
        render_storm(s, meta, os.path.join(out, img_rel), logo, now, source, aff=aff)
        hist = [{"issue": s["issue"].isoformat(), "image": arch_rel}]
        if history:
            os.makedirs(os.path.dirname(os.path.join(out, arch_rel)), exist_ok=True)
            import shutil
            shutil.copyfile(os.path.join(out, img_rel), os.path.join(out, arch_rel))
            ps = prev_storms.get(s["id"]) or prev_storms.get("td:%s:%s" % (s["tdNo"], s["year"]))
            for h in (ps or {}).get("history", []):
                if len(hist) >= HISTORY_KEEP:
                    break
                if not h.get("image") or h.get("issue") == hist[0]["issue"] or any(x["image"] == h["image"] for x in hist):
                    continue
                dst = os.path.join(out, h["image"])
                try:
                    data = http_get(base.rstrip("/") + "/" + h["image"], timeout=30)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    open(dst, "wb").write(data)
                    hist.append({"issue": h["issue"], "image": h["image"]})
                except Exception as e:
                    log("ℹ️ 舊圖 %s 抓不到，略過：%s" % (h["image"], e))
        else:
            hist = []
        cur = s["past"][-1]
        head, nos = storm_title(s)
        it = {
            "id": s["id"], "year": s["year"], "cwaTyNo": s["tyNo"], "cwaTdNo": s["tdNo"],
            "name": s["name"], "enName": s["enName"], "title": head + nos,
            "issue": s["issue"].isoformat(), "analysisTime": cur["t"].isoformat(),
            "category": pt_category(cur), "wind": cur["wind"], "gust": cur["gust"], "pressure": cur["p"],
            "lat": cur["lat"], "lon": cur["lon"],
            "r7": cur.get("r7"), "r7q": cur.get("r7q"), "r10": cur.get("r10"), "r10q": cur.get("r10q"),
            "forecastHours": int(s["forecast"][-1]["tau"]) if s["forecast"] else 0,
            "image": img_rel, "v": v, "sig": s["sig"],
            "url": (base.rstrip("/") + "/" + img_rel + "?v=" + v) if base else (img_rel + "?v=" + v),
            "history": hist,
            "towns": {k: aff[k] for k in ("r10", "r7", "r10Counties", "r7Counties")},
        }
        items.append(it)
    man = {
        "version": 1, "generator": SCRIPT_VERSION, "generated": now.isoformat(timespec="seconds"),
        "source": "中央氣象署 W-C0034-005（%s）" % source, "sent": meta.get("sent", ""),
        "sig": all_sig(storms), "count": len(items), "storms": items,
        "note": "目前沒有活動中的熱帶氣旋" if not items else "",
        "disclaimer": "氣象資料僅供參考，預報請依氣象署發布為準，並勿作為重大決策使用",
    }
    with open(os.path.join(tcdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    write_index(out, man)
    return man


def write_index(out, man):
    cards = "".join(
        '<figure><a href="{img}?v={v}"><img src="{img}?v={v}" alt="{t}" loading="lazy"></a><figcaption>{t}｜分析 {a}</figcaption></figure>'.format(
            img=s["image"], v=s["v"], t=s["title"], a=s["analysisTime"][:16].replace("T", " "))
        for s in man["storms"]) or "<p>目前沒有活動中的熱帶氣旋。</p>"
    html = """<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>颱風路徑圖</title><style>body{font-family:system-ui,"Noto Sans TC",sans-serif;margin:16px;background:#f8fafc;color:#0f172a}
img{max-width:100%;border:1px solid #cbd5e1;border-radius:8px}figure{margin:0 0 20px}small{color:#64748b}</style>
<h1>🌀 颱風路徑圖（中央氣象署資料）</h1><p><small>更新：""" + man["generated"] + """（臺灣時間）｜<a href="tc/manifest.json">manifest.json</a></small></p>
""" + cards + "<p><small>" + man["disclaimer"] + "</small></p></html>"
    open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(html)


# ------------------------------------------------------------------ main
def gh_output(k, v):
    p = os.environ.get("GITHUB_OUTPUT")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (k, v))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--input", help="用本機 JSON（datastore 或 S3 格式）")
    ap.add_argument("--check", action="store_true", help="只檢查有沒有新的一報")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--expect", default=os.environ.get("TC_EXPECT", ""), help="Apps Script 傳來的 id@yyyymmddHHMM，逗號分隔")
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--source-label", default="", help="配合 --input：manifest 上寫的資料來源（S3／datastore）")
    ap.add_argument("--logo", default=os.path.join(ROOT, "assets", "logo.png"))
    a = ap.parse_args()
    base = (os.environ.get("PAGES_BASE") or "").strip()
    if base and not base.endswith("/"):
        base += "/"

    if a.input:
        raw, source = json.load(open(a.input, encoding="utf-8")), (a.source_label or "本機檔案")
    else:
        raw, source = fetch_raw(a.expect.strip() or None)
    storms, meta = normalize(raw)
    sig = all_sig(storms)
    log("📡 來源 %s：%d 顆 %s｜sig %s" % (source, len(storms), "、".join("%s(%s %s)" % (s["id"], s["name"] or s["enName"], s["issue"].strftime("%m/%d %H:%M")) for s in storms) or "（無）", sig))

    prev = load_prev_manifest(base)
    if a.check:
        same = bool(prev) and prev.get("sig") == sig and not a.force
        log("⏭️ 線上已是最新（不畫圖、不部署）" if same else "🆕 有新資料（或第一次／強制），要畫圖")
        gh_output("skip", "true" if same else "false")
        # 把這次抓到的原始檔留給下一步，避免重抓
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "_raw.json"), "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        gh_output("source", source)
        gh_output("count", str(len(storms)))
        return 0
    man = build_site(storms, meta, a.out, base, prev, source, a.logo, history=not a.no_history)
    raw_cache = os.path.join(a.out, "_raw.json")
    if os.path.exists(raw_cache):
        os.remove(raw_cache)
    log("✅ 完成：%d 張圖，manifest sig %s" % (man["count"], man["sig"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
