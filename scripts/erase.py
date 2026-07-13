#!/usr/bin/env python3
"""Erase everything LLM OS knows about you.

    python scripts/erase.py            # asks first
    python scripts/erase.py --yes      # no prompt

Crypto-erasure covers the audit chain (rotate the salt), but the same
prompts and document bodies also live in plaintext stores that a hash chain
has no say over. This clears all of them in one act:

  memory_store/     episodic memory (ChromaDB)
  document_index/   embeddings of your files
  documents/        your source files
  conversations/    saved chat history
  approvals.json    pending/served tool approvals

…and rotates audit/.salt, so the tamper-evident log stays intact and
verifiable while its content becomes permanently unrecoverable.
"""

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_os import config  # noqa: E402


def _clear_dir(path: Path) -> int:
    """Delete everything inside a directory, keep the directory itself."""
    if not path.exists():
        return 0
    removed = 0
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    return removed


def erase_all(
    memory_dir: Path,
    document_index_dir: Path,
    documents_dir: Path,
    conversations_dir: Path,
    approvals_file: Path,
    audit_dir: Path,
) -> dict:
    """Clear every plaintext store and rotate the audit salt."""
    result = {
        "memory_store": _clear_dir(Path(memory_dir)),
        "document_index": _clear_dir(Path(document_index_dir)),
        "documents": _clear_dir(Path(documents_dir)),
        "conversations": _clear_dir(Path(conversations_dir)),
    }
    approvals_file = Path(approvals_file)
    if approvals_file.exists():
        approvals_file.unlink()
        result["approvals"] = 1
    salt = Path(audit_dir) / ".salt"
    result["salt_rotated"] = salt.exists()
    if salt.exists():
        salt.unlink()  # next AuditLog mints a fresh one; old commitments die
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = parser.parse_args()

    if not args.yes:
        print(__doc__)
        if input("Type 'erase' to permanently delete all of it: ").strip() != "erase":
            print("Aborted — nothing was deleted.")
            return 1

    result = erase_all(
        memory_dir=config.MEMORY_DIR,
        document_index_dir=config.DOCUMENT_INDEX_DIR,
        documents_dir=config.DOCUMENTS_DIR,
        conversations_dir=config.CONVERSATIONS_DIR,
        approvals_file=config.APPROVALS_FILE,
        audit_dir=config.AUDIT_DIR,
    )
    print("Erased:")
    for store, n in result.items():
        print(f"  {store}: {n}")
    print("\nThe audit log remains and still verifies; its content is now "
          "cryptographically unrecoverable (salt rotated).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
