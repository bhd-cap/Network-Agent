# FortiGate: HA (FGCP) troubleshooting

## State
- `get system ha status` - mode, cluster members, primary selection reason, sync status.
- `diagnose sys ha checksum cluster` - per-VDOM config checksums; all members must match.
- `diagnose sys ha history read` - failover history and reasons.
- `get system ha` / `show system ha` - group-id, priority, override, monitored interfaces, heartbeat devices.
- `execute ha manage <index> <admin>` - log in to the other member from the primary.

## Cluster will not form
- Same model, same firmware build, same hardware/disk layout, same licence level (FortiGuard/VDOM).
- Same `group-name`, `group-id`, `password`, `mode`.
- Heartbeat interfaces connected and listed in `hbdev` on both; avoid switches that filter ethertype
  0x8890/0x8891/0x8893 or enable jumbo frames if required.
- Two heartbeat links on separate interfaces to avoid split-brain.

## Out of sync
1. `diagnose sys ha checksum cluster` to find which table differs (`diagnose sys ha checksum show <vdom>`).
2. `execute ha synchronize start` on the secondary.
3. Persistent mismatch: `diagnose sys ha checksum recalculate` on both; as a last resort re-sync by
   restoring the primary's config to the secondary in a window.

## Unexpected failover / wrong primary
Primary selection order (override disabled): monitored interfaces up > uptime (age) > priority > serial.
With `set override enable`: monitored interfaces > priority > age > serial.
- Check `diagnose sys ha history read` for link-monitor or ping-server (`pingserver-monitor-interface`) events.
- A device rebooted most recently has lower age and loses - expected.
- To force a failover in a window: `execute ha failover set 1` (clear with `execute ha failover unset 1`),
  or `diagnose sys ha reset-uptime` on the primary.

## After failover, sessions drop
- `set session-pickup enable` (and `session-pickup-connectionless` for UDP/ICMP) to sync sessions.
- Upstream/downstream switches must learn the virtual MAC on the new port (gratuitous ARPs:
  `set arps`, `set arps-interval`); check port security and static MAC entries on the switches.
