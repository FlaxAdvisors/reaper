"""/etc/flax/family-map is bind-mounted (read-only, whole directory) into every
unit whose process resolves a product name through it: the units that read FRU 0
(spec 2026-09-14-fru-id0-only §6.1, §6.2) and, since flax-post 0.9.183 (3cdea9c),
flax-post-biosd, which maps the DMI Product Name to its manifest family
(flax_post/biosd/manifest.py BiosMatcher)."""
import pathlib

import flax_observe

ROOT = pathlib.Path(flax_observe.__file__).resolve().parents[1]
MOUNT = "-v /etc/flax/family-map:/etc/flax/family-map:ro"


def _unit(rel):
    return (ROOT / rel).read_text()


def test_observe_unit_mounts_the_family_map():
    assert MOUNT in _unit("roles/apply_flax_observe/templates/flax-observe.service.j2")


def test_post_units_that_use_the_family_map_mount_it():
    for name in ("flax-post.service.j2", "flax-post-observe.service.j2", "flax-post-biosd.service.j2"):
        assert MOUNT in _unit("roles/apply_flax_post/templates/" + name), name


def test_post_units_that_never_use_the_family_map_are_unchanged():
    for name in ("flax-post-fwd.service.j2", "flax-post-nicd.service.j2"):
        assert MOUNT not in _unit("roles/apply_flax_post/templates/" + name), name


def test_post_fwd_mounts_vlans_json_for_the_family_lookup():
    """fwd names a Leopard by its reservation VLAN via vlans.json
    (manifest.family_for_vid); without the mount it reads None and treats the
    AMI BMC as onetree (found live 2026-11-06, 0.9.201)."""
    assert ("-v /etc/flax/vlans.json:/etc/flax/vlans.json:ro"
            in _unit("roles/apply_flax_post/templates/flax-post-fwd.service.j2"))
