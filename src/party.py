"""需求二：可预约主题活动的门店 / 日期 / 场次可得性。

关键设计：门店池必须**按活动（spuId）重新拉取**。
    query-party-store(spuId) 返回的是「该活动支持的门店」，不同活动的门店集合
    并不相同（实测上海：海底小纵队常规版 227 家、四小福豪华版仅 10 家）。
    若沿用全局门店池，会把不支持该活动的门店也报成"可约"，属事实性错误。

能力边界（实测）：
    - query-party-store-date  -> 可约日期（当天无场次时返回空数组）
    - query-party-store-session -> 场次明细，含 leftNum（剩余名额），
      是 MCP 全量 35 个工具中**唯一能读到真实余量**的字段。
"""

from concurrent.futures import ThreadPoolExecutor

from . import geo

# mall-points-products 的类目筛选规则
PARTY_CATEGORIES = {
    "1>6>20": "生日类派对",
    "1>6>21": "主题类派对",
    "1>6>22": "麦麦体验营",
    "1>6>25": "品鉴会",
    "1>6>34": "读书会",
    "1>6>40": "积分兑换活动",
}
PARTY_CAT_RULE = ",".join(PARTY_CATEGORIES.keys())


def list_activities(client, only_available=True):
    """近期可预约的主题活动清单。

    only_available=True 时只保留 status=2（上架）的活动。
    """
    data = client.call("mall-points-products", {"catRuleIds": PARTY_CAT_RULE}) or []
    out = []
    for x in data:
        status = x.get("status")
        if only_available and status != 2:
            continue
        out.append(
            {
                "spuId": x.get("spuId"),
                "name": x.get("spuName"),
                "category": x.get("catName"),
                "selling": x.get("selling"),
                "price": x.get("price"),
                "points": x.get("point"),
                "upTime": x.get("upTime"),
                "downTime": x.get("downTime"),
                "status": status,
            }
        )
    out.sort(key=lambda x: (x["category"] or "", x["name"] or ""))
    return out


def fetch_activity_stores(client, city_code, lat, lng, spu_id):
    """该活动支持的门店列表（带真实坐标）。"""
    try:
        data = client.call(
            "query-party-store",
            {"code": city_code, "latitude": lat, "longitude": lng, "spuId": spu_id},
        )
    except Exception:
        return []
    out = []
    for s in data or []:
        out.append(
            {
                "code": str(s.get("code")),
                "name": s.get("name") or "",
                "short": s.get("shortName") or "",
                "address": s.get("address") or "",
                "lat": s.get("latitude"),
                "lng": s.get("longitude"),
            }
        )
    return out


def _with_distance(stores, base_lat, base_lng):
    for s in stores:
        if base_lat is not None and base_lng is not None and s.get("lat") and s.get("lng"):
            s["distance"] = geo.haversine(base_lat, base_lng, s["lat"], s["lng"])
        else:
            s["distance"] = None
    stores.sort(key=lambda x: (x["distance"] is None, x["distance"] or 0))
    return stores


def fetch_dates(client, store_code, spu_id):
    """某门店该活动的可约日期；无场次时返回空列表。"""
    try:
        data = client.call("query-party-store-date", {"spuId": spu_id, "storeCode": str(store_code)})
    except Exception:
        return None
    if not isinstance(data, list):
        return []
    return [d.get("date") for d in data if d.get("date")]


def fetch_sessions(client, store_code, spu_id, date_str):
    """某门店某日的场次明细，含 leftNum（剩余名额）。"""
    try:
        data = client.call(
            "query-party-store-session",
            {"spuId": spu_id, "storeCode": str(store_code), "dateStr": date_str},
        )
    except Exception:
        return []
    out = []
    for s in data or []:
        out.append(
            {
                "id": s.get("id"),
                "start": s.get("timeStart"),
                "end": s.get("timeEnd"),
                "min": s.get("partyMin"),
                "max": s.get("partyMax"),
                "price": s.get("price"),
                "left": s.get("leftNum"),
            }
        )
    return out


def nearest_stores_dates(
    client,
    city,
    spu_id,
    base_lat,
    base_lng,
    radius_m=5000,
    k=3,
    workers=6,
):
    """需求 2a：半径内最近 k 家门店，各自哪些日期可约。

    返回 {"radius", "activity_stores", "in_radius", "results":[...], "empty": bool}
    """
    stores = fetch_activity_stores(client, city["code"], city["lat"], city["lng"], spu_id)
    _with_distance(stores, base_lat, base_lng)
    in_radius = [s for s in stores if s["distance"] is not None and s["distance"] <= radius_m]
    picked = in_radius[:k]

    results = []
    if picked:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(picked)))) as pool:
            futures = {pool.submit(fetch_dates, client, s["code"], spu_id): s for s in picked}
            for fut in futures:
                s = futures[fut]
                dates = fut.result()
                results.append(
                    {
                        "code": s["code"],
                        "name": s["name"],
                        "address": s["address"],
                        "distance": s["distance"],
                        "dates": dates or [],
                        "bookable": bool(dates),
                    }
                )
    results.sort(key=lambda x: (x["distance"] is None, x["distance"] or 0))
    return {
        "radius": radius_m,
        "activity_stores_total": len(stores),
        "in_radius": len(in_radius),
        "results": results,
        "empty": all(not r["bookable"] for r in results) if results else True,
    }


def stores_on_date(
    client,
    city,
    spu_id,
    date_str,
    base_lat,
    base_lng,
    radius_m=5000,
    workers=8,
    max_stores=80,
):
    """需求 2b：指定日期下，半径内哪些门店可约。"""
    stores = fetch_activity_stores(client, city["code"], city["lat"], city["lng"], spu_id)
    _with_distance(stores, base_lat, base_lng)
    in_radius = [s for s in stores if s["distance"] is not None and s["distance"] <= radius_m]
    capped = len(in_radius) > max_stores
    if capped:
        in_radius = in_radius[:max_stores]

    hits = []
    if in_radius:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch_dates, client, s["code"], spu_id): s for s in in_radius}
            for fut in futures:
                s = futures[fut]
                dates = fut.result()
                if dates and date_str in dates:
                    hits.append(
                        {
                            "code": s["code"],
                            "name": s["name"],
                            "address": s["address"],
                            "distance": s["distance"],
                        }
                    )
    hits.sort(key=lambda x: (x["distance"] is None, x["distance"] or 0))
    return {
        "date": date_str,
        "radius": radius_m,
        "candidates": len(in_radius),
        "capped": capped,
        "stores": hits,
        "empty": not hits,
    }


def sessions_for_date(client, city, spu_id, date_str, base_lat, base_lng, radius_m=5000, limit=5):
    """对指定日期，取半径内最近若干可约门店的场次与余位明细。"""
    res = stores_on_date(client, city, spu_id, date_str, base_lat, base_lng, radius_m=radius_m)
    out = []
    for s in res["stores"][:limit]:
        out.append({"store": s, "sessions": fetch_sessions(client, s["code"], spu_id, date_str)})
    res["detail"] = out
    return res
