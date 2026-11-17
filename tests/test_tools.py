"""Network tools, the read-only command guard and secret redaction."""
import pytest

from netagent.security import redact_secrets
from netagent.tools import ToolContext, run_tool, tools_for
from netagent.tools.devices import check_read_only
from netagent.tools.network import analyze, detect_vendor


def _ctx(rt, agent_name="Cisco Expert"):
    agent = rt.db.one("SELECT * FROM agents WHERE name = ?", [agent_name])
    profile = rt.db.get("profiles", agent["profile_id"])
    task = rt.orchestrator.create_task("t", "t", agent_id=agent["id"], inline=True)
    ctx = ToolContext(rt=rt, agent=agent, profile=profile, task=task)
    return ctx, {t.spec.name for t in tools_for(profile["skills"])}


# ------------------------------------------------------ read-only guard
@pytest.mark.parametrize("platform,command", [
    ("cisco_xe", "show ip interface brief"),
    ("cisco_xe", "show running-config | section router bgp"),
    ("cisco_xe", "show ip interface brief | include up|down"),
    ("cisco_xe", "show interfaces | include ^Gi.*up$"),
    ("cisco_nxos", "show vpc consistency-parameters global"),
    ("cisco_asa", "packet-tracer input inside tcp 10.1.1.1 1234 8.8.8.8 443"),
    ("arista_eos", "show mlag | json"),
    ("arista_eos", "ping 10.0.0.1 source Loopback0 repeat 5"),
    ("fortinet", "get system status"),
    ("fortinet", "show full-configuration"),
    ("fortinet", "diagnose vpn tunnel list name VPN-DR"),
    ("fortinet", "execute ping 8.8.8.8"),
    ("fortinet", "get router info routing-table all | grep 10.20"),
])
def test_read_only_commands_are_allowed(platform, command):
    assert check_read_only(platform, command) is None


@pytest.mark.parametrize("platform,command", [
    ("cisco_xe", "configure terminal"),
    ("cisco_xe", "reload"),
    ("cisco_xe", "write erase"),
    ("cisco_xe", "copy running-config tftp://10.0.0.1/x"),
    ("cisco_xe", "clear ip bgp *"),
    ("cisco_xe", "show version\nconfigure terminal"),
    ("cisco_nxos", "show version ; configure terminal ; interface e1/1 ; shutdown"),
    ("cisco_xe", "show running-config | redirect tftp://10.0.0.1/cfg"),
    ("cisco_xe", "show running-config | include a | tee flash:x"),
    ("arista_eos", "show version | include x|bash"),
    ("arista_eos", "bash sudo reboot"),
    ("arista_eos", "show running-config > flash:copy"),
    ("cisco_xe", "ping 10.0.0.1 repeat 100000"),
    ("fortinet", "execute reboot"),
    ("fortinet", "execute factoryreset"),
    ("fortinet", "config system interface"),
    ("fortinet", "diagnose sys kill 9 123"),
    ("fortinet", "diagnose debug enable"),
    ("fortinet", "diagnose test application ipsmonitor 99"),
    ("cisco_xe", ""),
    ("junos", "show version"),
])
def test_dangerous_commands_are_refused(platform, command):
    assert check_read_only(platform, command) is not None


async def test_run_show_command_on_simulated_device(rt):
    ctx, allowed = _ctx(rt)
    output, is_error = await run_tool(ctx, "run_show_command", {"device": "sw-core-01", "command": "show ip bgp summary"}, allowed)
    assert not is_error and "10.255.0.5" in output and "Active" in output

    output, is_error = await run_tool(ctx, "run_show_command", {"device": "sw-core-01", "command": "conf t"}, allowed)
    assert is_error and "Refused" in output

    output, is_error = await run_tool(ctx, "run_show_command", {"device": "nope", "command": "show version"}, allowed)
    assert is_error and "sw-core-01" in output  # lists known devices


async def test_device_output_is_redacted(rt):
    ctx, allowed = _ctx(rt)
    output, _ = await run_tool(ctx, "run_show_command", {"device": "sw-core-01", "command": "show running-config"}, allowed)
    assert "$9$abcdefghijk" not in output and "s3cr3tRO" not in output
    assert output.count("<redacted>") >= 2
    output, _ = await run_tool(ctx, "run_show_command", {"device": "fw-edge-01", "command": "show full-configuration"}, allowed)
    assert "abcd1234" not in output and "set psksecret <redacted>" in output


def test_redaction_patterns():
    text = "\n".join([
        "enable secret 5 $1$mERr$hx5rVt7rPNoS4wqbXKX7m0",
        "username admin privilege 15 secret 9 $9$abc",
        "username bob password 7 0822455D0A16",
        "snmp-server community MyC0mm RO",
        " neighbor 10.0.0.1 password 7 094F471A1A0A",
        "tacacs-server host 10.1.1.1 key 7 13061E010803",
        "    set password ENC SH2abcdef==",
        "    set psksecret ENC abcd",
        "interface Vlan10",
        " ip address 10.10.10.1 255.255.255.0",
    ])
    out = redact_secrets(text)
    for secret in ("$1$mERr", "$9$abc", "0822455D0A16", "MyC0mm", "094F471A1A0A", "13061E010803", "SH2abcdef", "ENC abcd"):
        assert secret not in out
    assert "ip address 10.10.10.1 255.255.255.0" in out and "interface Vlan10" in out


async def test_propose_config_change_creates_pending_request(rt):
    ctx, allowed = _ctx(rt)
    output, is_error = await run_tool(ctx, "propose_config_change", {
        "device": "sw-core-01", "summary": "Add VLAN 300", "commands": ["vlan 300", " name LAB"],
        "rollback_commands": ["no vlan 300"], "verify_commands": ["show vlan id 300"], "risk": "low",
    }, allowed)
    assert not is_error and "CR-1" in output
    change = rt.db.get("change_requests", 1)
    assert change["status"] == "pending" and change["commands"] == "vlan 300\n name LAB"
    assert change["task_id"] == ctx.task["id"]


# ------------------------------------------------------------ net tools
async def test_subnet_calc(rt):
    ctx, allowed = _ctx(rt)
    out, err = await run_tool(ctx, "subnet_calc", {"cidr": "10.20.30.77/23", "new_prefix": 25, "contains": "10.20.31.9"}, allowed)
    assert not err
    for expected in ("Network: 10.20.30.0/23", "Netmask: 255.255.254.0", "Wildcard mask: 0.0.1.255",
                     "Usable hosts: 510 (10.20.30.1 - 10.20.31.254)", "10.20.31.9 in 10.20.30.0/23: True",
                     "4 subnets", "10.20.31.128/25"):
        assert expected in out
    out, err = await run_tool(ctx, "subnet_calc", {"cidr": "10.0.0.0/31"}, allowed)
    assert "point-to-point" in out
    out, err = await run_tool(ctx, "subnet_calc", {"cidr": "not-an-ip"}, allowed)
    assert err


async def test_vendor_command_reference(rt):
    ctx, allowed = _ctx(rt)
    out, _ = await run_tool(ctx, "vendor_command_reference", {"topic": "bgp summary"}, allowed)
    assert "get router info bgp summary" in out and "show ip bgp summary" in out
    out, _ = await run_tool(ctx, "vendor_command_reference", {"topic": "routing table", "vendor": "fortinet"}, allowed)
    assert "get router info routing-table all" in out and "Cisco" not in out


CISCO_CONFIG = """version 17.9
hostname sw1
enable password cisco123
no service password-encryption
username admin password 0 letmein
snmp-server community public RO
ip http server
interface GigabitEthernet1/0/1
 switchport mode trunk
line vty 0 4
 transport input telnet
"""
FORTI_CONFIG = """config system global
    set admintimeout 480
end
config system interface
    edit "wan1"
        set allowaccess ping https ssh http
        set role wan
    next
end
config system admin
    edit "admin"
        set accprofile "super_admin"
    next
end
config firewall policy
    edit 7
        set srcintf "lan"
        set dstintf "wan1"
        set action accept
        set srcaddr "all"
        set dstaddr "all"
        set service "ALL"
        set logtraffic disable
    next
end
"""


def test_config_analysis_cisco():
    vendor, findings = analyze(CISCO_CONFIG)
    text = "\n".join(f"{s} {m}" for s, m in findings)
    assert vendor == "cisco_ios"
    for expected in ("enable password", "Telnet is permitted", "Default SNMP community 'public'", "ip http server",
                     "trunk allows all VLANs", "No NTP server"):
        assert expected in text


def test_config_analysis_fortinet():
    assert detect_vendor(FORTI_CONFIG) == "fortinet"
    _, findings = analyze(FORTI_CONFIG)
    text = "\n".join(f"{s} {m}" for s, m in findings)
    for expected in ("policy 7: accepts any source, any destination, any service", "traffic logging disabled",
                     "exposed on a WAN-facing interface", "no trusted hosts", "admintimeout is 480"):
        assert expected in text


async def test_playbook_lookup_respects_profile_knowledge(rt):
    ctx, allowed = _ctx(rt, "Fortinet Expert")
    out, err = await run_tool(ctx, "lookup_playbook", {"query": "ipsec phase 2 selector mismatch"}, allowed)
    assert not err and "fortinet/ipsec-vpn" in out and "no matching IPsec selector" in out
    ctx, allowed = _ctx(rt, "Arista Expert")
    out, _ = await run_tool(ctx, "lookup_playbook", {"query": "mlag split brain peer-link"}, allowed)
    assert "arista/mlag" in out
    out, _ = await run_tool(ctx, "lookup_playbook", {"query": "zzzzqqq"}, allowed)
    assert "No playbook matched" in out


async def test_profile_skills_limit_the_tools(rt):
    general = rt.db.get("profiles", "general-assistant")
    names = {t.spec.name for t in tools_for(general["skills"])}
    assert "consult_agent" in names and "add_task_note" in names
    assert "run_show_command" not in names and "propose_config_change" not in names
    ctx, _ = _ctx(rt)
    out, err = await run_tool(ctx, "run_show_command", {"device": "sw-core-01", "command": "show version"}, names)
    assert err and "Unknown tool" in out
