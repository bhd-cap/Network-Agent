# FortiGate: routing and SD-WAN path selection

## Routing
- `get router info routing-table all` - active routes; `get router info routing-table database` - all
  candidates including inactive ones (lower distance wins; equal distance uses priority).
- `get router info routing-table details <ip>` - best match for one destination.
- `diagnose firewall proute list` - policy routes and SD-WAN rules (these are evaluated before the FIB).
- `get router info bgp summary`, `get router info bgp neighbors <ip> routes|advertised-routes`,
  `get router info ospf neighbor`.
- A static route is only active while its interface is up and, if configured, its link-monitor is alive.

## Lookup order
1. Policy routes (`config router policy`)
2. SD-WAN rules (only if the destination has a route via an SD-WAN member)
3. FIB (longest match, then distance, then priority)

## SD-WAN
- `diagnose sys sdwan health-check` - per-member latency, jitter, loss, SLA pass/fail.
- `diagnose sys sdwan member` - member state, gateway, weight.
- `diagnose sys sdwan service` (7.x; older: `diagnose sys virtual-wan-link service`) - which member each rule
  currently selects and why.
- A rule only steers traffic that a firewall policy allows and for which the selected member has a valid
  route to the destination; otherwise it falls through to the next rule and finally the implicit rule
  (ECMP by the configured load-balance mode).

## Common faults
| Symptom | Cause |
|---|---|
| Traffic ignores the SD-WAN rule | No route to the destination via that member; rule source/destination/service does not match; a policy route above it |
| Member flapping in and out | Health-check target unreliable - use two servers, relax thresholds, check `failtime`/`recoverytime` |
| Asymmetric / "reverse path check fail" | Return traffic arrives on a member with no route to the source - add the route or adjust distances so both are in the FIB |
| Existing sessions stay on the old link | Sessions keep their path until they expire; `set snat-route-change enable` or clear the sessions in a window |
