"""生成离线演示快照（真实数据固化，供 demo_offline.py 免 Token 复现）。

用法：
    python -m scripts.make_snapshot --city 上海 --base "31.235356,121.476960"
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab, party                      # noqa: E402
from src.mcp_client import McpClient               # noqa: E402
from src.storepool import build_pool, resolve_city  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "examples", "snapshot_shanghai.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="上海")
    ap.add_argument("--base", default="31.235356,121.476960")
    ap.add_argument("--item", default="联名")
    ap.add_argument("--spu", type=int, default=1830)
    ap.add_argument("--radius", type=int, default=5000)
    args = ap.parse_args()

    lat, lng = [float(x) for x in args.base.split(",")]
    client = McpClient().initialize()

    pool, meta = build_pool(client, args.city, lat, lng, use_amap=False)
    stores = pool.within(args.radius, with_unknown=False)
    stores = [s for s in stores if not str(s["code"]).startswith("AMAP-")]

    menus = collab.collect_menus(client, [s["code"] for s in stores], workers=8)
    avail = collab.stores_selling(menus, args.item, stores)
    cands = collab.find_collab_candidates(menus)[:12]

    city = resolve_city(client, args.city)
    dates = party.nearest_stores_dates(client, city, args.spu, lat, lng, radius_m=args.radius, k=3)
    on_date = None
    if dates["results"]:
        d0 = next((r["dates"][0] for r in dates["results"] if r["bookable"]), None)
        if d0:
            on_date = party.sessions_for_date(client, city, args.spu, d0, lat, lng,
                                              radius_m=args.radius, limit=2)
            on_date["date"] = d0

    snapshot = {
        "generated_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "离线演示快照：由真实麦当劳 MCP 调用固化，非实时数据。",
        "city": args.city,
        "base_lat": lat,
        "base_lng": lng,
        "radius": args.radius,
        "pool_meta": meta,
        "stores": stores,
        "collab_item": args.item,
        "availability": avail,
        "collab_candidates": cands,
        "scanned_stores": len(menus),
        "spu_id": args.spu,
        "party_dates": dates,
        "party_on_date": on_date,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=1)
    print("已写入 %s" % OUT)
    print("  门店池 %d 家 / 半径内 %d 家 / 已核菜单 %d 家" % (meta["pool"]["total"], len(stores), len(menus)))
    print("  在售「%s」门店 %d 家" % (args.item, len(avail)))
    print("  候选联动商品 %d 款" % len(cands))


if __name__ == "__main__":
    main()
