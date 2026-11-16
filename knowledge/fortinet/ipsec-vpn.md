# FortiGate: IPsec VPN troubleshooting

## State
- `get vpn ipsec tunnel summary` - tunnel up/down, selectors up, packet counters.
- `diagnose vpn ike gateway list name <phase1>` - IKE SA state, version, peer, NAT-T, DPD.
- `diagnose vpn tunnel list name <phase1>` - phase 2 SAs, selectors (`src:`/`dst:`), SPI, enc/dec counters and errors.
- `get router info routing-table all` - is the remote subnet routed to the tunnel interface?
- `show vpn ipsec phase1-interface <name>` / `show vpn ipsec phase2-interface <name>`

## IKE debug (stop it afterwards)
```
diagnose vpn ike log-filter dst-addr4 <peer-public-ip>     (7.4+: diagnose vpn ike log filter rem-addr4 <ip>)
diagnose debug application ike -1
diagnose debug enable
... reproduce ...
diagnose debug disable
diagnose debug reset
```

## Phase 1 does not come up
| Debug message | Cause |
|---|---|
| no response / retransmit | Peer unreachable, UDP 500/4500 blocked, wrong remote-gw, peer not initiating |
| `no SA proposal chosen` | Encryption/hash/DH group/IKE version mismatch |
| `PSK auth failed` / `probable pre-shared secret mismatch` | Pre-shared key mismatch |
| peer ID / `gw validation failed` | Local-ID/peer-ID mismatch, aggressive vs main mode, wrong peer type |
| negotiation succeeds then drops | DPD failing, NAT-T mismatch, overlapping tunnels to the same gateway |

## Phase 1 up, phase 2 down
- `no matching IPsec selector` / `failed to match peer selectors`: local/remote subnets must mirror exactly on
  both ends. With a third-party peer, define explicit selectors rather than 0.0.0.0/0, one phase 2 per pair.
- PFS/DH group or proposal mismatch in phase 2.

## Tunnel up, no traffic
1. Route: static route to the remote subnet via the tunnel interface (route-based).
2. Policy: firewall policies in both directions between the LAN interface and the tunnel interface.
3. Selectors: traffic source/destination must fall inside the phase 2 selectors.
4. NAT: no source NAT on the VPN policy unless intended.
5. Counters in `diagnose vpn tunnel list`: enc increases but dec does not -> the far end is not returning
   traffic (its route/policy/selector) or ESP is blocked in the path.
6. Follow one packet with the flow debug (see the policy/traffic-flow playbook).
7. MTU/MSS: large packets failing - set `set tcp-mss-sender` / `set tcp-mss-receiver` on the policy.

## Interop notes (Cisco IOS-XE / ASA peers)
- Match IKE version, lifetimes (seconds vs kilobytes - disable volume-based rekey on the Cisco side or
  match it), DH group and PFS explicitly.
- Cisco crypto ACLs must be exact mirrors of the FortiGate phase 2 selectors.
- VTI on Cisco (`tunnel mode ipsec ipv4`) pairs cleanly with a route-based FortiGate tunnel using
  0.0.0.0/0 selectors.
