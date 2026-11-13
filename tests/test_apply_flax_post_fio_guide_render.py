"""flax-post-fio-guide: an hourly oneshot that renders /guides/fio-drives.html on the VIP holder
(ExecCondition, so the standby skips without a failed unit), into the host's /srv/pxe/guides."""
import pathlib

import jinja2
import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1] / "roles" / "apply_flax_post"
VIP_GATE = """/bin/bash -c '. /etc/flax/site.env && ip -o -4 addr | grep -qE " $${MGMT_VIP}/"'"""


def _render(name):
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(ROLE / "templates")),
                              undefined=jinja2.StrictUndefined).get_template(name).render()


def test_the_service_renders_the_guide_from_the_post_image_on_the_vip_holder_only():
    u = _render("flax-post-fio-guide.service.j2")
    assert "Type=oneshot" in u
    assert "ExecCondition=" + VIP_GATE in u
    assert "ExecStartPre" not in u  # a standby skip must not leave a failed unit
    assert "--env-file /etc/flax/flax-post/flax-post.env" in u and "--env-file /etc/flax/site.env" in u
    assert "-v /srv/pxe/guides:/guides" in u
    assert "flax-control:${FLAX_POST_IMAGE_TAG}" in u
    assert u.rstrip().endswith("python -m flax_post.fio_guide --out /guides/fio-drives.html")


def test_the_timer_is_hourly_and_the_tasks_enable_it_and_make_the_guides_dir():
    t = _render("flax-post-fio-guide.timer.j2")
    assert "OnCalendar=hourly" in t and "Persistent=true" in t and "WantedBy=timers.target" in t
    tasks = yaml.safe_load((ROLE / "tasks" / "systemd.yml").read_text())
    dirs = [x["ansible.builtin.file"] for x in tasks
            if x.get("ansible.builtin.file", {}).get("path") == "/srv/pxe/guides"]
    assert dirs and dirs[0]["state"] == "directory" and dirs[0]["mode"] == "0755"
    units = {jinja2.Template(x["ansible.builtin.template"]["dest"]).render(item=i)
             for x in tasks if "ansible.builtin.template" in x for i in x.get("loop") or [None]}
    assert {"/etc/systemd/system/flax-post-fio-guide.service",
            "/etc/systemd/system/flax-post-fio-guide.timer"} <= units
    en = [x["ansible.builtin.systemd"] for x in tasks
          if x.get("ansible.builtin.systemd", {}).get("name") == "flax-post-fio-guide.timer"]
    assert en and en[0]["enabled"] is True and en[0]["state"] == "started"
