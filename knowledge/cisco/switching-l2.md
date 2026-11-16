# Cisco: Layer 2 troubleshooting - interfaces, err-disable, STP, EtherChannel

## Interface down or flapping
- `show interfaces <intf>` - line/protocol state, input errors, CRC, runts, giants, late collisions, last flap.
- `show interfaces status` / `show interfaces status err-disabled`
- `show interfaces <intf> transceiver detail` - optical power in range?
- `show logging | include <intf>`
- CRC/input errors that grow: cabling, optics, or duplex mismatch (late collisions on the half-duplex side).
  Hard-set speed/duplex on both ends or auto on both - never mixed.

## err-disabled ports
`show errdisable recovery` and `show interfaces status err-disabled` give the reason:
bpduguard, psecure-violation, udld, link-flap, loopback, dhcp-rate-limit, channel-misconfig.
Fix the cause first, then `shutdown` / `no shutdown`. Automatic recovery:
`errdisable recovery cause <cause>` and `errdisable recovery interval 300`.

## VLAN reachability
- `show vlan brief` - does the VLAN exist and is the port in it?
- `show interfaces trunk` - is the VLAN in "allowed and active" and "forwarding and not pruned" on every trunk in the path?
- Native VLAN mismatch shows in CDP logs (`%CDP-4-NATIVE_VLAN_MISMATCH`).
- Adding a VLAN to a trunk: `switchport trunk allowed vlan add <id>` - without `add` the list is replaced.

## Spanning tree
- `show spanning-tree vlan <id>` - who is root, which ports block, port roles/costs.
- `show spanning-tree detail | include ieee|occurr|from` - topology change count and where it came from.
- `show spanning-tree inconsistentports` - root/loop guard, PVID/type inconsistencies.
- Unexpected root: set priority deliberately on the intended root (`spanning-tree vlan <id> priority 4096`) and
  use root guard toward the access layer.
- Frequent TCNs: an edge port without PortFast. Use `spanning-tree portfast` + `bpduguard` on host ports.

## EtherChannel
- `show etherchannel summary` - flags: P bundled, s suspended, I standalone, D down.
- `show lacp neighbor` / `show lacp counters` - are LACPDUs being exchanged?
- Suspended members: mismatch in speed, duplex, switchport mode, allowed VLANs or native VLAN between
  members. Configure on the port-channel interface, not the members.
- Mode pairs that work: active/active, active/passive (LACP); `on` only with `on`.
