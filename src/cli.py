"""mcd-store-radar 命令行入口。

用法示例：
    # 自检（确认 Token 与工具可用）
    python -m src.cli tools

    # 需求一：某联名商品，5km 内在售门店（找不到自动扩到 10km）
    python -m src.cli check --city 上海 --base "31.235356,121.476960" --keyword 联名

    # 先看看附近有哪些联动/限定商品可查
    python -m src.cli candidates --city 上海 --base "31.235356,121.476960"

    # 需求二：近期可预约主题活动
    python -m src.cli party-list

    # 需求二a：5km 内最近 3 家门店各自的可约日期
    python -m src.cli party-dates --city 上海 --base "31.235356,121.476960" --spu 1830

    # 需求二b：某日期下 5km 内哪些门店可约（含余位）
    python -m src.cli party-stores --city 上海 --base "31.235356,121.476960" --spu 1830 --date 2026-10-15
"""

import argparse
import json
import sys

from . import collab, geo, party, report
from .mcp_client import McpClient, McpError
from .storepool import build_pool, fetch_amap_stores, _amap_key


def _parse_base(base, base_wgs84):
    """解析基准坐标。默认按 GCJ-02（与 MCP 门店坐标同系）；标 wgs84 时自动换算。"""
    raw = base_wgs84 or base
    if not raw:
        return None, None, None
    try:
        lat_s, lng_s = raw.split(",")
        lat, lng = float(lat_s), float(lng_s)
    except ValueError:
        raise SystemExit("基准坐标格式应为 '纬度,经度'，例如 31.235356,121.476960")
    if base_wgs84:
        lat, lng = geo.wgs84_to_gcj02(lat, lng)
        return lat, lng, "wgs84→gcj02"
    return lat, lng, "gcj02"


def _geocode(address, city, proxy=None):
    """地址文本 -> GCJ-02 坐标（需配置高德 Key；无 Key 则返回 None）。"""
    key = _amap_key()
    if not key:
        return None
    import urllib.parse
    import urllib.request
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = [("User-Agent", "Mozilla/5.0")]
    url = "https://restapi.amap.com/v3/geocode/geo?" + urllib.parse.urlencode(
        {"key": key, "address": address, "city": city or ""}
    )
    try:
        payload = json.loads(opener.open(url, timeout=25).read().decode("utf-8", "ignore"))
        loc = payload["geocodes"][0]["location"]
        lng_s, lat_s = loc.split(",")
        return float(lat_s), float(lng_s)
    except Exception:
        return None


def _client(args):
    return McpClient(token=args.token, proxy=args.proxy, verbose=args.verbose).initialize()


def _emit(text, payload, args):
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(text)


def cmd_tools(args):
    client = _client(args)
    names = client.tool_names()
    print("✅ 麦当劳 MCP 连通，共暴露 %d 个工具：" % len(names))
    for n in names:
        print("   -", n)


def cmd_pool(args):
    client = _client(args)
    lat, lng, note = _parse_base(args.base, args.base_wgs84)
    if lat is None and args.address:
        got = _geocode(args.address, args.city, proxy=args.proxy)
        if got:
            lat, lng = got
            note = "geocode(%s)" % args.address
    pool, meta = build_pool(
        client, args.city, lat, lng, keyword=getattr(args, "keyword_addr", None),
        amap_key=args.amap_key, proxy=args.proxy, use_amap=not args.no_amap,
    )
    text, stores = report.render_pool(pool, meta, limit=args.limit)
    _emit(text, {"meta": meta, "stores": stores}, args)


def cmd_candidates(args):
    client = _client(args)
    lat, lng, note = _parse_base(args.base, args.base_wgs84)
    pool, meta = build_pool(
        client, args.city, lat, lng, keyword=getattr(args, "keyword_addr", None),
        amap_key=args.amap_key, proxy=args.proxy, use_amap=not args.no_amap,
    )
    stores = pool.within(args.radius, with_unknown=False)
    stores = [s for s in stores if not str(s["code"]).startswith("AMAP-")][: args.max_stores]
    menus = collab.collect_menus(client, [s["code"] for s in stores], workers=args.workers)
    cands = collab.find_collab_candidates(menus)
    text = report.render_collab_candidates(cands, len(menus))
    _emit(text, {"candidates": cands, "scanned": len(menus)}, args)


def cmd_check(args):
    client = _client(args)
    lat, lng, note = _parse_base(args.base, args.base_wgs84)
    pool, meta = build_pool(
        client, args.city, lat, lng, keyword=args.keyword_addr,
        amap_key=args.amap_key, proxy=args.proxy, use_amap=not args.no_amap,
    )
    levels = [int(x) for x in (args.levels or "5000,10000").split(",")]
    rep = collab.availability_scan(
        client, pool, args.keyword, levels=levels,
        workers=args.workers, max_stores=args.max_stores,
    )
    rep["base_source"] = meta.get("base_source")
    text = report.render_availability(rep)
    _emit(text, rep, args)


def cmd_expand(args):
    """用户确认后继续扩大检索范围（一次性，不自动越界）。"""
    client = _client(args)
    lat, lng, note = _parse_base(args.base, args.base_wgs84)
    pool, meta = build_pool(
        client, args.city, lat, lng, keyword=args.keyword_addr,
        amap_key=args.amap_key, proxy=args.proxy, use_amap=not args.no_amap,
    )
    lv = collab.scan_beyond(client, pool, args.keyword, args.radius,
                            workers=args.workers, max_stores=args.max_stores)
    fake = {"keyword": args.keyword, "levels": [lv],
            "status": "found" if lv["selling"] else "needs_confirm"}
    if not lv["selling"]:
        fake["note"] = "%s 内仍无门店在售「%s」。建议停止扩大或人工联系门店确认。" % (
            geo.fmt_distance(args.radius), args.keyword)
    _emit(report.render_availability(fake), fake, args)


def cmd_party_list(args):
    client = _client(args)
    acts = party.list_activities(client, only_available=not args.include_offline)
    _emit(report.render_activity_list(acts), {"activities": acts}, args)


def _resolve_city(client, name):
    from .storepool import resolve_city
    city = resolve_city(client, name)
    if not city:
        raise SystemExit("未找到城市「%s」，请检查名称（可用「上海」或「上海市」）" % name)
    return city


def cmd_party_dates(args):
    client = _client(args)
    city = _resolve_city(client, args.city)
    lat, lng, note = _parse_base(args.base, args.base_wgs84)
    if lat is None:
        lat, lng = city["lat"], city["lng"]
    res = party.nearest_stores_dates(
        client, city, args.spu, lat, lng, radius_m=args.radius, k=args.k
    )
    res["base_source"] = note or "city_center"
    _emit(report.render_nearest_dates(res), res, args)


def cmd_party_stores(args):
    client = _client(args)
    city = _resolve_city(client, args.city)
    lat, lng, note = _parse_base(args.base, args.base_wgs84)
    if lat is None:
        lat, lng = city["lat"], city["lng"]
    if args.with_sessions:
        res = party.sessions_for_date(
            client, city, args.spu, args.date, lat, lng, radius_m=args.radius, limit=args.limit
        )
    else:
        res = party.stores_on_date(
            client, city, args.spu, args.date, lat, lng, radius_m=args.radius
        )
    res["base_source"] = note or "city_center"
    _emit(report.render_stores_on_date(res), res, args)


def build_parser():
    p = argparse.ArgumentParser(
        prog="mcd-store-radar",
        description="麦麦门店雷达 —— 联动商品在售门店 / 主题活动余位，递进式检索",
    )
    p.add_argument("--token", help="麦当劳 MCP Token（默认读环境变量 MCD_MCP_TOKEN）")
    p.add_argument("--proxy", help="HTTP 代理，如 http://127.0.0.1:7015")
    p.add_argument("--amap-key", help="高德 Web 服务 Key（可选，用于补全覆盖与品质分）")
    p.add_argument("--no-amap", action="store_true", help="禁用高德增强，仅用麦当劳 MCP")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.add_argument("--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def net_flags(sp):
        """把全局网络参数也挂到每个子命令上。

        原因：argparse 的全局可选参数必须写在子命令**之前**（`cli.py --no-amap pool ...`），
        而用户的自然写法是写在后面（`cli.py pool ... --no-amap`）。只挂全局会导致
        `error: unrecognized arguments: --no-amap`。
        子命令上用 default=SUPPRESS，保证「写在前面」的取值不会被子命令的默认值覆盖。
        """
        sp.add_argument("--token", default=argparse.SUPPRESS, help="麦当劳 MCP Token")
        sp.add_argument("--proxy", default=argparse.SUPPRESS, help="HTTP 代理")
        sp.add_argument("--amap-key", default=argparse.SUPPRESS, help="高德 Web 服务 Key")
        sp.add_argument("--no-amap", action="store_true", default=argparse.SUPPRESS,
                        help="禁用高德增强，仅用麦当劳 MCP")
        sp.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出原始 JSON")
        sp.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS)

    def common(sp, need_keyword_addr=True, with_radius=True, radius_required=False):
        net_flags(sp)
        sp.add_argument("--city", required=True, help="城市名，如 上海")
        sp.add_argument("--base", help="基准坐标 '纬度,经度'（GCJ-02）")
        sp.add_argument("--base-wgs84", help="基准坐标 '纬度,经度'（WGS-84，自动换算）")
        if need_keyword_addr:
            sp.add_argument("--keyword-addr", help="用于补充最近门店的地名关键词")
        if with_radius:
            if radius_required:
                sp.add_argument("--radius", type=int, required=True, help="半径（米）")
            else:
                sp.add_argument("--radius", type=int, default=5000, help="半径（米），默认 5000")
        sp.add_argument("--workers", type=int, default=8, help="并发数，默认 8")
        sp.add_argument("--max-stores", type=int, default=80, help="单轮最多核价门店数")

    sp = sub.add_parser("tools", help="自检：列出 MCP 工具")
    net_flags(sp)
    sp.set_defaults(func=cmd_tools)


    sp = sub.add_parser("pool", help="构建并展示门店池")
    common(sp)
    sp.add_argument("--address", help="地址文本（需高德 Key）")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_pool)

    sp = sub.add_parser("candidates", help="扫描附近门店，列出联动/限定候选商品")
    common(sp)
    sp.set_defaults(func=cmd_candidates)

    sp = sub.add_parser("check", help="需求一：递进式在售扫描（5km → 10km → 请用户确认）")
    common(sp)
    sp.add_argument("--keyword", required=True, help="联名商品关键词或名称，如 联名 / 素龙 / 棒球帽")
    sp.add_argument("--levels", default="5000,10000", help="递进半径列表（米），默认 5000,10000")
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("expand", help="用户确认后，一次性扩大检索范围")
    common(sp, with_radius=False)
    sp.add_argument("--keyword", required=True)
    sp.add_argument("--radius", type=int, required=True, help="扩大后的半径（米）")
    sp.set_defaults(func=cmd_expand)

    sp = sub.add_parser("party-list", help="需求二：近期可预约主题活动清单")
    net_flags(sp)
    sp.add_argument("--include-offline", action="store_true", help="包含已下架活动")
    sp.set_defaults(func=cmd_party_list)

    sp = sub.add_parser("party-dates", help="需求二a：5km 内最近 k 家门店的可约日期")
    common(sp)
    sp.add_argument("--spu", type=int, required=True, help="活动 spuId")
    sp.add_argument("--k", type=int, default=3, help="取最近几家门店，默认 3")
    sp.set_defaults(func=cmd_party_dates)

    sp = sub.add_parser("party-stores", help="需求二b：指定日期下半径内可约门店")
    common(sp)
    sp.add_argument("--spu", type=int, required=True)
    sp.add_argument("--date", required=True, help="日期 yyyy-MM-dd")
    sp.add_argument("--with-sessions", action="store_true", help="同时拉取场次与余位明细")
    sp.add_argument("--limit", type=int, default=5, help="场次明细最多几家门店")
    sp.set_defaults(func=cmd_party_stores)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except McpError as exc:
        print("❌ %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
