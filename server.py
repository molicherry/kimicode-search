import argparse
import json
import logging
import os
import platform
import socket
import time
import urllib.error
import urllib.request
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI
from mcp.server.fastmcp import Context, FastMCP

from audit import write_request_audit
from config import env_flag, env_int, load_app_config
from db import Database
from ratelimit import client_ip_from_context, enforce_rpm_limit
from webui import create_web_router


SERVER_NAME = "kimi-coding-mcp"
SERVER_VERSION = "0.3.0"
DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_USER_AGENT = "KimiCLI/1.24.0"
DEFAULT_MSH_PLATFORM = "kimi_cli"
DEFAULT_MSH_VERSION = "1.24.0"
DEFAULT_TRANSPORT = "stdio"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_STREAMABLE_HTTP_PATH = "/mcp"
REMOTE_API_KEY_HEADER = "x-kimi-api-key"
DEFAULT_LOG_DIR = "logs"
DEFAULT_LOG_FILE_NAME = "server.log"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 3
DEFAULT_LOG_PREVIEW_BYTES = 100
TEMPLATES_DIR = str(Path(__file__).parent / "templates")


class ToolCallError(Exception):
    pass


def normalize_api_key(value: str | None) -> str:
    if value is None:
        return ""

    stripped = value.strip()
    if not stripped:
        return ""

    if stripped.lower().startswith("bearer "):
        return stripped.split(" ", 1)[1].strip()

    return stripped


def mask_secret(value: str) -> str:
    if not value:
        return ""

    if len(value) <= 8:
        return "***"

    return f"{value[:4]}***{value[-4:]}"


def sanitize_log_value(value):
    if isinstance(value, dict):
        sanitized = {}
        for key, item in value.items():
            lowered_key = str(key).lower()
            if any(token in lowered_key for token in ("api_key", "authorization", "token", "secret", "password")):
                sanitized[key] = mask_secret(str(item))
            else:
                sanitized[key] = sanitize_log_value(item)
        return sanitized

    if isinstance(value, list):
        return [sanitize_log_value(item) for item in value]

    if isinstance(value, tuple):
        return tuple(sanitize_log_value(item) for item in value)

    return value


def preview_text_bytes(value: str, limit: int = DEFAULT_LOG_PREVIEW_BYTES) -> str:
    encoded = value.encode("utf-8", errors="replace")
    preview = encoded[:limit]
    suffix = "" if len(encoded) <= limit else "...(truncated)"
    return preview.decode("utf-8", errors="replace") + suffix


def payload_log_text(payload) -> str:
    sanitized_payload = sanitize_log_value(payload)
    serialized = json.dumps(sanitized_payload, ensure_ascii=False, sort_keys=True)
    return preview_text_bytes(serialized, env_int("KIMI_LOG_PREVIEW_BYTES", DEFAULT_LOG_PREVIEW_BYTES))


def response_log_text(text: str) -> str:
    return preview_text_bytes(text, env_int("KIMI_LOG_PREVIEW_BYTES", DEFAULT_LOG_PREVIEW_BYTES))


def build_logger() -> logging.Logger:
    logger = logging.getLogger(SERVER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, os.getenv("KIMI_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(), logging.INFO))
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    log_dir = Path(os.getenv("KIMI_LOG_DIR", DEFAULT_LOG_DIR))
    log_file = log_dir / DEFAULT_LOG_FILE_NAME

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_file,
            maxBytes=env_int("KIMI_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES),
            backupCount=env_int("KIMI_LOG_BACKUP_COUNT", DEFAULT_LOG_BACKUP_COUNT),
            encoding="utf-8",
        )
    except OSError:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


LOGGER = build_logger()


def default_device_id(device_name: str) -> str:
    kimi_device_id_path = Path.home() / ".kimi" / "device_id"

    try:
        device_id = kimi_device_id_path.read_text(encoding="utf-8").strip()
        if device_id:
            return device_id
    except OSError:
        pass

    return uuid.uuid5(uuid.NAMESPACE_DNS, device_name).hex


def resolve_request_api_key(ctx: Context | None) -> str:
    if ctx is None:
        return ""

    request = getattr(ctx.request_context, "request", None)
    if request is None:
        return ""

    header_value = normalize_api_key(request.headers.get(REMOTE_API_KEY_HEADER))
    if header_value:
        return header_value

    authorization = request.headers.get("authorization", "")
    candidate = normalize_api_key(authorization)
    if candidate.startswith("sk-"):
        return candidate

    return ""


def format_tool_text(status_code: int, content_type: str, body: bytes) -> str:
    charset = "utf-8"
    if "charset=" in content_type:
        charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()

    text = body.decode(charset, errors="replace")
    if "application/json" in content_type:
        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass

    if status_code >= 400:
        return f"HTTP {status_code}\n{text}"
    return text


class KimiCodingClient:
    def __init__(self, api_key: str | None = None, allow_env_fallback: bool = True):
        env_api_key = normalize_api_key(os.getenv("KIMI_API_KEY", "")) if allow_env_fallback else ""
        self.api_key = normalize_api_key(api_key) or env_api_key
        self.base_url = os.getenv("KIMI_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.user_agent = os.getenv("KIMI_USER_AGENT", DEFAULT_USER_AGENT)
        self.msh_platform = os.getenv("KIMI_MSH_PLATFORM", DEFAULT_MSH_PLATFORM)
        self.msh_version = os.getenv("KIMI_MSH_VERSION", DEFAULT_MSH_VERSION)
        self.device_name = os.getenv("KIMI_DEVICE_NAME", socket.gethostname() or "unknown-host")
        self.device_model = os.getenv(
            "KIMI_DEVICE_MODEL",
            f"{platform.system()} {platform.machine()}".strip() or "unknown-device",
        )
        self.os_version = os.getenv(
            "KIMI_OS_VERSION",
            platform.version() or platform.release() or "unknown-os",
        )
        self.device_id = os.getenv(
            "KIMI_DEVICE_ID",
            default_device_id(self.device_name),
        )

    def ensure_ready(self):
        if not self.api_key:
            raise ValueError(
                "未提供可用的 Kimi API key。兼容模式请在请求头传 X-Kimi-Api-Key，托管模式请传平台用户 API。"
            )

    def _headers(self, accept: str):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": accept,
            "User-Agent": self.user_agent,
            "X-Msh-Tool-Call-Id": f"tool_{uuid.uuid4().hex}",
            "X-Msh-Platform": self.msh_platform,
            "X-Msh-Version": self.msh_version,
            "X-Msh-Device-Name": self.device_name,
            "X-Msh-Device-Model": self.device_model,
            "X-Msh-Os-Version": self.os_version,
            "X-Msh-Device-Id": self.device_id,
        }

    def post(self, endpoint: str, payload, accept: str, timeout: int):
        self.ensure_ready()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        started_at = time.perf_counter()

        LOGGER.info(
            "kimi_request endpoint=%s url=%s accept=%s timeout=%s payload=%s",
            endpoint,
            url,
            accept,
            timeout,
            payload_log_text(payload),
        )

        request = urllib.request.Request(
            url=url,
            data=data,
            headers=self._headers(accept),
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "text/plain; charset=utf-8")
                body = response.read()
                formatted_text = format_tool_text(response.status, content_type, body)
                elapsed_ms = int((time.perf_counter() - started_at) * 1000)
                LOGGER.info(
                    "kimi_response endpoint=%s status=%s elapsed_ms=%s result_preview=%s",
                    endpoint,
                    response.status,
                    elapsed_ms,
                    response_log_text(formatted_text),
                )
                return response.status, formatted_text, elapsed_ms
        except urllib.error.HTTPError as exc:
            content_type = exc.headers.get("Content-Type", "text/plain; charset=utf-8")
            body = exc.read()
            formatted_text = format_tool_text(exc.code, content_type, body)
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            LOGGER.warning(
                "kimi_response endpoint=%s status=%s elapsed_ms=%s result_preview=%s",
                endpoint,
                exc.code,
                elapsed_ms,
                response_log_text(formatted_text),
            )
            raise ToolCallError(formatted_text) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            LOGGER.warning(
                "kimi_response endpoint=%s status=network_error elapsed_ms=%s result_preview=%s",
                endpoint,
                elapsed_ms,
                response_log_text(f"请求失败: {reason}"),
            )
            raise ToolCallError(f"请求失败: {reason}") from exc

    def search(
        self,
        text_query: str,
        limit: int = 10,
        enable_page_crawling: bool = False,
        timeout_seconds: int = 30,
    ):
        payload = {
            "text_query": text_query,
            "limit": int(limit),
            "enable_page_crawling": bool(enable_page_crawling),
            "timeout_seconds": int(timeout_seconds),
        }
        request_timeout = max(payload["timeout_seconds"] + 10, 30)
        return self.post("search", payload, "*/*", request_timeout)

    def fetch(self, url: str):
        payload = {"url": url}
        return self.post("fetch", payload, "text/markdown", 30)


def build_managed_runtime(config):
    database = Database(config.db_path)
    database.initialize()
    database.bootstrap_admin(config.bootstrap_admin_username, config.bootstrap_admin_password)
    return database


def resolve_managed_access(ctx: Context, endpoint: str, database: Database):
    user_api_key = resolve_request_api_key(ctx)
    if not user_api_key:
        raise ValueError("托管模式必须在请求头传平台用户 API。")

    validated_api_key = database.validate_user_api_key(user_api_key)
    if validated_api_key is None:
        raise ValueError("平台用户 API 无效、已禁用或已过期。")

    enforce_rpm_limit(database, validated_api_key, endpoint)
    upstream = database.select_upstream_for_user_api_key(int(validated_api_key["id"]))
    if upstream is None:
        raise ValueError("当前没有任何已启用的实际调用密钥可供使用。")
    return validated_api_key, upstream


def create_server(args, config, database: Database | None = None) -> FastMCP:
    instructions = (
        "Use kimi_search and kimi_fetch to access Kimi coding search/fetch APIs. "
        "Legacy mode accepts downstream Kimi API keys via X-Kimi-Api-Key; managed mode accepts platform-issued user API keys only."
    )
    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=instructions,
        host=args.host,
        port=args.port,
        streamable_http_path=args.streamable_http_path,
        stateless_http=args.stateless_http,
        json_response=args.json_response,
    )

    def execute_managed_call(endpoint: str, payload: dict, action):
        request_id = uuid.uuid4().hex
        if database is None:
            raise ValueError("托管模式数据库未初始化。")
        validated_api_key, upstream = resolve_managed_access(payload["ctx"], endpoint, database)
        client = KimiCodingClient(api_key=upstream["api_key"], allow_env_fallback=False)
        status_code = 500
        formatted_text = ""
        elapsed_ms = 0
        error_message = ""
        try:
            status_code, formatted_text, elapsed_ms = action(client)
            write_request_audit(
                database=database,
                request_id=request_id,
                user_id=int(validated_api_key["owner_user_id"]),
                user_api_key_id=int(validated_api_key["id"]),
                endpoint=endpoint,
                selected_kimi_key_id=int(upstream["kimi_key_id"]),
                status_code=status_code,
                success=True,
                latency_ms=elapsed_ms,
                client_ip=client_ip_from_context(payload["ctx"]),
                request_summary=payload_log_text(payload["log_payload"]),
                response_summary=response_log_text(formatted_text),
                error_message="",
            )
            return formatted_text
        except Exception as exc:
            if elapsed_ms == 0:
                elapsed_ms = 0
            error_message = str(exc)
            write_request_audit(
                database=database,
                request_id=request_id,
                user_id=int(validated_api_key["owner_user_id"]),
                user_api_key_id=int(validated_api_key["id"]),
                endpoint=endpoint,
                selected_kimi_key_id=int(upstream["kimi_key_id"]),
                status_code=status_code,
                success=False,
                latency_ms=elapsed_ms,
                client_ip=client_ip_from_context(payload["ctx"]),
                request_summary=payload_log_text(payload["log_payload"]),
                response_summary=response_log_text(formatted_text),
                error_message=error_message,
            )
            raise

    @mcp.tool(
        name="kimi_search",
        description="调用 Kimi 的 /coding/v1/search 接口，根据文本查询执行搜索。",
    )
    def kimi_search(
        text_query: str,
        ctx: Context,
        limit: int = 10,
        enable_page_crawling: bool = False,
        timeout_seconds: int = 30,
    ) -> str:
        cleaned_query = text_query.strip()
        if not cleaned_query:
            raise ValueError("kimi_search 需要非空字符串参数 text_query。")

        if config.webui_enabled:
            return execute_managed_call(
                "search",
                {
                    "ctx": ctx,
                    "log_payload": {
                        "text_query": cleaned_query,
                        "limit": limit,
                        "enable_page_crawling": enable_page_crawling,
                        "timeout_seconds": timeout_seconds,
                    },
                },
                lambda client: client.search(
                    text_query=cleaned_query,
                    limit=limit,
                    enable_page_crawling=enable_page_crawling,
                    timeout_seconds=timeout_seconds,
                ),
            )

        client = KimiCodingClient(api_key=resolve_request_api_key(ctx), allow_env_fallback=True)
        _, formatted_text, _ = client.search(
            text_query=cleaned_query,
            limit=limit,
            enable_page_crawling=enable_page_crawling,
            timeout_seconds=timeout_seconds,
        )
        return formatted_text

    @mcp.tool(
        name="kimi_fetch",
        description="调用 Kimi 的 /coding/v1/fetch 接口，根据 URL 抓取结果。",
    )
    def kimi_fetch(url: str, ctx: Context) -> str:
        cleaned_url = url.strip()
        if not cleaned_url:
            raise ValueError("kimi_fetch 需要非空字符串参数 url。")

        if config.webui_enabled:
            return execute_managed_call(
                "fetch",
                {"ctx": ctx, "log_payload": {"url": cleaned_url}},
                lambda client: client.fetch(cleaned_url),
            )

        client = KimiCodingClient(api_key=resolve_request_api_key(ctx), allow_env_fallback=True)
        _, formatted_text, _ = client.fetch(cleaned_url)
        return formatted_text

    return mcp


def create_managed_http_app(args, config):
    database = build_managed_runtime(config)
    mcp = create_server(args, config, database)
    mcp_app = mcp.streamable_http_app()
    app = FastAPI(lifespan=mcp_app.router.lifespan_context)
    app.include_router(create_web_router(config, database, TEMPLATES_DIR))

    @app.get("/")
    def root_redirect():
        return {"name": SERVER_NAME, "version": SERVER_VERSION, "webui": True, "mcp_path": args.streamable_http_path}

    @app.get("/health")
    def health_check():
        return {"status": "ok", "webui": True}

    app.mount("/", mcp_app)
    return app


def parse_args():
    parser = argparse.ArgumentParser(description="Kimi Coding MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default=os.getenv("MCP_TRANSPORT", DEFAULT_TRANSPORT),
        help="MCP transport mode. 默认保持 stdio，本地兼容；远程部署请使用 streamable-http。",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", DEFAULT_HOST),
        help="HTTP transport bind host.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT", str(DEFAULT_PORT))),
        help="HTTP transport bind port.",
    )
    parser.add_argument(
        "--streamable-http-path",
        default=os.getenv("MCP_STREAMABLE_HTTP_PATH", DEFAULT_STREAMABLE_HTTP_PATH),
        help="Path for streamable HTTP endpoint.",
    )
    parser.add_argument(
        "--stateless-http",
        action="store_true",
        default=env_flag("MCP_STATELESS_HTTP", False),
        help="Enable stateless HTTP mode for easier horizontal scaling.",
    )
    parser.add_argument(
        "--json-response",
        action="store_true",
        default=env_flag("MCP_JSON_RESPONSE", False),
        help="Prefer plain JSON responses when the client supports them.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_app_config()
    LOGGER.info(
        "server_start transport=%s host=%s port=%s streamable_http_path=%s log_dir=%s webui_enabled=%s",
        args.transport,
        args.host,
        args.port,
        args.streamable_http_path,
        os.getenv("KIMI_LOG_DIR", DEFAULT_LOG_DIR),
        config.webui_enabled,
    )

    if config.webui_enabled and args.transport == "streamable-http":
        import uvicorn

        app = create_managed_http_app(args, config)
        uvicorn.run(app, host=args.host, port=args.port)
        return

    mcp = create_server(args, config, None)
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
