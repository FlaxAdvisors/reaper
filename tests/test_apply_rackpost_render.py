# tests/test_apply_rackpost_render.py
"""Render tests for the rackpost deploy role + nginx routes (plain jinja2)."""
import os
import pathlib
import re
import jinja2
import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = REPO
ROLES = f"{CODE}/roles"

CTX = dict(inventory_hostname="bang-gouda", bang_lab_domain="lab.test", bang_domain="bangs.example.net",
           apply_flax_stack_https_port=443)
LOCS = ('location ~ "^/[a-z]{2}[0-9]{3}[a-z]?/" {', "location /rackpost/ {")


def _render(role, name, ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(f"{ROLES}/{role}/templates"),
        trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    return env.get_template(name).render(**ctx)


def _servers(conf):
    """Split nginx conf into {server_name_first_token: block_text}."""
    out = {}
    for blk in re.split(r"(?m)^server \{", conf)[1:]:
        m = re.search(r"server_name\s+(\S+)", blk)
        out[m.group(1)] = blk
    return out


def _check_post_only(conf, post_prefix):
    servers = _servers(conf)
    post = [k for k in servers if k.startswith(post_prefix)]
    assert len(post) == 1
    for k, blk in servers.items():
        for loc in LOCS:
            assert (loc in blk) == (k in post), (k, loc)
    blk = servers[post[0]]
    for loc in LOCS:
        body = blk[blk.index(loc):].split("\n    }", 1)[0]
        assert "proxy_pass         http://127.0.0.1:8452;" in body


def test_lab_ui_nginx_routes_only_in_post_block():
    conf = _render("apply_bang_services", "nginx-lab-ui.conf.j2", CTX)
    _check_post_only(conf, "post.")


def test_tls_nginx_routes_only_in_post_block():
    conf = _render("apply_flax_stack", "nginx.conf.j2", CTX)
    _check_post_only(conf, "post.")


def test_unit_and_wrapper():
    defaults = yaml.safe_load(open(f"{ROLES}/apply_rackpost/defaults/main.yml"))
    unit = _render("apply_rackpost", "flax-rackpost.service.j2", defaults)
    ex = [l for l in unit.splitlines() if l.startswith("ExecStart=")][0]
    assert "--port 8452" in ex and "--root /export/nodes" in ex
    assert "--bind 127.0.0.1" in ex and ex.startswith("ExecStart=/opt/flax/bin/rackpost serve")
    assert "Restart=on-failure" in unit
    ctx = dict(defaults, apply_rackpost_vendor_dir=jinja2.Template(
        defaults["apply_rackpost_vendor_dir"]).render(**defaults))
    wrap = _render("apply_rackpost", "rackpost.sh.j2", ctx)
    assert ("PYTHONPATH=/opt/flax/rackpost:/opt/flax/rackpost/vendor-python3.12 exec python3.12"
            " -m flax_rack \"$@\"") in wrap


def test_vendor_dir_is_keyed_by_the_interpreter_and_the_old_one_goes():
    """Vendored pips carry compiled cpython-3xx modules (charset_normalizer), so an
    interpreter change must land in a fresh dir, and the unkeyed python3.11 one is removed."""
    defaults = yaml.safe_load(open(f"{ROLES}/apply_rackpost/defaults/main.yml"))
    assert defaults["apply_rackpost_python"] == "python3.12"
    assert "{{ apply_rackpost_python }}" in defaults["apply_rackpost_vendor_dir"]
    tasks = tasks_list()
    pips = [x["ansible.builtin.pip"] for x in tasks if "ansible.builtin.pip" in x]
    assert pips and all("{{ apply_rackpost_vendor_dir }}" in p["extra_args"] for p in pips)
    gone = [x for x in tasks if x["name"] == "Remove the old unkeyed vendor dir"][0]
    assert gone["ansible.builtin.file"] == {"path": "{{ apply_rackpost_dest_dir }}/vendor",
                                            "state": "absent"}


def test_no_unquoted_brace_regex_location():
    """nginx parses `{` in an unquoted location regex as a block start
    ("unknown directive 2}[0-9]"); every regex location with braces must be quoted."""
    import re
    for t in (pathlib.Path(ROLES) / "apply_bang_services/templates/nginx-lab-ui.conf.j2",
              pathlib.Path(ROLES) / "apply_flax_stack/templates/nginx.conf.j2"):
        for line in t.read_text().splitlines():
            m = re.match(r"\s*location\s+~\*?\s+(\S+)", line)
            if m and "{" in m.group(1).rstrip("{"):
                assert m.group(1).startswith('"'), f"{t.name}: unquoted brace regex: {line.strip()}"


def tasks_list():
    return yaml.safe_load(open(f"{ROLES}/apply_rackpost/tasks/main.yml"))


def _shipped_tree(dest):
    """The deploy dir as the role lays it out: flax_rack, flax_rungs, and only the
    listed flax_post and flax_switch_sense modules."""
    import shutil
    defaults = yaml.safe_load(open(f"{ROLES}/apply_rackpost/defaults/main.yml"))
    for pkg in ("flax_rack", "flax_rungs"):
        shutil.copytree(f"{CODE}/{pkg}", dest / pkg,
                        ignore=shutil.ignore_patterns("__pycache__"))
    for m in defaults["apply_rackpost_post_modules"]:
        (dest / "flax_post" / m).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(f"{CODE}/flax_post/{m}", dest / "flax_post" / m)
    (dest / "flax_switch_sense").mkdir()
    for m in defaults["apply_rackpost_switch_sense_modules"]:
        shutil.copy(f"{CODE}/flax_switch_sense/{m}", dest / "flax_switch_sense" / m)


def test_shipped_tree_imports_without_psycopg(tmp_path):
    """The daemon is stdlib: its whole import graph resolves from the shipped files
    alone, with psycopg made unimportable (a module that needs it would raise)."""
    import subprocess
    import sys
    _shipped_tree(tmp_path)
    code = ("import sys; sys.path[:0] = [sys.argv[1]];"
            "sys.modules['psycopg'] = sys.modules['psycopg2'] = sys.modules['psycopg_pool'] = None;"
            "sys.modules['fastapi'] = sys.modules['uvicorn'] = sys.modules['starlette'] = None;"
            "import flax_rack.server, flax_rack.qualify, flax_rungs.sol, flax_post.qualify_rules;"
            "import flax_post.shipchecks, flax_post.blocklist, flax_post.observe.fru;"
            "import flax_post.observe.bootlog, flax_rungs.fwsteps, flax_rungs.ha, flax_rungs.identity;"
            "import flax_rack.switches, flax_rack.discover_ladder, flax_rack.linkmon;"
            "assert flax_rack.switches.make_driver.__module__ == 'flax_switch_sense.drivers';"
            "assert flax_rack.switches.make_driver.__code__.co_filename.startswith(sys.argv[1]);"
            "import flax_post.fwd.version, flax_post.fwd.manifest, flax_post.fwd.redfish;"
            "import flax_post.fwd.flasher, flax_post.fwd.creds;"
            "import flax_post.biosd.classify, flax_post.biosd.manifest, flax_post.biosd.driver;"
            "import flax_post.biosd.creds;"
            "import flax_post.nicd.classify, flax_post.nicd.manifest, flax_post.nicd.driver;"
            "assert flax_post.fwd.flasher.__file__.startswith(sys.argv[1]);"
            "assert flax_rack.qualify.qualify_rules.__file__.startswith(sys.argv[1]);"
            "bad = [m for m in sys.modules if m.startswith('psycopg') and sys.modules[m]];"
            "assert not bad, bad")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_role_ships_post_modules():
    defaults = yaml.safe_load(open(f"{ROLES}/apply_rackpost/defaults/main.yml"))
    mods = defaults["apply_rackpost_post_modules"]
    assert mods == ["__init__.py", "version.py", "population.py", "inventory.py", "qualclient.py",
                    "qualify_rules.py", "shipchecks.py", "blocklist.py", "fio_judge.py",
                    "fio-baselines-v1.json", "observe/__init__.py",
                    "observe/fru.py", "observe/family_map.py", "observe/family_map_live.py",
                    "observe/bootlog.py",
                    "fwd/__init__.py", "fwd/version.py", "fwd/manifest.py", "fwd/redfish.py",
                    "fwd/entity_cache.py", "fwd/flasher.py", "fwd/enforce.py", "fwd/creds.py",
                    "biosd/__init__.py", "biosd/version.py", "biosd/classify.py",
                    "biosd/manifest.py", "biosd/driver.py", "biosd/creds.py",
                    "nicd/__init__.py", "nicd/classify.py", "nicd/manifest.py",
                    "nicd/driver.py"]
    # one rsync ships exactly the listed modules (+ static files): anchored --include per
    # file, every dir walked (rsync creates subpackage dirs), everything else excluded
    tasks = yaml.safe_load(open(f"{ROLES}/apply_rackpost/tasks/main.yml"))
    t = [x for x in tasks if x["name"] == "Deploy shared flax_post modules + static files"][0]
    sync = t["ansible.posix.synchronize"]
    assert sync["dest"] == "{{ apply_rackpost_dest_dir }}/flax_post/"
    assert "apply_rackpost_post_modules" in t["vars"]["_files"]
    assert "'--include=*/'" in sync["rsync_opts"] and "'--exclude=*'" in sync["rsync_opts"]
    assert "--include=/" in sync["rsync_opts"]
    for m in mods:
        assert os.path.isfile(f"{CODE}/flax_post/{m}")


def test_role_ships_switch_sense_drivers_and_vendors_requests():
    """rackpost's switch reads (flax_rack/switches.py) import the shared read drivers:
    the role copies exactly those modules (never fetcher/db: psycopg) and vendors
    requests + urllib3 (driver_eos) next to openpyxl, since a bang's host python
    may lack them."""
    defaults = yaml.safe_load(open(f"{ROLES}/apply_rackpost/defaults/main.yml"))
    mods = defaults["apply_rackpost_switch_sense_modules"]
    assert "fetcher.py" not in mods and "db.py" not in mods
    for m in mods:
        assert os.path.isfile(f"{CODE}/flax_switch_sense/{m}")
    tasks = tasks_list()
    t = [x for x in tasks if x["name"] == "Deploy shared flax_switch_sense driver modules"][0]
    sync = t["ansible.posix.synchronize"]
    assert sync["dest"] == "{{ apply_rackpost_dest_dir }}/flax_switch_sense/"
    assert "apply_rackpost_switch_sense_modules" in sync["rsync_opts"] and "'--exclude=*'" in sync["rsync_opts"]
    vend = [x for x in tasks if x.get("ansible.builtin.pip", {}).get("name")
            == "{{ apply_rackpost_switch_driver_pips }}"]
    assert len(vend) == 1
    exe = jinja2.Template(vend[0]["ansible.builtin.pip"]["executable"]).render(**defaults)
    assert exe == "pip3.12"


def test_vendored_switch_driver_pips_are_pinned_and_install_only_when_missing(tmp_path):
    """requests/urllib3 are exact pins, and pip (which always reports changed
    under --target, and notifies the restart) runs only when a pinned version's
    dist-info marker is missing -- not on every deploy."""
    import jinja2
    defaults = yaml.safe_load(open(f"{ROLES}/apply_rackpost/defaults/main.yml"))
    pins = defaults["apply_rackpost_switch_driver_pips"]
    assert sorted(pins) == ["requests==2.31.0", "urllib3==2.0.7"]
    tasks = tasks_list()
    check = [x for x in tasks if x["name"] == "Check the vendored requests + urllib3 pins"][0]
    install = [x for x in tasks if x["name"] == "Install requests + urllib3 for the switch drivers"][0]
    env = jinja2.Environment()

    def render(expr, **kw):
        return env.from_string(expr).render(**kw)

    vendor = tmp_path / "vendor-python3.12"
    vendor.mkdir()
    ctx = {"apply_rackpost_switch_driver_pips": pins, "apply_rackpost_vendor_dir": str(vendor)}
    items = env.compile_expression(check["loop"].strip("{} "))(**ctx)
    paths = [render(check["ansible.builtin.stat"]["path"], item=i, **ctx) for i in items]

    def needs_install():
        results = [{"stat": {"exists": os.path.isdir(p)}} for p in paths]
        return env.compile_expression(install["when"])(
            apply_rackpost_pip_markers={"results": results})

    assert needs_install()
    for pin in pins:   # what pip --target leaves behind for these pins
        (vendor / (pin.replace("==", "-") + ".dist-info")).mkdir()
    assert not needs_install()
    (vendor / "urllib3-2.0.7.dist-info").rmdir()
    assert needs_install()


def test_shipped_modules_import_under_the_host_python(tmp_path):
    import shutil
    import subprocess
    py = shutil.which("python3.12")
    assert py, "python3.12 is the bangs' host python; ./setup installs it here"
    defaults = yaml.safe_load(open(f"{ROLES}/apply_rackpost/defaults/main.yml"))
    dest = tmp_path / "flax_post"
    dest.mkdir()
    for m in defaults["apply_rackpost_post_modules"]:
        (dest / m).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(f"{CODE}/flax_post/{m}", dest / m)
    r = subprocess.run([py, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]);"
                        "import flax_post.population, flax_post.qualclient, flax_post.observe.bootlog;"
                        "import flax_post.fwd.flasher, flax_post.biosd.classify, flax_post.nicd.classify",
                        str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_build_id_task_computes_from_the_checkout(tmp_path):
    """The role's BUILD task, run by real ansible on localhost against a scratch checkout:
    <flax_post version>·<short sha>[-dirty-<hash>], dirty = tracked changes or untracked
    files under the deployed dirs, the hash following their content."""
    import shutil
    import subprocess
    import pytest
    if not shutil.which("ansible-playbook"):
        pytest.skip("no ansible-playbook")
    t = [x for x in tasks_list() if x["name"] == "Write the rackpost build id"][0]
    assert t["notify"] == "Restart flax-rackpost"
    assert t["ansible.builtin.copy"]["dest"] == "{{ apply_rackpost_dest_dir }}/BUILD"
    t = {k: v for k, v in t.items() if k not in ("become", "notify")}
    t["ansible.builtin.copy"] = {k: v for k, v in t["ansible.builtin.copy"].items()
                                 if k not in ("owner", "group")}
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([{"hosts": "localhost", "gather_facts": False,
                                   "connection": "local", "tasks": [t]}], allow_unicode=True))
    repo, dest = tmp_path / "repo", tmp_path / "dest"
    (repo / "flax_post").mkdir(parents=True)
    (repo / "flax_rack").mkdir()
    dest.mkdir()
    (repo / "flax_post/version.py").write_text('__version__ = "0.9.1"\n')
    (repo / "flax_rack/a.py").write_text("a = 1\n")
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True,  # noqa: E731
                                    text=True, check=True).stdout.strip()
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    sha = git("rev-parse", "--short", "HEAD")

    def build():
        r = subprocess.run(["ansible-playbook", "-i", "localhost,", str(pb),
                            "-e", f"apply_rackpost_repo={repo}",
                            "-e", f"apply_rackpost_dest_dir={dest}"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        return (dest / "BUILD").read_text()
    assert build() == f"0.9.1·{sha}\n"
    (repo / "notes.txt").write_text("not deployed\n")       # outside the deployed dirs
    assert build() == f"0.9.1·{sha}\n"
    (repo / "flax_rack/new.py").write_text("b = 1\n")       # untracked, deployed
    one = build()
    assert re.fullmatch(rf"0\.9\.1·{sha}-dirty-[0-9a-f]{{7}}\n", one)
    (repo / "flax_rack/new.py").write_text("b = 2\n")
    two = build()
    assert two != one and "-dirty-" in two
    (repo / "flax_rack/new.py").unlink()
    (repo / "flax_rack/a.py").write_text("a = 2\n")         # a tracked change
    assert build() not in (one, two, f"0.9.1·{sha}\n")

