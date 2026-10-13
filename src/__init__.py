"""mcd-store-radar · 麦麦门店雷达

基于麦当劳中国官方 MCP 的门店可得性检索 Skill：
  · 需求一：联动活动（实物周边 / 联名商品）在半径内哪些门店在售，
            5km → 10km → 用户确认后继续扩大。
  · 需求二：近期可预约主题活动，半径内最近门店的可约日期，
            以及指定日期下半径内哪些门店可约（含真实余位 leftNum）。
"""

__version__ = "1.0.0"
__all__ = ["mcp_client", "geo", "storepool", "collab", "party", "report"]
