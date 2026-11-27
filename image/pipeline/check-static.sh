#!/bin/bash
# Run on the builder: the assurance suite's static tier for $VERSION, with the
# builder's own (read-only) rights. Results: /mnt/m7-check/results.json.
set -uo pipefail
: "${VERSION:?version}"; REGION="${SMH_REGION:-us-east-1}"; BUCKET=smh-poc-artifacts-${SMH_ACCOUNT:-111122223333}; SRC=/opt/smh/src
RUN=/mnt/m7-check; B=$RUN/build; rm -rf $RUN; mkdir -p $B
aws s3 cp --only-show-errors --region $REGION "s3://$BUCKET/build/out-$VERSION/summary.json" $B/summary.json
aws s3 cp --only-show-errors --region $REGION "s3://$BUCKET/build/out-$VERSION/ami-id.txt" $B/ami-id.txt
cp "/var/log/smh-deploy-$VERSION-build.log" $B/pipeline.log
SMH_PROFILE= python3 $SRC/assure/collect.py static $RUN "$(cut -d' ' -f1 $B/ami-id.txt)" $B
python3 $SRC/assure/report.py $RUN || true     # exit code 1 only says "not everything passed"; the fields say what
[ -s $RUN/results.json ]
