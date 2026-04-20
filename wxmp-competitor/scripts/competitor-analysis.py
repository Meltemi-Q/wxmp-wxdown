#!/usr/bin/env python3
"""
competitor-analysis.py — 微信公众号竞品分析（基于 wxdown / wechat-article-exporter）

用法:
  python3 competitor-analysis.py --daily         # 日报
  python3 competitor-analysis.py --weekly        # 周报
  python3 competitor-analysis.py --account "量子位"  # 分析指定公众号
  python3 competitor-analysis.py --list          # 列出关注的竞品公众号
"""

import sys, os, json, re, argparse, urllib.request, urllib.parse, time, random
from datetime import datetime, timedelta, timezone, time as dt_time
from collections import Counter

# wxdown 配置
WXDOWN_BASE = os.environ.get("WXDOWN_URL", "http://127.0.0.1:8067")
WXDOWN_CONTAINER = os.environ.get("WXDOWN_CONTAINER", "wechat-exporter")

# 关注列表（复用 wxmp-wxdown skill 的 follows.json）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WXDOWN_SKILL_DIR = os.path.join(SCRIPT_DIR, "..", "..", "wxmp-wxdown")
FOLLOWS_FILE = os.path.join(WXDOWN_SKILL_DIR, "follows.json")
# 备选：同级 skill 目录
if not os.path.exists(FOLLOWS_FILE):
    FOLLOWS_FILE = os.path.join(SCRIPT_DIR, "..", "follows.json")

AUTH_KEY_FILE = os.path.join(WXDOWN_SKILL_DIR, ".auth-key")
if not os.path.exists(AUTH_KEY_FILE):
    AUTH_KEY_FILE = os.path.join(SCRIPT_DIR, "..", ".auth-key")

# 频控保护参数（可通过环境变量调整）
REQUEST_DELAY_SEC = float(os.environ.get("WXDOWN_REQUEST_DELAY_SEC", "1.1"))
REQUEST_JITTER_SEC = float(os.environ.get("WXDOWN_REQUEST_JITTER_SEC", "0.35"))
RATE_LIMIT_RETRIES = int(os.environ.get("WXDOWN_RATE_LIMIT_RETRIES", "2"))
RATE_LIMIT_BACKOFF_SEC = float(os.environ.get("WXDOWN_RATE_LIMIT_BACKOFF_SEC", "6"))
COOLDOWN_EVERY_N = int(os.environ.get("WXDOWN_COOLDOWN_EVERY_N", "30"))
COOLDOWN_SEC = float(os.environ.get("WXDOWN_COOLDOWN_SEC", "8"))
MAX_ACCOUNTS = int(os.environ.get("WXDOWN_MAX_ACCOUNTS", "0"))


def _probe_fakeid():
    """取一个已关注账号做真实 article 探针，避免 authkey 假活跃。"""
    follows = load_follows()
    for fakeid in follows.keys():
        if fakeid:
            return fakeid
    return ""


def _validate_auth_key(auth_key):
    """校验 key 是否真的能拉文章，而不只是 authkey 接口返回 code=0。"""
    if not auth_key:
        return False

    req = urllib.request.Request(
        f"{WXDOWN_BASE}/api/public/v1/authkey",
        headers={"Cookie": f"auth-key={auth_key}"}
    )
    try:
        auth_state = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception:
        return False
    if auth_state.get("code") != 0:
        return False

    probe_fakeid = _probe_fakeid()
    if not probe_fakeid:
        req = urllib.request.Request(
            f"{WXDOWN_BASE}/api/public/v1/account?keyword=test&begin=0&size=1",
            headers={"Cookie": f"auth-key={auth_key}"}
        )
        try:
            result = json.loads(urllib.request.urlopen(req, timeout=10).read())
        except Exception:
            return False
        return (result.get("base_resp") or {}).get("ret", -1) != 200003

    params = urllib.parse.urlencode({"fakeid": probe_fakeid, "begin": 0, "size": 1})
    req = urllib.request.Request(
        f"{WXDOWN_BASE}/api/public/v1/article?{params}",
        headers={"Cookie": f"auth-key={auth_key}"}
    )
    try:
        result = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except urllib.error.HTTPError as e:
        try:
            result = json.loads(e.read().decode())
        except Exception:
            return False
    except Exception:
        return False

    base_resp = result.get("base_resp") or {}
    ret = base_resp.get("ret", result.get("code", -1))
    if ret == 0:
        return True

    err_text = f"{ret} {result.get('msg', '')} {base_resp.get('err_msg', '')}".lower()
    return not (
        ret in (200000, 200003)
        or "auth" in err_text
        or "login" in err_text
        or "expire" in err_text
        or "invalid session" in err_text
        or "认证信息无效" in err_text
    )


def load_auth_key():
    """加载 auth-key，支持从容器自动发现，并过滤假活跃 session。"""
    try:
        with open(AUTH_KEY_FILE, "r") as f:
            key = f.read().strip()
            if key and _validate_auth_key(key):
                return key
    except FileNotFoundError:
        pass

    import subprocess
    keys = []
    candidates = []
    commands = [
        "ls /app/.data/kv/cookie/ 2>/dev/null",
        "find /app/.data/kv -maxdepth 3 -type f -printf '%f\n' 2>/dev/null",
        "find /app/.data/kv -maxdepth 3 -type f -size -256c -exec cat {} \; 2>/dev/null",
    ]
    for docker_cmd in [["docker"], ["sudo", "docker"]]:
        for cmd in commands:
            try:
                out = subprocess.run(
                    docker_cmd + ["exec", WXDOWN_CONTAINER, "sh", "-c", cmd],
                    capture_output=True, text=True, timeout=10
                )
            except Exception:
                continue
            if out.returncode != 0 or not out.stdout.strip():
                continue
            if cmd.startswith("ls "):
                candidates.extend([l.strip() for l in out.stdout.strip().split("\n") if l.strip()])
            else:
                candidates.extend(re.findall(r"[0-9a-f]{32}", out.stdout))
    seen = set()
    for key in candidates:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    if keys:
        for key in keys:
            if _validate_auth_key(key):
                os.makedirs(os.path.dirname(os.path.abspath(AUTH_KEY_FILE)), exist_ok=True)
                with open(AUTH_KEY_FILE, "w") as f:
                    f.write(key)
                return key

    try:
        with open(AUTH_KEY_FILE, "w") as f:
            f.write("")
    except Exception:
        pass
    return ""



def api(path, timeout=15):
    """调用 wxdown API"""
    url = f"{WXDOWN_BASE}{path}"
    auth_key = load_auth_key()
    headers = {}
    if auth_key:
        headers["Cookie"] = f"auth-key={auth_key}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"code": e.code, "msg": e.read().decode()[:200]}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def load_follows():
    """加载关注列表"""
    try:
        with open(FOLLOWS_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_articles_for_account(fakeid, size=1):
    """获取指定公众号的文章（带频控重试）"""
    params = urllib.parse.urlencode({"fakeid": fakeid, "begin": 0, "size": size})

    for attempt in range(RATE_LIMIT_RETRIES + 1):
        result = api(f"/api/public/v1/article?{params}")
        ret = (result.get("base_resp") or {}).get("ret", result.get("code", -1))

        if ret == 0:
            articles = result.get("articles", [])
            if articles:
                return articles, "ok"
            return [], "empty"

        err_msg = result.get("msg", (result.get("base_resp") or {}).get("err_msg", ""))
        err_text = f"{ret} {err_msg}".lower()
        if err_msg:
            print(f"[debug] article 拉取失败 fakeid={fakeid} ret={ret} err={err_msg}", file=sys.stderr)

        if ret == 200000 or any(k in err_text for k in ("auth", "login", "expire")):
            return [], "auth_error"

        is_rate_limit = (ret == 200013) or ("freq control" in err_text) or ("too many" in err_text) or ("rate" in err_text)
        if is_rate_limit:
            if attempt < RATE_LIMIT_RETRIES:
                backoff = RATE_LIMIT_BACKOFF_SEC * (attempt + 1) + random.uniform(0, REQUEST_JITTER_SEC)
                time.sleep(backoff)
                continue
            return [], "rate_limited"

        return [], "request_error"

    return [], "request_error"


def get_all_competitor_articles(days=1, size_per_account=1, fixed_range=False, start_index=1, end_index=0):
    """获取所有关注号的最近文章（带错峰与二次补抓）
    
    Args:
        days: 天数（仅在fixed_range=False时使用）
        size_per_account: 每个账号获取文章数
        fixed_range: 是否使用固定时间段（前一天00:00-24:00，北京时间）
    """
    follows = load_follows()
    stats = {"ok": 0, "empty": 0, "auth_error": 0, "request_error": 0, "rate_limited": 0, "recovered": 0}
    if not follows:
        return [], {}, stats

    auth_key = load_auth_key()
    if not auth_key:
        print("[debug] wxdown auth invalid: no article-fetchable auth key", file=sys.stderr)
        return [], {}, {"auth_failed": True}

    # 时间范围计算
    now = datetime.now()
    if fixed_range:
        # 固定时间段：前一天00:00-24:00（北京时间）
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        report_day = (now_bj - timedelta(days=1)).date()
        cutoff_start = datetime(report_day.year, report_day.month, report_day.day, 0, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        cutoff_end = datetime(report_day.year, report_day.month, report_day.day, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))
        print(f"[info] 统计时间范围: {cutoff_start.strftime('%Y-%m-%d %H:%M')} ~ {cutoff_end.strftime('%Y-%m-%d %H:%M')}", file=sys.stderr)
    else:
        # 滚动24小时
        cutoff_start = now - timedelta(days=days)
        cutoff_end = now

    all_articles = []
    account_map = {}
    rate_limited_queue = []

    items = list(follows.items())
    if MAX_ACCOUNTS > 0:
        items = items[:MAX_ACCOUNTS]

    total_accounts = len(items)
    s = max(1, int(start_index or 1))
    e = int(end_index or 0)
    if e <= 0 or e > total_accounts:
        e = total_accounts
    if s > total_accounts:
        items = []
    else:
        items = items[s-1:e]
    print(f"[info] 采集分片: {s}-{e}/{total_accounts}", file=sys.stderr)

    for idx, (fakeid, info) in enumerate(items, start=1):
        name = info.get("name", fakeid)
        account_map[fakeid] = name

        if idx > 1:
            time.sleep(max(0.0, REQUEST_DELAY_SEC + random.uniform(0, REQUEST_JITTER_SEC)))
        if COOLDOWN_EVERY_N > 0 and idx % COOLDOWN_EVERY_N == 0:
            time.sleep(max(0.0, COOLDOWN_SEC + random.uniform(0, REQUEST_JITTER_SEC)))

        articles, status = get_articles_for_account(fakeid, size=size_per_account)
        if status in stats:
            stats[status] += 1
        if status == "rate_limited":
            rate_limited_queue.append((fakeid, info))

        # 每个账号只保留 1 篇头条（满足毯叔"每号只取1篇头条"的要求）
        # 筛选在指定时间范围内的文章
        selected = None
        for art in articles:
            art["_mp_name"] = name
            art["_fakeid"] = fakeid
            pub_time = art.get("update_time", art.get("create_time", 0))
            if isinstance(pub_time, (int, float)) and pub_time > 0:
                pub_dt = datetime.fromtimestamp(pub_time)
            else:
                pub_dt = datetime.now()
            art["_pub_dt"] = pub_dt
            # 检查是否在时间范围内（北京时间）
            pub_dt_bj = pub_dt.replace(tzinfo=timezone(timedelta(hours=8))) if pub_dt.tzinfo is None else pub_dt.astimezone(timezone(timedelta(hours=8)))
            if cutoff_start <= pub_dt_bj <= cutoff_end:
                if selected is None or pub_dt > selected["_pub_dt"]:
                    selected = art
        if selected is not None:
            all_articles.append(selected)

    if rate_limited_queue:
        time.sleep(max(0.0, RATE_LIMIT_BACKOFF_SEC * 2))
        for fakeid, info in rate_limited_queue:
            time.sleep(max(0.0, REQUEST_DELAY_SEC + random.uniform(0, REQUEST_JITTER_SEC)))
            articles, status = get_articles_for_account(fakeid, size=size_per_account)
            if status == "ok":
                stats["recovered"] += 1
                stats["rate_limited"] = max(0, stats["rate_limited"] - 1)
                name = info.get("name", fakeid)
                selected = None
                for art in articles:
                    art["_mp_name"] = name
                    art["_fakeid"] = fakeid
                    pub_time = art.get("update_time", art.get("create_time", 0))
                    if isinstance(pub_time, (int, float)) and pub_time > 0:
                        pub_dt = datetime.fromtimestamp(pub_time)
                    else:
                        pub_dt = datetime.now()
                    art["_pub_dt"] = pub_dt
                    pub_dt_bj = pub_dt.replace(tzinfo=timezone(timedelta(hours=8))) if pub_dt.tzinfo is None else pub_dt.astimezone(timezone(timedelta(hours=8)))
                    if cutoff_start <= pub_dt_bj <= cutoff_end:
                        if selected is None or pub_dt > selected["_pub_dt"]:
                            selected = art
                if selected is not None:
                    all_articles.append(selected)

    all_articles.sort(key=lambda a: a["_pub_dt"], reverse=True)
    return all_articles, account_map, stats


def extract_keywords(titles, top_n=10):
    """从标题中提取高频关键词"""
    stop_words = {"的", "了", "在", "是", "和", "与", "为", "到", "从", "这", "那",
                  "你", "我", "他", "她", "它", "不", "有", "也", "就", "都", "要",
                  "会", "能", "被", "把", "让", "用", "着", "过", "对", "很", "而",
                  "但", "如", "何", "什么", "怎么", "为什么", "如何", "一个", "一种"}
    words = Counter()
    for title in titles:
        clean = re.sub(r'[^\w\u4e00-\u9fff]', ' ', title)
        for n in range(2, 5):
            for i in range(len(clean) - n + 1):
                gram = clean[i:i+n]
                if gram.strip() and gram not in stop_words and not gram.isdigit():
                    if re.search(r'[\u4e00-\u9fff]', gram):
                        words[gram] += 1
    return [(w, c) for w, c in words.most_common(top_n * 3) if c >= 2][:top_n]


def check_auth_expiry_reminder():
    """检查 auth-key 是否即将过期，返回提醒文本或空"""
    auth_key = load_auth_key()
    if not auth_key:
        return "⚠ wxdown 未登录，请扫码登录后才能获取竞品文章。\n"
    if not _validate_auth_key(auth_key):
        return "⚠ wxdown 登录已过期，请重新扫码登录。\n"
    return ""


def generate_daily_report(articles, account_map, stats=None):
    """生成竞品日报 — 按公众号分组，编号列表，含原文链接"""
    now = datetime.now()
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    today_str = now.strftime(f"%m月%d日竞品公众号更新汇总")
    date_str = now.strftime(f"%Y年%m月%d日（{weekdays[now.weekday()]}）")
    report = [today_str]
    report.append(f"日期：{date_str}")
    report.append("监测窗口：前一天00:00-24:00（北京时间）")

    show_stats = stats is not None
    if stats is None:
        stats = {}

    if stats.get("auth_failed"):
        report.append("")
        report.append("⚠ wxdown 认证失败，无法获取文章。请重新扫码登录。\n")

    # 登录状态提醒
    reminder = check_auth_expiry_reminder()
    if reminder:
        report.append("")
        report.append(reminder)

    def append_stats_line():
        if not show_stats:
            return
        ok = stats.get("ok", 0)
        empty = stats.get("empty", 0)
        failed = stats.get("auth_error", 0) + stats.get("request_error", 0) + stats.get("rate_limited", 0)
        report.append("")
        report.append(f"---\n📊 统计：{ok}个账号成功，{empty}个无更新，{failed}个失败（含频控{stats.get('rate_limited',0)}），二次补抓恢复{stats.get('recovered',0)}个")

    if not articles:
        report.append("")
        if not load_auth_key():
            report.append("未登录，无法获取文章。请先扫码登录。")
        else:
            report.append("今日暂无竞品新文章。")
        follows = load_follows()
        if not follows:
            report.append("尚未关注任何竞品公众号。")
        append_stats_line()
        return "\n".join(report)

    # 按公众号分组
    by_account = {}
    for a in articles:
        name = a.get("_mp_name", "未知")
        by_account.setdefault(name, []).append(a)

    report.append(f"共 {len(articles)} 篇更新，来自 {len(by_account)} 个公众号")
    report.append("")

    # 按发文数排序，逐号输出
    idx = 1
    for name, arts in sorted(by_account.items(), key=lambda x: -len(x[1])):
        report.append(f"📌 {name}（{len(arts)}篇）")
        for a in arts:
            title = a.get("title", "无标题")
            digest = a.get("digest", "")
            if not digest:
                digest = title
            elif len(digest) > 100:
                digest = digest[:97] + "..."
            url = a.get("link", a.get("content_url", "") )
            report.append(f"{idx}. {title}")
            if digest != title:
                report.append(f"   内容：{digest}")
            if url:
                report.append(f"   原文链接：{url}")
            idx += 1
        report.append("")

    append_stats_line()
    return "\n".join(report)



def main():
    parser = argparse.ArgumentParser(description="微信公众号竞品分析")
    parser.add_argument("--daily", action="store_true", help="生成日报")
    # --weekly 已停用
    parser.add_argument("--account", type=str, help="分析指定公众号")
    parser.add_argument("--list", action="store_true", help="列出关注的竞品公众号")
    parser.add_argument("--start-index", type=int, default=1, help="分片起始序号（1-based，含）")
    parser.add_argument("--end-index", type=int, default=0, help="分片结束序号（1-based，含；0表示到末尾）")
    args = parser.parse_args()

    if args.list:
        follows = load_follows()
        if follows:
            print(f"关注的竞品公众号（{len(follows)} 个）:\n")
            for fid, info in follows.items():
                print(f"  - {info.get('name', '?')} (ID: {fid})")
        else:
            print("尚未关注任何公众号。")
            print("请先使用 wxmp-wxdown skill 搜索并关注竞品号。")
        return

    if args.daily:
        # 使用固定时间段：前一天00:00-24:00（北京时间）
        articles, account_map, stats = get_all_competitor_articles(days=1, size_per_account=10, fixed_range=True, start_index=args.start_index, end_index=args.end_index)
        print(generate_daily_report(articles, account_map, stats=stats))
    elif args.account:
        follows = load_follows()
        target_fid = None
        for fid, info in follows.items():
            if args.account in info.get("name", ""):
                target_fid = fid
                break
        if not target_fid:
            # 没在 follows 里，直接搜索
            print(f"「{args.account}」不在关注列表中。请先关注后再分析。")
            return
        articles, _status = get_articles_for_account(target_fid, size=20)
        for a in articles:
            a["_mp_name"] = args.account
            pub_time = a.get("update_time", a.get("create_time", 0))
            if isinstance(pub_time, (int, float)) and pub_time > 0:
                a["_pub_dt"] = datetime.fromtimestamp(pub_time)
            else:
                a["_pub_dt"] = datetime.now()
        account_map = {target_fid: args.account}
        print(generate_daily_report(articles, account_map))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
