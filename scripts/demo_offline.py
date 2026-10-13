"""离线演示 —— 不需要 MCP Token，直接复现真实跑通的结果。

数据来自 examples/snapshot_shanghai.json，由 scripts/make_snapshot.py 用真实
麦当劳 MCP 调用固化（非实时）。用于评审快速验证输出形态，无需申请 Token。

    python -m scripts.demo_offline
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import report  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(os.path.dirname(HERE), "examples", "snapshot_shanghai.json")


def main():
    with open(SNAP, encoding="utf-8") as fh:
        s = json.load(fh)

    print("=" * 78)
    print("麦麦门店雷达 · 离线演示（数据固化于 %s）" % s["generated_at"])
    print("  城市 %s ｜ 基准坐标 %.6f,%.6f ｜ 半径 %dm"
          % (s["city"], s["base_lat"], s["base_lng"], s["radius"]))
    print("  ⚠️ 本演示为快照回放，非实时查询；实时查询请配置 MCP Token 后运行 CLI。")
    print("=" * 78)

    print("\n■ 需求一：联动活动（实物周边 / 联名商品）在售门店")
    print("-" * 78)
    print(report.render_collab_candidates(s["collab_candidates"], s["scanned_stores"]))
    print()
    fake = {
        "keyword": s["collab_item"],
        "status": "found",
        "levels": [{
            "radius": s["radius"],
            "candidates": len(s["stores"]),
            "checked": s["scanned_stores"],
            "failed": len(s["stores"]) - s["scanned_stores"],
            "selling": s["availability"],
        }],
    }
    lines = report.render_availability(fake).splitlines()
    for ln in lines[:16]:
        print(ln)
    if len(lines) > 16:
        print("  …（其余 %d 家在完整输出中列出）" % (len(s["availability"]) - 14))

    print("\n■ 需求二：可预约主题活动")
    print("-" * 78)
    pd = s["party_dates"]
    print(report.render_nearest_dates(pd, activity_name="麦当劳亲子读书会（spuId=%s）" % s["spu_id"]))
    if s.get("party_on_date"):
        print()
        od = dict(s["party_on_date"])
        od["stores"] = [
            {"code": x["code"], "name": x["name"], "address": x["address"], "distance": x["distance"]}
            for x in (od.get("stores") or [])[:3]
        ]
        print(report.render_stores_on_date(od, activity_name="麦当劳亲子读书会"))

    print("\n" + "=" * 78)
    print("说明：MCP 不提供库存数量字段，需求一只输出「该门店在售 / 不在售」；")
    print("      需求二的场次 leftNum 是 MCP 全量工具中唯一可得的真实余量。")
    print("=" * 78)


if __name__ == "__main__":
    main()
