# flax_post/fwd/redfish.py
"""Minimal Redfish client for post Tioga Pass BMCs.

Transport + auth + TLS mirror flax_reconcile/bmc_reset.py: raw http.client over an
unverified TLS context (BMCs ship legacy self-signed certs), HTTP Basic auth,
trying each credential until one authenticates. No `requests` dependency.

Methods return (value, detail); value is None on any failure (the caller turns
that into a fault row). Tioga Pass success is VERSION-based: callers re-read
get_fw_version after the BMC self-activates, not TaskState.
"""
import base64
import http.client
import json
import ssl

_GET_TIMEOUT = 5
_POST_TIMEOUT = 300   # a ~36MB image push to a slow BMC; the working bmc-fw-update uses 300s
_TASK_TIMEOUT = 10

# Unverified TLS — same fix as bmc_reset.py / the eAPI driver.
_UNVERIFIED_SSL = ssl.create_default_context()
_UNVERIFIED_SSL.check_hostname = False
_UNVERIFIED_SSL.verify_mode = ssl.CERT_NONE
_UNVERIFIED_SSL.set_ciphers("DEFAULT:@SECLEVEL=0")


def _basic_auth_header(user, password):
    token = base64.b64encode((user + ":" + password).encode()).decode()
    return "Basic " + token


def _task_id_from(location, raw):
    """Extract the TaskService task id from the Location header, else the body @odata.id."""
    if location and "/Tasks/" in location:
        return location.rstrip("/").rsplit("/", 1)[-1]
    try:
        odata = (json.loads(raw) or {}).get("@odata.id", "")
    except (ValueError, AttributeError):
        odata = ""
    return odata.rstrip("/").rsplit("/", 1)[-1] if odata else ""


class RedfishClient:
    def __init__(self, bmc_ip: str, creds: list):
        self.bmc_ip = bmc_ip
        self.creds = creds or []

    # --- low-level helpers -------------------------------------------------
    def _request(self, method, path, body=None, content_type=None, timeout=_GET_TIMEOUT):
        """Try each cred. Returns (status, raw_bytes, detail, location).

        status is None on transport/auth failure; location is the response's
        Location header (None if absent) — the firmware push returns its task there.
        """
        if not self.bmc_ip:
            return None, b"", "no bmc_ip", None
        if not self.creds:
            return None, b"", "no redfish credentials configured", None
        last = "no cred authenticated"
        for cred in self.creds:
            user, password = cred.get("bmcuser"), cred.get("bmcpass")
            if not user or not password:
                continue
            headers = {"Authorization": _basic_auth_header(user, password), "Connection": "close"}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                conn = http.client.HTTPSConnection(self.bmc_ip, timeout=timeout, context=_UNVERIFIED_SSL)
                conn.request(method, path, body=body, headers=headers)
                r = conn.getresponse()
                status, raw, location = r.status, r.read(), r.getheader("Location")
                try:
                    conn.close()
                except Exception:
                    pass
            except (http.client.HTTPException, ConnectionError, OSError) as e:
                last = "%s %s transport error: %s" % (method, path, e)
                continue
            if status == 401:
                last = "%s %s HTTP 401" % (method, path)
                continue
            return status, raw, "HTTP %d" % status, location
        return None, b"", last, None

    def _get_json(self, path, timeout=_GET_TIMEOUT):
        status, raw, detail, _location = self._request("GET", path, timeout=timeout)
        if status is None or status >= 300:
            return None, detail if status is None else ("%s HTTP %d" % (path, status))
        try:
            return json.loads(raw), "ok"
        except ValueError as e:
            return None, "non-JSON body: %s" % e

    def _system_member(self):
        """Resolve the first /redfish/v1/Systems member object (GET it)."""
        coll, detail = self._get_json("/redfish/v1/Systems")
        if coll is None:
            return None, detail
        members = coll.get("Members") or []
        if not members:
            return None, "no Systems members"
        odata = members[0].get("@odata.id")
        if not odata:
            return None, "Systems member missing @odata.id"
        obj, detail = self._get_json(odata)
        if obj is None:
            return None, detail
        return obj, "ok"

    def _chassis_member(self):
        """Resolve the first /redfish/v1/Chassis member object (GET it)."""
        coll, detail = self._get_json("/redfish/v1/Chassis")
        if coll is None:
            return None, detail
        members = coll.get("Members") or []
        if not members:
            return None, "no Chassis members"
        odata = members[0].get("@odata.id")
        if not odata:
            return None, "Chassis member missing @odata.id"
        return self._get_json(odata)

    def _get_json_unauth(self, path, timeout=_GET_TIMEOUT):
        """GET a Redfish path WITHOUT credentials -> (dict, detail) | (None, detail).

        The service-root identity probe is deliberately unauthenticated: it works
        regardless of host power and even when the board rejects our BMC creds (the
        AMI OEM boards 401 credentials-bmc.json). Mirrors flax_observe.bmc_probe."""
        if not self.bmc_ip:
            return None, "no bmc_ip"
        try:
            conn = http.client.HTTPSConnection(self.bmc_ip, timeout=timeout, context=_UNVERIFIED_SSL)
            conn.request("GET", path, headers={"Connection": "close"})
            r = conn.getresponse()
            status, raw = r.status, r.read()
            try:
                conn.close()
            except Exception:
                pass
        except (http.client.HTTPException, ConnectionError, OSError) as e:
            return None, "GET %s transport error: %s" % (path, e)
        if status >= 300:
            return None, "%s HTTP %d" % (path, status)
        try:
            return json.loads(raw), "ok"
        except ValueError as e:
            return None, "non-JSON body: %s" % e

    def _system_path(self):
        """The first Systems member @odata.id (the PATCH/Reset target), or (None, detail)."""
        coll, detail = self._get_json("/redfish/v1/Systems")
        if coll is None:
            return None, detail
        members = coll.get("Members") or []
        if not members or not members[0].get("@odata.id"):
            return None, "no Systems member @odata.id"
        return members[0]["@odata.id"], "ok"

    # --- public API --------------------------------------------------------
    def get_product_name(self):
        obj, detail = self._system_member()
        if obj is None:
            return None, detail
        model = (obj.get("Model") or "").strip()
        maker = (obj.get("Manufacturer") or "").strip()
        name = (maker + " " + model).strip()
        return (name or None), ("ok" if name else "no Model/Manufacturer")

    def get_power_state(self):
        obj, detail = self._system_member()
        if obj is None:
            return None, detail
        state = obj.get("PowerState")
        return (state or None), ("ok" if state else "no PowerState")

    def get_serial(self):
        """Board serial via Redfish (the IPMI-FRU fallback for Redfish-only AMI
        boards). Prefer Systems.SerialNumber (SMBIOS-backed -> blank when the host
        is powered off), then Chassis.SerialNumber (BMC-resident -> readable off).
        Returns (serial, detail); (None, detail) when neither exposes one."""
        obj, sdetail = self._system_member()
        if obj is not None:
            s = (obj.get("SerialNumber") or "").strip()
            if s:
                return s, "ok"
        cobj, cdetail = self._chassis_member()
        if cobj is not None:
            s = (cobj.get("SerialNumber") or "").strip()
            if s:
                return s, "ok"
        return None, (sdetail if obj is None else cdetail)

    def get_redfish_root(self):
        """UNAUTHENTICATED GET /redfish/v1/ -> (is_bmc, redfish_version, product_name).

        A service root whose @odata.type names ServiceRoot, or that links Managers,
        proves a BMC (fast, no creds, host-power independent). product_name is
        best-effort: the root's own Product ('AMI Redfish Server'), else a synthesized
        '<Oem-vendor> Redfish' ('Ami Redfish') so an OEM board that hides Product still
        carries a manifest-matchable identifier. Mirrors flax_observe.bmc_probe so the
        post fwd driver can recognise a reachable-but-not-onetree board it must not flash."""
        root, _detail = self._get_json_unauth("/redfish/v1/")
        if not isinstance(root, dict):
            return False, None, None
        is_bmc = ("serviceroot" in str(root.get("@odata.type", "")).lower()
                  or "Managers" in root)
        if not is_bmc:
            return False, None, None
        redfish_version = (root.get("RedfishVersion") or "").strip() or None
        product = (root.get("Product") or "").strip() or None
        if not product:
            oem = root.get("Oem")
            if isinstance(oem, dict) and oem:
                product = (next(iter(oem)).strip() + " Redfish").strip() or None
        return True, redfish_version, product

    def get_fw_version(self, fw_id):
        obj, detail = self._get_json("/redfish/v1/UpdateService/FirmwareInventory/" + fw_id)
        if obj is None:
            return None, detail
        ver = obj.get("Version")
        return (ver or None), ("ok" if ver else "no Version")

    def post_flash(self, image, filename):
        # flax-onetree bmcweb resets multipart/form-data; the working push (per the
        # ghost bmc-fw-update bin) is the raw image as application/octet-stream to the
        # HttpPushUri. filename is unused by this push form (kept for the caller's API).
        status, raw, detail, location = self._request(
            "POST", "/redfish/v1/UpdateService/update",
            body=image, content_type="application/octet-stream", timeout=_POST_TIMEOUT)
        if status is None or status >= 300:
            return None, detail if status is None else ("flash POST HTTP %d" % status)
        task_id = _task_id_from(location, raw)
        if not task_id:
            return None, "flash accepted (HTTP %d) but no task id in Location/body" % status
        return task_id, "ok"

    def poll_task(self, task_id):
        obj, detail = self._get_json(
            "/redfish/v1/TaskService/Tasks/" + task_id, timeout=_TASK_TIMEOUT)
        if obj is None:
            return None, 0, detail
        return obj.get("TaskState"), int(obj.get("PercentComplete") or 0), "ok"

    def set_boot_pxe(self):
        """PATCH the system Boot override to Pxe/Continuous/UEFI (wiwynn-tp needs Redfish;
        IPMI boot flags are unsupported on this fleet). Returns (True, detail) on 2xx."""
        path, detail = self._system_path()
        if path is None:
            return None, detail
        body = json.dumps({"Boot": {
            "BootSourceOverrideEnabled": "Continuous",
            "BootSourceOverrideTarget": "Pxe",
            "BootSourceOverrideMode": "UEFI"}}).encode()
        status, _raw, sdetail, _loc = self._request(
            "PATCH", path, body=body, content_type="application/json")
        if status is None or status >= 300:
            return None, sdetail if status is None else ("boot-pxe PATCH HTTP %d" % status)
        return True, "ok"

    def power_on(self):
        """POST ComputerSystem.Reset {ResetType: On}. Returns (True, detail) on 2xx."""
        path, detail = self._system_path()
        if path is None:
            return None, detail
        body = json.dumps({"ResetType": "On"}).encode()
        status, _raw, sdetail, _loc = self._request(
            "POST", path + "/Actions/ComputerSystem.Reset",
            body=body, content_type="application/json")
        if status is None or status >= 300:
            return None, sdetail if status is None else ("power-on POST HTTP %d" % status)
        return True, "ok"

    def _manager_path(self):
        """The first /redfish/v1/Managers member @odata.id (the BMC manager)."""
        coll, detail = self._get_json("/redfish/v1/Managers")
        if coll is None:
            return None, detail
        members = coll.get("Members") or []
        if not members or not members[0].get("@odata.id"):
            return None, "no Managers member @odata.id"
        return members[0]["@odata.id"], "ok"

    def manager_reset(self, reset_type="ForceRestart"):
        """POST Manager.Reset to reboot the BMC ITSELF (not the host) -- out of
        context for SSH/IPMI, so it dodges the `ipmitool mc reset cold` hang.
        Returns (True, detail) on a 2xx accept; does NOT poll for the BMC to
        return (mirrors flax_reconcile.bmc_reset). AMI often returns 204."""
        path, detail = self._manager_path()
        if path is None:
            return False, detail
        body = json.dumps({"ResetType": reset_type}).encode()
        status, _raw, mdetail, _loc = self._request(
            "POST", path + "/Actions/Manager.Reset",
            body=body, content_type="application/json", timeout=15)
        if status is None or status >= 300:
            return False, mdetail if status is None else ("Manager.Reset HTTP %d" % status)
        return True, "ok (HTTP %d)" % status
