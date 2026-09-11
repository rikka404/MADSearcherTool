"""Recover this project's verified CUDA wheel from pip 23's legacy HTTP cache.

Used only after the observed old-pip dependency-resolution failure. Cache is read-only.
"""
import hashlib
import os
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "6f99d8459369cfd6661c2aee14787592fe50156a33faf9ef643ba04e42d6543f"
# Hash supplied by https://download.pytorch.org/whl/cu124/torch/ for this exact wheel.
DEST = ROOT / "artifacts" / "wheels" / "torch-2.5.1+cu124-cp310-cp310-win_amd64.whl"


def main():
    cache = Path(os.environ["LOCALAPPDATA"]) / "pip" / "cache" / "http"
    for path in cache.rglob("*"):
        if not path.is_file() or path.stat().st_size < 2_500_000_000:
            continue
        with path.open("rb") as source:
            prefix = source.read(26)
            if not prefix.startswith(b"cc=4,\x82\xa8response\x87\xa4body\xc6"):
                continue
            size = struct.unpack(">I", prefix[-4:])[0]
            if source.read(4) != b"PK\x03\x04":
                continue
            source.seek(26)
            DEST.parent.mkdir(parents=True, exist_ok=True)
            temporary = DEST.with_suffix(".partial")
            digest = hashlib.sha256()
            with temporary.open("wb") as output:
                remaining = size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError("Truncated cache entry")
                    digest.update(chunk)
                    output.write(chunk)
                    remaining -= len(chunk)
            if digest.hexdigest() != EXPECTED:
                raise RuntimeError("Cache wheel hash does not match the official PyTorch index")
            temporary.replace(DEST)
            print(f"Verified and recovered: {DEST}")
            return
    raise RuntimeError("No matching legacy cache entry; use the normal setup script")


if __name__ == "__main__":
    main()
