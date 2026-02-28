#!/usr/bin/env python3
"""
=============================================================
  PUPPET SLAVE AGENT  v2  -  Complete & Real-Time Edition
=============================================================
"""

import socket, subprocess, json, os, sys, time
import platform, threading, argparse, struct, shutil

DEFAULT_MASTER_IP   = "192.168.1.100"
DEFAULT_MASTER_PORT = 9999
RECONNECT_DELAY     = 5
HEARTBEAT_INTERVAL  = 15

# =============================================================================
# UTILITIES
# =============================================================================
def get_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception:
        return "127.0.0.1"

def get_info():
    return {
        "hostname":    socket.gethostname(),
        "ip":          get_ip(),
        "os":          platform.system(),
        "os_version":  platform.version(),
        "machine":     platform.machine(),
        "user":        os.environ.get("USERNAME") or os.environ.get("USER", "unknown"),
        "python":      sys.version.split()[0],
        "cwd":         os.getcwd(),
        "choco":       get_choco_version(),
        "is_admin":    _is_admin(),
    }

def _is_admin():
    try:
        if platform.system() == "Windows":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        else:
            return os.geteuid() == 0
    except Exception:
        return False

def get_choco_version():
    choco = shutil.which("choco") or shutil.which("choco.exe")
    if not choco: return "not installed"
    try:
        r = subprocess.run(["choco", "--version"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "installed"
    except Exception:
        return "installed (version unknown)"

# =============================================================================
# PROTOCOL
# =============================================================================
def send_msg(sock, data: dict):
    raw = json.dumps(data).encode("utf-8")
    sock.sendall(struct.pack(">I", len(raw)) + raw)

def recv_msg(sock) -> dict:
    header = _recv_exact(sock, 4)
    if not header: return None
    n = struct.unpack(">I", header)[0]
    raw = _recv_exact(sock, n)
    if not raw: return None
    return json.loads(raw.decode("utf-8"))

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk: return None
        buf += chunk
    return buf

# =============================================================================
# COMMAND HANDLERS (REAL-TIME STREAMING)
# =============================================================================
def run_shell(cmd, timeout=360):
    print(f"\n[MASTER-CMD] Executing: '{cmd}'")
    try:
        # Popen allows us to read output in real-time instead of waiting
        process = subprocess.Popen(
            cmd, shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, 
            text=True,
            bufsize=1,
            universal_newlines=True
        )
        
        output_lines = []
        for line in process.stdout:
            print(line, end="", flush=True) # Print to Slave's screen real-time
            output_lines.append(line)
            
        process.wait(timeout=timeout)
        full_output = "".join(output_lines)
        
        print(f"\n[MASTER-CMD] Command finished with exit code {process.returncode}")
        return {
            "type":       "shell_result",
            "stdout":     full_output,
            "stderr":     "",
            "returncode": process.returncode,
            "cmd":        cmd,
        }
    except subprocess.TimeoutExpired:
        process.kill()
        print(f"\n[MASTER-CMD] Command timed out after {timeout}s")
        return {"type": "error", "msg": f"Timed out after {timeout}s"}
    except Exception as e:
        print(f"\n[MASTER-CMD] ERROR: {e}")
        return {"type": "error", "msg": str(e)}

# ── CHOCOLATEY ────────────────────────────────────────────────────────────────

def choco_install_self():
    print("\n[MASTER-CMD] Received request to setup/install Chocolatey...")
    if platform.system() != "Windows":
        return {"type": "error", "msg": "Chocolatey is Windows-only."}

    ver = get_choco_version()
    if ver != "not installed":
        print(f"[MASTER-CMD] Chocolatey is already installed (v{ver}). Skipping.")
        return {"type": "choco_result", "action": "install_choco", "stdout": f"Chocolatey already installed: v{ver}", "stderr": "", "returncode": 0}

    if not _is_admin():
        print("[MASTER-CMD] ERROR: Master tried to install Chocolatey, but this slave lacks Admin rights!")
        return {"type": "error", "msg": "Admin rights required to install Chocolatey."}

    ps_cmd = (
        "Set-ExecutionPolicy Bypass -Scope Process -Force; "
        "[System.Net.ServicePointManager]::SecurityProtocol = "
        "[System.Net.ServicePointManager]::SecurityProtocol -bor 3072; "
        "iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))"
    )
    cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{ps_cmd}"'
    r   = run_shell(cmd, timeout=300)
    r["action"] = "install_choco"
    r["type"]   = "choco_result"
    r["choco_version"] = get_choco_version()
    
    if r["returncode"] == 0:
        print("[MASTER-CMD] Chocolatey installed successfully.")
    return r

def choco_install_package(package, version=None, extra_args=""):
    print(f"\n[MASTER-CMD] Master is requesting Chocolatey to INSTALL: '{package}'")
    if platform.system() != "Windows": return {"type": "error", "msg": "Windows-only."}
    if not _is_admin(): return {"type": "error", "msg": "Admin rights required."}

    pkg_arg = package + (f" --version {version}" if version else "")
    cmd = f"choco install {pkg_arg} -y --no-progress {extra_args}"
    
    r = run_shell(cmd, timeout=360) 
    r["type"], r["action"], r["package"] = "choco_result", "install_package", package
    return r

def choco_uninstall_package(package):
    print(f"\n[MASTER-CMD] Master is requesting Chocolatey to UNINSTALL: '{package}'")
    if platform.system() != "Windows": return {"type": "error", "msg": "Windows-only."}
    if not _is_admin(): return {"type": "error", "msg": "Admin rights required."}

    cmd = f"choco uninstall {package} -y --no-progress"
    r   = run_shell(cmd, timeout=120)
    r["type"], r["action"], r["package"] = "choco_result", "uninstall_package", package
    return r

def choco_upgrade_package(package="all"):
    print(f"\n[MASTER-CMD] Master is requesting Chocolatey to UPGRADE: '{package}'")
    if platform.system() != "Windows": return {"type": "error", "msg": "Windows-only."}
    if not _is_admin(): return {"type": "error", "msg": "Admin rights required."}

    cmd = f"choco upgrade {package} -y --no-progress"
    r   = run_shell(cmd, timeout=360)
    r["type"], r["action"], r["package"] = "choco_result", "upgrade", package
    return r

# ── FILE OPS ─────────────────────────────────────────────────────────────────

def handle_upload(sock, msg):
    path = msg.get("path", "received_file")
    size = msg.get("size", 0)
    print(f"\n[MASTER-CMD] Master is uploading file to this PC: {path} ({size} bytes)")
    send_msg(sock, {"type": "ready"})
    received = b""
    while len(received) < size:
        chunk = sock.recv(min(65536, size - len(received)))
        if not chunk: break
        received += chunk
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f: f.write(received)
        print(f"[MASTER-CMD] File successfully saved.")
        return {"type": "ok", "msg": f"Saved {len(received)} bytes → '{path}'"}
    except Exception as e:
        return {"type": "error", "msg": str(e)}

def handle_download(sock, msg):
    path = msg.get("path", "")
    print(f"\n[MASTER-CMD] Master is downloading file from this PC: {path}")
    if not os.path.exists(path):
        send_msg(sock, {"type": "error", "msg": f"File not found: {path}"})
        return
    size = os.path.getsize(path)
    send_msg(sock, {"type": "file_meta", "size": size, "path": path})
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk: break
            sock.sendall(chunk)
    print(f"[MASTER-CMD] File sent to Master.")

def handle_listdir(msg):
    path = msg.get("path", ".")
    print(f"\n[MASTER-CMD] Master requested directory listing for: {path}")
    try:
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            entries.append({
                "name": name, 
                "type": "dir" if os.path.isdir(full) else "file", 
                "size": os.path.getsize(full) if os.path.isfile(full) else 0
            })
        return {"type": "listdir_result", "path": path, "entries": entries}
    except Exception as e:
        return {"type": "error", "msg": str(e)}

# =============================================================================
# MAIN MESSAGE DISPATCHER
# =============================================================================
def dispatch(sock, msg):
    t = msg.get("type", "")

    # Silent background operations
    if t == "ping": return {"type": "pong"}
    elif t == "heartbeat": return None
    
    # System Info
    elif t == "info":
        print("\n[MASTER-CMD] Master requested system info.")
        return {"type": "info_result", **get_info()}
    
    # Shell Execution
    elif t == "shell":
        return run_shell(msg.get("cmd", "echo ok"), msg.get("timeout", 60))
        
    # File Operations
    elif t == "upload":
        return handle_upload(sock, msg)
    elif t == "download":
        handle_download(sock, msg)
        return None # Response sent inline
    elif t == "listdir":
        return handle_listdir(msg)

    # Chocolatey Operations
    elif t == "choco_install_self":    # <--- This is the one that was missing!
        return choco_install_self()
    elif t == "choco_install":
        return choco_install_package(msg.get("package", ""), msg.get("version"), msg.get("args", ""))
    elif t == "choco_uninstall":
        return choco_uninstall_package(msg.get("package", ""))
    elif t == "choco_upgrade":
        return choco_upgrade_package(msg.get("package", "all"))
    elif t == "choco_list":
        print("\n[MASTER-CMD] Master requested list of installed packages.")
        r = run_shell("choco list --local-only --no-progress", timeout=60)
        r["type"], r["action"] = "choco_result", "list"
        return r
    elif t == "choco_search":
        print(f"\n[MASTER-CMD] Master searching Chocolatey for: '{msg.get('query', '')}'")
        r = run_shell(f"choco search {msg.get('query', '')} --no-progress --limit-output", timeout=60)
        r["type"], r["action"], r["query"] = "choco_result", "search", msg.get("query", "")
        return r
    elif t == "choco_status":
        ver = get_choco_version()
        return {"type": "choco_status", "version": ver, "installed": ver != "not installed", "is_admin": _is_admin(), "hostname": socket.gethostname()}
    
    # Puppet .pp File Execution
    elif t == "puppet_apply":
        print("\n[MASTER-CMD] Master is applying a Puppet (.pp) manifest...")
        code = msg.get("code", "")
        temp_path = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "master_manifest.pp")
        try:
            os.makedirs(os.path.dirname(temp_path), exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as f: f.write(code)
            r = run_shell(f"puppet apply {temp_path}", timeout=300)
            if os.path.exists(temp_path): os.remove(temp_path)
            return r
        except Exception as e:
            return {"type": "error", "msg": f"Failed to run .pp file: {str(e)}"}
            
    elif t == "die":
        print("\n[MASTER-CMD] Shutdown command received. Exiting.")
        os._exit(0)
    else:
        return {"type": "error", "msg": f"Unknown command type: '{t}'"}

# =============================================================================
# SLAVE AGENT CONNECTION
# =============================================================================
class SlaveAgent:
    def __init__(self, master_ip, master_port):
        self.master_ip, self.master_port = master_ip, master_port
        self.sock, self.running = None, True

    def connect(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((self.master_ip, self.master_port))
            self.sock.settimeout(None)
            send_msg(self.sock, {"type": "register", **get_info()})
            print(f"[SLAVE] Connected to Puppet Master at {self.master_ip}:{self.master_port}")
            return True
        except Exception as e:
            print(f"[SLAVE] Cannot connect — {e}. Retrying...")
            return False

    def run(self):
        info = get_info()
        print(f"=====================================================")
        print(f"               PUPPET SLAVE AGENT v2                 ")
        print(f"=====================================================")
        print(f"[SLAVE] Hostname  : {info['hostname']}")
        print(f"[SLAVE] IP Address: {info['ip']}")
        print(f"[SLAVE] Privileges: {'ADMINISTRATOR' if info['is_admin'] else 'Standard User'}")
        print(f"[SLAVE] Chocolatey: {info['choco']}")
        print(f"[SLAVE] Target    : {self.master_ip}:{self.master_port}")
        print(f"=====================================================\n")

        while self.running:
            if not self.connect():
                time.sleep(RECONNECT_DELAY)
                continue
            
            threading.Thread(target=self._heartbeat, daemon=True).start()
            
            try:
                while self.running:
                    msg = recv_msg(self.sock)
                    if msg is None: raise ConnectionError("Connection closed by master")
                    resp = dispatch(self.sock, msg)
                    if resp is not None: send_msg(self.sock, resp)
            except Exception as e:
                print(f"\n[SLAVE] Disconnected from Master: {e}")
            finally:
                try: self.sock.close()
                except Exception: pass
            
            time.sleep(RECONNECT_DELAY)

    def _heartbeat(self):
        while self.running and self.sock:
            time.sleep(HEARTBEAT_INTERVAL)
            try: send_msg(self.sock, {"type": "heartbeat"})
            except Exception: break

def install_autostart(master_ip, master_port):
    if platform.system() != "Windows": return
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        val = f'"{sys.executable}" "{os.path.abspath(__file__)}" --master {master_ip} --port {master_port}'
        winreg.SetValueEx(key, "PuppetSlave", 0, winreg.REG_SZ, val)
        winreg.CloseKey(key)
        print(f"[SLAVE] Auto-start installed in Windows Registry.")
    except Exception as e:
        print(f"[SLAVE] Could not install auto-start: {e}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--master", default=DEFAULT_MASTER_IP)
    p.add_argument("--port", type=int, default=DEFAULT_MASTER_PORT)
    p.add_argument("--install", action="store_true", help="Add to Windows startup")
    args = p.parse_args()

    if args.install: install_autostart(args.master, args.port)
    SlaveAgent(args.master, args.port).run()