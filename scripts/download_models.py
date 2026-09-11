"""Download official SAM sources and weights into this repository only."""
import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAM_REVISION = "2b90b9f5ceec907a1c18123530e92e794ad901a4"
ASSETS = {
    "source": (f"https://codeload.github.com/facebookresearch/sam2/zip/{SAM_REVISION}", "sam2-source.zip"),
    "weights": ("https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt", "sam2.1_hiera_tiny.pt"),
}


def download(kind, direct=False):
    url, name = ASSETS[kind]
    directory = ROOT / "models"
    directory.mkdir(exist_ok=True)
    target = directory / name
    receipt = target.with_suffix(target.suffix + ".download.json")
    if target.exists() and receipt.exists():
        previous = json.loads(receipt.read_text(encoding="utf-8"))
        # Stream verification also works on Python 3.10 without file_digest.
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if previous.get("url") == url and digest.hexdigest() == previous.get("sha256"):
            return target
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if direct else urllib.request.build_opener()
    temporary = target.with_suffix(target.suffix + ".partial")
    print(f"Downloading {name} from official source...", flush=True)
    digest = hashlib.sha256()
    with opener.open(url, timeout=60) as response, temporary.open("wb") as stream:
        length = int(response.headers.get("Content-Length", "0"))
        size = 0
        for chunk in iter(lambda: response.read(1024 * 1024), b""):
            stream.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        if length and size != length:
            raise RuntimeError(f"Incomplete download: {size} / {length}")
    if size < 100_000:
        raise RuntimeError("Download is unexpectedly small; file was not installed.")
    temporary.replace(target)
    receipt.write_text(json.dumps({"url": url, "size": size, "sha256": digest.hexdigest(), "sam_revision": SAM_REVISION}, indent=2), encoding="utf-8")
    print(str(target), flush=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--direct", action="store_true", help="Use direct HTTPS for this process only, if a local proxy is unavailable")
    parser.add_argument("--asset", choices=["all", *ASSETS], default="all")
    args = parser.parse_args()
    for kind in ASSETS if args.asset == "all" else [args.asset]:
        download(kind, args.direct)
