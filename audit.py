from db import Database


def write_request_audit(
    database: Database,
    request_id: str,
    user_id: int | None,
    user_api_key_id: int | None,
    endpoint: str,
    selected_kimi_key_id: int | None,
    status_code: int,
    success: bool,
    latency_ms: int,
    client_ip: str,
    request_summary: str,
    response_summary: str,
    error_message: str,
) -> None:
    database.insert_request_log(
        request_id=request_id,
        user_id=user_id,
        user_api_key_id=user_api_key_id,
        endpoint=endpoint,
        selected_kimi_key_id=selected_kimi_key_id,
        status_code=status_code,
        success=success,
        latency_ms=latency_ms,
        client_ip=client_ip,
        request_summary=request_summary,
        response_summary=response_summary,
        error_message=error_message,
    )
