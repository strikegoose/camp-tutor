#!/usr/bin/env python3
"""Step4 向量检索库:chunk(带 course/讲师/时间戳 metadata)→ 阿里 DashScope embedding 入库 + BM25 索引;
2 课 recall@k 实验(语义段 vs 固定窗口),出切分策略结论。
学科无关;embedding 走 OpenAI 兼容端点,URL/MODEL/KEY 全环境变量。"""
import json, math, os, re, sys, time, urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common, transcript as T  # noqa: E402

import yaml  # noqa: E402

EMB_CACHE = common.DATA / "cache" / "emb"
BATCH = 10
FIXED_WIN, FIXED_OVERLAP = 500, 100
SEM_MIN, SEM_MAX = 300, 700


# ---------- embedding ----------

def embed_batch(texts, part="step4_vector"):
    """调 DashScope embedding,带内容寻址缓存。返回 [vector,...]。"""
    base = common.env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    key = common.env("DASHSCOPE_API_KEY", required=True)
    model = common.env("DASHSCOPE_EMBED_MODEL") or common.env("EMBEDDING_MODEL", "text-embedding-v4")
    out = [None] * len(texts)
    todo = []
    EMB_CACHE.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(texts):
        cf = EMB_CACHE / f"{common.sha256_text(model + '|' + t)}.json"
        if cf.exists():
            out[i] = json.loads(cf.read_text())
        else:
            todo.append((i, t, cf))
    for s in range(0, len(todo), BATCH):
        batch = todo[s:s + BATCH]
        payload = {"model": model, "input": [b[1] for b in batch]}
        req = urllib.request.Request(
            base + "/embeddings", data=json.dumps(payload).encode(),
            headers={"content-type": "application/json", "authorization": f"Bearer {key}"},
            method="POST")
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    r = json.loads(resp.read().decode())
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(min(2 ** attempt * 2, 30))
        else:
            raise last
        for item, (i, t, cf) in zip(sorted(r["data"], key=lambda d: d["index"]), batch):
            vec = item["embedding"]
            out[i] = vec
            cf.write_text(json.dumps(vec))
        common.record_tokens(part, model, r.get("usage", {}).get("total_tokens", 0), 0, cached=False)
        time.sleep(0.2)
    return out


# ---------- chunking ----------

def chunk_fixed(blocks):
    """固定窗口:按字符 500/重叠 100,时间戳取覆盖块区间。"""
    chunks, cur, cur_blocks = [], "", []
    for b in blocks:
        cur_blocks.append(b)
        cur += b.text + "\n"
        while len(cur) >= FIXED_WIN:
            head = cur[:FIXED_WIN]
            used = []
            acc = 0
            for bb in cur_blocks:
                acc += len(bb.text) + 1
                used.append(bb)
                if acc >= FIXED_WIN:
                    break
            chunks.append((used, head))
            cur = cur[FIXED_WIN - FIXED_OVERLAP:]
            cur_blocks = used[-2:] if len(used) > 2 else used
    if cur.strip() and cur_blocks:
        chunks.append((cur_blocks, cur))
    return chunks


def chunk_semantic(blocks):
    """语义段:以 Speaker 切换与长度为界聚合整块,300~700 字。"""
    chunks, cur, cur_text = [], [], ""
    def flush():
        if cur and cur_text.strip():
            chunks.append((list(cur), cur_text))
    for b in blocks:
        if cur and (b.speaker != cur[-1].speaker and len(cur_text) >= SEM_MIN or len(cur_text) >= SEM_MAX):
            flush()
            cur, cur_text = [], ""
        cur.append(b)
        cur_text += b.text + "\n"
    flush()
    return chunks


def to_records(chunks, course, strategy):
    recs = []
    for i, (blocks, text) in enumerate(chunks):
        recs.append({
            "chunk_id": f"{course['canonical_id']}-{strategy}-{i:04d}",
            "course_id": course["canonical_id"], "camp": course["camp"],
            "instructor": course.get("instructor", ""), "title": course.get("title", ""),
            "start_sec": blocks[0].start_sec, "end_sec": blocks[-1].start_sec,
            "strategy": strategy, "text": text.strip(),
        })
    return recs


# ---------- BM25(字符 bigram,学科无关) ----------

def bigrams(text):
    t = re.sub(r"\s+", "", text)
    return [t[i:i + 2] for i in range(len(t) - 1)] or [t]


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(bigrams(d)) for d in docs]
        self.dl = [sum(c.values()) for c in self.tf]
        self.avgdl = sum(self.dl) / max(1, len(self.dl))
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        self.N = len(docs)
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def score(self, query):
        q = Counter(bigrams(query))
        scores = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in q:
                if t not in tf:
                    continue
                f = tf[t]
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl))
            scores.append(s)
        return scores


def topk(scores, k):
    return sorted(range(len(scores)), key=lambda i: -scores[i])[:k]


# ---------- 主流程 ----------

def main():
    logger = common.StepLogger("step4_vector")
    run = common.get_run_dir()
    outdir = run / "step4"
    outdir.mkdir(parents=True, exist_ok=True)

    if not common.env("DASHSCOPE_API_KEY"):
        common.write_json(outdir / "status.json", {"status": "BLOCKED", "reason": "DASHSCOPE_API_KEY 缺失"})
        common.notify("camp-tutor step4 BLOCKED", "DASHSCOPE_API_KEY 缺失,向量库未建")
        logger.close(ok=False, status="BLOCKED")
        return

    master = common.read_json(run / "step0" / "courses_master.json")
    cleaned_dir = run / "step1" / "cleaned"

    # 1) 两种策略切 chunk(全量 44 课)
    all_chunks = {"fixed": [], "semantic": []}
    missing = []
    for course in master:
        cid = course["canonical_id"]
        cfile = cleaned_dir / f"{cid}.txt"
        src = cfile if cfile.exists() else Path(course["transcript_path"])
        if not src.exists():
            missing.append(cid)
            continue
        t = T.parse(src.read_text(encoding="utf-8"))
        all_chunks["fixed"].extend(to_records(chunk_fixed(t.blocks), course, "fixed"))
        all_chunks["semantic"].extend(to_records(chunk_semantic(t.blocks), course, "semantic"))
    if missing:
        common.notify(f"step4 本轮 {len(missing)} 课缺稿:{'/'.join(missing)}",
                      "缺稿课节跳过 chunk,明细见日志")
    logger.log(f"chunk 完成: fixed={len(all_chunks['fixed'])} semantic={len(all_chunks['semantic'])}")

    # 2) recall@k 实验:抽 2 课,以知识卡片 quote 为 query(答案=含该 quote 的 chunk)
    cards_file = run / "step2" / "cards.jsonl"
    experiment = {}
    exp_courses = []
    if cards_file.exists():
        cards = [json.loads(l) for l in cards_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        by_course = {}
        for c in cards:
            by_course.setdefault(c["course_id"], []).append(c)
        exp_courses = sorted(by_course, key=lambda k: -len(by_course[k]))[:2]
    for cid in exp_courses:
        queries = [c for c in by_course[cid] if c.get("quote") and c.get("content")][:30]
        experiment[cid] = {"queries": len(queries)}
        for strategy in ("fixed", "semantic"):
            chunks = [c for c in all_chunks[strategy] if c["course_id"] == cid]
            vecs = embed_batch([c["text"] for c in chunks])
            import numpy as np
            M = np.array(vecs)
            M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
            bm = BM25([c["text"] for c in chunks])
            hits_vec = hits_bm = hits_hybrid = 0
            for card in queries:
                qv = embed_batch([card["content"]])[0]
                qv = np.array(qv)
                qv = qv / (np.linalg.norm(qv) + 1e-9)
                sims = (M @ qv).tolist()
                truth = [i for i, c in enumerate(chunks) if card["quote"][:60] in c["text"]]
                if not truth:
                    continue
                t0 = truth[0]
                v_rank = topk(sims, 5)
                b_rank = topk(bm.score(card["content"]), 5)
                # hybrid:向量+BM25 分数各自归一后相加
                bs = bm.score(card["content"])
                def norm(x):
                    mx = max(x) or 1.0
                    return [v / mx for v in x]
                hy = [a + b for a, b in zip(norm(sims), norm(bs))]
                h_rank = topk(hy, 5)
                hits_vec += t0 in v_rank
                hits_bm += t0 in b_rank
                hits_hybrid += t0 in h_rank
            n = len(queries)
            experiment[cid][strategy] = {
                "chunks": len(chunks),
                "recall@5_vector": round(hits_vec / n, 3),
                "recall@5_bm25": round(hits_bm / n, 3),
                "recall@5_hybrid": round(hits_hybrid / n, 3),
            }
        logger.log(f"recall 实验 {cid}: {experiment[cid]}")

    # 3) 选策略:hybrid 召回为主,chunk 粒度取两策略中实验召回高者
    chosen = "semantic"
    if experiment:
        agg = {s: sum(experiment[c][s]["recall@5_hybrid"] for c in experiment) / len(experiment)
               for s in ("fixed", "semantic")}
        chosen = max(agg, key=agg.get)
    logger.log(f"选定切分策略: {chosen}")

    # 4) 全量入库(选定策略):chunks.jsonl + vectors.npy + bm25 语料
    final = all_chunks[chosen]
    vecs = embed_batch([c["text"] for c in final])
    import numpy as np
    np.save(outdir / "vectors.npy", np.array(vecs, dtype="float32"))
    with open(outdir / "chunks.jsonl", "w", encoding="utf-8") as f:
        for c in final:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    common.write_json(outdir / "bm25_corpus.json",
                      {"tokenizer": "char-bigram", "k1": 1.5, "b": 0.75,
                       "chunk_ids": [c["chunk_id"] for c in final]})
    common.write_json(outdir / "index_meta.json",
                      {"strategy": chosen, "chunks": len(final), "dim": len(vecs[0]) if vecs else 0,
                       "metadata_fields": ["chunk_id", "course_id", "camp", "instructor",
                                           "title", "start_sec", "end_sec", "strategy", "text"]})

    # 5) 实验报告
    rep = ["# recall@k 切分策略实验报告", "", f"- 运行: {run.name}",
           f"- 实验课: {', '.join(experiment) if experiment else '无(cards 未就绪)'}",
           "- query: 各课知识卡片 content(断言改写,语义查询,≤30 条/课);正解=包含该卡 quote 原文的 chunk",
           "- 指标: recall@5(vector / BM25 / hybrid)", ""]
    if experiment:
        rep += ["| 课程 | 策略 | chunk 数 | vec | bm25 | hybrid |", "|---|---|---|---|---|---|"]
        for cid, exp in experiment.items():
            for s in ("fixed", "semantic"):
                e = exp[s]
                rep.append(f"| {cid} | {s} | {e['chunks']} | {e['recall@5_vector']} "
                           f"| {e['recall@5_bm25']} | {e['recall@5_hybrid']} |")
    rep += ["", f"## 结论", "",
            f"选定切分策略:**{chosen}**(fixed=500字/重叠100;semantic=Speaker 边界聚合 300~700 字)。",
            "两策略召回持平时取 fixed 的平局裁决依据:窗口时间戳粒度更细且均匀,分钟级引文定位更稳;",
            "检索采用 hybrid(向量余弦 + BM25 归一化加和),依据为上表 hybrid 列召回。",
            "embedding: DashScope text-embedding-v4(环境变量配置);BM25: 字符 bigram。"]
    (outdir / "recall_report.md").write_text("\n".join(rep), encoding="utf-8")

    logger.close(ok=True, strategy=chosen, chunks=len(final),
                 experiment_courses=list(experiment), outputs=str(outdir))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        common.notify("camp-tutor step4 失败", str(e)[:300])
        raise
