#!/usr/bin/env python3
"""Download CDN-hosted markdown image URLs and rewrite them to local paths.

Example:
    python scripts/download_markdown_cdn_images.py linux-handbook.md \
        --images-dir assets/images --backup
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
from urllib.request import Request, urlopen


IMAGE_LINK_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<inside>[^\n)]+)\)")
KNOWN_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}


def looks_like_cdn(url: str, domains: list[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if domains:
        return any(host == d or host.endswith("." + d) for d in domains)
    return "cdn" in host


def split_inside(inside: str) -> Tuple[str, str]:
    parts = inside.strip().split(maxsplit=1)
    url = parts[0] if parts else ""
    remainder = " " + parts[1] if len(parts) > 1 else ""
    return url, remainder


def infer_extension(url: str, content_type: str) -> str:
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in KNOWN_EXTENSIONS:
        return path_ext
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
        if guessed:
            return guessed
    return ".img"


def build_unique_filename(url: str, candidate_name: str, existing: set[str]) -> str:
    name = Path(candidate_name).name or "image"
    stem = Path(name).stem or "image"
    suffix = Path(name).suffix
    if not suffix:
        suffix = ".img"

    final_name = f"{stem}{suffix}"
    if final_name not in existing:
        existing.add(final_name)
        return final_name

    short_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    final_name = f"{stem}-{short_hash}{suffix}"
    existing.add(final_name)
    return final_name


def download_file(url: str, destination: Path, timeout: int) -> Optional[str]:
    req = Request(url, headers={"User-Agent": "markdown-image-localizer/1.0"})
    with urlopen(req, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "")
        data = response.read()
    destination.write_bytes(data)
    return content_type


def rewrite_markdown(
    markdown_path: Path,
    images_dir: Path,
    domains: list[str],
    dry_run: bool,
    timeout: int,
) -> Tuple[int, int, int]:
    text = markdown_path.read_text(encoding="utf-8")
    downloaded = 0
    replaced = 0
    failed = 0

    images_dir.mkdir(parents=True, exist_ok=True)

    url_to_local: Dict[str, str] = {}
    reserved_names = {p.name for p in images_dir.iterdir() if p.is_file()}

    def replace_match(match: re.Match[str]) -> str:
        nonlocal downloaded, replaced, failed

        inside = match.group("inside")
        url, remainder = split_inside(inside)

        if not looks_like_cdn(url, domains):
            return match.group(0)

        if url in url_to_local:
            local_rel = url_to_local[url]
            replaced += 1
            return f"![{match.group('alt')}]({local_rel}{remainder})"

        parsed = urlparse(url)
        base_name = Path(parsed.path).name or "image"

        # We may need content-type to infer extension if URL has no image suffix.
        suffix = Path(base_name).suffix.lower()
        needs_extension = suffix not in KNOWN_EXTENSIONS

        candidate_name = base_name
        if needs_extension and "." not in base_name:
            candidate_name = "image"

        unique_name = build_unique_filename(url, candidate_name, reserved_names)
        local_abs = images_dir / unique_name

        try:
            content_type = ""
            if not dry_run:
                content_type = download_file(url, local_abs, timeout)
                if needs_extension:
                    final_ext = infer_extension(url, content_type)
                    if local_abs.suffix.lower() != final_ext:
                        renamed = local_abs.with_suffix(final_ext)
                        if renamed.name in reserved_names:
                            short_hash = hashlib.sha256(
                                url.encode("utf-8")
                            ).hexdigest()[:8]
                            renamed = renamed.with_name(
                                f"{renamed.stem}-{short_hash}{final_ext}"
                            )
                        local_abs.rename(renamed)
                        reserved_names.discard(unique_name)
                        reserved_names.add(renamed.name)
                        local_abs = renamed
                downloaded += 1
            else:
                downloaded += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[warn] Failed to download {url}: {exc}", file=sys.stderr)
            return match.group(0)

        local_rel = os.path.relpath(local_abs, start=markdown_path.parent).replace(
            os.sep, "/"
        )
        url_to_local[url] = local_rel
        replaced += 1
        return f"![{match.group('alt')}]({local_rel}{remainder})"

    new_text = IMAGE_LINK_RE.sub(replace_match, text)

    if not dry_run and new_text != text:
        markdown_path.write_text(new_text, encoding="utf-8")

    return downloaded, replaced, failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download CDN-hosted image links from markdown and rewrite URLs to local files."
    )
    parser.add_argument(
        "markdown_file", type=Path, help="Path to markdown file to process"
    )
    parser.add_argument(
        "--images-dir",
        type=Path,
        default=Path("images"),
        help="Directory where downloaded images will be stored (default: images)",
    )
    parser.add_argument(
        "--domain",
        action="append",
        default=[],
        help="Limit downloads to these hostnames (can be repeated). Default behavior matches hosts containing 'cdn'.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview actions without writing files"
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create a .bak backup of the markdown file",
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="HTTP timeout in seconds (default: 30)"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    markdown_path = args.markdown_file.resolve()
    if not markdown_path.exists():
        print(f"[error] Markdown file not found: {markdown_path}", file=sys.stderr)
        return 1

    images_dir = args.images_dir
    if not images_dir.is_absolute():
        images_dir = (markdown_path.parent / images_dir).resolve()

    if args.backup and not args.dry_run:
        backup_path = markdown_path.with_suffix(markdown_path.suffix + ".bak")
        backup_path.write_text(
            markdown_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        print(f"[info] Backup created: {backup_path}")

    downloaded, replaced, failed = rewrite_markdown(
        markdown_path=markdown_path,
        images_dir=images_dir,
        domains=[d.lower() for d in args.domain],
        dry_run=args.dry_run,
        timeout=args.timeout,
    )

    print(
        f"[done] Downloaded: {downloaded}, Replaced links: {replaced}, Failed: {failed}"
    )
    if args.dry_run:
        print("[info] Dry run: no files were written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
