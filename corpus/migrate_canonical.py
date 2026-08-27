#!/usr/bin/env python3
"""下游产物 canonical 迁移工具(一次性口径迁移,幂等)。
用途:词典 canonical 口径变更(v3:𬌗 系正字退役为 variants)后,step1/step1b/step4 由管道
重跑再生产物;step2(卡片)/step5(题库/讲义)为 DeepSeek LLM 产物,全量重抽成本高且
本次变更为纯机械字词替换——同一映射同时作用于清洗稿与产物,quote 逐字可定位不变式保持。
映射来源:运行目录 dict_v1.yaml 中含退役字的 variant → 其 canonical(最长优先),代码零词表硬编码。
幂等:再跑替换数=0;每文件替换计数落日志;迁移后断言目标目录 0 残留。"""
import json, sys
from pathlib import Path

import yaml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common  # noqa: E402

PART = "migrate_canonical"
ORTHO_CHAR = "𬌗"  # U+2C317 正字(v3 口径退役,仅词典 variants/note 备查)
TARGET_STEPS = ("step2", "step5")  # LLM 产物目录;step1/1b/4 由管道重跑再生,不在此列
TEXT_SUFFIX = {".json", ".jsonl", ".md", ".txt"}


def build_map(run):
    doc = yaml.safe_load((run / "step1" / "dict_v1.yaml").read_text(encoding="utf-8"))
    m = {}
    for t in doc["terms"]:
        for v in t.get("variants") or []:
            if ORTHO_CHAR in v:
                m[v] = t["canonical"]
    if not m:
        raise RuntimeError("词典中未找到含退役字的 variant,映射为空——词典口径未就绪?")
    return dict(sorted(m.items(), key=lambda kv: -len(kv[0])))  # 最长优先


def main():
    logger = common.StepLogger(PART)
    run = common.get_run_dir()
    mapping = build_map(run)
    logger.log(f"迁移映射 {len(mapping)} 条(最长优先): " +
               ", ".join(f"{k}→{v}" for k, v in mapping.items()))
    total, per_file = 0, {}
    fallback_log = {}
    for step in TARGET_STEPS:
        d = run / step
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if f.suffix not in TEXT_SUFFIX or not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            if ORTHO_CHAR not in text:
                continue
            n_file = 0
            for v, c in mapping.items():
                n = text.count(v)
                if n:
                    text = text.replace(v, c)
                    n_file += n
            # 兜底:𬌗 为「咬合」语素正字(与「颌」骨义无歧义),LLM 产物中的词典外组合
            # (如 前伸𬌗/𬌗关系)一律转 合,逐处落日志并计入报告,供创始人裁决是否补入词典
            fallback = []
            while True:
                i = text.find(ORTHO_CHAR)
                if i < 0:
                    break
                fallback.append(text[max(0, i - 10): i + 10].replace("\n", " "))
                text = text[:i] + "合" + text[i + 1:]
            if fallback:
                n_file += len(fallback)
                for ctx in fallback:
                    logger.log(f"  兜底 𬌗→合 [{f.name}]: …{ctx}…")
                fallback_log[str(f.relative_to(run))] = fallback
            f.write_text(text, encoding="utf-8")
            per_file[str(f.relative_to(run))] = n_file
            total += n_file
    logger.log(f"迁移完成: {len(per_file)} 个文件,替换 {total} 处")
    for fp, n in per_file.items():
        logger.log(f"  {fp}: {n} 处")
    # 断言:目标目录零残留
    for step in TARGET_STEPS:
        d = run / step
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.suffix in TEXT_SUFFIX and f.is_file() and ORTHO_CHAR in f.read_text(encoding="utf-8"):
                raise RuntimeError(f"迁移后仍有残留: {f}")
    common.write_json(run / "migration_report.json",
                      {"part": PART, "run": run.name, "mapping": mapping,
                       "files": per_file, "total_replacements": total,
                       "fallback_ortho_to_he": fallback_log})
    logger.close(ok=True, files=len(per_file), replacements=total)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        common.notify("camp-tutor migrate_canonical 失败", str(e)[:300])
        raise
