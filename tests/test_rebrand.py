"""The product name is changeable in one command without touching internals or history."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rebrand = _load("rebrand")

BRAND = {"name": "ACME", "slug": "acme", "subtitle": "Old Subtitle Here", "tagline": "Old tagline."}
LOCK_DEPS = "\n".join(f'    "dep{i}": {{"integrity": "sha512-acme/acme+acme"}}' for i in range(20))


@pytest.fixture
def repo(tmp_path):
    files = {
        "brand.json": json.dumps(BRAND),
        "README.md": "# ACME\n\nOld Subtitle Here. Old tagline.\ncd acme\nunACMEd ACMEs acmeish\n",
        "templates/page.html": "<title>ACME</title><a>ACME</a>",
        "package-lock.json": '{\n  "name": "acme",\n  "packages": {\n    "": {"name": "acme"},\n' + LOCK_DEPS + "\n  }\n}\n",
        "CHANGELOG.md": "# History of ACME\n",
        "logo.png": "not text",
        "app/code.py": "acme = 'a common word in code'  # ACME\n",
        "app/ai/prompting/content/p.md": "A bride in ACME and acme, wearing ACME.\n",
    }
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _new(**overrides):
    return {**BRAND, "name": "LACE", "slug": "lace", **overrides}


def test_name_slug_subtitle_and_tagline_are_rewritten(repo):
    changed = rebrand.rebrand(repo, _new(subtitle="New Subtitle", tagline="New tagline."))

    readme = (repo / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# LACE\n\nNew Subtitle. New tagline.\ncd lace\n")
    assert (repo / "templates/page.html").read_text(encoding="utf-8") == "<title>LACE</title><a>LACE</a>"
    assert set(changed) == {"README.md", "templates/page.html", "package-lock.json", "app/code.py"}
    assert json.loads((repo / "brand.json").read_text(encoding="utf-8"))["name"] == "LACE"


def test_only_whole_words_are_replaced(repo):
    rebrand.rebrand(repo, _new())

    assert "unACMEd ACMEs acmeish" in (repo / "README.md").read_text(encoding="utf-8")


def test_history_binary_files_and_lockfile_dependencies_are_untouched(repo):
    rebrand.rebrand(repo, _new())

    assert (repo / "CHANGELOG.md").read_text(encoding="utf-8") == "# History of ACME\n"
    assert (repo / "logo.png").read_text(encoding="utf-8") == "not text"
    lock = (repo / "package-lock.json").read_text(encoding="utf-8")
    assert '"name": "lace"' in lock
    assert lock.count("sha512-acme/acme+acme") == 20


def test_dry_run_writes_nothing(repo):
    changed = rebrand.rebrand(repo, _new(), dry_run=True)

    assert "README.md" in changed
    assert "ACME" in (repo / "README.md").read_text(encoding="utf-8")
    assert json.loads((repo / "brand.json").read_text(encoding="utf-8"))["name"] == "ACME"


def test_extra_literal_replacements_cover_the_repo_name(repo):
    (repo / "README.md").write_text("git clone https://example.test/Old-Repo.git\n", encoding="utf-8")

    rebrand.rebrand(repo, _new(), [("Old-Repo", "lace")])

    assert (repo / "README.md").read_text(encoding="utf-8") == "git clone https://example.test/lace.git\n"


def test_default_slug_is_derived_from_the_name():
    assert rebrand.default_slug("Media Lab!") == "media-lab"


def test_the_committed_brand_matches_the_tree():
    brand = json.loads((SCRIPTS.parent / "brand.json").read_text(encoding="utf-8"))
    package = json.loads((SCRIPTS.parent / "package.json").read_text(encoding="utf-8"))
    assert package["name"] == brand["slug"]
    assert brand["name"] in (SCRIPTS.parent / "docs/README.md").read_text(encoding="utf-8")


def test_placeholder_assets_are_generated_from_the_brand(tmp_path):
    brand_assets = _load("brand_assets")
    (tmp_path / "desktop/assets").mkdir(parents=True)
    (tmp_path / "docs/assets").mkdir(parents=True)
    try:
        written = brand_assets.generate(tmp_path, BRAND)
    except FileNotFoundError:
        pytest.skip("no system font available")
    assert all(path.is_file() and path.stat().st_size > 0 for path in written)


def test_slug_is_not_applied_to_code_and_prompt_text_is_protected(repo):
    rebrand.rebrand(repo, _new())

    assert (repo / "app/code.py").read_text(encoding="utf-8") == "acme = 'a common word in code'  # LACE\n"
    prompt = (repo / "app/ai/prompting/content/p.md").read_text(encoding="utf-8")
    assert prompt == "A bride in ACME and acme, wearing ACME.\n"
