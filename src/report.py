"""结果渲染 —— 把结构化结果转成便于对话展示的文本。

设计原则：
    1. 结论前置：先给「找到了 / 没找到」，再给明细。
    2. 交付物优先：距离由近到远排序，每行给出「门店 + 距离 + 关键信息」。
    3. 不夸大：库存只能报「在售 / 不在售」，余位才能报数字。
"""

from . import geo


def _line(rec):
    parts = ["%s（%s）" % (rec.get("name") or "-", geo.fmt_distance(rec.get("distance")))]
    if rec.get("code") and not str(rec["code"]).startswith("AMAP-"):
        parts.append("编码 %s" % rec["code"])
    if rec.get("address"):
        parts.append(rec["address"])
    if rec.get("rating"):
        parts.append("评分 %s" % rec["rating"])
    return " · ".join(parts)


def render_pool(pool, meta, limit=None):
    lines = []
    stats = meta["pool"]
    lines.append("【门店池】%d 家（%d 家带坐标）｜来源：%s"
                 % (stats["total"], stats["with_coord"],
                    "、".join("%s %d" % (k, v) for k, v in stats["by_source"].items())))
    if meta.get("base_source") == "city_center":
        lines.append("  · 基准位置：城市中心（未提供坐标，半径含义为距市中心）")
    else:
        lines.append("  · 基准位置：用户提供坐标")
    for n in meta.get("notes") or []:
        lines.append("  · " + n)
    stores = pool.within(10 ** 9)
    if limit:
        stores = stores[:limit]
    lines.append("")
    lines.append("按距离由近到远：")
    for i, s in enumerate(stores, 1):
        flag = "  [高德未映射]" if str(s["code"]).startswith("AMAP-") else ""
        lines.append("  %2d. %s%s" % (i, _line(s), flag))
    if limit and stats["total"] > limit:
        lines.append("  …（共 %d 家，此处显示前 %d 家）" % (stats["total"], limit))
    return "\n".join(lines), stores


def render_collab_candidates(cands, total_stores):
    lines = ["【联动 / 限定类候选商品】共 %d 款（按覆盖门店数升序，越靠前越稀缺）" % len(cands)]
    lines.append("  （统计口径：已扫描 %d 家门店的在售菜单，按商品名归一）" % total_stores)
    for c in cands[:20]:
        tag = " / ".join(c["tags"][:3]) if c["tags"] else "-"
        lines.append("  · %-28s 覆盖 %2d 家  [%s]" % (c["name"][:26], c["coverage"], tag))
    if len(cands) > 20:
        lines.append("  …（其余 %d 款略）" % (len(cands) - 20))
    return "\n".join(lines)


def render_availability(report):
    lines = []
    kw = report["keyword"]
    if report["status"] == "found":
        first = next(l for l in report["levels"] if l["selling"])
        lines.append("✅ 在 %s 内找到 %d 家门店在售「%s」（已核 %d 家，失败 %d 家）"
                     % (geo.fmt_distance(first["radius"]), len(first["selling"]), kw,
                        first["checked"], first["failed"]))
    else:
        lines.append("❌ %s 内没有任何门店在售「%s」"
                     % (geo.fmt_distance(report["levels"][-1]["radius"]), kw))

    for lv in report["levels"]:
        head = "— 半径 %s：候选 %d 家，成功核价 %d 家，在售 %d 家%s" % (
            geo.fmt_distance(lv["radius"]), lv["candidates"], lv["checked"], len(lv["selling"]),
            "（已截断）" if lv.get("capped") else "")
        lines.append(head)
        for i, s in enumerate(lv["selling"], 1):
            lines.append("    %d. %s" % (i, _line(s)))
            why = s.get("matched_reason") or ("、".join(s.get("matched") or []) or "")
            if why:
                lines.append("         ↳ %s" % why)

    if report["status"] == "needs_confirm":
        lines.append("")
        lines.append("⚠️ " + report["note"])
        lines.append("   —— 需要用户确认后才会继续扩大检索范围（本工具不会自动越界）。")
    lines.append("")
    lines.append("说明：MCP 不提供库存数量，以上仅为「该门店在售 / 不在售」；"
                 "能否买到以下单结果为准。")
    return "\n".join(lines)


def render_activity_list(acts):
    lines = ["【近期可预约主题活动】共 %d 项" % len(acts)]
    cur = None
    for a in acts:
        if a["category"] != cur:
            cur = a["category"]
            lines.append("  ▸ %s" % cur)
        lines.append("    · %-30s spuId=%-6s 价格 %s  %s ~ %s"
                     % ((a["name"] or "")[:28], a["spuId"], a.get("price") or "-",
                        (a.get("upTime") or "")[:10], (a.get("downTime") or "")[:10]))
    return "\n".join(lines)


def render_nearest_dates(res, activity_name=None):
    lines = []
    title = "（%s）" % activity_name if activity_name else ""
    lines.append("【半径 %s 内最近 %d 家门店的可约日期】%s"
                 % (geo.fmt_distance(res["radius"]), len(res["results"]), title))
    lines.append("  该活动全城共 %d 家门店支持，%s 内 %d 家"
                 % (res["activity_stores_total"], geo.fmt_distance(res["radius"]), res["in_radius"]))
    if not res["results"]:
        lines.append("  ❌ %s 内没有支持该活动的门店。" % geo.fmt_distance(res["radius"]))
        return "\n".join(lines)
    for i, r in enumerate(res["results"], 1):
        if r["bookable"]:
            lines.append("  %d. %s" % (i, _line(r)))
            lines.append("     可约日期：%s" % "、".join(r["dates"][:10]))
            if len(r["dates"]) > 10:
                lines.append("     （共 %d 个可约日期，此处显示前 10 个）" % len(r["dates"]))
        else:
            lines.append("  %d. %s" % (i, _line(r)))
            lines.append("     ⚠️ 该门店当前无可约日期（接口返回空，即不可预约）")
    if res["empty"]:
        lines.append("  ⚠️ %s 内这 %d 家门店均无可约日期。"
                     % (geo.fmt_distance(res["radius"]), len(res["results"])))
    return "\n".join(lines)


def render_stores_on_date(res, activity_name=None):
    lines = []
    title = "（%s）" % activity_name if activity_name else ""
    lines.append("【%s 当日可约门店 · 半径 %s 内】%s"
                 % (res["date"], geo.fmt_distance(res["radius"]), title))
    lines.append("  已核 %d 家门店%s" % (res["candidates"], "（已截断）" if res.get("capped") else ""))
    if res["empty"]:
        lines.append("  ❌ %s 内没有可约该日期的门店（已逐店核实）。"
                     % geo.fmt_distance(res["radius"]))
    else:
        for i, s in enumerate(res["stores"], 1):
            lines.append("  %d. %s" % (i, _line(s)))
    for item in res.get("detail") or []:
        lines.append("  ▸ %s 场次：" % item["store"]["name"][:24])
        if not item["sessions"]:
            lines.append("      （无场次明细）")
        for s in item["sessions"]:
            lines.append("      %s-%s  限 %s-%s 人  余位 %s  价格 %s"
                         % (s["start"], s["end"], s["min"], s["max"], s["left"],
                            ("%.2f 元" % (s["price"] / 100.0)) if s.get("price") else "-"))
    return "\n".join(lines)
