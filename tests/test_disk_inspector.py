"""Disk inspector logic tests (pure functions, no MCP transport)."""

import importlib.util
import sys
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "examples" / "disk_inspector_server.py"
)
spec = importlib.util.spec_from_file_location("disk_inspector_server", MODULE_PATH)
disk = importlib.util.module_from_spec(spec)
sys.modules["disk_inspector_server"] = disk
spec.loader.exec_module(disk)


@pytest.fixture
def jail(tmp_path, monkeypatch):
    monkeypatch.setenv("DISK_INSPECTOR_ROOT", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "big.bin").write_bytes(b"x" * 10_000)
    (downloads / "copy-a.txt").write_bytes(b"same content here")
    (downloads / "copy-b.txt").write_bytes(b"same content here")
    (downloads / "unique.txt").write_bytes(b"different content")
    sub = downloads / "archive"
    sub.mkdir()
    (sub / "copy-c.txt").write_bytes(b"same content here")
    (sub / "filler.bin").write_bytes(b"y" * 5000)
    return tmp_path


def test_folder_size(jail):
    result = disk.folder_size("Downloads")
    assert result["file_count"] == 6
    assert result["size_bytes"] == 10_000 + 5000 + 17 * 4
    assert result["truncated"] is False


def test_largest_items_attributes_to_top_level_children(jail):
    result = disk.largest_items("Downloads", top_n=3)
    names = [item["name"] for item in result["largest"]]
    assert names[0] == "big.bin"
    assert "archive" in names  # subfolder aggregated as one entry


def test_find_duplicates_by_content(jail):
    result = disk.find_duplicates("Downloads")
    assert result["duplicate_groups"] == 1
    group = result["groups"][0]
    assert group["copies"] == 3
    assert sorted(group["files"]) == ["archive/copy-c.txt", "copy-a.txt", "copy-b.txt"]
    assert result["wasted_gb"] == 0.0  # tiny files round to 0 GB


def test_jail_blocks_outside_paths(jail):
    with pytest.raises(ValueError, match="outside"):
        disk.folder_size("/etc")
    with pytest.raises(ValueError, match="outside"):
        disk.folder_size("../somewhere")


def test_missing_folder_rejected(jail):
    with pytest.raises(ValueError, match="not a folder"):
        disk.folder_size("NoSuchFolder")


@pytest.mark.parametrize(
    "phrase", ["download folder", "downloads", "Download", "DOWNLOADS directory"]
)
def test_human_phrasing_resolves(jail, phrase):
    result = disk.folder_size(phrase)
    # Case-insensitive: macOS resolves 'downloads' directly; on Linux the
    # fuzzy child-matching finds 'Downloads'. Same folder either way.
    assert result["folder"].lower().endswith("/downloads")
    assert result["file_count"] == 6
