#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

EXPECTED_ASSETS = [
    "owndiff-darwin-arm64",
    "owndiff-darwin-x86_64",
    "owndiff-linux-arm64",
    "owndiff-linux-x86_64",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_sha256(text: str) -> str:
    digest = text.strip().split()[0].lower() if text.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("checksum file does not start with a valid SHA-256 digest")
    return digest


def auth_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "owndiff-release-verifier",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers=auth_headers())
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_bytes(url).decode("utf-8"))


def verify_release_dir(release_dir: Path, assets: list[str]) -> list[str]:
    findings = []
    for asset in assets:
        binary = release_dir / asset
        checksum = release_dir / f"{asset}.sha256"
        if not binary.is_file():
            findings.append(f"missing release asset: {asset}")
            continue
        if not checksum.is_file():
            findings.append(f"missing checksum sidecar: {asset}.sha256")
            continue
        try:
            expected = parse_sha256(checksum.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            findings.append(f"invalid checksum sidecar {asset}.sha256: {exc}")
            continue
        actual = sha256_file(binary)
        if actual != expected:
            findings.append(f"checksum mismatch for {asset}")
    return findings


def github_release_url(repo: str, tag: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repo must be owner/name")
    if tag == "latest":
        return f"https://api.github.com/repos/{repo}/releases/latest"
    if not re.fullmatch(r"v[0-9A-Za-z._-]+", tag):
        raise ValueError("tag must be latest or a v-prefixed release tag")
    return f"https://api.github.com/repos/{repo}/releases/tags/{tag}"


def verify_github_release(repo: str, tag: str, assets: list[str]) -> list[str]:
    release = fetch_json(github_release_url(repo, tag))
    by_name = {asset.get("name"): asset for asset in release.get("assets", []) if isinstance(asset, dict)}
    findings = []

    for asset in assets:
        binary = by_name.get(asset)
        checksum = by_name.get(f"{asset}.sha256")
        if binary is None:
            findings.append(f"missing release asset: {asset}")
            continue
        if checksum is None:
            findings.append(f"missing checksum sidecar: {asset}.sha256")
            continue

        try:
            checksum_text = fetch_bytes(str(checksum["browser_download_url"])).decode("utf-8")
            expected = parse_sha256(checksum_text)
        except (KeyError, OSError, ValueError, urllib.error.URLError) as exc:
            findings.append(f"could not verify checksum sidecar {asset}.sha256: {exc}")
            continue

        digest = str(binary.get("digest") or "")
        if digest.startswith("sha256:"):
            actual = digest.removeprefix("sha256:").lower()
        else:
            try:
                actual = hashlib.sha256(fetch_bytes(str(binary["browser_download_url"]))).hexdigest()
            except (KeyError, OSError, urllib.error.URLError) as exc:
                findings.append(f"could not verify release asset digest for {asset}: {exc}")
                continue

        if actual != expected:
            findings.append(f"checksum sidecar does not match release asset digest for {asset}")

    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify OwnDiff release binaries and SHA-256 sidecar assets.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--release-dir", help="Local directory containing release assets.")
    source.add_argument("--repo", help="GitHub repository in owner/name form.")
    parser.add_argument("--tag", default="latest", help="GitHub release tag. Default: latest.")
    parser.add_argument("--asset", action="append", dest="assets", help="Expected asset name. May be repeated.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assets = args.assets or EXPECTED_ASSETS
    try:
        findings = (
            verify_release_dir(Path(args.release_dir), assets)
            if args.release_dir
            else verify_github_release(str(args.repo), str(args.tag), assets)
        )
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        findings = [str(exc)]

    payload = {"ok": not findings, "assets": assets, "findings": findings}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"OwnDiff release asset verification: {'PASS' if payload['ok'] else 'FAIL'}")
        for finding in findings:
            print(f"ERROR: {finding}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
