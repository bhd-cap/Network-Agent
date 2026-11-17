# Cisco NX-OS: vPC troubleshooting

## Commands
- `show vpc` - peer status, keepalive status, consistency status, per-vPC state.
- `show vpc consistency-parameters global` and `show vpc consistency-parameters interface port-channel <n>`
- `show vpc peer-keepalive`
- `show vpc role` - primary/secondary, sticky bit.
- `show port-channel summary`, `show lacp neighbor`
- `show vpc orphan-ports`

## Common failures
| Symptom | Cause | Action |
|---|---|---|
| Peer-link down, secondary suspends its vPCs | Expected protection behaviour when the peer-link fails and the keepalive is alive | Restore the peer-link; check `show vpc role` before bringing ports back |
| Both think they are primary (dual-active) | Peer-link and keepalive both lost | Restore keepalive first (dedicated mgmt/VRF path), then the peer-link |
| vPC down with "Type-1 inconsistency" | Mismatch in STP mode/region, VLAN-to-instance mapping, port-channel mode, MTU, speed | `show vpc consistency-parameters ...` shows the mismatched parameter; make both peers identical |
| Type-2 inconsistency | VLAN/SVI mismatch between peers | Traffic may blackhole for that VLAN; align VLAN and SVI config |
| Traffic blackholed after a reload | SVIs up before routing converged | `delay restore <seconds>` under `vpc domain`, `peer-gateway`, `layer3 peer-router` where routing over vPC is needed |
| Orphan port loses connectivity on peer-link failure | Single-attached host on the secondary | `vpc orphan-port suspend` or dual-home the device |

## Design rules worth checking
- Keepalive must not ride the peer-link. Use mgmt0 or a dedicated L3 link/VRF.
- Peer-link is a port-channel of at least two 10G+ members on different line cards, trunking all vPC VLANs.
- Same vPC domain ID on both peers, unique in the L2 domain (it derives the system MAC).
- `peer-switch` so both peers present one STP root; `auto-recovery` for dual-failure cases.
- Any change to a vPC member port-channel must be made on both peers.
