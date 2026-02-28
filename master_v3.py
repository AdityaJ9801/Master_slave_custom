#!/usr/bin/env python3
"""
=============================================================
  PUPY MASTER CLI  v2  -  Chocolatey + Pupy Commands
=============================================================
Controls slave PCs over direct TCP (no SSH).
Includes:
  - Chocolatey install on all slaves
  - choco install / uninstall / upgrade / search
  - Shell commands per-slave or broadcast to all
  - File upload / download
  - Friendly name assignment (saved across sessions)

USAGE:
  python master.py
  python master.py --port 9999

REQUIREMENTS:
  pip install colorama tabulate
"""

import socket, json, os, sys, time, threading
import struct, argparse, platform
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import queue
# ── Optional imports ──────────────────────────────────────────────────────────
try:
    from colorama import init, Fore, Style
    init(autoreset=True)
    HAS_COLOR = True
except ImportError:
    HAS_COLOR = False
    class Fore:
        RED=GREEN=YELLOW=CYAN=MAGENTA=WHITE=BLUE=RESET=""
    class Style:
        BRIGHT=DIM=RESET_ALL=""

try:
    from tabulate import tabulate
    HAS_TABLE = True
except ImportError:
    HAS_TABLE = False

# =============================================================================
# CONFIG
# =============================================================================
LISTEN_PORT   = 9999
SAVE_FILE     = "pupy_hosts.json"
CMD_TIMEOUT   = 40
CHOCO_TIMEOUT = 360   # package installs can take minutes

# =============================================================================
# COLOR HELPERS
# =============================================================================
def c(text, col="", style=""):
    return f"{style}{col}{text}{Style.RESET_ALL}" if HAS_COLOR else str(text)

def banner():
    print(c("""
  ╔══════════════════════════════════════════════════════════════════╗
  ║     PUPY MASTER CLI  v2  —  Chocolatey + Command Control        ║
  ║   No SSH. Slaves connect to YOU. Install apps on 50 PCs.       ║
  ╚══════════════════════════════════════════════════════════════════╝
""", Fore.CYAN, Style.BRIGHT))

def ok(m):      print(c(f"  [ OK ] {m}", Fore.GREEN))
def err(m):     print(c(f"  [ERR] {m}", Fore.RED))
def warn(m):    print(c(f"  [ ! ] {m}", Fore.YELLOW))
def info(m):    print(c(f"  [INF] {m}", Fore.CYAN))
def section(t):
    print(c(f"\n  {'─'*62}", Fore.MAGENTA, Style.BRIGHT))
    print(c(f"  {t}", Fore.MAGENTA, Style.BRIGHT))
    print(c(f"  {'─'*62}", Fore.MAGENTA, Style.BRIGHT))

# =============================================================================
# PROTOCOL  (length-prefixed JSON, mirrors slave.py)
# =============================================================================
def send_msg(sock, data: dict):
    raw = json.dumps(data).encode("utf-8")
    sock.sendall(struct.pack(">I", len(raw)) + raw)

def recv_msg(sock, timeout=CMD_TIMEOUT) -> dict:
    old = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        hdr = _recv_exact(sock, 4)
        if not hdr:
            return None
        n   = struct.unpack(">I", hdr)[0]
        raw = _recv_exact(sock, n)
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))
    except socket.timeout:
        return {"type": "error", "msg": "Timed out — slave took too long"}
    except Exception as e:
        return {"type": "error", "msg": str(e)}
    finally:
        sock.settimeout(old)

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        except Exception:
            return None
    return buf

def recv_raw(sock, size):
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(min(65536, size - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf

# =============================================================================
# SLAVE RECORD
# =============================================================================
class SlaveRecord:
    def __init__(self, sock, addr, reg: dict):
        self.sock       = sock
        self.addr       = addr
        self.ip         = reg.get("ip", addr[0])
        self.hostname   = reg.get("hostname", addr[0])
        self.os         = reg.get("os", "?")
        self.user       = reg.get("user", "?")
        self.choco      = reg.get("choco", "?")
        self.is_admin   = reg.get("is_admin", False)
        self.last_seen  = datetime.now()
        self.name       = ""
        self.lock       = threading.Lock()
        
        # Thread-safe queue to prevent socket collisions!
        self.resp_queue = queue.Queue()

    def label(self):
        return self.name if self.name else self.hostname

    def short(self):
        return f"{self.label()} ({self.ip})"

    def cmd(self, data: dict, timeout=CMD_TIMEOUT):
        """Send command, wait safely for response without hitting JSON errors."""
        # Clear any old garbage left in the queue
        while not self.resp_queue.empty():
            try: self.resp_queue.get_nowait()
            except queue.Empty: break

        try:
            with self.lock:
                send_msg(self.sock, data)
            # Safely wait for the background listener to hand us the response
            return self.resp_queue.get(timeout=timeout)
        except queue.Empty:
            return {"type": "error", "msg": f"Command timed out after {timeout} seconds."}
        except Exception as e:
            return {"type": "error", "msg": f"Socket error: {str(e)}"}

    def alive(self):
        try:
            r = self.cmd({"type": "ping"}, timeout=5)
            return r and r.get("type") == "pong"
        except Exception:
            return False

    def choco_installed(self):
        return self.choco != "not installed" and self.choco != "?"
    def admin_str(self):
        return c("ADMIN", Fore.GREEN) if self.is_admin else c("user ", Fore.YELLOW)

# =============================================================================
# HOST STORE  (persistent friendly names)
# =============================================================================
class HostStore:
    def __init__(self):
        self.names = {}
        self._load()

    def _load(self):
        if os.path.exists(SAVE_FILE):
            try:
                self.names = json.load(open(SAVE_FILE)).get("names", {})
            except Exception:
                pass

    def save(self):
        try:
            json.dump({"names": self.names}, open(SAVE_FILE, "w"), indent=2)
        except Exception:
            pass

    def get(self, hostname):
        return self.names.get(hostname, hostname)

    def set(self, hostname, name):
        self.names[hostname] = name
        self.save()

# =============================================================================
# MASTER SERVER
# =============================================================================
class MasterServer:
    def __init__(self, port=LISTEN_PORT):
        self.port    = port
        self.slaves  = {}
        self.store   = HostStore()
        self._id     = 0
        self._lock   = threading.Lock()

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", self.port))
        except OSError as e:
            err(f"Cannot bind port {self.port}: {e}")
            err("Another master already running? Use --port XXXX")
            sys.exit(1)
        srv.listen(100)
        ok(f"Listening on 0.0.0.0:{self.port} — waiting for slaves...")
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()

    def _accept(self, srv):
        while True:
            try:
                conn, addr = srv.accept()
                threading.Thread(target=self._on_slave,
                                 args=(conn, addr), daemon=True).start()
            except Exception:
                break

    def _on_slave(self, conn, addr):
        try:
            reg = recv_msg(conn, timeout=10)
            if not reg or reg.get("type") != "register":
                conn.close(); return

            slave = SlaveRecord(conn, addr, reg)
            slave.name = self.store.get(slave.hostname)

            with self._lock:
                self._id += 1
                sid = self._id
                self.slaves[sid] = slave

            print(c(f"\n  [+] #{sid:>2}  {slave.label():<20}  {slave.ip:<16} connected.", Fore.GREEN))
            print(c("  puppet> ", Fore.MAGENTA, Style.BRIGHT), end="", flush=True)

            # Start the ONLY function allowed to read from this socket
            self._monitor(conn, sid, slave)

        except Exception:
            pass
        finally:
            with self._lock:
                if sid in self.slaves:
                    print(c(f"\n  [-] DISCONNECTED: {self.slaves[sid].short()}", Fore.RED))
                    del self.slaves[sid]
                    print(c("  puppet> ", Fore.MAGENTA, Style.BRIGHT), end="", flush=True)
            try: conn.close()
            except Exception: pass

    def _monitor(self, conn, sid, slave):
        """Dedicated thread to safely read incoming socket data without colliding."""
        while True:
            try:
                msg = recv_msg(conn, timeout=None) # Block safely forever
                if msg is None: break
                
                if msg.get("type") == "heartbeat":
                    with self._lock:
                        slave.last_seen = datetime.now()
                else:
                    # We got a command result! Put it in the queue for the CLI thread
                    slave.resp_queue.put(msg)
            except Exception:
                break

    # ── Slave resolution ──────────────────────────────────────────────────────
    def get(self, choice) -> SlaveRecord:
        """Resolve user input → SlaveRecord. Accepts: #id, name, hostname, IP."""
        s = self.slaves
        if not s:
            return None
        # Exact #id
        if str(choice).isdigit():
            sid = int(choice)
            if sid in s:
                return s[sid]
            # positional
            items = list(s.items())
            pos   = int(choice) - 1
            if 0 <= pos < len(items):
                return items[pos][1]
            return None
        # Exact name / hostname / IP
        cl = str(choice).lower()
        for sl in s.values():
            if cl in (sl.name.lower(), sl.hostname.lower(), sl.ip):
                return sl
        # Partial
        matches = [sl for sl in s.values()
                   if cl in sl.name.lower() or cl in sl.hostname.lower()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            warn(f"Ambiguous '{choice}' — matches: {[sl.label() for sl in matches]}")
        return None

    def all_slaves(self):
        return dict(self.slaves)

    # ── List ──────────────────────────────────────────────────────────────────
    def list_slaves(self):
        slaves = self.slaves
        if not slaves:
            warn("No slaves connected.")
            info(f"On each PC run:  python slave.py --master <YOUR_IP>")
            return

        now  = datetime.now()
        rows = []
        for sid, s in sorted(slaves.items()):
            age    = int((now - s.last_seen).total_seconds())
            seen   = f"{age}s ago" if age < 60 else f"{age//60}m ago"
            choco  = (c(s.choco, Fore.GREEN) if s.choco_installed()
                      else c("NOT INSTALLED", Fore.RED))
            admin  = c("ADMIN", Fore.GREEN, Style.BRIGHT) if s.is_admin else c("user", Fore.YELLOW)
            rows.append([
                c(str(sid), Fore.CYAN, Style.BRIGHT),
                c(s.label(), Fore.WHITE, Style.BRIGHT),
                s.ip,
                s.os,
                s.user,
                admin,
                choco,
                c(seen, Fore.GREEN if age < 30 else Fore.YELLOW),
            ])

        hdrs = ["#", "Name/Hostname", "IP", "OS", "User", "Rights", "Chocolatey", "Last Seen"]
        if HAS_TABLE:
            for line in tabulate(rows, hdrs, tablefmt="rounded_outline").splitlines():
                print("  " + line)
        else:
            print(c(f"  {'#':<4} {'Name':<22} {'IP':<16} {'OS':<8} {'User':<12} {'Admin':<6} {'Choco':<20} Seen",
                    Fore.CYAN, Style.BRIGHT))
            print("  " + "─"*90)
            for r in rows:
                print(f"  {r[0]:<4} {r[1]:<22} {r[2]:<16} {r[3]:<8} {r[4]:<12} {r[5]:<6} {r[6]:<20} {r[7]}")

        print(c(f"\n  {len(slaves)} slave(s) connected.", Fore.CYAN))

# =============================================================================
# OUTPUT FORMATTERS
# =============================================================================
def print_shell_result(resp, prefix=""):
    if not resp:
        err("No response from slave.")
        return
    if resp.get("type") == "error":
        err(resp.get("msg", "Unknown error"))
        return
    stdout = resp.get("stdout", "").rstrip()
    stderr = resp.get("stderr", "").rstrip()
    rc     = resp.get("returncode", 0)
    if stdout: print(stdout)
    if stderr: print(c(stderr, Fore.RED))
    if rc != 0: print(c(f"  [exit code: {rc}]", Fore.YELLOW))

def print_choco_result(resp, slave_label=""):
    if not resp:
        err("No response.")
        return
    if resp.get("type") == "error":
        err(resp.get("msg", "Unknown error"))
        hint = _choco_error_hint(resp.get("msg", ""))
        if hint:
            print(c(f"  FIX: {hint}", Fore.YELLOW))
        return

    stdout = resp.get("stdout", "").rstrip()
    stderr = resp.get("stderr", "").rstrip()
    rc     = resp.get("returncode", 0)
    action = resp.get("action", "")

    if rc == 0:
        ok(f"{slave_label}  {action}  SUCCESS")
    else:
        err(f"{slave_label}  {action}  FAILED (exit {rc})")

    if stdout: print(stdout)
    if stderr: print(c(stderr, Fore.RED))

    # Show new choco version after install
    if "choco_version" in resp:
        if resp["choco_version"] != "not installed":
            ok(f"Chocolatey now installed: v{resp['choco_version']}")

def _choco_error_hint(msg):
    msg_l = msg.lower()
    if "not installed" in msg_l:
        return "Run 'choco install' first on this slave."
    if "admin" in msg_l or "administrator" in msg_l or "elevated" in msg_l:
        return ("Restart slave.py with admin rights on that PC.\n"
                "  Windows: right-click → Run as Administrator")
    if "network" in msg_l or "unable to connect" in msg_l:
        return "Slave PC has no internet. It needs internet to download packages."
    return ""

# =============================================================================
# BROADCAST HELPER
# =============================================================================
def broadcast_cmd(server: MasterServer, msg: dict, timeout=CMD_TIMEOUT,
                  result_fmt="shell"):
    """
    Send same command to ALL slaves in parallel.
    result_fmt: 'shell' or 'choco'
    """
    slaves = server.all_slaves()
    if not slaves:
        warn("No slaves connected.")
        return {}

    results = {}
    lock    = threading.Lock()

    def run_one(sid, slave):
        r = slave.cmd(msg, timeout=timeout)
        with lock:
            results[sid] = (slave, r)

    info(f"Broadcasting to {len(slaves)} slave(s)...")
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = [pool.submit(run_one, sid, sl) for sid, sl in slaves.items()]
        for f in futs: f.result()

    for sid, (slave, resp) in sorted(results.items()):
        section(f"[#{sid}] {slave.label()} ({slave.ip})")
        if result_fmt == "choco":
            print_choco_result(resp, slave.label())
        else:
            print_shell_result(resp)

    return results

# =============================================================================
# INTERACTIVE SHELL  (per slave)
# =============================================================================
def interactive_shell(slave: SlaveRecord, store: HostStore):
    section(f"Shell: {slave.label()}  ({slave.ip})  [{slave.os}]  {slave.admin_str()}")
    print(c("  Commands: shell cmd | info | ls [path] | upload | download", Fore.CYAN))
    print(c("  Choco   : choco install <pkg> | choco list | choco upgrade | choco search <q>", Fore.CYAN))
    print(c("  Type 'exit' to return to main menu.\n", Fore.CYAN))

    while True:
        try:
            prompt = c(f"  [{slave.label()}]$ ", Fore.GREEN, Style.BRIGHT)
            raw    = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print(); break

        if not raw:
            continue

        parts  = raw.split()
        action = parts[0].lower()

        if action in ("exit", "quit", "back"):
            break

        elif action == "help":
            _shell_help()

        elif action == "info":
            resp = slave.cmd({"type": "info"})
            if resp:
                section("System Info")
                for k, v in resp.items():
                    if k != "type":
                        col = Fore.GREEN if k in ("choco", "is_admin") else Fore.CYAN
                        print(f"    {c(k+':', col)} {v}")

        elif action == "ls":
            path = parts[1] if len(parts) > 1 else "."
            resp = slave.cmd({"type": "listdir", "path": path})
            if resp and resp.get("type") == "listdir_result":
                for e in resp.get("entries", []):
                    icon = c("[DIR] ", Fore.CYAN) if e["type"] == "dir" else c("[FILE]", Fore.WHITE, Style.DIM)
                    size = f"  {e['size']:>12,} bytes" if e["type"] == "file" else ""
                    print(f"  {icon}  {e['name']}{size}")
            elif resp:
                err(resp.get("msg", "Error"))

        elif action == "upload":
            if len(parts) < 3:
                err("Usage: upload <local_path> <remote_path>"); continue
            _do_upload(slave, parts[1], parts[2])

        elif action == "download":
            if len(parts) < 3:
                err("Usage: download <remote_path> <local_path>"); continue
            _do_download(slave, parts[1], parts[2])

        # ── Chocolatey commands ───────────────────────────────────────────────
        elif action == "choco":
            if len(parts) < 2:
                err("Usage: choco <install|uninstall|upgrade|list|search|status|setup>")
                continue
            sub = parts[1].lower()

            if sub == "setup":
                # Install Chocolatey itself
                info("Installing Chocolatey on this slave...")
                warn("This requires the slave to be running as Administrator.")
                resp = slave.cmd({"type": "choco_install_self"}, timeout=CHOCO_TIMEOUT)
                print_choco_result(resp, slave.label())

            elif sub == "install":
                if len(parts) < 3:
                    err("Usage: choco install <package> [version]"); continue
                if parts[2].lower() in ("all", "1", "2", "3"):
                    err("ERROR: You are already inside the Slave terminal!")
                    err("Do not type 'all' or '1' here. Just type: choco install puppet-agent")
                    continue
                pkg = parts[2]
                ver = parts[3] if len(parts) > 3 else None
                info(f"Installing '{pkg}' on {slave.label()}...")
                warn("Admin rights required on the slave PC.")
                resp = slave.cmd({"type": "choco_install",
                                  "package": pkg, "version": ver},
                                 timeout=CHOCO_TIMEOUT)
                print_choco_result(resp, slave.label())

            elif sub == "uninstall":
                if len(parts) < 3:
                    err("Usage: choco uninstall <package>"); continue
                resp = slave.cmd({"type": "choco_uninstall", "package": parts[2]},
                                 timeout=120)
                print_choco_result(resp, slave.label())

            elif sub == "upgrade":
                pkg  = parts[2] if len(parts) > 2 else "all"
                info(f"Upgrading '{pkg}' on {slave.label()}...")
                resp = slave.cmd({"type": "choco_upgrade", "package": pkg},
                                 timeout=CHOCO_TIMEOUT)
                print_choco_result(resp, slave.label())

            elif sub == "list":
                resp = slave.cmd({"type": "choco_list"}, timeout=60)
                if resp and resp.get("type") != "error":
                    print(resp.get("stdout", "").rstrip())
                elif resp:
                    err(resp.get("msg", "Error"))

            elif sub == "search":
                if len(parts) < 3:
                    err("Usage: choco search <query>"); continue
                resp = slave.cmd({"type": "choco_search", "query": parts[2]}, timeout=60)
                if resp and resp.get("type") != "error":
                    print(resp.get("stdout", "").rstrip())
                elif resp:
                    err(resp.get("msg", "Error"))

            elif sub == "info":
                if len(parts) < 3:
                    err("Usage: choco info <package>"); continue
                resp = slave.cmd({"type": "choco_info", "package": parts[2]}, timeout=30)
                if resp:
                    print(resp.get("stdout", "").rstrip())

            elif sub == "status":
                resp = slave.cmd({"type": "choco_status"}, timeout=15)
                if resp:
                    ver     = resp.get("version", "?")
                    inst    = resp.get("installed", False)
                    is_adm  = resp.get("is_admin", False)
                    print(c(f"  Chocolatey : {'v'+ver if inst else 'NOT INSTALLED'}",
                            Fore.GREEN if inst else Fore.RED))
                    print(c(f"  Admin      : {'YES' if is_adm else 'NO (some commands need admin)'}",
                            Fore.GREEN if is_adm else Fore.YELLOW))
            else:
                err(f"Unknown choco sub-command: '{sub}'")
                err("Available: setup install uninstall upgrade list search info status")

        else:
            # Generic shell command
            resp = slave.cmd({"type": "shell", "cmd": raw, "timeout": 60})
            print_shell_result(resp)

# =============================================================================
# FILE TRANSFER HELPERS
# =============================================================================
def _do_upload(slave: SlaveRecord, local, remote):
    if not os.path.exists(local):
        err(f"Local file not found: {local}"); return
    size = os.path.getsize(local)
    info(f"Uploading {local} ({size:,} bytes) → {remote}")
    try:
        with slave.lock:
            send_msg(slave.sock, {"type": "upload", "path": remote, "size": size})
            ack = recv_msg(slave.sock, timeout=10)
            if not ack or ack.get("type") != "ready":
                err("Slave did not acknowledge upload."); return
            with open(local, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    slave.sock.sendall(chunk)
            resp = recv_msg(slave.sock)
            if resp and resp.get("type") == "ok":
                ok(resp.get("msg", "Upload complete"))
            else:
                err(str(resp))
    except Exception as e:
        err(f"Upload failed: {e}")

def _do_download(slave: SlaveRecord, remote, local):
    info(f"Downloading {remote} → {local}")
    try:
        with slave.lock:
            send_msg(slave.sock, {"type": "download", "path": remote})
            meta = recv_msg(slave.sock, timeout=10)
            if not meta:
                err("No response from slave."); return
            if meta.get("type") == "error":
                err(meta.get("msg", "Error")); return
            size = meta.get("size", 0)
            data = recv_raw(slave.sock, size)
        with open(local, "wb") as f:
            f.write(data)
        ok(f"Downloaded {len(data):,} bytes → {local}")
    except Exception as e:
        err(f"Download failed: {e}")

# =============================================================================
# HELP TEXTS
# =============================================================================
def print_help():
    print(c("""
  ┌─ Main Commands ──────────────────────────────────────────────────────────┐
  │  list                          Show all connected slaves                 │
  │  connect <name/#/ip>           Open interactive shell on a slave         │
  │  run <name/#/ip> <cmd>         Run one shell command on a slave          │
  │  broadcast <cmd>               Run shell command on ALL slaves           │
  │  apply <name/#/all> <file.pp>  Run a Puppet .pp file on slave(s)         │  
  │                                                                          │
  ├─ Chocolatey (from main menu) ────────────────────────────────────────────┤
  │  choco setup <name/#>          Install Chocolatey on one slave           │
  │  choco setup all               Install Chocolatey on ALL slaves          │
  │  choco install <name/#> <pkg>  Install a package on one slave            │
  │  choco install all <pkg>       Install a package on ALL slaves           │
  │  choco upgrade <name/#>        Upgrade all packages on one slave         │
  │  choco upgrade all             Upgrade all packages on ALL slaves        │
  │  choco uninstall <name/#> <p>  Uninstall package from one slave         │
  │  choco list <name/#>           List installed packages on one slave      │
  │  choco search <query>          Search Chocolatey for packages            │
  │  choco status                  Show choco status on ALL slaves           │
  │                                                                          │
  ├─ Slave Management ───────────────────────────────────────────────────────┤
  │  rename <name/#> <newname>     Give a slave a friendly name              │
  │  info <name/#>                 Show detailed system info                 │
  │  ping <name/#>                 Ping one slave                            │
  │  pingall                       Ping all slaves                           │
  │  help                          Show this help                            │
  │  quit                          Exit master                               │
  └──────────────────────────────────────────────────────────────────────────┘
""", Fore.CYAN))

def _shell_help():
    print(c("""
  Shell mode commands:
    <any command>                    Execute on remote PC
    info                             Show remote system info
    ls [path]                        List remote files/dirs
    upload <local> <remote>          Send file to slave
    download <remote> <local>        Get file from slave
    choco setup                      Install Chocolatey on this slave
    choco install <pkg> [version]    Install a package
    choco uninstall <pkg>            Uninstall a package
    choco upgrade [pkg|all]          Upgrade packages
    choco list                       List installed packages
    choco search <query>             Search for packages
    choco status                     Check Chocolatey status + admin rights
    exit                             Return to main menu
""", Fore.CYAN))

# =============================================================================
# MAIN CLI LOOP
# =============================================================================
def cli_loop(server: MasterServer):
    print_help()

    while True:
        try:
            raw = input(c("\n  pupy> ", Fore.MAGENTA, Style.BRIGHT)).strip()
        except (EOFError, KeyboardInterrupt):
            print(); break

        if not raw:
            continue

        parts  = raw.split()
        action = parts[0].lower()

        # ── list ──────────────────────────────────────────────────────────────
        if action in ("list", "ls", "l"):
            server.list_slaves()

        # ── connect ───────────────────────────────────────────────────────────
        elif action in ("connect", "c", "shell"):
            if len(parts) < 2:
                server.list_slaves()
                if not server.slaves: continue
                try:
                    choice = input(c("  Connect to (#/name): ", Fore.YELLOW)).strip()
                except (EOFError, KeyboardInterrupt):
                    continue
            else:
                choice = parts[1]
            s = server.get(choice)
            if s:
                interactive_shell(s, server.store)
            else:
                err(f"Slave '{choice}' not found. Use 'list' to see slaves.")

        # ── run ───────────────────────────────────────────────────────────────
        elif action in ("run", "exec"):
            if len(parts) < 3:
                err("Usage: run <name/#/ip> <command>"); continue
            s = server.get(parts[1])
            if not s:
                err(f"Slave '{parts[1]}' not found."); continue
            cmd  = " ".join(parts[2:])
            resp = s.cmd({"type": "shell", "cmd": cmd})
            section(f"Output from {s.label()}")
            print_shell_result(resp)

        # ── broadcast ─────────────────────────────────────────────────────────
        elif action in ("broadcast", "bc", "all"):
            if len(parts) < 2:
                err("Usage: broadcast <command>"); continue
            broadcast_cmd(server, {"type": "shell", "cmd": " ".join(parts[1:])})

        # ── apply (.pp files) ─────────────────────────────────────────────────
        elif action == "apply":
            if len(parts) < 3:
                err("Usage: apply <name/#/all> <file.pp>")
                continue
            
            target = parts[1]
            local_file = parts[2]
            
            if not os.path.exists(local_file):
                err(f"Local file not found: {local_file}")
                continue
                
            info(f"Reading {local_file}...")
            try:
                with open(local_file, "r", encoding="utf-8") as f:
                    pp_code = f.read()
            except Exception as e:
                err(f"Could not read file: {e}")
                continue

            msg = {"type": "puppet_apply", "code": pp_code}
            
            if target == "all":
                warn(f"Applying {local_file} on ALL slaves...")
                broadcast_cmd(server, msg, timeout=300, result_fmt="shell")
            else:
                s = server.get(target)
                if not s:
                    err(f"Slave '{target}' not found."); continue
                info(f"Applying {local_file} on {s.label()}...")
                resp = s.cmd(msg, timeout=300)
                print_shell_result(resp)

        # ── info ──────────────────────────────────────────────────────────────
        elif action == "info":
            if len(parts) < 2:
                err("Usage: info <name/#/ip>"); continue
            s = server.get(parts[1])
            if not s:
                err(f"Slave '{parts[1]}' not found."); continue
            resp = s.cmd({"type": "info"})
            if resp:
                section(f"Info: {s.label()}")
                for k, v in resp.items():
                    if k != "type":
                        col = Fore.GREEN if k in ("choco", "is_admin") else Fore.CYAN
                        print(f"    {c(k+':', col)} {v}")

        # ── rename ────────────────────────────────────────────────────────────
        elif action == "rename":
            if len(parts) < 3:
                err("Usage: rename <name/#> <newname>"); continue
            s = server.get(parts[1])
            if not s:
                err(f"Slave '{parts[1]}' not found."); continue
            s.name = parts[2]
            server.store.set(s.hostname, parts[2])
            ok(f"Renamed '{s.hostname}' → '{parts[2]}'")

        # ── ping ──────────────────────────────────────────────────────────────
        elif action == "ping":
            if len(parts) < 2:
                err("Usage: ping <name/#/ip>"); continue
            s = server.get(parts[1])
            if not s:
                err(f"Slave '{parts[1]}' not found."); continue
            t0 = time.time()
            if s.alive():
                ok(f"{s.label()} ALIVE  ({round((time.time()-t0)*1000,1)}ms)")
            else:
                err(f"{s.label()} did not respond.")

        # ── pingall ───────────────────────────────────────────────────────────
        elif action == "pingall":
            slaves = server.all_slaves()
            if not slaves:
                warn("No slaves connected."); continue
            results = {}
            lock = threading.Lock()
            def _p(sid, sl):
                t0 = time.time()
                a  = sl.alive()
                with lock: results[sid] = (sl, a, round((time.time()-t0)*1000,1))
            with ThreadPoolExecutor(max_workers=20) as pool:
                futs = [pool.submit(_p, sid, sl) for sid, sl in slaves.items()]
                for f in futs: f.result()
            rows = []
            for sid, (sl, alive, rtt) in sorted(results.items()):
                rows.append([
                    str(sid), sl.label(), sl.ip,
                    c("ALIVE", Fore.GREEN) if alive else c("DEAD", Fore.RED),
                    c(f"{rtt}ms", Fore.GREEN if rtt<100 else Fore.YELLOW) if alive else c("--", Style.DIM)
                ])
            if HAS_TABLE:
                for line in tabulate(rows, ["#","Name","IP","Status","RTT"],
                                     tablefmt="rounded_outline").splitlines():
                    print("  "+line)
            else:
                for r in rows:
                    print(f"  {r[0]:<4} {r[1]:<22} {r[2]:<16} {r[3]}  {r[4]}")

        # ── CHOCOLATEY MAIN MENU ──────────────────────────────────────────────
        elif action == "choco":
            if len(parts) < 2:
                err("Usage: choco <setup|install|uninstall|upgrade|list|search|status> ...")
                err("Type 'help' for full command reference.")
                continue

            sub = parts[1].lower()

            # ── choco status (all slaves) ─────────────────────────────────────
            if sub == "status":
                slaves = server.all_slaves()
                if not slaves:
                    warn("No slaves connected."); continue
                info("Checking Chocolatey status on all slaves...")
                results = {}
                lock = threading.Lock()
                def _cs(sid, sl):
                    r = sl.cmd({"type": "choco_status"}, timeout=15)
                    with lock: results[sid] = (sl, r)
                with ThreadPoolExecutor(max_workers=20) as pool:
                    futs = [pool.submit(_cs, sid, sl) for sid, sl in slaves.items()]
                    for f in futs: f.result()

                rows = []
                for sid, (sl, r) in sorted(results.items()):
                    if r and r.get("type") != "error":
                        ver   = r.get("version", "?")
                        inst  = r.get("installed", False)
                        adm   = r.get("is_admin", False)
                        rows.append([
                            str(sid), sl.label(), sl.ip,
                            c("YES v"+ver, Fore.GREEN) if inst else c("NOT INSTALLED", Fore.RED),
                            c("ADMIN", Fore.GREEN) if adm else c("user (need admin for installs)", Fore.YELLOW)
                        ])
                    else:
                        rows.append([str(sid), sl.label(), sl.ip,
                                     c("ERROR", Fore.RED), "?"])

                if HAS_TABLE:
                    for line in tabulate(rows, ["#","Name","IP","Chocolatey","Rights"],
                                         tablefmt="rounded_outline").splitlines():
                        print("  "+line)
                else:
                    for r in rows:
                        print(f"  {r[0]:<4} {r[1]:<22} {r[2]:<16} {r[3]}  {r[4]}")

            # ── choco setup <target|all> ──────────────────────────────────────
            elif sub == "setup":
                target = parts[2] if len(parts) > 2 else "all"
                warn("Chocolatey install requires ADMIN rights on slave PCs.")
                warn("If slave is not running as admin, you'll see an error with a fix hint.")
                if target == "all":
                    broadcast_cmd(server,
                                  {"type": "choco_install_self"},
                                  timeout=CHOCO_TIMEOUT,
                                  result_fmt="choco")
                else:
                    s = server.get(target)
                    if not s:
                        err(f"Slave '{target}' not found."); continue
                    info(f"Installing Chocolatey on {s.label()}...")
                    resp = s.cmd({"type": "choco_install_self"}, timeout=CHOCO_TIMEOUT)
                    print_choco_result(resp, s.label())

            # ── choco install <target|all> <package> ─────────────────────────
            elif sub == "install":
                # choco install all <pkg>
                # choco install <slave> <pkg> [version]
                if len(parts) < 4:
                    err("Usage: choco install <name/#/all> <package> [version]"); continue
                target = parts[2]
                pkg    = parts[3]
                ver    = parts[4] if len(parts) > 4 else None
                msg    = {"type": "choco_install", "package": pkg, "version": ver}

                if target == "all":
                    warn(f"Installing '{pkg}' on ALL slaves — requires admin on each.")
                    broadcast_cmd(server, msg, timeout=CHOCO_TIMEOUT, result_fmt="choco")
                else:
                    s = server.get(target)
                    if not s:
                        err(f"Slave '{target}' not found."); continue
                    info(f"Installing '{pkg}' on {s.label()}...")
                    resp = s.cmd(msg, timeout=CHOCO_TIMEOUT)
                    print_choco_result(resp, s.label())

            # ── choco uninstall <target> <package> ────────────────────────────
            elif sub == "uninstall":
                if len(parts) < 4:
                    err("Usage: choco uninstall <name/#/all> <package>"); continue
                target = parts[2]; pkg = parts[3]
                msg    = {"type": "choco_uninstall", "package": pkg}
                if target == "all":
                    broadcast_cmd(server, msg, timeout=120, result_fmt="choco")
                else:
                    s = server.get(target)
                    if not s:
                        err(f"Slave '{target}' not found."); continue
                    resp = s.cmd(msg, timeout=120)
                    print_choco_result(resp, s.label())

            # ── choco upgrade <target|all> [pkg] ──────────────────────────────
            elif sub == "upgrade":
                target = parts[2] if len(parts) > 2 else "all"
                pkg    = parts[3] if len(parts) > 3 else "all"
                msg    = {"type": "choco_upgrade", "package": pkg}
                if target == "all":
                    warn(f"Upgrading '{pkg}' on ALL slaves...")
                    broadcast_cmd(server, msg, timeout=CHOCO_TIMEOUT, result_fmt="choco")
                else:
                    s = server.get(target)
                    if not s:
                        err(f"Slave '{target}' not found."); continue
                    info(f"Upgrading '{pkg}' on {s.label()}...")
                    resp = s.cmd(msg, timeout=CHOCO_TIMEOUT)
                    print_choco_result(resp, s.label())

            # ── choco list <target> ───────────────────────────────────────────
            elif sub == "list":
                target = parts[2] if len(parts) > 2 else None
                if not target:
                    err("Usage: choco list <name/#/all>"); continue
                if target == "all":
                    broadcast_cmd(server, {"type": "choco_list"}, timeout=60)
                else:
                    s = server.get(target)
                    if not s:
                        err(f"Slave '{target}' not found."); continue
                    resp = s.cmd({"type": "choco_list"}, timeout=60)
                    if resp and resp.get("type") != "error":
                        print(resp.get("stdout", "").rstrip())
                    elif resp:
                        err(resp.get("msg"))

            # ── choco search <query> ──────────────────────────────────────────
            elif sub == "search":
                if len(parts) < 3:
                    err("Usage: choco search <query>"); continue
                # Search runs locally or on first slave
                slaves = server.all_slaves()
                if not slaves:
                    warn("No slaves connected."); continue
                sl   = list(slaves.values())[0]
                resp = sl.cmd({"type": "choco_search",
                               "query": " ".join(parts[2:])}, timeout=60)
                if resp and resp.get("type") != "error":
                    print(resp.get("stdout","").rstrip())
                elif resp:
                    err(resp.get("msg"))

            else:
                err(f"Unknown choco sub-command: '{sub}'")
                err("Available: setup install uninstall upgrade list search status")

        # ── help ──────────────────────────────────────────────────────────────
        elif action in ("help", "h", "?"):
            print_help()

        # ── quit ──────────────────────────────────────────────────────────────
        elif action in ("quit", "exit", "q"):
            print(c("\n  Goodbye!\n", Fore.CYAN, Style.BRIGHT))
            break

        else:
            err(f"Unknown command: '{action}' — type 'help'")

# =============================================================================
# ENTRY POINT
# =============================================================================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"

def main():
    p = argparse.ArgumentParser(description="Pupy Master CLI v2")
    p.add_argument("--port", type=int, default=LISTEN_PORT)
    args = p.parse_args()

    banner()
    my_ip = get_local_ip()
    info(f"Your IP    : {c(my_ip, Fore.GREEN, Style.BRIGHT)}")
    info(f"Listen port: {args.port}")
    print()
    info(f"On each slave PC run:")
    print(c(f"    python slave.py --master {my_ip} --port {args.port}", Fore.WHITE, Style.BRIGHT))
    print()

    server = MasterServer(port=args.port)
    server.start()
    cli_loop(server)

if __name__ == "__main__":
    main()