"""report.html: one self-contained page (no external assets, no links)."""
from html import escape

CSS = """body{font:14px/1.4 system-ui,sans-serif;margin:16px;background:#fff;color:#111}
table{border-collapse:collapse;width:100%}td,th{padding:3px 8px;border-bottom:1px solid #ddd;text-align:left}
.FAIL{background:#fdd}.unreachable{background:#e6e0f5}.warn{background:#ffe9b3}.info{color:#555}.dormant{color:#888}
@media (prefers-color-scheme:dark){body{background:#111;color:#eee}td,th{border-color:#333}
.FAIL{background:#511}.unreachable{background:#332a4d}.warn{background:#533d00}}"""


def render_html(title: str, summary: str, rows: list) -> str:
    body = "".join(
        f'<tr class="{escape(r.status)}"><td>{escape(r.site)}</td><td>{escape(r.host)}</td>'
        f"<td>{escape(r.section)}</td><td>{escape(r.item)}</td><td>{escape(r.status)}</td>"
        f"<td>{escape(r.value)}</td></tr>" for r in rows)
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{escape(title)}</title>"
            f"<style>{CSS}</style></head><body><h1>{escape(title)}</h1><p>{escape(summary)}</p>"
            "<table><tr><th>site</th><th>bang</th><th>section</th><th>item</th><th>status</th>"
            f"<th>value</th></tr>{body}</table></body></html>")
