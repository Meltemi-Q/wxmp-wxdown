#!/usr/bin/env python3
"""
wxdown-manage.py — 微信公众号管理（基于 wechat-article-exporter / wxdown）

用法:
  python3 wxdown-manage.py status                      # 检查登录状态
  python3 wxdown-manage.py login                       # 触发扫码登录
  python3 wxdown-manage.py search "量子位"              # 搜索公众号
  python3 wxdown-manage.py articles <fakeid> [--size N] # 获取文章列表
  python3 wxdown-manage.py download <url> [--format md] # 下载文章内容
  python3 wxdown-manage.py info <fakeid>                # 公众号详情
  python3 wxdown-manage.py follow "名称" <fakeid>       # 添加到关注列表
  python3 wxdown-manage.py unfollow <fakeid>            # 从关注列表移除
  python3 wxdown-manage.py follows                      # 查看关注列表
  python3 wxdown-manage.py latest [--size N]            # 所有关注号的最新文章
  python3 wxdown-manage.py logout                       # 退出登录
"""

import os, re, sys, json, time, subprocess, urllib.request, urllib.parse
from datetime import datetime

WXDOWN_BASE = os.environ.get("WXDOWN_URL", "http://127.0.0.1:8067")
WXDOWN_CONTAINER = os.environ.get("WXDOWN_CONTAINER", "wechat-exporter")

# 关注列表本地存储（wxdown 无内置订阅，用 JSON 文件管理）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FOLLOWS_FILE = os.path.join(SCRIPT_DIR, "..", "follows.json")

# auth-key 本地缓存
AUTH_KEY_FILE = os.path.join(SCRIPT_DIR, "..", ".auth-key")

# 待完成登录的 uuid 文件（扫码后用于完成 bizlogin）
PENDING_UUID_FILE = os.path.join(SCRIPT_DIR, "..", ".pending-uuid")

# QR 码保存路径
# OpenClaw 只允许从 workspace 目录发送媒体文件（/tmp/ 和 skills/ 都会被拦截）
_WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
QR_FILE = os.path.join(_WORKSPACE, "wxdown_qr_latest.png") if os.path.isdir(_WORKSPACE) else "/tmp/wxdown_qr_latest.png"

LOGIN_WATCH_PID_FILE = os.path.join(SCRIPT_DIR, "..", ".pending-login-watch.pid")
LOGIN_WATCH_STATE_FILE = os.path.join(SCRIPT_DIR, "..", ".pending-login-state.json")
LOGIN_WATCH_TIMEOUT_SEC = int(os.environ.get("WXDOWN_LOGIN_WATCH_TIMEOUT_SEC", "150"))
LOGIN_WATCH_INTERVAL_SEC = float(os.environ.get("WXDOWN_LOGIN_WATCH_INTERVAL_SEC", "2"))
LOGIN_WATCH_MESSAGE = "后台自动轮询中（二维码发出后约2分钟内会自动补完成登录，无需再回复‘已扫码’）"
LOGIN_WATCH_LOG_FILE = os.path.join(_WORKSPACE, "wxdown_login_watch.log") if os.path.isdir(_WORKSPACE) else "/tmp/wxdown_login_watch.log"


# ============================================================
# HTTP 工具
# ============================================================

def api(path, method="GET", headers=None, data=None, timeout=15, raw=False, auth_key=None):
    """通用 API 请求"""
    url = f"{WXDOWN_BASE}{path}"
    if headers is None:
        headers = {}

    # 自动附加 auth-key（允许显式指定，避免校验时反复写本地文件）
    if auth_key is None:
        auth_key = load_auth_key()
    if auth_key:
        headers["Cookie"] = f"auth-key={auth_key}"

    body = None
    if data and isinstance(data, dict):
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()
    elif data and isinstance(data, (str, bytes)):
        body = data.encode() if isinstance(data, str) else data

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read()
            if raw:
                return content
            return json.loads(content.decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        try:
            return json.loads(err_body)
        except Exception:
            return {"code": e.code, "msg": f"HTTP {e.code}: {err_body[:200]}"}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


# ============================================================
# Auth-key 管理
# ============================================================

def load_auth_key():
    """从本地文件加载 auth-key"""
    try:
        with open(AUTH_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    return ""


def save_auth_key(key):
    """保存 auth-key 到本地文件"""
    os.makedirs(os.path.dirname(os.path.abspath(AUTH_KEY_FILE)), exist_ok=True)
    with open(AUTH_KEY_FILE, "w") as f:
        f.write(key)


def docker_exec(cmd):
    """执行 docker exec，自动尝试 sudo"""
    for docker_cmd in [["docker"], ["sudo", "docker"]]:
        try:
            out = subprocess.run(
                docker_cmd + ["exec", WXDOWN_CONTAINER, "sh", "-c", cmd],
                capture_output=True, text=True, timeout=10
            )
            if out.returncode == 0:
                return out.stdout
        except Exception:
            continue
    return ""


def discover_auth_keys_from_container():
    """从 wxdown Docker 容器 KV 存储中发现可用的 auth-key"""
    candidates = []

    # 兼容旧版本路径
    output = docker_exec("ls /app/.data/kv/cookie/ 2>/dev/null")
    candidates.extend([line.strip() for line in output.strip().split("\n") if line.strip()])

    # 兼容新版本：KV 目录结构可能变化，尝试从文件名提取 key
    output = docker_exec("find /app/.data/kv -maxdepth 3 -type f -printf '%f\\n' 2>/dev/null")
    for token in re.findall(r"[0-9a-f]{32}", output):
        candidates.append(token)

    # 再兜底：从小文件内容中提取可能的 key
    output = docker_exec("find /app/.data/kv -maxdepth 3 -type f -size -256c -exec cat {} \\; 2>/dev/null")
    for token in re.findall(r"[0-9a-f]{32}", output):
        candidates.append(token)

    keys = []
    seen = set()
    for key in candidates:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _extract_cookie(headers, cookie_name):
    """从响应头中提取 cookie 值（兼容多 Set-Cookie）。"""
    if not headers:
        return ""

    cookie_headers = []
    try:
        cookie_headers.extend(headers.get_all("Set-Cookie", []))
    except Exception:
        pass

    single = headers.get("Set-Cookie", "")
    if single:
        cookie_headers.append(single)

    prefix = f"{cookie_name}="
    for raw in cookie_headers:
        for part in str(raw).split(";"):
            part = part.strip()
            if part.startswith(prefix):
                return part[len(prefix):].strip()
    return ""


def _load_pending_uuid():
    try:
        with open(PENDING_UUID_FILE, "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _save_pending_uuid(uuid_cookie):
    os.makedirs(os.path.dirname(os.path.abspath(PENDING_UUID_FILE)), exist_ok=True)
    with open(PENDING_UUID_FILE, "w") as f:
        f.write(uuid_cookie)


def _cleanup_pending_uuid():
    """清理过期的 pending-uuid 文件"""
    try:
        os.remove(PENDING_UUID_FILE)
    except OSError:
        pass


def _read_login_watch_state():
    try:
        with open(LOGIN_WATCH_STATE_FILE, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_login_watch_state(state, message=""):
    os.makedirs(os.path.dirname(os.path.abspath(LOGIN_WATCH_STATE_FILE)), exist_ok=True)
    with open(LOGIN_WATCH_STATE_FILE, "w") as f:
        json.dump({
            "state": state,
            "message": message,
            "updated_at": int(time.time()),
        }, f, ensure_ascii=False)


def _clear_login_watch_pid():
    try:
        os.remove(LOGIN_WATCH_PID_FILE)
    except OSError:
        pass


def _watcher_running():
    try:
        with open(LOGIN_WATCH_PID_FILE, "r") as f:
            pid = int(f.read().strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        _clear_login_watch_pid()
        return False


def _login_watch_status_text():
    data = _read_login_watch_state()
    if not data:
        return ""
    age = time.time() - float(data.get("updated_at", 0) or 0)
    if age > 900:
        return ""
    state = data.get("state", "")
    message = data.get("message", "")
    if state == "watching":
        return message or LOGIN_WATCH_MESSAGE
    if state in ("expired", "timeout", "error") and message:
        return f"最近一次扫码登录状态：{message}"
    return ""


def _spawn_pending_login_watcher():
    if _watcher_running():
        _write_login_watch_state("watching", LOGIN_WATCH_MESSAGE)
        return False

    os.makedirs(os.path.dirname(os.path.abspath(LOGIN_WATCH_LOG_FILE)), exist_ok=True)
    _write_login_watch_state("watching", LOGIN_WATCH_MESSAGE)
    with open(LOGIN_WATCH_LOG_FILE, "a") as log_fp:
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "__watch-pending-login"],
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            start_new_session=True,
            close_fds=True,
        )
    with open(LOGIN_WATCH_PID_FILE, "w") as f:
        f.write(str(proc.pid))
    return True


def _run_pending_login_watcher():
    timeout_sec = int(os.environ.get("WXDOWN_LOGIN_WATCH_TIMEOUT_SEC", str(LOGIN_WATCH_TIMEOUT_SEC)))
    interval_sec = float(os.environ.get("WXDOWN_LOGIN_WATCH_INTERVAL_SEC", str(LOGIN_WATCH_INTERVAL_SEC)))
    if not _load_pending_uuid():
        _write_login_watch_state("idle", "当前没有待补完成的二维码")
        _clear_login_watch_pid()
        return 0

    _write_login_watch_state("watching", LOGIN_WATCH_MESSAGE)
    try:
        deadline = time.time() + max(5, timeout_sec)
        while time.time() < deadline:
            result = try_complete_pending_login()
            if result == "completed":
                _write_login_watch_state("completed", "扫码登录已自动完成")
                return 0
            if result == "expired":
                _write_login_watch_state("expired", "二维码已过期，请重发二维码")
                return 1
            time.sleep(max(0.5, interval_sec))

        final = try_complete_pending_login()
        if final == "completed":
            _write_login_watch_state("completed", "扫码登录已自动完成")
            return 0
        if final == "expired":
            _write_login_watch_state("expired", "二维码已过期，请重发二维码")
            return 1

        _write_login_watch_state("timeout", "后台自动轮询已结束；若刚完成扫码，可直接发任意指令再次检查，或重发二维码")
        return 2
    except Exception as e:
        _write_login_watch_state("error", f"后台自动轮询异常：{e}")
        raise
    finally:
        _clear_login_watch_pid()


def _attempt_pending_bizlogin(uuid_cookie):
    """对已扫码的 pending uuid 主动补调用 bizlogin，并容忍 cookie 落盘延迟。"""
    print("[debug] 尝试调用 bizlogin 补完成登录...", file=sys.stderr)
    biz_result, biz_headers = _login_request(
        f"{WXDOWN_BASE}/api/web/login/bizlogin",
        method="POST", uuid_cookie=uuid_cookie, timeout=10)
    print(f"[debug] bizlogin result={biz_result}", file=sys.stderr)

    auth_key = _extract_cookie(biz_headers, "auth-key")
    if auth_key:
        save_auth_key(auth_key)
        if _collection_session_ok(auth_key):
            return "completed"

    ret = (biz_result.get("base_resp") or {}).get("ret", biz_result.get("code", -1))
    if ret == 0:
        for wait_sec in (1, 2):
            time.sleep(wait_sec)
            for key in discover_auth_keys_from_container():
                save_auth_key(key)
                if _collection_session_ok(key):
                    return "completed"

    return "waiting"


def try_complete_pending_login():
    """尝试完成之前的扫码登录（用保存的 uuid 调 scan + bizlogin）
    返回: "completed" | "waiting" | "expired" | None
    """
    try:
        with open(PENDING_UUID_FILE, "r") as f:
            uuid_cookie = f.read().strip()
    except FileNotFoundError:
        return None
    if not uuid_cookie:
        return None

    try:
        age = time.time() - os.path.getmtime(PENDING_UUID_FILE)
        if age > 300:
            print(f"[debug] pending-uuid 已超时 ({int(age)}s)", file=sys.stderr)
            _cleanup_pending_uuid()
            return "expired"
    except OSError:
        pass

    scan_result, _ = _login_request(
        f"{WXDOWN_BASE}/api/web/login/scan",
        uuid_cookie=uuid_cookie, timeout=5)
    if not isinstance(scan_result, dict):
        print("[debug] scan API 异常，清理旧 uuid", file=sys.stderr)
        _cleanup_pending_uuid()
        return "expired"

    status = scan_result.get("status", scan_result.get("code", -1))
    print(f"[debug] scan status={status}", file=sys.stderr)

    if status == 0:
        return "waiting"

    if status in (1, 4, 6):
        result = _attempt_pending_bizlogin(uuid_cookie)
        if result == "completed":
            _cleanup_pending_uuid()
            return "completed"
        print(f"[debug] status={status}, 已扫码但登录尚未落盘，保留 pending-uuid", file=sys.stderr)
        return "waiting"

    print(f"[debug] uuid 已过期或异常 (status={status})", file=sys.stderr)
    _cleanup_pending_uuid()
    return "expired"


def _pending_scan_status_text():
    """返回当前待扫码二维码状态说明，便于诊断用户到底有没有扫到。"""
    uuid_cookie = _load_pending_uuid()
    if not uuid_cookie:
        return ""
    scan_result, _ = _login_request(
        f"{WXDOWN_BASE}/api/web/login/scan",
        uuid_cookie=uuid_cookie, timeout=5)
    if not isinstance(scan_result, dict):
        return "当前二维码状态：未知（可能已过期，建议重新推二维码）"
    status = scan_result.get("status", scan_result.get("code", -1))
    if status == 0:
        return "当前二维码状态：未扫码（服务器还没收到扫码）"
    if status in (1, 4, 6):
        return "当前二维码状态：已扫码，系统正在补完成登录；若微信里有“继续/授权/登录”提示就完成它，没有额外提示也属正常"
    return f"当前二维码状态：已过期或异常（status={status}）"



def _probe_fakeid():
    """取一个已关注账号作为真实 article 探针，避免把假活跃 session 当成已登录。"""
    follows = load_follows()
    for fakeid in follows.keys():
        if fakeid:
            return fakeid
    return ""


def _collection_session_ok(auth_key=""):
    """校验当前 key 是否真的能拉文章，而不只是 authkey 接口返回 code=0。"""
    auth_key = auth_key or load_auth_key()
    if not auth_key:
        return False

    auth_state = api("/api/public/v1/authkey", auth_key=auth_key)
    if auth_state.get("code") != 0:
        return False

    probe_fakeid = _probe_fakeid()
    if not probe_fakeid:
        search_result = api("/api/public/v1/account?keyword=test&begin=0&size=1", auth_key=auth_key)
        base_resp = search_result.get("base_resp", {})
        return base_resp.get("ret", -1) != 200003

    params = urllib.parse.urlencode({"fakeid": probe_fakeid, "begin": 0, "size": 1})
    probe_result = api(f"/api/public/v1/article?{params}", auth_key=auth_key)
    base_resp = probe_result.get("base_resp", {})
    ret = base_resp.get("ret", probe_result.get("code", -1))
    if ret == 0:
        return True

    err_text = f"{ret} {probe_result.get('msg', '')} {base_resp.get('err_msg', '')}".lower()
    return not (
        ret in (200000, 200003)
        or "auth" in err_text
        or "login" in err_text
        or "expire" in err_text
        or "invalid session" in err_text
        or "认证信息无效" in err_text
    )


def check_auth():
    """检查 auth-key 是否有效且真实微信 session 可拉文章，返回 True/False。"""
    # 先试本地缓存的 key
    auth_key = load_auth_key()
    if auth_key and _collection_session_ok(auth_key):
        _cleanup_pending_uuid()
        return True

    # 尝试完成待处理的扫码登录
    pending = try_complete_pending_login()
    if pending == "completed":
        time.sleep(1)

    # 从容器 KV 发现 auth-key
    keys = discover_auth_keys_from_container()
    for key in keys:
        save_auth_key(key)
        if _collection_session_ok(key):
            _cleanup_pending_uuid()
            return True

    # 全部无效，清除本地缓存，避免后续继续拿假活跃 key 当真
    save_auth_key("")
    return False


def require_auth():
    """需要登录的操作前调用，未登录则提示并退出。
    关键：如果已有待扫码的 QR，不重复生成新的（防止覆盖 uuid）。
    """
    if check_auth():
        return True

    # 检查是否有待扫码的 QR（check_auth 内已调用 try_complete_pending_login）
    # 再读一次 pending 状态
    pending_exists = False
    try:
        with open(PENDING_UUID_FILE, "r") as f:
            pending_exists = bool(f.read().strip())
    except FileNotFoundError:
        pass

    if pending_exists:
        # 有待扫码的 QR，不要生成新的！
        print("## 等待扫码登录\n")
        print("二维码已生成，请用**已绑定公众号后台管理员/运营者权限的个人微信号**，在微信里扫描聊天中最新的二维码图片。")
        print("（不是扫公众号二维码，也不是任意微信号；需该微信号能登录 mp.weixin.qq.com 后台）\n")
        print("二维码发出后，后台会继续自动轮询补完成登录（约2分钟），无需再回复“已扫码”；若微信里出现“继续 / 授权 / 登录”之类提示，请完成它；如果没有额外提示也正常。")
        return False

    # 没有待处理的登录，生成新 QR
    print("## 未登录或登录已过期\n")
    cmd_login()
    return False


# ============================================================
# 关注列表管理（本地 JSON）
# ============================================================

def load_follows():
    """加载关注列表"""
    try:
        with open(FOLLOWS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_follows(follows):
    """保存关注列表"""
    os.makedirs(os.path.dirname(FOLLOWS_FILE), exist_ok=True)
    with open(FOLLOWS_FILE, "w") as f:
        json.dump(follows, f, ensure_ascii=False, indent=2)


# ============================================================
# 命令实现
# ============================================================

def cmd_status():
    """检查系统状态"""
    print("## wxdown 系统状态\n")

    # 检查服务是否运行
    try:
        req = urllib.request.Request(f"{WXDOWN_BASE}/api/public/v1/authkey")
        urllib.request.urlopen(req, timeout=5)
        print("- 服务: 运行中")
    except urllib.error.HTTPError:
        print("- 服务: 运行中")  # 返回错误码也说明服务在跑
    except Exception:
        print("- 服务: **无法连接**")
        print(f"  请确认 wechat-article-exporter 容器正在运行（{WXDOWN_BASE}）")
        return

    # 检查登录状态（会自动尝试从容器发现 auth-key）
    if check_auth():
        auth_key = load_auth_key()
        print(f"- 公众号后台登录: **有效** (key: {auth_key[:8]}...)")
    else:
        print("- 公众号后台登录: **未登录/已过期**（需要扫码）")
        watch_msg = _login_watch_status_text()
        if watch_msg:
            print(f"  {watch_msg}")
        pending_msg = _pending_scan_status_text()
        if pending_msg:
            print(f"  {pending_msg}")

    # 关注列表
    follows = load_follows()
    print(f"- 关注公众号: {len(follows)} 个")
    if follows:
        names = [info["name"] for info in follows.values()]
        print(f"  {', '.join(names)}")


def _login_request(url, method="GET", uuid_cookie="", timeout=15, raw=False):
    """登录流程专用请求（手动管理 uuid cookie，绕过 Secure 标志限制）"""
    req = urllib.request.Request(url, method=method)
    if uuid_cookie:
        req.add_header("Cookie", f"uuid={uuid_cookie}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if raw:
                return data, resp.headers
            return json.loads(data.decode()), resp.headers
    except urllib.error.HTTPError as e:
        body = e.read().decode() if e.fp else ""
        try:
            return json.loads(body), {}
        except Exception:
            return {"code": e.code, "msg": body[:200]}, {}
    except Exception as e:
        return {"code": -1, "msg": str(e)}, {}


def cmd_login():
    """触发扫码登录流程（非阻塞：生成 QR 后立即返回，不轮询）"""
    print("## 公众号后台扫码登录\n")

    # Step 1: 创建登录会话，从 Set-Cookie 中提取 uuid
    sid = f"{int(time.time())}_{os.getpid()}"
    result, headers = _login_request(
        f"{WXDOWN_BASE}/api/web/login/session/{sid}", method="POST")

    # 提取 uuid cookie（Set-Cookie: uuid=xxx; Path=/; Secure; HttpOnly）
    uuid_cookie = ""
    set_cookie = headers.get("Set-Cookie", "") if headers else ""
    for part in set_cookie.split(";"):
        part = part.strip()
        if part.startswith("uuid="):
            uuid_cookie = part[5:]
            break
    if not uuid_cookie:
        print(f"创建会话失败（无法获取 uuid cookie）")
        print(f"请直接访问 Web UI 扫码: {WXDOWN_BASE}")
        return

    # Step 2: 获取 QR 码（手动携带 uuid cookie）
    qr_data, _ = _login_request(
        f"{WXDOWN_BASE}/api/web/login/getqrcode",
        uuid_cookie=uuid_cookie, raw=True)

    if not qr_data or len(qr_data) < 100:
        print("QR 码获取失败。请直接访问 Web UI 扫码:")
        print(f"  {WXDOWN_BASE}")
        print("\n扫码完成后重新运行 `status` 检查。")
        return

    os.makedirs(os.path.dirname(os.path.abspath(QR_FILE)), exist_ok=True)
    with open(QR_FILE, "wb") as f:
        f.write(qr_data)

    # 保存 uuid，下次请求时自动完成 scan + bizlogin
    os.makedirs(os.path.dirname(os.path.abspath(PENDING_UUID_FILE)), exist_ok=True)
    with open(PENDING_UUID_FILE, "w") as f:
        f.write(uuid_cookie)

    spawned = _spawn_pending_login_watcher()

    print("请用**已绑定公众号后台管理员/运营者权限的个人微信号**，在微信里扫描下方二维码登录：")
    print("（不是扫公众号二维码，也不是任意微信号；需该微信号能登录 mp.weixin.qq.com 后台）\n")
    print(f"![wxdown QR]({QR_FILE})\n")
    if spawned:
        print("二维码已发送，后台已开始自动轮询登录结果（约2分钟），无需再回复“已扫码”；扫描完成后等待系统自动完成登录即可。")
    else:
        print("二维码已发送，后台轮询已在运行中；扫描完成后等待系统自动完成登录即可。")
    print("若微信里出现“继续 / 授权 / 登录”之类提示，请一并完成；如果没有额外提示也正常。")


def cmd_search(keyword, size=10):
    """搜索公众号"""
    if not require_auth():
        return

    params = urllib.parse.urlencode({"keyword": keyword, "begin": 0, "size": size})
    result = api(f"/api/public/v1/account?{params}")

    # wxdown 返回格式: {"base_resp": {"ret": 0}, "list": [...], "total": N}
    # 或 {"code": 0, "data": {"list": [...]}}
    ret = (result.get("base_resp") or {}).get("ret", result.get("code", -1))
    if ret != 0:
        print(f"搜索失败: {result.get('msg', result.get('message', json.dumps(result.get('base_resp', {}), ensure_ascii=False)))}")
        return

    items = result.get("list", [])
    if not items and isinstance(result.get("data"), dict):
        items = result["data"].get("list", [])
    if not items and isinstance(result.get("data"), list):
        items = result["data"]

    if not items:
        print(f"未找到与「{keyword}」相关的公众号。")
        return

    total = result.get("total", len(items))
    print(f"## 搜索「{keyword}」结果（共 {total} 个）\n")
    for i, item in enumerate(items, 1):
        name = item.get("nickname", item.get("mp_name", "未知"))
        fakeid = item.get("fakeid", item.get("mp_id", ""))
        alias = item.get("alias", "")
        signature = item.get("signature", item.get("mp_intro", ""))

        print(f"{i}. **{name}**")
        if alias:
            print(f"   微信号: {alias}")
        if fakeid:
            print(f"   ID: `{fakeid}`")
        if signature:
            print(f"   简介: {signature[:100]}")
        print()

    # 提示关注命令
    if items:
        sample = items[0]
        sample_name = sample.get("nickname", "名称")
        sample_id = sample.get("fakeid", "fakeid")
        print(f"关注示例: `python3 scripts/wxdown-manage.py follow \"{sample_name}\" {sample_id}`")


def cmd_articles(fakeid, size=10, keyword=""):
    """获取指定公众号的文章列表"""
    if not require_auth():
        return

    params = {"fakeid": fakeid, "begin": 0, "size": size}
    if keyword:
        params["keyword"] = keyword
    qs = urllib.parse.urlencode(params)
    result = api(f"/api/public/v1/article?{qs}")

    # wxdown 返回: {"base_resp": {"ret": 0}, "articles": [...]}
    ret = (result.get("base_resp") or {}).get("ret", result.get("code", -1))
    if ret != 0:
        print(f"获取文章失败: {result.get('msg', json.dumps(result.get('base_resp', {}), ensure_ascii=False))}")
        return

    # 文章在 "articles" 字段
    items = result.get("articles", result.get("list", []))
    if not items and isinstance(result.get("data"), (list, dict)):
        items = result["data"] if isinstance(result["data"], list) else result["data"].get("list", [])

    if not items:
        print("该公众号暂无文章。")
        return

    # 查关注列表获取名称
    follows = load_follows()
    mp_name = follows.get(fakeid, {}).get("name", fakeid)

    print(f"## 「{mp_name}」最新文章\n")
    for i, art in enumerate(items, 1):
        title = art.get("title", "无标题")
        digest = art.get("digest", "")
        url = art.get("link", art.get("content_url", ""))
        pub_time = art.get("update_time", art.get("create_time", ""))
        author = art.get("author_name", "")

        if isinstance(pub_time, (int, float)) and pub_time > 0:
            pub_time = datetime.fromtimestamp(pub_time).strftime("%Y-%m-%d %H:%M")

        print(f"{i}. **{title}**")
        if author:
            print(f"   作者: {author}")
        if pub_time:
            print(f"   发布: {pub_time}")
        if digest:
            print(f"   摘要: {digest[:120]}")
        if url:
            print(f"   链接: {url}")
        print()

        if i >= size:
            break


def cmd_download(url, fmt="markdown"):
    """下载单篇文章内容"""
    if not require_auth():
        return

    params = urllib.parse.urlencode({"url": url, "format": fmt})
    result = api(f"/api/public/v1/download?{params}", timeout=30)

    if isinstance(result, dict) and result.get("code") != 0:
        print(f"下载失败: {result.get('msg', '未知错误')}")
        return

    if isinstance(result, dict):
        content = result.get("data", result.get("content", ""))
        title = result.get("title", "")
        if title:
            print(f"# {title}\n")
        print(content if isinstance(content, str) else json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(str(result))


def cmd_info(fakeid):
    """获取公众号详情"""
    if not require_auth():
        return

    result = api(f"/api/public/beta/aboutbiz?fakeid={fakeid}")

    if result.get("code") != 0:
        print(f"获取详情失败: {result.get('msg', '未知错误')}")
        return

    data = result.get("data", result)
    print("## 公众号详情\n")
    for key in ["nickname", "alias", "signature", "principal_name", "service_type", "verify_type_info"]:
        val = data.get(key, "")
        if val:
            label = {
                "nickname": "名称", "alias": "微信号", "signature": "简介",
                "principal_name": "主体", "service_type": "类型",
                "verify_type_info": "认证"
            }.get(key, key)
            print(f"- {label}: {val}")
    print(f"- ID: `{fakeid}`")


def cmd_follow(name, fakeid):
    """添加到关注列表"""
    follows = load_follows()
    follows[fakeid] = {
        "name": name,
        "fakeid": fakeid,
        "added_at": datetime.now().isoformat()
    }
    save_follows(follows)
    print(f"已关注「{name}」(ID: {fakeid})")
    print(f"当前关注 {len(follows)} 个公众号。")


def cmd_unfollow(fakeid):
    """从关注列表移除"""
    follows = load_follows()
    if fakeid in follows:
        name = follows[fakeid].get("name", fakeid)
        del follows[fakeid]
        save_follows(follows)
        print(f"已取消关注「{name}」")
    else:
        print(f"关注列表中没有 ID 为 `{fakeid}` 的公众号。")


def cmd_follows():
    """查看关注列表"""
    follows = load_follows()
    if not follows:
        print("当前没有关注任何公众号。\n")
        print("使用 `search` 搜索公众号，然后 `follow` 添加关注。")
        return

    print(f"## 关注列表（共 {len(follows)} 个）\n")
    for i, (fid, info) in enumerate(follows.items(), 1):
        name = info.get("name", "未知")
        added = info.get("added_at", "")[:10]
        print(f"{i}. **{name}**")
        print(f"   ID: `{fid}`")
        if added:
            print(f"   关注于: {added}")
        print()


def cmd_latest(size=5):
    """获取所有关注号的最新文章"""
    if not require_auth():
        return

    follows = load_follows()
    if not follows:
        print("当前没有关注任何公众号，请先添加关注。")
        return

    print(f"## 关注公众号最新文章\n")
    for fakeid, info in follows.items():
        name = info.get("name", fakeid)
        print(f"### {name}\n")

        params = urllib.parse.urlencode({"fakeid": fakeid, "begin": 0, "size": size})
        result = api(f"/api/public/v1/article?{params}")

        ret = (result.get("base_resp") or {}).get("ret", result.get("code", -1))
        if ret != 0:
            print(f"获取失败\n")
            continue

        items = result.get("articles", result.get("list", []))
        if not items:
            print("暂无文章\n")
            continue

        for i, art in enumerate(items, 1):
            title = art.get("title", "无标题")
            url = art.get("link", art.get("content_url", ""))
            pub_time = art.get("update_time", art.get("create_time", ""))
            if isinstance(pub_time, (int, float)) and pub_time > 0:
                pub_time = datetime.fromtimestamp(pub_time).strftime("%m-%d %H:%M")
            line = f"- {title}"
            if pub_time:
                line += f" ({pub_time})"
            print(line)
            if url:
                print(f"  {url}")
            if i >= size:
                break
        print()


def cmd_logout():
    """退出登录"""
    result = api("/api/web/mp/logout")
    # 清除本地 auth-key
    try:
        os.remove(AUTH_KEY_FILE)
    except FileNotFoundError:
        pass
    print("已退出登录。")


def usage():
    print("""用法:
  wxdown-manage.py status                     系统状态
  wxdown-manage.py login                      扫码登录
  wxdown-manage.py search "关键词"             搜索公众号
  wxdown-manage.py articles <fakeid> [--size N] 文章列表
  wxdown-manage.py download <url> [--format md] 下载文章
  wxdown-manage.py info <fakeid>              公众号详情
  wxdown-manage.py follow "名称" <fakeid>      添加关注
  wxdown-manage.py unfollow <fakeid>          取消关注
  wxdown-manage.py follows                    关注列表
  wxdown-manage.py latest [--size N]          所有关注号最新文章
  wxdown-manage.py logout                     退出登录""")


def main():
    if len(sys.argv) < 2:
        usage()
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "__watch-pending-login":
        sys.exit(_run_pending_login_watcher())
    elif cmd == "status":
        cmd_status()
    elif cmd == "login":
        cmd_login()
    elif cmd == "logout":
        cmd_logout()
    elif cmd == "search":
        if len(sys.argv) < 3:
            print("用法: wxdown-manage.py search \"关键词\"")
            sys.exit(1)
        size = 10
        if "--size" in sys.argv:
            idx = sys.argv.index("--size")
            if idx + 1 < len(sys.argv):
                size = int(sys.argv[idx + 1])
        cmd_search(sys.argv[2], size)
    elif cmd == "articles":
        if len(sys.argv) < 3:
            print("用法: wxdown-manage.py articles <fakeid> [--size N]")
            sys.exit(1)
        size = 10
        keyword = ""
        if "--size" in sys.argv:
            idx = sys.argv.index("--size")
            if idx + 1 < len(sys.argv):
                size = int(sys.argv[idx + 1])
        if "--keyword" in sys.argv:
            idx = sys.argv.index("--keyword")
            if idx + 1 < len(sys.argv):
                keyword = sys.argv[idx + 1]
        cmd_articles(sys.argv[2], size, keyword)
    elif cmd == "download":
        if len(sys.argv) < 3:
            print("用法: wxdown-manage.py download <url> [--format md|html|text]")
            sys.exit(1)
        fmt = "markdown"
        if "--format" in sys.argv:
            idx = sys.argv.index("--format")
            if idx + 1 < len(sys.argv):
                fmt = sys.argv[idx + 1]
        cmd_download(sys.argv[2], fmt)
    elif cmd == "info":
        if len(sys.argv) < 3:
            print("用法: wxdown-manage.py info <fakeid>")
            sys.exit(1)
        cmd_info(sys.argv[2])
    elif cmd == "follow":
        if len(sys.argv) < 4:
            print("用法: wxdown-manage.py follow \"名称\" <fakeid>")
            sys.exit(1)
        cmd_follow(sys.argv[2], sys.argv[3])
    elif cmd == "unfollow":
        if len(sys.argv) < 3:
            print("用法: wxdown-manage.py unfollow <fakeid>")
            sys.exit(1)
        cmd_unfollow(sys.argv[2])
    elif cmd == "follows":
        cmd_follows()
    elif cmd == "latest":
        size = 5
        if "--size" in sys.argv:
            idx = sys.argv.index("--size")
            if idx + 1 < len(sys.argv):
                size = int(sys.argv[idx + 1])
        cmd_latest(size)
    else:
        print(f"未知命令: {cmd}")
        usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
