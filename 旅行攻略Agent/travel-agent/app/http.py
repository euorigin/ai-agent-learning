"""极简 HTTP 客户端：只依赖标准库，带超时、重试与错误上下文。

不引入 requests/httpx，是为了让工程在受限网络与干净环境里都能直接跑起来。
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "travel-guide-agent/2.0 (+https://example.local)"


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        self.status = status
        self.url = url
        self.body = body[:400]
        super().__init__(f"HTTP {status} @ {url} :: {self.body}")


def request_json(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    form_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    retries: int = 2,
) -> Any:
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"

    data: bytes | None = None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json; charset=utf-8"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if headers:
        hdrs.update(headers)

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
            # 4xx 是请求本身的问题，重试无意义（429 除外）
            if exc.code != 429 and 400 <= exc.code < 500:
                raise HttpError(exc.code, url, body) from exc
            last_err = HttpError(exc.code, url, body)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last_err = exc
        except json.JSONDecodeError as exc:
            raise HttpError(0, url, f"返回内容不是合法 JSON: {exc}") from exc
        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))

    raise HttpError(0, url, f"请求失败：{type(last_err).__name__}: {last_err}")
