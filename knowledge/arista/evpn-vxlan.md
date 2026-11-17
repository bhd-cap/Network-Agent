# Arista EOS: BGP EVPN / VXLAN troubleshooting

Work bottom-up: underlay -> EVPN control plane -> VXLAN data plane.

## 1. Underlay
- `show ip bgp summary` (or `show ip ospf neighbor`) - all spine sessions Established?
- `show ip route <remote-loopback>` and `ping <remote-vtep> source Loopback1` - VTEP-to-VTEP reachability.
- MTU: fabric links need at least 50 bytes more than the largest tenant frame (use 9214).
  `ping <vtep> source Loopback1 size 8972 df-bit`.

## 2. EVPN control plane
- `show bgp evpn summary` - EVPN address family sessions (usually loopback-to-loopback to the spines).
- `show bgp evpn route-type imet` - type-3 routes: one per VTEP per VNI (builds the flood list).
- `show bgp evpn route-type mac-ip [vni <vni>]` - type-2 routes: host MACs/IPs.
- `show bgp evpn route-type ip-prefix ipv4` - type-5 routes (L3 VNI / VRF prefixes).
- `show bgp evpn instance` - per MAC-VRF RD and route-targets.
Requirements: `neighbor ... send-community extended`, eBGP overlay sessions with `ebgp-multihop 3` and
`update-source Loopback0`, spines with `next-hop-unchanged` for the EVPN family, and matching route-targets
(import of one side = export of the other).

## 3. VXLAN data plane
- `show interfaces Vxlan1` - source interface, UDP port 4789, VLAN-to-VNI and VRF-to-VNI mappings.
- `show vxlan vtep` - remote VTEPs discovered.
- `show vxlan flood vtep` - flood list per VLAN (from type-3 routes or static).
- `show vxlan address-table` - remote MACs and the VTEP they sit behind.
- `show mac address-table vlan <id>`, `show arp vrf <vrf>`, `show ip route vrf <vrf>`

## Faults
| Symptom | Cause |
|---|---|
| Hosts in the same VLAN on different leafs cannot reach each other | VNI mismatch for the VLAN, missing type-3 routes (flood list empty), RT mismatch, VLAN not allowed on the host port |
| Remote VTEP missing in `show vxlan vtep` | Underlay loopback not advertised, EVPN session down, `send-community extended` missing |
| MAC learned locally but not seen remotely | `redistribute learned` missing under the VLAN/VLAN-aware bundle in `router bgp` |
| Inter-VLAN routing fails across leafs (symmetric IRB) | L3 VNI not mapped (`vxlan vrf <vrf> vni <n>`), VRF RT mismatch, `redistribute connected` missing in the VRF, missing `ip virtual-router address` / anycast gateway on one leaf |
| Works for small packets only | Underlay MTU too small for VXLAN overhead |
| Flapping MACs between VTEPs | MLAG pair not sharing the VTEP IP / virtual MAC, or a real L2 loop |

## Minimal leaf pieces to compare between a working and a failing leaf
```
interface Vxlan1
   vxlan source-interface Loopback1
   vxlan udp-port 4789
   vxlan vlan 100 vni 10100
   vxlan vrf TENANT vni 50001
router bgp 65101
   vlan 100
      rd auto
      route-target both 100:10100
      redistribute learned
   vrf TENANT
      rd 10.0.1.1:50001
      route-target import evpn 50001:50001
      route-target export evpn 50001:50001
      redistribute connected
```
