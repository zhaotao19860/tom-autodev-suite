from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_DESTINATIONS = [Path.home() / ".comate" / "skills", Path.home() / ".codex" / "skills"]


def install_links(
    source_root: Path | str,
    destinations: list[Path | str],
    skill_names: list[str],
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    source_root = Path(source_root).expanduser().resolve()
    actions: list[dict[str, Any]] = []
    for destination_root in destinations:
        destination_root = Path(destination_root).expanduser()
        for name in skill_names:
            source = source_root / name
            target = destination_root / name
            action = _action_for(source, target)
            actions.append({
                "action": action,
                "source": str(source),
                "target": str(target),
            })
            if action == "CREATE" and not dry_run:
                destination_root.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source, target_is_directory=True)
    return actions


def _action_for(source: Path, target: Path) -> str:
    if not source.is_dir():
        return "MISSING_SOURCE"
    if target.is_symlink():
        return "UNCHANGED" if target.resolve() == source.resolve() else "CONFLICT"
    if target.exists():
        return "CONFLICT"
    return "CREATE"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install-tom-autodev-links")
    parser.add_argument("--root", required=True)
    parser.add_argument("--destination", action="append", dest="destinations")
    parser.add_argument("--skill", action="append", dest="skills")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser()
    skills = args.skills or sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and (path.name.startswith("tom-") or path.name == "setup-tom-autodev")
    )
    destinations = [Path(item) for item in args.destinations] if args.destinations else DEFAULT_DESTINATIONS
    actions = install_links(root, destinations, skills, dry_run=args.dry_run)
    for action in actions:
        print(json.dumps(action, ensure_ascii=False, sort_keys=True))
    return 1 if any(item["action"] in {"CONFLICT", "MISSING_SOURCE"} for item in actions) else 0


if __name__ == "__main__":
    raise SystemExit(main())
