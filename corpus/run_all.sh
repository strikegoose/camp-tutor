#!/bin/bash
# run_all.sh — 全量跑管道:新建 data/runs/<ts>/ 版本目录,按序执行部件 0→5。
# 用法: bash corpus/run_all.sh [--dry-run] [--from stepN]
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

DRY_RUN=0
FROM_STEP=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --from) shift; FROM_STEP="${1:-0}" ;;
  esac
done

STEPS=(step0_reconcile step1_terms step2_cards step3_frames step4_vector step5_quiz)

if [ "$DRY_RUN" = "1" ]; then
  echo "[dry-run] 将创建新运行目录 data/runs/<ts>/ 并按序执行:"
  for s in "${STEPS[@]}"; do
    if [ -f "corpus/${s}.py" ]; then st="就绪"; else st="缺脚本(跳过)"; fi
    echo "  - corpus/${s}.py  [$st]"
  done
  echo "  - corpus/cost_report.py (成本汇总)"
  echo "[dry-run] LLM 调用有内容寻址缓存(data/cache/llm/),重跑幂等"
  exit 0
fi

# 新建版本化运行目录
RUN_DIR=$($PY -c "import sys; sys.path.insert(0,'corpus'); from lib import common; print(common.new_run_dir())")
export CAMP_TUTOR_RUN_DIR="$RUN_DIR"
echo "=== 运行目录: $RUN_DIR"

i=0
for s in "${STEPS[@]}"; do
  i=$((i+1))
  if [ ! -f "corpus/${s}.py" ]; then
    echo "--- 跳过 ${s}(脚本不存在)"
    continue
  fi
  echo "--- [$(date '+%H:%M:%S')] 执行 ${s}"
  if ! $PY "corpus/${s}.py"; then
    echo "!!! ${s} 失败(已走 notify),继续后续部件" >&2
  fi
done

echo "--- 成本汇总"
$PY corpus/cost_report.py
echo "=== run_all 完成: $RUN_DIR"
