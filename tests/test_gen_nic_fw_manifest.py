import importlib.util, os
from importlib.machinery import SourceFileLoader
# spec_from_file_location can't infer a loader for an extensionless filename
# (no .py suffix), so the loader is passed explicitly.
_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "gen-nic-fw-manifest")
_spec = importlib.util.spec_from_file_location(
    "gen_nic_fw_manifest", _path, loader=SourceFileLoader("gen_nic_fw_manifest", _path))
gen = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(gen)

def test_build_manifest_enriches_with_queried_target():
    intent = {"FB_2450111034": {"dir": "MCX4411A-ACQN", "bin": "fw-x"}}
    def query_bin(d, b):
        assert (d, b) == ("MCX4411A-ACQN", "fw-x")
        return ("14.32.1010", "MT_2450112034")
    out = gen.build_manifest(intent, query_bin)
    assert out == {"FB_2450111034": {"dir": "MCX4411A-ACQN", "bin": "fw-x",
                                     "target_version": "14.32.1010", "target_psid": "MT_2450112034"}}

def test_build_manifest_skips_bin_that_fails_query():
    intent = {"A": {"dir": "d", "bin": "b1"}, "B": {"dir": "d", "bin": "b2"}}
    def query_bin(d, b):
        if b == "b2":
            raise RuntimeError("bin missing on share")
        return ("14.32.1010", "MT_x")
    out = gen.build_manifest(intent, query_bin)
    assert "A" in out and "B" not in out
