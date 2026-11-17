"""Offline network-engineering tools (no device access needed)."""
from __future__ import annotations

import difflib
import ipaddress
import re
from pathlib import Path

from ..security import redact_secrets
from . import ToolContext, ToolError, tool

# ---------------------------------------------------------------------------
# subnet_calc
# ---------------------------------------------------------------------------


@tool(
    "subnet_calc", "network-core",
    "Calculate IPv4/IPv6 subnet details (network, mask, wildcard, usable range, host count). Optionally split "
    "the network into smaller subnets or check whether an address belongs to it. Use this instead of doing "
    "subnet arithmetic by hand.",
    {
        "cidr": {"type": "string", "description": "Network or interface address in CIDR form, e.g. 10.20.30.0/23 or 192.168.1.77/27"},
        "new_prefix": {"type": "integer", "description": "Optional: split into subnets of this prefix length"},
        "contains": {"type": "string", "description": "Optional: an IP address to test for membership"},
    },
    ["cidr"],
)
async def subnet_calc(ctx: ToolContext, args: dict) -> str:
    try:
        iface = ipaddress.ip_interface(str(args["cidr"]).strip())
    except ValueError as exc:
        raise ToolError(f"Invalid CIDR: {exc}") from exc
    net = iface.network
    lines = [f"Input: {iface.with_prefixlen}", f"Network: {net.with_prefixlen}"]
    if net.version == 4:
        lines += [
            f"Netmask: {net.netmask}",
            f"Wildcard mask: {net.hostmask}",
            f"Broadcast: {net.broadcast_address}",
        ]
        if net.prefixlen >= 31:
            lines.append(f"Usable hosts: {net.num_addresses} ({net[0]} - {net[-1]}) [point-to-point/host route]")
        else:
            lines.append(f"Usable hosts: {net.num_addresses - 2} ({net[1]} - {net[-2]})")
    else:
        lines.append(f"Addresses: {net.num_addresses}")
        lines.append(f"Range: {net[0]} - {net[-1]}")
    if iface.ip != net.network_address:
        lines.append(f"Host address {iface.ip} is inside {net.with_prefixlen}")
    lines.append(f"Private: {net.is_private}")

    if args.get("contains"):
        try:
            ip = ipaddress.ip_address(str(args["contains"]).strip())
            lines.append(f"{ip} in {net.with_prefixlen}: {ip in net}")
        except ValueError as exc:
            raise ToolError(f"Invalid 'contains' address: {exc}") from exc

    new_prefix = args.get("new_prefix")
    if new_prefix not in (None, ""):
        new_prefix = int(new_prefix)
        if new_prefix <= net.prefixlen or new_prefix > net.max_prefixlen:
            raise ToolError(f"new_prefix must be between {net.prefixlen + 1} and {net.max_prefixlen}")
        total = 2 ** (new_prefix - net.prefixlen)
        lines.append(f"\nSplit into /{new_prefix}: {total} subnets")
        for i, sub in enumerate(net.subnets(new_prefix=new_prefix)):
            if i >= 64:
                lines.append(f"... ({total - 64} more)")
                break
            lines.append(f"  {sub.with_prefixlen}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# vendor_command_reference
# ---------------------------------------------------------------------------
# intent -> (keywords, {vendor: command})
COMMANDS: list[tuple[str, str, dict[str, str]]] = [
    ("System / software version", "version system uptime model serial",
     {"cisco_ios": "show version", "cisco_nxos": "show version", "arista_eos": "show version",
      "fortinet": "get system status"}),
    ("Running configuration", "running config configuration show",
     {"cisco_ios": "show running-config", "cisco_nxos": "show running-config",
      "arista_eos": "show running-config", "fortinet": "show  (non-default settings) / show full-configuration"}),
    ("Interface summary (IP / state)", "interface interfaces ip brief status up down",
     {"cisco_ios": "show ip interface brief", "cisco_nxos": "show ip interface brief vrf all / show interface brief",
      "arista_eos": "show ip interface brief", "fortinet": "get system interface physical / diagnose ip address list"}),
    ("Interface counters and errors", "interface errors counters crc drops duplex speed",
     {"cisco_ios": "show interfaces <intf>", "cisco_nxos": "show interface <intf>",
      "arista_eos": "show interfaces <intf> / show interfaces counters errors",
      "fortinet": "diagnose hardware deviceinfo nic <port>"}),
    ("Switchport status", "switchport status vlan access trunk",
     {"cisco_ios": "show interfaces status / show interfaces trunk", "cisco_nxos": "show interface status / show interface trunk",
      "arista_eos": "show interfaces status / show interfaces trunk", "fortinet": "n/a on FortiGate (FortiSwitch: diagnose switch physical-ports summary)"}),
    ("Routing table", "route routing table rib",
     {"cisco_ios": "show ip route [vrf <name>]", "cisco_nxos": "show ip route [vrf <name>]",
      "arista_eos": "show ip route [vrf <name>]", "fortinet": "get router info routing-table all"}),
    ("Route lookup for one destination", "route lookup destination longest match",
     {"cisco_ios": "show ip route <ip> / show ip cef <ip>", "cisco_nxos": "show ip route <ip>",
      "arista_eos": "show ip route <ip>", "fortinet": "get router info routing-table details <ip>"}),
    ("BGP summary", "bgp summary neighbors peers state",
     {"cisco_ios": "show ip bgp summary / show bgp ipv4 unicast summary", "cisco_nxos": "show bgp ipv4 unicast summary [vrf all]",
      "arista_eos": "show ip bgp summary [vrf all]", "fortinet": "get router info bgp summary"}),
    ("BGP neighbor detail", "bgp neighbor detail advertised received routes",
     {"cisco_ios": "show ip bgp neighbors <ip> [advertised-routes|routes]",
      "cisco_nxos": "show bgp ipv4 unicast neighbors <ip> [advertised-routes|routes]",
      "arista_eos": "show ip bgp neighbors <ip> [advertised-routes|received-routes]",
      "fortinet": "get router info bgp neighbors <ip> [advertised-routes|routes]"}),
    ("OSPF neighbors", "ospf neighbor adjacency",
     {"cisco_ios": "show ip ospf neighbor", "cisco_nxos": "show ip ospf neighbors",
      "arista_eos": "show ip ospf neighbor", "fortinet": "get router info ospf neighbor"}),
    ("ARP table", "arp address resolution",
     {"cisco_ios": "show ip arp", "cisco_nxos": "show ip arp [vrf all]", "arista_eos": "show ip arp",
      "fortinet": "get system arp"}),
    ("MAC address table", "mac address table cam",
     {"cisco_ios": "show mac address-table", "cisco_nxos": "show mac address-table",
      "arista_eos": "show mac address-table", "fortinet": "n/a on FortiGate routed ports (FortiSwitch: diagnose switch mac-address list)"}),
    ("VLANs", "vlan vlans",
     {"cisco_ios": "show vlan brief", "cisco_nxos": "show vlan brief", "arista_eos": "show vlan",
      "fortinet": "show system interface  (VLANs are sub-interfaces with 'set vlanid')"}),
    ("LLDP / CDP neighbors", "lldp cdp neighbors topology",
     {"cisco_ios": "show cdp neighbors detail / show lldp neighbors", "cisco_nxos": "show cdp neighbors / show lldp neighbors",
      "arista_eos": "show lldp neighbors [detail]", "fortinet": "diagnose lldprx neighbor summary"}),
    ("Port-channel / LAG", "port-channel etherchannel lag lacp aggregate",
     {"cisco_ios": "show etherchannel summary / show lacp neighbor", "cisco_nxos": "show port-channel summary",
      "arista_eos": "show port-channel summary / show lacp neighbor", "fortinet": "diagnose netlink aggregate name <agg>"}),
    ("Spanning tree", "spanning tree stp root blocked",
     {"cisco_ios": "show spanning-tree [vlan <id>]", "cisco_nxos": "show spanning-tree [vlan <id>]",
      "arista_eos": "show spanning-tree [vlan <id>]", "fortinet": "n/a on FortiGate"}),
    ("Redundancy / HA / multi-chassis", "ha redundancy failover vpc mlag stack cluster",
     {"cisco_ios": "show redundancy / show switch (StackWise) / show standby brief (HSRP)", "cisco_nxos": "show vpc / show vpc consistency-parameters global",
      "arista_eos": "show mlag / show mlag config-sanity", "fortinet": "get system ha status / diagnose sys ha checksum cluster"}),
    ("IPsec VPN state", "vpn ipsec ike tunnel sa phase1 phase2",
     {"cisco_ios": "show crypto ikev2 sa / show crypto ipsec sa", "cisco_nxos": "n/a on most platforms",
      "arista_eos": "show ip security connection", "fortinet": "get vpn ipsec tunnel summary / diagnose vpn ike gateway list / diagnose vpn tunnel list"}),
    ("Session / connection table", "session connection nat translation state table",
     {"cisco_ios": "show ip nat translations (ASA: show conn)", "cisco_nxos": "n/a",
      "arista_eos": "n/a", "fortinet": "diagnose sys session filter <...> then diagnose sys session list"}),
    ("Logs", "log logs logging syslog events",
     {"cisco_ios": "show logging", "cisco_nxos": "show logging logfile", "arista_eos": "show logging [last 30 minutes]",
      "fortinet": "execute log filter category <n> then execute log display"}),
    ("CPU and memory", "cpu memory utilization performance resources",
     {"cisco_ios": "show processes cpu sorted / show memory statistics", "cisco_nxos": "show system resources",
      "arista_eos": "show processes top once", "fortinet": "get system performance status / diagnose sys top-summary"}),
    ("NTP", "ntp time clock sync",
     {"cisco_ios": "show ntp associations / show ntp status", "cisco_nxos": "show ntp peer-status",
      "arista_eos": "show ntp associations / show ntp status", "fortinet": "diagnose sys ntp status"}),
    ("Transceivers / optics", "transceiver optic sfp dom light levels",
     {"cisco_ios": "show interfaces transceiver detail", "cisco_nxos": "show interface transceiver details",
      "arista_eos": "show interfaces transceiver", "fortinet": "get system interface transceiver"}),
    ("Environment (power, fans, temperature)", "environment power fan temperature hardware",
     {"cisco_ios": "show environment all", "cisco_nxos": "show environment", "arista_eos": "show environment all",
      "fortinet": "execute sensor list"}),
    ("Ping with a source", "ping source reachability",
     {"cisco_ios": "ping <ip> source <intf|ip>", "cisco_nxos": "ping <ip> source <ip> [vrf <name>]",
      "arista_eos": "ping [vrf <name>] <ip> source <intf|ip>", "fortinet": "execute ping-options source <ip> then execute ping <ip>"}),
    ("Save configuration", "save write memory startup persist",
     {"cisco_ios": "copy running-config startup-config (write memory)", "cisco_nxos": "copy running-config startup-config",
      "arista_eos": "write memory (copy running-config startup-config)", "fortinet": "automatic on 'end' (unless cfg-save is manual/revert)"}),
    ("Review pending changes / config diff", "diff session candidate compare archive rollback",
     {"cisco_ios": "show archive config differences (with archive configured)", "cisco_nxos": "show diff rollback-patch checkpoint <a> running-config",
      "arista_eos": "configure session <name> ... show session-config diffs ... commit | abort",
      "fortinet": "no candidate config; use configuration revisions (System > Configuration > Revisions)"}),
]

VENDOR_ALIASES = {
    "cisco": ["cisco_ios", "cisco_nxos"], "ios": ["cisco_ios"], "ios-xe": ["cisco_ios"], "iosxe": ["cisco_ios"],
    "cisco_ios": ["cisco_ios"], "cisco_xe": ["cisco_ios"], "nxos": ["cisco_nxos"], "nx-os": ["cisco_nxos"],
    "cisco_nxos": ["cisco_nxos"], "nexus": ["cisco_nxos"], "arista": ["arista_eos"], "eos": ["arista_eos"],
    "arista_eos": ["arista_eos"], "fortinet": ["fortinet"], "fortigate": ["fortinet"], "fortios": ["fortinet"],
}
VENDOR_LABELS = {"cisco_ios": "Cisco IOS / IOS-XE", "cisco_nxos": "Cisco NX-OS", "arista_eos": "Arista EOS",
                 "fortinet": "Fortinet FortiOS"}


@tool(
    "vendor_command_reference", "network-core",
    "Look up the equivalent CLI command for a task across Cisco IOS/IOS-XE, Cisco NX-OS, Arista EOS and Fortinet "
    "FortiOS. Useful when translating a procedure between vendors.",
    {
        "topic": {"type": "string", "description": "What you want to do or see, e.g. 'bgp summary', 'interface errors', 'save config'"},
        "vendor": {"type": "string", "description": "Optional: limit to one vendor (cisco, ios, nxos, arista, fortinet)"},
    },
    ["topic"],
)
async def vendor_command_reference(ctx: ToolContext, args: dict) -> str:
    words = [w for w in re.findall(r"[a-z0-9-]+", str(args["topic"]).lower()) if len(w) > 1]
    vendor = str(args.get("vendor") or "").strip().lower()
    columns = VENDOR_ALIASES.get(vendor, list(VENDOR_LABELS)) if vendor else list(VENDOR_LABELS)
    scored = []
    for title, keywords, commands in COMMANDS:
        haystack = f"{title} {keywords}".lower()
        score = sum(1 for w in words if w in haystack)
        if score:
            scored.append((score, title, commands))
    if not scored:
        topics = "; ".join(t for t, _, _ in COMMANDS)
        return f"No entry matched '{args['topic']}'. Topics covered: {topics}."
    scored.sort(key=lambda s: -s[0])
    out = []
    for _, title, commands in scored[:4]:
        out.append(f"## {title}")
        out += [f"- {VENDOR_LABELS[c]}: `{commands[c]}`" for c in columns]
    out.append("\nSyntax varies by platform and release - confirm with '?' on the device.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# analyze_config
# ---------------------------------------------------------------------------
def detect_vendor(config: str) -> str:
    if re.search(r"(?m)^config (system|firewall|router|vpn) ", config) or "\nedit " in config and "\nnext" in config:
        return "fortinet"
    if re.search(r"(?m)^(! device: .*EOS|daemon TerminAttr|management api http-commands|transceiver qsfp default-mode)", config) \
            or re.search(r"(?m)^! Command: show running-config", config) and "Arista" in config:
        return "arista_eos"
    if re.search(r"(?m)^(feature \S+|vdc \S+|vpc domain)", config):
        return "cisco_nxos"
    if re.search(r"(?m)^(version \d+\.\d+|service timestamps|line vty|interface (GigabitEthernet|TenGigabitEthernet|Vlan))", config):
        return "cisco_ios"
    return "unknown"


def _blocks(config: str, start: str) -> list[tuple[str, list[str]]]:
    """Split an IOS/EOS style config into (header, body-lines) blocks whose header matches ``start``."""
    out, header, body = [], None, []
    for line in config.splitlines():
        if re.match(start, line):
            if header is not None:
                out.append((header, body))
            header, body = line.strip(), []
        elif header is not None:
            if line.startswith((" ", "\t")):
                body.append(line.strip())
            else:
                out.append((header, body))
                header, body = None, []
    if header is not None:
        out.append((header, body))
    return out


def lint_cisco_like(config: str, vendor: str) -> list[tuple[str, str]]:
    f: list[tuple[str, str]] = []
    eos = vendor == "arista_eos"
    nxos = vendor == "cisco_nxos"
    if re.search(r"(?m)^enable password ", config):
        f.append(("HIGH", "'enable password' is used - replace with 'enable secret' (strong hash)."))
    if re.search(r"(?m)^username \S+ .*\bpassword (0|7) ", config) or re.search(r"(?m)^username \S+ password \S+$", config):
        f.append(("HIGH", "Local user(s) with reversible/cleartext 'password' - use 'secret' with a strong hash."))
    if re.search(r"(?m)^username \S+ .*\bsecret 0 ", config):
        f.append(("HIGH", "Local user secret stored in cleartext (type 0)."))
    if not eos and not nxos and re.search(r"(?m)^no service password-encryption", config):
        f.append(("MEDIUM", "'no service password-encryption' - type 0 passwords are visible in the config."))
    for community in re.findall(r"(?m)^snmp-server community (\S+)", config):
        if community.lower() in {"public", "private"}:
            f.append(("HIGH", f"Default SNMP community '{community}' is configured."))
    if re.search(r"(?m)^snmp-server community \S+", config) and not re.search(r"(?m)^snmp-server (user|group) ", config):
        f.append(("MEDIUM", "SNMP v1/v2c communities in use - prefer SNMPv3 with authPriv."))
    for header, body in _blocks(config, r"^line vty"):
        text = " ".join(body)
        if re.search(r"transport input (all|telnet)", text) or ("transport input" not in text and not eos and not nxos):
            f.append(("HIGH", f"{header}: Telnet is permitted - set 'transport input ssh'."))
        if "access-class" not in text:
            f.append(("MEDIUM", f"{header}: no access-class restricting management sources."))
        if "exec-timeout 0 0" in text:
            f.append(("MEDIUM", f"{header}: 'exec-timeout 0 0' disables the idle timeout."))
    if re.search(r"(?m)^ip http server", config) and not eos:
        f.append(("MEDIUM", "'ip http server' (cleartext HTTP management) is enabled."))
    if eos and re.search(r"(?ms)^management api http-commands.*?^!", config) and re.search(r"(?m)^\s+protocol http\b(?!s)", config):
        f.append(("HIGH", "eAPI is enabled over cleartext HTTP - use 'protocol https' only."))
    if eos and re.search(r"(?ms)^management telnet\s*\n\s+no shutdown", config):
        f.append(("HIGH", "Telnet management is enabled."))
    if nxos and re.search(r"(?m)^feature telnet", config):
        f.append(("HIGH", "'feature telnet' is enabled."))
    if not re.search(r"(?m)^(logging (host|server|\d)|logging \S+ \d)", config) and not re.search(r"(?m)^logging host", config):
        f.append(("LOW", "No remote syslog server configured."))
    if not re.search(r"(?m)^ntp server", config):
        f.append(("LOW", "No NTP server configured - timestamps and certificate validation will drift."))
    if not re.search(r"(?m)^aaa (new-model|authentication|authorization)", config):
        f.append(("LOW", "No AAA configuration found - logins rely on local line/user settings."))
    if not eos and not nxos and re.search(r"(?m)^ip ssh version 1", config):
        f.append(("HIGH", "SSH version 1 is enabled."))
    undesc = 0
    for header, body in _blocks(config, r"^interface "):
        if re.search(r"(?i)(loopback|vlan|port-channel|mgmt|management|null)", header):
            continue
        if "shutdown" in body and "no shutdown" not in body:
            continue
        if not any(b.startswith("description") for b in body):
            undesc += 1
        text = " ".join(body)
        if "switchport mode trunk" in text and "switchport trunk allowed vlan" not in text:
            f.append(("LOW", f"{header}: trunk allows all VLANs - prune with 'switchport trunk allowed vlan'."))
        if re.search(r"switchport mode access", text) and "spanning-tree portfast" in text and "bpduguard" not in text \
                and not re.search(r"(?m)^spanning-tree portfast (edge )?bpduguard default", config):
            f.append(("LOW", f"{header}: PortFast without BPDU Guard."))
    if undesc:
        f.append(("INFO", f"{undesc} active interface(s) have no description."))
    for header, body in _blocks(config, r"^router bgp"):
        peers = {m.group(1) for b in body if (m := re.match(r"neighbor (\S+) remote-as", b))}
        filtered = {m.group(1) for b in body if (m := re.match(r"neighbor (\S+) (route-map|prefix-list|filter-list|distribute-list)", b))}
        unfiltered = sorted(peers - filtered)
        if unfiltered and not any(b.startswith(("address-family", "vrf")) for b in body):
            f.append(("MEDIUM", f"{header}: neighbor(s) without any inbound/outbound policy: {', '.join(unfiltered[:6])}."))
    return f


def lint_fortinet(config: str) -> list[tuple[str, str]]:
    f: list[tuple[str, str]] = []
    # interfaces
    iface_section = re.search(r"(?ms)^config system interface\n(.*?)^end", config)
    if iface_section:
        for name, body in re.findall(r'(?ms)^\s*edit "([^"]+)"\n(.*?)^\s*next', iface_section.group(1)):
            access = re.search(r"set allowaccess (.+)", body)
            if not access:
                continue
            services = access.group(1).split()
            wanish = bool(re.search(r"(?i)wan|internet|outside|isp", name)) or 'set role wan' in body
            if "telnet" in services:
                f.append(("HIGH", f"interface {name}: Telnet administrative access enabled."))
            if "http" in services:
                f.append(("MEDIUM" if not wanish else "HIGH", f"interface {name}: cleartext HTTP administrative access enabled."))
            if wanish and {"https", "ssh"} & set(services):
                f.append(("HIGH", f"interface {name}: management ({', '.join(sorted({'https', 'ssh'} & set(services)))}) "
                                  "exposed on a WAN-facing interface - restrict with trusted hosts or a local-in policy."))
    # admins
    admin_section = re.search(r"(?ms)^config system admin\n(.*?)^end", config)
    if admin_section:
        for name, body in re.findall(r'(?ms)^\s*edit "([^"]+)"\n(.*?)^\s*next', admin_section.group(1)):
            if "set trusthost" not in body:
                f.append(("MEDIUM", f"admin '{name}': no trusted hosts configured."))
            if "set two-factor" not in body:
                f.append(("LOW", f"admin '{name}': no two-factor authentication."))
    # policies
    policy_section = re.search(r"(?ms)^config firewall policy\n(.*?)^end", config)
    if policy_section:
        for pid, body in re.findall(r"(?ms)^\s*edit (\d+)\n(.*?)^\s*next", policy_section.group(1)):
            if "set action accept" not in body:
                continue
            anyany = all(re.search(p, body) for p in (r'set srcaddr "all"', r'set dstaddr "all"', r'set service "ALL"'))
            if anyany:
                f.append(("HIGH", f"policy {pid}: accepts any source, any destination, any service."))
            elif re.search(r'set service "ALL"', body):
                f.append(("MEDIUM", f"policy {pid}: accepts service ALL - restrict to required services."))
            if "set logtraffic disable" in body:
                f.append(("MEDIUM", f"policy {pid}: traffic logging disabled."))
            if "set utm-status enable" not in body and re.search(r"(?i)wan|internet", body):
                f.append(("LOW", f"policy {pid}: internet-facing accept policy without security profiles."))
    glob = re.search(r"(?ms)^config system global\n(.*?)^end", config)
    if glob:
        timeout = re.search(r"set admintimeout (\d+)", glob.group(1))
        if timeout and int(timeout.group(1)) > 30:
            f.append(("LOW", f"admintimeout is {timeout.group(1)} minutes - consider 15 or less."))
        if "set admin-https-redirect disable" in glob.group(1):
            f.append(("LOW", "HTTP-to-HTTPS admin redirect is disabled."))
    for community in re.findall(r'(?m)^\s*set name "(public|private)"', config):
        f.append(("HIGH", f"Default SNMP community '{community}' is configured."))
    if not re.search(r"(?ms)^config system ntp\n.*?set ntpsync enable", config):
        f.append(("LOW", "NTP synchronisation is not enabled."))
    if not re.search(r"(?ms)^config log (syslogd|fortianalyzer)\d? setting\n.*?set status enable", config):
        f.append(("LOW", "No remote logging (syslog / FortiAnalyzer) enabled."))
    return f


def analyze(config: str, vendor: str = "") -> tuple[str, list[tuple[str, str]]]:
    vendor = VENDOR_ALIASES.get(vendor.lower(), [vendor.lower()])[0] if vendor else detect_vendor(config)
    if vendor == "fortinet":
        return vendor, lint_fortinet(config)
    if vendor in {"cisco_ios", "cisco_nxos", "arista_eos"}:
        return vendor, lint_cisco_like(config, vendor)
    return "unknown", []


@tool(
    "analyze_config", "network-core",
    "Run automated hygiene and security checks over a pasted device configuration (Cisco IOS/IOS-XE/NX-OS, "
    "Arista EOS or FortiOS). Returns findings with severities. It is a lint pass, not a substitute for your "
    "own review - use it as a starting point.",
    {
        "config": {"type": "string", "description": "The configuration text"},
        "vendor": {"type": "string", "description": "Optional: cisco_ios, cisco_nxos, arista_eos or fortinet (auto-detected if omitted)"},
    },
    ["config"],
)
async def analyze_config(ctx: ToolContext, args: dict) -> str:
    vendor, findings = analyze(str(args["config"]), str(args.get("vendor") or ""))
    if vendor == "unknown":
        return "Could not identify the vendor from the text. Pass 'vendor' explicitly (cisco_ios, cisco_nxos, arista_eos, fortinet)."
    order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
    findings.sort(key=lambda x: order[x[0]])
    head = f"Detected platform: {VENDOR_LABELS.get(vendor, vendor)}\nFindings: {len(findings)}"
    if not findings:
        return head + "\nNo issues matched the built-in checks."
    return head + "\n" + "\n".join(f"[{sev}] {msg}" for sev, msg in findings)


# ---------------------------------------------------------------------------
# compare_configs
# ---------------------------------------------------------------------------
@tool(
    "compare_configs", "network-core",
    "Produce a unified diff between two configuration texts (e.g. before/after a change, or two devices that "
    "should match).",
    {
        "before": {"type": "string", "description": "Original configuration"},
        "after": {"type": "string", "description": "New configuration"},
    },
    ["before", "after"],
)
async def compare_configs(ctx: ToolContext, args: dict) -> str:
    before = str(args["before"]).splitlines()
    after = str(args["after"]).splitlines()
    diff = list(difflib.unified_diff(before, after, "before", "after", lineterm="", n=2))
    if not diff:
        return "The two configurations are identical."
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return redact_secrets(f"{added} line(s) added, {removed} line(s) removed\n" + "\n".join(diff))


# ---------------------------------------------------------------------------
# lookup_playbook
# ---------------------------------------------------------------------------
def _knowledge_files(root: Path, vendors: list[str]) -> list[Path]:
    files: list[Path] = []
    for vendor in dict.fromkeys(["common", *vendors]):
        folder = root / vendor
        if folder.is_dir():
            files += sorted(folder.glob("*.md"))
    return files


def search_playbooks(root: Path, vendors: list[str], query: str) -> list[tuple[int, Path, str]]:
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    results = []
    for path in _knowledge_files(root, vendors):
        text = path.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        name = path.stem.lower().replace("-", " ")
        headings = " ".join(re.findall(r"(?m)^#+ (.+)$", low))
        score = 0
        for w in words:
            score += 6 * (w in name) + 3 * (w in headings) + min(low.count(w), 4)
        if score:
            results.append((score, path, text))
    results.sort(key=lambda r: -r[0])
    return results


@tool(
    "lookup_playbook", "network-core",
    "Search the troubleshooting and configuration playbook library (vendor-specific runbooks maintained by the "
    "team). Check here before working a problem from scratch - the playbooks encode the expected methodology "
    "and the exact commands for each platform.",
    {
        "query": {"type": "string", "description": "Keywords, e.g. 'ipsec phase 2 mismatch' or 'mlag split brain'"},
        "vendor": {"type": "string", "description": "Optional: cisco, fortinet or arista (defaults to this agent's vendors)"},
    },
    ["query"],
)
async def lookup_playbook(ctx: ToolContext, args: dict) -> str:
    root = ctx.rt.settings.knowledge_dir
    vendor = str(args.get("vendor") or "").strip().lower()
    vendors = [vendor] if vendor else list(ctx.profile.get("knowledge") or [])
    if not vendors:
        vendors = [p.name for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
    results = search_playbooks(root, vendors, str(args["query"]))
    if not results:
        available = ", ".join(f"{p.parent.name}/{p.stem}" for p in _knowledge_files(root, vendors)) or "none"
        return f"No playbook matched. Available playbooks: {available}."
    _, path, text = results[0]
    out = [f"# Playbook: {path.parent.name}/{path.stem}", text[:9000]]
    if len(results) > 1:
        out.append("\nOther matches: " + ", ".join(f"{p.parent.name}/{p.stem}" for _, p, _ in results[1:5]))
    return "\n".join(out)
