#!/usr/bin/env python3
"""Step2 知识卡片库:按课分段 LLM 抽取五型卡片 → 本地硬校验(quote 逐字可定位、
时间戳落在块区间)→ cards.jsonl(≥300,review_status=待审)。
随后跨课同主题聚类 + LLM 判定相反断言 → conflicts.md;统计落 extract_stats.json。
学科无关:五型/四维框架读 config/framework.yaml;幂等靠 llm.py 内容寻址缓存。"""
import json, re, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common, llm, transcript  # noqa: E402

import yaml  # noqa: E402

PART = "step2_cards"
VERSION = "step2-20260826-1"
SEG_CHARS = 4000          # 每段约 4000 字(按 Speaker 块整段切)
CARDS_PER_SEG = 3         # 每段最多抽取卡片数
TARGET_MIN, TARGET_MAX = 7, 12   # 每课目标卡片数区间
QUOTE_MIN, QUOTE_MAX = 30, 150   # quote 长度(字)
LLM_MAX_TOKENS = 8192     # thinking 型模型,output 预算给足
CONFLICT_BATCH = 15       # 每批送 LLM 判定的候选冲突对数
WORKERS = 6               # 段级并发(实测单次调用 ~74s,串行 231 段超 2h 时限,故段级并发)

SPK_LINE_RE = re.compile(r"^Speaker\s+\d+\s+\d{2}:\d{2}:\d{2}\.\d{3}\s*$", re.M)

EXTRACT_SYS = (
    "你是训练营课程逐字稿的知识卡片抽取专家。从口播原文中抽取可独立成立的知识卡片,"
    "严格按用户要求的 JSON 数组输出,不输出任何其他文字。"
)


def build_prompt(title, camp_label, card_types, seg_text):
    types_desc = (
        "事实(客观知识/定义/数据)、流程(操作步骤/先后顺序)、"
        "决策(什么情况下选择什么方案/工具)、禁忌(不可做/必须避免/风险警示)、"
        "运营(门诊运营/医患沟通/团队管理)"
    )
    return f"""课程《{title}》({camp_label})。下面是该课逐字稿的一个片段,含 "Speaker N HH:MM:SS.mmm" 时间戳行,其余为口播原文。

请从片段中抽取最有价值的知识卡片,最多 {CARDS_PER_SEG} 张。卡片五型:{types_desc}。
要求:
1. type 只能是:{"、".join(card_types)} 之一。
2. content:卡片正文,1-3 句,断言式(可直接判断对错的陈述),不要出现"讲师说""本课"之类指代。
3. quote:从上面口播原文中【逐字复制】的一段连续片段,30~150 字,作为卡片证据。绝对不许改写、增删、修正任何一个字或标点;不要包含 "Speaker" 时间戳行;不要跨块拼接。
4. start_sec:quote 所在 Speaker 块的开始时间(秒,数值)。
5. 只抽原文明确支持的内容,抽不到就返回空数组,不要编造。同一片段内不要出含义重复的卡片。

只输出 JSON 数组,格式:[{{"type":"事实","content":"...","quote":"...","start_sec":123.4}}]

逐字稿片段:
{seg_text}"""


# ---------- 逐字稿切块与定位 ----------

def segment_blocks(blocks, seg_chars=SEG_CHARS):
    """按 Speaker 块整段切 ~seg_chars 字段。返回 [(seg_text, [block_idx...])]。"""
    segs, cur_text, cur_idx = [], [], []
    for i, b in enumerate(blocks):
        head = f"Speaker {b.speaker} {transcript.fmt_ts(b.start_sec)} "
        piece = head + "\n" + b.text
        if cur_idx and sum(len(p) for p in cur_text) + len(piece) > seg_chars:
            segs.append(("\n\n".join(cur_text), cur_idx))
            cur_text, cur_idx = [], []
        cur_text.append(piece)
        cur_idx.append(i)
    if cur_idx:
        segs.append(("\n\n".join(cur_text), cur_idx))
    return segs


def build_norm_index(raw_text):
    """去空白归一化索引:norm 串 + norm 下标 → raw 下标映射。"""
    chars, pos = [], []
    for i, ch in enumerate(raw_text):
        if not ch.isspace():
            chars.append(ch)
            pos.append(i)
    return "".join(chars), pos


def locate_quote(raw_text, norm, pos, quote):
    """quote(允许首尾空白/空白差异)在清洗稿全文中定位,返回原文逐字 span。"""
    q = re.sub(r"\s+", "", quote or "")
    if not q:
        return None
    idx = norm.find(q)
    if idx < 0:
        return None
    raw_start, raw_end = pos[idx], pos[idx + len(q) - 1] + 1
    return raw_text[raw_start:raw_end].strip()


def block_spans(raw_text, blocks):
    """每个 Speaker 块在原文中的文本区间 [(text_start, text_end)](不含时间戳行)。"""
    matches = list(SPK_LINE_RE.finditer(raw_text))
    spans = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        spans.append((start, end))
    return spans


def find_block_of(raw_pos, spans):
    for i, (s, e) in enumerate(spans):
        if s <= raw_pos < e:
            return i
    return None


# ---------- 抽取与校验 ----------

def prepare_course(course):
    """读清洗稿,切块并预建定位索引。"""
    cid = course["canonical_id"]
    raw_text = (common.get_run_dir() / "step1" / "cleaned" / f"{cid}.txt").read_text(encoding="utf-8")
    t = transcript.parse(raw_text)
    norm, pos = build_norm_index(raw_text)
    return {
        "course": course, "raw_text": raw_text, "blocks": t.blocks,
        "segs": segment_blocks(t.blocks), "norm": norm, "pos": pos,
        "spans": block_spans(raw_text, t.blocks),
    }


def call_segment(prep, card_types, seg_text):
    """worker:单段 LLM 抽取,返回解析后的 items(失败抛异常由调用方记账)。"""
    course = prep["course"]
    prompt = build_prompt(course["title"], course.get("camp_label", ""), card_types, seg_text)
    text, _cached = llm.chat(PART, prompt, system=EXTRACT_SYS, max_tokens=LLM_MAX_TOKENS)
    items = llm.parse_json(text)
    if isinstance(items, dict):
        items = [items]
    return items


def validate_items(prep, card_types, items, discard):
    """主线程:逐条硬校验 + 汇总丢弃原因。返回合格候选卡。"""
    out = []
    for it in items:
        if not isinstance(it, dict):
            discard["bad_item"] += 1
            continue
        ok, reason, card = validate_card(it, prep["course"], card_types, prep["raw_text"],
                                         prep["norm"], prep["pos"], prep["spans"], prep["blocks"])
        if ok:
            out.append(card)
        else:
            discard[reason] += 1
    return out


def validate_card(it, course, card_types, raw_text, norm, pos, spans, blocks):
    """硬校验:类型合法、content 非空、quote 逐字可定位且 30~150 字、
    start_sec/end_sec 取 quote 所在块区间。返回 (ok, reason, card)。"""
    ctype = str(it.get("type", "")).strip()
    if ctype not in card_types:
        return False, "bad_type", None
    content = str(it.get("content", "")).strip()
    if len(content) < 8:
        return False, "bad_content", None
    span = locate_quote(raw_text, norm, pos, it.get("quote"))
    if span is None:
        return False, "quote_not_found", None
    if "Speaker" in span:
        return False, "quote_has_header", None
    if not (QUOTE_MIN <= len(span) <= QUOTE_MAX):
        return False, "quote_length", None
    raw_pos = raw_text.find(span)
    bi = find_block_of(raw_pos, spans)
    if bi is None:
        return False, "block_not_found", None
    start_sec = blocks[bi].start_sec
    if bi + 1 < len(blocks):
        end_sec = blocks[bi + 1].start_sec
    else:  # 末块:按语速估算
        end_sec = start_sec + max(len(blocks[bi].text) / 5.0, 5.0)
    card = {
        "course_id": course["canonical_id"],
        "camp": course.get("camp_label") or course.get("camp", ""),
        "instructor": course.get("instructor", ""),
        "type": ctype,
        "content": content,
        "quote": span,
        "start_sec": round(start_sec, 3),
        "end_sec": round(end_sec, 3),
        "review_status": "待审",
    }
    return True, "", card


def select_cards(candidates, card_types):
    """按五型轮转选取 TARGET_MAX 张,保证已有类型尽量覆盖。"""
    by_type = defaultdict(list)
    for c in candidates:
        by_type[c["type"]].append(c)
    picked, order = [], [t for t in card_types if by_type.get(t)]
    i = 0
    while len(picked) < TARGET_MAX and order:
        t = order[i % len(order)]
        if by_type[t]:
            picked.append(by_type[t].pop(0))
        else:
            order.remove(t)
            if not order:
                break
            continue
        i += 1
    return picked


# ---------- 冲突检测 ----------

def card_bigrams(text):
    return {text[i:i + 2] for i in range(len(text) - 1) if not text[i + 1].isspace()}


def find_candidate_pairs(cards):
    """跨课同主题候选对:内容 bigram 重合度足够高即同主题。"""
    grams = [card_bigrams(c["content"] + c["quote"][:60]) for c in cards]
    pairs = []
    for i in range(len(cards)):
        for j in range(i + 1, len(cards)):
            if cards[i]["course_id"] == cards[j]["course_id"]:
                continue
            inter = len(grams[i] & grams[j])
            union = len(grams[i] | grams[j]) or 1
            if inter >= 6 and inter / union >= 0.12:
                pairs.append((i, j))
    return pairs


CONFLICT_SYS = (
    "你是医学课程内容审校专家。给定来自不同课程的知识卡片断言对,"
    "判断每对是否构成真正的相反断言冲突(同一主题、同一适用条件,结论相反)。"
    "注意:适用条件不同(如年龄/骨型/阶段不同)不算冲突,只是角度不同也不算冲突。"
    "只输出 JSON。"
)


def judge_conflicts(cards, pairs, logger):
    """分批送 LLM 判定,返回冲突列表。"""
    conflicts = []
    for k in range(0, len(pairs), CONFLICT_BATCH):
        batch = pairs[k:k + CONFLICT_BATCH]
        lines = []
        for n, (i, j) in enumerate(batch, 1):
            a, b = cards[i], cards[j]
            lines.append(
                f"{n}. A[{a['course_id']} {transcript.fmt_ts(a['start_sec'])[:-4]}] {a['content']}\n"
                f"   B[{b['course_id']} {transcript.fmt_ts(b['start_sec'])[:-4]}] {b['content']}"
            )
        prompt = ("下面是候选断言对(来自同一训练营体系不同课程)。逐对判断:\n"
                  "- 若构成真冲突(同主题同条件下结论相反):给出 conflict=true、主题、冲突说明、建议处置。\n"
                  "- 否则 conflict=false。\n"
                  '只输出 JSON 数组:[{"pair":1,"conflict":true,"topic":"...","explain":"...","suggestion":"..."}]\n\n'
                  + "\n".join(lines))
        try:
            text, _ = llm.chat(PART, prompt, system=CONFLICT_SYS, max_tokens=LLM_MAX_TOKENS)
            items = llm.parse_json(text)
            if isinstance(items, dict):
                items = [items]
        except Exception as e:  # noqa: BLE001
            logger.log(f"冲突判定批次失败(k={k}): {e!r}")
            common.notify("step2 冲突判定批次失败", f"batch k={k}: {e!r}")
            continue
        for it in items:
            if not isinstance(it, dict) or not it.get("conflict"):
                continue
            try:
                i, j = batch[int(it.get("pair", 0)) - 1]
            except Exception:  # noqa: BLE001
                continue
            conflicts.append({
                "topic": str(it.get("topic", "")).strip(),
                "a": cards[i], "b": cards[j],
                "explain": str(it.get("explain", "")).strip(),
                "suggestion": str(it.get("suggestion", "")).strip(),
            })
    return conflicts


def write_conflicts_md(path, conflicts, n_pairs, n_cards, courses_scope):
    lines = [
        "# 知识卡片冲突检测报告",
        "",
        f"- 排查范围:{courses_scope},共 {n_cards} 张卡片,跨课同主题候选断言对 {n_pairs} 对,LLM 逐对判定。",
        f"- 结论:发现真冲突 {len(conflicts)} 处。" if conflicts else "- 结论:未发现真冲突(候选对或为适用条件不同、或角度不同,非相反断言)。",
        "",
    ]
    if conflicts:
        lines += ["| # | 主题 | 断言A(课/时间戳) | 断言B(课/时间戳) | 冲突说明 | 建议处置 |",
                  "|---|---|---|---|---|---|"]
        for n, c in enumerate(conflicts, 1):
            a, b = c["a"], c["b"]
            ta = transcript.fmt_ts(a["start_sec"])[:-4]
            tb = transcript.fmt_ts(b["start_sec"])[:-4]
            esc = lambda s: s.replace("|", "\\|").replace("\n", " ")  # noqa: E731
            lines.append(f"| {n} | {esc(c['topic'])} | {a['course_id']} {ta}:{esc(a['content'])} | "
                         f"{b['course_id']} {tb}:{esc(b['content'])} | {esc(c['explain'])} | {esc(c['suggestion'])} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 主流程 ----------

def main():
    logger = common.StepLogger(PART)
    out_dir = common.get_run_dir() / "step2"
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        master = common.read_json(common.get_run_dir() / "step0" / "courses_master.json")
        fw = yaml.safe_load((common.CONFIG / "framework.yaml").read_text(encoding="utf-8"))
        card_types = list(fw["card_types"])
        all_cards, stats = [], {"version": VERSION, "per_course": {},
                                "total_cards": 0, "total_discarded": 0, "discard_reasons": {}}
        # --- 段级并发抽取(llm.chat 内置重试与内容寻址缓存,线程安全) ---
        preps = {}
        tasks = []  # (cid, seg_idx, seg_text)
        for course in master:
            cid = course["canonical_id"]
            try:
                prep = prepare_course(course)
            except Exception as e:  # noqa: BLE001 - 单课失败可恢复:notify 后继续
                logger.log(f"{cid} 读取失败: {e!r}")
                common.notify("step2 单课读取失败", f"{cid}: {e!r}")
                stats["per_course"][cid] = {"cards": 0, "type_dist": {}, "discarded": 0,
                                            "segments": 0, "error": repr(e)}
                continue
            preps[cid] = prep
            for si, (seg_text, _idxs) in enumerate(prep["segs"]):
                tasks.append((cid, si, seg_text))
        logger.log(f"课程 {len(preps)} 节,分段 {len(tasks)} 个,并发 {WORKERS} 抽取")
        seg_items = {}   # cid -> {seg_idx: items}
        seg_discard = defaultdict(Counter)
        failed_segs = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = {ex.submit(call_segment, preps[cid], card_types, seg_text): (cid, si)
                    for cid, si, seg_text in tasks}
            done = 0
            for fut in as_completed(futs):
                cid, si = futs[fut]
                done += 1
                try:
                    seg_items.setdefault(cid, {})[si] = fut.result()
                except Exception as e:  # noqa: BLE001 - 单段失败记丢弃,不中断
                    failed_segs += 1
                    seg_discard[cid]["llm_or_parse_error"] += 1
                    logger.log(f"{cid} 段{si} 抽取失败: {e!r}")
                if done % 20 == 0:
                    logger.log(f"抽取进度 {done}/{len(tasks)}")
        # --- 主线程校验/去重/选课 ---
        for course in master:
            cid = course["canonical_id"]
            if cid not in preps:
                continue
            prep = preps[cid]
            discard = seg_discard[cid]
            candidates = []
            for si in sorted(seg_items.get(cid, {})):
                candidates.extend(validate_items(prep, card_types, seg_items[cid][si], discard))
            # 去重:quote 归一化后互为子串的视为重复
            deduped, seen = [], []
            for c in candidates:
                qn = re.sub(r"\s+", "", c["quote"])
                if any(qn in s or s in qn for s in seen):
                    discard["duplicate"] += 1
                    continue
                seen.append(qn)
                deduped.append(c)
            picked = select_cards(deduped, card_types)
            overflow = len(deduped) - len(picked)
            if overflow > 0:
                discard["overflow_dropped"] += overflow
            for seq, c in enumerate(picked, 1):
                c["card_id"] = f"{cid}-{seq:04d}"
            all_cards.extend(picked)
            tdist = Counter(c["type"] for c in picked)
            stats["per_course"][cid] = {
                "cards": len(picked), "type_dist": dict(tdist),
                "discarded": sum(discard.values()), "discard_detail": dict(discard),
                "segments": len(prep["segs"]),
            }
            if len(picked) < TARGET_MIN:
                logger.log(f"{cid} 卡片不足 {TARGET_MIN} 张({len(picked)}),只抽不补,如实记录")
                common.notify("step2 单课卡片不足", f"{cid} 仅 {len(picked)} 张(<{TARGET_MIN})")
            logger.log(f"{cid} 段落 {len(prep['segs'])} 候选 {len(deduped)} "
                       f"入选 {len(picked)} 丢弃 {sum(discard.values())}")
        with open(out_dir / "cards.jsonl", "w", encoding="utf-8") as f:
            for c in all_cards:
                f.write(json.dumps(c, ensure_ascii=False) + "\n")
        stats["total_cards"] = len(all_cards)
        stats["total_discarded"] = sum(v["discarded"] for v in stats["per_course"].values())
        dr = Counter()
        for v in stats["per_course"].values():
            dr.update(v.get("discard_detail", {}))
        stats["discard_reasons"] = dict(dr)
        failed_courses = [cid for cid, v in stats["per_course"].items() if v.get("error")]
        stats["failed_courses"] = failed_courses
        stats["failed_segments"] = failed_segs
        logger.log(f"卡片合计 {len(all_cards)} 张,丢弃 {stats['total_discarded']}")

        # --- 冲突检测 ---
        pairs = find_candidate_pairs(all_cards)
        logger.log(f"冲突候选对 {len(pairs)}")
        conflicts = judge_conflicts(all_cards, pairs, logger) if pairs else []
        scope = f"{len(master)} 节课({master[0]['canonical_id']}~{master[-1]['canonical_id']})"
        write_conflicts_md(out_dir / "conflicts.md", conflicts, len(pairs), len(all_cards), scope)
        stats["conflict_candidates"] = len(pairs)
        stats["conflicts_found"] = len(conflicts)
        common.write_json(out_dir / "extract_stats.json", stats)
        logger.close(ok=True, cards=len(all_cards), conflicts=len(conflicts),
                     failed_courses=failed_courses)
    except Exception as e:  # noqa: BLE001 - 顶层兜底:notify 后抛出
        common.notify("step2 知识卡片失败", f"{e!r}")
        logger.close(ok=False, error=repr(e))
        raise


if __name__ == "__main__":
    main()
