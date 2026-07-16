import json
import random
import secrets
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


KURO_BASE = "https://api.kurobbs.com"
CAPTCHA_ID = "ec4aa4174277d822d73f2442a165a2cd"
PRODUCT = "bind"
KURO_VERSION = "2.10.0"
IOS_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) KuroGameBox/2.10.0"
)
ANDROID_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 16; 25098PN5AC Build/BP2A.250605.031.A3; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/143.0.7499.34 "
    "Mobile Safari/537.36 Kuro/2.10.0 KuroGameBox/2.10.0"
)

SESSIONS: dict[str, dict[str, str]] = {}
SESSIONS_LOCK = threading.Lock()
SESSION_OPERATION_LOCKS: dict[str, threading.Lock] = {}
SESSION_OPERATION_LOCKS_LOCK = threading.Lock()
OWNER_OPERATION_LOCKS: dict[str, threading.Lock] = {}
OWNER_OPERATION_LOCKS_LOCK = threading.Lock()
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Kuro Login</title>
  <script src="https://static.geetest.com/v4/gt4.js"></script>
  <style>
    body { margin: 0; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; background: linear-gradient(135deg, #102131, #32556d); color: #11222d; }
    .wrap { min-height: 100vh; display: grid; place-items: center; padding: 24px; }
    .card { width: min(100%, 440px); background: rgba(245, 248, 250, 0.94); border-radius: 20px; padding: 28px; box-shadow: 0 20px 60px rgba(0,0,0,.2); }
    h1 { margin: 0 0 8px; font-size: 28px; }
    p { margin: 0 0 18px; color: #52626d; line-height: 1.5; }
    label { display:block; margin: 14px 0 8px; font-size: 14px; font-weight: 600; }
    input { width: 100%; padding: 12px 14px; border: 1px solid rgba(17,34,45,.12); border-radius: 12px; font-size: 16px; box-sizing: border-box; }
    .row { display:grid; grid-template-columns: 1fr auto; gap: 10px; align-items: end; }
    button { border: 0; border-radius: 12px; padding: 12px 16px; font-size: 15px; font-weight: 700; color: #fff; background: #0e7c86; cursor: pointer; }
    button:disabled { background: #9aa7af; cursor: not-allowed; }
    .secondary { width: 120px; }
    .actions { display:grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
    .status { margin-top: 16px; padding: 12px 14px; border-radius: 12px; background: rgba(17,34,45,.06); white-space: pre-wrap; line-height: 1.5; font-size: 14px; }
    .result { margin-top: 16px; padding: 14px; border-radius: 14px; background: #f7fbfc; border: 1px solid rgba(17,34,45,.08); display: none; }
    .summary { white-space: pre-wrap; line-height: 1.6; font-size: 14px; }
    details { margin-top: 12px; border-top: 1px solid rgba(17,34,45,.08); padding-top: 10px; }
    summary { cursor: pointer; color: #09565d; font-weight: 700; }
    pre { margin: 10px 0 0; padding: 12px; border-radius: 12px; background: #0e1720; color: #d7f3ff; font-size: 12px; white-space: pre-wrap; word-break: break-all; overflow:auto; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Kuro Token Login</h1>
      <p>先手动完成 GeeTest 发送短信验证码，再输入验证码登录。当前链路固定为 H5 发码，APP 登录。</p>
      <label for="phone">手机号</label>
      <input id="phone" type="tel" maxlength="11" placeholder="请输入 11 位手机号">
      <label for="code">短信验证码</label>
      <div class="row">
        <input id="code" type="text" maxlength="6" placeholder="请输入 6 位验证码">
        <button id="sendBtn" class="secondary" type="button" disabled>获取验证码</button>
      </div>
      <button id="loginBtn" type="button" style="margin-top: 18px;" disabled>登录并获取 Token</button>
      <div class="actions">
        <button id="wavesSignBtn" type="button" disabled>鸣潮签到</button>
        <button id="bbsSignBtn" type="button" disabled>社区签到</button>
      </div>
      <div id="status" class="status">等待输入手机号。</div>
      <div id="result" class="result"></div>
    </div>
  </div>
  <script>
    const phoneInput = document.getElementById("phone");
    const codeInput = document.getElementById("code");
    const sendBtn = document.getElementById("sendBtn");
    const loginBtn = document.getElementById("loginBtn");
    const wavesSignBtn = document.getElementById("wavesSignBtn");
    const bbsSignBtn = document.getElementById("bbsSignBtn");
    const statusBox = document.getElementById("status");
    const resultBox = document.getElementById("result");
    function setStatus(text) { statusBox.textContent = text; }
    function escapeHtml(text) { return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
    function formatTaskProgress(task) { return `${task.remark}: ${task.completeTimes}/${task.needActionTimes}`; }
    function summarizePayload(obj) {
      if (obj && obj.mode === "waves") {
        const task = obj.taskListResponse || {};
        const data = task.data || {};
        const stateMap = { signed: "签到成功", already_signed: "今日已签到", failed: "签到失败" };
        return [
          `鸣潮签到: ${stateMap[obj.result] || obj.result}`,
          `角色: ${(obj.context || {}).roleName || "-"}`,
          `roleId: ${(obj.context || {}).roleId || "-"}`,
          `serverId: ${(obj.context || {}).serverId || "-"}`,
          `当前状态: ${data.isSigIn ? "已签到" : "未签到"}`
        ].join("\\n");
      }
      if (obj && obj.mode === "bbs") {
        const task = obj.taskResponse || {};
        const data = task.data || {};
        const daily = Array.isArray(data.dailyTask) ? data.dailyTask : [];
        const pending = daily.filter((t) => t.completeTimes !== t.needActionTimes);
        const actionCodes = Object.entries(obj.actions || {})
          .filter(([, value]) => value && typeof value === "object" && "code" in value)
          .map(([key, value]) => `${key}: ${value.code}`);
        return [
          "社区任务执行完成",
          `今日库洛币: ${data.currentDailyGold ?? "-"} / ${data.maxDailyGold ?? "-"}`,
          pending.length ? `未完成任务:\\n${pending.map(formatTaskProgress).join("\\n")}` : "今日任务已全部完成",
          actionCodes.length ? `本次执行:\\n${actionCodes.join("\\n")}` : "本次无需执行额外动作"
        ].join("\\n");
      }
      if (obj && obj.data && obj.data.token) {
        return [
          "登录成功",
          `userId: ${obj.data.userId || "-"}`,
          `userName: ${obj.data.userName || "-"}`,
          `headUrl: ${obj.data.headUrl || "-"}`,
          `traceId: ${obj.traceId || "-"}`
        ].join("\\n");
      }
      if (obj && obj.data && typeof obj.data.geeTest === "boolean") {
        return [
          "验证码请求结果",
          `geeTest: ${obj.data.geeTest}`,
          `msg: ${obj.msg || "-"}`,
          `traceId: ${obj.traceId || "-"}`
        ].join("\\n");
      }
      return JSON.stringify(obj, null, 2);
    }
    function setResult(obj) {
      resultBox.style.display = "block";
      const summary = summarizePayload(obj);
      const raw = escapeHtml(JSON.stringify(obj, null, 2));
      resultBox.innerHTML = `<div class="summary">${escapeHtml(summary).replace(/\\n/g, "<br>")}</div><details><summary>原始响应</summary><pre>${raw}</pre></details>`;
    }
    function validatePhone() { return /^1\\d{10}$/.test(phoneInput.value.trim()); }
    function validateCode() { return /^\\d{6}$/.test(codeInput.value.trim()); }
    function refreshState() { sendBtn.disabled = !validatePhone(); loginBtn.disabled = !(validatePhone() && validateCode()); }
    phoneInput.addEventListener("input", refreshState);
    codeInput.addEventListener("input", refreshState);
    refreshState();
    const pageSid = "%SESSION_ID%";
    function postJson(url, data) {
      return fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-Kuro-Session": pageSid }, body: JSON.stringify(data) }).then(async (res) => {
        const payload = await res.json();
        if (!res.ok) throw payload;
        return payload;
      });
    }
    async function postSign(url) { const payload = await postJson(url, {}); setResult(payload); return payload; }
    initGeetest4({ captchaId: "%CAPTCHA_ID%", product: "%PRODUCT%" }, function (captcha) {
      captcha.onSuccess(async function () {
        const validate = captcha.getValidate();
        if (!validate) { setStatus("GeeTest 未完成。"); return; }
        validate.captcha_id = "%CAPTCHA_ID%";
        setStatus("正在请求发送短信...");
        try {
          const payload = await postJson("/api/send_sms", { mobile: phoneInput.value.trim(), geeTestData: validate });
          setStatus("短信请求已发送，请查看返回结果。");
          setResult(payload);
        } catch (err) {
          setStatus("发送短信失败。");
          setResult(err);
        }
      });
      captcha.onError(function () { setStatus("GeeTest 初始化失败。"); });
      sendBtn.addEventListener("click", function () { resultBox.style.display = "none"; setStatus("请完成 GeeTest 验证。"); captcha.showBox(); });
    });
    loginBtn.addEventListener("click", async function () {
      setStatus("正在登录...");
      resultBox.style.display = "none";
      try {
        const payload = await postJson("/api/login", { mobile: phoneInput.value.trim(), code: codeInput.value.trim() });
        setStatus("登录请求已完成。");
        setResult(payload);
        if (payload && payload.data && payload.data.token) { wavesSignBtn.disabled = false; bbsSignBtn.disabled = false; }
      } catch (err) { setStatus("登录失败。"); setResult(err); }
    });
    wavesSignBtn.addEventListener("click", async function () {
      setStatus("正在执行鸣潮签到...");
      resultBox.style.display = "none";
      try { await postSign("/api/sign/waves"); setStatus("鸣潮签到请求已完成。"); }
      catch (err) { setStatus("鸣潮签到失败。"); setResult(err); }
    });
    bbsSignBtn.addEventListener("click", async function () {
      setStatus("正在执行社区签到任务...");
      resultBox.style.display = "none";
      try { await postSign("/api/sign/bbs"); setStatus("社区签到任务请求已完成。"); }
      catch (err) { setStatus("社区签到任务失败。"); setResult(err); }
    });
  </script>
</body>
</html>
"""

def _load_template(name: str, fallback: str) -> str:
    try:
        return (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return fallback


def build_html(sid: str = "") -> bytes:
    html = _load_template("login.html", HTML_PAGE)
    return (
        html.replace("%CAPTCHA_ID%", CAPTCHA_ID)
        .replace("%PRODUCT%", PRODUCT)
        .replace("%SESSION_ID%", sid)
        .encode("utf-8")
    )





def json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


def post_form(url: str, headers: dict[str, str], data: dict[str, str]) -> dict[str, Any]:
    body = urlencode(data).encode("utf-8")
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
    except URLError as exc:
        return {"code": -1, "msg": f"network error: {exc}"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"code": -1, "msg": "non-json response", "raw": raw}


def build_rover_base_headers(
    token: str | None = None,
    did: str | None = None,
    bat: str | None = None,
    devcode: str | None = None,
) -> dict[str, str]:
    use_ios = random.choice([True, False])
    user_agent = IOS_USER_AGENT if use_ios else ANDROID_USER_AGENT
    headers = {
        "source": "ios" if use_ios else "android",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "User-Agent": user_agent,
        "version": KURO_VERSION,
        "devCode": devcode or f"127.0.0.1, {user_agent}",
    }
    if token:
        headers["token"] = token
    if did is not None:
        headers["did"] = did
    if bat is not None:
        headers["b-at"] = bat
    return headers


def find_waves_role(token: str) -> dict[str, Any]:
    headers = {
        "osversion": "Android",
        "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
        "countrycode": "CN",
        "ip": "10.0.2.233",
        "model": "2211133C",
        "source": "android",
        "lang": "zh-Hans",
        "version": "1.0.9",
        "versioncode": "1090",
        "token": token,
        "content-type": "application/x-www-form-urlencoded; charset=utf-8",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/3.10.0",
    }
    return post_form(f"{KURO_BASE}/gamer/role/list", headers, {"gameId": "3"})


def refresh_bat_token(token: str, did: str, role_id: str, server_id: str) -> dict[str, Any]:
    return post_form(f"{KURO_BASE}/aki/roleBox/requestToken", build_rover_base_headers(token=token, did=did, bat=""), {"serverId": server_id, "roleId": role_id})


def waves_sign_task_list(token: str, did: str, bat: str, role_id: str, server_id: str) -> dict[str, Any]:
    return post_form(f"{KURO_BASE}/encourage/signIn/initSignInV2", build_rover_base_headers(token=token, did=did, bat=bat, devcode=""), {"gameId": "3", "serverId": server_id, "roleId": role_id})


def waves_do_sign(token: str, did: str, bat: str, role_id: str, server_id: str) -> dict[str, Any]:
    return post_form(f"{KURO_BASE}/encourage/signIn/v2", build_rover_base_headers(token=token, did=did, bat=bat, devcode=""), {"gameId": "3", "serverId": server_id, "roleId": role_id, "reqMonth": f"{datetime.now().month:02}"})


def bbs_get_task(token: str, did: str, bat: str) -> dict[str, Any]:
    return post_form(f"{KURO_BASE}/encourage/level/getTaskProcess", build_rover_base_headers(token=token, did=did, bat=bat), {"gameId": "0"})


def bbs_get_posts(token: str, did: str, bat: str) -> dict[str, Any]:
    headers = build_rover_base_headers(token=token, did=did, bat=bat)
    headers["version"] = "2.25"
    return post_form(f"{KURO_BASE}/forum/list", headers, {"pageIndex": "1", "pageSize": "20", "timeType": "0", "searchType": "1", "forumId": "9", "gameId": "3"})


def bbs_do_sign(token: str, did: str, bat: str) -> dict[str, Any]:
    return post_form(f"{KURO_BASE}/user/signIn", build_rover_base_headers(token=token, did=did, bat=bat), {"gameId": "2"})


def bbs_do_post_detail(token: str, did: str, post_id: str) -> dict[str, Any]:
    headers = build_rover_base_headers(token=token, devcode=did)
    headers["token"] = token
    return post_form(f"{KURO_BASE}/forum/getPostDetail", headers, {"postId": post_id, "showOrderType": "2", "isOnlyPublisher": "0"})


def bbs_do_like(token: str, did: str, bat: str, post_id: str, to_user_id: str) -> dict[str, Any]:
    return post_form(f"{KURO_BASE}/forum/like", build_rover_base_headers(token=token, did=did, bat=bat), {"gameId": "3", "likeType": "1", "operateType": "1", "postId": post_id, "toUserId": to_user_id})


def bbs_do_share(token: str, did: str, bat: str) -> dict[str, Any]:
    return post_form(f"{KURO_BASE}/encourage/level/shareTask", build_rover_base_headers(token=token, did=did, bat=bat), {"gameId": "3"})


def ensure_sign_context(session: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    token = session.get("token", "")
    if not token:
        return False, {"code": 401, "msg": "no token in session"}
    did = session.get("did", "") or str(uuid.uuid4()).upper()
    session["did"] = did
    role_id = session.get("waves_role_id", "")
    server_id = session.get("waves_server_id", "")
    role_name = session.get("waves_role_name", "")
    if not role_id or not server_id:
        role_res = find_waves_role(token)
        role_list = role_res.get("data") or []
        if not isinstance(role_list, list) or not role_list:
            return False, {"code": 400, "msg": "no waves role found", "roleResponse": role_res}
        role = role_list[0]
        role_id = str(role.get("roleId", ""))
        server_id = str(role.get("serverId", ""))
        role_name = str(role.get("roleName", ""))
        session["waves_role_id"] = role_id
        session["waves_server_id"] = server_id
        session["waves_role_name"] = role_name
    bat_res = refresh_bat_token(token, did, role_id, server_id)
    bat = str((bat_res.get("data") or {}).get("accessToken", "")) if isinstance(bat_res.get("data"), dict) else ""
    session["b_at"] = bat
    return True, {"token": token, "did": did, "b_at": bat, "roleId": role_id, "serverId": server_id, "roleName": role_name, "batResponse": bat_res}


def run_waves_sign(session: dict[str, str]) -> dict[str, Any]:
    ok, context = ensure_sign_context(session)
    if not ok:
        return {"success": False, "stage": "prepare", "detail": context}
    task_res = waves_sign_task_list(context["token"], context["did"], context["b_at"], context["roleId"], context["serverId"])
    if isinstance(task_res.get("data"), dict) and bool(task_res["data"].get("isSigIn", False)):
        return {"success": True, "mode": "waves", "result": "already_signed", "taskListResponse": task_res, "context": {k: context[k] for k in ("roleId", "serverId", "roleName")}}
    sign_res = waves_do_sign(context["token"], context["did"], context["b_at"], context["roleId"], context["serverId"])
    sign_code = sign_res.get("code")
    return {"success": sign_code in (200, 1511), "mode": "waves", "result": "signed" if sign_code == 200 else "already_signed" if sign_code == 1511 else "failed", "taskListResponse": task_res, "signResponse": sign_res, "context": {k: context[k] for k in ("roleId", "serverId", "roleName")}}


def run_bbs_sign(session: dict[str, str]) -> dict[str, Any]:
    ok, context = ensure_sign_context(session)
    if not ok:
        return {"success": False, "stage": "prepare", "detail": context}
    task_res = bbs_get_task(context["token"], context["did"], context["b_at"])
    daily_tasks = ((task_res.get("data") or {}).get("dailyTask") or []) if isinstance(task_res, dict) else []
    if not isinstance(daily_tasks, list):
        return {"success": False, "stage": "get_task", "taskResponse": task_res}
    actions: dict[str, Any] = {}
    posts_cache: list[dict[str, Any]] = []
    for task in daily_tasks:
        remark = str(task.get("remark", ""))
        need_runs = task.get("completeTimes") != task.get("needActionTimes")
        if "签到" in remark and need_runs:
            actions["sign"] = bbs_do_sign(context["token"], context["did"], context["b_at"])
        elif "浏览" in remark and need_runs:
            if not posts_cache:
                posts_res = bbs_get_posts(context["token"], context["did"], context["b_at"])
                posts_cache = ((posts_res.get("data") or {}).get("postList") or []) if isinstance(posts_res, dict) else []
                actions["postList"] = posts_res
            if posts_cache:
                actions["detail"] = bbs_do_post_detail(context["token"], context["did"], str(posts_cache[0].get("postId", "")))
        elif "点赞" in remark and need_runs:
            if not posts_cache:
                posts_res = bbs_get_posts(context["token"], context["did"], context["b_at"])
                posts_cache = ((posts_res.get("data") or {}).get("postList") or []) if isinstance(posts_res, dict) else []
                actions["postList"] = posts_res
            if posts_cache:
                post = posts_cache[0]
                actions["like"] = bbs_do_like(context["token"], context["did"], context["b_at"], str(post.get("postId", "")), str(post.get("userId", "")))
        elif "分享" in remark and need_runs:
            actions["share"] = bbs_do_share(context["token"], context["did"], context["b_at"])
    return {"success": True, "mode": "bbs", "taskResponse": task_res, "actions": actions, "context": {k: context[k] for k in ("roleId", "serverId", "roleName")}}


def create_session(owner_key: str = "", notify_umo: str = "") -> str:
    sid = secrets.token_hex(16)
    with SESSIONS_LOCK:
        SESSIONS[sid] = {
            "h5_devcode": uuid.uuid4().hex,
            "did": str(uuid.uuid4()).upper(),
            "owner_key": owner_key,
            "notify_umo": notify_umo,
        }
    return sid


def get_session_operation_lock(sid: str) -> threading.Lock:
    with SESSION_OPERATION_LOCKS_LOCK:
        lock = SESSION_OPERATION_LOCKS.get(sid)
        if lock is None:
            lock = threading.Lock()
            SESSION_OPERATION_LOCKS[sid] = lock
        return lock


def get_owner_operation_lock(owner_key: str) -> threading.Lock:
    with OWNER_OPERATION_LOCKS_LOCK:
        lock = OWNER_OPERATION_LOCKS.get(owner_key)
        if lock is None:
            lock = threading.Lock()
            OWNER_OPERATION_LOCKS[owner_key] = lock
        return lock


def get_or_create_session(handler: BaseHTTPRequestHandler) -> tuple[str, bool]:
    cookie = SimpleCookie(handler.headers.get("Cookie"))
    sid = cookie["sid"].value if "sid" in cookie else ""
    with SESSIONS_LOCK:
        if sid in SESSIONS:
            return sid, False
    return create_session(), True
