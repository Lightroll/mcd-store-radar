"""地理计算工具 —— 纯标准库。

为什么必须自己算距离：
    麦当劳 MCP 的 query-party-store 虽然接受 latitude/longitude 入参，
    但实测其返回的 distance 字段不随入参变化（传城市中心 / 传人民广场 /
    传北京坐标，同一门店返回的 distance 恒为 415m）。因此半径筛选必须
    由本模块用门店真实坐标与用户基准位置自行计算。

坐标系：
    - query-party-store 返回的门店经纬度属于 GCJ-02（火星坐标）体系
    - 浏览器 navigator.geolocation / GPS 设备给出的是 WGS-84
    - 两者在中国境内相差数百米，混用会导致半径判定错位
"""

import math
import re

EARTH_RADIUS_M = 6371000.0
_PI = math.pi / 180.0

# GCJ-02 加偏参数
_A = 6378245.0
_EE = 0.00669342162296594323


def haversine(lat1, lng1, lat2, lng2):
    """两点的球面直线距离，单位米。"""
    dlat = (lat2 - lat1) * _PI
    dlng = (lng2 - lng1) * _PI
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1 * _PI) * math.cos(lat2 * _PI) * math.sin(dlng / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _out_of_china(lat, lng):
    """粗略判断是否在中国境外（境外不做加偏）。"""
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x, y):
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x, y):
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat, lng):
    """WGS-84 -> GCJ-02。"""
    if _out_of_china(lat, lng):
        return lat, lng
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lng + dlng


def gcj02_to_wgs84(lat, lng):
    """GCJ-02 -> WGS-84（迭代反解，精度 < 1e-6 度）。"""
    if _out_of_china(lat, lng):
        return lat, lng
    wlat, wlng = lat, lng
    for _ in range(8):
        glat, glng = wgs84_to_gcj02(wlat, wlng)
        wlat += lat - glat
        wlng += lng - glng
    return wlat, wlng


def fmt_distance(meters):
    """距离友好显示：<1km 显示米（取整到 10m），否则显示公里保留一位小数。"""
    if meters is None:
        return "未知"
    if meters < 1000:
        return "%dm" % (int(round(meters / 10.0)) * 10)
    return "%.1fkm" % (meters / 1000.0)


def normalize_name(text):
    """门店名归一化，用于门店匹配。

    去掉「麦当劳」「甜品站」前后缀、括号、空白，只保留具辨识度的主体。
    """
    if not text:
        return ""
    s = text
    s = s.replace("麦当劳", "").replace("McDonald's", "").replace("McDonalds", "")
    s = s.replace("餐厅", "").replace("甜品站", "").replace("得来速", "")
    s = "".join(ch for ch in s if ch not in "（）()【】[] 　\t")
    return s.strip()


def store_keyword(name):
    """从门店名里抽出适合做检索关键词的片段。

    实测：把门店名的核心词交给 query-nearby-stores，目标门店会排在返回结果
    的第一位 —— 这是把「高德 POI」映射到「MCP storeCode」的关键手段。

    处理规则：
        · 优先取括号内的部分（如「麦当劳(雅居乐国际广场餐厅)」→ 雅居乐国际广场）
        · 去掉 麦当劳 / 餐厅 / 店 / 分店 等通用词
        · 去掉「…号口街铺」「地铁站」等描述性后缀
    """
    if not name:
        return ""
    s = name.replace("McDonald's", "").replace("McDonalds", "")
    inner = re.findall(r"[（(]([^）)]+)[）)]", s)
    if inner:
        s = max(inner, key=len)
    s = s.replace("麦当劳", "").replace("甜品站", "")
    for junk in ["餐厅", "分店", "店", "街铺", "铺位", "商铺"]:
        s = s.replace(junk, "")
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[0-9A-Za-z]+号口.*$", "", s)
    s = "".join(ch for ch in s if ch not in "（）()【】[] 　\t")
    return s.strip()


def is_kiosk(name):
    """甜品站/小食亭等非门店业态 —— 这类点不参与门店在售查询。"""
    return bool(name) and ("甜品站" in name or "小食亭" in name or "冰品站" in name)



def longest_common_substring(a, b):
    """最长公共子串长度（中文门店名匹配比 Jaccard 更稳）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def name_similar(a, b):
    """门店名相似度（0~1）。

    中文门店名的难点：高德叫「雅居乐国际广场」、MCP 叫「上海雅居乐」，
    二元组 Jaccard 只有 0.25，但二者显然同店。因此这里取
    「Jaccard」与「最长公共子串占比」的较大值，后者能识别这种包含关系。
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 0.9
    ga = {na[i:i + 2] for i in range(len(na) - 1)}
    gb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    jac = (len(ga & gb) / float(len(ga | gb))) if (ga and gb) else 0.0
    lcs = longest_common_substring(na, nb)
    ratio = lcs / float(max(1, min(len(na), len(nb))))
    # 公共子串至少要 2 个字，且占较短名字的一半以上，才认为同店
    contain = ratio if (lcs >= 2 and ratio >= 0.5) else 0.0
    return max(jac, contain)

