# Troubleshooting method (all vendors)

Use this structure for every fault. It keeps the work evidence-led and makes the final report easy to follow.

## 1. Define the problem
- What exactly fails (application, source, destination, protocol/port)? What still works?
- Since when? What changed (config, software, cabling, provider, firewall policy)?
- Scope: one host, one VLAN/subnet, one site, everything?

## 2. Localise the fault domain
Work from the source toward the destination, one hop at a time:
1. Layer 1/2: link state, errors, duplex/speed, VLAN membership, trunk allowed list, STP state, LACP/MLAG/vPC state.
2. Layer 3: interface addressing, ARP/ND, routing table (is there a route, is it the expected next hop?), return path.
3. Control plane: IGP/BGP adjacency state, timers, authentication, MTU, route policy.
4. Policy: ACLs, firewall policy match, NAT, security profiles.
5. Path/MTU: asymmetric routing, PMTUD, MSS, tunnels (IPsec/VXLAN/GRE overhead).

## 3. Prove it
- Quote the command output that confirms each conclusion. Never infer state you can read.
- Compare with a known-good device or the peer end (both ends of a link or tunnel must agree).
- Check logs around the time the problem started.

## 4. Fix safely
- State the root cause, then the smallest change that corrects it.
- Give the exact commands, the expected impact, verification commands and a rollback.
- Changes to shared control plane (BGP policy, STP, MLAG/vPC, HA) need a change window.

## 5. Report
Answer first, then: evidence, root cause, change made or proposed, verification, residual risk, follow-ups.
