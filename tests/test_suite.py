"""mcd-store-radar 端到端测试套件（真实调用，可复现）。

覆盖：
    需求一  主路径 / 递进确认分支 / 关闭高德模式 / WGS-84 坐标换算
    需求二  活动清单 / 最近 k 家可约日期 / 指定日期可约门店 + 余位 / 无可约分支
    准确率  抽样回验（在售判定双向核对）
    性能    调用次数与耗时

用法：
    python -m tests.test_suite --city 上海 --base "31.235356,121.476960"
"""

import argparse
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import collab, geo, party                                     # noqa: E402
from src.mcp_client import McpClient                                   # noqa: E402
from src.storepool import build_pool, fetch_amap_stores, resolve_city  # noqa: E402

RESULTS = []


def record(name, ok, detail, seconds=None, calls=None):
    RESULTS.append({
        "case": name, "ok": bool(ok), "detail": detail,
        "seconds": round(seconds, 1) if seconds is not None else None,
        "calls": calls,
    })
    flag = "✅ PASS" if ok else "❌ FAIL"
    extra = ""
    if seconds is not None:
        extra += "  [%.1fs" % seconds
        if calls is not None:
            extra += ", %d calls" % calls
        extra += "]"
    print("%s  %s%s" % (flag, name, extra))
    print("        %s" % detail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="上海")
    ap.add_argument("--base", default="31.235356,121.476960")
    ap.add_argument("--spu", type=int, default=1830, help="主题活动 spuId（默认亲子读书会）")
    ap.add_argument("--radius", type=int, default=5000)
    ap.add_argument("--item", default="联名", help="需求一查询的联名关键词")
    args = ap.parse_args()

    lat, lng = [float(x) for x in args.base.split(",")]
    client = McpClient().initialize()

    print("=" * 80)
    print("mcd-store-radar 测试套件  |  %s  |  基准 %.6f,%.6f"
          % (time.strftime("%Y-%m-%d %H:%M:%S"), lat, lng))
    print("=" * 80)

    # ---------- 0. 连通性 ----------
    t0 = time.time()
    names = client.tool_names()
    record("0. MCP 连通与工具枚举", len(names) >= 30,
           "服务端暴露 %d 个工具" % len(names), time.time() - t0)

    # ---------- 共享：门店池 ----------
    t0 = time.time(); c0 = client.call_count
    pool, meta = build_pool(client, args.city, lat, lng, keyword="人民广场")
    pool_secs, pool_calls = time.time() - t0, client.call_count - c0
    record("1. 门店池构建（三层并集）", len(pool) > 100,
           "池内 %d 家 / 带坐标 %d 家 / 来源 %s"
           % (meta["pool"]["total"], meta["pool"]["with_coord"],
              ", ".join("%s %d" % (k, v) for k, v in meta["pool"]["by_source"].items())),
           pool_secs, pool_calls)

    in5 = [s for s in pool.within(args.radius) if not str(s["code"]).startswith("AMAP-")]
    record("2. 半径筛选（自行 haversine）", len(in5) > 0,
           "%dm 内可用编码门店 %d 家，最近一家 %.0fm（%s）"
           % (args.radius, len(in5), in5[0]["distance"] if in5 else -1,
              in5[0]["name"][:20] if in5 else "-"))

    # ---------- 需求一 主路径 ----------
    t0 = time.time(); c0 = client.call_count
    rep = collab.availability_scan(client, pool, args.item, levels=[args.radius], workers=8, max_stores=60)
    secs, calls = time.time() - t0, client.call_count - c0
    lv = rep["levels"][0]
    ok = rep["status"] == "found" and len(lv["selling"]) > 0
    record("3. 需求一 · 5km 在售门店（主路径）", ok,
           "核价 %d 家（失败 %d）→ 在售 %d 家；最近：%s %.0fm"
           % (lv["checked"], lv["failed"], len(lv["selling"]),
              lv["selling"][0]["name"][:22] if lv["selling"] else "-",
              lv["selling"][0]["distance"] if lv["selling"] else -1),
           secs, calls)

    # ---------- 需求一 准确率抽样回验 ----------
    if lv["selling"]:
        selling_codes = {s["code"] for s in lv["selling"]}
        menus = collab.collect_menus(client, [s["code"] for s in lv["selling"][:6]], workers=6)
        check_ok, checked = 0, 0
        for code, menu in menus.items():
            hit = any(args.item in (i.get("name") or "") or
                      any(args.item in t for t in (i.get("tags") or []))
                      for i in menu.values())
            checked += 1
            check_ok += 1 if hit else 0
        record("4. 回验 · 报称『在售』再核一遍", check_ok == checked,
               "抽 %d 家重新拉菜单 → %d 家确认在售（误报 %d）"
               % (checked, check_ok, checked - check_ok))

    # ---------- 需求一 递进 + 确认分支 ----------
    t0 = time.time(); c0 = client.call_count
    rare = "泷泷星愿"
    rep2 = collab.availability_scan(client, pool, rare, levels=[args.radius], workers=8, max_stores=60)
    lv2 = rep2["levels"][0]
    missing = [s for s in in5 if s["code"] not in {x["code"] for x in lv2["selling"]}][:3]
    miss_ok = 0
    for s in missing:
        menu = collab.fetch_menu(client, s["code"])
        if menu and not any(rare in (i.get("name") or "") for i in menu.values()):
            miss_ok += 1
    record("5. 回验 · 报称『不在售』再核一遍", miss_ok == len(missing),
           "抽 %d 家 → %d 家确认确实没有（漏报 %d）" % (len(missing), miss_ok, len(missing) - miss_ok))

    # 确认分支：用一个必然找不到的关键词
    rep3 = collab.availability_scan(client, pool, "绝不存在的商品名XYZ", levels=[args.radius], workers=8, max_stores=25)
    need_confirm = rep3["status"] == "needs_confirm" and rep3.get("next_radius")
    record("6. 需求一 · 找不到时请求确认（不自行越界）", need_confirm,
           "状态=%s，建议下一档半径=%s；提示语：%s"
           % (rep3["status"], geo.fmt_distance(rep3.get("next_radius") or 0),
              (rep3.get("note") or "")[:52]))

    # ---------- 需求一 关闭高德 ----------
    t0 = time.time(); c0 = client.call_count
    pool2, meta2 = build_pool(client, args.city, lat, lng, use_amap=False)
    in5_noamap = [s for s in pool2.within(args.radius) if not str(s["code"]).startswith("AMAP-")]
    record("7. 需求一 · --no-amap 纯 MCP 模式可用", len(in5_noamap) > 0,
           "纯 MCP 门店池 %d 家 / %dm 内 %d 家（对比含高德 %d 家）"
           % (len(pool2), args.radius, len(in5_noamap), len(in5)),
           time.time() - t0, client.call_count - c0)

    # ---------- 需求一 WGS-84 换算 ----------
    wlat, wlng = geo.gcj02_to_wgs84(lat, lng)
    back = geo.wgs84_to_gcj02(wlat, wlng)
    drift = geo.haversine(lat, lng, back[0], back[1])
    offset = geo.haversine(lat, lng, wlat, wlng)
    record("8. WGS-84 ↔ GCJ-02 往返精度", drift < 1.0,
           "GCJ→WGS→GCJ 往返偏差 %.4f 米；两坐标系实际偏移 %.0f 米（这就是必须换算的原因）"
           % (drift, offset))

    # ---------- 需求二 ----------
    city = resolve_city(client, args.city)
    t0 = time.time(); c0 = client.call_count
    acts = party.list_activities(client)
    record("9. 需求二 · 可预约主题活动清单", len(acts) > 0,
           "共 %d 项，类目 %s"
           % (len(acts), "/".join(sorted({a["category"] for a in acts if a["category"]}))),
           time.time() - t0, client.call_count - c0)

    t0 = time.time(); c0 = client.call_count
    nd = party.nearest_stores_dates(client, city, args.spu, lat, lng, radius_m=args.radius, k=3)
    bookable = [r for r in nd["results"] if r["bookable"]]
    record("10. 需求二a · 5km 内最近 3 家门店可约日期",
           len(nd["results"]) > 0 and len(bookable) > 0,
           "活动支持门店 %d 家（全城）/ %d 家（%dm 内）；最近 3 家可约 %d 家，"
           "最早可约日期 %s"
           % (nd["activity_stores_total"], nd["in_radius"], args.radius, len(bookable),
              bookable[0]["dates"][0] if bookable else "-"),
           time.time() - t0, client.call_count - c0)

    if bookable:
        target_date = bookable[0]["dates"][0]
        t0 = time.time(); c0 = client.call_count
        od = party.sessions_for_date(client, city, args.spu, target_date, lat, lng,
                                     radius_m=args.radius, limit=2)
        has_left = any(s.get("left") is not None
                       for d in (od.get("detail") or []) for s in d["sessions"])
        record("11. 需求二b · 指定日期可约门店 + 余位",
               len(od["stores"]) > 0 and has_left,
               "%s 在 %dm 内可约门店 %d 家（已核 %d 家）；余位字段 leftNum 可读=%s"
               % (target_date, args.radius, len(od["stores"]), od["candidates"], has_left),
               time.time() - t0, client.call_count - c0)

        if od.get("detail"):
            st = od["detail"][0]
            sess = st["sessions"][0] if st["sessions"] else None
            if sess:
                record("12. 余位明细字段完整性",
                       all(k in sess for k in ("start", "end", "min", "max", "price", "left")),
                       "%s %s-%s 限%s-%s人 余位%s 价格%.2f元"
                       % (st["store"]["name"][:20], sess["start"], sess["end"],
                          sess["min"], sess["max"], sess["left"], (sess["price"] or 0) / 100.0))

    # 无可约分支
    t0 = time.time(); c0 = client.call_count
    far = party.stores_on_date(client, city, args.spu, "2026-12-31", lat, lng, radius_m=args.radius)
    record("13. 需求二 · 无可约门店时明确告知", far["empty"] is True,
           "%s / %dm 内可约门店 = %d → 触发『已逐店核实』明示分支"
           % (far["date"], args.radius, len(far["stores"])),
           time.time() - t0, client.call_count - c0)

    # ---------- CLI 参数位置回归 ----------
    # 这一组是补测：此前测试套件只验证了程序内的 use_amap=False，
    # 没有走命令行，因此漏掉了「--no-amap 写在子命令之后就报错」的真实 bug。
    from src.cli import build_parser
    parser = build_parser()
    cli_cases = [
        # (argv, 待检属性, 期望值, 说明)
        (["pool", "--city", "上海", "--no-amap"], "no_amap", True, "全局参数写在子命令【之后】"),
        (["--no-amap", "pool", "--city", "上海"], "no_amap", True, "全局参数写在子命令【之前】"),
        (["pool", "--city", "上海"], "no_amap", False, "未传时默认为 False"),
        (["check", "--city", "上海", "--keyword", "联名", "--no-amap"], "no_amap", True, "check 传 --no-amap"),
        (["party-list", "--no-amap"], "no_amap", True, "party-list 传 --no-amap"),
        (["party-dates", "--city", "上海", "--spu", "1830", "--no-amap"], "no_amap", True, "party-dates 传 --no-amap"),
        (["tools", "--json"], "json", True, "tools 传 --json"),
        (["tools"], "no_amap", False, "tools 不传时为 False"),
    ]
    cli_ok, cli_fail = 0, []
    for argv, attr, expected, desc in cli_cases:
        try:
            ns = parser.parse_args(argv)
        except SystemExit:
            cli_fail.append("%s（解析直接退出）" % desc)
            continue
        got = getattr(ns, attr, None)
        if bool(got) is expected:
            cli_ok += 1
        else:
            cli_fail.append("%s（%s 得到 %r，期望 %r）" % (desc, attr, got, expected))
    record("14. CLI 参数位置回归（曾漏测致 bug）", cli_ok == len(cli_cases),
           "%d/%d 组参数解析正确%s"
           % (cli_ok, len(cli_cases), "" if not cli_fail else "；失败：" + "；".join(cli_fail)))

    # ---------- 汇总 ----------
    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    total_calls = client.call_count
    print("=" * 80)
    print("汇总：%d/%d 通过   接口累计调用 %d 次（限流 600 次/分钟）"
          % (passed, total, total_calls))
    print("=" * 80)

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({
            "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "city": args.city, "base": {"lat": lat, "lng": lng},
            "radius": args.radius, "spu": args.spu, "item": args.item,
            "pool": meta["pool"], "total_calls": total_calls,
            "passed": passed, "total": total, "results": RESULTS,
        }, fh, ensure_ascii=False, indent=1)
    print("测试明细已写入 %s" % out)
    return 0 if passed == total else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
