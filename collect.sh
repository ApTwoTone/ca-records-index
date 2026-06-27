#!/bin/bash
# collect.sh <run_id> [out_csv] -- download all shard artifacts, merge, report, place.
set -e
RUN_ID="$1"
REPO=ApTwoTone/ca-records-index
WORK=/tmp/ca-records-index
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="/Users/kai/Documents/Project Pure/outputs/netr_ain_fanout/${RUN_ID}"
LOCAL_OUT="${2:-$OUT_DIR/ain_docs_merged_${STAMP}.csv}"
SCAN_OUT="${SCAN_OUT:-$OUT_DIR/ain_scan_merged_${STAMP}.csv}"
SPARK_DIR="${SPARK_DIR:-/home/kai/Real-Estate-Work/analysis/netr_ain_fanout/${RUN_ID}}"

rm -rf "$WORK/artifacts" && mkdir -p "$WORK/artifacts"
gh run download "$RUN_ID" -R "$REPO" -D "$WORK/artifacts"
# flatten: all shard CSVs into one dir
mkdir -p "$WORK/shards_all"
find "$WORK/artifacts" -name '*.csv' -exec cp {} "$WORK/shards_all/" \;
echo "shard files: $(ls "$WORK/shards_all" | wc -l)"

mkdir -p "$(dirname "$LOCAL_OUT")"
if find "$WORK/artifacts" -name '*_docs.csv' | grep -q .; then
  python3 "$WORK/merge_ain_artifacts.py" "$WORK/artifacts" "$LOCAL_OUT" "$SCAN_OUT"
else
  python3 "$WORK/merge_and_report.py" "$WORK/shards_all" "$LOCAL_OUT"
fi
echo "=== scp to spark ==="
ssh -q spark "mkdir -p '$SPARK_DIR'"
scp -q "$LOCAL_OUT" "spark:$SPARK_DIR/$(basename "$LOCAL_OUT")"
if [ -f "$SCAN_OUT" ]; then
  scp -q "$SCAN_OUT" "spark:$SPARK_DIR/$(basename "$SCAN_OUT")"
fi
echo "scp OK -> spark:$SPARK_DIR/"
