#!/usr/bin/env python3
"""Deterministic Soft Hub package builder — mirrors soft-hub/scripts/build_plugin.py."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

IGNORED_PARTS = {"__pycache__", ".venv", ".git", ".DS_Store"}


def files_for(source: Path) -> list[Path]:
    files: list[Path] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if any(part in IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink запрещён: {relative}")
        if path.is_file() and relative.as_posix() != "hub.checksums.json":
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(source).as_posix())


def zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.create_system = 3
    return info


def build(source: Path, output: Path) -> Path:
    source = source.resolve()
    output = output.expanduser().resolve()
    if output == source or source in output.parents:
        raise ValueError("Выходной архив должен находиться вне source-каталога")
    manifest_path = source / "hub.plugin.json"
    if not manifest_path.is_file():
        raise ValueError("В source отсутствует hub.plugin.json")

    files = files_for(source)
    checksums: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    for path in files:
        name = unicodedata.normalize(
            "NFC", PurePosixPath(path.relative_to(source)).as_posix()
        )
        if name in payloads:
            raise ValueError(f"Пути конфликтуют после Unicode NFC normalization: {name}")
        payload = path.read_bytes()
        payloads[name] = payload
        checksums[name] = hashlib.sha256(payload).hexdigest()

    checksums_payload = (
        json.dumps(checksums, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with zipfile.ZipFile(temporary, "w") as archive:
        for name, payload in payloads.items():
            archive.writestr(zip_info(name), payload)
        archive.writestr(zip_info("hub.checksums.json"), checksums_payload)
    os.replace(temporary, output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Soft Hub plugin package")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = build(args.source, args.output)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
