#!/usr/bin/env bash
# Pull the shared OHLC cache from S3 into ./data/ohlc.
#
# The cache is produced and refreshed daily by the jojo_quant project's
# download_ohlc.py updater (a cron job). feiyangyang is a read-only consumer:
# it syncs the latest snapshot from the shared S3 bucket on demand.
#
# Requires the AWS CLI with credentials that can read the bucket.
set -euo pipefail

S3_DIR="s3://staking-ledger-bpt/jojo_quant/ohlc/"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/ohlc"

mkdir -p "$DEST"
echo "Syncing $S3_DIR -> $DEST ..."
aws s3 sync "$S3_DIR" "$DEST"
echo "Done. Cache ready at $DEST"
