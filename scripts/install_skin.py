"""
Installs a purchased HUD skin (a `.jarvisskin.zip` from `package_skin.py`)
into this clone: drops its CSS/JS into `jarvis/hud/static/`, wires the
<link>/<script> tags and Google Fonts into index.html, inserts its markup
into #skins-inner, and registers it in themes.js's theme switcher. Refuses
to touch anything if the skin looks already installed, so running it twice
is harmless rather than duplicating markup.

    python scripts/install_skin.py ~/Downloads/roblox.jarvisskin.zip
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC = REPO_ROOT / "jarvis" / "hud" / "static"
INDEX_HTML = STATIC / "index.html"
THEMES_JS = STATIC / "themes.js"


def load(zip_path: Path) -> tuple[dict, str, str, str]:
    with zipfile.ZipFile(zip_path) as z:
        manifest = json.loads(z.read("manifest.json"))
        markup = z.read("markup.html").decode("utf-8")
        css = z.read(manifest["css_file"]).decode("utf-8")
        js = z.read(manifest["js_file"]).decode("utf-8")
    return manifest, markup, css, js


def add_fonts(html: str, fragments: list[str]) -> str:
    m = re.search(r'(<link href="https://fonts\.googleapis\.com/css2\?)([^"]*)("[^>]*>)', html)
    if not m:
        raise SystemExit("couldn't find the Google Fonts <link> in index.html")
    existing = m.group(2)
    missing = [f for f in fragments if f not in existing]
    if not missing:
        return html
    new_query = existing.rstrip("&") + "".join(f"&{f}" for f in missing)
    return html[: m.start(2)] + new_query + html[m.end(2) :]


def add_asset_tags(html: str, skin_id: str, css_file: str, js_file: str) -> str:
    link = f'<link rel="stylesheet" href="/static/{css_file}">\n'
    if link in html:
        return html
    html = html.replace("</head>", link + "</head>", 1)

    script = f'  <script src="/static/{js_file}"></script>\n'
    if script.strip() in html:
        return html
    marker = '  <script src="/static/themes.js"></script>\n'
    if marker not in html:
        raise SystemExit("couldn't find themes.js's <script> tag in index.html")
    return html.replace(marker, script + marker, 1)


def add_markup(html: str, skin_id: str, markup: str) -> str:
    if f'data-skin="{skin_id}"' in html:
        return html
    empty = "<div id=\"skins-inner\"></div>"
    if empty in html:
        return html.replace(empty, f'<div id="skins-inner">\n{markup}\n    </div>', 1)
    marker = '    <div id="skins-flash"></div>'
    if marker not in html:
        raise SystemExit("couldn't find #skins-inner or #skins-flash in index.html")
    return html.replace(marker, f"{markup}\n{marker}", 1)


def patch_themes_js(text: str, skin_id: str, global_name: str) -> str:
    if f"'{skin_id}'" in text or f'"{skin_id}"' in text:
        return text

    order_m = re.search(r"var ORDER = \[(.*?)\];", text, re.S)
    if not order_m:
        raise SystemExit("couldn't find `var ORDER = [...]` in themes.js")
    new_order = order_m.group(1).rstrip() + f", '{skin_id}'"
    text = text[: order_m.start(1)] + new_order + text[order_m.end(1) :]

    registry_m = re.search(r"(function registry\(\) \{\s*return \{)(.*?)(\};\s*\})", text, re.S)
    if not registry_m:
        raise SystemExit("couldn't find registry()'s return block in themes.js")
    body = registry_m.group(2).rstrip()
    sep = "," if not body.rstrip().endswith(",") and body.strip() else ""
    new_body = body + f"{sep}\n      {skin_id}: global.{global_name}\n    "
    text = text[: registry_m.start(2)] + new_body + text[registry_m.end(2) :]
    return text


def install(zip_path: Path) -> None:
    manifest, markup, css, js = load(zip_path)
    skin_id = manifest["id"]

    if not INDEX_HTML.exists() or not THEMES_JS.exists():
        raise SystemExit("run this from a JARVIS clone (jarvis/hud/static/index.html not found)")

    html = INDEX_HTML.read_text(encoding="utf-8")
    if f'data-skin="{skin_id}"' in html:
        print(f"'{skin_id}' looks already installed — nothing to do.")
        return

    (STATIC / manifest["css_file"]).write_text(css, encoding="utf-8")
    (STATIC / manifest["js_file"]).write_text(js, encoding="utf-8")

    html = add_fonts(html, manifest.get("google_fonts", []))
    html = add_asset_tags(html, skin_id, manifest["css_file"], manifest["js_file"])
    html = add_markup(html, skin_id, markup)
    INDEX_HTML.write_text(html, encoding="utf-8")

    themes_text = THEMES_JS.read_text(encoding="utf-8")
    themes_text = patch_themes_js(themes_text, skin_id, manifest["global_name"])
    THEMES_JS.write_text(themes_text, encoding="utf-8")

    print(f"installed '{skin_id}'. Restart `make run` (or reload the HUD) to see it.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("zip_path", type=Path)
    args = parser.parse_args()
    if not args.zip_path.exists():
        raise SystemExit(f"no such file: {args.zip_path}")
    install(args.zip_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
