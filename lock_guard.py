#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Codex 归档锁守护（lock_guard.py）
==================================
作用：让 Codex 桌面版左栏"归档"按钮在没有登录账号时也能正常使用。

原理
----
Codex 会给每个"打开过/跑过"的会话留一把写锁文件：
    C:\\Users\\<你>\\.codex\\thread-writer-locks\\<会话ID>.lock
只要这把锁还在（尤其被 app-server 进程持有），官方归档就会报：
    "already has an active writer" / "failed to open thread writer lock ... os error 32"
桌面左栏点归档就弹红色提示。

重要发现（2026-10-01 真机实验证明）
--------------------------------
锁文件是以 FILE_SHARE_DELETE 方式打开的，所以「被进程持有」的锁也可以
直接改名挪走；改名后 canonical 名称消失，官方归档立刻成功，而持有的
老句柄只指向已经被挪走的那个文件，不影响会话数据。归档完成后再把锁放
回原位即可（或保留 .parked 副本，重启后自然消失）。

本工具提供两种解放方式：
  A. 删僵尸锁（原有逻辑）：锁无人持有且会话已跑完时，直接删除。
  B. park 挪锁（新模式）：锁被进程持有但会话已空闲时，改名挪走，
     让官方归档可以落盘，随后自动放回 / 或保留副本备用。

安全规则（缺一不可）
--------------------
1) 不在"永不清理"白名单里（当前对话在名单里）
2) 会话最后一条关键事件是 task_complete / turn_aborted（= 已跑完）
3) 锁文件、会话文件至少 2 分钟没有更新
正在运行的（BUSY）一律不碰；拿不准（UNKNOWN）的默认跳过，
可以用 --park-tid TID --force 手动处理个别会话。

日志：logs\\lock_guard.log    备份：backup\\locks\\
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import sqlite3
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

USER_HOME = os.environ.get("USERPROFILE") or os.path.expanduser("~")
BASE_DIR = os.path.join(USER_HOME, ".codex")
LOCK_DIR = os.path.join(BASE_DIR, "thread-writer-locks")
STATE_DB = os.path.join(BASE_DIR, "state_5.sqlite")
HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "backup", "locks")
LOG_DIR = os.path.join(HERE, "logs")
LOG_FILE = os.path.join(LOG_DIR, "lock_guard.log")
PID_FILE = os.path.join(LOG_DIR, "lock_guard.pid")

# 永不清理的会话 ID（当前正在用的对话）。多个用逗号分隔。
# 设置环境变量 CODEX_ARCHIVE_PROTECTED="id1,id2" 即可，留空表示不保护任何会话。
NEVER_TOUCH = {
    x.strip() for x in os.environ.get("CODEX_ARCHIVE_PROTECTED", "").split(",") if x.strip()
}
GRACE_SECONDS = 120
DEFAULT_INTERVAL = 15
TAIL_CHUNK = 16 * 1024 * 1024

STATE_LABEL = {
    "PROTECTED": "受保护",
    "BUSY": "正在运行",
    "HELD": "被占用，可挪锁解锁",
    "UNKNOWN": "状态未知",
    "IDLE": "空闲可解锁",
    "MISSING": "找不到会话",
}


def log(msg, echo=True):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[%s] %s" % (stamp, msg)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    if echo:
        print(line)


def fmt_age(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%d秒" % seconds
    if seconds < 3600:
        return "%d分钟" % (seconds // 60)
    if seconds < 86400:
        return "%.1f小时" % (seconds / 3600.0)
    return "%.1f天" % (seconds / 86400.0)


import ctypes
from ctypes import wintypes as _wt

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.argtypes = [_wt.LPCWSTR, _wt.DWORD, _wt.DWORD, ctypes.c_void_p,
                             _wt.DWORD, _wt.DWORD, _wt.HANDLE]
_k32.CreateFileW.restype = _wt.HANDLE
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def is_lock_held(path):
    """True=有进程占用；False=残留；None=探测失败。"""
    if not path or not os.path.exists(path):
        return False
    h = _k32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err in (32, 33):
            return True
        return None
    _k32.CloseHandle(h)
    return False


def tail_state(rollout_path):
    """空闲判定：从文件末尾往前找最后一条关键事件。"""
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


def query_threads():
    info = {}
    try:
        uri = "file:" + STATE_DB.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        cur = con.cursor()
        cur.execute("SELECT id, title, rollout_path, archived FROM threads")
        for tid, title, rollout, archived in cur.fetchall():
            info[tid] = {
                "title": title or "",
                "rollout": rollout or "",
                "archived": archived,
            }
        con.close()
    except Exception as e:
        log("读取数据库失败：%r" % (e,))
    return info


def collect(grace):
    now = time.time()
    threads = query_threads()
    items = []
    for lock_path in sorted(glob.glob(os.path.join(LOCK_DIR, "*.lock"))):
        tid = os.path.basename(lock_path)[:-len(".lock")]
        if tid.startswith("."):
            continue  # .coordination.lock 永远不碰
        try:
            lock_age = now - os.path.getmtime(lock_path)
        except OSError:
            continue
        held = is_lock_held(lock_path)
        row = threads.get(tid)
        item = {
            "tid": tid,
            "lock": lock_path,
            "lock_age": lock_age,
            "title": (row or {}).get("title", ""),
            "rollout": (row or {}).get("rollout", ""),
            "archived": (row or {}).get("archived", 0),
            "held": held,
            "state": "",
            "reason": "",
            "cleanable": False,
            "parkable": False,
            "rollout_age": None,
        }
        if tid in NEVER_TOUCH:
            item["state"] = "PROTECTED"
            item["reason"] = "白名单：当前对话，永不解锁"
            items.append(item)
            continue
        if held is None:
            item["state"] = "UNKNOWN"
            item["reason"] = "无法判断锁是否被占用，为安全起见跳过"
            items.append(item)
            continue
        if row is None:
            item["state"] = "MISSING"
            item["reason"] = "数据库里找不到这个会话（跳过，可手动强制解锁）"
            items.append(item)
            continue
        rollout = item["rollout"]
        if not rollout or not os.path.exists(rollout):
            item["state"] = "MISSING"
            item["reason"] = "会话文件不在（跳过，可手动强制解锁）"
            items.append(item)
            continue
        try:
            item["rollout_age"] = now - os.path.getmtime(rollout)
        except OSError:
            item["rollout_age"] = 0
        st = tail_state(rollout)
        if st == "BUSY":
            item["state"] = "BUSY"
            item["reason"] = "正在运行，绝不解锁"
        elif st == "UNKNOWN":
            item["state"] = "UNKNOWN"
            item["reason"] = "拿不准是否空闲（可手动强制解锁）"
        else:
            if held is True:
                item["state"] = "HELD"
            else:
                item["state"] = "IDLE"
            if lock_age < grace:
                item["reason"] = "锁刚创建（%s前），先等等" % fmt_age(lock_age)
            elif item["rollout_age"] < grace:
                item["reason"] = "会话刚还在动（%s前），先等等" % fmt_age(item["rollout_age"])
            elif held is True:
                item["parkable"] = True
                item["reason"] = ("空闲但锁被占用（已静置%s），可以挪锁解锁"
                                  % fmt_age(item["rollout_age"]))
            else:
                item["cleanable"] = True
                item["reason"] = "空闲（末次事件=task_complete，已静置%s）" % fmt_age(item["rollout_age"])
        items.append(item)
    return items


def _valid_tid(tid):
    if not tid:
        return False
    tid = tid.strip()
    return len(tid) == 36 and all(c in "0123456789abcdefABCDEF-" for c in tid)


def clean_one(item, force=False, dry_run=False):
    """删僵尸锁：只处理无人持有的锁。"""
    if item["tid"] in NEVER_TOUCH:
        log("跳过（白名单）：%s" % item["tid"][:8])
        return False
    if item.get("held") is True:
        log("跳过（被进程占用，不能删；可挪锁）：%s" % item["tid"][:8])
        return False
    if not force and not item["cleanable"]:
        log("跳过：%s %s" % (item["tid"][:8], item["reason"]))
        return False
    lock_path = item["lock"]
    if not os.path.exists(lock_path):
        return False
    if dry_run:
        log("[演练] 会删除僵尸锁：%s | %s | 原因：%s"
            % (item["tid"][:8], (item["title"] or "")[:30], item["reason"]))
        return False
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(BACKUP_DIR, "%s_%s.lock" % (stamp, item["tid"]))
        shutil.copy2(lock_path, backup)
        os.remove(lock_path)
        log("已删僵尸锁：%s | %s | 锁龄%s | 原因：%s | 备份：%s"
            % (item["tid"][:8], (item["title"] or "")[:30],
               fmt_age(item["lock_age"]), item["reason"], os.path.basename(backup)))
        return True
    except Exception as e:
        log("删除失败：%s | %r" % (item["tid"][:8], e))
        return False


def park_one(item, force=False, dry_run=False):
    """park 挪锁：把被进程占用、但会话已空闲的锁改名挪走。

    锁文件是 0 字节标记，且以 FILE_SHARE_DELETE 方式打开，
    改名不需要读内容；改名后 canonical 名消失，官方归档即可成功。
    挪走的文件保留为 <tid>.lock.parked-<时间戳>，随时可以复原。
    """
    tid = item["tid"]
    if tid in NEVER_TOUCH:
        log("跳过（白名单）：%s" % tid[:8])
        return False
    if not _valid_tid(tid):
        log("跳过（ID 不合法）：%s" % tid[:40])
        return False
    if item.get("held") is not True:
        # 不是持有中的锁，交给删除逻辑
        return clean_one(item, force=force, dry_run=dry_run)
    if not force and not item["parkable"]:
        log("跳过（未确认空闲）：%s | 状态=%s | %s"
            % (tid[:8], item["state"], item["reason"]))
        return False
    lock_path = item["lock"]
    if not os.path.exists(lock_path):
        log("跳过（锁已不在）：%s" % tid[:8])
        return False
    if dry_run:
        log("[演练] 会挪锁（park）：%s | %s | 原因：%s"
            % (tid[:8], (item["title"] or "")[:30], item["reason"]))
        return False
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    parked = lock_path + ".parked-" + stamp
    if os.path.exists(parked):
        parked = lock_path + ".parked-%s-%d" % (stamp, os.getpid())
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        # 锁被独占，无法读内容；锁本身固定 0 字节，写一个空备份即可留痕。
        backup = os.path.join(BACKUP_DIR, "%s_%s.held.lock" % (stamp, tid))
        with open(backup, "wb"):
            pass
        os.replace(lock_path, parked)
        gone = not os.path.exists(lock_path)
        log("已挪锁（park）：%s | %s | 锁龄%s | 原因：%s | 挪到：%s | 备份：%s"
            % (tid[:8], (item["title"] or "")[:30], fmt_age(item["lock_age"]),
               item["reason"], os.path.basename(parked),
               os.path.basename(backup)))
        if not gone:
            log("注意：挪锁后原位置又出现了同名锁（可能会话被重新打开），不影响使用。")
        return True
    except Exception as e:
        log("挪锁失败：%s | %r" % (tid[:8], e))
        return False


def unpark_one(tid, dry_run=False):
    """复原：把最近一次 park 的锁改名回 canonical 位置。"""
    if not _valid_tid(tid):
        print("会话 ID 不合法。")
        return False
    if tid in NEVER_TOUCH:
        print("这是受保护的当前对话，不能操作。")
        return False
    lock_path = os.path.join(LOCK_DIR, tid + ".lock")
    if os.path.exists(lock_path):
        print("原锁本来就在，无需复原。")
        return False
    cands = sorted(glob.glob(lock_path + ".parked*") + glob.glob(lock_path + ".*"))
    cands = [c for c in cands if not c.endswith(".lock")]
    if not cands:
        print("没有找到这个会话的挪锁副本。")
        return False
    newest = cands[-1]
    if dry_run:
        print("[演练] 会复原：%s -> %s" % (os.path.basename(newest), tid + ".lock"))
        return False
    try:
        os.replace(newest, lock_path)
        log("已复原锁：%s <- %s" % (tid[:8], os.path.basename(newest)))
        print("已复原。")
        return True
    except Exception as e:
        print("复原失败：%r" % (e,))
        return False


def print_items(items):
    print()
    print("============ Codex 归档锁守护 ============")
    if not items:
        print("  当前没有锁文件，归档应该完全正常。")
    else:
        for i, it in enumerate(items, 1):
            title = (it["title"] or "(无标题)").replace("\n", " ")[:34]
            label = STATE_LABEL.get(it["state"], it["state"])
            print("  [%d] %s  %s" % (i, it["tid"][:8], title))
            print("      状态：%s —— %s" % (label, it["reason"]))
    print("==========================================")
    print("  1. 重新检查")
    print("  2. 一键处理：删僵尸锁 + 挪走空闲占用的锁")
    print("  3. 常驻守护（每 %d 秒自动处理）" % DEFAULT_INTERVAL)
    print("  4. 指定编号强制处理（状态未知时用）")
    print("  5. 复原某个会话的锁（把挪走的锁放回去）")
    print("  0. 退出")
    print()


def _pid_alive(pid):
    """Windows 下判断进程是否还活着（纯标准库，不误杀）。"""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if h:
        k.CloseHandle(h)
        return True
    return False


def _read_pid():
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def another_guard_running():
    pid = _read_pid()
    if pid and pid != os.getpid() and _pid_alive(pid):
        return pid
    return 0


def stop_guard():
    pid = _read_pid()
    if not pid:
        print("没有找到正在运行的守护（没有 PID 记录）。")
        return 1
    if not _pid_alive(pid):
        print("PID 记录里的进程已经不在了，清理记录。")
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
        return 1
    import subprocess
    r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    print((r.stdout or "").strip() or (r.stderr or "").strip())
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return 0


def process_all(grace, dry_run=False, force=False):
    """完整流程：先删僵尸锁，再挪走空闲但被占用的锁。返回 (删除数, 挪锁数)。"""
    items = collect(grace)
    cleaned = parked = 0
    for it in items:
        if it["cleanable"]:
            if dry_run:
                cleaned += 1        # 演练：只统计"本来会处理几个"
            elif clean_one(it, dry_run=False, force=force):
                cleaned += 1
        elif it["parkable"]:
            if dry_run:
                parked += 1
            elif park_one(it, dry_run=False, force=force):
                parked += 1
    return cleaned, parked, items


def watch_mode(grace, interval):
    other = another_guard_running()
    if other:
        print("已经有一个锁守护在运行了（PID %d），不用重复启动。" % other)
        print("如果要重启，请先运行：python lock_guard.py --stop")
        return
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    print()
    print("常驻守护已启动：每 %d 秒检查一次空闲锁（删僵尸 + 挪锁）。" % interval)
    print("关掉本窗口或按 Ctrl+C 就会停止。")
    log("常驻守护启动：interval=%ds grace=%ds pid=%d" % (interval, grace, os.getpid()))
    try:
        while True:
            process_all(grace)
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        print("已停止常驻守护。")
        log("常驻守护已停止（用户中断）")
    finally:
        try:
            if _read_pid() == os.getpid():
                os.remove(PID_FILE)
        except OSError:
            pass


def interactive(grace):
    while True:
        items = collect(grace)
        print_items(items)
        try:
            choice = input("请输入选项：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "0":
            return
        if choice == "1":
            continue
        if choice == "2":
            cleaned = parked = 0
            for it in items:
                if it["cleanable"] and clean_one(it):
                    cleaned += 1
                elif it["parkable"] and park_one(it):
                    parked += 1
            if cleaned == 0 and parked == 0:
                print("没有可处理的会话（正在运行 / 状态未知的都跳过了）。")
            else:
                print("完成：删除僵尸锁 %d 个，挪锁解锁 %d 个。现在去左栏点归档试试。" % (cleaned, parked))
            time.sleep(1.5)
        elif choice == "3":
            watch_mode(grace, DEFAULT_INTERVAL)
        elif choice == "4":
            pick = input("输入要强制处理的编号：").strip()
            if pick.isdigit() and 1 <= int(pick) <= len(items):
                it = items[int(pick) - 1]
                print("将强制处理：[%s] %s" % (it["tid"][:8], (it["title"] or "")[:30]))
                confirm = input("确认？(y/N)：").strip().lower()
                if confirm == "y":
                    if it.get("held") is True:
                        park_one(it, force=True)
                    else:
                        clean_one(it, force=True)
            else:
                print("编号无效。")
            time.sleep(1)
        elif choice == "5":
            tid = input("输入要复原锁的会话 ID（完整 36 位）：").strip()
            unpark_one(tid)
            time.sleep(1.5)
        else:
            print("没有这个选项。")


def main():
    parser = argparse.ArgumentParser(description="Codex 归档锁守护")
    parser.add_argument("--once", action="store_true", help="删僵尸锁 + 挪空闲占用的锁，执行一次后退出")
    parser.add_argument("--dry-run", action="store_true", help="只看会处理什么，不真动手")
    parser.add_argument("--watch", action="store_true", help="常驻守护")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="常驻检查间隔秒数")
    parser.add_argument("--grace", type=int, default=GRACE_SECONDS, help="静置多少秒才允许处理")
    parser.add_argument("--park-tid", metavar="TID", help="指定会话：把空闲但被占用的锁挪走")
    parser.add_argument("--unpark-tid", metavar="TID", help="指定会话：把挪走的锁复原回去")
    parser.add_argument("--force", action="store_true", help="配合 --park-tid：状态未知也强制挪锁")
    parser.add_argument("--stop", action="store_true", help="停止后台常驻守护")
    args = parser.parse_args()

    if args.stop:
        sys.exit(stop_guard())
    if args.watch:
        watch_mode(args.grace, args.interval)
        return
    if args.unpark_tid:
        unpark_one(args.unpark_tid, dry_run=args.dry_run)
        return
    if args.park_tid:
        tid = args.park_tid.strip()
        target = None
        for it in collect(args.grace):
            if it["tid"] == tid:
                target = it
                break
        if target is None:
            print("锁目录里没有这个会话的锁（可能已被处理）。")
            return
        ok = park_one(target, force=args.force, dry_run=args.dry_run)
        if ok and not args.dry_run:
            print("已挪锁：%s。现在去 Codex 左栏点归档，应该能成功。" % tid[:8])
        elif not ok and not args.dry_run:
            print("没有挪锁。看上面的原因；如确认会话已空闲，可加 --force 再试。")
        return
    if args.once or args.dry_run:
        cleaned, parked, _ = process_all(args.grace, dry_run=args.dry_run, force=args.force)
        if args.dry_run:
            print("演练完成：可删僵尸锁 %d 个，可挪锁 %d 个。" % (cleaned, parked))
        else:
            print("检查完成：删除僵尸锁 %d 个，挪锁解锁 %d 个。" % (cleaned, parked))
        return
    interactive(args.grace)


if __name__ == "__main__":
    main()
