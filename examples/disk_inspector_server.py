"""Disk inspector MCP server: read-only filesystem analytics.

Answers the questions people actually ask ("how big is my Downloads
folder?", "what's eating my disk?", "do I have duplicate files?") with
real measurements instead of letting the model dress up whole-volume
stats as folder stats.

Safety model:
- strictly read-only (never writes, moves, or deletes)
- jailed to the user's home directory (override: DISK_INSPECTOR_ROOT)
- hard caps on entries walked and bytes hashed, with a `truncated`
  flag in results, so no query can run away or stall the kernel

Run standalone:  python examples/disk_inspector_server.py
"""

import hashlib
import os
from collections import defaultdict
from pathlib import Path

from mcp.server.fastmcp import FastMCP

MAX_ENTRIES = 50_000          # directory entries walked per request
MAX_HASH_FILE_BYTES = 200 * 2**20   # skip hashing files larger than 200MB
MAX_TOTAL_HASH_BYTES = 2 * 2**30    # stop hashing after 2GB total
PROBE_BYTES = 128 * 1024      # quick-hash probe size

mcp = FastMCP("disk-inspector")


def _root() -> Path:
    return Path(os.environ.get("DISK_INSPECTOR_ROOT", Path.home())).resolve()


def _resolve_folder(path: str) -> Path:
    """Resolve user input ('Downloads', 'download folder', '~/Documents',
    absolute path) to a real directory, refusing anything outside the
    jail root. Models pass human phrasing, so matching is forgiving:
    a trailing 'folder'/'directory' word is dropped and root children
    are matched case-insensitively, with/without a plural 's'."""
    root = _root()
    cleaned = path.strip().strip("/")
    for suffix in (" folder", " directory", " dir"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()

    candidate = Path(cleaned).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Refused: '{path}' is outside {root}.")

    if not candidate.is_dir() and candidate.parent == root:
        wanted = candidate.name.lower()
        try:
            for entry in root.iterdir():
                name = entry.name.lower()
                if entry.is_dir() and name in (wanted, wanted + "s"):
                    return entry.resolve()
        except OSError:
            pass
    if not candidate.is_dir():
        raise ValueError(f"'{path}' is not a folder under {root}.")
    return candidate


def _walk_files(folder: Path):
    """Yield (path, size) for regular files; skips symlinks and
    unreadable entries; bounded by MAX_ENTRIES."""
    seen = 0
    stack = [folder]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > MAX_ENTRIES:
                        yield None, None  # truncation sentinel
                        return
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path), entry.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue


def _gb(size: int) -> float:
    return round(size / 2**30, 2)


def folder_size(path: str) -> dict:
    folder = _resolve_folder(path)
    total, files, truncated = 0, 0, False
    for file_path, size in _walk_files(folder):
        if file_path is None:
            truncated = True
            break
        total += size
        files += 1
    return {
        "folder": str(folder),
        "size_gb": _gb(total),
        "size_bytes": total,
        "file_count": files,
        "truncated": truncated,
    }


def largest_items(path: str, top_n: int = 10) -> dict:
    folder = _resolve_folder(path)
    top_n = max(1, min(int(top_n), 25))
    sizes = defaultdict(int)
    truncated = False
    for file_path, size in _walk_files(folder):
        if file_path is None:
            truncated = True
            break
        relative = file_path.relative_to(folder)
        # Attribute the file to its top-level child of the folder.
        child = relative.parts[0] if len(relative.parts) > 1 else relative.name
        sizes[child] += size
    ranked = sorted(sizes.items(), key=lambda kv: -kv[1])[:top_n]
    return {
        "folder": str(folder),
        "largest": [{"name": name, "size_gb": _gb(size)} for name, size in ranked],
        "truncated": truncated,
    }


def find_duplicates(path: str) -> dict:
    folder = _resolve_folder(path)
    by_size = defaultdict(list)
    truncated = False
    for file_path, size in _walk_files(folder):
        if file_path is None:
            truncated = True
            break
        if 0 < size <= MAX_HASH_FILE_BYTES:
            by_size[size].append(file_path)

    hashed_bytes = 0
    groups = []
    wasted = 0
    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        # Two-stage: cheap probe hash first, full hash only on probe ties.
        probes = defaultdict(list)
        for file_path in candidates:
            digest = _hash_file(file_path, PROBE_BYTES)
            if digest:
                probes[digest].append(file_path)
        for tied in probes.values():
            if len(tied) < 2:
                continue
            full = defaultdict(list)
            for file_path in tied:
                if hashed_bytes >= MAX_TOTAL_HASH_BYTES:
                    truncated = True
                    break
                digest = _hash_file(file_path, None)
                hashed_bytes += size
                if digest:
                    full[digest].append(file_path)
            for paths in full.values():
                if len(paths) >= 2:
                    groups.append(
                        {
                            "size_mb": round(size / 2**20, 2),
                            "copies": len(paths),
                            "files": [str(p.relative_to(folder)) for p in paths],
                        }
                    )
                    wasted += size * (len(paths) - 1)
    groups.sort(key=lambda g: -(g["size_mb"] * (g["copies"] - 1)))
    return {
        "folder": str(folder),
        "duplicate_groups": len(groups),
        "wasted_gb": _gb(wasted),
        "groups": groups[:20],
        "truncated": truncated,
    }


def _hash_file(path: Path, limit) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as f:
            if limit is not None:
                digest.update(f.read(limit))
            else:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


@mcp.tool()
def get_folder_size(path: str) -> dict:
    """Measure the total size and file count of a specific folder, e.g.
    'Downloads' or 'Documents/projects'. Use for questions about how
    big a folder or directory is (NOT for whole-disk free space)."""
    return folder_size(path)


@mcp.tool()
def get_largest_items(path: str, top_n: int = 10) -> dict:
    """Break down which files and subfolders use the most space inside
    a folder. Use for questions like 'what is taking up space in X?'"""
    return largest_items(path, top_n)


@mcp.tool()
def find_duplicate_files(path: str) -> dict:
    """Find duplicate files inside a folder by content hash. Reports
    groups of identical files and how much space the extra copies
    waste. Use for questions about duplicate or repeated files."""
    return find_duplicates(path)


if __name__ == "__main__":
    mcp.run()
