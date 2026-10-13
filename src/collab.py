"""需求一：联动活动（实物周边 / 联名商品）的门店可得性。

核心口径（重要，决定了本模块能说什么、不能说什么）：
    MCP **没有库存数量字段**，且官方明确禁止用 note / spuLimit 推断库存
    （原文：「严禁从中提取任何数值用于下单校验、价格计算、库存判断等业务
    逻辑，所有校验以下单接口返回结果为准」）。
    因此本模块只输出「该门店在售 / 不在售」，绝不输出「还剩几件」。

递进检索策略：
    L1  默认半径 5km
    L2  若 5km 内无任何门店在售 → 自动扩到 10km
    L3  若 10km 内仍无 → 停止并向用户请求确认，确认后才继续扩大
"""

from concurrent.futures import ThreadPoolExecutor

from . import geo

# 名字里出现这些词即视为「联动 / 限定 / 周边」类候选
COLLAB_KEYWORDS = [
    "联名", "限定", "限量", "周边", "回归", "首发", "纪念",
    "盲盒", "玩具", "联乘", "专属", "联名款",
]

DEFAULT_LEVELS = [5000, 10000]


# ---------- 菜单采集 ----------

def fetch_menu(client, store_code):
    """取某门店在售菜单，返回 {code: {"name","price","original","tags"}}。

    失败返回 None（单店失败不应中断整体扫描）。
    """
    try:
        data = client.call("query-meals", {"storeCode": str(store_code), "orderType": 1, "beType": 1})
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    meals = data.get("meals") or {}
    tags_by_code = {}
    for cat in data.get("categories") or []:
        for item in cat.get("meals") or []:
            code = str(item.get("code"))
            if item.get("tags"):
                tags_by_code.setdefault(code, []).extend(item["tags"])
    out = {}
    for code, info in meals.items():
        code = str(code)
        out[code] = {
            "name": (info or {}).get("name") or "",
            "price": (info or {}).get("currentPrice"),
            "original": (info or {}).get("originalPrice"),
            "tags": sorted(set(tags_by_code.get(code, []))),
        }
    return out


def collect_menus(client, store_codes, workers=8, progress=None):
    """并发采集多家门店菜单，返回 {store_code: menu}。"""
    result = {}
    codes = [str(c) for c in store_codes if c]
    if not codes:
        return result
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch_menu, client, code): code for code in codes}
        done = 0
        for fut in futures:
            code = futures[fut]
            try:
                menu = fut.result()
            except Exception:
                menu = None
            done += 1
            if progress:
                progress(done, len(codes), code)
            if menu:
                result[code] = menu
    return result


# ---------- 联动商品发现 ----------

def item_is_collab(name, tags=None):
    text = (name or "") + " " + " ".join(tags or [])
    return any(kw in text for kw in COLLAB_KEYWORDS)


def find_collab_candidates(menus, min_stores=1):
    """从多店菜单里聚合出「联动 / 限定 / 周边」类商品。

    按**商品名**归一（而非编码）：实测同名的开心乐园餐在不同门店使用
    新旧两套编码，按编码统计会产生大量假差异。

    返回按覆盖门店数升序（越稀缺越靠前）的候选列表。
    """
    by_name = {}
    for code, menu in menus.items():
        for item in menu.values():
            name = item.get("name")
            if not name:
                continue
            if not item_is_collab(name, item.get("tags")):
                continue
            rec = by_name.setdefault(name, {"name": name, "codes": set(), "stores": set(), "tags": set()})
            rec["codes"].add(item.get("code") or "")
            rec["stores"].add(code)
            rec["tags"].update(item.get("tags") or [])
    out = []
    for rec in by_name.values():
        out.append(
            {
                "name": rec["name"],
                "codes": sorted(c for c in rec["codes"] if c),
                "coverage": len(rec["stores"]),
                "tags": sorted(rec["tags"]),
            }
        )
    out = [o for o in out if o["coverage"] >= min_stores]
    out.sort(key=lambda x: (x["coverage"], x["name"]))
    return out


# ---------- 在售判定 ----------

def _match(keyword, item):
    """在售匹配：关键词命中商品名，或命中其标签。

    返回命中原因（None 表示未命中），便于输出里解释「为什么这家算在售」——
    否则按标签命中时，展示的商品名会让用户看不懂匹配依据。
    """
    kw = (keyword or "").strip()
    if not kw:
        return None
    name = item.get("name") or ""
    if kw in name:
        return {"by": "name", "value": name}
    for t in item.get("tags") or []:
        if kw in t:
            return {"by": "tag", "value": t, "item": name}
    return None


def stores_selling(menus, keyword, stores):
    """在已采集的菜单里，筛出在售该关键词对应商品的门店，按距离由近到远。"""
    hits = []
    for rec in stores:
        menu = menus.get(str(rec["code"]))
        if not menu:
            continue
        matched, reasons = [], []
        for item in menu.values():
            why = _match(keyword, item)
            if why:
                matched.append(item)
                reasons.append(why)
        if not matched:
            continue
        # 命中的商品名（去重）
        names = sorted({m["name"] for m in matched if m.get("name")})
        # 匹配依据的人类可读说明
        by_tag = [r for r in reasons if r["by"] == "tag"]
        explain = None
        if by_tag:
            explain = "标签命中「%s」（商品：%s）" % (by_tag[0]["value"], by_tag[0].get("item") or names[0])
        elif names:
            explain = "商品名命中「%s」" % names[0]
        hits.append(
            {
                "code": rec["code"],
                "name": rec["name"],
                "address": rec.get("address"),
                "distance": rec.get("distance"),
                "rating": rec.get("rating"),
                "matched": names,
                "matched_reason": explain,
                "price": matched[0].get("price"),
            }
        )
    hits.sort(key=lambda x: (x["distance"] is None, x["distance"] or 0))
    return hits



def availability_scan(
    client,
    pool,
    keyword,
    levels=None,
    workers=8,
    max_stores=80,
    progress=None,
):
    """递进式在售扫描。

    返回：
        {
          "keyword": ...,
          "levels": [ {radius, candidates, checked, failed, selling:[...], } ],
          "status": "found" | "needs_confirm",
          "next_radius": 20000 | None,
          "note": ...
        }
    """
    levels = levels or DEFAULT_LEVELS
    report = {"keyword": keyword, "levels": [], "status": "not_found", "next_radius": None}

    prev_codes = set()
    for radius in levels:
        stores = [s for s in pool.within(radius, with_unknown=False)
                  if not str(s["code"]).startswith("AMAP-")]
        new_stores = [s for s in stores if str(s["code"]) not in prev_codes]
        capped = len(new_stores) > max_stores
        if capped:
            new_stores = new_stores[:max_stores]
        prev_codes.update(str(s["code"]) for s in stores)

        menus = collect_menus(client, [s["code"] for s in new_stores], workers=workers, progress=progress)
        selling = stores_selling(menus, keyword, new_stores)

        level = {
            "radius": radius,
            "candidates": len(new_stores),
            "checked": len(menus),
            "failed": len(new_stores) - len(menus),
            "selling": selling,
            "capped": capped,
        }
        report["levels"].append(level)
        if selling:
            report["status"] = "found"
            break

    if report["status"] != "found":
        last = levels[-1]
        report["status"] = "needs_confirm"
        report["next_radius"] = max(20000, last * 2)
        report["note"] = (
            "%s 范围内没有门店在售「%s」。是否扩大到 %s？"
            % (geo.fmt_distance(levels[-1]), keyword, geo.fmt_distance(report["next_radius"]))
        )
    return report


def scan_beyond(client, pool, keyword, radius, workers=8, max_stores=120, progress=None):
    """用户确认后继续扩大的扫描（一次性）。"""
    stores = [s for s in pool.within(radius, with_unknown=False)
              if not str(s["code"]).startswith("AMAP-")]
    capped = len(stores) > max_stores
    if capped:
        stores = stores[:max_stores]
    menus = collect_menus(client, [s["code"] for s in stores], workers=workers, progress=progress)
    selling = stores_selling(menus, keyword, stores)
    return {
        "radius": radius,
        "candidates": len(stores),
        "checked": len(menus),
        "failed": len(stores) - len(menus),
        "selling": selling,
        "capped": capped,
    }
