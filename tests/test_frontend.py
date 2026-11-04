"""The front end has no build step, so nothing would catch a broken module until a browser loads it."""
import shutil
import subprocess

import pytest

from cts.paths import WEB_DIR

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node is not installed")
@pytest.mark.parametrize("script", sorted((WEB_DIR / "js").glob("*.js")), ids=lambda p: p.name)
def test_every_js_file_parses_as_an_es_module(script, tmp_path):
    # `node --check file.js` would treat it as a CommonJS script and miss module-level mistakes
    # (duplicate declarations, bad import/export syntax), so check a .mjs copy.
    copy = tmp_path / (script.stem + ".mjs")
    copy.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run([node, "--check", str(copy)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_element_id_used_by_the_scripts_exists_in_the_html():
    import re

    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([\w-]+)"', html))
    used = set()
    for script in (WEB_DIR / "js").glob("*.js"):
        used |= set(re.findall(r"\$\('#([\w-]+)'\)", script.read_text(encoding="utf-8")))
    assert not (used - ids), f"scripts reference missing elements: {sorted(used - ids)}"
