#!/usr/bin/env python3
"""Fetch the TravelPlanner per-query reference data (``*_ref_info.jsonl``).

Source: Hugging Face dataset ``osunlp/TravelPlanner``, pinned to revision
``8736504ecfc31b7f8b7e40122873c337e83fff7c`` (lastModified 2024-07-14). Every
file is verified against a pinned SHA-256 after download, so a corrupted or
changed upstream file fails the build instead of silently changing solver
behavior.

The small ``background/citySet_with_states.txt`` file (city -> state, 312
entries, ~8 KB, from the official database.zip, sha256
a3d18b5c692857cd561cbb4ad8d221ac3eb6c47af7c787a3489e3ffa184ca4d6) is committed
in this repository instead of fetched: its only upstream is a Google Drive zip,
which is not reliably scriptable from CI/build environments.

Usage: python3 scripts/fetch_data.py [--out DIR]   (default: <repo>/data)
"""

import argparse
import hashlib
import os
import sys
import urllib.request

HF_REVISION = "8736504ecfc31b7f8b7e40122873c337e83fff7c"
BASE_URL = "https://huggingface.co/datasets/osunlp/TravelPlanner/resolve/{}/".format(HF_REVISION)

FILES = {
    "train_ref_info.jsonl": "f9eff8c9e3056c726c4bc46b8183a51efdf63760755b0183fa36f1c9f14b6e83",
    "validation_ref_info.jsonl": "0555348b457226a7cff866a7ec1962b60105fd90bb9e5c030b9e9ec3f2aff1d0",
    "test_ref_info.jsonl": "3f9e1c6b41be66831835778dd16d52a6bbde8fd1bf893a1b60f3a72712938d01",
}

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_one(name, expected_sha256, out_dir):
    dest = os.path.join(out_dir, name)
    if os.path.isfile(dest) and _sha256(dest) == expected_sha256:
        print("ok (cached) {}".format(name))
        return
    url = BASE_URL + name
    tmp = dest + ".tmp"
    print("downloading {} ...".format(url))
    urllib.request.urlretrieve(url, tmp)
    actual = _sha256(tmp)
    if actual != expected_sha256:
        os.remove(tmp)
        raise SystemExit("sha256 mismatch for {}: expected {}, got {}".format(name, expected_sha256, actual))
    os.replace(tmp, dest)
    print("ok {} ({} bytes, sha256 verified)".format(name, os.path.getsize(dest)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT, help="output directory (default: <repo>/data)")
    args = parser.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    for name, sha in FILES.items():
        fetch_one(name, sha, args.out)
    print("all reference data present in {}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
