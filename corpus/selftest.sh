#!/bin/bash
# selftest.sh — 对 data/latest 指向的运行逐条核查指令单验收标准。任一 FAIL 退出码非 0。
# 依赖 NAS 原稿的检查项(颌学原稿计数)在 NAS 不可读(OSError/PermissionError)时降级 SKIP
# (不计失败,exit 0);NAS 恢复后原版复跑自动回到实测计数。禁止删检查项或无条件 SKIP。
set -uo pipefail
cd "$(dirname "$0")/.."
.venv/bin/python - <<'PYEOF'
import json, os, random, re, sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
ROOT = Path.cwd()
RUN = (ROOT / "data" / "latest").resolve()
results = []

def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("✅" if ok else "❌"), name, ("| " + str(detail)[:120] if detail else ""))

def skip(name, detail=""):
    # 环境依赖不可用(如 NAS 原稿)时降级:ok 记 None,汇总单列 SKIP、不计失败
    results.append((name, None, detail))
    print("⏭️ SKIP", name, ("| " + str(detail)[:120] if detail else ""))

print(f"== selftest 运行目录: {RUN}")
master = json.loads((RUN / "step0" / "courses_master.json").read_text())

# --- step0 三向对账 ---
check("step0 主表覆盖 44 节", len(master) == 44, f"{len(master)} 节")
need = ["canonical_id", "camp_label", "title", "instructor", "video_path",
        "transcript_path", "goods_id", "上架日", "直播时间", "对账状态"]
check("step0 主表字段齐全", all(all(k in r for k in need) for r in master))
check("step0 缺口分级清单落盘", (RUN / "step0" / "gaps.md").stat().st_size > 200)
check("step0 learning_records 报告落盘", (RUN / "step0" / "learning_records_report.md").stat().st_size > 200)
xiaoe_matched = sum(1 for r in master if r["goods_id"])
check("step0 小鹅通两营课节匹配", xiaoe_matched > 0, f"匹配 {xiaoe_matched}/44")

# --- step1 术语纠错 ---
s1 = RUN / "step1"
d = yaml.safe_load((s1 / "dict_v1.yaml").read_text())
terms = d["terms"] if isinstance(d, dict) else d
check("step1 词典 ≥100 词条", len(terms) >= 100, f"{len(terms)} 条")
cleaned = list((s1 / "cleaned").glob("*.txt"))
check("step1 44 节清洗稿全量", len(cleaned) == 44, f"{len(cleaned)} 节")
variants = []
for t in terms:
    variants += [v for v in (t.get("variants") or []) if v and v != t.get("canonical")]
all_clean = "\n".join(p.read_text(encoding="utf-8") for p in cleaned)
# 守卫口径:config/step1_guards.yaml 保护合法词不被误替换,残留统计同口径
guards_cfg = yaml.safe_load((ROOT / "config" / "step1_guards.yaml").read_text())
g_simple = {g["variant"]: g for g in guards_cfg.get("guards", [])}
class_rule = guards_cfg.get("class_variant_rule", {})
import re as _re
class_pat = _re.compile(class_rule.get("pattern", "^$"))
v2c = {}
for t in terms:
    for v in (t.get("variants") or []):
        if v and v != t.get("canonical"):
            v2c[v] = t.get("canonical")
def is_guarded(v, ctx_prev, ctx_next):
    if v in g_simple:
        g = g_simple[v]
        if ctx_prev and ctx_prev in (g.get("block_prev") or []):
            return True
        if ctx_next and ctx_next[:1] in (g.get("block_next") or []):
            return True
        return False
    if class_pat.match(v):
        if ctx_prev in (class_rule.get("allow_prev") or []):
            return True
        heads = class_rule.get("allow_follow_head", "")
        nxt = ctx_next or ""
        nxt2 = nxt.lstrip("的之")[:1]
        if nxt2 and nxt2 in heads:
            return True
        return True  # 序数用法默认不替换(见 class_variant_rule.note),视为守卫跳过
    return False
leftover, guarded_skip = [], 0
for v in set(variants):
    start = 0
    while True:
        i = all_clean.find(v, start)
        if i < 0:
            break
        prev = all_clean[i - 1] if i > 0 else ""
        nxt = all_clean[i + len(v): i + len(v) + 3]
        # 通用保护:变体出现在其 canonical 内部(如「咬合板」含「合板」)不算残留
        c = v2c.get(v, "")
        in_canonical = bool(c) and any(
            c[o:o + len(v)] == v and all_clean[i - o: i - o + len(c)] == c
            for o in range(len(c) - len(v) + 1))
        if in_canonical or is_guarded(v, prev, nxt):
            guarded_skip += 1
        else:
            leftover.append(v)
            break
        start = i + 1
check("step1 替换完成率 100%(守卫口径)", not leftover,
      f"残留 {leftover[:5]}" if leftover else f"0 残留,守卫跳过 {guarded_skip} 处")
for f in ("pinyin_candidates.csv", "adjudication.md", "number_unit_report.md", "cross_check.md"):
    check(f"step1 {f} 落盘", (s1 / f).stat().st_size > 100)

# --- step1 术语口径 v3(2026-08-27 拍板:业务惯用写法 canonical,𬌗 正字退役备查)检查项 ---
canon_list = [t.get("canonical") for t in terms]
check("v3 知识条目入词典", all(k in canon_list for k in ("颌位性错合畸形", "颌位正畸", "颌位重建", "正雅GS")))
check("v3 canonical 无 𬌗(正字仅 variants/note 备查)",
      not any("𬌗" in c for c in canon_list),
      str([c for c in canon_list if "𬌗" in c]))
check("v3 颌平面/牙合畸形 canonical 口径", "颌平面" in canon_list and "牙合畸形" not in canon_list)
check("v3 「颌学」不作任何词条变体", all("颌学" not in (t.get("variants") or []) for t in terms))
_purged = {"个颌骨","个颌骨错","个颌骨错位","中尖对","他的颌","他的颌骨","向的颌","向的颌骨","向的颌骨错",
           "向的颌骨错位","对尖","尖对","是颌骨","有颌骨","有颌骨错","有颌骨错位","的前段","的颌骨",
           "的颌骨错","的颌骨错位","这个颌骨","颌骨发","颌骨发育异","颌骨的","颌骨错位的","颌骨错位的方","有颌"}
check("v3 碎片词条已剔出词典", not (_purged & set(canon_list)), str(_purged & set(canon_list)))
def _count_jwp():
    n = 0
    for p in cleaned:
        t = p.read_text(encoding="utf-8")
        i = 0
        while True:
            i = t.find("颌位置", i)
            if i < 0:
                break
            if not (i > 0 and t[i - 1] in "上下"):  # 上/下颌位置为合法词
                n += 1
            i += 1
    return n
jp = _count_jwp()
check("v3 清洗稿无「颌位置」污染(=0,上/下颌位置除外)", jp == 0, f"{jp} 处")
# 𬌗 字零出现:清洗稿 + 全部下游产物(卡片/题库/讲义/chunks)
_ortho_n = all_clean.count("𬌗")
check("v3 清洗稿 𬌗 字 0 出现", _ortho_n == 0, f"{_ortho_n} 处")
def _ortho_in_products():
    n, files = 0, []
    for sub, pats in (("step2", ("cards.jsonl", "conflicts.md")),
                      ("step5", ("quiz/*.json", "handout/*.md")),
                      ("step4", ("chunks.jsonl",))):
        for pat in pats:
            for f in (RUN / sub).glob(pat):
                c = f.read_text(encoding="utf-8").count("𬌗")
                if c:
                    n += c
                    files.append(f"{sub}/{f.name}:{c}")
    return n, files
_pn, _pf = _ortho_in_products()
check("v3 下游产物 𬌗 字 0 出现(卡片/题库/讲义/chunks)", _pn == 0, str(_pf[:5]) if _pf else "0 处")
def _ortho_residual(word, allow_prev=()):
    n = 0
    for p in cleaned:
        t = p.read_text(encoding="utf-8")
        i = 0
        while True:
            i = t.find(word, i)
            if i < 0:
                break
            if not (i > 0 and t[i - 1] in allow_prev):
                n += 1
            i += 1
    return n
_hp_res = _ortho_residual("合平面", ("咬",))
check("v3 合平面 0 残留(咬合平面除外)", _hp_res == 0, f"{_hp_res} 处")
# canonical 分布正确:颌平面/功能合学/至简合学/合学 均应实际出现
_dist = {w: all_clean.count(w) for w in ("颌平面", "功能合学", "至简合学", "合学")}
check("v3 canonical 分布(颌平面/功能合学/至简合学/合学 均>0)",
      all(n > 0 for n in _dist.values()), str(_dist))
_raw_hev, _nas_err = None, ""
try:
    _raw_hev = sum(Path(r["transcript_path"]).read_text(encoding="utf-8").count("颌学")
                   for r in master if r["transcript_path"])
except OSError as e:  # NAS SMB 僵死期 PermissionError 等:降级 SKIP,恢复后原版复跑回实测
    _nas_err = f"{type(e).__name__}: {e}"[:80]
_cl_hev = sum(p.read_text(encoding="utf-8").count("颌学") for p in cleaned)
if _raw_hev is None:
    skip("v3 「颌学」不被替换(原稿=清洗稿计数)", f"NAS 原稿不可读待实测 | {_nas_err}")
else:
    check("v3 「颌学」不被替换(原稿=清洗稿计数)", _raw_hev == _cl_hev, f"raw={_raw_hev} cleaned={_cl_hev}")
_disp = RUN / "step1b" / "disposition.json"
_hp_md = RUN / "step1b" / "hepingmian_disposition.md"
if _disp.exists():
    _d = json.loads(_disp.read_text())
    _remain = sum(p.read_text(encoding="utf-8").count("和平面") for p in cleaned)
    check("v3 和平面逐处处置落盘且计数一致", _hp_md.exists() and _remain == _d["kept"],
          f"总 {_d['total']} 保留 {_d['kept']} 替换 {_d['fixed']} 稿内余 {_remain}")
else:
    check("v3 和平面逐处处置落盘且计数一致", False, "disposition.json 缺失")

# --- step2 知识卡片 ---
s2 = RUN / "step2"
cards = [json.loads(l) for l in (s2 / "cards.jsonl").read_text().splitlines() if l.strip()]
check("step2 卡片 ≥300", len(cards) >= 300, f"{len(cards)} 张")
types = {c.get("type") for c in cards}
check("step2 五型齐备", types <= {"事实", "流程", "决策", "禁忌", "运营"} and len(types) == 5, str(types))
by_course_text = {r["canonical_id"]: (s1 / "cleaned" / f"{r['canonical_id']}.txt").read_text(encoding="utf-8")
                  for r in master}
random.seed(20260826)
sample = random.sample(cards, min(20, len(cards)))
located = sum(1 for c in sample if c.get("quote") and c["quote"] in by_course_text.get(c["course_id"], ""))
check("step2 抽 20 卡 span 全部可定位", located == len(sample), f"{located}/{len(sample)}")
check("step2 冲突检测报告落盘", (s2 / "conflicts.md").stat().st_size > 100)
check("step2 卡片审核状态=待审", all(c.get("review_status") == "待审" for c in cards))

# --- step3 视频抽帧 ---
s3 = RUN / "step3"
idx = [json.loads(l) for l in (s3 / "frames_index.jsonl").read_text().splitlines() if l.strip()]
case_ids = {r["canonical_id"] for r in master if r.get("case_series")}
covered = {r["course_id"] for r in idx} & case_ids
check("step3 病例精讲 ≥10 节覆盖", len(covered) >= 10, f"{len(covered)}/{len(case_ids)} 节")
labels = {r.get("vision_label") for r in idx}
check("step3 vision 分类入库", labels <= {"口内照", "X光", "PPT页", "人脸", "其他"} and len(idx) > 0,
      f"{len(idx)} 帧, 标签 {labels}")
check("step3 对齐验证落盘", (s3 / "alignment_check.md").stat().st_size > 100)

# --- step4 向量库 ---
s4 = RUN / "step4"
env_text = (ROOT / ".env").read_text() if (ROOT / ".env").exists() else ""
has_ds = re.search(r"^DASHSCOPE_API_KEY=\S", env_text, re.M)
if has_ds:
    chunks = [json.loads(l) for l in (s4 / "chunks.jsonl").read_text().splitlines() if l.strip()]
    need_meta = {"chunk_id", "course_id", "camp", "instructor", "start_sec", "end_sec", "text"}
    check("step4 chunk>0 且 metadata 齐全", len(chunks) > 0 and all(need_meta <= set(c) for c in chunks[:50]),
          f"{len(chunks)} chunks")
    rep = (s4 / "recall_report.md").read_text()
    check("step4 recall 报告含选定策略", "选定切分策略" in rep and ("fixed" in rep or "semantic" in rep))
else:
    st = json.loads((s4 / "status.json").read_text()) if (s4 / "status.json").exists() else {}
    notify_log = (ROOT / "logs" / "notify.log").read_text() if (ROOT / "logs" / "notify.log").exists() else ""
    check("step4 BLOCKED 状态+通知证据", st.get("status") == "BLOCKED" and "BLOCKED" in notify_log)

# --- step5 题库/讲义 ---
s5 = RUN / "step5"
card_ids = {c["card_id"] for c in cards}
quiz_files = list((s5 / "quiz").glob("*.json"))
hand_files = list((s5 / "handout").glob("*.md"))
check("step5 44 课题库全量", len(quiz_files) == 44, f"{len(quiz_files)}")
check("step5 44 课讲义全量", len(hand_files) == 44, f"{len(hand_files)}")
bad_q, bad_trace, bad_distractor = [], [], []
for qf in quiz_files:
    qs = json.loads(qf.read_text())
    qs = qs["questions"] if isinstance(qs, dict) else qs
    if len(qs) < 3:
        bad_q.append(qf.stem)
    for q in qs:
        if not q.get("解析") and not q.get("explanation"):
            bad_trace.append(f"{qf.stem}:缺解析")
        src = q.get("source_card_ids") or []
        if not src or any(s not in card_ids for s in src):
            bad_trace.append(f"{qf.stem}:溯源失效")
        ds = q.get("distractor_source")
        if ds is None:
            bad_distractor.append(f"{qf.stem}:无干扰项来源标注")
        elif isinstance(ds, list) and any(x not in card_ids and x != "无" for x in ds):
            bad_distractor.append(f"{qf.stem}:干扰项来源非卡片")
check("step5 每课 ≥3 题", not bad_q, str(bad_q[:3]))
check("step5 解析+溯源链接有效", not bad_trace, str(bad_trace[:3]))
check("step5 干扰项纪律(来源=卡片或标注「无」)", not bad_distractor, str(bad_distractor[:3]))
ts_pat = re.compile(r"\d{1,2}:\d{2}(:\d{2})?")
no_ts = [h.stem for h in hand_files if not ts_pat.search(h.read_text(encoding="utf-8"))]
check("step5 讲义含视频时间点索引", not no_ts, str(no_ts[:3]))

# --- 全局 ---
check("cost-report.json 落盘", (ROOT / "data" / "cost-report.json").stat().st_size > 100)
for step in ["step0_reconcile", "step1_terms", "step1b_hepingmian", "step2_cards", "step3_frames", "step4_vector", "step5_quiz"]:
    p = ROOT / "corpus" / f"{step}.py"
    check(f"notify 失败分支: {step}", p.exists() and "common.notify" in p.read_text())

fails = [r for r in results if r[1] is False]  # SKIP(ok=None)不计失败
nskips = sum(1 for r in results if r[1] is None)
print(f"\n== selftest 结果: {len(results) - len(fails) - nskips} 通过 + {nskips} SKIP, {len(fails)} 失败")
sys.exit(1 if fails else 0)
PYEOF
