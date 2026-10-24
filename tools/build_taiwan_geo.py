"""把 taiwan-atlas（dkaoster/taiwan-atlas v2021.9.20，MIT；原始資料：內政部國土測繪中心 NLSC，政府資料開放授權）
towns-10t.json 轉成精簡的 assets/taiwan/taiwan_towns.json（只需執行一次；GitHub Actions 不會再下載）。

輸出格式（共用邊界的 arc 拓撲，所以縣市界、鄉鎮界不會重複、簡化後也不會出縫）：
  arcs:  [[[lon*1e4, lat*1e4], ...], ...]（整數，0.0001°≈11 m）
  kind:  每條 arc 的種類 0=海岸線 1=縣市界 2=鄉鎮界
  towns: [{c: 縣市, t: 鄉鎮, code: TOWNCODE, rings: [[arc idx, ...], ...]  (負數 = ~i 反向；polygon 以 -1 分隔外環/內環群組) }]
用法：python3 tools/build_taiwan_geo.py towns-10t.json assets/taiwan/taiwan_towns.json [容許誤差度數 0.0008]
"""
import json, sys
from collections import Counter
from shapely.geometry import LineString

src, dst = sys.argv[1], sys.argv[2]
tol = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0008      # 約 80 m
topo = json.load(open(src, encoding="utf-8"))
sx, sy = topo["transform"]["scale"]; tx, ty = topo["transform"]["translate"]

arcs = []
for a in topo["arcs"]:
    x = y = 0; pts = []
    for dx, dy in a:
        x += dx; y += dy
        pts.append((x * sx + tx, y * sy + ty))
    arcs.append(pts)

def geom_arcs(g):
    if g["type"] == "Polygon":
        return [g["arcs"]]
    if g["type"] == "MultiPolygon":
        return g["arcs"]
    return []

def flat(polys):
    for poly in polys:
        for ring in poly:
            for i in ring:
                yield i if i >= 0 else ~i

town_use = Counter(); county_use = Counter()
for g in topo["objects"]["towns"]["geometries"]:
    town_use.update(set(flat(geom_arcs(g))))
for g in topo["objects"]["counties"]["geometries"]:
    county_use.update(set(flat(geom_arcs(g))))

def norm(n):
    return n.replace("台", "臺")

used = sorted(set(town_use))
remap = {old: new for new, old in enumerate(used)}
out_arcs, kinds = [], []
npts0 = npts1 = 0
for old in used:
    pts = arcs[old]
    npts0 += len(pts)
    if len(pts) > 2:
        ls = LineString(pts).simplify(tol, preserve_topology=False)
        sp = list(ls.coords)
        if len(sp) < 2 or (pts[0] == pts[-1] and len(sp) < 4):
            sp = pts if len(pts) < 4 else [pts[0], pts[len(pts) // 3], pts[2 * len(pts) // 3], pts[-1]]
    else:
        sp = pts
    sp[0], sp[-1] = pts[0], pts[-1]          # 端點不動，相鄰 arc 才接得起來
    npts1 += len(sp)
    out_arcs.append([[round(x * 1e4), round(y * 1e4)] for x, y in sp])
    if town_use[old] == 1:
        kinds.append(0)                        # 只屬於一個鄉鎮＝海岸線（或國界）
    elif county_use.get(old, 0) >= 2:
        kinds.append(1)
    else:
        kinds.append(2)

def rm(i):
    return remap[i] if i >= 0 else ~remap[~i]

towns = []
for g in topo["objects"]["towns"]["geometries"]:
    p = g["properties"]
    polys = [[[rm(i) for i in ring] for ring in poly] for poly in geom_arcs(g)]
    towns.append({"c": norm(p["COUNTYNAME"]), "t": norm(p["TOWNNAME"]), "code": p["TOWNCODE"], "p": polys})

out = {"source": "taiwan-atlas v2021.9.20 towns-10t.json（MIT，https://github.com/dkaoster/taiwan-atlas）；"
                  "原始資料：內政部國土測繪中心（政府資料開放授權條款第 1 版）",
       "simplify_deg": tol, "scale": 1e-4, "arcs": out_arcs, "kind": kinds, "towns": towns}
json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
import os
print("towns %d, arcs %d (海岸 %d／縣市界 %d／鄉鎮界 %d), 點數 %d → %d, %s %.0f KB" % (
    len(towns), len(out_arcs), kinds.count(0), kinds.count(1), kinds.count(2), npts0, npts1, dst, os.path.getsize(dst) / 1024))
