#!/bin/bash
# collect_doc_pages.sh <run_id> -- download doc-page artifacts, merge sidecars, send to Spark.
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ID="$1"
REPO="${REPO:-ApTwoTone/ca-records-index}"
WORK="${WORK:-/tmp/ca-records-index-doc-pages-collect}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="/Users/kai/Documents/Project Pure/outputs/netr_doc_pages/${RUN_ID}"
MERGED_OUT="${MERGED_OUT:-$OUT_DIR/merged_${STAMP}}"
SPARK_DIR="${SPARK_DIR:-/home/kai/Real-Estate-Work/analysis/netr_doc_pages/${RUN_ID}}"
COPY_IMAGES="${COPY_IMAGES:-0}"

if [ -z "$RUN_ID" ]; then
  echo "Usage: collect_doc_pages.sh <run_id>" >&2
  exit 2
fi

rm -rf "$WORK/artifacts"
mkdir -p "$WORK/artifacts" "$MERGED_OUT"
gh run download "$RUN_ID" -R "$REPO" -D "$WORK/artifacts"
python3 "$SCRIPT_DIR/merge_doc_page_artifacts.py" "$WORK/artifacts" "$MERGED_OUT"

echo "=== scp merged manifests/intelligence to spark ==="
ssh -q spark "mkdir -p '$SPARK_DIR/merged'"
scp -q "$MERGED_OUT"/* "spark:$SPARK_DIR/merged/"

if [ "$COPY_IMAGES" = "1" ]; then
  echo "=== rsync PNG page images to spark ==="
  ssh -q spark "mkdir -p '$SPARK_DIR/pages'"
  find "$WORK/artifacts" -name '*.png' -print0 | rsync -0av --files-from=- / "spark:$SPARK_DIR/pages/"
fi

echo "OK -> spark:$SPARK_DIR/"
