"""flax-post-deployd runs on the bang's host python3.12 (2026-11-12, everything on 3.12), with
psycopg vendored into a dir keyed by that interpreter: psycopg_binary is compiled per
cpython version, so an interpreter change must install a fresh set, and the old unkeyed
(python3.11) dir is removed."""
import pathlib

import jinja2
import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1] / "roles" / "apply_flax_post"


def _defaults():
    d = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    for _ in range(3):   # resolve the defaults that reference other defaults
        d = {k: jinja2.Template(v).render(**d) if isinstance(v, str) else v for k, v in d.items()}
    return d


def test_unit_runs_the_host_python312_on_the_keyed_vendor_dir():
    ctx = dict(_defaults(), post_iperf_server="bang-edam")
    unit = jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
                              undefined=jinja2.StrictUndefined) \
        .get_template("flax-post-deployd.service.j2").render(**ctx)
    assert "ExecStart=/usr/bin/python3.12 -m flax_post.deployd" in unit
    assert ("Environment=PYTHONPATH=/opt/flax/post-deployd:/opt/flax/post-deployd/vendor-python3.12"
            in unit)


def test_psycopg_is_vendored_by_that_interpreters_pip_and_the_old_dir_goes():
    d = _defaults()
    tasks = yaml.safe_load((ROLE / "tasks" / "systemd.yml").read_text())
    pip = [t["ansible.builtin.pip"] for t in tasks
           if t.get("ansible.builtin.pip", {}).get("name") == "{{ apply_flax_post_deployd_pips }}"]
    assert len(pip) == 1
    assert jinja2.Template(pip[0]["executable"]).render(**d) == "pip3.12"
    assert "--target {{ apply_flax_post_deployd_vendor_dir }}" in pip[0]["extra_args"]
    marker = [t for t in tasks if t["name"] == "Check the vendored psycopg pin"][0]
    assert marker["ansible.builtin.stat"]["path"].startswith("{{ apply_flax_post_deployd_vendor_dir }}/")
    gone = [t for t in tasks if t.get("ansible.builtin.file", {}).get("state") == "absent"
            and t["ansible.builtin.file"]["path"] == "{{ apply_flax_post_deployd_dir }}/vendor"]
    assert len(gone) == 1
