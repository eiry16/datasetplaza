#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Dataset Plazz 控制台启动器：把「打开控制台」和「后端进程」绑成一条命。

用法（项目根目录）::

    python -m bdsp.launcher                # 打开控制台 → 拉起后端；关闭控制台 → 后端退出
    python 总后台.py                       # 同一个入口的兼容别名
    python -m bdsp.launcher --mode production  # 上线：后端常驻，不受控制台开关影响
    python -m bdsp.launcher --reuse        # 兼容旧行为：复用已有后端，且不负责关它
    python -m bdsp.launcher --no-browser   # 只起后端，等 Ctrl+C

联动机制
--------
后端启动时带一个一次性会话 token（--console-token）；首页把它注入
``<meta name="dp-console-token">``，页面每 5 秒发一次心跳，关闭时上报 leave。
后端只在心跳还在时活着：最后一个页面离开数秒后自行退出，心跳静默超时也会退出。
因此生命周期绑定的是「页面」而不是浏览器进程——浏览器复用进程、多标签、
刷新、换浏览器、甚至启动器被强杀，都不会留下孤儿后端。

浏览器进程如果可靠，只作为「立即关机」的加速信号；主信号始终是后端自退。
只用标准库；pythonw 下无黑窗，日志写 runtime/launcher.log。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
LAUNCHER_LOG = RUNTIME / "launcher.log"
PY = sys.executable

BROWSER_CANDIDATES = [
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
]

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
HEADLESS = sys.stdout is None  # pythonw.exe：没有控制台，日志必须落盘


def say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:  # pythonw / 无 stdout 环境
        pass
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        with LAUNCHER_LOG.open("a", encoding="utf-8") as fp:
            fp.write(msg + "\n")
    except OSError:
        pass


def reset_log() -> None:
    """只保留最近一次会话的日志，避免无限增长。"""
    try:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        LAUNCHER_LOG.write_text("", encoding="utf-8")
    except OSError:
        pass


def base_url(host: str, port: int, https: bool) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    return f"{'https' if https else 'http'}://{shown}:{port}"


def status(url: str, https: bool, timeout: float = 1.5) -> tuple[int, dict | None]:
    """Probe the backend. A JSON body with our own markers identifies it."""
    import ssl

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()) if https
        else urllib.request.HTTPHandler(),
    )
    try:
        req = urllib.request.Request(url + "/api/auth/status", headers={"User-Agent": "DatasetPlazzLauncher/1"})
        with opener.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
            return resp.status, body
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode("utf-8", "replace"))
        except Exception:
            return exc.code, None
    except Exception:
        return 0, None


def post_json(url: str, path: str, body: dict, https: bool = False,
              timeout: float = 2.0) -> tuple[int, dict | None]:
    """本机停机请求；不走系统代理，HTTPS 自签证书不校验。"""
    import ssl

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl._create_unverified_context()) if https
        else urllib.request.HTTPHandler(),
    )
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url + path, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "DatasetPlazzLauncher/1"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return 0, None


def is_ours(payload: dict | None) -> bool:
    """Any version of this project (used to avoid killing other services)."""
    return bool(payload) and "payment" in payload and "session_ttl_seconds" in payload


def is_current(payload: dict | None) -> bool:
    """A 0.4+ backend that actually serves the catalogue shelf."""
    return is_ours(payload) and payload.get("shelf_enabled") is True


def new_console_token() -> str:
    """每次打开控制台换一个凭证，旧页面不会给新后端续命。"""
    return secrets.token_hex(16)


def should_takeover(args) -> bool:
    """本地联动必须独占后端：端口上已有本项目实例时，停旧起新。

    --reuse / --no-browser 是显式逃生口；production 模式从不接管。
    """
    return (args.mode == "local" and not args.reuse and not args.no_browser)


def listening_pids(port: int) -> list[int]:
    """Windows/netstat fallback; returns owning PIDs for LISTENING sockets."""
    pids: list[int] = []
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
            timeout=8, encoding="utf-8", errors="replace",
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts and parts[1].endswith(f":{port}"):
                try:
                    pids.append(int(parts[-1]))
                except ValueError:
                    pass
    except Exception:
        pass
    if pids:
        return sorted(set(pids))
    # PS fallback (kept as a fallback only; netstat is the primary path)
    try:
        ps = (
            f"Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue "
            "| Select-Object -ExpandProperty OwningProcess"
        )
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace",
        ).stdout
        pids = [int(x) for x in re.findall(r"\d+", out or "")]
    except Exception:
        pass
    return sorted(set(pids))


def process_command(pid: int) -> str:
    try:
        ps = f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace",
        ).stdout.strip()
    except Exception:
        return ""


def command_lines() -> dict[int, str]:
    """枚举含 bdsp 的进程命令行（沙箱里 netstat 看不到监听 PID 时的兜底）。"""
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*bdsp*' } "
          "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        ).stdout or ""
        data = json.loads(out)
    except Exception:
        return {}
    if isinstance(data, dict):
        data = [data]
    found: dict[int, str] = {}
    for row in data or []:
        try:
            found[int(row["ProcessId"])] = str(row.get("CommandLine") or "")
        except (KeyError, TypeError, ValueError):
            continue
    return found


def backend_pids(port: int) -> dict[int, str]:
    """返回 port 上的本项目后端 PID → 命令行；绝不返回 PID 0 这种无效值。"""
    found: dict[int, str] = {}
    for pid in listening_pids(port):
        if pid > 0:
            found[pid] = process_command(pid)
    if found:
        return found
    needle = re.compile(r"--port(?:[ =])%d(?!\d)" % port)
    for pid, cmd in command_lines().items():
        if "bdsp" in cmd.lower() and needle.search(cmd):
            found[pid] = cmd
    return found


def wait_port_free(base: str, https: bool, seconds: float) -> bool:
    if not base:
        return True
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, _ = status(base, https, timeout=1.0)
        if code == 0:  # 连不上 = 端口已释放
            return True
        time.sleep(0.25)
    return status(base, https, timeout=1.0)[0] == 0


def stop_backend(port: int, base: str = "", https: bool = False) -> bool:
    """让端口上的本项目后端退出；绝不把「找不到 PID」当成「已停止」。

    优先走本机停机接口（连 PID 都不用找，常驻/沙箱环境都有效），
    失败再按命令行核对后 taskkill。最后必须确认端口真的空闲。
    """
    RUNTIME.mkdir(parents=True, exist_ok=True)
    token = new_console_token()
    (RUNTIME / "console_stop.token").write_text(token, encoding="utf-8")

    http_ok = False
    if base:
        code, _ = post_json(base, "/api/console/stop", {"stop_token": token}, https)
        http_ok = code == 200
        if http_ok:
            say("  已请求旧后端自行退出（本机停机接口）")
        else:
            say(f"  本机停机接口不可用 (HTTP {code})，改用进程定位")
    if http_ok and wait_port_free(base, https, seconds=6.0):
        return True
    if wait_port_free(base, https, seconds=0.5):
        return True  # 旧实例已自行退出，不必再找 PID

    found = backend_pids(port)
    if not found:
        say("  [警告] 端口上仍有服务，但 netstat 与进程命令行都定位不到本项目后端")
    for pid, cmd in found.items():
        if "bdsp" not in cmd.lower():
            say(f"  [拒绝] 端口 {port} 由非本项目进程占用 (PID {pid})："
                f"{cmd[:120] or '无法读取命令行'}")
            return False
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=8)
            say(f"  已停止旧后端 PID {pid}")
        except Exception as exc:  # noqa: BLE001
            say(f"  [警告] 停止 PID {pid} 失败：{exc}")

    if not wait_port_free(base, https, seconds=6.0):
        say(f"  [错误] 端口 {port} 仍被占用；已中止，请手动关闭后重试。")
        return False
    return True


def wait_ready(url: str, https: bool, seconds: float = 25.0,
               expect_session: str | None = None) -> bool:
    """只有「本次会话的后端」才算就绪，防止旧实例还活着被误判。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        code, payload = status(url, https)
        if code == 200 and is_current(payload):
            if expect_session:
                cs = (payload or {}).get("console") or {}
                if not (cs.get("bound") and cs.get("session") == expect_session[:8]):
                    time.sleep(0.3)
                    continue
            return True
        time.sleep(0.3)
    return False


def start_backend(args, url: str) -> subprocess.Popen | None:
    """启动后端。local 模式顺便给它绑上本次控制台会话 token。"""
    args.console_token = ""
    cmd = [
        PY, "-m", "bdsp.cli", "admin",
        "--host", args.host, "--port", str(args.port),
        "--sources", str(ROOT / "sources"),
        "--db", str(ROOT / "index.sqlite3"),
        "--runtime", str(RUNTIME),
        "--listings", str(ROOT / "listings" / "listings.json"),
    ]
    if args.https:
        cmd.append("--https")
    if args.no_auth:
        cmd.append("--no-auth")
    if args.mode == "local" and not args.no_browser:
        args.console_token = new_console_token()
        cmd += ["--console-token", args.console_token,
                "--console-idle", str(args.console_idle),
                "--console-boot-grace", str(args.console_boot_grace)]
    env = dict(os.environ, PYTHONUTF8="1",
               PYTHONPATH=str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if args.mode == "production":
        log = RUNTIME / "backend.log"
        handle = open(log, "ab", buffering=0)
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=handle,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=flags)
        say(f"  后端已作为常驻进程启动 (PID {proc.pid})，日志 {log}")
        return None  # detached: intentionally not tracked
    say("  启动后端：" + " ".join(cmd[2:]))
    if HEADLESS:
        # pythonw 没有控制台，后端输出必须落盘，否则排错时两眼一抹黑
        handle = open(RUNTIME / "backend-local.log", "ab", buffering=0)
        return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=handle,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW)
    return subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                            stdin=subprocess.DEVNULL)


def find_browser() -> Path | None:
    override = os.environ.get("DATASET_PLAZZ_BROWSER")
    if override and Path(override).is_file():
        return Path(override)
    for p in BROWSER_CANDIDATES:
        if p and p.is_file():
            return p
    return None


def launch_frontend(url: str, args) -> subprocess.Popen | None:
    exe = find_browser()
    target = url + "/"
    if exe is None:
        say("  未找到 Edge/Chrome，改用系统默认浏览器（由页面心跳判定关闭）")
        webbrowser.open(target)
        return None
    profile = RUNTIME / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(exe), f"--app={target}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=msEdgeIdentityFRE",
        "--window-size=1440,960",
    ]
    say(f"  拉起控制台：{exe.name} --app={target}")
    return subprocess.Popen(cmd, cwd=str(ROOT))


def watch_local(browser, backend, poll_interval: float = 0.6,
                max_polls: int = 100_000, trust_browser: bool = True) -> str:
    """Pure lifecycle watch for local mode (no side effects).

    返回 'frontend_closed' | 'backend_exited' | 'interrupted' | 'timeout'。
    trust_browser=False 时完全交给后端自己的心跳守护（浏览器进程 fork/复用时）。
    """
    try:
        for _ in range(max_polls):
            if trust_browser and browser is not None and browser.poll() is not None:
                return "frontend_closed"
            if backend is not None and backend.poll() is not None:
                return "backend_exited"
            if poll_interval:
                time.sleep(poll_interval)
        return "timeout"
    except KeyboardInterrupt:
        return "interrupted"


def terminate(proc: subprocess.Popen | None, label: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=4)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:
            pass
    say(f"  已停止{label}")


def password_hint() -> str:
    p = RUNTIME / "admin_password.txt"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return "(见 runtime/admin_password.txt)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bdsp.launcher", description="Dataset Plazz：打开控制台即起后端，关闭即停"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--mode", choices=["local", "production"], default="local",
                        help="local: 控制台关闭即停后端；production: 后端常驻")
    parser.add_argument("--restart", action="store_true", help="强制停掉已运行的本项目后端再重启")
    parser.add_argument("--reuse", action="store_true",
                        help="复用已运行后端，且不负责关闭它（兼容旧行为）")
    parser.add_argument("--no-browser", action="store_true", help="只起后端，不打开控制台")
    parser.add_argument("--https", action="store_true", help="启用自签 HTTPS")
    parser.add_argument("--no-auth", action="store_true", help="关闭登录鉴权（仅离线调试）")
    parser.add_argument("--console-idle", type=float, default=20.0,
                        help="页面心跳最长静默秒数（默认 20）")
    parser.add_argument("--console-boot-grace", type=float, default=120.0,
                        help="控制台从未打开时的退出宽限秒数（默认 120）")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    reset_log()
    url = base_url(args.host, args.port, args.https)
    say(f"=== Dataset Plazz 控制台 | {args.mode} 模式 | {url} ==="
        + (" [无窗口]" if HEADLESS else ""))

    # 1) 启动前一定探测端口，避免误杀别的服务
    code, payload = status(url, args.https)
    if code and not is_ours(payload):
        say(f"[错误] 端口 {args.port} 已被其他服务占用（HTTP {code}），不会启动也不会误杀。")
        say("       换端口：python -m bdsp.launcher --port 8790")
        return 2
    running = code == 200 and is_ours(payload)
    stale = running and not is_current(payload)
    say(f"  探测后端：{'已在运行（本项目' + ('·旧版实例' if stale else '') + '）' if running else '未运行'}"
        + (f" · 鉴权={'开启' if payload.get('enabled') else '关闭'}" if running else ""))
    if stale:
        say("  旧版实例缺少目录货架，自动重启")
        args.restart = True

    # 2) 本地联动要独占后端：已有实例（哪怕是本项目）也先停掉再拉起
    takeover = running and should_takeover(args)
    if running and (args.restart or takeover):
        say("  " + ("本地联动要求独占会话，" if takeover else "--restart：")
            + "停止已有本项目后端…")
        if not stop_backend(args.port, url, args.https):
            say("[错误] 旧后端停止失败，已中止，避免端口冲突。")
            return 3
        running = False

    backend: subprocess.Popen | None = None
    args.console_token = ""
    if not running:
        backend = start_backend(args, url)
        if not wait_ready(url, args.https, expect_session=args.console_token or None):
            say("[错误] 后端在 25 秒内未就绪：可能是旧实例仍占住端口，或新进程启动失败。"
                "请检查上方输出或 runtime/backend.log")
            terminate(backend, "后端")
            return 4
        say("  后端就绪 ✓")
    else:
        say("  复用已运行的后端（--reuse / production；关闭控制台不会停它）")

    code, payload = status(url, args.https)
    console_state = (payload or {}).get("console") or {}
    if args.console_token:
        if console_state.get("bound") and console_state.get("session") == args.console_token[:8]:
            say(f"  控制台会话已绑定 ✓（{args.console_token[:8]}；"
                f"心跳静默 {args.console_idle:g}s 自动退出）")
        else:
            say("  [警告] 后端未进入控制台会话模式（旧版本？），关闭控制台可能不会自动停后端")
    say(f"  鉴权：{'开启' if (payload or {}).get('enabled') else '关闭'} · "
        f"支付：{(payload or {}).get('payment', {}).get('mode', '?')}")
    say(f"  管理员密码：{password_hint()}")
    say(f"  控制台地址：{url}/")

    if args.no_browser:
        if args.mode == "local":
            say("  --no-browser：后端保持运行，按 Ctrl+C 停止。")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                terminate(backend, "后端")
        else:
            say("  production 模式：后端常驻，脚本退出不影响它。")
        return 0

    browser = launch_frontend(url, args)

    if args.mode == "production":
        say("  production 模式：控制台关闭不会停后端；后端常驻直到手动停止。")
        return 0

    # local：页面心跳是主信号，浏览器进程只是可选的加速信号
    trusted = browser is not None
    if trusted:
        time.sleep(3)
        if browser.poll() is not None:
            say("  [提示] 浏览器启动进程已退出（可能复用已有实例）；改由页面心跳判定控制台关闭。")
            trusted = False

    if backend is None:
        say("  复用模式下本次控制台不拥有后端；关闭窗口不会停止它。按 Ctrl+C 退出启动器。")
    else:
        say("  本地联动已建立：关闭控制台页面 → 后端自动退出；也可按 Ctrl+C。")
    reason = watch_local(browser, backend, trust_browser=trusted)
    if reason == "frontend_closed":
        say("  检测到控制台窗口已关闭。")
    elif reason == "backend_exited":
        say("  控制台已全部关闭，后端自行退出。")
    elif reason == "interrupted":
        say("  收到 Ctrl+C。")
    terminate(backend, "本地后端")
    say("=== 已收尾 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
