#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex 归档管理器（图形界面版）
================================
一个本机网页界面的 Codex 会话管理器：

- 同时管理【未归档】和【已归档】的会话
- 归档 / 取消归档 / 删除 / 导出 Markdown / 打开对话 / 搜索
- 显示每把"会话写锁"的状态，支持"强制归档（先解锁/被占用时临时挪锁）"
- 删除前自动留快照，所有动作写日志

技术：Python 标准库 http.server + 自带网页，不装任何第三方包，
不联网、只绑定本机 127.0.0.1，安全。

双击桌面「Codex 归档管理器」图标即可使用（后台无窗口，关掉网页自动退出）。
"""

import glob
import http.server
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

try:
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _print(*a, **kw):
    """pythonw（无控制台）下 stdout 是 None，打印不能让它崩掉。"""
    try:
        if sys.stdout is not None:
            print(*a, **kw)
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backup"
EXPORT_DIR = BASE_DIR / "exports"
WEBUI_DIR = BASE_DIR / "webui"
LOG_FILE = LOG_DIR / "manager_gui.log"

USER_HOME = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (USER_HOME / ".codex"))
LOCK_DIR = CODEX_HOME / "thread-writer-locks"
ARCHIVED_DIR = CODEX_HOME / "archived_sessions"
SESSIONS_DIR = CODEX_HOME / "sessions"

# 永不触碰的会话 ID（当前正在用的对话）。多个用逗号分隔。
# 设置环境变量 CODEX_ARCHIVE_PROTECTED="id1,id2" 即可，留空表示不保护任何会话。
PROTECTED_IDS = {
    x.strip() for x in os.environ.get("CODEX_ARCHIVE_PROTECTED", "").split(",") if x.strip()
}

GRACE_SECONDS = 120          # 锁/会话静置多久才认为是"僵尸锁"
DEFAULT_PORT = 8765
TAIL_CHUNK = 16 * 1024 * 1024
APP_TAG = "codex-archive-manager"
PID_FILE = BASE_DIR / "manager.pid"
HEARTBEAT_TIMEOUT = 180.0   # 兜底：3 分钟没心跳才认为页面已消失
                            # （正常关闭网页有即时通知，不等这个时间）
CLOSE_GRACE = 10.0          # 页面全关后再等 10 秒（刷新页面时不会误退）
# 可选的只读会话查看器（Sessions Viewer，第三方程序，需自行安装）。
# 用环境变量 SESSIONS_VIEWER_PATH 指向它的 exe；不配置则该按钮给出提示，不影响其他功能。
SESSIONS_VIEWER = os.environ.get("SESSIONS_VIEWER_PATH", "").strip()

LONG_ACTIONS = ("/api/archive", "/api/archive-held", "/api/unarchive",
                "/api/delete", "/api/export", "/api/backup",
                "/api/archive-all-zombies")


def log(msg):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = "[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ---- 网页心跳：所有页面都关掉后，后台进程自动退出 ----
_clients = {}          # 页面随机id -> 最后一次心跳时间
_clients_lock = threading.Lock()
_had_client = False
_busy = 0


def note_client(cid):
    global _had_client
    with _clients_lock:
        _clients[cid] = time.time()
        _had_client = True


def drop_client(cid):
    global _had_client
    with _clients_lock:
        _clients.pop(cid, None)
        # 只要开过页面就算“有过客户端”，即使它还没来得及发心跳就被关掉，
        # 后台进程也要能自动退出，不会一直挂在后台。
        _had_client = True


def had_client():
    with _clients_lock:
        return _had_client


def busy_enter():
    global _busy
    with _clients_lock:
        _busy += 1


def busy_leave():
    global _busy
    with _clients_lock:
        _busy -= 1


def shutdown_watcher(httpd):
    """所有网页都关掉后，过几秒自动退出，免得后台一直挂着一个进程。
    刷新页面 / 正在执行归档时都不会误退。
    """
    last_check = time.time()
    while True:
        time.sleep(2.0)
        now = time.time()
        # 电脑睡眠/休眠时线程也会被挂起，唤醒后时间会突然跳很大一截。
        # 这种情况下的旧时间戳不能当成“网页已经关了”，先把它们刷新一遍。
        if now - last_check > 30.0:
            with _clients_lock:
                for c in list(_clients):
                    _clients[c] = now
            last_check = now
            continue
        last_check = now
        with _clients_lock:
            dead = [c for c, ts in _clients.items() if now - ts > HEARTBEAT_TIMEOUT]
            for c in dead:
                _clients.pop(c, None)
            n = len(_clients)
            busy = _busy
        if busy or n or not _had_client:
            continue
        time.sleep(CLOSE_GRACE)
        with _clients_lock:
            if _clients or _busy:
                continue
        log("AUTO-STOP 页面已全部关闭，后台进程退出")
        try:
            httpd.shutdown()
        except Exception:
            pass
        return


def find_state_db():
    cands = list(CODEX_HOME.glob("state_*.sqlite"))
    cands = [c for c in cands if ".bak" not in c.name]
    if not cands:
        return None

    def num(p):
        try:
            return int(p.stem.split("_")[1])
        except Exception:
            return -1
    return max(cands, key=num)


def find_codex():
    p = shutil.which("codex")
    if p:
        return p
    local = USER_HOME / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    try:
        cands = glob.glob(str(local / "*" / "codex.exe"))
    except OSError:
        cands = []
    if cands:
        return max(cands, key=os.path.getmtime)
    try:
        cands = glob.glob(r"C:\Program Files\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe")
    except OSError:
        cands = []
    if cands:
        return max(cands, key=os.path.getmtime)
    return None


def run_codex(args, dry=False, timeout=180):
    """调用官方 codex CLI，返回 (rc, out, err)。"""
    exe = find_codex()
    if not exe:
        return 127, "", "找不到 codex.exe"
    env = os.environ.copy()
    env["HOME"] = env.get("USERPROFILE") or env.get("HOME") or str(USER_HOME)
    cmd = [exe] + list(args)
    log("RUN %s%s" % ("[dry] " if dry else "", " ".join(args)))
    if dry:
        return 0, "[dry-run] codex %s" % " ".join(args), ""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        log("TIMEOUT %s" % " ".join(args))
        return 4, "", "codex 命令超过 %d 秒未返回" % timeout
    except OSError as exc:
        log("OSERROR %s %r" % (" ".join(args), exc))
        return 5, "", "无法启动 codex：%r" % (exc,)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        log("FAIL exit=%d err=%s" % (proc.returncode, (err or out)[-300:]))
    else:
        log("OK exit=0")
    return proc.returncode, out, err


# ------------------------------------------------------------------ 会话数据

SQL = """
SELECT id, archived, archived_at, updated_at, updated_at_ms, recency_at_ms,
       name, title, preview, first_user_message, cwd, rollout_path, tokens_used
FROM threads
"""


def norm_path(s):
    return (s or "").replace("\\\\?\\", "").replace("\\?\\", "")


class Thread:
    __slots__ = ("id", "archived", "archived_at", "ts", "title", "cwd",
                 "rollout", "tokens")

    def __init__(self, row):
        (tid, archived, archived_at, updated_s, updated_ms, recency_ms,
         name, title, preview, first_msg, cwd, rollout, tokens) = row
        self.id = tid
        self.archived = bool(archived)
        self.archived_at = archived_at
        t = (name or "").strip() or (title or "").strip() or (preview or "").strip() \
            or (first_msg or "").strip() or "(无标题)"
        self.title = " ".join(t.split())
        self.cwd = norm_path(cwd)
        self.rollout = norm_path(rollout)
        self.tokens = tokens or 0
        ms = recency_ms or updated_ms or (updated_s or 0) * 1000
        self.ts = ms / 1000.0 if ms else 0

    def to_dict(self):
        return {
            "id": self.id,
            "short": self.id[:8],
            "archived": self.archived,
            "title": self.title,
            "cwd": self.cwd,
            "cwd_name": Path(self.cwd.rstrip("\\/")).name if self.cwd else "",
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M") if self.ts else "?",
            "tokens": self.tokens,
            "rollout_exists": bool(self.rollout) and Path(self.rollout).exists(),
        }


def load_threads():
    db = find_state_db()
    if not db:
        return [], "找不到 state_*.sqlite"
    try:
        uri = "file:%s?mode=ro" % db.as_posix()
        con = sqlite3.connect(uri, uri=True, timeout=8)
    except sqlite3.OperationalError:
        con = sqlite3.connect(str(db), timeout=8)
    try:
        rows = [Thread(r) for r in con.execute(SQL).fetchall()]
    except sqlite3.OperationalError as exc:
        return [], "读取 threads 表失败：%s" % exc
    finally:
        try:
            con.close()
        except Exception:
            pass
    rows.sort(key=lambda t: t.ts, reverse=True)
    return rows, None


def lock_map():
    """返回 {会话id: 锁文件路径}。"""
    out = {}
    if not LOCK_DIR.is_dir():
        return out
    for p in glob.glob(str(LOCK_DIR / "*.lock")):
        name = os.path.basename(p)
        if name.startswith("."):
            continue
        tid = name[:-len(".lock")]
        out[tid] = p
    return out


def tail_state(rollout_path):
    """从 rollout 末尾判断：IDLE / BUSY / UNKNOWN"""
    try:
        with open(rollout_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_CHUNK))
            raw = f.read(TAIL_CHUNK)
        text = raw.decode("utf-8", "replace")
    except Exception:
        return "UNKNOWN"
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "event_msg":
            continue
        ev = (obj.get("payload") or {}).get("type")
        if ev == "task_started":
            return "BUSY"
        if ev in ("task_complete", "turn_aborted"):
            return "IDLE"
    return "UNKNOWN"


# ---- Windows 独占探测：判断锁文件是不是正被某个进程"攥着" ----
_IS_WINDOWS = os.name == "nt"
if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes as _wt
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, ctypes.c_void_p,
                                 _wt.DWORD, _wt.DWORD, _wt.HANDLE]
    _k32.CreateFileW.restype = _wt.HANDLE
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value if _IS_WINDOWS else -1


def is_lock_held(path):
    """True=有进程正占用；False=文件存在但没人占用（残留锁）；None=探测失败/非Windows。"""
    if not path or not os.path.exists(path):
        return False
    if not _IS_WINDOWS:
        return None
    h = _k32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err in (32, 33):   # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
            return True
        return None
    _k32.CloseHandle(h)
    return False


def lock_status(t, lock_path):
    """给一条会话 + 它的锁，计算界面要显示的锁状态。"""
    now = time.time()
    if t.id in PROTECTED_IDS:
        return {
            "state": "protected",
            "label": "当前对话·受保护",
            "detail": "这是你正在使用的当前对话，为防止误删误归档，已锁定保护。",
            "unlockable": False,
        }
    if not lock_path or not os.path.exists(lock_path):
        return {"state": "none", "label": "", "detail": "", "unlockable": False}
    try:
        lock_age = now - os.path.getmtime(lock_path)
    except OSError:
        lock_age = 0

    # 第一步也是最重要的一步：锁是不是正被某个 Codex 进程占用。
    # 被占用 = 独占打开会报 os error 32；此时归档必然失败，且锁不能删。
    held = is_lock_held(lock_path)
    if held is True:
        return {
            "state": "held",
            "label": "被其他窗口占用·可临时挪锁归档",
            "detail": "这个会话正被一个 Codex 窗口/进程占用写锁。"
                      "点“仍要归档”后，程序会先备份并把锁临时改名挪开，"
                      "调用官方归档，再把锁放回原位；不会关闭你的窗口。",
            "unlockable": False, "held": True, "can_park": True, "lock_age": lock_age,
        }
    if held is None:
        return {
            "state": "unknown",
            "label": "锁状态无法判断·请稍后",
            "detail": "系统没能判断这把锁有没有被占用，为安全起见不提供解锁，请稍后再刷新。",
            "unlockable": False, "held": None, "lock_age": lock_age,
        }

    rollout = Path(t.rollout) if t.rollout else None
    if not rollout or not rollout.exists():
        return {
            "state": "orphan",
            "label": "残留锁·会话文件缺失",
            "detail": "锁没被进程占用，但数据库里记录的会话文件不在。"
                      "可以先点“仅解锁”把残留锁清掉（会备份）。",
            "unlockable": True, "held": False, "lock_age": lock_age,
        }
    try:
        rollout_age = now - os.path.getmtime(str(rollout))
    except OSError:
        rollout_age = 0
    st = tail_state(str(rollout))
    if st == "BUSY":
        return {
            "state": "busy",
            "label": "最近一次记录是开始·锁不可动",
            "detail": "会话文件最后一个关键事件是 task_started，看起来还在跑或上次异常结束。"
                      "为安全起见不解锁；可以等 2 分钟后再刷新，仍显示此状态再点“强制归档”。",
            "unlockable": False, "held": False, "lock_age": lock_age,
        }
    if st == "UNKNOWN":
        if lock_age >= GRACE_SECONDS and rollout_age >= GRACE_SECONDS:
            return {
                "state": "stale",
                "label": "残留锁·状态未知（可强制归档）",
                "detail": "锁没被进程占用，而且已经静置超过 2 分钟。"
                          "可以点“强制归档”，程序会先备份残留锁再归档。",
                "unlockable": True, "held": False, "lock_age": lock_age,
            }
        return {
            "state": "settling",
            "label": "锁没被占用·刚活动过",
            "detail": "锁没被占用，但会话/锁不到 2 分钟前还在动，先等一等再解锁更稳妥。",
            "unlockable": False, "held": False, "lock_age": lock_age,
        }
    if lock_age < GRACE_SECONDS or rollout_age < GRACE_SECONDS:
        wait = max(0, GRACE_SECONDS - min(lock_age, rollout_age))
        return {
            "state": "settling",
            "label": "残留锁·刚活动过",
            "detail": "会话已经跑完，但最近还在动（还剩约 %d 秒）。"
                      "等一小会儿再刷新，就能安全清掉这把残留锁。" % int(wait + 0.5),
            "unlockable": False, "held": False, "lock_age": lock_age,
            "wait": int(wait + 0.5),
        }
    return {
        "state": "zombie",
        "label": "残留锁·可安全解锁",
        "detail": "会话已跑完、锁没被任何进程占用，是残留锁。解锁后归档就不会再报错。",
        "unlockable": True, "held": False, "lock_age": lock_age,
    }


def collect_state():
    rows, err = load_threads()
    if err:
        return {"ok": False, "error": err}
    locks = lock_map()
    threads = []
    for t in rows:
        d = t.to_dict()
        ls = lock_status(t, locks.get(t.id))
        d["lock"] = ls
        d["protected"] = t.id in PROTECTED_IDS
        threads.append(d)
    n_active = sum(1 for t in threads if not t["archived"])
    n_arch = sum(1 for t in threads if t["archived"])
    n_zombie = sum(1 for t in threads if t["lock"].get("unlockable"))
    n_busy = sum(1 for t in threads if t["lock"]["state"] == "busy")
    n_held = sum(1 for t in threads if t["lock"]["state"] == "held")
    return {
        "ok": True,
        "threads": threads,
        "stats": {
            "active": n_active, "archived": n_arch,
            "total": len(threads), "zombie": n_zombie, "busy": n_busy,
            "held": n_held,
        },
        "codex": find_codex() or "",
        "db": str(find_state_db() or ""),
        "now": time.time(),
    }


# ------------------------------------------------------------------ 动作

def unlock_thread(tid, force=False, manual=False):
    """对一条会话解锁。返回 (ok, message)。

    manual=True  : 用户在界面上点了"强制解锁"，跳过空闲判定（仍跳过受保护会话）
    force=True   : 内部使用（A 的"强制归档"链），同样跳过空闲判定
    否则按空闲判定，只有"僵尸锁"才解锁。
    """
    if tid in PROTECTED_IDS:
        return False, "这是当前对话，受保护，不能解锁/归档/删除。"
    lock_path = LOCK_DIR / (tid + ".lock")
    if not lock_path.exists():
        return True, "本来就没有锁，无需解锁。"
    if is_lock_held(str(lock_path)) is True:
        return False, ("这把锁正被另一个 Codex 窗口/进程占用，不能删。"
                       "请先关闭正在使用该会话的窗口，或等它这一轮跑完，再点归档。")
    if not (force or manual):
        rows, err = load_threads()
        if err:
            return False, err
        target = next((t for t in rows if t.id == tid), None)
        if not target:
            return False, "数据库里找不到这条会话"
        ls = lock_status(target, str(lock_path))
        if not ls.get("unlockable"):
            return False, "该会话当前状态不允许自动解锁（%s）" % ls.get("label", "")
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        lock_bak = BACKUP_DIR / ("lock_%s_%s.lock" % (datetime.now().strftime("%Y%m%d-%H%M%S"), tid))
        shutil.copy2(str(lock_path), str(lock_bak))
        os.remove(str(lock_path))
        log("UNLOCK %s by=%s backup=%s" % (tid, "manual" if manual else "auto", lock_bak.name))
        return True, "已解锁（锁文件已备份）"
    except PermissionError:
        log("UNLOCK-BUSY %s" % tid)
        return False, ("这把锁正被另一个 Codex 窗口/进程占用，删不掉。"
                       "请先关闭那个窗口，或等它跑完，再回来点归档。")
    except Exception as exc:
        log("UNLOCK-FAIL %s %r" % (tid, exc))
        return False, "解锁失败：%r" % (exc,)


def restore_lock(tid):
    """从最近的备份恢复某个会话的锁（回滚用）。"""
    cands = sorted(glob.glob(str(BACKUP_DIR / ("lock_%s_*.lock" % tid))), reverse=True)
    if not cands:
        return False, "没有找到这个会话的锁备份"
    lock_path = LOCK_DIR / (tid + ".lock")
    if lock_path.exists():
        return True, "锁本来就在，无需恢复"
    try:
        shutil.copy2(cands[0], str(lock_path))
        log("RESTORE-LOCK %s <- %s" % (tid, os.path.basename(cands[0])))
        return True, "锁已恢复"
    except Exception as exc:
        return False, "恢复失败：%r" % (exc,)


def archive_held(tid):
    """被占用的锁：备份 -> 临时改名挪开 -> 官方归档 -> 清掉挪走的旧锁。

    不关闭窗口、不结束进程。锁文件以 FILE_SHARE_DELETE 打开，所以改名时
    原窗口持有的句柄不受影响。注意：官方 archive / unarchive 都要求原位置
    没有同名锁文件，所以归档成功后不能把锁放回去，只能清掉挪走的那份
    （备份已经留在 backup 目录）。
    """
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", tid or ""):
        return {"ok": False, "message": "会话 id 不合法"}
    if tid in PROTECTED_IDS:
        return {"ok": False, "message": "这是当前对话，受保护，不能挪锁归档。"}

    lock_path = LOCK_DIR / (tid + ".lock")
    if not lock_path.exists():
        return action_archive(tid)

    held = is_lock_held(str(lock_path))
    if held is not True:
        if held is False:
            return action_archive(tid, force=True)
        return {"ok": False, "message": "系统无法确认这把锁是否被占用，为安全起见没有动它。"}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    parked = lock_path.with_name(lock_path.name + ".parked-" + stamp)
    if parked.exists():
        parked = lock_path.with_name(lock_path.name + ".parked-%s-%d" % (stamp, os.getpid()))
    backup = BACKUP_DIR / ("lock_%s_%s.held-backup.lock" % (tid, stamp))
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    moved = False
    retained_copy = None
    parked_removed = False
    archive_ok = False
    out = err = ""
    note = ""
    try:
        try:
            shutil.copy2(str(lock_path), str(backup))
        except OSError as exc:
            # 真实 Codex 进程可能以独占读方式持有这把锁，导致读不出来。
            # 锁本身固定是 0 字节标记文件，读不出来时写一个空备份文件留痕即可。
            log("PARK-BACKUP-COPY-SKIP %s %r" % (tid, exc))
            with open(str(backup), 'wb'):
                pass
        os.replace(str(lock_path), str(parked))
        moved = True
        log("PARK-LOCK %s -> %s (backup=%s)" % (tid, parked.name, backup.name))

        rc, out, err = run_codex(["archive", tid])
        archive_ok = (rc == 0)
        if not archive_ok:
            note = (err or out or "归档失败").splitlines()[-1]
    except OSError as exc:
        log("PARK-FAIL %s %r" % (tid, exc))
        note = "挪开锁失败：%s" % exc
    finally:
        if moved:
            if archive_ok:
                # 官方 archive / unarchive 都要求原位置没有同名锁文件，
                # 所以归档成功后不能再把锁放回去；挪走的那份直接清掉（备份已留）。
                try:
                    os.remove(str(parked))
                    parked_removed = True
                    moved = False
                    log("UNPARK-DEL %s %s" % (tid, parked.name))
                except OSError as exc:
                    log("UNPARK-DEL-FAIL %s %r" % (tid, exc))
                    note = (note + "；" if note else "") + "归档已执行，但挪走的旧锁没清掉：%s（副本在 %s）" % (exc, parked)
            else:
                # 归档没成功：把锁放回原处，恢复原状。
                try:
                    if lock_path.exists():
                        # 归档期间原窗口可能又建了同名锁；不覆盖新锁，保留挪走的那份副本。
                        keep = parked.with_name(parked.name + ".kept")
                        os.replace(str(parked), str(keep))
                        retained_copy = keep
                        moved = False
                        note = (note + "；" if note else "") + "原位置已有新锁，旧锁副本保留在 %s" % keep
                        log("UNPARK-KEEP %s -> %s" % (tid, keep.name))
                    else:
                        os.replace(str(parked), str(lock_path))
                        moved = False
                        log("UNPARK-LOCK %s <- %s" % (tid, parked.name))
                except OSError as exc:
                    log("UNPARK-FAIL %s %r" % (tid, exc))
                    note = (note + "；" if note else "") + "归档失败，且锁没放回：%s（副本在 %s）" % (exc, parked)

    if archive_ok:
        msg = out or "已归档"
        if retained_copy is not None:
            msg += "（归档成功；原位置已有新锁，旧锁副本保留在 %s）" % retained_copy
        elif parked_removed:
            msg += "（已临时挪开写锁完成归档；挪走的旧锁已清掉，备份留在 backup\\%s，窗口未关闭）" % backup.name
        else:
            msg += "（已临时挪开写锁完成归档，窗口未关闭）"
        if note:
            msg += "；" + note
        return {"ok": True, "message": msg, "refresh": True}
    return {"ok": False, "message": note or "归档失败", "rc": 1}


def action_archive(tid, force=False):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", tid or ""):
        return {"ok": False, "message": "会话 id 不合法"}
    if tid in PROTECTED_IDS:
        return {"ok": False, "message": "这是当前对话，受保护，不能归档。"}
    if force:
        ok, msg = unlock_thread(tid, manual=True)
        if not ok:
            return {"ok": False, "message": "先解锁失败：" + msg}
    rc, out, err = run_codex(["archive", tid])
    if rc != 0:
        return {"ok": False, "message": (err or out or "归档失败").splitlines()[-1],
                "rc": rc}
    return {"ok": True, "message": out or "已归档", "refresh": True}


def action_unarchive(tid):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", tid or ""):
        return {"ok": False, "message": "会话 id 不合法"}
    if tid in PROTECTED_IDS:
        return {"ok": False, "message": "这是当前对话，受保护，不能取消归档。"}
    lock_path = LOCK_DIR / (tid + ".lock")
    parked = None
    if lock_path.exists():
        # 官方 unarchive 同样要求原位置没有同名锁文件，先把锁挪走。
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        cand = lock_path.with_name(lock_path.name + ".parked-" + stamp)
        parked = cand if not cand.exists() else lock_path.with_name(
            lock_path.name + ".parked-%s-%d" % (stamp, os.getpid()))
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            backup = BACKUP_DIR / ("lock_%s_%s.held-backup.lock" % (tid, stamp))
            try:
                shutil.copy2(str(lock_path), str(backup))
            except OSError:
                with open(str(backup), "wb"):
                    pass
            os.replace(str(lock_path), str(parked))
            log("PARK-LOCK(unarchive) %s -> %s" % (tid, parked.name))
        except OSError as exc:
            log("PARK-FAIL(unarchive) %s %r" % (tid, exc))
            parked = None
    rc, out, err = run_codex(["unarchive", tid])
    if parked is not None:
        try:
            os.remove(str(parked))
            log("UNPARK-DEL(unarchive) %s %s" % (tid, parked.name))
        except OSError as exc:
            log("UNPARK-DEL-FAIL(unarchive) %s %r" % (tid, exc))
    if rc != 0:
        return {"ok": False, "message": (err or out or "取消归档失败").splitlines()[-1],
                "rc": rc}
    msg = out or "已取消归档"
    if parked is not None:
        msg += "（原先占着位置的锁已挪走并清理，备份已留）"
    return {"ok": True, "message": msg, "refresh": True}


def action_delete(tid):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", tid or ""):
        return {"ok": False, "message": "会话 id 不合法"}
    if tid in PROTECTED_IDS:
        return {"ok": False, "message": "这是当前对话，受保护，不能删除。"}
    rows, err = load_threads()
    if err:
        return {"ok": False, "message": err}
    target = next((t for t in rows if t.id == tid), None)
    if target and not target.archived:
        # 顺手先把僵尸锁清掉，避免删除时又被锁挡住
        lp = LOCK_DIR / (tid + ".lock")
        if lp.exists():
            ls = lock_status(target, str(lp))
            if ls.get("unlockable"):
                unlock_thread(tid)
    # 删除不可逆：先留原始副本
    snapshot = None
    if target:
        try:
            snapshot = snapshot_deleted(target)
        except Exception as exc:
            return {"ok": False, "message": "删除前快照失败，已中止：%r" % (exc,)}
    rc, out, err = run_codex(["delete", tid, "--force"])
    if rc != 0:
        return {"ok": False, "message": (err or out or "删除失败").splitlines()[-1],
                "rc": rc}
    return {"ok": True, "message": out or "已删除",
            "snapshot": snapshot.name if snapshot else None, "refresh": True}


def snapshot_deleted(t):
    dest = BACKUP_DIR / "deleted"
    dest.mkdir(parents=True, exist_ok=True)
    src = Path(t.rollout) if t.rollout else None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if src and src.exists():
        target = dest / ("%s_%s_%s" % (t.id[:8], stamp, src.name))
        shutil.copy2(str(src), str(target))
    else:
        target = dest / ("%s_%s_MISSING.txt" % (t.id[:8], stamp))
        target.write_text("会话 %s 的文件未找到\n标题：%s\n路径：%s\n"
                          % (t.id, t.title, t.rollout), encoding="utf-8")
    log("SNAPSHOT-DELETE %s -> %s" % (t.id, target.name))
    return target


ROLE = {"user": "用户", "agent": "助手", "think": "思考"}
INJECT_LEADS = ("<environment_context>", "<skills_instructions>", "<app-context>",
                "<permissions instructions>", "<collaboration_mode>",
                "<user_instructions>", "<INSTRUCTIONS>", "<turn_aborted>",
                "# AGENTS.md")


def rollout_path_of(t):
    return Path(t.rollout) if t.rollout else None


def _iter_rollout_lines(path):
    try:
        with open(str(path), "rb") as f:
            for raw in f:
                try:
                    yield json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
    except OSError:
        return


def read_transcript(path, max_events=4000):
    """从 rollout 提取 [(时间戳, 角色, 文本)]，优先 event_msg。"""
    evs, ris = [], []
    for o in _iter_rollout_lines(path):
        ts = 0.0
        s = o.get("timestamp")
        if isinstance(s, str) and len(s) >= 19:
            try:
                ts = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc).timestamp()
            except ValueError:
                ts = 0.0
        typ = o.get("type")
        pl = o.get("payload") or {}
        if typ == "event_msg":
            et = pl.get("type")
            if et == "user_message":
                evs.append((ts, "user", pl.get("message") or ""))
            elif et == "agent_message":
                evs.append((ts, "agent", pl.get("message") or ""))
            elif et == "agent_reasoning":
                evs.append((ts, "think", pl.get("text") or ""))
        elif typ == "response_item":
            if pl.get("type") != "message":
                continue
            role = pl.get("role")
            if role not in ("user", "assistant"):
                continue
            text = []
            for c in pl.get("content") or []:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    text.append(c["text"])
            txt = "\n".join(text).strip()
            if not txt:
                continue
            if role == "user":
                head = txt.lstrip()
                if head.startswith(INJECT_LEADS):
                    continue
                ris.append((ts, "user", txt))
            else:
                ris.append((ts, "agent", txt))
        if len(evs) + len(ris) > max_events:
            break
    if evs:
        thinks = [e for e in evs if e[1] == "think"]
        if thinks:
            return evs
        # 没有思考事件时，把 response_item 的思考补进去可省略，保持干净
        return evs
    return ris


def export_markdown(t, include_think=True):
    path = rollout_path_of(t)
    if not path or not path.exists():
        return None, "会话文件不存在：%s" % t.rollout
    events = read_transcript(path)
    if not events:
        return None, "未能从会话文件解析出任何内容。"
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|\r\n]+', "_", t.title).strip("_ ")[:60] or t.id[:8]
    out = EXPORT_DIR / ("%s_%s.md" % (t.id[:8], safe))
    lines = ["# %s" % t.title, "",
             "- 会话 ID：`%s`" % t.id,
             "- 状态：%s" % ("已归档" if t.archived else "活动"),
             "- 目录：`%s`" % t.cwd,
             "- 导出时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "- 来源：`%s`" % path.name, "", "---", ""]
    for i, (ts, role, text) in enumerate(events, 1):
        if role == "think" and not include_think:
            continue
        stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else "--"
        if role == "think":
            lines.append("> **思考 %d**（%s）" % (i, stamp))
            lines.append(">")
            for ln in (text or "").split("\n"):
                lines.append("> " + ln)
        else:
            lines.append("## [%d] %s · %s" % (i, ROLE.get(role, role), stamp))
            lines.append("")
            lines.append(text or "")
        lines.append("")
    try:
        out.write_text("\n".join(lines), encoding="utf-8")
    except OSError as exc:
        return None, "写入失败：%r" % (exc,)
    log("EXPORT %s -> %s" % (t.id, out.name))
    return out, None


def open_thread(t):
    """在独立终端窗口用 codex resume 打开会话。"""
    exe = find_codex()
    if not exe:
        return False, "找不到 codex.exe"
    tmp = BASE_DIR / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(USER_HOME)
    bat = tmp / ("resume_%s.bat" % t.id[:8])
    bat.write_text(
        '@echo off\r\nchcp 65001 >nul\r\ntitle Codex %s\r\nset "HOME=%s"\r\n"%s" resume %s\r\necho.\r\necho [会话已退出，按任意键关闭本窗口]\r\npause >nul\r\n'
        % (t.id[:8], home, exe, t.id), encoding="utf-8")
    try:
        subprocess.Popen(["cmd.exe", "/c", "start", "", "cmd.exe", "/c", str(bat)],
                         shell=False, cwd=str(BASE_DIR))
    except OSError as exc:
        return False, "无法启动终端：%r" % (exc,)
    log("OPEN %s" % t.id)
    return True, "已在新窗口打开"


def open_sessions_viewer():
    """打开可选的只读会话查看器（Sessions Viewer，第三方程序）。"""
    if not SESSIONS_VIEWER:
        return False, ("未配置会话查看器。把环境变量 SESSIONS_VIEWER_PATH 指向 "
                       "Sessions Viewer.exe 后可用（可选功能，不影响归档）。")
    p = Path(SESSIONS_VIEWER)
    if not p.exists():
        return False, "找不到会话查看器：%s" % p
    try:
        subprocess.Popen([str(p)], cwd=str(p.parent))
    except OSError as exc:
        return False, "无法启动会话查看器：%r" % (exc,)
    log("OPEN-VIEWER %s" % p)
    return True, "已打开会话查看器（只读浏览/统计，不影响归档）"


def do_backup():
    db = find_state_db()
    if not db:
        return False, "找不到 state 数据库"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / ("%s.%s.sqlite" % (db.stem, stamp))
    con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=8)
    out = sqlite3.connect(str(dst))
    con.backup(out)
    out.close()
    con.close()
    n = 0
    if ARCHIVED_DIR.is_dir():
        dest = BACKUP_DIR / ("archived_sessions.%s" % stamp)
        dest.mkdir(exist_ok=True)
        for f in ARCHIVED_DIR.glob("*.jsonl"):
            shutil.copy2(str(f), str(dest / f.name))
            n += 1
    log("BACKUP %s files=%d" % (dst.name, n))
    return True, "备份完成：%s（归档会话文件 %d 个）" % (dst.name, n)


def unlock_all_zombies():
    rows, err = load_threads()
    if err:
        return 0, err
    locks = lock_map()
    done = 0
    for t in rows:
        lp = locks.get(t.id)
        if not lp:
            continue
        ls = lock_status(t, lp)
        if ls.get("unlockable"):
            ok, _ = unlock_thread(t.id)
            if ok:
                done += 1
    return done, None


# ------------------------------------------------------------------ HTTP 服务

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CodexArchiveManagerGUI/1.0"

    def log_message(self, fmt, *a):
        pass  # 静音：逐条请求日志没有价值

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            return json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            f = WEBUI_DIR / "index.html"
            if not f.exists():
                return self._send(500, "缺少界面文件 webui/index.html", "text/plain; charset=utf-8")
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(collect_state())
        if path == "/api/ping":
            return self._json({"ok": True, "app": APP_TAG})
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        data = self._read_json()
        tid = (data.get("id") or "").strip()
        # 网页心跳 / 页面关闭：用来判断还有没有页面开着
        if path == "/api/heartbeat":
            note_client(str(data.get("cid") or "default")[:64])
            return self._json({"ok": True})
        if path == "/api/close":
            drop_client(str(data.get("cid") or "default")[:64])
            return self._json({"ok": True})
        busy = path in LONG_ACTIONS
        if busy:
            busy_enter()
        try:
            if path == "/api/archive":
                if data.get("held"):
                    return self._json(archive_held(tid))
                return self._json(action_archive(tid, force=bool(data.get("force"))))
            if path == "/api/archive-held":
                return self._json(archive_held(tid))
            if path == "/api/unarchive":
                return self._json(action_unarchive(tid))
            if path == "/api/delete":
                return self._json(action_delete(tid))
            if path == "/api/unlock":
                ok, msg = unlock_thread(tid, manual=True)
                return self._json({"ok": ok, "message": msg, "refresh": True})
            if path == "/api/restore-lock":
                ok, msg = restore_lock(tid)
                return self._json({"ok": ok, "message": msg, "refresh": True})
            if path == "/api/unlock-zombies":
                n, err = unlock_all_zombies()
                if err:
                    return self._json({"ok": False, "message": err})
                return self._json({"ok": True, "message": "已自动解锁 %d 个僵尸锁" % n,
                                   "refresh": True})
            if path == "/api/archive-all-zombies":
                rows, err = load_threads()
                if err:
                    return self._json({"ok": False, "message": err})
                locks = lock_map()
                done, failed = 0, []
                for t in rows:
                    if t.archived or t.id in PROTECTED_IDS:
                        continue
                    lp = locks.get(t.id)
                    if not lp:
                        continue
                    ls = lock_status(t, lp)
                    if not ls.get("unlockable"):
                        continue
                    un, _ = unlock_thread(t.id)
                    if not un:
                        continue
                    rc, out, e = run_codex(["archive", t.id])
                    if rc == 0:
                        done += 1
                    else:
                        failed.append(t.id[:8])
                msg = "已归档 %d 个" % done
                if failed:
                    msg += "，%d 个失败（%s）" % (len(failed), ", ".join(failed))
                return self._json({"ok": True, "message": msg, "refresh": True})
            if path == "/api/export":
                rows, err = load_threads()
                if err:
                    return self._json({"ok": False, "message": err})
                t = next((x for x in rows if x.id == tid), None)
                if not t:
                    return self._json({"ok": False, "message": "找不到这条会话"})
                out, e = export_markdown(t, include_think=bool(data.get("think", True)))
                if e:
                    return self._json({"ok": False, "message": e})
                return self._json({"ok": True, "message": "已导出：%s" % out.name,
                                   "path": str(out)})
            if path == "/api/open":
                rows, err = load_threads()
                if err:
                    return self._json({"ok": False, "message": err})
                t = next((x for x in rows if x.id == tid), None)
                if not t:
                    return self._json({"ok": False, "message": "找不到这条会话"})
                ok, msg = open_thread(t)
                return self._json({"ok": ok, "message": msg})
            if path == "/api/backup":
                ok, msg = do_backup()
                return self._json({"ok": ok, "message": msg})
            if path == "/api/open-viewer":
                ok, msg = open_sessions_viewer()
                return self._json({"ok": ok, "message": msg})
            if path == "/api/reveal":
                # 在资源管理器中打开导出目录或备份目录
                which = data.get("which") or "exports"
                d = EXPORT_DIR if which == "exports" else BACKUP_DIR
                d.mkdir(parents=True, exist_ok=True)
                subprocess.Popen(["explorer", str(d)])
                return self._json({"ok": True, "message": "已打开 %s" % d})
        except Exception as exc:
            log("API-ERROR %s %r" % (path, exc))
            return self._json({"ok": False, "message": "程序内部错误：%r" % (exc,)}, 500)
        finally:
            if busy:
                busy_leave()
        return self._json({"ok": False, "message": "未知接口"}, 404)


def pick_port(preferred=DEFAULT_PORT):
    for port in range(preferred, preferred + 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            return port
        except OSError:
            s.close()
            continue
    return 0


def try_ping(port, timeout=0.6):
    """本机这个端口上是不是正在跑归档管理器。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=timeout) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        return obj.get("app") == APP_TAG
    except Exception:
        return False


def find_running(port_hint=DEFAULT_PORT):
    ports = list(range(port_hint, port_hint + 20))
    ports += [p for p in range(DEFAULT_PORT, DEFAULT_PORT + 20) if p not in ports]
    for port in ports:
        if try_ping(port):
            return port
    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Codex 归档管理器（图形界面版）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-watchdog", action="store_true",
                    help="不启用「页面全关后自动退出」（自测用）")
    ap.add_argument("--check", action="store_true", help="只做自检后退出")
    args = ap.parse_args()

    if args.check:
        st = collect_state()
        if not st.get("ok"):
            _print("FAIL:", st.get("error"))
            return 1
        _print("OK threads=%d active=%d archived=%d cleanable=%d busy=%d held=%d"
              % (st["stats"]["total"], st["stats"]["active"], st["stats"]["archived"],
                 st["stats"]["zombie"], st["stats"]["busy"], st["stats"].get("held", 0)))
        _print("codex:", st["codex"])
        _print("db:", st["db"])
        return 0

    # 已经有一个在跑：不重复启动，直接把网页打开。
    if not args.no_watchdog:
        running = find_running(args.port)
        if running:
            url = "http://127.0.0.1:%d/" % running
            log("ALREADY-RUNNING %s" % url)
            if not args.no_browser:
                webbrowser.open(url)
            return 0

    rows, err = load_threads()
    if err:
        log("启动失败：%s" % err)
        _print("启动失败：%s" % err)
        return 1

    port = pick_port(args.port)
    if not port:
        log("找不到可用端口，无法启动。")
        _print("找不到可用端口，无法启动。")
        return 1
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    log("SERVER START %s pid=%d" % (url, os.getpid()))
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass
    _print("=" * 52)
    _print("  Codex 归档管理器已启动")
    _print("  地址：%s" % url)
    _print("  提示：关掉网页后，这个后台程序会自己退出。")
    _print("=" * 52)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if not args.no_watchdog:
        threading.Thread(target=shutdown_watcher, args=(httpd,), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
                PID_FILE.unlink()
        except Exception:
            pass
        log("SERVER STOP")
    return 0

if __name__ == "__main__":
    sys.exit(main())
