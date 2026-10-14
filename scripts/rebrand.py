"""Rename the product: update brand.json and every literal brand string in the tree.

    python scripts/rebrand.py LACE --subtitle "..." --tagline "..."
    python scripts/rebrand.py LACE --slug lace-app --replace ComfyUI-Meta-Viewer=lace --dry-run

Internals (env vars, storage keys, data folders, the app id) use the brand-independent codename
"mediastudio" and are deliberately left alone, so a rename never touches user data.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND_FILE = "brand.json"
# History and generated lockfiles keep the names they were written with.
SKIP_FILES = {"CHANGELOG.md", "poetry.lock", BRAND_FILE}
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".db", ".woff", ".woff2", ".ttf", ".otf", ".zip", ".mp4", ".svg",
}
# LLM prompt text carries only the neutral codename, and common words (a lowercase slug like "lace")
# appear in it as ordinary vocabulary, so it is never rewritten.
PROTECTED_PREFIXES = ("app/ai/prompting/content/",)
# The slug is a package/repo name, a common word elsewhere: rewrite it only where it names the package or the repo.
SLUG_FILES = {"package.json", "pyproject.toml"}
LOCKFILE = "package-lock.json"
LOCKFILE_HEAD_LINES = 12  # the package's own "name" fields sit here; dependency data is never rewritten


def default_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _word(value: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])")


def is_docs(rel: str) -> bool:
    return (rel.endswith(".md") and not rel.startswith("app/")) or rel.startswith("site/")


class Rules:
    """Replacement rules, grouped by how widely each may be applied."""

    def __init__(self, old: dict[str, str], new: dict[str, str], extra: list[tuple[str, str]]) -> None:
        self.literal = [(re.compile(re.escape(a)), b) for a, b in extra]
        # Long phrases first so a name inside the subtitle is not rewritten twice.
        for key in ("subtitle", "tagline"):
            if old[key] != new[key]:
                self.literal.append((re.compile(re.escape(old[key])), new[key]))
        self.name = [(_word(old["name"]), new["name"])] if old["name"] != new["name"] else []
        self.slug = [(_word(old["slug"]), new["slug"])] if old["slug"] != new["slug"] else []

    def for_file(self, rel: str) -> list[tuple[re.Pattern[str], str]]:
        if rel.startswith(PROTECTED_PREFIXES):
            return []
        rules = self.literal + self.name
        if rel in SLUG_FILES or rel == LOCKFILE or is_docs(rel):
            rules = rules + self.slug
        return rules


def tracked_files(root: Path) -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def apply_rules(text: str, rules: list[tuple[re.Pattern[str], str]]) -> str:
    for pattern, replacement in rules:
        text = pattern.sub(lambda _m, r=replacement: r, text)
    return text


def rebrand(root: Path, new_brand: dict[str, str], extra: list[tuple[str, str]] | None = None, *, dry_run: bool = False) -> list[str]:
    """Rewrite ``root`` to ``new_brand`` and return the changed relative paths."""
    old = json.loads((root / BRAND_FILE).read_text(encoding="utf-8"))
    ruleset = Rules(old, new_brand, extra or [])
    changed: list[str] = []
    for rel in tracked_files(root):
        if rel in SKIP_FILES or Path(rel).suffix.lower() in BINARY_SUFFIXES:
            continue
        path = root / rel
        rules = ruleset.for_file(rel)
        if not rules:
            continue
        try:
            text = path.read_bytes().decode("utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        if rel == LOCKFILE:
            lines = text.splitlines(keepends=True)
            for index, line in enumerate(lines[:LOCKFILE_HEAD_LINES]):
                if '"name":' in line:
                    lines[index] = apply_rules(line, rules)
            updated = "".join(lines)
        else:
            updated = apply_rules(text, rules)
        if updated != text:
            changed.append(rel)
            if not dry_run:
                path.write_bytes(updated.encode("utf-8"))
    if not dry_run:
        (root / BRAND_FILE).write_text(json.dumps(new_brand, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="new product name, e.g. LACE")
    parser.add_argument("--slug", help="lowercase package/repo slug (default: derived from the name)")
    parser.add_argument("--subtitle", help="new subtitle (default: unchanged)")
    parser.add_argument("--tagline", help="new tagline (default: unchanged)")
    parser.add_argument("--replace", action="append", default=[], metavar="OLD=NEW",
                        help="extra literal replacement, e.g. the GitHub repo name after a repo rename")
    parser.add_argument("--no-assets", action="store_true", help="do not regenerate the README banner")
    parser.add_argument("--dry-run", action="store_true", help="list the files that would change and stop")
    parser.add_argument("--allow-dirty", action="store_true", help="run even with uncommitted changes")
    args = parser.parse_args(argv)

    if not args.allow_dirty and not args.dry_run:
        status = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        if status.strip():
            print("Working tree has uncommitted changes; commit them first or pass --allow-dirty.", file=sys.stderr)
            return 1
    old = json.loads((ROOT / BRAND_FILE).read_text(encoding="utf-8"))
    new = {
        "name": args.name,
        "slug": args.slug or default_slug(args.name),
        "subtitle": args.subtitle or old["subtitle"],
        "tagline": args.tagline or old["tagline"],
    }
    extra = []
    for item in args.replace:
        before, sep, after = item.partition("=")
        if not sep or not before:
            parser.error(f"--replace expects OLD=NEW, got {item!r}")
        extra.append((before, after))

    changed = rebrand(ROOT, new, extra, dry_run=args.dry_run)
    print(f"{'Would change' if args.dry_run else 'Changed'} {len(changed)} files: {old['name']} -> {new['name']}")
    for rel in changed:
        print(f"  {rel}")
    if not args.dry_run and not args.no_assets:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from brand_assets import (
            generate,
        )

        for path in generate(ROOT, new):
            print(f"  generated {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
