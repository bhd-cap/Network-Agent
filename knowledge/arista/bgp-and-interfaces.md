# Arista EOS: interfaces and BGP quick reference

## Interfaces
- `show interfaces status` - connected/notconnect/errdisabled, VLAN, speed, type.
- `show interfaces <intf>` - counters, last flap; `show interfaces counters errors`, `show interfaces counters discards`.
- `show interfaces <intf> transceiver` - optical levels (dBm) vs thresholds.
- `show interfaces status errdisabled` - cause (bpduguard, link-flap, mlag reload-delay, ...).
- `show lldp neighbors detail` - confirm cabling against the design.
- Speed/FEC mismatches on 25G/100G links are common: check `show interfaces <intf> phy detail` and set
  `speed forced 25gfull` / `error-correction encoding` to match the peer.
- `show logging last 30 minutes | grep -i <intf>`

## BGP
- `show ip bgp summary [vrf all]` - state and prefixes; `show bgp neighbors <ip>` for the last error.
- `show ip bgp neighbors <ip> advertised-routes` / `received-routes` / `routes`
- `show ip bgp <prefix> detail` - why a path is or is not best.
- States: Idle (no route / shutdown / max-routes), Active or Connect (TCP 179 not completing - wrong address,
  update-source, ACL, VRF), OpenConfirm stuck (AS or password mismatch), Established with 0 prefixes (policy
  or missing `activate` in the address family).
- EOS defaults that surprise people: `no bgp default ipv4-unicast` is common in designs, so each neighbor must
  be activated per address family; eBGP needs explicit `maximum-routes`; multi-agent routing
  (`service routing protocols model multi-agent`) is required for EVPN and needs a reload when changed.
- After a policy change: `clear ip bgp <ip> soft in|out` rather than a hard reset.

## Leaf/spine eBGP underlay checklist
- Unique ASN per leaf (or per MLAG pair), spines share one ASN.
- `maximum-paths 4 ecmp 4` for ECMP toward the spines.
- Advertise loopbacks (`network` or `redistribute connected` with a prefix-list).
- BFD on fabric links: `neighbor <ip> bfd`.
- Point-to-point links as /31 with `no switchport`, MTU 9214.
