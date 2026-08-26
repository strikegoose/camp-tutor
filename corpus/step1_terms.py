#!/usr/bin/env python3
"""Step1 术语纠错:种子词典替换 → 拼音聚类挖候选 → LLM 裁决 → 数字单位专项
→ 两营交叉校验 → 终稿清洗 + 100% 自检。
学科无关:术语/守卫规则全部读 config(seed_terms.yaml / step1_guards.yaml)。
幂等:各 LLM 阶段结果落 step1/*_raw.json,输入未变时重跑直接复用(llm.py 另有内容寻址缓存)。"""
import csv, json, os, re, sys, time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, os.path.expanduser(os.environ.get("CORPUS_HUB", "~/Claude/projects/corpus-hub")))
from lib import common, llm, transcript  # noqa: E402
from step0_reconcile import similarity  # noqa: E402

import yaml  # noqa: E402
from pypinyin import lazy_pinyin, Style  # noqa: E402

PART = "step1_terms"
VERSION = "step1-20260826-3"
# 模型为 thinking 型:思考计入 output budget,max_tokens 需留足;批次适当小
BATCH = 15
LLM_MAX_TOKENS = 16384
CJK = re.compile(r"[一-鿿]{2,}")
MIN_DICT = 100

ADJ_SYS = ("你是口腔正畸(早矫/功能合学)训练营课程 ASR 逐字稿的术语纠错专家。"
           "判断同音/近音异形词对:哪个是正确术语、哪个是误转写;或两者是合法同义词;或与术语无关应拒绝。"
           "拿不准一律拒绝,宁缺毋滥。只输出 JSON。")
NUM_SYS = ("你是口腔临床语境校验专家。给定逐字稿中「数字+单位」片段及上下文,"
           "判断是否存在明显误转写(量纲或数字与临床常识冲突,如「牙弓宽度 3 个月」应为毫米)。"
           "语境合理或拿不准即为 ok。只输出 JSON。")
XC_SYS = ("同一讲师体系两期训练营的同主题课程,术语写法应一致。"
          "给定课程对中同音异形的写法差异,判断应统一为哪个写法(入词典),或证据不足存疑。"
          "只输出 JSON。")


# ---------- 通用工具 ----------

_PY_CACHE = {}


def parse_loose(text):
    """比 lib.parse_json 更宽容:支持逐行 JSON 对象、前后杂文本、多对象拼接。"""
    try:
        return llm.parse_json(text)
    except Exception:  # noqa: BLE001
        pass
    items, dec = [], json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            obj, _ = dec.raw_decode(text[m.start():])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, list):
            items.extend(obj)
        elif isinstance(obj, dict):
            items.append(obj)
    if items:
        return items
    raise ValueError(f"模型输出无法解析为 JSON: {text[:200]}")


def pinyin_key(s):
    """逐字拼音缓存(忽略声调),n-gram 全量统计下避免重复调 pypinyin。"""
    out = []
    for ch in s:
        p = _PY_CACHE.get(ch)
        if p is None:
            r = lazy_pinyin(ch, style=Style.NORMAL, errors="ignore")
            p = r[0] if r else ""
            _PY_CACHE[ch] = p
        out.append(p)
    return " ".join(out)


def load_inputs():
    master = common.read_json(common.get_run_dir() / "step0" / "courses_master.json")
    seed = yaml.safe_load((common.CONFIG / "seed_terms.yaml").read_text(encoding="utf-8"))
    guards = yaml.safe_load((common.CONFIG / "step1_guards.yaml").read_text(encoding="utf-8"))
    excl_file = common.CONFIG / "step1_exclude.yaml"
    excl = yaml.safe_load(excl_file.read_text(encoding="utf-8")) if excl_file.exists() else {}
    return master, seed, guards, excl


def input_hash():
    excl_file = common.CONFIG / "step1_exclude.yaml"
    h = common.sha256_text(
        (common.get_run_dir() / "step0" / "courses_master.json").read_text(encoding="utf-8")
        + (common.CONFIG / "seed_terms.yaml").read_text(encoding="utf-8")
        + (common.CONFIG / "step1_guards.yaml").read_text(encoding="utf-8")
        + (excl_file.read_text(encoding="utf-8") if excl_file.exists() else "")
        + VERSION)
    return h


def apply_human_review(dict_terms, excl, logger):
    """人工终审:剔除 exclude 对(非 seed 词条剔空 variants 后整条删除),补入 include 条。"""
    pairs = {(e["canonical"], e["variant"]): e["reason"] for e in excl.get("exclude", []) or []}
    if not pairs and not excl.get("include"):
        return dict_terms
    removed = []
    out = []
    for t in dict_terms:
        vs = [v for v in (t.get("variants") or []) if (t["canonical"], v) not in pairs]
        for v in (t.get("variants") or []):
            if (t["canonical"], v) in pairs:
                removed.append(f"{t['canonical']} <= {v}")
        t = dict(t, variants=vs)
        if not vs and t.get("source") != "seed" and any(c == t["canonical"] for c, _ in pairs):
            continue  # 非 seed 词条,variants 被剔空 → 整条删除
        out.append(t)
    for e in excl.get("include", []) or []:
        if not any(t["canonical"] == e["canonical"] for t in out):
            out.append({"canonical": e["canonical"], "variants": list(e.get("variants", [])),
                        "category": e.get("category", "其他"),
                        "source": e.get("source", "pinyin裁决"), "note": e.get("note", e.get("reason", ""))})
    logger.log(f"人工终审: 剔除 {len(removed)} 对,补入 {len(excl.get('include', []) or [])} 条,词典 {len(out)} 条")
    return out


from corpus.terms import RuleSet  # noqa: E402  # T2: 公共替换引擎(20260826-03)


# ---------- 语料加载与初稿 ----------

def load_transcripts(master, logger):
    """解析 44 节原始逐字稿;单课失败 notify 后继续。返回 {cid: Transcript}。"""
    out = {}
    for row in master:
        cid = row["canonical_id"]
        try:
            raw = Path(row["transcript_path"]).read_text(encoding="utf-8")
            t = transcript.parse(raw)
            if not t.blocks:
                raise ValueError("无 Speaker 块")
            out[cid] = t
        except Exception as e:  # noqa: BLE001
            common.notify("camp-tutor step1 逐字稿解析失败", f"{cid}: {e!r}"[:300])
            logger.log(f"WARN 解析失败跳过 {cid}: {e!r}")
    return out


def clean_transcript(t, ruleset, counter, skip_counter):
    """对 Transcript 应用替换规则(keywords + 各 Speaker 块),原地修改。"""
    t.keywords = ruleset.apply(t.keywords, counter, skip_counter)
    for b in t.blocks:
        b.text = ruleset.apply(b.text, counter, skip_counter)


# ---------- n-gram 统计与拼音聚类 ----------

def ngram_stats(texts, nmin=2, nmax=6):
    """中文 n-gram 全量统计。返回 {gram: [次数, set(课程)]}。"""
    stats = {}
    for cid, text in texts.items():
        per = Counter()
        for run in CJK.finditer(text):
            s = run.group(0)
            L = len(s)
            for n in range(nmin, nmax + 1):
                for i in range(0, L - n + 1):
                    per[s[i:i + n]] += 1
        for g, c in per.items():
            e = stats.get(g)
            if e is None:
                stats[g] = [c, {cid}]
            else:
                e[0] += c
                e[1].add(cid)
    return stats


def find_context(texts, gram, width=22):
    """取 gram 的首个出现处上下文(课程, 片段)。"""
    for cid, text in texts.items():
        i = text.find(gram)
        if i >= 0:
            seg = text[max(0, i - width): i + len(gram) + width].replace("\n", " ")
            return cid, f"…{seg}…"
    return "", ""


def mine_candidates(stats, texts, dict_terms, known_pairs, ngram_min_freq, cluster_min_freq,
                    cluster_cap, logger):
    """锚定(与 canonical 同音异形)+ 高频聚类组内同音异形对。返回候选列表。"""
    grams = {g: v for g, v in stats.items() if v[0] >= ngram_min_freq}
    canonicals = {}
    known_variants = set()
    for t in dict_terms:
        canonicals[t["canonical"]] = pinyin_key(t["canonical"])
        for v in t.get("variants", []) or []:
            known_variants.add(v)
    py_of = {}
    for g in grams:
        py_of[g] = pinyin_key(g)
    cands = []
    # 锚定:与词典 canonical 同拼音但字形不同
    for g, (cnt, cids) in sorted(grams.items()):
        if g in canonicals or g in known_variants:
            continue
        pg = py_of[g]
        for canon, pc in canonicals.items():
            if pc and pg == pc and abs(len(g) - len(canon)) == 0:
                pair = (canon, g)
                if pair in known_pairs:
                    continue
                known_pairs.add(pair)
                cands.append({"a": canon, "b": g, "kind": "锚定",
                              "ca": 0, "cb": cnt, "nb": len(cids)})
                break
    # 聚类:高频 n-gram 按拼音分组,组内近音异形对
    groups = defaultdict(list)
    for g, (cnt, cids) in sorted(grams.items()):
        if cnt >= cluster_min_freq:
            groups[py_of[g]].append((g, cnt, len(cids)))
    cluster_pairs = []
    for py, items in sorted(groups.items()):
        if len(items) < 2:
            continue
        items.sort(key=lambda x: -x[1])
        items = items[:6]
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                (ga, ca, na), (gb, cb, nb) = items[i], items[j]
                if ga in canonicals and gb in canonicals:
                    continue
                pair = (ga, gb) if (ga, gb) not in known_pairs and (gb, ga) not in known_pairs else None
                if pair is None:
                    continue
                known_pairs.add(pair)
                cluster_pairs.append({"a": ga, "b": gb, "kind": "聚类", "py": py,
                                      "ca": ca, "cb": cb, "na": na, "nb": nb,
                                      "score": min(ca, cb)})
    cluster_pairs.sort(key=lambda c: -c["score"])
    cluster_pairs = cluster_pairs[:cluster_cap]
    cands.extend(cluster_pairs)
    # 补上下文与建议
    for c in cands:
        c["py"] = c.get("py") or pinyin_key(c["a"])
        if c["kind"] == "锚定":
            c["na"] = len(texts)
            _, c["ctx_b"] = find_context(texts, c["b"])
            c["ctx_a"] = f"(词典 canonical:{c['a']})"
            c["suggest"] = "建议采纳(与词典术语同音异形)"
        else:
            _, c["ctx_a"] = find_context(texts, c["a"])
            _, c["ctx_b"] = find_context(texts, c["b"])
            c["suggest"] = "建议 LLM 裁决"
    logger.log(f"挖候选: 锚定={sum(1 for c in cands if c['kind'] == '锚定')} "
               f"聚类={sum(1 for c in cands if c['kind'] == '聚类')} (阈值 ngram>={ngram_min_freq}, cluster>={cluster_min_freq})")
    return cands


def write_candidates_csv(path, cands):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["词形A", "词形B", "拼音", "次数A", "次数B", "课程数A", "课程数B",
                    "示例A", "示例B", "裁决建议", "来源"])
        for c in cands:
            w.writerow([c["a"], c["b"], c["py"], c.get("ca", ""), c.get("cb", ""),
                        c.get("na", ""), c.get("nb", ""), c.get("ctx_a", ""),
                        c.get("ctx_b", ""), c["suggest"], c["kind"]])


# ---------- LLM 阶段 ----------

def llm_batches(items):
    for i in range(0, len(items), BATCH):
        yield items[i:i + BATCH]


def adjudicate(cands, logger):
    """候选分批 LLM 裁决。返回裁决记录列表(含失败批次标记)。
    告警聚合(2026-08-26 标准):逐批失败只记日志,本轮结束后聚合一条 notify。"""
    results, failed_batches = [], []
    for bi, batch in enumerate(llm_batches(cands), 1):
        lines = []
        for k, c in enumerate(batch, 1):
            lines.append(f"{k}. A「{c['a']}」({c.get('ca', 0)}次) vs B「{c['b']}」({c.get('cb', 0)}次,{c.get('nb', 0)}课)\n"
                         f"   A例: {c.get('ctx_a', '')}\n   B例: {c.get('ctx_b', '')}")
        prompt = ("以下是 ASR 逐字稿中发现的同音/近音异形词对(口腔正畸早矫训练营语境)。\n"
                  "逐条裁决,输出 JSON 数组,每条形如:\n"
                  '{"id": 编号, "decision": "采纳纠错|同义保留|拒绝", '
                  '"canonical": "正确/推荐写法(采纳或同义时必填,须为A或B之一)", '
                  '"variant": "误转写形(采纳时必填,为另一个)", '
                  '"category": "解剖|临床|诊断|工具|讲师|机构|学科|运营|其他", '
                  '"reason": "一句话依据"}\n'
                  "判定口径:「采纳纠错」=一个是另一个的误转写;「同义保留」=两者皆合法但建议统一写法;"
                  "「拒绝」=两词无关或无法判断。\n\n" + "\n".join(lines))
        try:
            text, cached = llm.chat(PART, prompt, system=ADJ_SYS, max_tokens=LLM_MAX_TOKENS, temperature=0.0)
            arr = parse_loose(text)
            by_id = {}
            for item in arr:
                try:
                    by_id[int(item.get("id"))] = item
                except Exception:  # noqa: BLE001
                    continue
            for k, c in enumerate(batch, 1):
                it = by_id.get(k)
                if not it:
                    results.append({"a": c["a"], "b": c["b"], "kind": c["kind"],
                                    "decision": "拒绝", "reason": "LLM 未返回该条,按拒绝处理"})
                    continue
                dec = str(it.get("decision", "拒绝"))
                canon, var = str(it.get("canonical", "")), str(it.get("variant", ""))
                if dec == "采纳纠错" and {canon, var} != {c["a"], c["b"]}:
                    dec, reason = "拒绝", "canonical/variant 不在候选对内,按拒绝处理"
                else:
                    reason = str(it.get("reason", ""))
                results.append({"a": c["a"], "b": c["b"], "kind": c["kind"], "decision": dec,
                                "canonical": canon, "variant": var,
                                "category": str(it.get("category", "其他")), "reason": reason})
            logger.log(f"裁决批次 {bi}: {len(batch)} 条 (cache={cached})")
        except Exception as e:  # noqa: BLE001
            failed_batches.append(bi)
            logger.log(f"WARN 裁决批次 {bi} 失败: {e!r}")
            for c in batch:
                results.append({"a": c["a"], "b": c["b"], "kind": c["kind"],
                                "decision": "未裁决", "reason": f"批次失败: {e!r}"[:200]})
    if failed_batches:
        ids = "/".join(str(b) for b in failed_batches)
        common.notify(f"step1 本轮裁决失败 {len(failed_batches)} 批:{ids}",
                      f"批次 {ids} 按未裁决继续,明细见 logs/{logger.path.name}")
        logger.log(f"WARN 共 {len(failed_batches)} 个裁决批次失败(已聚合 notify:{ids})")
    return results


def extract_number_unit(texts, rules, logger):
    """按 seed number_unit_rules 正则抽取「数字+单位」出现,带上下文。
    正则中的数字组统一升级为支持小数,避免把「3.5 毫米」截成「5 毫米」。"""
    items = []
    for cid, text in texts.items():
        for ru in rules:
            pat = ru["pattern"].replace(r"(\d+)", r"(\d+(?:\.\d+)?)")
            for m in re.finditer(pat, text):
                frag = m.group(0)
                items.append({"course": cid, "unit": ru["unit"], "fragment": frag,
                              "ctx": text[max(0, m.start() - 30): m.end() + 30].replace("\n", " ")})
    logger.log(f"数字+单位抽取: {len(items)} 处 (规则 {len(rules)} 条)")
    return items


NU_REVIEW_CAP = 8  # 每课送审上限(mm/度优先),其余标注「未逐一送审」;抽取全量入报告


def review_number_unit(items, logger):
    """LLM 分批审「数字+单位」,标明显误转写并给修正。每课抽样送审(临床量纲风险高的 mm/度 优先)。"""
    out = list(items)
    prio = {"mm": 0, "度": 1, "月": 2, "岁": 3}
    by_course = defaultdict(list)
    for i, it in enumerate(items):
        by_course[it["course"]].append(i)
    review_idx = []
    for cid, idxs in by_course.items():
        idxs.sort(key=lambda i: (prio.get(items[i]["unit"], 9), i))
        review_idx.extend(idxs[:NU_REVIEW_CAP])
    reviewed = set(review_idx)
    failed_batches = []
    logger.log(f"数字单位送审 {len(review_idx)}/{len(items)} 处(每课上限 {NU_REVIEW_CAP},mm/度 优先)")
    for bi, batch_idx in enumerate(llm_batches(review_idx), 1):
        lines = []
        for k, gi in enumerate(batch_idx, 1):
            it = items[gi]
            lines.append(f"{k}. [{it['course']}] 「{it['fragment']}」 上下文: …{it['ctx']}…")
        prompt = ("逐条判断「数字+单位」在临床语境是否明显误转写。输出 JSON 数组:\n"
                  '{"id": 编号, "ok": true|false, "corrected": "修正后的片段(仅 ok=false 时给,'
                  '只改数字/单位,不动其他字)", "reason": "一句话(仅 ok=false 时给)"}\n'
                  "拿不准一律 ok=true。\n\n" + "\n".join(lines))
        try:
            text, cached = llm.chat(PART, prompt, system=NUM_SYS, max_tokens=LLM_MAX_TOKENS, temperature=0.0)
            arr = parse_loose(text)
            by_id = {}
            for it in arr:
                try:
                    by_id[int(it.get("id"))] = it
                except Exception:  # noqa: BLE001
                    continue
            for k, gi in enumerate(batch_idx, 1):
                r = by_id.get(k)
                if r and r.get("ok") is False and r.get("corrected"):
                    out[gi] = dict(out[gi], ok=False, corrected=str(r["corrected"]),
                                   reason=str(r.get("reason", "")), reviewed=True)
                else:
                    out[gi] = dict(out[gi], ok=True, reviewed=True)
            logger.log(f"数字单位审查批次 {bi}: {len(batch_idx)} 条 (cache={cached})")
        except Exception as e:  # noqa: BLE001
            failed_batches.append(bi)
            logger.log(f"WARN 数字单位批次 {bi} 失败: {e!r}")
            for gi in batch_idx:
                out[gi] = dict(out[gi], ok=True, review_failed=True)
    if failed_batches:
        ids = "/".join(str(b) for b in failed_batches)
        common.notify(f"step1 本轮数字单位审查失败 {len(failed_batches)} 批:{ids}",
                      f"批次 {ids} 按 ok 继续,明细见 logs/{logger.path.name}")
    for gi in range(len(out)):
        if gi not in reviewed and not out[gi].get("reviewed"):
            out[gi] = dict(out[gi], ok=True, sampled_out=True)
    return out


def apply_fragment_fix(t, fragment, corrected, ctx):
    """片段级修正:用上下文定位唯一出现处替换,绝不全局替换纯数字。
    安全闸:命中处前一字符是数字或小数点(疑似小数被截断的抽取)时不替换。"""
    text = t.keywords + "\n" + "\n".join(b.text for b in t.blocks)
    hits = [m.start() for m in re.finditer(re.escape(fragment), text)]
    if not hits:
        return False, "片段未找到(可能已被术语替换改变)"
    best, best_score = None, -1
    core = ctx.replace("…", "").replace(fragment, "§")
    pre, _, post = core.partition("§")
    for h in hits:
        score = 0
        before = text[max(0, h - 30):h]
        after = text[h + len(fragment): h + len(fragment) + 30]
        for tok in re.findall(r"[一-鿿]{2,}|\d+", pre)[-4:]:
            if tok in before:
                score += 1
        for tok in re.findall(r"[一-鿿]{2,}|\d+", post)[:4]:
            if tok in after:
                score += 1
        if score > best_score:
            best, best_score = h, score
    if best > 0 and (text[best - 1].isdigit() or text[best - 1] == "."):
        return False, "命中处前一字为数字/小数点,疑似截断抽取,跳过"
    # 在对应块内替换唯一一处
    pos = 0
    target = best
    kw_end = len(t.keywords) + 1
    if target < kw_end - 1:
        t.keywords = t.keywords.replace(fragment, corrected, 1)
        return True, "keywords"
    for b in t.blocks:
        b_start = kw_end + pos
        b_end = b_start + len(b.text)
        if b_start <= target < b_end:
            off = target - b_start
            b.text = b.text[:off] + corrected + b.text[off + len(fragment):]
            return True, f"block@{b.start_sec:.1f}s"
        pos += len(b.text) + 1
    return False, "定位失败"


# ---------- 两营交叉校验 ----------

def cross_check(master, draft_texts, dict_terms, logger):
    """同主题跨营课对 → 同音异形写法差异 → LLM 确认。"""
    rows = [r for r in master if r["canonical_id"] in draft_texts]
    pairs = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if a["camp"] == b["camp"]:
                continue
            sc = similarity(a["title"], b["title"])
            if sc >= 0.5:
                pairs.append((sc, a["canonical_id"], b["canonical_id"]))
    pairs.sort(key=lambda p: -p[0])
    logger.log(f"跨营同主题课对(相似度>=0.5): {len(pairs)} 对")
    known_variants, canonicals = set(), set()
    for t in dict_terms:
        canonicals.add(t["canonical"])
        known_variants.update(t.get("variants", []) or [])
    diffs = []
    for sc, ca, cb in pairs:
        texts = {ca: draft_texts[ca], cb: draft_texts[cb]}
        stats = ngram_stats(texts)
        groups = defaultdict(set)
        for g, (cnt, cids) in sorted(stats.items()):
            if cnt >= 2:
                groups[pinyin_key(g)].add(g)
        made = 0
        for py, forms in sorted(groups.items()):
            if len(forms) < 2 or made >= 12:
                continue
            in_a = [g for g in sorted(forms) if g in texts[ca]]
            in_b = [g for g in sorted(forms) if g in texts[cb] and g not in texts[ca]]
            for ga in in_a:
                for gb in in_b:
                    if made >= 12:
                        break
                    if ga in canonicals and gb in known_variants:
                        continue
                    _, ctxa = find_context(texts, ga)
                    _, ctxb = find_context(texts, gb)
                    diffs.append({"pair": f"{ca}↔{cb}", "score": round(sc, 2),
                                  "a": ga, "b": gb, "py": py, "ctx_a": ctxa, "ctx_b": ctxb})
                    made += 1
    diffs = diffs[:120]  # 全局上限:课对按标题相似度降序,取最有代表性的差异送审
    logger.log(f"交叉校验写法差异候选: {len(diffs)} 条(全局上限 120)")
    results, failed_batches = [], []
    for bi, batch in enumerate(llm_batches(diffs), 1):
        lines = []
        for k, d in enumerate(batch, 1):
            lines.append(f"{k}. 课对 {d['pair']}:「{d['a']}」 vs 「{d['b']}」(拼音 {d['py']})\n"
                         f"   例A: {d['ctx_a']}\n   例B: {d['ctx_b']}")
        prompt = ("判断每对写法差异是否同一术语的两种写法、应统一。输出 JSON 数组:\n"
                  '{"id": 编号, "decision": "统一|存疑", "canonical": "推荐统一写法(须为候选之一)", '
                  '"variant": "另一写法", "reason": "一句话"}\n'
                  "两者是不同概念或无法判断则「存疑」。\n\n" + "\n".join(lines))
        try:
            text, cached = llm.chat(PART, prompt, system=XC_SYS, max_tokens=LLM_MAX_TOKENS, temperature=0.0)
            arr = parse_loose(text)
            by_id = {}
            for it in arr:
                try:
                    by_id[int(it.get("id"))] = it
                except Exception:  # noqa: BLE001
                    continue
            for k, d in enumerate(batch, 1):
                it = by_id.get(k)
                if it and it.get("decision") == "统一" and {it.get("canonical"), it.get("variant")} == {d["a"], d["b"]}:
                    results.append(dict(d, decision="统一", canonical=str(it["canonical"]),
                                        variant=str(it["variant"]), reason=str(it.get("reason", ""))))
                else:
                    rsn = str(it.get("reason", "")) if it else "LLM 未返回该条"
                    results.append(dict(d, decision="存疑", reason=rsn))
            logger.log(f"交叉校验批次 {bi}: {len(batch)} 条 (cache={cached})")
        except Exception as e:  # noqa: BLE001
            failed_batches.append(bi)
            logger.log(f"WARN 交叉校验批次 {bi} 失败: {e!r}")
            for d in batch:
                results.append(dict(d, decision="存疑", reason=f"批次失败: {e!r}"[:200]))
    if failed_batches:
        ids = "/".join(str(b) for b in failed_batches)
        common.notify(f"step1 本轮交叉校验失败 {len(failed_batches)} 批:{ids}",
                      f"批次 {ids} 按存疑继续,明细见 logs/{logger.path.name}")
    return pairs, results


# ---------- 报告 ----------

def write_adjudication_md(path, results, run_name):
    acc = [r for r in results if r["decision"] == "采纳纠错"]
    syn = [r for r in results if r["decision"] == "同义保留"]
    rej = [r for r in results if r["decision"] == "拒绝"]
    una = [r for r in results if r["decision"] == "未裁决"]
    L = ["# 拼音聚类候选·LLM 裁决记录", "", f"- 运行: {run_name}",
         f"- 候选 {len(results)} 条:采纳纠错 {len(acc)} / 同义保留 {len(syn)} / 拒绝 {len(rej)} / 未裁决 {len(una)}",
         "", "## 采纳纠错(并入词典 v1)", "",
         "| canonical | variant | 类别 | 来源 | 理由 |", "|---|---|---|---|---|"]
    for r in acc:
        L.append(f"| {r['canonical']} | {r['variant']} | {r.get('category', '')} | {r['kind']} | {r['reason']} |")
    if not acc:
        L.append("| - | - | - | - | 无 |")
    L += ["", "## 同义保留(并入词典 v1,标记同义非纠错,统一为推荐写法)", "",
          "| canonical | variant | 类别 | 来源 | 理由 |", "|---|---|---|---|---|"]
    for r in syn:
        L.append(f"| {r['canonical']} | {r['variant']} | {r.get('category', '')} | {r['kind']} | {r['reason']} |")
    if not syn:
        L.append("| - | - | - | - | 无 |")
    L += ["", "## 拒绝", "", "| 词形A | 词形B | 来源 | 理由 |", "|---|---|---|---|"]
    for r in rej:
        L.append(f"| {r['a']} | {r['b']} | {r['kind']} | {r['reason']} |")
    if not rej:
        L.append("| - | - | - | 无 |")
    if una:
        L += ["", "## 未裁决(批次失败,见 logs/notify.log)", "", "| 词形A | 词形B | 原因 |", "|---|---|---|"]
        for r in una:
            L.append(f"| {r['a']} | {r['b']} | {r['reason']} |")
    L.append("")
    Path(path).write_text("\n".join(L), encoding="utf-8")


def write_number_unit_md(path, items, applied, run_name):
    bad = [it for it in items if not it.get("ok", True)]
    n_reviewed = sum(1 for it in items if it.get("reviewed"))
    L = ["# 数字与单位专项校验报告", "", f"- 运行: {run_name}",
         f"- 抽取「数字+单位」共 {len(items)} 处;LLM 送审 {n_reviewed} 处(每课上限 {NU_REVIEW_CAP},mm/度 优先),"
         f"标出明显误转写 {len(bad)} 处,"
         f"已按原文片段级修正 {sum(1 for a in applied.values() if a[0])} 处",
         "", "## 确认的误转写与修正", "",
         "| 课程 | 原片段 | 修正 | 理由 | 应用 |", "|---|---|---|---|---|"]
    for i, it in enumerate(bad):
        ok, where = applied.get(id(it), (None, ""))
        L.append(f"| {it['course']} | {it['fragment']} | {it['corrected']} | {it.get('reason', '')} | "
                 f"{'已修正(' + where + ')' if ok else ('未应用:' + where if where else '待应用')} |")
    if not bad:
        L.append("| - | - | - | - | LLM 未发现明显误转写 |")
    L += ["", "## 分课程抽取量", "", "| 课程 | 处数 |", "|---|---|"]
    cnt = Counter(it["course"] for it in items)
    for cid in sorted(cnt):
        L.append(f"| {cid} | {cnt[cid]} |")
    L.append("")
    Path(path).write_text("\n".join(L), encoding="utf-8")


def append_human_review_md(path, excl):
    """在裁决记录文末追加「人工终审剔除记录」(幂等:先移除旧章节再追加)。"""
    items = excl.get("exclude", []) or []
    inc = excl.get("include", []) or []
    if not items and not inc:
        return
    p = Path(path)
    text = p.read_text(encoding="utf-8") if p.exists() else "# 拼音聚类候选·LLM 裁决记录\n"
    text = text.split("\n## 人工终审剔除记录")[0].rstrip() + "\n"
    L = [text, "## 人工终审剔除记录", "",
         "以下 LLM 裁决条目经人工终审判定为方向错误/通用词/语法碎片,已从词典 v1 剔除:", "",
         "| canonical | variant | 剔除理由 |", "|---|---|---|"]
    for e in items:
        L.append(f"| {e['canonical']} | {e['variant']} | {e['reason']} |")
    if inc:
        L += ["", "人工终审补入(原批次失败未裁决、但语料证据确凿的术语对):", "",
              "| canonical | variants | 理由 |", "|---|---|---|"]
        for e in inc:
            L.append(f"| {e['canonical']} | {'、'.join(e.get('variants', []))} | {e.get('reason', e.get('note', ''))} |")
    L.append("")
    p.write_text("\n".join(L), encoding="utf-8")


def write_cross_check_md(path, pairs, results, run_name):
    uni = [r for r in results if r["decision"] == "统一"]
    L = ["# 两营重合主题·术语写法交叉校验报告", "", f"- 运行: {run_name}",
         f"- 跨营同主题课对(标题相似度≥0.5): {len(pairs)} 对;写法差异候选 {len(results)} 条,"
         f"确认统一 {len(uni)} 条(入词典 v1),存疑 {len(results) - len(uni)} 条",
         "", "## 课对", "", "| 课对 | 标题相似度 |", "|---|---|"]
    for sc, ca, cb in pairs:
        L.append(f"| {ca} ↔ {cb} | {sc:.2f} |")
    L += ["", "## 确认统一(入词典 v1,source=交叉校验)", "",
          "| 课对 | canonical | variant | 理由 |", "|---|---|---|---|"]
    for r in uni:
        L.append(f"| {r['pair']} | {r['canonical']} | {r['variant']} | {r['reason']} |")
    if not uni:
        L.append("| - | - | - | 无 |")
    L += ["", "## 存疑(不入词典,留人工)", "", "| 课对 | 写法A | 写法B | 理由 |", "|---|---|---|---|"]
    for r in results:
        if r["decision"] != "统一":
            L.append(f"| {r['pair']} | {r['a']} | {r['b']} | {r.get('reason', '')} |")
    L.append("")
    Path(path).write_text("\n".join(L), encoding="utf-8")


# ---------- 主流程 ----------

def main():
    logger = common.StepLogger(PART)
    t0 = time.time()
    run = common.get_run_dir()
    outdir = run / "step1"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "cleaned").mkdir(exist_ok=True)

    master, seed, guards, excl = load_inputs()
    ihash = input_hash()
    state_path = outdir / "state.json"
    state = common.read_json(state_path) if state_path.exists() else {}
    fresh = state.get("input_hash") == ihash
    logger.log(f"输入: 课程 {len(master)} 节, 种子词 {len(seed['terms'])} 条; "
               f"{'输入未变,复用各阶段缓存' if fresh else '输入变化或首跑,全量执行'}")

    def load_raw(name):
        p = outdir / name
        return common.read_json(p) if (fresh and p.exists()) else None

    # 阶段0:解析逐字稿
    transcripts = load_transcripts(master, logger)
    logger.summary(phase="parse", courses=len(transcripts))

    # 阶段1:种子词典替换(raw → 初稿)
    seed_rules = RuleSet(seed["terms"], guards)
    draft_texts, seed_counts = {}, {}
    for cid, t in transcripts.items():
        dt = transcript.parse(transcript.serialize(t))  # 副本,不动原始解析结果
        counter, skip = Counter(), Counter()
        clean_transcript(dt, seed_rules, counter, skip)
        draft_texts[cid] = transcript.full_text(dt) + "\n" + dt.keywords
        seed_counts[cid] = dict(counter)
    logger.summary(phase="seed_replace",
                   total=sum(sum(c.values()) for c in seed_counts.values()),
                   courses=len(draft_texts))

    # 阶段2+3:拼音聚类挖候选 + LLM 裁决(轮次:阈值递减,直到词典 >=100 或轮次耗尽)
    dict_terms = [dict(t, source="seed") for t in seed["terms"]]
    canon_seen = {t["canonical"] for t in dict_terms}

    def merge_result(r):
        """裁决/交叉校验确认的词对并入词典。"""
        if not r.get("canonical") or not r.get("variant"):
            return
        note = "同义非纠错" if r.get("decision") == "同义保留" else ""
        src = "交叉校验" if r.get("decision") == "统一" else "pinyin裁决"
        if r["canonical"] in canon_seen:
            for t in dict_terms:
                if t["canonical"] == r["canonical"] and r["variant"] not in (t.get("variants") or []):
                    t.setdefault("variants", []).append(r["variant"])
        else:
            canon_seen.add(r["canonical"])
            e = {"canonical": r["canonical"], "variants": [r["variant"]],
                 "category": r.get("category", "其他"), "source": src}
            if note:
                e["note"] = note
            dict_terms.append(e)

    adj_raw = load_raw("adjudication_raw.json")
    dict_yaml = outdir / "dict_v1.yaml"
    dict_from_yaml = False
    if adj_raw is not None:
        all_cands, all_results = adj_raw.get("candidates", []), adj_raw["results"]
        for r in all_results:
            if r["decision"] in ("采纳纠错", "同义保留"):
                merge_result(r)
        logger.log(f"拼音裁决: 复用缓存(候选 {len(all_cands)} 条,裁决 {len(all_results)} 条)")
        write_candidates_csv(outdir / "pinyin_candidates.csv", all_cands)
        write_adjudication_md(outdir / "adjudication.md", all_results, run.name)
    elif dict_yaml.exists():
        # 裁决缓存缺失但词典已产出:直接复用词典,跳过重挖掘/重裁决(人工终审路径)
        dict_terms = yaml.safe_load(dict_yaml.read_text(encoding="utf-8"))["terms"]
        canon_seen = {t["canonical"] for t in dict_terms}
        dict_from_yaml = True
        logger.log(f"词典: 复用 dict_v1.yaml({len(dict_terms)} 条),跳过候选挖掘与裁决")
    else:
        all_cands, all_results = [], []
        known_pairs = set()
        rounds = [(3, 5, 300), (2, 3, 400), (2, 2, 500)]
        stats_cache = None
        for ri, (nf, cf, cap) in enumerate(rounds, 1):
            done_pairs = {(r["a"], r["b"]) for r in all_results} | {(r["b"], r["a"]) for r in all_results}
            if ri == 1 or len(dict_terms) < MIN_DICT:
                if stats_cache is None:
                    stats_cache = ngram_stats(draft_texts)
                cands = mine_candidates(stats_cache, draft_texts, dict_terms,
                                        known_pairs | done_pairs, nf, cf, cap, logger)
                cands = [c for c in cands if (c["a"], c["b"]) not in done_pairs]
                all_cands.extend(cands)
                if cands:
                    res = adjudicate(cands, logger)
                    all_results.extend(res)
                    for r in res:
                        if r["decision"] in ("采纳纠错", "同义保留"):
                            merge_result(r)
                logger.summary(phase=f"round{ri}", candidates=len(cands),
                               dict_terms=len(dict_terms), elapsed_s=round(time.time() - t0, 1))
            if len(dict_terms) >= MIN_DICT:
                break
        if len(dict_terms) < MIN_DICT:
            common.notify("camp-tutor step1 词典不足", f"3 轮挖掘后词典 {len(dict_terms)} 条 < {MIN_DICT}")
            logger.log(f"WARN 词典 {len(dict_terms)} 条 < {MIN_DICT}(已 notify)")
        common.write_json(outdir / "adjudication_raw.json",
                          {"candidates": all_cands, "results": all_results})
        write_candidates_csv(outdir / "pinyin_candidates.csv", all_cands)
        write_adjudication_md(outdir / "adjudication.md", all_results, run.name)

    # 阶段5:两营交叉校验(词典来自 dict_v1.yaml 复用时,其结果已并入词典,跳过)
    xc_raw = load_raw("cross_check_raw.json")
    if dict_from_yaml:
        logger.log("交叉校验: 词典复用模式,跳过(保留既有报告)")
    elif xc_raw is None:
        xc_pairs, xc_results = cross_check(master, draft_texts, dict_terms, logger)
        xc_raw = {"pairs": xc_pairs, "results": xc_results}
        common.write_json(outdir / "cross_check_raw.json", xc_raw)
        for r in xc_results:
            if r["decision"] == "统一":
                merge_result(r)
        write_cross_check_md(outdir / "cross_check.md", xc_pairs, xc_results, run.name)
    else:
        xc_pairs, xc_results = [tuple(p) for p in xc_raw["pairs"]], xc_raw["results"]
        logger.log("交叉校验: 复用缓存")
        for r in xc_results:
            if r["decision"] == "统一":
                merge_result(r)
        write_cross_check_md(outdir / "cross_check.md", xc_pairs, xc_results, run.name)

    # 人工终审:剔除有害条目 + 补入(config/step1_exclude.yaml)
    dict_terms = apply_human_review(dict_terms, excl, logger)
    append_human_review_md(outdir / "adjudication.md", excl)
    if len(dict_terms) < MIN_DICT:
        common.notify("camp-tutor step1 终审后词典不足", f"{len(dict_terms)} 条 < {MIN_DICT}")
        logger.log(f"WARN 终审后词典 {len(dict_terms)} 条 < {MIN_DICT}(已 notify)")

    # 词典 v1 落盘
    dict_doc = {"version": f"v1-{run.name}",
                "terms": [{k: v for k, v in t.items() if v or k in ("variants",)} for t in dict_terms]}
    (outdir / "dict_v1.yaml").write_text(
        yaml.safe_dump(dict_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    logger.summary(phase="dict_v1", terms=len(dict_terms),
                   variants=sum(len(t.get("variants") or []) for t in dict_terms))

    # 阶段6:终稿 — raw 应用词典 v1 → 数字单位专项 → 落盘 + 自检
    v1_rules = RuleSet(dict_terms, guards)
    prefix_texts = {}
    per_course = {}
    raw_counts = {}
    for cid, t in transcripts.items():
        counter, skip = Counter(), Counter()
        clean_transcript(t, v1_rules, counter, skip)  # t 仍是原始解析(阶段1用的是副本)
        raw_counts[cid] = (dict(counter), dict(skip))
        prefix_texts[cid] = t
        per_course[cid] = {"v1_replacements": sum(counter.values()),
                           "by_canonical": dict(counter), "guarded_skips": dict(skip)}
    logger.summary(phase="v1_apply", total=sum(p["v1_replacements"] for p in per_course.values()))

    # 阶段4:数字与单位专项(在 v1 应用后文本上抽取,保证片段可定位)
    full_pre = {cid: transcript.full_text(t) + "\n" + t.keywords for cid, t in prefix_texts.items()}
    nu_raw = load_raw("number_unit_raw.json")
    if nu_raw is None:
        nu_items = extract_number_unit(full_pre, seed.get("number_unit_rules", []), logger)
        nu_items = review_number_unit(nu_items, logger)
        nu_raw = {"items": nu_items}
        common.write_json(outdir / "number_unit_raw.json", nu_raw)
    else:
        nu_items = nu_raw["items"]
        logger.log("数字单位专项: 复用缓存")
    applied = {}
    for it in nu_items:
        if it.get("ok", True) or not it.get("corrected"):
            continue
        t = prefix_texts.get(it["course"])
        if t is None:
            applied[id(it)] = (False, "课程缺失")
            continue
        ok, where = apply_fragment_fix(t, it["fragment"], it["corrected"], it["ctx"])
        applied[id(it)] = (ok, where)
        if not ok:
            logger.log(f"WARN 片段修正未应用 {it['course']} 「{it['fragment']}」: {where}")
    write_number_unit_md(outdir / "number_unit_report.md",
                         [it for it in nu_items], applied, run.name)
    n_fixed = sum(1 for ok, _ in applied.values() if ok)

    # 落盘清洗稿 + 自检
    self_bad, fmt_bad = {}, {}
    for cid, t in prefix_texts.items():
        out = transcript.serialize(t)
        (outdir / "cleaned" / f"{cid}.txt").write_text(out, encoding="utf-8")
        bad = v1_rules.actionable_count(out)
        if bad:
            self_bad[cid] = bad
        # 格式校验:Speaker 块结构与原稿一致(重新解析原文件比对)
        raw_t = transcript.parse(Path(next(r for r in master if r["canonical_id"] == cid)
                                      ["transcript_path"]).read_text(encoding="utf-8"))
        if len(t.blocks) != len(raw_t.blocks) or \
           [b.speaker for b in t.blocks] != [b.speaker for b in raw_t.blocks] or \
           [round(b.start_sec, 3) for b in t.blocks] != [round(b.start_sec, 3) for b in raw_t.blocks]:
            fmt_bad[cid] = "Speaker 块结构变化"
        per_course[cid]["number_unit_fixes"] = sum(
            1 for it in nu_items if it["course"] == cid and applied.get(id(it), (False,))[0])
    if self_bad:
        common.notify("camp-tutor step1 自检未过", json.dumps(self_bad, ensure_ascii=False)[:300])
    stats = {
        "run": run.name, "courses": len(prefix_texts),
        "dict_terms": len(dict_terms),
        "dict_variants": sum(len(t.get("variants") or []) for t in dict_terms),
        "per_course": per_course,
        "totals": {
            "seed_replacements": sum(sum(c.values()) for c in seed_counts.values()),
            "v1_replacements": sum(p["v1_replacements"] for p in per_course.values()),
            "number_unit_fixes": n_fixed,
        },
        "self_check": {
            "口径": "variant 出现处按守卫规则判定:应替换而未替换才计违规;守卫拦截处(合法词)单列 guarded_skips",
            "variants_checked": sum(len(t.get("variants") or []) for t in dict_terms),
            "violations": self_bad, "pass": not self_bad,
            "format_check": {"pass": not fmt_bad, "bad": fmt_bad},
        },
    }
    common.write_json(outdir / "cleaning_stats.json", stats)
    common.write_json(state_path, {"input_hash": ihash, "version": VERSION,
                                   "finished_at": time.strftime("%F %T")})
    logger.summary(phase="finalize", cleaned=len(prefix_texts), self_check=not self_bad,
                   format_ok=not fmt_bad, nu_fixed=n_fixed)
    logger.close(ok=not self_bad and not fmt_bad, outputs=str(outdir),
                 dict_terms=len(dict_terms))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        common.notify("camp-tutor step1 失败", str(e)[:300])
        raise
