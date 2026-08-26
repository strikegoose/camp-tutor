# camp-tutor · 训练营 AI 助教内容管道

学科无关的离线内容加工管道(指令单 20260826-01)。

## 快速开始

```bash
# 全量跑管道(新建 data/runs/<ts>/ 版本目录)
bash corpus/run_all.sh
# 干跑(只打印执行计划)
bash corpus/run_all.sh --dry-run
# 自检(对 data/latest 指向的运行)
bash corpus/selftest.sh
```

## 部件

| 步骤 | 脚本 | 产出(data/latest/stepN/) |
|---|---|---|
| 0 三向对账 | `corpus/step0_reconcile.py` | courses_master.csv/json、gaps.md、learning_records_report.md |
| 1 术语纠错 | `corpus/step1_terms.py` | dict_v1.yaml、pinyin_candidates.csv、adjudication.md、number_unit_report.md、cross_check.md、cleaned/(44 节清洗稿) |
| 2 知识卡片 | `corpus/step2_cards.py` | cards.jsonl(≥300,五型,span 可定位)、conflicts.md |
| 3 视频抽帧 | `corpus/step3_frames.py` | frames/、frames_index.jsonl、alignment_check.md |
| 4 向量库 | `corpus/step4_vector.py` | chunks.jsonl、vectors、BM25 索引、recall_report.md |
| 5 题库/讲义 | `corpus/step5_quiz.py` | quiz/(每课≥3 题)、handout/(每课 1–2 页) |

成本逐件统计:`data/cost-report.json`(`corpus/cost_report.py` 生成)。
