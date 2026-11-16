# FortiGate: traffic not passing - policy, NAT and flow debugging

## Follow the packet
Order of operations: ingress interface -> DoS/local-in -> DNAT (VIP) -> routing -> policy lookup ->
security profiles -> SNAT -> egress.

## Quick checks
- Route: `get router info routing-table details <dst-ip>` (and the reverse path for the source - RPF drops
  show as "reverse path check fail").
- Policy that would match:
  `diagnose firewall iprope lookup <src-ip> <src-port> <dst-ip> <dst-port> <proto> <ingress-intf>`
  (GUI: Policy & Objects > Firewall Policy > Policy Match).
- Session: `diagnose sys session filter clear`, `diagnose sys session filter dst <ip>`,
  `diagnose sys session list` - shows policy ID, NAT, state, counters. `diagnose sys session filter clear`
  again afterwards.
- Logs: forward traffic log filtered on source/destination; the "implicit deny" (policy 0) entry means no
  policy matched.

## Flow debug (authoritative - keep the filter tight, then turn it off)
```
diagnose debug reset
diagnose debug flow filter clear
diagnose debug flow filter addr <host-ip>
diagnose debug flow filter port <port>
diagnose debug flow show function-name enable
diagnose debug flow trace start 20
diagnose debug enable
... reproduce ...
diagnose debug disable
diagnose debug reset
```
Reading it:
| Message | Meaning |
|---|---|
| `Allowed by Policy-<id>` | Matched and permitted; look at SNAT and the egress interface next |
| `Denied by forward policy check (policy 0)` | No policy matches - interface pair, addresses, service or schedule |
| `iprope_in_check() check failed, drop` | Traffic to the FortiGate itself not allowed (allowaccess, local-in policy, VIP missing) |
| `reverse path check fail, drop` | No route back to the source via the ingress interface (asymmetric routing / missing route) |
| `no session matched` | Mid-flow packet without a session (asymmetry, session timeout, HA failover) |
| `find a route: ... gw-<ip> via <intf>` | The route chosen - is it the interface you expected (SD-WAN)? |
| `SNAT <a> -> <b>` / `DNAT` | Translation applied |

## Packet capture
`diagnose sniffer packet <intf|any> 'host <ip> and port <port>' 4 20 l`
(verbosity 4 shows the interface; `20` packets then stops). Sees ingress and egress, so you can tell whether
the packet left and whether a reply came back. Offloaded (NP) sessions are not visible - disable offload on
the test policy with `set auto-asic-offload disable` if needed, and re-enable it afterwards.

## Policy hygiene when fixing
- Policies are evaluated top-down per interface pair; a broad policy above shadows a specific one below.
- Add the narrow rule (specific source, destination, service) - never any/any/ALL as a workaround.
- VIP traffic needs a policy whose destination is the VIP object, with the mapped port as the service.
- Keep `set logtraffic all` on policies you are troubleshooting.
