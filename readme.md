# Pupy Master & Slave Agent v2

A lightweight, **SSH-free** remote management system designed for Windows environments. This tool allows a single Master CLI to control dozens of Slave PCs simultaneously, specializing in **Chocolatey** package management, remote shell execution, and file transfers.

Unlike traditional agents, the Slaves connect to the Master (outbound), making it ideal for managing PCs behind NAT or firewalls without complex port forwarding on the client side.

---

## 🚀 Key Features

- **Zero SSH Required**: Uses a custom TCP protocol with length-prefixed JSON messaging.
- **Chocolatey Orchestration**:
  - One-click Chocolatey installation on all connected slaves.
  - Broadcast `install`, `upgrade`, `uninstall`, and `search` commands across the entire fleet.
- **Real-Time Shell**: Interactive shell for individual slaves or broadcast commands to all.
- **Puppet Integration**: Remotely apply `.pp` manifests to Windows nodes via the `apply` command.
- **File Management**: Built-in `upload`, `download`, and `ls` capabilities.
- **Persistence**: Assign friendly names to Slaves saved to `pupy_hosts.json` to track hardware across sessions.
- **Automatic Reconnect**: Slaves include a heartbeat system and automatically attempt to reconnect if the Master goes offline.

---

## 🛠️ Installation

### Prerequisites

- **Python 3.x** installed on Master and Slaves.

### Master Dependencies

```bash
pip install colorama tabulate
```

### Slave Dependencies

None (Standard Library only).

---

## 📖 Usage Guide

### 1️⃣ Start the Master

Run the master on your central control machine. It will display your local IP and listen for incoming connections.

```bash
python master_v3.py --port 9999
```

---

### 2️⃣ Connect the Slaves

Run the slave script on any number of target Windows PCs.

#### Basic connection

```bash
python slave_v4_5.py --master <MASTER_IP> --port 9999
```

#### Install to Windows Startup (Registry)

```bash
python slave_v4_5.py --master <MASTER_IP> --install
```

> **Note:** Run the slave as Administrator on Windows to allow Chocolatey package installations.

---

### 3️⃣ Master Commands

Once slaves are connected, use the interactive CLI:

| Command | Description |
|----------|------------|
| `list` | Show all connected slaves, their OS, and Admin status. |
| `connect <#>` | Open an interactive session with a specific slave. |
| `broadcast <cmd>` | Execute a shell command on every connected PC. |
| `choco setup all` | Automatically install Chocolatey on every slave. |
| `choco install all <pkg>` | Install a specific package on every PC. |
| `rename <#> <name>` | Give a slave a friendly name for easier tracking. |

---

## 📁 File Structure

```
master_v3.py       # Central Controller / Server CLI
slave_v4_5.py      # Agent deployed on client PCs
pupy_hosts.json    # (Generated) Stores persistent friendly names
```

---

## ⚠️ Safety & Security

This tool is designed for **internal network administration only**.

- **No Encryption**: Traffic is sent via raw TCP. Do not use this over the public internet without a VPN.
- **Admin Access**: The slave can execute any command sent by the master. Ensure only authorized users have access to the Master CLI.

---

## 📌 Optional Add-ons

Would you like me to generate:

- A `requirements.txt` file?
- A Windows `.bat` deployment script?
- A PowerShell auto-install script?
- An encrypted version using SSL/TLS?