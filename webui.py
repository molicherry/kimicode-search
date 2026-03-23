from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import generate_session_id, generate_user_api_key, hash_password, session_expiry, verify_password


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _query_str(request: Request, key: str, default: str = "") -> str:
    return request.query_params.get(key, default).strip()


def _query_int(request: Request, key: str, default: int, minimum: int = 1) -> int:
    raw_value = request.query_params.get(key)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return max(value, minimum)


def create_web_router(config, database, templates_dir: str) -> APIRouter:
    router = APIRouter(prefix="/web")
    templates = Jinja2Templates(directory=templates_dir)

    def checked_user(user):
        if user is None:
            raise ValueError("用户会话不存在。")
        return user

    def current_user(request: Request):
        session_id = request.cookies.get(config.session_cookie_name, "")
        if not session_id:
            return None
        return database.get_session(session_id)

    def require_user(request: Request):
        user = current_user(request)
        if user is None:
            return None, _redirect("/web/login")
        return user, None

    def require_admin(request: Request):
        user, response = require_user(request)
        if response is not None:
            return None, response
        existing_user = checked_user(user)
        if existing_user["role"] != "admin":
            return None, HTMLResponse("Forbidden", status_code=403)
        return existing_user, None

    def render(request: Request, template_name: str, context: dict, status_code: int = 200):
        base_context = {
            "request": request,
            "current_user": current_user(request),
            "message": request.query_params.get("message", ""),
            "error": request.query_params.get("error", ""),
        }
        base_context.update(context)
        return templates.TemplateResponse(template_name, base_context, status_code=status_code)

    def pagination_window(paginated: dict) -> list[int]:
        total_pages = int(paginated["total_pages"])
        current_page = int(paginated["page"])
        if total_pages <= 7:
            return list(range(1, total_pages + 1))
        start = max(current_page - 2, 1)
        end = min(start + 4, total_pages)
        start = max(end - 4, 1)
        return list(range(start, end + 1))

    def pagination_context(paginated: dict) -> dict:
        return {
            "page": paginated["page"],
            "page_size": paginated["page_size"],
            "total": paginated["total"],
            "total_pages": paginated["total_pages"],
            "page_numbers": pagination_window(paginated),
        }

    def validate_api_form(name: str, search_rpm: int, fetch_rpm: int) -> str:
        if not name.strip():
            return "请填写名称。"
        if search_rpm <= 0 or fetch_rpm <= 0:
            return "请填写大于 0 的 RPM 值。"
        return ""

    def admin_api_keys_context(request: Request) -> dict:
        filters = {
            "search": _query_str(request, "search"),
            "status": _query_str(request, "status"),
            "page": _query_int(request, "page", 1),
        }
        paginated = database.list_user_api_keys_paginated(
            search=filters["search"],
            status=filters["status"],
            page=filters["page"],
            page_size=10,
        )
        keys = paginated["items"]
        return {
            "api_keys": keys,
            "users": database.list_users(),
            "new_key_value": "",
            "created_key_owner": "",
            "filters": filters,
            "pagination": pagination_context(paginated),
        }

    def admin_kimi_context(request: Request) -> dict:
        filters = {
            "search": _query_str(request, "search"),
            "status": _query_str(request, "status"),
            "page": _query_int(request, "page", 1),
        }
        paginated = database.list_kimi_api_keys_paginated(
            search=filters["search"],
            status=filters["status"],
            page=filters["page"],
            page_size=10,
        )
        kimi_keys = paginated["items"]
        return {
            "kimi_keys": kimi_keys,
            "new_kimi_key_value": "",
            "filters": filters,
            "pagination": pagination_context(paginated),
        }

    def user_api_keys_context(request: Request, user_id: int) -> dict:
        filters = {
            "search": _query_str(request, "search"),
            "status": _query_str(request, "status"),
            "page": _query_int(request, "page", 1),
        }
        paginated = database.list_user_api_keys_paginated(
            user_id=user_id,
            search=filters["search"],
            status=filters["status"],
            page=filters["page"],
            page_size=10,
        )
        return {
            "api_keys": paginated["items"],
            "usage": database.get_usage_summary_for_user(user_id),
            "new_key_value": "",
            "filters": filters,
            "pagination": pagination_context(paginated),
        }

    @router.get("/", response_class=HTMLResponse)
    def web_home(request: Request):
        user = current_user(request)
        if user is None:
            return _redirect("/web/login")
        if user["role"] == "admin":
            return _redirect("/web/admin/users")
        return _redirect("/web/me/api-keys")

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return render(request, "login.html", {"title": "登录控制台"})

    @router.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        user = database.get_user_by_username(username.strip())
        if user is None or not user["is_active"] or not verify_password(password, user["password_hash"]):
            return _redirect("/web/login?error=用户名或密码错误")

        session_id = generate_session_id()
        database.create_session(
            session_id=session_id,
            user_id=int(user["id"]),
            expires_at=session_expiry(config.session_ttl_seconds).isoformat(),
            ip_address=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
        database.touch_last_login(int(user["id"]))
        response = _redirect("/web/")
        response.set_cookie(
            config.session_cookie_name,
            session_id,
            httponly=True,
            samesite="lax",
            max_age=config.session_ttl_seconds,
        )
        return response

    @router.post("/logout")
    def logout(request: Request):
        session_id = request.cookies.get(config.session_cookie_name, "")
        if session_id:
            database.delete_session(session_id)
        response = _redirect("/web/login?message=已退出登录")
        response.delete_cookie(config.session_cookie_name)
        return response

    @router.get("/profile/password", response_class=HTMLResponse)
    def password_page(request: Request):
        user, response = require_user(request)
        if response is not None:
            return response
        return render(request, "password.html", {"target_user": user, "is_self": True})

    @router.post("/profile/password")
    async def password_submit(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
    ):
        user, response = require_user(request)
        if response is not None:
            return response
        existing_user = checked_user(user)
        if not verify_password(current_password, existing_user["password_hash"]):
            return _redirect("/web/profile/password?error=当前密码错误")
        database.update_user_password(int(existing_user["id"]), hash_password(new_password))
        return _redirect("/web/profile/password?message=密码已更新")

    @router.get("/admin/users", response_class=HTMLResponse)
    def admin_users(request: Request):
        user, response = require_admin(request)
        if response is not None:
            return response
        filters = {
            "search": _query_str(request, "search"),
            "role": _query_str(request, "role"),
            "status": _query_str(request, "status"),
            "page": _query_int(request, "page", 1),
        }
        paginated = database.list_users_paginated(
            search=filters["search"],
            role=filters["role"],
            status=filters["status"],
            page=filters["page"],
            page_size=10,
        )
        return render(
            request,
            "admin_users.html",
            {
                "users": paginated["items"],
                "viewer": user,
                "filters": filters,
                "pagination": pagination_context(paginated),
                "title": "用户管理",
            },
        )

    @router.post("/admin/users")
    async def admin_create_user(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        role: str = Form(...),
    ):
        _, response = require_admin(request)
        if response is not None:
            return response
        if not username.strip():
            return _redirect("/web/admin/users?error=请填写用户名。")
        if len(password.strip()) < 6:
            return _redirect("/web/admin/users?error=请填写至少 6 位的密码。")
        if role not in {"admin", "user"}:
            return _redirect("/web/admin/users?error=请选择合法角色。")
        if database.get_user_by_username(username.strip()):
            return _redirect("/web/admin/users?error=该用户名已存在，请更换。")
        database.create_user(username.strip(), hash_password(password), role)
        return _redirect("/web/admin/users?message=用户已创建。")

    @router.post("/admin/users/{user_id}/toggle")
    async def admin_toggle_user(request: Request, user_id: int):
        _, response = require_admin(request)
        if response is not None:
            return response
        target = database.get_user_by_id(user_id)
        if target is None:
            return _redirect("/web/admin/users?error=未找到要操作的用户。")
        database.set_user_active(user_id, not bool(target["is_active"]))
        return _redirect("/web/admin/users?message=用户状态已更新。")

    @router.post("/admin/users/{user_id}/password")
    async def admin_reset_password(request: Request, user_id: int, new_password: str = Form(...)):
        _, response = require_admin(request)
        if response is not None:
            return response
        if database.get_user_by_id(user_id) is None:
            return _redirect("/web/admin/users?error=未找到要重置密码的用户。")
        if len(new_password.strip()) < 6:
            return _redirect("/web/admin/users?error=请填写至少 6 位的新密码。")
        database.update_user_password(user_id, hash_password(new_password))
        return _redirect("/web/admin/users?message=密码已重置。")

    @router.get("/admin/api-keys", response_class=HTMLResponse)
    def admin_api_keys(request: Request):
        _, response = require_admin(request)
        if response is not None:
            return response
        return render(request, "admin_api_keys.html", {**admin_api_keys_context(request), "title": "用户 API 管理"})

    @router.post("/admin/api-keys")
    async def admin_create_api_key(
        request: Request,
        user_id: int = Form(...),
        name: str = Form(...),
        search_rpm: int = Form(...),
        fetch_rpm: int = Form(...),
        expires_at: str = Form(""),
        group_ids: list[int] = Form(default=[]),
    ):
        _, response = require_admin(request)
        if response is not None:
            return response
        validation_error = validate_api_form(name, search_rpm, fetch_rpm)
        if validation_error:
            return _redirect(f"/web/admin/api-keys?error={validation_error}")
        raw_key = generate_user_api_key()
        database.create_user_api_key(
            user_id=user_id,
            name=name.strip() or "default",
            raw_key=raw_key,
            search_rpm=search_rpm,
            fetch_rpm=fetch_rpm,
            expires_at=expires_at.strip() or None,
        )
        context = admin_api_keys_context(request)
        owner = database.get_user_by_id(user_id)
        context["new_key_value"] = raw_key
        context["created_key_owner"] = owner["username"] if owner else ""
        context["message"] = "新用户 API 已创建，请立即复制保存。"
        context["title"] = "用户 API 管理"
        return render(request, "admin_api_keys.html", context)

    @router.post("/admin/api-keys/{key_id}/toggle")
    async def admin_toggle_api_key(request: Request, key_id: int):
        _, response = require_admin(request)
        if response is not None:
            return response
        target = database.get_user_api_key(key_id)
        if target is None:
            return _redirect("/web/admin/api-keys?error=未找到要操作的用户 API。")
        database.set_user_api_key_active(key_id, not bool(target["is_active"]))
        return _redirect("/web/admin/api-keys?message=用户 API 状态已更新。")

    @router.post("/admin/api-keys/{key_id}/delete")
    async def admin_delete_api_key(request: Request, key_id: int):
        _, response = require_admin(request)
        if response is not None:
            return response
        database.delete_user_api_key(key_id)
        return _redirect("/web/admin/api-keys?message=用户 API 已删除。")

    @router.get("/admin/kimi", response_class=HTMLResponse)
    def admin_kimi(request: Request):
        _, response = require_admin(request)
        if response is not None:
            return response
        return render(request, "admin_kimi.html", {**admin_kimi_context(request), "title": "Kimi密钥状态配置"})

    @router.post("/admin/kimi-keys")
    async def admin_create_kimi_key(
        request: Request,
        name: str = Form(...),
        api_key: str = Form(...),
    ):
        _, response = require_admin(request)
        if response is not None:
            return response
        if not name.strip() or not api_key.strip():
            return _redirect("/web/admin/kimi?error=请填写名称和 Kimi 密钥。")
        database.create_kimi_api_key(name.strip(), api_key.strip())
        context = admin_kimi_context(request)
        context["new_kimi_key_value"] = api_key.strip()
        context["message"] = "Kimi 密钥已保存，请立即复制保存。"
        context["title"] = "Kimi密钥状态配置"
        return render(request, "admin_kimi.html", context)

    @router.post("/admin/kimi-keys/{key_id}/toggle")
    async def admin_toggle_kimi_key(request: Request, key_id: int):
        _, response = require_admin(request)
        if response is not None:
            return response
        keys = database.list_kimi_api_keys()
        target = next((item for item in keys if int(item["id"]) == key_id), None)
        if target is None:
            return _redirect("/web/admin/kimi?error=未找到要操作的 Kimi 密钥。")
        database.set_kimi_api_key_active(key_id, not bool(target["is_active"]))
        return _redirect("/web/admin/kimi?message=Kimi 密钥启用状态已更新。")

    @router.post("/admin/kimi-keys/{key_id}/delete")
    async def admin_delete_kimi_key(request: Request, key_id: int):
        _, response = require_admin(request)
        if response is not None:
            return response
        keys = database.list_kimi_api_keys()
        target = next((item for item in keys if int(item["id"]) == key_id), None)
        if target is None:
            return _redirect("/web/admin/kimi?error=未找到要删除的 Kimi 密钥。")
        database.delete_kimi_api_key(key_id)
        return _redirect("/web/admin/kimi?message=Kimi 密钥已删除。")

    @router.get("/admin/logs", response_class=HTMLResponse)
    def admin_logs(request: Request):
        _, response = require_admin(request)
        if response is not None:
            return response
        filters = {
            "search": _query_str(request, "search"),
            "endpoint": _query_str(request, "endpoint"),
            "success": _query_str(request, "success"),
            "page": _query_int(request, "page", 1),
        }
        paginated = database.list_request_logs_paginated(
            search=filters["search"],
            endpoint=filters["endpoint"],
            success=filters["success"],
            page=filters["page"],
            page_size=20,
        )
        return render(
            request,
            "admin_logs.html",
            {
                "logs": paginated["items"],
                "filters": filters,
                "pagination": pagination_context(paginated),
                "title": "全局调用日志",
            },
        )

    @router.get("/admin/settings", response_class=HTMLResponse)
    def admin_settings(request: Request):
        _, response = require_admin(request)
        if response is not None:
            return response
        return render(
            request,
            "admin_settings.html",
            {
                "log_level": database.get_setting("log_level", "INFO"),
                "log_preview_bytes": database.get_setting("log_preview_bytes", "100"),
                "title": "系统设置",
            },
        )

    @router.post("/admin/settings")
    async def admin_settings_submit(
        request: Request,
        log_level: str = Form(...),
        log_preview_bytes: str = Form(...),
    ):
        _, response = require_admin(request)
        if response is not None:
            return response
        database.set_setting("log_level", log_level.strip().upper())
        database.set_setting("log_preview_bytes", log_preview_bytes.strip())
        return _redirect("/web/admin/settings?message=设置已更新。")

    @router.get("/me/api-keys", response_class=HTMLResponse)
    def my_api_keys(request: Request):
        user, response = require_user(request)
        if response is not None:
            return response
        existing_user = checked_user(user)
        return render(
            request,
            "user_api_keys.html",
            {**user_api_keys_context(request, int(existing_user["id"])), "title": "我的用户 API"},
        )

    @router.post("/me/api-keys")
    async def my_create_api_key(
        request: Request,
        name: str = Form(...),
        search_rpm: int = Form(...),
        fetch_rpm: int = Form(...),
        expires_at: str = Form(""),
    ):
        user, response = require_user(request)
        if response is not None:
            return response
        existing_user = checked_user(user)
        validation_error = validate_api_form(name, search_rpm, fetch_rpm)
        if validation_error:
            return _redirect(f"/web/me/api-keys?error={validation_error}")
        raw_key = generate_user_api_key()
        database.create_user_api_key(
            user_id=int(existing_user["id"]),
            name=name.strip() or "default",
            raw_key=raw_key,
            search_rpm=search_rpm,
            fetch_rpm=fetch_rpm,
            expires_at=expires_at.strip() or None,
        )
        context = user_api_keys_context(request, int(existing_user["id"]))
        context["new_key_value"] = raw_key
        context["message"] = "新的用户 API 已创建，请立即复制保存。"
        context["title"] = "我的用户 API"
        return render(request, "user_api_keys.html", context)

    @router.post("/me/api-keys/{key_id}/delete")
    async def my_delete_api_key(request: Request, key_id: int):
        user, response = require_user(request)
        if response is not None:
            return response
        existing_user = checked_user(user)
        target = database.get_user_api_key(key_id)
        if target is None or int(target["user_id"]) != int(existing_user["id"]):
            return _redirect("/web/me/api-keys?error=未找到要删除的用户 API。")
        database.delete_user_api_key(key_id)
        return _redirect("/web/me/api-keys?message=用户 API 已删除。")

    @router.get("/me/logs", response_class=HTMLResponse)
    def my_logs(request: Request):
        user, response = require_user(request)
        if response is not None:
            return response
        existing_user = checked_user(user)
        filters = {
            "search": _query_str(request, "search"),
            "endpoint": _query_str(request, "endpoint"),
            "success": _query_str(request, "success"),
            "page": _query_int(request, "page", 1),
        }
        paginated = database.list_request_logs_paginated(
            user_id=int(existing_user["id"]),
            search=filters["search"],
            endpoint=filters["endpoint"],
            success=filters["success"],
            page=filters["page"],
            page_size=20,
        )
        return render(
            request,
            "user_logs.html",
            {
                "logs": paginated["items"],
                "usage": database.get_usage_summary_for_user(int(existing_user["id"])),
                "filters": filters,
                "pagination": pagination_context(paginated),
                "title": "我的调用日志",
            },
        )

    return router
