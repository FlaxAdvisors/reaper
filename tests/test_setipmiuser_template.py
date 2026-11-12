import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

J2 = (REPO / "roles/apply_pxe_payloads/files/post/setipmiuser.sh.j2")


def test_template_has_no_plaintext_passwords():
    t = J2.read_text()
    # every `set password` line must interpolate a vault var, never a bare literal
    pw_lines = [l for l in t.splitlines() if "set password" in l]
    assert pw_lines and all("{{" in l for l in pw_lines)


def test_two_distinct_credential_pairs_templated():
    t = J2.read_text()
    # root password pair
    root_pw = [l for l in t.splitlines() if "user set password $rootid" in l]
    assert root_pw and all("{{ post_ipmi_root_password }}" in l for l in root_pw)
    # admin (USERID) pair: name + password, both from the admin vars
    admin_pw = [l for l in t.splitlines() if "user set password $userid" in l]
    assert admin_pw and all("{{ post_ipmi_admin_password }}" in l for l in admin_pw)
    name_ln = [l for l in t.splitlines() if "user set name $userid" in l]
    assert name_ln and all("{{ post_ipmi_admin_user }}" in l for l in name_ln)
    # the account lookup no longer greps a bare literal USERID (uses the var)
    assert "getuserid USERID" not in t
    # the two password vars are distinct references (pairs kept separate)
    assert "{{ post_ipmi_root_password }}" in t and "{{ post_ipmi_admin_password }}" in t
