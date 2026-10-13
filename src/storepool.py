"""门店池构建 —— 解决「给定基准位置，附近有哪些门店」这个 MCP 没有直接答案的问题。

背景（实测结论，决定架构）：
    1. query-nearby-stores 是「最近 N 家」接口（实测固定返回 5 家），
       **不是枚举接口**，无法用来列全半径内门店。
    2. query-party-store 返回**真实经纬度**且自带 distance 字段，是 MCP 内
       唯一带坐标的门店列表；但它只覆盖「可承接主题活动的门店」。
    3. 因此本模块用**分层并集**构建门店池：
         L1  query-party-store 多用几个 spuId 取并集（坐标可靠，覆盖主题活动全量）
         L2  query-nearby-stores 以用户给出的地名做补充（补齐最近 5 家）
         L3  高德 POI place/around（可选，有 Key 时启用，用于补全非派对门店）
       最后统一用 haversine 自行计算距离 —— 因为 MCP 返回的 distance
       被验证为**不随入参坐标变化**，不能直接采用。
"""

import json
import os
import re
import urllib.parse
import urllib.request

from . import geo

# query-party-store 在不同 spuId 下的门店覆盖不同，取并集可最大化覆盖。
# 取值依据：上海实测各 spuId 覆盖数（17356=227、3089=224、6370=222、
# 8084=220、1830=220、15276=218、10675=198、8516=201、1779/1065=170）。
WIDE_SPU_IDS = [17356, 3089, 6370, 8084, 1830, 15276]

# 用于拉取城市列表（城市清单与 spuId 无关）
CITY_SPU_ID = 1830


def _amap_key(explicit=None):
    """高德 Web 服务 Key：显式 > 环境变量 > 本机 WorkBuddy 配置。"""
    if explicit:
        return explicit
    env = os.environ.get("AMAP_KEY")
    if env:
        return env.strip()
    path = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    hit = re.search(r"amap-maps.*?key=([A-Za-z0-9]+)", raw, re.S)
    return hit.group(1) if hit else None


class StorePool:
    """门店池：统一的门店记录 + 距离计算 + 半径筛选。"""

    def __init__(self, base_lat=None, base_lng=None):
        self.base_lat = base_lat
        self.base_lng = base_lng
        self._stores = {}       # code -> record

    # ---------- 构建 ----------

    def add(self, code, name, address=None, lat=None, lng=None, source="mcp"):
        if not code:
            return
        rec = self._stores.get(code)
        if rec is None:
            rec = {
                "code": str(code),
                "name": name or "",
                "address": address or "",
                "lat": lat,
                "lng": lng,
                "sources": set(),
                "rating": None,
            }
            self._stores[code] = rec
        else:
            # 用更完整的信息补齐已有记录
            if not rec["name"] and name:
                rec["name"] = name
            if not rec["address"] and address:
                rec["address"] = address
            if rec["lat"] in (None, 0, 0.0) and lat:
                rec["lat"] = lat
            if rec["lng"] in (None, 0, 0.0) and lng:
                rec["lng"] = lng
        rec["sources"].add(source)

    def add_many(self, records, source="mcp"):
        for r in records or []:
            self.add(
                r.get("code") or r.get("storeCode"),
                r.get("name") or r.get("storeName") or r.get("shortName"),
                r.get("address"),
                r.get("latitude"),
                r.get("longitude"),
                source=source,
            )

    # ---------- 查询 ----------

    def __len__(self):
        return len(self._stores)

    def all(self):
        out = []
        for rec in self._stores.values():
            rec = dict(rec)
            rec["sources"] = sorted(rec["sources"])
            rec["distance"] = self.distance_of(rec)
            out.append(rec)
        return out

    def distance_of(self, rec):
        if self.base_lat is None or self.base_lng is None:
            return None
        lat, lng = rec.get("lat"), rec.get("lng")
        if not lat or not lng:
            return None
        return geo.haversine(self.base_lat, self.base_lng, lat, lng)

    def within(self, radius_m, with_unknown=False):
        """按半径筛选，按距离由近到远排序。

        with_unknown=True 时，坐标缺失的门店也会被保留（distance=None），
        排在有坐标的门店之后 —— 宁可多查一家，也不要漏掉。
        """
        items = []
        for rec in self._stores.values():
            rec = dict(rec)
            rec["sources"] = sorted(rec["sources"])
            d = self.distance_of(rec)
            rec["distance"] = d
            if d is None:
                if with_unknown:
                    items.append(rec)
                continue
            if d <= radius_m:
                items.append(rec)
        items.sort(key=lambda x: (x["distance"] is None, x["distance"] or 0))
        return items

    def nearest(self, k, radius_m=None, with_unknown=False):
        pool = self.within(radius_m, with_unknown) if radius_m else self._sorted_all()
        return pool[:k]

    def raw(self):
        """直接暴露内部记录（供合并/打标用，不做拷贝）。"""
        return list(self._stores.values())

    def get(self, code):
        return self._stores.get(str(code))

    def set_rating(self, code, rating):
        rec = self._stores.get(str(code))
        if rec is not None and rating:
            rec["rating"] = rating

    def _sorted_all(self):
        items = self.all()
        items.sort(key=lambda x: (x["distance"] is None, x["distance"] or 0))
        return items

    def stats(self):
        n_coord = sum(1 for r in self._stores.values() if r.get("lat") and r.get("lng"))
        src = {}
        for r in self._stores.values():
            for s in r["sources"]:
                src[s] = src.get(s, 0) + 1
        return {"total": len(self._stores), "with_coord": n_coord, "by_source": src}


# ---------- 采集器 ----------

def fetch_city_list(client):
    """取主题活动城市列表（含城市中心坐标），用于把城市名换成 city code。"""
    data = client.call("query-party-city", {"spuId": CITY_SPU_ID}) or []
    out = []
    for c in data:
        out.append(
            {
                "code": str(c.get("code")),
                "name": c.get("name") or "",
                "lat": c.get("latitude"),
                "lng": c.get("longitude"),
            }
        )
    return out


def resolve_city(client, city_name):
    """城市名 -> (code, lat, lng)。支持「上海」「上海市」等写法。"""
    cities = fetch_city_list(client)
    target = (city_name or "").strip().rstrip("市")
    for c in cities:
        if c["name"].rstrip("市") == target:
            return c
    for c in cities:
        if target and target in c["name"]:
            return c
    return None


def fetch_party_stores(client, city_code, lat, lng, spu_ids=None):
    """L1：用多个 spuId 取并集，拿到带真实坐标的门店列表。"""
    pool = StorePool()
    for spu in spu_ids or WIDE_SPU_IDS:
        try:
            data = client.call(
                "query-party-store",
                {"code": city_code, "latitude": lat, "longitude": lng, "spuId": spu},
            )
        except Exception:
            continue
        pool.add_many(data or [], source="party")
    return pool


def fetch_nearby_supplement(client, city_name, keyword, be_type=1):
    """L2：query-nearby-stores 补充最近若干家（该接口固定返回约 5 家）。"""
    try:
        data = client.call(
            "query-nearby-stores",
            {"beType": be_type, "searchType": 2, "city": city_name, "keyword": keyword},
        )
    except Exception:
        return []
    return data or []


def resolve_store_code(client, city_name, amap_name, cache=None, _calls=None):
    """把「高德 POI 名」解析成 MCP storeCode。

    实测依据：把门店名核心词交给 query-nearby-stores，目标门店会排在返回结果
    的第一位。因此这里取核心词检索，再在返回的 5 家里按名称相似度择优。
    """
    if geo.is_kiosk(amap_name):
        return None
    key = geo.store_keyword(amap_name)
    if not key:
        return None
    if cache is not None and key in cache:
        return cache[key]
    try:
        data = client.call(
            "query-nearby-stores",
            {"beType": 1, "searchType": 2, "city": city_name, "keyword": key},
        )
    except Exception:
        data = None
    if _calls is not None:
        _calls.append(key)
    best, best_score = None, 0.0
    for s in data or []:
        score = geo.name_similar(amap_name, s.get("storeName") or "")
        if score > best_score:
            best, best_score = s, score
    code = best.get("storeCode") if (best is not None and best_score >= 0.5) else None
    if cache is not None:
        cache[key] = code
    return code


def fetch_amap_stores(lat, lng, radius_m, key=None, proxy=None, pages=3):
    """L3（可选）：高德 POI 周边搜索，补全非派对门店并获得品质评分。

    返回 [{name, address, lat, lng, rating, tel, distance}]
    注意：高德坐标同为 GCJ-02，与 MCP 门店坐标同系，可直接比较。
    """
    key = _amap_key(key)
    if not key:
        return None
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]

    out, seen = [], set()
    for page in range(1, max(1, pages) + 1):
        query = urllib.parse.urlencode(
            {
                "key": key,
                "location": "%.6f,%.6f" % (lng, lat),
                "radius": int(radius_m),
                "keywords": "麦当劳",
                "offset": 25,
                "page": page,
                "extensions": "all",
            }
        )
        url = "https://restapi.amap.com/v3/place/around?" + query
        try:
            payload = json.loads(opener.open(url, timeout=30).read().decode("utf-8", "ignore"))
        except Exception:
            break
        pois = payload.get("pois") or []
        if not pois:
            break
        for p in pois:
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            loc = (p.get("location") or "").split(",")
            if len(loc) != 2:
                continue
            biz = p.get("biz_ext") or {}
            out.append(
                {
                    "id": pid,
                    "name": p.get("name") or "",
                    "address": p.get("address") or "",
                    "lat": float(loc[1]),
                    "lng": float(loc[0]),
                    "rating": biz.get("rating"),
                    "cost": biz.get("cost"),
                    "tel": p.get("tel"),
                }
            )
        if len(pois) < 25:
            break
    return out


def build_pool(
    client,
    city_name,
    base_lat,
    base_lng,
    keyword=None,
    amap_radius_m=10000,
    amap_key=None,
    proxy=None,
    use_amap=True,
):
    """构建门店池并返回 (pool, meta)。

    基准位置优先使用调用方给出的坐标；若未给出，则退化为城市中心
    （此时"半径"含义变为"距市中心"，调用方应向用户说明）。
    """
    meta = {"city": None, "base_source": "user", "amap_used": False, "notes": []}

    city = resolve_city(client, city_name) if city_name else None
    if city:
        meta["city"] = city
        if base_lat is None or base_lng is None:
            base_lat, base_lng = city["lat"], city["lng"]
            meta["base_source"] = "city_center"
            meta["notes"].append(
                "未提供基准坐标，已退化为「%s」市中心，半径含义为距市中心距离" % city["name"]
            )

    pool = StorePool(base_lat, base_lng)

    if city:
        l1 = fetch_party_stores(client, city["code"], city["lat"], city["lng"])
        for rec in l1.all():
            pool.add(rec["code"], rec["name"], rec["address"], rec["lat"], rec["lng"], "party")

    if keyword and city_name:
        pool.add_many(fetch_nearby_supplement(client, city_name, keyword), source="nearby")
        meta["notes"].append("已用关键词「%s」补充最近门店" % keyword)

    if use_amap and base_lat is not None:
        amap = fetch_amap_stores(base_lat, base_lng, amap_radius_m, key=amap_key, proxy=proxy)
        if amap:
            meta["amap_used"] = True
            meta["amap_count"] = len(amap)
            # 高德门店要用于 query-meals 在售查询，必须先拿到 MCP storeCode。
            # 三级匹配策略（逐级降级，最大化命中率）：
            #   ① 名称相似度 >= 0.5（与已有 MCP 门店直接比对）
            #   ② 坐标邻近 < 150m（高德与 MCP 同为 GCJ-02，可直接比较）
            #   ③ 反查 query-nearby-stores：用门店名核心词检索，取最相似者
            #      （实测目标门店会被排到第一位）
            cache = _load_storemap_cache()
            calls = []
            matched, via_lookup = 0, 0
            existing = pool.raw()
            for ap in amap:
                best, best_score = None, 0.0
                for rec in existing:
                    if geo.is_kiosk(ap["name"]):
                        break
                    score = geo.name_similar(ap["name"], rec["name"])
                    if score >= 0.5:
                        if score > best_score:
                            best, best_score = rec, score
                        continue
                    if rec.get("lat") and rec.get("lng"):
                        gap = geo.haversine(ap["lat"], ap["lng"], rec["lat"], rec["lng"])
                        if gap < 150 and best_score < 0.5:
                            best, best_score = rec, 0.5
                if best is not None:
                    matched += 1
                    pool.set_rating(best["code"], ap.get("rating"))
                    if not best.get("address") and ap.get("address"):
                        best["address"] = ap["address"]
                    continue
                # ③ 反查
                code = resolve_store_code(client, city_name, ap["name"], cache=cache, _calls=calls)
                if code:
                    via_lookup += 1
                    matched += 1
                    pool.add(code, ap["name"], ap.get("address"), ap["lat"], ap["lng"], source="amap")
                    pool.set_rating(code, ap.get("rating"))
                else:
                    pool.add(
                        "AMAP-%s" % ap["id"],
                        ap["name"],
                        ap["address"],
                        ap["lat"],
                        ap["lng"],
                        source="amap_only",
                    )
                    pool.set_rating("AMAP-%s" % ap["id"], ap.get("rating"))
            _save_storemap_cache(cache)
            meta["amap_matched"] = matched
            meta["amap_via_lookup"] = via_lookup
            meta["notes"].append(
                "已引入高德 POI 增强覆盖：%d 家，其中 %d 家映射到 MCP 门店"
                "（%d 家由名称关键词反查得到），%d 家未映射（仅作位置/评分参考，"
                "不参与在售查询）"
                % (len(amap), matched, via_lookup, len(amap) - matched)
            )


    meta["pool"] = pool.stats()
    return pool, meta


def build_pool_offline(snapshot):
    """从快照（离线演示用）构建门店池，不发起任何网络请求。"""
    pool = StorePool(snapshot.get("base_lat"), snapshot.get("base_lng"))
    for rec in snapshot.get("stores", []):
        pool.add(
            rec.get("code"),
            rec.get("name"),
            rec.get("address"),
            rec.get("lat"),
            rec.get("lng"),
            source=rec.get("source", "party"),
        )
        if rec.get("rating"):
            pool.set_rating(rec.get("code"), rec.get("rating"))
    return pool


# ---------- 门店名 -> storeCode 映射缓存 ----------
# 反查映射每次都要打 query-nearby-stores，代价较高；且门店名与编码的对应关系
# 非常稳定，适合本地缓存。缓存文件不含任何凭据。

_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "storemap.json")


def _load_storemap_cache():
    try:
        with open(_CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_storemap_cache(cache):
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=1)
    except Exception:
        pass


