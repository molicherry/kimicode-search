import time

from mcp.server.fastmcp import Context


def enforce_rpm_limit(database, validated_api_key: dict, endpoint: str) -> int:
    if endpoint == "search":
        limit = int(validated_api_key["search_rpm"])
    else:
        limit = int(validated_api_key["fetch_rpm"])

    window_start = int(time.time()) // 60
    request_count = database.increment_rate_counter(
        user_api_key_id=int(validated_api_key["id"]),
        endpoint=endpoint,
        window_start=window_start,
    )
    if request_count > limit:
        raise ValueError(f"{endpoint} RPM 超限：当前限制为 {limit} RPM。")
    return limit


def client_ip_from_context(ctx: Context | None) -> str:
    if ctx is None:
        return ""
    request = getattr(ctx.request_context, "request", None)
    if request is None:
        return ""
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    client = getattr(request, "client", None)
    if client is None:
        return ""
    host = getattr(client, "host", "")
    return host or ""
