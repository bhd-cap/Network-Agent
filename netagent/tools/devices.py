"""Device inventory access.

Agents may run *read-only* commands on their own. Anything that changes a device
goes through a change request that a human approves in the console first.
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from ..db import now
from ..security import redact_secrets
from . import ToolContext, ToolError, tool

if TYPE_CHECKING:  # pragma: no cover
    from ..runtime import Runtime

PLATFORMS = {
    "cisco_ios": "Cisco IOS",
    "cisco_xe": "Cisco IOS-XE",
    "cisco_nxos": "Cisco NX-OS",
    "cisco_asa": "Cisco ASA",
    "cisco_xr": "Cisco IOS-XR",
    "arista_eos": "Arista EOS",
    "fortinet": "Fortinet FortiOS",
}

# ---------------------------------------------------------------------------
# Read-only command guard
# ---------------------------------------------------------------------------
_CISCO_LIKE = ("show ", "ping ", "traceroute ")
READ_ONLY_PREFIXES: dict[str, tuple[str, ...]] = {
    "cisco_ios": _CISCO_LIKE,
    "cisco_xe": _CISCO_LIKE,
    "cisco_nxos": _CISCO_LIKE,
    "cisco_xr": _CISCO_LIKE,
    "cisco_asa": _CISCO_LIKE + ("packet-tracer ",),
    "arista_eos": _CISCO_LIKE,
    "fortinet": (
        "get ", "show", "execute ping ", "execute ping6 ", "execute traceroute ",
        "diagnose vpn tunnel list", "diagnose vpn ike gateway list", "diagnose vpn ipsec status",
        "diagnose sys ha status", "diagnose sys ha checksum", "diagnose sys ntp status",
        "diagnose sys top-summary", "diagnose hardware deviceinfo nic", "diagnose ip address list",
        "diagnose ip arp list", "diagnose ip route list", "diagnose netlink aggregate name",
        "diagnose netlink interface list", "diagnose lldprx neighbor", "diagnose sys sdwan health-check",
        "diagnose sys sdwan member", "diagnose sys sdwan service", "diagnose firewall proute list",
        "diagnose sys session stat", "diagnose autoupdate versions",
    ),
}
_SAFE_PIPES = {"include", "inc", "i", "exclude", "exc", "e", "begin", "beg", "b", "section", "sec", "count",
               "json", "json-pretty", "grep", "egrep", "no-more", "nz", "last", "head", "tail", "wc", "sort", "uniq"}
_UNSAFE_PIPES = {"redirect", "tee", "append", "save", "copy", "write", "bash", "vsh", "sh", "python", "email"}
_FORBIDDEN = re.compile(r"[;\n\r<>]|&&")
_REPEAT = re.compile(r"\b(?:repeat|count)\s+(\d+)")


def check_read_only(platform: str, command: str) -> str | None:
    """Returns None when the command is safe to run, else the reason it is refused."""
    cmd = command.strip()
    if not cmd:
        return "empty command"
    if len(cmd) > 400:
        return "command too long"
    if _FORBIDDEN.search(cmd):
        return "command chaining, redirection and multi-line input are not allowed"
    prefixes = READ_ONLY_PREFIXES.get(platform)
    if prefixes is None:
        return f"unsupported platform '{platform}'"
    low = cmd.lower()
    head, *pipes = [part.strip() for part in low.split("|")]
    if not any(head == p.strip() or head.startswith(p) for p in prefixes):
        allowed = ", ".join(sorted({p.split()[0] for p in prefixes}))
        return (f"only read-only commands are allowed on {PLATFORMS.get(platform, platform)} "
                f"(starting with: {allowed}). Use propose_config_change for anything that modifies the device.")
    repeat = _REPEAT.search(head)
    if repeat and int(repeat.group(1)) > 100:
        return "ping/traceroute repeat counts above 100 are not allowed"
    for index, pipe in enumerate(pipes):
        words = pipe.split()
        word = words[0] if words else ""
        if word in _SAFE_PIPES:
            continue
        # After the first modifier, a bare token is regex alternation (include up|down).
        if index > 0 and len(words) == 1 and word not in _UNSAFE_PIPES:
            continue
        return f"output modifier '| {word}' is not allowed"
    return None


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------
BACKUP_COMMAND = {"fortinet": "show full-configuration"}
SAVE_COMMAND = {
    "cisco_ios": "write memory", "cisco_xe": "write memory", "cisco_asa": "write memory",
    "cisco_nxos": "copy running-config startup-config", "arista_eos": "write memory",
    "cisco_xr": "",  # XR commits are persistent
    "fortinet": "",  # FortiOS saves on 'end'
}

_SIM_OUTPUT = {
    "cisco": {
        "show version": "Cisco IOS XE Software, Version 17.09.04a\n{name} uptime is 41 weeks, 2 days, 5 hours\n"
                        "System image file is \"flash:packages.conf\"\ncisco C9300-48P (X86) processor with 1331809K/6147K bytes of memory.\n"
                        "Processor board ID FOC2412L0AB\nConfiguration register is 0x102",
        "show ip interface brief": "Interface              IP-Address      OK? Method Status                Protocol\n"
                                   "Vlan10                 10.10.10.1      YES NVRAM  up                    up\n"
                                   "Vlan20                 10.10.20.1      YES NVRAM  up                    up\n"
                                   "GigabitEthernet1/0/1   unassigned      YES unset  up                    up\n"
                                   "GigabitEthernet1/0/2   unassigned      YES unset  down                  down\n"
                                   "TenGigabitEthernet1/1/1 10.255.0.2     YES NVRAM  up                    up\n"
                                   "Loopback0              10.0.0.11       YES NVRAM  up                    up",
        "show ip bgp summary": "BGP router identifier 10.0.0.11, local AS number 65010\n"
                               "Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd\n"
                               "10.255.0.1      4        65000  184211  183990     9142    0    0 5w2d            412\n"
                               "10.255.0.5      4        65000       0       0        1    0    0 00:14:37 Active",
        "show vlan brief": "VLAN Name                             Status    Ports\n"
                           "1    default                          active    Gi1/0/47, Gi1/0/48\n"
                           "10   USERS                            active    Gi1/0/1, Gi1/0/2, Gi1/0/3\n"
                           "20   VOICE                            active    Gi1/0/1, Gi1/0/2\n"
                           "99   MGMT                             active",
        "show running-config": "hostname {name}\n!\nenable secret 9 $9$abcdefghijk\n!\nvlan 10\n name USERS\nvlan 20\n name VOICE\n!\n"
                               "interface Vlan10\n ip address 10.10.10.1 255.255.255.0\n!\nrouter bgp 65010\n"
                               " neighbor 10.255.0.1 remote-as 65000\n neighbor 10.255.0.5 remote-as 65000\n!\n"
                               "snmp-server community s3cr3tRO RO\nline vty 0 4\n transport input ssh\n!\nend",
    },
    "arista": {
        "show version": "Arista DCS-7050SX3-48YC8\nHardware version: 11.10\nSerial number: JPE21234567\n"
                        "Software image version: 4.31.2F\nArchitecture: x86_64\nUptime: 12 weeks, 3 days, 1 hour and 12 minutes\n"
                        "Total memory: 8099732 kB\nFree memory: 5621840 kB",
        "show ip interface brief": "                                                                        Address\n"
                                   "Interface       IP Address         Status       Protocol         MTU    Owner\n"
                                   "Ethernet49/1    10.255.1.1/31      up           up              9214\n"
                                   "Ethernet50/1    10.255.1.3/31      up           up              9214\n"
                                   "Loopback0       10.0.1.1/32        up           up             65535\n"
                                   "Management1     192.168.0.21/24    up           up              1500\n"
                                   "Vlan4094        169.254.0.1/30     up           up              9214",
        "show ip bgp summary": "BGP summary information for VRF default\nRouter identifier 10.0.1.1, local AS number 65101\n"
                               "  Neighbor     V AS      MsgRcvd MsgSent  InQ OutQ  Up/Down State   PfxRcd PfxAcc\n"
                               "  10.255.1.0   4 65100     91022   91017    0    0   63d02h Estab   38     38\n"
                               "  10.255.1.2   4 65100     91019   91020    0    0   63d02h Estab   38     38",
        "show mlag": "MLAG Configuration:\ndomain-id           :              MLAG1\nlocal-interface     :           Vlan4094\n"
                     "peer-address        :        169.254.0.2\npeer-link           :     Port-Channel10\n\nMLAG Status:\n"
                     "state               :             Active\nnegotiation status  :          Connected\npeer-link status    :                 Up\n"
                     "local-int status    :                 Up\n\nMLAG Ports:\nDisabled            :                  0\nActive-full         :                 12",
        "show running-config": "! device: {name} (DCS-7050SX3-48YC8, EOS-4.31.2F)\nhostname {name}\n!\nusername admin privilege 15 role network-admin secret sha512 $6$abcdef\n!\n"
                               "vlan 100\n   name SERVERS\n!\ninterface Ethernet49/1\n   no switchport\n   ip address 10.255.1.1/31\n!\n"
                               "router bgp 65101\n   neighbor 10.255.1.0 remote-as 65100\n   neighbor 10.255.1.2 remote-as 65100\n!\nend",
    },
    "fortinet": {
        "get system status": "Version: FortiGate-200F v7.4.4,build2662,240514 (GA.F)\nSerial-Number: FG200FT921900001\nHostname: {name}\n"
                             "Operation Mode: NAT\nCurrent virtual domain: root\nCurrent HA mode: a-p, primary\nSystem time: Mon Nov 16 09:12:44 2026",
        "get system interface physical": "== [onboard]\n\t==[port1]\n\t\tmode: static\n\t\tip: 203.0.113.10 255.255.255.248\n\t\tstatus: up\n\t\tspeed: 1000Mbps (Duplex: full)\n"
                                         "\t==[port2]\n\t\tmode: static\n\t\tip: 10.10.0.1 255.255.255.0\n\t\tstatus: up\n\t\tspeed: 10000Mbps (Duplex: full)\n"
                                         "\t==[port3]\n\t\tmode: static\n\t\tip: 0.0.0.0 0.0.0.0\n\t\tstatus: down\n\t\tspeed: n/a",
        "get router info routing-table all": "Routing table for VRF=0\nS*      0.0.0.0/0 [10/0] via 203.0.113.9, port1, [1/0]\n"
                                             "C       10.10.0.0/24 is directly connected, port2\nB       10.20.0.0/16 [20/0] via 169.254.10.2 (recursive via VPN-BRANCH tunnel 198.51.100.7), 02w3d\n"
                                             "C       203.0.113.8/29 is directly connected, port1",
        "get vpn ipsec tunnel summary": "'VPN-BRANCH' 198.51.100.7:0  selectors(total,up): 2/1  rx(pkt,err): 918273/0  tx(pkt,err): 1002931/4\n"
                                        "'VPN-DR' 192.0.2.44:0  selectors(total,up): 1/0  rx(pkt,err): 0/0  tx(pkt,err): 0/0",
        "get system ha status": "HA Health Status: OK\nModel: FortiGate-200F\nMode: HA A-P\nGroup: 12\nCluster Uptime: 96 days 4:11:27\n"
                                "Primary: {name}, FG200FT921900001, HA cluster index = 0\nSecondary: {name}-b, FG200FT921900002, HA cluster index = 1\nConfiguration Status:\n"
                                "    FG200FT921900001(updated 2 seconds ago): in-sync\n    FG200FT921900002(updated 4 seconds ago): in-sync",
        "show full-configuration": "config system global\n    set hostname \"{name}\"\n    set admintimeout 5\nend\nconfig system interface\n    edit \"port1\"\n"
                                   "        set ip 203.0.113.10 255.255.255.248\n        set allowaccess ping\n        set role wan\n    next\nend\n"
                                   "config vpn ipsec phase1-interface\n    edit \"VPN-BRANCH\"\n        set remote-gw 198.51.100.7\n        set psksecret ENC abcd1234\n    next\nend",
    },
}


def _family(platform: str) -> str:
    return "fortinet" if platform == "fortinet" else "arista" if platform == "arista_eos" else "cisco"


class SimulatedDriver:
    """Canned output for demos and tests - no network access."""

    def __init__(self, device: dict):
        self.device = device
        self.table = _SIM_OUTPUT[_family(device["platform"])]

    def run(self, command: str) -> str:
        base = command.strip().lower().split("|")[0].strip()
        for known, output in self.table.items():
            if base == known or base.startswith(known):
                return output.replace("{name}", self.device["name"])
        return f"% Simulated device: no canned output for '{command.strip()}'. Known: {', '.join(self.table)}"

    def configure(self, commands: list[str], save: bool) -> str:
        lines = [f"{self.device['name']}(config)# {c}" for c in commands]
        lines.append("[simulated device - nothing was changed]")
        if save:
            lines.append("[simulated save]")
        return "\n".join(lines)

    def test(self) -> str:
        return "simulated device reachable"


class NetmikoDriver:
    """SSH via Netmiko. Each call opens and closes its own session."""

    def __init__(self, device: dict, password: str, enable: str):
        self.device = device
        self.password = password
        self.enable = enable

    def _connect(self):
        try:
            from netmiko import ConnectHandler
        except ImportError as exc:
            raise ToolError("SSH device access needs the 'netmiko' package (pip install netmiko).") from exc
        params = {
            "device_type": self.device["platform"],
            "host": self.device["host"],
            "port": int(self.device.get("port") or 22),
            "username": self.device["username"],
            "password": self.password,
            "conn_timeout": 15,
            "fast_cli": False,
        }
        if self.enable:
            params["secret"] = self.enable
        conn = ConnectHandler(**params)
        if self.enable and self.device["platform"] != "fortinet":
            conn.enable()
        return conn

    def run(self, command: str) -> str:
        conn = self._connect()
        try:
            return conn.send_command(command, read_timeout=60)
        finally:
            conn.disconnect()

    def configure(self, commands: list[str], save: bool) -> str:
        conn = self._connect()
        try:
            if self.device["platform"] == "fortinet":
                # FortiOS has no global config mode: send the lines as typed.
                output = "\n".join(conn.send_command_timing(c, read_timeout=30) for c in commands)
            else:
                output = conn.send_config_set(commands, read_timeout=120)
                if self.device["platform"] == "cisco_xr":
                    output += "\n" + conn.commit()
                    conn.exit_config_mode()
            save_cmd = SAVE_COMMAND.get(self.device["platform"], "")
            if save and save_cmd:
                output += "\n" + conn.send_command_timing(save_cmd, read_timeout=60)
            return output
        finally:
            conn.disconnect()

    def test(self) -> str:
        conn = self._connect()
        try:
            return f"connected, prompt: {conn.find_prompt()}"
        finally:
            conn.disconnect()


def get_driver(rt: "Runtime", device: dict):
    if device["transport"] == "simulated":
        return SimulatedDriver(device)
    if not device.get("host") or not device.get("username"):
        raise ToolError(f"Device '{device['name']}' has no host/username configured.")
    return NetmikoDriver(device, rt.box.decrypt(device.get("password_enc", "")), rt.box.decrypt(device.get("enable_enc", "")))


def find_device(rt: "Runtime", name: str) -> dict:
    device = rt.db.one("SELECT * FROM devices WHERE name = ? COLLATE NOCASE OR host = ?", [name.strip(), name.strip()])
    if not device:
        names = ", ".join(d["name"] for d in rt.db.query("SELECT name FROM devices ORDER BY name LIMIT 40")) or "none"
        raise ToolError(f"No device named '{name}' in the inventory. Known devices: {names}.")
    return device


async def run_on_device(rt: "Runtime", device: dict, command: str) -> str:
    driver = get_driver(rt, device)
    try:
        output = await asyncio.wait_for(asyncio.to_thread(driver.run, command), timeout=120)
    except ToolError:
        raise
    except asyncio.TimeoutError as exc:
        raise ToolError(f"Timed out running the command on {device['name']}.") from exc
    except Exception as exc:
        raise ToolError(f"Could not run the command on {device['name']}: {exc.__class__.__name__}: {exc}") from exc
    if rt.db.get_setting("redact_secrets", True):
        output = redact_secrets(output)
    return output


async def apply_change(rt: "Runtime", change: dict, save: bool) -> tuple[bool, str, str]:
    """Push an approved change request. Returns (ok, output, pre-change backup)."""
    device = rt.db.get("devices", change["device_id"])
    if not device:
        return False, "Device no longer exists.", ""
    if not device["allow_config"]:
        return False, f"Configuration changes are not enabled for {device['name']} (inventory setting).", ""
    driver = get_driver(rt, device)
    commands = [c for c in change["commands"].splitlines() if c.strip()]
    backup = ""
    try:
        backup_cmd = BACKUP_COMMAND.get(device["platform"], "show running-config")
        backup = await asyncio.wait_for(asyncio.to_thread(driver.run, backup_cmd), timeout=120)
        output = await asyncio.wait_for(asyncio.to_thread(driver.configure, commands, save), timeout=300)
        verify = [c for c in (change.get("verify") or "").splitlines() if c.strip()]
        for command in verify[:8]:
            if check_read_only(device["platform"], command) is None:
                result = await asyncio.wait_for(asyncio.to_thread(driver.run, command), timeout=120)
                output += f"\n\n--- verify: {command} ---\n{result}"
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}", backup
    # Device rejections come back as output, not exceptions.
    rejected = re.search(r"(?im)^(%\s*(invalid|incomplete|ambiguous)|.*command parse error|.*unknown action)", output)
    return (not rejected), redact_secrets(output), backup


async def test_device(rt: "Runtime", device: dict) -> tuple[bool, str]:
    try:
        driver = get_driver(rt, device)
        return True, await asyncio.wait_for(asyncio.to_thread(driver.test), timeout=40)
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@tool(
    "list_devices", "device-access",
    "List the network devices in the inventory (name, platform, site, tags). Use it to find the exact device "
    "name before running commands.",
    {"filter": {"type": "string", "description": "Optional text to match against name, platform, site or tags"}},
)
async def list_devices(ctx: ToolContext, args: dict) -> str:
    needle = str(args.get("filter") or "").strip().lower()
    rows = ctx.rt.db.query("SELECT * FROM devices ORDER BY name")
    lines = []
    for d in rows:
        text = f"{d['name']} {d['platform']} {d['site']} {d['tags']} {d['host']}".lower()
        if needle and needle not in text:
            continue
        lines.append(
            f"- {d['name']} | {PLATFORMS.get(d['platform'], d['platform'])} | site: {d['site'] or '-'} | "
            f"tags: {d['tags'] or '-'} | config changes: {'allowed with approval' if d['allow_config'] else 'not allowed'}"
            + (" | simulated" if d["transport"] == "simulated" else "")
        )
    if not lines:
        return "No devices match." if needle else "The device inventory is empty. Ask an administrator to add devices in the console."
    return "\n".join(lines[:100])


@tool(
    "run_show_command", "device-access",
    "Run ONE read-only command on an inventory device and return its output (show ..., ping, traceroute; on "
    "FortiOS: get ..., show ..., execute ping, and a safe subset of diagnose). Configuration commands are "
    "refused - use propose_config_change. Credentials in the output are redacted.",
    {
        "device": {"type": "string", "description": "Device name from the inventory"},
        "command": {"type": "string", "description": "A single read-only CLI command"},
    },
    ["device", "command"],
)
async def run_show_command(ctx: ToolContext, args: dict) -> str:
    device = find_device(ctx.rt, str(args["device"]))
    command = str(args["command"]).strip()
    refusal = check_read_only(device["platform"], command)
    if refusal:
        raise ToolError(f"Refused: {refusal}")
    output = await run_on_device(ctx.rt, device, command)
    return f"{device['name']}# {command}\n{output}"


@tool(
    "propose_config_change", "device-access",
    "Submit a configuration change for human approval. Nothing is pushed until an administrator approves it in "
    "the console; you will not see the outcome within this task, so finish by telling the requester the change "
    "request ID and what will happen on approval. Always include rollback commands and verification commands.",
    {
        "device": {"type": "string", "description": "Device name from the inventory"},
        "summary": {"type": "string", "description": "One-line description of the change"},
        "rationale": {"type": "string", "description": "Why the change is needed and what it affects"},
        "commands": {"type": "array", "items": {"type": "string"},
                     "description": "Configuration lines exactly as they should be entered in config mode, in order"},
        "rollback_commands": {"type": "array", "items": {"type": "string"},
                              "description": "Lines that undo the change"},
        "verify_commands": {"type": "array", "items": {"type": "string"},
                            "description": "Read-only commands to run afterwards to confirm success"},
        "risk": {"type": "string", "enum": ["low", "medium", "high"],
                 "description": "Blast radius if the change goes wrong"},
    },
    ["device", "summary", "commands"],
)
async def propose_config_change(ctx: ToolContext, args: dict) -> str:
    device = find_device(ctx.rt, str(args["device"]))
    commands = [str(c).rstrip() for c in (args.get("commands") or []) if str(c).strip()]
    if not commands:
        raise ToolError("'commands' must contain at least one configuration line.")
    if len(commands) > 300:
        raise ToolError("Too many lines for one change request (max 300). Split the change.")
    rollback = [str(c).rstrip() for c in (args.get("rollback_commands") or []) if str(c).strip()]
    verify = [str(c).strip() for c in (args.get("verify_commands") or []) if str(c).strip()]
    risk = str(args.get("risk") or "medium").lower()
    if risk not in {"low", "medium", "high"}:
        risk = "medium"
    change_id = ctx.rt.db.insert("change_requests", {
        "task_id": ctx.task["id"], "agent_id": ctx.agent["id"], "device_id": device["id"],
        "summary": str(args["summary"])[:300], "rationale": str(args.get("rationale") or ""),
        "risk": risk, "commands": "\n".join(commands), "rollback": "\n".join(rollback),
        "verify": "\n".join(verify), "created_at": now(),
    })
    ctx.rt.orchestrator.add_event(
        ctx.task["id"], "approval", f"Change request CR-{change_id} submitted for {device['name']}: {args['summary']}",
        {"change_id": change_id, "device": device["name"], "risk": risk},
    )
    ctx.rt.bus.publish("change.created", change_id=change_id)
    await ctx.rt.notify_change_request(change_id)
    note = "" if device["allow_config"] else (
        f" Note: configuration pushes are currently disabled for {device['name']} in the inventory; an administrator "
        "must enable them, or apply the change manually."
    )
    return (f"Change request CR-{change_id} created and waiting for human approval ({len(commands)} line(s), "
            f"risk {risk}). It will only be pushed to {device['name']} after approval.{note}")
