# Cisco: BGP neighbor troubleshooting (IOS / IOS-XE / NX-OS)

## Commands
- `show ip bgp summary` (NX-OS: `show bgp ipv4 unicast summary`, add `vrf all` as needed)
- `show ip bgp neighbors <ip>` - state, last reset reason, timers, capabilities, prefixes sent/received
- `show ip route <peer-ip>` and `ping <peer-ip> source <update-source>`
- `show tcp brief | include 179` (IOS) - is the TCP session there?
- `show logging | include BGP`

## By state
| State | Meaning | Usual cause |
|---|---|---|
| Idle | Not trying | No route to the peer, neighbor shut down, max-prefix exceeded (`Idle (PfxCt)`), waiting after a reset |
| Active | TCP not establishing | Peer not configured for us, wrong neighbor address/update-source, ACL/firewall blocking TCP 179, eBGP multihop/TTL, wrong VRF |
| OpenSent / OpenConfirm | TCP up, OPEN rejected | AS number mismatch, authentication (MD5) mismatch, capability/address-family mismatch |
| Established, 0 prefixes | Session fine, policy problem | No `network`/redistribution, missing `activate` in the address family, route-map/prefix-list denies, next-hop unreachable |

## Checks that find most faults
1. Reachability from the correct source: loopback peering needs `neighbor x update-source Loopback0`, and eBGP
   between loopbacks needs `ebgp-multihop 2` (or `disable-connected-check`).
2. `show ip bgp neighbors <ip> | include reset|Notification` - the last reset reason names the cause
   (hold time expired, peer AS mismatch, admin shutdown, max prefix).
3. MD5: `%TCP-6-BADAUTH` in the log means a key mismatch.
4. Hold-timer expiry on an otherwise healthy link: MTU/PMTUD on the path (large UPDATEs dropped), CPU, or CoPP.
5. iBGP learned routes not used: next-hop unreachable - add `next-hop-self` on the border router or carry the
   next-hop in the IGP.
6. Routes received but not installed: `show ip bgp <prefix>` shows the reason (not best, RIB-failure,
   next hop inaccessible, AS loop).

## Policy
- `show ip bgp neighbors <ip> advertised-routes` / `routes` (needs `soft-reconfiguration inbound` for
  `received-routes`).
- After a policy change use `clear ip bgp <ip> soft` (non-disruptive) rather than a hard clear.
- NX-OS: `feature bgp`; neighbors need an address family: `neighbor x` / `address-family ipv4 unicast`.
