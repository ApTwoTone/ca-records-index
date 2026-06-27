#!/bin/bash
# collect.sh <run_id> [out_csv] -- download all shard artifacts, merge, report, place.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="$1"
REPO=ApTwoTone/ca-records-index
WORK="${WORK:-/tmp/ca-records-index-collect}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="/Users/kai/Documents/Project Pure/outputs/netr_ain_fanout/${RUN_ID}"
LOCAL_OUT="${2:-$OUT_DIR/ain_docs_merged_${STAMP}.csv}"
SCAN_OUT="${SCAN_OUT:-$OUT_DIR/ain_scan_merged_${STAMP}.csv}"
SUMMARY_OUT="${SCAN_OUT%.csv}_summary.json"
UNFINISHED_OUT="${SCAN_OUT%.csv}_unfinished_ains.csv"
SPARK_DIR="${SPARK_DIR:-/home/kai/Real-Estate-Work/analysis/netr_ain_fanout/${RUN_ID}}"

rm -rf "$WORK/artifacts" && mkdir -p "$WORK/artifacts"
gh run download "$RUN_ID" -R "$REPO" -D "$WORK/artifacts"
# flatten: all shard CSVs into one dir
mkdir -p "$WORK/shards_all"
find "$WORK/artifacts" -name '*.csv' -exec cp {} "$WORK/shards_all/" \;
echo "shard files: $(ls "$WORK/shards_all" | wc -l)"

mkdir -p "$(dirname "$LOCAL_OUT")"
if find "$WORK/artifacts" -name '*_docs.csv' | grep -q .; then
  python3 "$SCRIPT_DIR/merge_ain_artifacts.py" "$WORK/artifacts" "$LOCAL_OUT" "$SCAN_OUT"
else
  python3 "$SCRIPT_DIR/merge_and_report.py" "$WORK/shards_all" "$LOCAL_OUT"
fi
echo "=== scp to spark ==="
ssh -q spark "mkdir -p '$SPARK_DIR'"
scp -q "$LOCAL_OUT" "spark:$SPARK_DIR/$(basename "$LOCAL_OUT")"
if [ -f "$SCAN_OUT" ]; then
  scp -q "$SCAN_OUT" "spark:$SPARK_DIR/$(basename "$SCAN_OUT")"
fi
if [ -f "$SUMMARY_OUT" ]; then
  scp -q "$SUMMARY_OUT" "spark:$SPARK_DIR/$(basename "$SUMMARY_OUT")"
fi
if [ -f "$UNFINISHED_OUT" ]; then
  scp -q "$UNFINISHED_OUT" "spark:$SPARK_DIR/$(basename "$UNFINISHED_OUT")"
fi
echo "scp OK -> spark:$SPARK_DIR/"
