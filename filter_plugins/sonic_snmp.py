# filter_plugins/sonic_snmp.py
"""SONiC half of roles/apply_switch_snmp (tasks/sonic.yml).

sonic_snmp_plan(running, community, location, contact) compares the running
config_db (`show runningconfiguration all` JSON) with the wanted v2c read-only
SNMP settings and returns the sonic-utilities `config` commands that close the
gap -- like eos_config/ios_config `lines:`, only what differs is written, and
SNMP settings this role does not own (other communities, users) are left alone:

    SNMP_COMMUNITY|<community>  TYPE RO      config snmp community add <c> RO
    SNMP|LOCATION   Location               config snmp location add|modify <l>
    SNMP|CONTACT    {<name>: <email>}      config snmp contact del <old>; add <name> <email>
    FEATURE|snmp    state enabled          config feature state snmp enabled

SONiC's contact is a (name, email) pair; the role's contact var is an email, so
the name is its local part (ops@example.com -> ops).

Returns {"cmds": [...], "changes": [...]}. cmds are `sudo -n ... </dev/null`
(passwordless sudo, empty stdin -- the SONiC CLI rules) and carry the community,
so the task running them is no_log; changes describe the same edits WITHOUT the
community value, safe to print in --check --diff.
"""
import json
import shlex


def _cmd(*args):
    return "sudo -n config " + " ".join(shlex.quote(str(a)) for a in args) + " </dev/null"


def sonic_snmp_plan(running, community, location, contact):
    cfg = json.loads(running) if isinstance(running, str) else (running or {})
    cmds, changes = [], []

    comms = cfg.get("SNMP_COMMUNITY") or {}
    if (comms.get(community) or {}).get("TYPE") != "RO":
        if community in comms:
            cmds.append(_cmd("snmp", "community", "del", community))
        cmds.append(_cmd("snmp", "community", "add", community, "RO"))
        changes.append("community <vault_snmp_community> RO")

    snmp = cfg.get("SNMP") or {}
    cur_loc = (snmp.get("LOCATION") or {}).get("Location")
    if cur_loc != location:
        verb = "add" if cur_loc is None else "modify"
        cmds.append(_cmd("snmp", "location", verb, location))
        changes.append("location " + repr(cur_loc) + " -> " + repr(location))

    name = contact.split("@", 1)[0]
    cur_con = snmp.get("CONTACT") or {}
    if cur_con != {name: contact}:
        for old in sorted(cur_con):
            cmds.append(_cmd("snmp", "contact", "del", old))
        cmds.append(_cmd("snmp", "contact", "add", name, contact))
        changes.append("contact " + repr(cur_con) + " -> " + repr({name: contact}))

    state = ((cfg.get("FEATURE") or {}).get("snmp") or {}).get("state")
    if state is not None and state != "enabled":
        cmds.append(_cmd("feature", "state", "snmp", "enabled"))
        changes.append("feature snmp " + state + " -> enabled")

    return {"cmds": cmds, "changes": changes}


class FilterModule(object):
    def filters(self):
        return {"sonic_snmp_plan": sonic_snmp_plan}
