# Cisco: OSPF adjacency troubleshooting (IOS / IOS-XE / NX-OS)

## Read the state first
- `show ip ospf neighbor` (NX-OS: `show ip ospf neighbors`) - which state is it stuck in?
- `show ip ospf interface <intf>` - area, network type, timers, authentication, passive, cost, DR/BDR.
- `show ip ospf` - router ID, areas, SPF runs.
- `show logging | include OSPF` - adjacency change reasons.

## By stuck state
| State | Usual cause | Check |
|---|---|---|
| No neighbor at all | OSPF not enabled on the interface, passive-interface, L2 problem, ACL blocking protocol 89 / 224.0.0.5 | `show ip ospf interface brief`, ping the peer, interface ACLs |
| INIT | One-way hellos: peer does not see us (ACL, unidirectional link, auth mismatch on one side) | Both ends: `show ip ospf interface`, `debug ip ospf adj` in a window |
| 2-WAY | Normal between DROTHERs on broadcast segments | Not a fault unless neither is DR/BDR |
| EXSTART / EXCHANGE | **MTU mismatch** (most common), duplicate router ID | `show ip interface <intf>` MTU both ends; `show ip ospf` router IDs |
| LOADING | Corrupt/filtered LSA, MTU on a path that fragments | `show ip ospf request-list`, `show ip ospf bad` |

## Parameters that must match
Area ID, area type (stub/NSSA flags), hello and dead timers, authentication type and key, subnet and mask
(except point-to-point), and network type compatibility (broadcast vs point-to-point will form but not
install routes correctly).

## Fixes
- MTU mismatch: correct the MTU. `ip ospf mtu-ignore` hides the symptom - use only as a stopgap.
- Duplicate router ID: set `router-id` explicitly, then `clear ip ospf process` (disruptive).
- Timers: `ip ospf hello-interval` / `dead-interval` on the interface, both ends.
- NX-OS: `feature ospf` must be enabled and the interface needs `ip router ospf <tag> area <id>`.

## Route present in LSDB but not in RIB
Check `show ip ospf database`, forwarding address reachability for type-5/7, `distribute-list in`,
and that the network type agrees on both ends.
