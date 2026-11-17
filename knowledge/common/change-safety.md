# Change safety checklist

Every proposed configuration change should include:

1. **Scope** - which device(s), which lines, what traffic is affected and for how long.
2. **Pre-checks** - commands that capture the current state (neighbors, routes, interface state, counters).
3. **The change** - complete, ordered configuration lines, exactly as they will be entered.
4. **Verification** - commands and the expected output that prove success.
5. **Rollback** - the lines that undo the change, and when to trigger rollback.
6. **Risk** - low (single access port, description, additive ACL entry), medium (routing policy on one peer,
   new VLAN on trunks), high (anything on core/HA/MLAG/vPC, default routes, redistribution, firewall any/any,
   management access, AAA).

## Safety nets by platform
- **Cisco IOS/IOS-XE**: `reload in 10` before a risky remote change, `reload cancel` after; or
  `configure terminal revert timer 5` with archive configured, then `configure confirm`. The change is not
  persistent until `write memory`.
- **Cisco NX-OS**: `checkpoint <name>` before the change, `rollback running-config checkpoint <name>` to undo it.
- **Cisco IOS-XR**: nothing applies until `commit`; use `commit confirmed 300` and then `commit`.
- **Arista EOS**: `configure session <name>`, review with `show session-config diffs`, then
  `commit timer 00:05:00` and `configure session <name> commit` to confirm; `abort` discards.
- **FortiOS**: each `end` applies and saves immediately. For risky remote changes set
  `config system global` / `set cfg-save revert` / `set cfg-revert-timeout 300`, then `execute cfg save` to keep.

## Never
- Remove a trunk allowed VLAN list with `switchport trunk allowed vlan <x>` when you meant `add <x>`.
- Change the management path you are connected through without an out-of-band way back.
- Paste any/any firewall rules or disable logging "temporarily".
