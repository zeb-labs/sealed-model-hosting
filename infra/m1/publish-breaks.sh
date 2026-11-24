#!/usr/bin/env bash
# Register the two break images made by image/pipeline/build-breaks.sh and
# write their ids to <build dir>/breaks.json. They carry the SAME Secure Boot
# data as the real image. Delete them after the suite has run.
#   bash infra/m1/publish-breaks.sh evidence/m6/build-0.6.0
set -euo pipefail
BUILD="${1:?build dir}"; DIR="$(cd "$(dirname "$0")" && pwd)"
for b in b2 b3; do
  UEFI_DATA="$(cat "$BUILD/uefi-data.blob")" SIZE_GB=10 RAW=/mnt/m6/$b.raw bash "$DIR/publish-image.sh" "break-$b" > "$BUILD/publish-$b.log" 2>&1 \
    || { echo "REGISTRATION FAILED for $b"; tail -5 "$BUILD/publish-$b.log"; exit 1; }
done
python3 - "$BUILD" <<'PY'
import json, re, sys
b = sys.argv[1]; out = {}
for name, key in (("b2", "B2"), ("b3", "B3")):
    t = open(f"{b}/publish-{name}.log").read()
    out[key] = re.search(r"AMI (ami-[0-9a-f]+)", t).group(1); out[key + "_snapshot"] = re.search(r"SNAPSHOT (snap-[0-9a-f]+)", t).group(1)
json.dump(out, open(f"{b}/breaks.json", "w"), indent=1); print(out)
PY
