#!/bin/bash
# run_all.sh — 全量跑管道:新建 data/runs/<ts>/ 版本目录,按序执行部件 0→5。
# 用法: bash corpus/run_all.sh [--dry-run] [--from stepN]
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
# 确定性:固定 hash 种子,防 set/dict 迭代序在进程间漂移(2026-08-26 幂等返工)
export PYTHONHASHSEED=0

DRY_RUN=0
FROM_STEP=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --from) shift; FROM_STEP="${1:-0}" ;;
  esac
done

STEPS=(step0_reconcile step1_terms step1b_hepingmian step2_cards step3_frames step4_vector step5_quiz)

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

# 新建版本化运行目录。不变式(AIHQ 2026-08-27 核定):data/latest 只在 selftest 全绿后推进,
# 未全绿回滚原指向——发布态永远是已验证状态
RUN_TS=$(date '+%Y%m%d-%H%M%S')
RUN_DIR="$(pwd)/data/runs/$RUN_TS"
mkdir -p "$RUN_DIR"
export CAMP_TUTOR_RUN_DIR="$RUN_DIR"
PREV_LATEST=""
if [ -L data/latest ]; then PREV_LATEST=$(readlink data/latest); fi
echo "=== 运行目录: $RUN_DIR(原 latest: ${PREV_LATEST:-无})"

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

echo "--- 推进 latest 并跑 selftest 验证"
rm -f data/latest
ln -s "runs/$RUN_TS" data/latest
if bash corpus/selftest.sh; then
  echo "=== run_all 完成: $RUN_DIR(selftest 全绿,latest 已推进)"
else
  echo "!!! selftest 未全绿,latest 回滚至 ${PREV_LATEST:-空}" >&2
  rm -f data/latest
  if [ -n "$PREV_LATEST" ]; then ln -s "$PREV_LATEST" data/latest; fi
  exit 1
fi
