"""Task 1 — design-system component macros (_components.html).

Renders a minimal template that imports the macros and asserts the emitted
classes match what style.css styles. Pure Jinja (no DB / TestClient needed).
"""
import pytest
from jinja2 import Environment, FileSystemLoader


@pytest.fixture
def env():
    return Environment(
        loader=FileSystemLoader("flax_control/templates"),
        autoescape=True,
    )


def render(env, body):
    tmpl = env.from_string('{% import "_components.html" as c %}' + body)
    return tmpl.render()


def test_badge_kind_class(env):
    out = render(env, "{{ c.badge('OK', 'g') }}")
    assert 'class="badge g"' in out
    assert ">OK<" in out


def test_badge_each_kind(env):
    for kind in ("g", "w", "c", "a", "n"):
        out = render(env, "{{ c.badge('x', '%s') }}" % kind)
        assert 'class="badge %s"' % kind in out


def test_page_header_with_sub(env):
    out = render(env, "{{ c.page_header('Nodes', 'sub text') }}")
    assert '<h1 class="page">Nodes' in out
    assert 'class="page-sub"' in out
    assert "sub text" in out


def test_page_header_without_sub(env):
    out = render(env, "{{ c.page_header('Nodes') }}")
    assert '<h1 class="page">Nodes' in out
    assert "page-sub" not in out


def test_kv_row(env):
    out = render(env, "{{ c.kv('Power', 'on') }}")
    assert 'class="kv"' in out
    assert 'class="k"' in out
    assert 'class="v"' in out
    assert "Power" in out and "on" in out


def test_stat_tile(env):
    out = render(env, "{{ c.stat_tile('Devices', 247) }}")
    assert 'class="kpi"' in out
    assert 'class="lab"' in out
    assert 'class="val"' in out
    assert "247" in out


def test_stat_tile_with_note(env):
    out = render(env, "{{ c.stat_tile('BMC firmware', 42, 'up-to-date') }}")
    assert "up-to-date" in out


def test_data_table_open_close(env):
    out = render(
        env,
        "{{ c.data_table_open(['File', 'Status']) }}"
        "<tr><td>a</td><td>b</td></tr>"
        "{{ c.data_table_close() }}",
    )
    assert 'class="data-table"' in out
    assert "<thead>" in out
    assert "<th>File</th>" in out
    assert "<th>Status</th>" in out
    assert "<tbody>" in out
    assert "</tbody></table>" in out
    # wrapped in an overflow container
    assert "table-scroll" in out


def test_data_table_escapes_headers(env):
    out = render(env, "{{ c.data_table_open(['<x>']) }}{{ c.data_table_close() }}")
    assert "&lt;x&gt;" in out


# ---- Task 8.5 — outcome_badge (residual .pill -> design system) ----------

@pytest.mark.parametrize("value,kind", [
    ("applied", "g"), ("active", "g"), ("link", "g"), ("up", "g"),
    ("ok", "g"), ("converged", "g"), ("success", "g"),
    ("noop", "n"), ("neutral", "n"), ("info", "n"),
    ("deferred", "w"), ("skipped", "w"), ("warn", "w"), ("down", "w"),
    ("failed", "c"), ("error", "c"), ("crit", "c"), ("failure", "c"),
])
def test_outcome_badge_kind_mapping(env, value, kind):
    out = render(env, "{{ c.outcome_badge('%s') }}" % value)
    assert 'class="badge %s"' % kind in out
    assert ">%s<" % value in out


def test_outcome_badge_unknown_value_falls_back_to_neutral(env):
    """Raw/unmapped values (e.g. a numeric lease-state code) render as a
    neutral badge, same as the old `.pill.neutral` fallback."""
    out = render(env, "{{ c.outcome_badge(3) }}")
    assert 'class="badge n"' in out
    assert ">3<" in out


def test_outcome_badge_is_case_insensitive_on_lookup_but_preserves_text(env):
    out = render(env, "{{ c.outcome_badge('Success') }}")
    assert 'class="badge g"' in out
    assert ">Success<" in out  # display text keeps original casing
