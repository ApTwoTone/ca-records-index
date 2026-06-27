#!/bin/bash
# collect.sh <run_id> [out_csv] -- download all shard artifacts, merge, report, place.
set -e
RUN_ID="$1"
REPO=ApTwoTone/ca-records-index
WORK=/tmp/ca-records-index
STAMP="$(date +%Y%m%d_%H%M%S)"
LOCAL_OUT="${2:-/Users/kai/Documents/Project Pure/outputs/netr_ain_fanout/${RUN_ID}/ain_docs_merged_${STAMP}.csv}"
SPARK_DIR="${SPARK_DIR:-/home/kai/Real-Estate-Work/analysis/netr_ain_fanout/${RUN_ID}}"

rm -rf "$WORK/artifacts" && mkdir -p "$WORK/artifacts"
gh run download "$RUN_ID" -R "$REPO" -D "$WORK/artifacts"
# flatten: all shard CSVs into one dir
mkdir -p "$WORK/shards_all"
find "$WORK/artifacts" -name '*.csv' -exec cp {} "$WORK/shards_all/" \;
echo "shard files: $(ls "$WORK/shards_all" | wc -l)"

mkdir -p "$(dirname "$LOCAL_OUT")"
python3 "$WORK/merge_and_report.py" "$WORK/shards_all" "$LOCAL_OUT"
echo "=== scp to spark ==="
ssh -q spark "mkdir -p '$SPARK_DIR'"
scp -q "$LOCAL_OUT" "spark:$SPARK_DIR/$(basename "$LOCAL_OUT")" && echo "scp OK -> spark:$SPARK_DIR/"
