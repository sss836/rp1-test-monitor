"""Check both source trees before applying; never builds, deploys or starts services."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--motors", type=Path, required=True)
parser.add_argument("--inference", type=Path, required=True)
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()
root = Path(__file__).resolve().parent
manifest = json.loads((root / "manifest.json").read_text())
repos = {"roboparty_motors": args.motors, "roboparty_inference": args.inference}
for name, repo in repos.items():
    for path, expected in manifest["original_sha256"][name].items():
        file = repo / path
        actual = hashlib.sha256(file.read_bytes()).hexdigest() if file.exists() else None
        if actual != expected:
            raise SystemExit(f"Source mismatch: {name}/{path}; refusing to apply")
    subprocess.run(["git", "apply", "--check", str(root / f"{name}.patch")], cwd=repo, check=True)
print("Both source trees match; patch check passed.")
if args.apply:
    for name, repo in repos.items():
        subprocess.run(["git", "apply", str(root / f"{name}.patch")], cwd=repo, check=True)
    print("Patches applied to the specified checkouts; nothing built or started.")
