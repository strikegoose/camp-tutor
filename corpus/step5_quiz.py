#!/usr/bin/env python3
"""Step5 题库与讲义生成器:只从 step2 知识卡片生成,禁止从清洗稿直接出题。

- quiz/<cid>.json:每课 ≥3 道选择题,source_card_ids/distractor_source 全部可解析到真实卡片;
  干扰项只许来自卡片中老师纠正过的错误说法或禁忌/决策卡反例,找不到则标 ["无"] 并注明
  「概念近邻辨析」;每课至少 1 题干扰项有真实卡片来源。本地硬校验,不过则修补重生成或丢弃计数。
- handout/<cid>.md:每课 1~2 页讲义,按时间组织卡片,每个知识点带 【HH:MM:SS】 视频时间点索引;
  LLM 产出校验不过时回退本地模板生成(保证时间点全部来自卡片 start_sec)。
- gen_stats.json:每课题数、卡片覆盖率、丢弃/修补统计。
幂等:LLM 走 lib.llm.chat 内容寻址缓存,重跑零成本。学科无关:卡片五型读 config/framework.yaml。"""
import json, re, sys, time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common, llm, transcript  # noqa: E402

import yaml  # noqa: E402

PART = "step5_quiz"
VERSION = "step5-20260826-1"
LLM_MAX_TOKENS = 8192      # thinking 型模型,output 预算给足
WORKERS = 5                # 课级并发(每课 2 次调用:题库 + 讲义)
QUESTIONS_TARGET = 5       # 每课让 LLM 出题数(校验淘汰后仍需 ≥3)
QUESTIONS_MIN = 3
HANDOUT_MIN_CHARS = 500    # 1 页底线(字)
TS_RE = re.compile(r"【(\d{2}):(\d{2}):(\d{2})】")

QUIZ_SYS = (
    "你是训练营课程的命题专家,严格依据给定的知识卡片出选择题,遵守干扰项纪律,"
    "只输出用户要求的 JSON 数组,不输出任何其他文字。"
)
HANDOUT_SYS = (
    "你是训练营课程的讲义编辑,严格依据给定的知识卡片编写学员讲义,"
    "只输出 Markdown 正文,不输出任何其他文字。"
)
NO_DISTRACTOR_NOTE = "干扰项为概念近邻辨析,非临床错误断言"


def ts_hms(sec):
    """秒 → HH:MM:SS(transcript.fmt_ts 去掉毫秒)。"""
    return transcript.fmt_ts(sec)[:-4]


def fmt_card(c):
    return (f"卡片 {c['card_id']}|{c['type']}|【{ts_hms(c['start_sec'])}】\n"
            f"内容:{c['content']}\n原文:{c['quote']}")


def build_quiz_prompt(title, camp_label, cards, feedback=None):
    cards_text = "\n\n".join(fmt_card(c) for c in cards)
    fb = ""
    if feedback:
        fb = ("\n\n上次输出未通过本地校验,问题如下,请全部修正后重新输出完整题目:\n- "
              + "\n- ".join(feedback))
    return f"""课程《{title}》({camp_label})。下面是该课的全部知识卡片(从逐字稿抽取,含卡片ID、五型、视频时间点、内容与口播原文)。

请只依据这些卡片出 {QUESTIONS_TARGET} 道单项选择题。硬性要求:
1. 每题知识点必须落在卡片上,source_card_ids 列 1~3 张支撑卡片的ID(只能用上面给出的卡片ID,不得编造)。
2. 四个选项 A/B/C/D,answer 为正确项字母;正确项内容必须被 source_card_ids 卡片直接支持。各题正确答案字母尽量分布均匀。
3. 干扰项纪律(重要):错误选项只许来自 (a) 卡片中老师课上纠正过的错误说法/学员误区,(b) 禁忌型或决策型卡片中的反例。
   - 满足时:distractor_source 填该来源卡片ID列表,distractor_note 说明"干扰项X来自哪张卡片的什么错误说法"。
   - 找不到合规来源时:distractor_source 填 ["无"],distractor_note 填「{NO_DISTRACTOR_NOTE}」,此时错误选项只能是与正确概念相邻但可明确区分的概念,不得自创临床错误断言。
   - {QUESTIONS_TARGET} 题中至少 1 题的 distractor_source 必须是真实卡片ID(优先用禁忌/决策卡,其次用事实/流程/运营卡中老师纠正过的说法)。
4. 解析:说明为什么选正确答案,并引用 source_card_ids 卡片的内容(可简述),让学员能回溯到卡片。
5. 题干具体、有考核点,不要"以下说法正确的是"配空泛选项;各题考点不重复。

只输出 JSON 数组,格式:
[{{"stem":"题干","options":{{"A":"...","B":"...","C":"...","D":"..."}},"answer":"B","解析":"...","source_card_ids":["..."],"distractor_source":["..."],"distractor_note":"..."}}]{fb}

知识卡片:
{cards_text}"""


def validate_questions(items, card_id_set, course_card_types):
    """本地硬校验。返回 (valid_questions, problems, discard_counter)。"""
    valid, problems = [], []
    discard = defaultdict(int)
    seen_stems = set()
    for i, q in enumerate(items or [], 1):
        if not isinstance(q, dict):
            discard["not_a_dict"] += 1
            continue
        stem = str(q.get("stem", "")).strip()
        opts = q.get("options") or {}
        answer = str(q.get("answer", "")).strip().upper()
        expl = str(q.get("解析", "")).strip()
        src = q.get("source_card_ids")
        ds = q.get("distractor_source")
        note = str(q.get("distractor_note", "")).strip()
        err = None
        if len(stem) < 8:
            err = "题干过短"
        elif not isinstance(opts, dict) or sorted(opts.keys()) != ["A", "B", "C", "D"]:
            err = "options 非 ABCD 四键"
        elif any(not str(v).strip() for v in opts.values()):
            err = "存在空选项"
        elif len({re.sub(r"\\s+", "", str(v)) for v in opts.values()}) < 4:
            err = "选项内容重复"
        elif answer not in ("A", "B", "C", "D"):
            err = f"answer 非法:{answer!r}"
        elif len(expl) < 10:
            err = "解析为空或过短"
        elif not isinstance(src, list) or not src or any(s not in card_id_set for s in src):
            err = "source_card_ids 含不存在的卡片ID"
        elif not isinstance(ds, list) or not ds:
            err = "distractor_source 缺失"
        elif ds != ["无"] and any(s not in card_id_set for s in ds):
            err = "distractor_source 含不存在的卡片ID"
        elif not note:
            err = "distractor_note 为空"
        elif stem in seen_stems:
            err = "题干重复"
        if err:
            problems.append(f"第{i}题:{err}")
            discard[err.split(":")[0]] += 1
            continue
        # 无来源干扰项:note 归一化,确保含「概念近邻辨析」标准说明(模型措辞/标点不一时补注)
        if ds == ["无"] and "概念近邻" not in note:
            note = f"{note};{NO_DISTRACTOR_NOTE}"
        seen_stems.add(stem)
        valid.append({
            "stem": stem,
            "options": {k: str(opts[k]).strip() for k in ("A", "B", "C", "D")},
            "answer": answer,
            "解析": expl,
            "source_card_ids": [str(s) for s in src],
            "distractor_source": [str(s) for s in ds],
            "distractor_note": note,
        })
    return valid, problems, discard


def has_real_distractor(q):
    return q["distractor_source"] != ["无"]


def gen_quiz(course, cards, logger):
    """单课题库:LLM 出题 → 本地校验 → 不达标修补一次。返回 (questions, course_stats)。"""
    cid, title = course["canonical_id"], course.get("title", "")
    camp_label = course.get("camp_label", "")
    card_id_set = {c["card_id"] for c in cards}
    card_types = {c["card_id"]: c["type"] for c in cards}
    st = {"llm_calls": 0, "repaired": False, "dropped": 0, "drop_detail": {},
          "problems": [], "llm_raw_counts": []}
    all_valid, feedback = [], None
    for attempt in (1, 2):
        prompt = build_quiz_prompt(title, camp_label, cards, feedback)
        text, _cached = llm.chat(PART, prompt, system=QUIZ_SYS, max_tokens=LLM_MAX_TOKENS)
        st["llm_calls"] += 1
        try:
            items = llm.parse_json(text)
            if isinstance(items, dict):
                items = items.get("questions", [])
            if not isinstance(items, list):
                items = []
            valid, batch_problems, batch_discard = validate_questions(
                items, card_id_set, card_types)
        except Exception as e:  # noqa: BLE001 - 解析失败按整批丢弃,进入修补
            items, valid = [], []
            batch_problems = [f"JSON 解析失败: {e!r}"]
            batch_discard = {"json_parse_error": QUESTIONS_TARGET}
        # 合并两轮的合格题(按题干去重)
        known = {q["stem"] for q in all_valid}
        for q in valid:
            if q["stem"] not in known:
                all_valid.append(q)
                known.add(q["stem"])
        st["llm_raw_counts"].append(len(items) if isinstance(items, list) else 0)
        st["dropped"] += sum(batch_discard.values())
        for k, v in batch_discard.items():
            st["drop_detail"][k] = st["drop_detail"].get(k, 0) + v
        st["problems"].extend(batch_problems)
        logger.log(f"{cid} 题库第{attempt}轮:产出 {st['llm_raw_counts'][-1]} 题,"
                   f"本轮合格 {len(valid)} 累计合格 {len(all_valid)}")
        enough = len(all_valid) >= QUESTIONS_MIN and any(has_real_distractor(q) for q in all_valid)
        if enough or attempt == 2:
            break
        st["repaired"] = True
        feedback = batch_problems[:8]
        if len(all_valid) < QUESTIONS_MIN:
            feedback.append(f"合格题不足 {QUESTIONS_MIN} 道,请补足")
        if not any(has_real_distractor(q) for q in all_valid):
            feedback.append("缺少 distractor_source 为真实卡片ID的题,至少 1 题必须满足")
    # 最终定额:最多 QUESTIONS_TARGET 题,优先保留真实干扰项题
    all_valid.sort(key=lambda q: (not has_real_distractor(q),))
    picked = all_valid[:QUESTIONS_TARGET]
    for seq, q in enumerate(picked, 1):
        q["qid"] = f"{cid}-Q{seq}"
    return picked, st


def build_handout_prompt(title, camp_label, cards):
    cards_text = "\n".join(
        f"【{ts_hms(c['start_sec'])}】[{c['type']}] {c['content']}" for c in cards)
    return f"""课程《{title}》({camp_label})。下面是该课按视频时间排序的全部知识卡片(每条以 【HH:MM:SS】 时间点开头)。

请据此编写一份学员复习讲义(Markdown),要求:
1. 结构:# 标题 + 一句课程导言 + 3~6 个 ## 小节(按内容主题或时间推进组织)。
2. 每个知识点都以对应卡片的 【HH:MM:SS】 时间点开头(照抄上面的时间点,不得编造、不得改格式),方便学员回看视频定位。
3. 医学/专业内容只写卡片里有的,不得补充卡片外的临床断言;可以把卡片口语表述改写为通顺书面语,但不得改变含义。
4. 禁忌/风险类内容用 > 引用块突出。篇幅 1~2 页(约 800~1600 字)。
5. 只输出 Markdown 正文。

知识卡片(按时间排序):
{cards_text}"""


def validate_handout(text, cards):
    """讲义校验:≥3 个时间点索引,且每个 【HH:MM:SS】 都能在卡片 start_sec(±5s)中找到。"""
    if not text or len(text) < HANDOUT_MIN_CHARS:
        return False, "篇幅不足"
    hits = [(int(h) * 3600 + int(m) * 60 + int(s)) for h, m, s in TS_RE.findall(text)]
    if len(hits) < 3:
        return False, f"时间点索引不足 3 个({len(hits)})"
    card_secs = [c["start_sec"] for c in cards]
    bad = [t for t in hits if not any(abs(t - cs) <= 5 for cs in card_secs)]
    if bad:
        return False, f"存在非卡片时间点:{[ts_hms(t) for t in bad[:3]]}"
    return True, ""


def fallback_handout(title, camp_label, cards, type_order):
    """本地模板兜底讲义:按五型分节,每条卡片一个带时间点的要点。时间点全部来自卡片。"""
    lines = [f"# 《{title}》复习讲义", "",
             f"> {camp_label}课程要点整理,由知识卡片自动生成;【HH:MM:SS】为视频时间点,可点击回看定位。", ""]
    by_type = defaultdict(list)
    for c in cards:
        by_type[c["type"]].append(c)
    section_names = {"事实": "核心事实", "流程": "操作流程", "决策": "临床决策",
                     "禁忌": "禁忌与风险", "运营": "运营要点"}
    for t in type_order:
        group = sorted(by_type.get(t, []), key=lambda c: c["start_sec"])
        if not group:
            continue
        lines.append(f"## {section_names.get(t, t)}")
        lines.append("")
        for c in group:
            body = f"【{ts_hms(c['start_sec'])}】{c['content']}"
            if t == "禁忌":
                lines.append(f"> {body}")
            else:
                lines.append(f"- {body}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def gen_handout(course, cards, type_order, logger):
    """单课讲义:LLM 生成 + 校验,失败回退本地模板。返回 (markdown, source, note)。"""
    cid, title = course["canonical_id"], course.get("title", "")
    camp_label = course.get("camp_label", "")
    prompt = build_handout_prompt(title, camp_label, cards)
    try:
        text, _cached = llm.chat(PART, prompt, system=HANDOUT_SYS, max_tokens=LLM_MAX_TOKENS)
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(markdown)?\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        ok, why = validate_handout(text, cards)
        if ok:
            return text + "\n", "llm", ""
        logger.log(f"{cid} 讲义 LLM 产出未过校验({why}),回退本地模板")
        return fallback_handout(title, camp_label, cards, type_order), "fallback", why
    except Exception as e:  # noqa: BLE001 - 讲义失败回退模板并 notify
        logger.log(f"{cid} 讲义 LLM 调用失败: {e!r},回退本地模板")
        common.notify("step5 讲义生成回退", f"{cid}: {e!r}")
        return fallback_handout(title, camp_label, cards, type_order), "fallback", repr(e)


def process_course(course, cards, type_order, logger):
    cid = course["canonical_id"]
    questions, qst = gen_quiz(course, cards, logger)
    handout, h_src, h_note = gen_handout(course, cards, type_order, logger)
    return cid, questions, qst, handout, h_src, h_note


def main():
    logger = common.StepLogger(PART)
    run_dir = common.get_run_dir()
    out_dir = run_dir / "step5"
    quiz_dir, hand_dir = out_dir / "quiz", out_dir / "handout"
    quiz_dir.mkdir(parents=True, exist_ok=True)
    hand_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        master = common.read_json(run_dir / "step0" / "courses_master.json")
        fw = yaml.safe_load((common.CONFIG / "framework.yaml").read_text(encoding="utf-8"))
        type_order = list(fw.get("card_types", []))
        cards_by_course = defaultdict(list)
        with open(run_dir / "step2" / "cards.jsonl", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    c = json.loads(line)
                    cards_by_course[c["course_id"]].append(c)
        for cid in cards_by_course:
            cards_by_course[cid].sort(key=lambda c: c["start_sec"])
        courses = [c for c in master if c["canonical_id"] in cards_by_course]
        skipped = [c["canonical_id"] for c in master if c["canonical_id"] not in cards_by_course]
        if skipped:
            logger.log(f"警告:{len(skipped)} 课无卡片,跳过: {skipped}")
            common.notify("step5 缺卡片课程", f"{len(skipped)} 课无卡片: {skipped}")
        logger.log(f"待生成 {len(courses)} 课,并发 {WORKERS}(题库+讲义各 1 次 LLM 调用/课)")

        results = {}
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(process_course, course, cards_by_course[course["canonical_id"]],
                              type_order, logger): course["canonical_id"] for course in courses}
            done = 0
            for fut in as_completed(futs):
                cid = futs[fut]
                done += 1
                try:
                    results[cid] = fut.result()
                except Exception as e:  # noqa: BLE001 - 单课失败可恢复:notify 后继续
                    logger.log(f"{cid} 处理失败: {e!r}")
                    common.notify("step5 单课处理失败", f"{cid}: {e!r}")
                    results[cid] = ("err", cid, repr(e))
                if done % 10 == 0:
                    logger.log(f"进度 {done}/{len(courses)}")

        # --- 主线程写盘与统计(按 master 顺序,保证确定性) ---
        stats = {"version": VERSION, "per_course": {}, "totals": {}}
        fail_courses = []
        for course in courses:
            cid = course["canonical_id"]
            r = results.get(cid)
            if not r or r[0] == "err":
                fail_courses.append(cid)
                stats["per_course"][cid] = {"error": r[2] if r else "no_result"}
                continue
            _cid, questions, qst, handout, h_src, h_note = r
            if len(questions) < QUESTIONS_MIN or not any(has_real_distractor(q) for q in questions):
                fail_courses.append(cid)
                common.notify("step5 题库不达标",
                              f"{cid}: 合格题 {len(questions)},真实干扰项题 "
                              f"{sum(has_real_distractor(q) for q in questions)}")
            common.write_json(quiz_dir / f"{cid}.json",
                              {"course_id": cid, "questions": questions})
            (hand_dir / f"{cid}.md").write_text(handout, encoding="utf-8")
            cards = cards_by_course[cid]
            used = {s for q in questions for s in q["source_card_ids"]}
            used |= {s for q in questions for s in q["distractor_source"] if s != "无"}
            stats["per_course"][cid] = {
                "questions": len(questions),
                "real_distractor_q": sum(has_real_distractor(q) for q in questions),
                "none_distractor_q": sum(not has_real_distractor(q) for q in questions),
                "cards_total": len(cards),
                "cards_covered": len(used),
                "card_coverage": round(len(used) / len(cards), 3) if cards else 0,
                "llm_calls": qst["llm_calls"] + 1,
                "repaired": qst["repaired"],
                "dropped": qst["dropped"],
                "drop_detail": qst["drop_detail"],
                "handout_source": h_src,
                "handout_note": h_note,
                "handout_chars": len(handout),
                "handout_ts_count": len(TS_RE.findall(handout)),
            }
            logger.log(f"{cid} 题 {len(questions)}(真实干扰项 {stats['per_course'][cid]['real_distractor_q']})"
                       f" 覆盖率 {stats['per_course'][cid]['card_coverage']:.0%} 讲义[{h_src}]")

        pc = stats["per_course"]
        ok_pc = [v for v in pc.values() if "error" not in v]
        stats["totals"] = {
            "courses": len(courses),
            "quiz_files": len(list(quiz_dir.glob('*.json'))),
            "handout_files": len(list(hand_dir.glob('*.md'))),
            "questions_total": sum(v["questions"] for v in ok_pc),
            "real_distractor_q_total": sum(v["real_distractor_q"] for v in ok_pc),
            "none_distractor_q_total": sum(v["none_distractor_q"] for v in ok_pc),
            "repaired_courses": sum(v["repaired"] for v in ok_pc),
            "dropped_total": sum(v["dropped"] for v in ok_pc),
            "card_coverage_avg": round(sum(v["card_coverage"] for v in ok_pc) / len(ok_pc), 3) if ok_pc else 0,
            "handout_llm": sum(v["handout_source"] == "llm" for v in ok_pc),
            "handout_fallback": sum(v["handout_source"] == "fallback" for v in ok_pc),
            "llm_calls_total": sum(v["llm_calls"] for v in ok_pc),
            "failed_courses": fail_courses,
            "elapsed_s": round(time.time() - t0, 1),
        }
        common.write_json(out_dir / "gen_stats.json", stats)
        logger.close(ok=not fail_courses, **{k: stats["totals"][k] for k in
                     ("questions_total", "repaired_courses", "dropped_total",
                      "handout_fallback", "failed_courses")})
        if fail_courses:
            raise RuntimeError(f"step5 存在不达标课程: {fail_courses}")
    except Exception as e:  # noqa: BLE001 - 顶层兜底:notify 后抛出
        common.notify("step5 题库/讲义生成失败", f"{e!r}")
        try:
            logger.close(ok=False, error=repr(e))
        except Exception:  # noqa: BLE001
            pass
        raise


if __name__ == "__main__":
    main()
