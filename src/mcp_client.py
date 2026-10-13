"""麦当劳 MCP 客户端 —— 纯标准库，无第三方依赖。

传输：Streamable HTTP + JSON-RPC 2.0
鉴权：Authorization: Bearer <MCD_MCP_TOKEN>

Token 解析优先级：
  1. 显式传入（CLI --token）
  2. 环境变量 MCD_MCP_TOKEN
  3. 本机 WorkBuddy 配置 ~/.workbuddy/mcp.json 中 mcd-mcp 条目

注意：本模块从不打印 Token，也不写入任何日志或输出文件。
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid

ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "mcd-store-radar"
CLIENT_VERSION = "1.0.0"

# 限流：官方说明为 600 次/分钟，超限返回 429
MAX_PER_MINUTE = 600


class McpError(RuntimeError):
    """MCP 调用失败（网络 / 鉴权 / 业务错误）。"""

    def __init__(self, message, code=None, tool=None):
        super().__init__(message)
        self.code = code
        self.tool = tool


def _read_token_from_workbuddy_config():
    """从本机 ~/.workbuddy/mcp.json 读取 mcd-mcp 的 Bearer Token。

    仅读取 Authorization 字段，不做任何落盘或打印。
    """
    path = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None
    block = re.search(r'"mcd-mcp"\s*:\s*\{(.*?)\}', raw, re.S)
    if not block:
        return None
    token = re.search(r'Bearer\s+([^\s"]+)', block.group(1))
    return token.group(1) if token else None


def resolve_token(explicit=None):
    """按优先级解析 Token。"""
    if explicit:
        return explicit
    env = os.environ.get("MCD_MCP_TOKEN")
    if env:
        return env.strip()
    return _read_token_from_workbuddy_config()


class McpClient:
    """极简 Streamable HTTP JSON-RPC 客户端。"""

    def __init__(self, token=None, endpoint=ENDPOINT, timeout=45, proxy=None, verbose=False):
        self.token = resolve_token(token)
        if not self.token:
            raise McpError(
                "未找到麦当劳 MCP Token。请设置环境变量 MCD_MCP_TOKEN，"
                "或先在本机 WorkBuddy 中配置 mcd-mcp 连接器。"
            )
        self.endpoint = endpoint
        self.timeout = timeout
        self.verbose = verbose
        self.call_count = 0
        self._session_id = None

        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            # 未显式指定代理时，主动忽略环境变量里的代理，避免"假代理"导致 502
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    # ---------- 传输层 ----------

    def _post(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            body["id"] = str(uuid.uuid4())

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + self.token,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        req = urllib.request.Request(
            self.endpoint, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            resp = self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise McpError("触发限流（600 次/分钟），请降低请求频率后重试", code=429) from None
            if exc.code == 401:
                raise McpError("MCP Token 无效或已过期，请重新申请", code=401) from None
            raise McpError("HTTP %s：%s" % (exc.code, exc.reason), code=exc.code) from None
        except urllib.error.URLError as exc:
            raise McpError("网络不可达：%s" % exc.reason) from None

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid

        text = resp.read().decode("utf-8", "ignore")
        if notify:
            return None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    return json.loads(line[5:].strip())
                except ValueError:
                    continue
        try:
            return json.loads(text)
        except ValueError:
            return {"raw": text[:400]}

    def initialize(self):
        """建立会话（服务端为无状态实现，通常不返回 session id）。"""
        self._post(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self._post("notifications/initialized", {}, notify=True)
        return self

    # ---------- 业务层 ----------

    def call(self, tool, arguments=None, retries=2):
        """调用一个 MCP 工具，返回其 data 部分（dict / list）。

        官方工具返回的 text 内容 = 「人读前缀 + JSON + 尾部说明」，
        因此必须用 raw_decode 而不是 json.loads，否则尾部文字会导致解析失败。
        """
        arguments = arguments or {}
        last_err = None
        for attempt in range(retries + 1):
            try:
                result = self._post("tools/call", {"name": tool, "arguments": arguments})
                break
            except McpError as exc:
                last_err = exc
                if exc.code == 429 and attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise
        else:
            raise last_err

        self.call_count += 1
        if not result or "result" not in result:
            msg = ""
            if isinstance(result, dict):
                msg = str(result.get("error") or result.get("raw") or "")[:200]
            raise McpError("工具 %s 无有效返回：%s" % (tool, msg), tool=tool)

        payload = None
        for chunk in result["result"].get("content", []):
            if chunk.get("type") == "text":
                text = chunk["text"]
                start = text.find('{"success"')
                if start < 0:
                    start = text.find("{")
                if start < 0:
                    continue
                try:
                    payload, _ = json.JSONDecoder().raw_decode(text[start:])
                except ValueError:
                    continue
                break

        if payload is None:
            raise McpError("工具 %s 返回无法解析" % tool, tool=tool)

        if payload.get("success") is False:
            raise McpError(
                "%s 返回失败：%s" % (tool, payload.get("message") or payload.get("code")),
                code=payload.get("code"),
                tool=tool,
            )
        return payload.get("data")

    def tool_names(self):
        """列出服务端暴露的全部工具名（用于自检与文档核对）。"""
        result = self._post("tools/list", {})
        tools = ((result or {}).get("result") or {}).get("tools") or []
        return sorted(t.get("name") for t in tools if t.get("name"))
