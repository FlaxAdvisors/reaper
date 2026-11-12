"""reaper completion: bash completion generated from the live command table.
Use: source <(reaper completion)"""
from reapercli.lib.components import load_components
from reapercli.lib.sites import load_sites

SUB = {"ha": "status check quiesce arm keepalived-roll reseed --yes --confirm", "svc": "ls logs restart",
       "image": "ls build ship", "net": "fetch drift plan push setup --yes --stage", "push": "--yes --stage",
       "ipxe": "--cached --yes --branch"}


def _fw_families(site_map) -> list:
    """Flashable families (an entry with a flash key) in any site's bmc/host manifest vars."""
    import yaml
    from reapercli.lib.inventory import _Loader
    out = set()
    for s in site_map.values():
        f = s.inventory / "group_vars" / "all" / "vars.yml"
        d = (yaml.load(f.read_text(), Loader=_Loader) or {}) if f.exists() else {}
        for var in ("bmc_fw_platforms", "host_fw_platforms"):
            out |= {k for k, e in (d.get(var) or {}).items()
                    if isinstance(e, dict) and isinstance(e.get("flash"), dict) and e.get("updatable") is not False}
    return sorted(out)


def run(argv: list, ctx) -> int:
    from reapercli.__main__ import COMMANDS, NET_ALIASES
    cmds = " ".join(sorted(set(COMMANDS) | set(NET_ALIASES) | {"shell"}))   # shell: handled by the bash shim
    site_map = load_sites(ctx.root)
    sites = " ".join(["all", *sorted(site_map)])
    comps = load_components(ctx.root, site_map)
    deployable = sorted(({c.name for c in comps.values()} | {c.group for c in comps.values() if c.group}) - {"keepalived"})
    from reapercli.lib.diagscope import _words
    sub = {**SUB, "fw": " ".join(["status", "bmc", "bios", "--yes", *_fw_families(site_map)]),
           "deploy": " ".join(deployable), "diag": " ".join([*sorted(_words(comps)), "--since", "--window"])}
    cases = "\n".join(f'    {k}) COMPREPLY=( $(compgen -W "{v} {sites}" -- "$cur") ); return;;' for k, v in sub.items())
    print(f"""_reaper_complete() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  if [[ $COMP_CWORD -eq 1 ]]; then COMPREPLY=( $(compgen -W "{cmds}" -- "$cur") ); return; fi
  case "${{COMP_WORDS[1]}}" in
{cases}
  esac
  [[ $COMP_CWORD -eq 2 ]] && COMPREPLY=( $(compgen -W "{sites}" -- "$cur") )
}}
complete -F _reaper_complete reaper""")
    return 0
