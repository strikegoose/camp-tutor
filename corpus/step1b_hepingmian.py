#!/usr/bin/env python3
"""Step1b 「和平面」语境终审处置(补充指令A v2 / v3 口径):「和平面」不做自动替换——
一半是「X和平面」连词歧义,一半是「颌平面」误写。逐处 LLM 终审,误写做片段级修正,全部落处置记录。
学科无关:目标 canonical 从词典读取(variants 含「合平面」的词条),代码零硬编码。"""
import json, re, sys
from pathlib import Path

import yaml  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common, llm  # noqa: E402

TARGET = "和平面"
ANCHOR_VARIANT = "合平面"  # 锚点变体:其所属词条的 canonical 即「和平面」误写的修正目标
CTX = 60


def load_canon(run):
    """从运行目录词典(dict_v1.yaml)查锚点变体所属词条的 canonical。"""
    doc = yaml.safe_load((run / "step1" / "dict_v1.yaml").read_text(encoding="utf-8"))
    for t in doc["terms"]:
        if ANCHOR_VARIANT in (t.get("variants") or []):
            return t["canonical"]
    raise RuntimeError(f"词典中找不到锚点变体「{ANCHOR_VARIANT}」所属词条,无法确定修正目标")


def main():
    logger = common.StepLogger("step1b_hepingmian")
    run = common.get_run_dir()
    outdir = run / "step1b"
    outdir.mkdir(parents=True, exist_ok=True)
    cleaned_dir = run / "step1" / "cleaned"
    CANON = load_canon(run)
    logger.log(f"修正目标 canonical(词典锚点「{ANCHOR_VARIANT}」): {CANON}")

    # 1) 收集全部「和平面」出现处
    hits = []
    for f in sorted(cleaned_dir.glob("*.txt")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(re.escape(TARGET), text):
            hits.append({
                "course": f.stem, "pos": m.start(),
                "ctx": text[max(0, m.start() - CTX): m.end() + CTX].replace("\n", " "),
                "file": str(f),
            })
    logger.log(f"「{TARGET}」出现 {len(hits)} 处")
    if not hits:
        common.write_json(outdir / "disposition.json", {"total": 0, "kept": 0, "fixed": 0})
        (outdir / "hepingmian_disposition.md").write_text(
            f"# 「{TARGET}」语境终审处置记录\n\n- 语料中 0 处,无需处置。\n", encoding="utf-8")
        logger.close(ok=True, total=0)
        return

    # 2) LLM 逐处终审(一次调用)
    lines = [f"{i + 1}. [{h['course']}] …{h['ctx']}…" for i, h in enumerate(hits)]
    prompt = (
        f"以下是课程逐字稿中「{TARGET}」的全部出现处。逐条判断它是:\n"
        f"(a) 连词歧义——「X 和 平面」(「和」是连词,如「下颌和平面导板」中「和平面」非一个词);\n"
        f"(b) 术语误写——应为「{CANON}」(指咬合平面)。\n"
        "输出 JSON 数组:[{\"id\": 编号, \"verdict\": \"连词歧义\" 或 \"术语误写\", \"reason\": \"一句话\"}]\n"
        "拿不准一律判「连词歧义」(保守,不误改)。快速判定,直接输出 JSON,不要长篇推理。\n\n" + "\n".join(lines))
    text, _ = llm.chat("step1b_hepingmian", prompt, max_tokens=65536)
    verdicts = {v["id"]: v for v in llm.parse_json(text)}

    # 3) 处置:术语误写做片段级替换(按位置从后往前,防位移)
    kept, fixed = [], []
    by_file = {}
    for i, h in enumerate(hits, 1):
        v = verdicts.get(i, {"verdict": "连词歧义", "reason": "LLM 未返回,保守保留"})
        h.update(v)
        (fixed if v["verdict"] == "术语误写" else kept).append(h)
        if v["verdict"] == "术语误写":
            by_file.setdefault(h["file"], []).append(h)
    for fp, hs in by_file.items():
        p = Path(fp)
        text = p.read_text(encoding="utf-8")
        for h in sorted(hs, key=lambda x: -x["pos"]):
            assert text[h["pos"]: h["pos"] + len(TARGET)] == TARGET
            text = text[: h["pos"]] + CANON + text[h["pos"] + len(TARGET):]
        p.write_text(text, encoding="utf-8")
        logger.log(f"{Path(fp).stem}: 片段修正 {len(hs)} 处")

    # 4) 处置记录落盘(人读友好,每行可处置)
    md = [f"# 「{TARGET}」语境终审处置记录(v3 口径)", "",
          f"- 运行: {run.name}", f"- 总出现: {len(hits)} 处 | 术语误写→替换为「{CANON}」: {len(fixed)} 处 | 连词歧义→保留: {len(kept)} 处",
          "- 终审: DeepSeek V4 Flash 逐处判定(保守原则:拿不准判连词歧义)", "",
          "| # | 课程 | 上下文 | 判定 | 理由 | 处置 |",
          "|---|------|--------|------|------|------|"]
    for i, h in enumerate(hits, 1):
        act = f"替换为{CANON}" if h["verdict"] == "术语误写" else "保留原文"
        md.append(f"| {i} | {h['course']} | …{h['ctx']}… | {h['verdict']} | {h.get('reason','')} | {act} |")
    (outdir / "hepingmian_disposition.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    common.write_json(outdir / "disposition.json",
                      {"total": len(hits), "kept": len(kept), "fixed": len(fixed),
                       "items": [{k: h[k] for k in ("course", "pos", "verdict", "reason")} for h in hits]})
    logger.close(ok=True, total=len(hits), fixed=len(fixed), kept=len(kept))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        common.notify("camp-tutor step1b 失败", str(e)[:300])
        raise
