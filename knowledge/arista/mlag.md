# Arista EOS: MLAG troubleshooting

## Commands
- `show mlag` - state (active/inactive/disabled), negotiation status, peer-link and local-interface status,
  reload-delay, count of active/inactive MLAG ports.
- `show mlag detail` - primary/secondary role, failover, heartbeat counters.
- `show mlag config-sanity` - parameters that differ between the peers (the first thing to check).
- `show mlag interfaces [detail]` - per-MLAG state: active-full, active-partial, inactive, disabled.
- `show port-channel dense`, `show lacp neighbor`, `show lacp interface`
- `show spanning-tree`, `show vlan 4094` (the peering VLAN)

## Reference configuration
```
vlan 4094
   trunk group MLAGPEER
no spanning-tree vlan-id 4094
interface Port-Channel10
   switchport mode trunk
   switchport trunk group MLAGPEER
interface Vlan4094
   ip address 169.254.0.1/30          (peer: 169.254.0.2/30)
   no autostate
mlag configuration
   domain-id MLAG1
   local-interface Vlan4094
   peer-address 169.254.0.2
   peer-link Port-Channel10
   reload-delay mlag 300
   reload-delay non-mlag 330
interface Port-Channel20
   mlag 20
```

## Faults
| Symptom | Cause | Action |
|---|---|---|
| State `inactive`, negotiation `connecting` | Peer address unreachable: VLAN 4094 SVI down, peer-link down, trunk group missing | `ping 169.254.0.x`, `show interfaces Port-Channel10`, check `trunk group` on VLAN and peer-link |
| `domain-id mismatch` / sanity errors | Different domain-id, or differing global/interface parameters | `show mlag config-sanity`; make both peers identical |
| MLAG port `active-partial` | Port-channel up on one peer only | Member links, LACP on the other peer, cabling |
| MLAG port `inactive` | Port-channel down on both, or `mlag <id>` missing on one side | `show mlag interfaces detail` |
| Ports err-disabled after reload | Normal: `reload-delay` holds ports down until the peer state syncs | Wait for the timer; do not shorten below convergence time |
| Split brain (both active, peer-link down) | Peer-link failure without a working heartbeat | Restore the peer-link; consider `peer-address heartbeat` on the management network and dual-primary detection |
| Duplicate MAC / flapping to hosts | Different MLAG IDs or LACP system-id for the same downstream LAG | Same `mlag <id>` on both peers for the same device |

## Rules
- STP: both peers act as one bridge; configure the same priority, and keep VLAN 4094 out of STP.
- With VXLAN: use a shared loopback (same VTEP IP) or the virtual VTEP address on both peers, and
  `ip virtual-router mac-address` identical on both.
- Every change to an MLAG port-channel or its VLANs must be mirrored on the peer; re-run
  `show mlag config-sanity` after.
