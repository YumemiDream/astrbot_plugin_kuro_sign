import asyncio
import json
import threading
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from quart import jsonify, request

from . import kuro_core as core


PLUGIN_NAME = "astrbot_plugin_kuro_sign"


def parse_hhmm(value: str) -> str | None:
    raw = value.strip()
    if len(raw) != 5 or raw[2] != ":":
        return None
    hh, mm = raw[:2], raw[3:]
    if not (hh.isdigit() and mm.isdigit()):
        return None
    hour = int(hh)
    minute = int(mm)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return f"{hour:02}:{minute:02}"


def _safe_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _decode_owner_key(raw: str) -> str:
    value = str(raw or "").strip()
    for _ in range(3):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


class OwnerStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.owner_map = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, json.JSONDecodeError):
            self.owner_map = {}

    def _save_locked(self) -> None:
        self.path.write_text(json.dumps(self.owner_map, ensure_ascii=False, indent=2), encoding="utf-8")

    def bind(self, owner_key: str, sid: str) -> None:
        if not owner_key:
            return
        with self.lock:
            self.owner_map[owner_key] = sid
            self._save_locked()

    def get(self, owner_key: str) -> str:
        with self.lock:
            return self.owner_map.get(owner_key, "")

    def items(self) -> list[tuple[str, str]]:
        with self.lock:
            return list(self.owner_map.items())

    def unbind(self, owner_key: str) -> bool:
        if not owner_key:
            return False
        with self.lock:
            if owner_key not in self.owner_map:
                return False
            self.owner_map.pop(owner_key)
            self._save_locked()
            return True


class ScheduleStateStore:
    def __init__(self, path: Path, enabled: bool, run_time: str):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {
            "enabled": enabled,
            "time": run_time,
            "last_run_date": "",
            "last_run_at": "",
            "last_result": {},
        }
        self._load()
        self.state["enabled"] = enabled
        self.state["time"] = run_time
        self._save_locked()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.state.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass

    def _save_locked(self) -> None:
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.state)

    def update(self, enabled: bool | None = None, run_time: str | None = None) -> dict[str, Any]:
        with self.lock:
            if enabled is not None:
                self.state["enabled"] = bool(enabled)
            if run_time is not None:
                self.state["time"] = run_time
            self._save_locked()
            return dict(self.state)

    def should_trigger(self, now: datetime) -> bool:
        with self.lock:
            if not bool(self.state.get("enabled", False)):
                return False
            if str(self.state.get("time", "")) != now.strftime("%H:%M"):
                return False
            return str(self.state.get("last_run_date", "")) != now.strftime("%Y-%m-%d")

    def mark_run(self, result: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now()
        with self.lock:
            self.state["last_run_date"] = now.strftime("%Y-%m-%d")
            self.state["last_run_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self.state["last_result"] = result
            self._save_locked()
            return dict(self.state)


class KuroBridge:
    def __init__(
        self,
        host: str,
        port: int,
        owner_store: OwnerStore,
        schedule_store: ScheduleStateStore,
        public_ip: str = "",
        public_base_url: str = "",
        use_https: bool = True,
    ):
        self.host = host
        self.port = port
        self.use_https = use_https
        self.public_ip = public_ip.strip()
        self.public_base_url = public_base_url.strip()
        self.owner_store = owner_store
        self.schedule_store = schedule_store
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._run_all_callback: Callable[[str], dict[str, Any]] | None = None

    def set_run_all_callback(self, callback: Callable[[str], dict[str, Any]]) -> None:
        self._run_all_callback = callback

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AstrBotKuro/0.3"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                sid, created = core.get_or_create_session(self)
                cookie_sid = sid if created else None

                if parsed.path == "/":
                    owner_key = (parse_qs(parsed.query).get("user") or [""])[0].strip()
                    if owner_key:
                        decoded_owner_key = _decode_owner_key(owner_key)
                        bridge.owner_store.bind(owner_key, sid)
                        if decoded_owner_key and decoded_owner_key != owner_key:
                            bridge.owner_store.bind(decoded_owner_key, sid)
                    self._send_html(core.build_html(), cookie_sid)
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"code": 404, "msg": "not found"}, cookie_sid)

            def do_POST(self) -> None:
                sid, created = core.get_or_create_session(self)
                cookie_sid = sid if created else None
                try:
                    payload = self._read_json()
                except json.JSONDecodeError:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"code": 400, "msg": "invalid json"}, cookie_sid)
                    return

                with core.SESSIONS_LOCK:
                    session = core.SESSIONS[sid]

                if self.path == "/api/send_sms":
                    mobile = str(payload.get("mobile", "")).strip()
                    gee_test_data = payload.get("geeTestData")
                    if not (mobile.isdigit() and len(mobile) == 11):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"code": 400, "msg": "invalid mobile"}, cookie_sid)
                        return
                    if not isinstance(gee_test_data, dict):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"code": 400, "msg": "invalid geeTestData"}, cookie_sid)
                        return
                    headers = {
                        "source": "h5",
                        "devcode": session["h5_devcode"],
                        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                        "User-Agent": "Mozilla/5.0",
                    }
                    data = {"mobile": mobile, "geeTestData": json.dumps(gee_test_data, ensure_ascii=False)}
                    response = core.post_form(f"{core.KURO_BASE}/user/getSmsCodeForH5", headers, data)
                    self._send_json(HTTPStatus.OK, response, cookie_sid)
                    return

                if self.path == "/api/login":
                    mobile = str(payload.get("mobile", "")).strip()
                    code = str(payload.get("code", "")).strip()
                    if not (mobile.isdigit() and len(mobile) == 11):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"code": 400, "msg": "invalid mobile"}, cookie_sid)
                        return
                    if not (code.isdigit() and len(code) == 6):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"code": 400, "msg": "invalid code"}, cookie_sid)
                        return
                    headers = core.build_rover_base_headers()
                    response = core.post_form(
                        f"{core.KURO_BASE}/user/sdkLogin",
                        headers,
                        {"mobile": mobile, "code": code, "devCode": session["did"]},
                    )
                    if response.get("code") == 200 and isinstance(response.get("data"), dict):
                        data_obj = response["data"]
                        session["token"] = str(data_obj.get("token", ""))
                        session["user_id"] = str(data_obj.get("userId", ""))
                        session["user_name"] = str(data_obj.get("userName", ""))
                        session["head_url"] = str(data_obj.get("headUrl", ""))
                        session["login_mode_used"] = "xwuid_style"
                    self._send_json(HTTPStatus.OK, response, cookie_sid)
                    return

                if self.path == "/api/sign/waves":
                    self._send_json(HTTPStatus.OK, core.run_waves_sign(session), cookie_sid)
                    return

                if self.path == "/api/sign/bbs":
                    self._send_json(HTTPStatus.OK, core.run_bbs_sign(session), cookie_sid)
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"code": 404, "msg": "not found"}, cookie_sid)

            def _send_json(self, status: int, data: dict[str, Any], sid: str | None = None) -> None:
                payload = core.json_bytes(data)
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                if sid:
                    self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
                self.wfile.write(payload)

            def _send_html(self, html: bytes, sid: str | None = None) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                if sid:
                    self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly; SameSite=Lax")
                self.end_headers()
                self.wfile.write(html)

            def _read_json(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8") if length else "{}"
                return json.loads(raw)

        return Handler

    def ensure_started(self) -> None:
        if self._server:
            return
        with self._lock:
            if self._server:
                return
            self._server = ThreadingHTTPServer((self.host, self.port), self._make_handler())
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._server:
                return
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None

    def _resolve_public_base(self) -> str:
        scheme = "https" if self.use_https else "http"
        if self.public_ip:
            ip_or_host = self.public_ip.strip().rstrip("/")
            if ip_or_host.startswith(("http://", "https://")):
                return ip_or_host
            if ":" in ip_or_host:
                return f"{scheme}://{ip_or_host}"
            return f"{scheme}://{ip_or_host}:{self.port}"
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        return f"{scheme}://{self.host}:{self.port}"

    def login_url(self, owner_key: str) -> str:
        self.ensure_started()
        base = self._resolve_public_base()
        return f"{base}/?user={quote(owner_key, safe='')}"

    def _get_session(self, owner_key: str) -> tuple[dict[str, str] | None, str]:
        sid = self.owner_store.get(owner_key)
        if not sid:
            return None, "未找到登录会话，请先执行 /kuro_login"
        with core.SESSIONS_LOCK:
            session = core.SESSIONS.get(sid)
        if not session:
            return None, "会话不存在，请重新登录"
        return session, ""

    def unbind(self, owner_key: str) -> dict[str, Any]:
        sid = self.owner_store.get(owner_key)
        removed = self.owner_store.unbind(owner_key)
        if sid:
            with core.SESSIONS_LOCK:
                core.SESSIONS.pop(sid, None)
        if not removed:
            return {"success": False, "msg": "未找到该账号的绑定记录"}
        return {"success": True, "msg": "已解绑并清除本地会话数据"}

    def status(self, owner_key: str) -> dict[str, Any]:
        session, err = self._get_session(owner_key)
        if not session:
            return {"logged_in": False, "msg": err}
        return {
            "logged_in": bool(session.get("token")),
            "userId": session.get("user_id", ""),
            "userName": session.get("user_name", ""),
            "headUrl": session.get("head_url", ""),
            "roleId": session.get("waves_role_id", ""),
            "serverId": session.get("waves_server_id", ""),
            "roleName": session.get("waves_role_name", ""),
        }

    def waves_sign(self, owner_key: str) -> dict[str, Any]:
        session, err = self._get_session(owner_key)
        if not session:
            return {"success": False, "msg": err}
        return core.run_waves_sign(session)

    def bbs_sign(self, owner_key: str) -> dict[str, Any]:
        session, err = self._get_session(owner_key)
        if not session:
            return {"success": False, "msg": err}
        return core.run_bbs_sign(session)

    def sign_both(self, owner_key: str) -> dict[str, Any]:
        waves = self.waves_sign(owner_key)
        bbs = self.bbs_sign(owner_key)
        return {"success": bool(waves.get("success")) and bool(bbs.get("success")), "waves": waves, "bbs": bbs}

    def sign_all_users(self, trigger: str) -> dict[str, Any]:
        if self._run_all_callback:
            return self._run_all_callback(trigger)
        return {"success": False, "trigger": trigger, "msg": "run callback not configured"}

    def admin_status(self) -> dict[str, Any]:
        owners: list[dict[str, Any]] = []
        for owner_key, sid in self.owner_store.items():
            with core.SESSIONS_LOCK:
                session = dict(core.SESSIONS.get(sid) or {})
            owners.append(
                {
                    "ownerKey": owner_key,
                    "loggedIn": bool(session.get("token")),
                    "userId": session.get("user_id", ""),
                    "userName": session.get("user_name", ""),
                    "roleName": session.get("waves_role_name", ""),
                }
            )
        schedule = self.schedule_store.snapshot()
        return {
            "code": 200,
            "msg": "ok",
            "schedule": schedule,
            "owners": owners,
            "ownerCount": len(owners),
            "loggedInCount": len([o for o in owners if o.get("loggedIn")]),
            "publicBase": self._resolve_public_base(),
        }


def fmt_waves_result(payload: dict[str, Any]) -> str:
    if not payload.get("success"):
        return f"鸣潮签到失败\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    context = payload.get("context") or {}
    result = payload.get("result", "-")
    label = {"signed": "签到成功", "already_signed": "今日已签到"}.get(result, result)
    return "\n".join(
        [
            f"鸣潮签到: {label}",
            f"角色: {context.get('roleName', '-')}",
            f"roleId: {context.get('roleId', '-')}",
            f"serverId: {context.get('serverId', '-')}",
        ]
    )


def fmt_bbs_result(payload: dict[str, Any]) -> str:
    if not payload.get("success"):
        return f"社区任务失败\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    data = (payload.get("taskResponse") or {}).get("data") or {}
    daily = data.get("dailyTask") or []
    pending = [
        f"{task.get('remark', '-')}: {task.get('completeTimes', 0)}/{task.get('needActionTimes', 0)}"
        for task in daily
        if task.get("completeTimes") != task.get("needActionTimes")
    ]
    actions = [
        f"{name}: {value.get('code')}"
        for name, value in (payload.get("actions") or {}).items()
        if isinstance(value, dict) and "code" in value
    ]
    lines = [
        "社区任务执行完成",
        f"今日库洛币: {data.get('currentDailyGold', '-')}/{data.get('maxDailyGold', '-')}",
        "未完成任务:" if pending else "今日任务已全部完成",
    ]
    if pending:
        lines.extend(pending)
    if actions:
        lines.append("本次执行:")
        lines.extend(actions)
    return "\n".join(lines)


def fmt_sign_both(payload: dict[str, Any]) -> str:
    return "\n\n".join([fmt_waves_result(payload.get("waves") or {}), fmt_bbs_result(payload.get("bbs") or {})])


def _resolve_plugin_data_dir(plugin_dir: Path) -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        data_dir = get_astrbot_data_path() / "plugin_data" / PLUGIN_NAME
    except Exception:
        data_dir = plugin_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@register("astrbot_plugin_kuro_sign", "Kuro Sign", "网页登录获取 Kuro token，并支持定时签到与 WebUI 控制", "0.3.0")
class KuroSignPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        plugin_dir = Path(__file__).resolve().parent
        data_dir = _resolve_plugin_data_dir(plugin_dir)
        self.owner_store = OwnerStore(data_dir / "owner_map.json")
        self.schedule_store = ScheduleStateStore(
            data_dir / "schedule_state.json",
            enabled=_safe_bool(self._cfg("auto_sign_enabled", False), False),
            run_time=self._get_schedule_time(),
        )

        host = str(self._cfg("host", "0.0.0.0"))
        public_ip = str(self._cfg("public_ip", "")).strip()
        if public_ip and host in ("127.0.0.1", "localhost"):
            host = "0.0.0.0"
        self.bridge = KuroBridge(
            host=host,
            port=self._cfg_int("port", 8765),
            owner_store=self.owner_store,
            schedule_store=self.schedule_store,
            public_ip=public_ip,
            public_base_url=str(self._cfg("public_base_url", "")),
            use_https=_safe_bool(self._cfg("use_https", True), True),
        )
        self.bridge.set_run_all_callback(self._run_all_sign)
        self._register_web_apis()
        self.schedule_notify = _safe_bool(self._cfg("schedule_notify", True), True)
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(target=self._schedule_loop, daemon=True)
        self._scheduler_thread.start()
        self._login_watch_tasks: dict[str, asyncio.Task] = {}

    def _cfg(self, key: str, default: Any) -> Any:
        if isinstance(self.config, dict):
            return self.config.get(key, default)
        getter = getattr(self.config, "get", None)
        if callable(getter):
            return getter(key, default)
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_list(self, key: str) -> list[str]:
        value = self._cfg(key, [])
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return []

    def _get_schedule_time(self) -> str:
        parsed = parse_hhmm(str(self._cfg("auto_sign_time", "04:05")))
        return parsed or "04:05"

    def _owner_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or event.get_sender_id())

    def _admin_keys(self) -> set[str]:
        return set(self._cfg_list("admin_ids"))

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admin_keys = self._admin_keys()
        if not admin_keys:
            return False
        sender_id = str(event.get_sender_id())
        owner_key = self._owner_key(event)
        return sender_id in admin_keys or owner_key in admin_keys

    def _run_all_sign(self, trigger: str) -> dict[str, Any]:
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        owners = self.owner_store.items()
        results: list[dict[str, Any]] = []
        ok_count = 0
        for owner_key, sid in owners:
            with core.SESSIONS_LOCK:
                session = core.SESSIONS.get(sid)
            if not session:
                results.append({"ownerKey": owner_key, "success": False, "msg": "session missing"})
                continue
            waves = core.run_waves_sign(session)
            bbs = core.run_bbs_sign(session)
            success = bool(waves.get("success")) and bool(bbs.get("success"))
            if success:
                ok_count += 1
            results.append(
                {
                    "ownerKey": owner_key,
                    "userName": session.get("user_name", ""),
                    "success": success,
                    "wavesResult": waves.get("result", "failed"),
                    "bbsSuccess": bool(bbs.get("success")),
                }
            )
        payload = {
            "success": True,
            "trigger": trigger,
            "startedAt": started_at,
            "total": len(owners),
            "ok": ok_count,
            "failed": len(owners) - ok_count,
            "results": results,
        }
        self.schedule_store.mark_run(payload)
        return payload

    def _register_web_apis(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/admin/status",
            self.api_admin_status,
            ["POST"],
            "获取 Kuro Sign 管理状态",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/admin/schedule",
            self.api_admin_schedule,
            ["POST"],
            "更新定时签到配置",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/admin/run_all",
            self.api_admin_run_all,
            ["POST"],
            "立即执行全量签到",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/admin/unbind",
            self.api_admin_unbind,
            ["POST"],
            "解绑/删除账号并清除本地会话数据",
        )

    async def api_admin_status(self):
        try:
            return self.bridge.admin_status()
        except Exception as e:
            logger.error(f"kuro api_admin_status error: {e}")
            return {"code": 500, "msg": str(e)}

    async def api_admin_schedule(self):
        try:
            data = await request.get_json(silent=True) or {}
            enabled = data.get("enabled")
            run_time = data.get("time")
            normalized_time: str | None = None
            if run_time is not None:
                normalized_time = parse_hhmm(str(run_time))
                if not normalized_time:
                    return {"code": 400, "msg": "time must be HH:MM"}, 400
            state = self.bridge.schedule_store.update(
                enabled=_safe_bool(enabled, False) if enabled is not None else None,
                run_time=normalized_time,
            )
            return {"code": 200, "msg": "ok", "schedule": state}
        except Exception as e:
            logger.error(f"kuro api_admin_schedule error: {e}")
            return {"code": 500, "msg": str(e)}

    async def api_admin_run_all(self):
        try:
            result = self.bridge.sign_all_users(trigger="webui_manual")
            return {"code": 200, "msg": "ok", "result": result}
        except Exception as e:
            logger.error(f"kuro api_admin_run_all error: {e}")
            return {"code": 500, "msg": str(e)}

    async def api_admin_unbind(self):
        try:
            data = await request.get_json(silent=True) or {}
            target = str(data.get("target") or "").strip()
            if not target:
                return {"code": 400, "msg": "target required"}, 400
            result = self.bridge.unbind(target)
            if not result.get("success"):
                return {"code": 404, "msg": result.get("msg", "unbind failed")}, 404
            return {"code": 200, "msg": "ok", "result": result}
        except Exception as e:
            logger.error(f"kuro api_admin_unbind error: {e}")
            return {"code": 500, "msg": str(e)}

    def _schedule_loop(self) -> None:
        while not self._scheduler_stop.wait(15):
            now = datetime.now()
            if not self.schedule_store.should_trigger(now):
                continue
            try:
                result = self._run_all_sign("schedule")
                logger.info(f"kuro scheduled sign done: total={result.get('total')} ok={result.get('ok')}")
                if self.schedule_notify:
                    self._notify_schedule(result)
            except Exception as exc:
                error_payload = {"success": False, "trigger": "schedule", "error": str(exc)}
                self.schedule_store.mark_run(error_payload)
                logger.error(f"kuro scheduled sign failed: {exc}")

    def _fmt_schedule_owner(self, item: dict[str, Any]) -> str:
        user_name = item.get("userName") or item.get("ownerKey") or "-"
        if not item.get("success") and item.get("msg"):
            return "\n".join(
                [
                    "定时签到结果（schedule）",
                    f"账号: {user_name}",
                    f"失败: {item.get('msg')}",
                ]
            )
        waves_map = {"signed": "签到成功", "already_signed": "今日已签到", "failed": "签到失败"}
        waves_label = waves_map.get(str(item.get("wavesResult", "")), str(item.get("wavesResult", "失败")))
        bbs_label = "成功" if item.get("bbsSuccess") else "失败"
        return "\n".join(
            [
                "定时签到完成（schedule）",
                f"账号: {user_name}",
                f"鸣潮: {waves_label}",
                f"社区: {bbs_label}",
            ]
        )

    def _notify_schedule(self, result: dict[str, Any]) -> None:
        if not self._loop:
            logger.warning("kuro schedule notify skipped: event loop unavailable")
            return
        items = result.get("results") or []
        for item in items:
            owner_key = item.get("ownerKey")
            if not owner_key:
                continue
            text = self._fmt_schedule_owner(item)
            try:
                asyncio.run_coroutine_threadsafe(self._send_private_text(owner_key, text), self._loop)
            except RuntimeError as exc:
                logger.warning(f"kuro schedule notify failed for {owner_key}: {exc}")

    async def _wait_login_success(self, owner_key: str, timeout_sec: int = 180, poll_sec: int = 3) -> dict[str, Any] | None:
        rounds = max(1, timeout_sec // poll_sec)
        for _ in range(rounds):
            await asyncio.sleep(poll_sec)
            payload = await asyncio.to_thread(self.bridge.status, owner_key)
            if payload.get("logged_in"):
                return payload
        return None

    async def _send_private_text(self, owner_key: str, text: str) -> bool:
        try:
            await self.context.send_message(owner_key, MessageChain().message(text))
            return True
        except Exception as exc:
            logger.warning(f"kuro proactive notify failed for {owner_key}: {exc}")
            return False

    def _start_login_watch(self, owner_key: str) -> None:
        old_task = self._login_watch_tasks.get(owner_key)
        if old_task and not old_task.done():
            old_task.cancel()

        async def _watch() -> None:
            payload = await self._wait_login_success(owner_key)
            if not payload:
                return
            user_name = payload.get("userName") or payload.get("userId") or "-"
            await self._send_private_text(owner_key, f"登录成功：{user_name}")

        task = asyncio.create_task(_watch())
        self._login_watch_tasks[owner_key] = task

        def _cleanup(done_task: asyncio.Task) -> None:
            current = self._login_watch_tasks.get(owner_key)
            if current is done_task:
                self._login_watch_tasks.pop(owner_key, None)

        task.add_done_callback(_cleanup)

    @filter.command("kuro_login")
    async def kuro_login(self, event: AstrMessageEvent):
        owner_key = self._owner_key(event)
        url = await asyncio.to_thread(self.bridge.login_url, owner_key)
        yield event.plain_result(
            "打开下方登录页，完成极验和短信登录：\n"
            f"{url}\n\n"
            "登录成功后可执行 /kuro_sign"
        )
        self._start_login_watch(owner_key)

    @filter.command("kuro_status")
    async def kuro_status(self, event: AstrMessageEvent):
        payload = await asyncio.to_thread(self.bridge.status, self._owner_key(event))
        if not payload.get("logged_in"):
            yield event.plain_result(payload.get("msg", "未登录"))
            return
        lines = [
            "当前登录状态正常",
            f"userId: {payload.get('userId', '-')}",
            f"userName: {payload.get('userName', '-')}",
            f"roleName: {payload.get('roleName', '-')}",
            f"roleId: {payload.get('roleId', '-')}",
        ]
        head_url = str(payload.get("headUrl", "") or "")
        if head_url:
            lines.append(f"headUrl: {head_url}")
        yield event.plain_result("\n".join(lines))

    @filter.command("kuro_unbind")
    async def kuro_unbind(self, event: AstrMessageEvent, target: str = ""):
        owner_key = self._owner_key(event)
        if target:
            if not self._is_admin(event):
                yield event.plain_result("权限不足：仅管理员可解绑他人，请留空以解绑自己。")
                return
            unbind_key = target.strip()
        else:
            unbind_key = owner_key
        result = await asyncio.to_thread(self.bridge.unbind, unbind_key)
        watch_task = self._login_watch_tasks.pop(unbind_key, None)
        if watch_task and not watch_task.done():
            watch_task.cancel()
        yield event.plain_result(result.get("msg", "已解绑"))

    @filter.command("kuro_sign", alias={"ksign", "kuro_checkin"})
    async def kuro_sign(self, event: AstrMessageEvent):
        payload = await asyncio.to_thread(self.bridge.sign_both, self._owner_key(event))
        yield event.plain_result(fmt_sign_both(payload))

    @filter.command("kuro_waves_sign")
    async def kuro_waves_sign(self, event: AstrMessageEvent):
        payload = await asyncio.to_thread(self.bridge.waves_sign, self._owner_key(event))
        yield event.plain_result(fmt_waves_result(payload))

    @filter.command("kuro_bbs_sign")
    async def kuro_bbs_sign(self, event: AstrMessageEvent):
        payload = await asyncio.to_thread(self.bridge.bbs_sign, self._owner_key(event))
        yield event.plain_result(fmt_bbs_result(payload))

    @filter.command("kuro_admin")
    async def kuro_admin(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("权限不足：仅管理员可用。请在插件配置中设置 admin_ids。")
            return
        yield event.plain_result(
            "Kuro Sign 管理页面已整合进 AstrBot 控制台（Dashboard）。\n"
            "请在网页端打开 AstrBot Dashboard -> 插件 -> Kuro Sign，即可调整定时签到与手动触发全量签到。"
        )

    @filter.command("kuro_auto_on")
    async def kuro_auto_on(self, event: AstrMessageEvent, run_time: str = ""):
        if not self._is_admin(event):
            yield event.plain_result("权限不足：仅管理员可用。")
            return
        normalized = parse_hhmm(run_time) if run_time else self.schedule_store.snapshot().get("time", "04:05")
        if not normalized:
            yield event.plain_result("时间格式错误，请使用 HH:MM，例如 /kuro_auto_on 04:05")
            return
        state = await asyncio.to_thread(self.schedule_store.update, True, normalized)
        yield event.plain_result(f"已开启定时签到，执行时间 {state.get('time')}")

    @filter.command("kuro_auto_off")
    async def kuro_auto_off(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("权限不足：仅管理员可用。")
            return
        state = await asyncio.to_thread(self.schedule_store.update, False, None)
        yield event.plain_result(f"已关闭定时签到（当前时间配置保留为 {state.get('time')}）")

    @filter.command("kuro_auto_status")
    async def kuro_auto_status(self, event: AstrMessageEvent):
        state = await asyncio.to_thread(self.schedule_store.snapshot)
        yield event.plain_result(
            "\n".join(
                [
                    f"定时签到: {'开启' if state.get('enabled') else '关闭'}",
                    f"执行时间: {state.get('time', '-')}",
                    f"最近执行日期: {state.get('last_run_date', '-') or '-'}",
                    f"最近执行时间: {state.get('last_run_at', '-') or '-'}",
                ]
            )
        )

    @filter.command("kuro_auto_run")
    async def kuro_auto_run(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            yield event.plain_result("权限不足：仅管理员可用。")
            return
        result = await asyncio.to_thread(self._run_all_sign, "command_manual")
        yield event.plain_result(
            "\n".join(
                [
                    "已执行全量签到",
                    f"总数: {result.get('total', 0)}",
                    f"成功: {result.get('ok', 0)}",
                    f"失败: {result.get('failed', 0)}",
                ]
            )
        )

    async def terminate(self):
        logger.info("stopping kuro sign scheduler and local server")
        self._scheduler_stop.set()
        for task in list(self._login_watch_tasks.values()):
            task.cancel()
        if self._login_watch_tasks:
            await asyncio.gather(*self._login_watch_tasks.values(), return_exceptions=True)
        self._login_watch_tasks.clear()
        await asyncio.to_thread(self.bridge.stop)
