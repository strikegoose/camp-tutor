#!/usr/bin/env python3
"""Step0 三向对账:小鹅通课节(courses+store_lives) ↔ NAS 视频 ↔ NAS 逐字稿。
产出:canonical 课节主表(CSV+JSON)、缺口分级清单(A 缺逐字稿/B 缺视频/C 标题时长不符)、
learning_records 覆盖度报告。学科无关:名单与营期窗口读 config/courses.yaml。
匹配纪律:候选课节按营期窗口过滤;序号(一)(二)参与比对;全局 1:1 分配(一分货物只配一节)。"""
import csv, difflib, os, re, subprocess, sys, unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common  # noqa: E402

import yaml  # noqa: E402

NAS_CORPUS = Path(os.path.expanduser("~/NAS-数据仓/转写语料"))
NAS_VIDEO = Path(os.path.expanduser("~/NAS-视频/003-课程视频/49天合学早矫精进陪伴营课程"))
FFPROBE = "/opt/homebrew/bin/ffprobe"
MATCH_THRESHOLD = 0.55
LIVE_THRESHOLD = 0.75
DURATION_TOL = 0.05  # 视频时长与 meta 时长偏差容忍

FRANKLIN_PY = r'''
import sqlite3, json
con = sqlite3.connect("file:%s?mode=ro", uri=True)
con.row_factory = sqlite3.Row
out = {}
out["courses"] = [dict(r) for r in con.execute(
    "select goods_id, goods_name, resource_type, price_low, sell_num, sale_status, created_at from courses")]
out["store_lives"] = [dict(r) for r in con.execute(
    "select live_id, title, zb_start_at, zb_stop_at, alive_state from store_lives")]
out["lr_by_goods"] = [dict(r) for r in con.execute(
    "select goods_id, count(*) as records, count(distinct user_id) as users, "
    "sum(case when is_finish=1 then 1 else 0 end) as finishes from learning_records group by goods_id")]
print(json.dumps(out, ensure_ascii=False))
'''


def franklin_snapshot(logger):
    key = os.path.expanduser(common.env("FRANKLIN_SSH_KEY", required=True))
    host = common.env("FRANKLIN_HOST", required=True)
    db = common.env("FRANKLIN_DB", required=True)
    py = FRANKLIN_PY % db
    b64 = __import__("base64").b64encode(py.encode("utf-8")).decode("ascii")
    cmd = ["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
           host, f"echo {b64} | base64 -d | sudo python3 -"]
    logger.log("拉取富兰克林只读快照(courses/store_lives/learning_records 聚合)...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"富兰克林快照失败: {r.stderr[:500]}")
    return r.stdout


def normalize(s):
    """归一化标题:去【】、去课号前缀、括号内容保留(序号参与比对)、合字族统一。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("𬌗", "合").replace("（", "(").replace("）", ")")
    s = re.sub(r"【[^】]*】", "", s)
    s = re.sub(r"^第[一二三四五六七八九十百0-9]+课[:：]?\s*", "", s)
    s = re.sub(r"^[一二三四五六七八九十0-9]+[、.]\s*", "", s)
    s = re.sub(r"^[\u4e00-\u9fff]{2,4}-(?=[^\d])", "", s)
    s = s.replace("(", "").replace(")", "")  # 括号去掉但保留内容(一)/(二)/软组织等参与比对
    s = re.sub(r"[\s,，、:：\-—_]+", "", s)
    return s.lower()


def similarity(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na in nb or nb in na:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def in_window(date_str, window):
    return bool(date_str) and window[0] <= date_str <= window[1]


def ffprobe_duration(path):
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
        return round(float(r.stdout.strip()), 1)
    except Exception:
        return None


def assign_1to1(pairs, threshold):
    """pairs: [(score, roster_cid, candidate_id)] 贪心全局 1:1 分配。"""
    assignment = {}
    used = set()
    for score, cid, gid in sorted(pairs, key=lambda p: -p[0]):
        if score < threshold:
            break
        if cid in assignment or gid in used:
            continue
        assignment[cid] = (gid, score)
        used.add(gid)
    return assignment


def main():
    logger = common.StepLogger("step0_reconcile")
    run = common.get_run_dir()
    outdir = run / "step0"
    outdir.mkdir(parents=True, exist_ok=True)

    cfg = yaml.safe_load((common.CONFIG / "courses.yaml").read_text(encoding="utf-8"))
    roster = cfg["courses"]
    camps = cfg["camps"]

    # 1) 富兰克林快照(同一次运行内复用)
    snap_file = outdir / "franklin_snapshot.json"
    if snap_file.exists():
        snap = common.read_json(snap_file)
    else:
        snap = __import__("json").loads(franklin_snapshot(logger))
        common.write_json(snap_file, snap)
    logger.log(f"快照: courses={len(snap['courses'])} store_lives={len(snap['store_lives'])} lr_goods={len(snap['lr_by_goods'])}")

    # 2) NAS 扫描
    nas_rows = {}
    for c in roster:
        camp = camps[c["camp"]]
        tdir = NAS_CORPUS / camp["nas_dir"] / c["dir_name"]
        meta_file = tdir / "meta.json"
        meta = common.read_json(meta_file) if meta_file.exists() else {}
        transcript = tdir / "transcript.txt"
        vpath = NAS_VIDEO / camp["video_dir"] / (meta.get("video_name") or (c["dir_name"] + ".mp4"))
        row = {
            "canonical_id": c["canonical_id"], "camp": c["camp"], "camp_label": camp["label"],
            "title": c["title"], "instructor": c.get("instructor", ""),
            "case_series": c.get("case_series", False),
            "transcript_path": str(transcript) if transcript.exists() else "",
            "transcript_chars": transcript.stat().st_size if transcript.exists() else 0,
            "meta_duration_secs": meta.get("duration_secs"),
            "video_path": str(vpath) if vpath.exists() else "",
            "video_duration_secs": None,
        }
        if row["video_path"]:
            row["video_duration_secs"] = ffprobe_duration(vpath)
        nas_rows[c["canonical_id"]] = row
    logger.log(f"NAS 扫描完成: {len(nas_rows)} 节")

    # 3) 小鹅通候选(按营期窗口过滤)+ 全局 1:1 匹配
    lesson_goods = [g for g in snap["courses"] if g.get("resource_type") in (3, 4, 50)]
    camp_goods = [g for g in snap["courses"] if "49天" in (g.get("goods_name") or "")
                  and g.get("resource_type") == 7]
    lives = snap["store_lives"]

    goods_pairs = []
    for c in roster:
        win = camps[c["camp"]]["xiaoe_window"]
        cands = [g for g in lesson_goods if in_window((g.get("created_at") or "")[:10], win)]
        for g in cands:
            sc = similarity(c["title"], g.get("goods_name"))
            if sc >= 0.3:
                goods_pairs.append((sc, c["canonical_id"], g["goods_id"]))
    goods_assign = assign_1to1(goods_pairs, MATCH_THRESHOLD)
    goods_by_id = {g["goods_id"]: g for g in lesson_goods}

    live_pairs = []
    for c in roster:
        win = camps[c["camp"]]["xiaoe_window"]
        cands = [l for l in lives if in_window((l.get("zb_start_at") or "")[:10], win)]
        for l in cands:
            sc = similarity(c["title"], l.get("title"))
            if sc >= 0.5:
                live_pairs.append((sc, c["canonical_id"], l["live_id"]))
    live_assign = assign_1to1(live_pairs, LIVE_THRESHOLD)
    lives_by_id = {l["live_id"]: l for l in lives}

    for cid, row in nas_rows.items():
        if cid in goods_assign:
            gid, gs = goods_assign[cid]
            g = goods_by_id[gid]
            row.update(goods_id=gid, goods_name=g["goods_name"],
                       上架日=g.get("created_at", ""), match_score=round(gs, 3))
        else:
            row.update(goods_id="", goods_name="", 上架日="", match_score=0)
        if cid in live_assign:
            lid, ls = live_assign[cid]
            row.update(live_id=lid, 直播时间=lives_by_id[lid].get("zb_start_at", ""),
                       live_match_score=round(ls, 3))
        else:
            row.update(live_id="", 直播时间="", live_match_score=0)

    # 4) 对账状态与缺口分级
    gaps = {"A": [], "B": [], "C": []}
    for cid, row in nas_rows.items():
        status = []
        if not row["transcript_path"]:
            status.append("缺逐字稿")
            gaps["A"].append({"canonical_id": cid, "title": row["title"], "camp": row["camp_label"],
                              "detail": "NAS 无 transcript.txt"})
        if not row["video_path"]:
            status.append("缺视频")
            gaps["B"].append({"canonical_id": cid, "title": row["title"], "camp": row["camp_label"],
                              "detail": "NAS 视频目录无对应 mp4"})
        if row["meta_duration_secs"] and row["video_duration_secs"]:
            dev = abs(row["video_duration_secs"] - row["meta_duration_secs"]) / row["meta_duration_secs"]
            row["duration_deviation"] = round(dev, 4)
            if dev > DURATION_TOL:
                status.append("时长不符")
                gaps["C"].append({"canonical_id": cid, "title": row["title"], "camp": row["camp_label"],
                                  "detail": f"meta {row['meta_duration_secs']}s vs 视频 {row['video_duration_secs']}s (偏差 {dev:.1%})"})
        if row["goods_id"] and row["match_score"] < 0.99:
            status.append(f"标题差异(平台名「{row['goods_name']}」)")
            gaps["C"].append({"canonical_id": cid, "title": row["title"], "camp": row["camp_label"],
                              "detail": f"NAS 名与平台名「{row['goods_name']}」相似度 {row['match_score']}"})
        if not row["goods_id"]:
            status.append("小鹅通窗口内无匹配课节")
        row["对账状态"] = ";".join(status) if status else "OK"

    # 窗口内平台课节未匹配到 NAS 逐字稿 → A 级(平台有课、NAS 缺逐字稿)
    # 过滤:仅视频/课程类(3/50);标题须与名单某课有主题相关度(≥0.35)防他营课混入;按归一化标题去重
    matched_goods = {gid for gid, _ in goods_assign.values()}
    roster_titles = [c["title"] for c in roster]
    dedup = {}
    for camp_id, camp in camps.items():
        win = camp["xiaoe_window"]
        for g in lesson_goods:
            if g.get("resource_type") not in (3, 50):
                continue
            if g["goods_id"] in matched_goods:
                continue
            name = g.get("goods_name") or ""
            if not in_window((g.get("created_at") or "")[:10], win):
                continue
            if not re.match(r"^第[一二三四五六七八九十百0-9]+课", unicodedata.normalize("NFKC", name)):
                continue
            topical = max(similarity(name, t) for t in roster_titles)
            if topical < 0.35:
                continue
            key = normalize(name)
            if key not in dedup:
                dedup[key] = {"canonical_id": "", "title": name, "camp": f"小鹅通({camp['label']}窗口)",
                              "goods_ids": [g["goods_id"]], "created": g.get("created_at", ""),
                              "topical": round(topical, 2)}
            else:
                dedup[key]["goods_ids"].append(g["goods_id"])
    for d in dedup.values():
        d["detail"] = (f"goods_id={','.join(d['goods_ids'])} 上架 {d['created']},"
                       f"与名单最近似标题相似度 {d['topical']},NAS 无对应逐字稿")
        gaps["A"].append(d)

    # 5) 主表落盘
    fields = [("canonical_id", "canonical_id"), ("camp_label", "营期"), ("title", "课名"),
              ("instructor", "讲师"), ("video_path", "视频路径"), ("transcript_path", "逐字稿路径"),
              ("goods_id", "goods_id"), ("上架日", "上架日"), ("直播时间", "直播时间"),
              ("对账状态", "对账状态"), ("match_score", "小鹅通匹配分"), ("live_match_score", "直播匹配分")]
    with open(outdir / "courses_master.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([cn for _, cn in fields])
        for cid in sorted(nas_rows):
            w.writerow([nas_rows[cid].get(k, "") for k, _ in fields])
    common.write_json(outdir / "courses_master.json", [nas_rows[cid] for cid in sorted(nas_rows)])

    # 6) 缺口分级清单(人读友好,供创始人签字处置)
    lines = ["# 三向对账·缺口分级清单", "",
             f"- 运行: {run.name}", f"- 名单课节: {len(nas_rows)}(一营 20 + 二营 24)",
             f"- A 级(缺逐字稿): {len(gaps['A'])} 条 | B 级(缺视频): {len(gaps['B'])} 条 | C 级(标题/时长不符): {len(gaps['C'])} 条",
             "", "每行可处置:创始人确认后标注「补录/忽略/修正」即可。", ""]
    for level, label in [("A", "缺逐字稿"), ("B", "缺视频"), ("C", "标题/时长不符")]:
        lines += [f"## {level} 级:{label}", "",
                  "| # | 课节 | 营期/来源 | 情况 | 处置(补录/忽略/修正) | 签字 |",
                  "|---|------|----------|------|--------------------|------|"]
        for i, g in enumerate(gaps[level], 1):
            who = f"{g['canonical_id']} {g['title']}" if g["canonical_id"] else g["title"]
            lines.append(f"| {i} | {who} | {g['camp']} | {g['detail']} |  |  |")
        if not gaps[level]:
            lines.append("| - | 无 | - | - | - | - |")
        lines.append("")
    (outdir / "gaps.md").write_text("\n".join(lines), encoding="utf-8")

    # 7) learning_records 覆盖度报告
    lr = {r["goods_id"]: r for r in snap["lr_by_goods"]}
    rep = ["# learning_records 覆盖度报告", "", f"- 运行: {run.name}",
           f"- 小鹅通两营容器课(资源类型 7 且名称含「49天」): {len(camp_goods)} 个", ""]
    rep += ["| 容器课 goods_id | 名称 | 记录数 | 覆盖学员数 | 完成记录 |", "|---|---|---|---|---|"]
    for g in camp_goods:
        r = lr.get(g["goods_id"], {})
        rep.append(f"| {g['goods_id']} | {g['goods_name']} | {r.get('records', 0)} | {r.get('users', 0)} | {r.get('finishes', 0)} |")
    rep += ["", "| 匹配课节 | goods_id | 记录数 | 覆盖学员数 |", "|---|---|---|---|"]
    covered = 0
    for cid in sorted(nas_rows):
        row = nas_rows[cid]
        r = lr.get(row["goods_id"], {}) if row["goods_id"] else {}
        if r.get("records"):
            covered += 1
        rep.append(f"| {cid} {row['title']} | {row['goods_id'] or '-'} | {r.get('records', 0)} | {r.get('users', 0)} |")
    rep += ["", f"- 有学习记录的课节: {covered}/{len(nas_rows)}",
            "- 注:小鹅通学习记录按 goods 粒度;训练营实际学习行为多记在容器课(c_)上,课节级(v_/l_)记录偏少属平台机制,供参考。"]
    (outdir / "learning_records_report.md").write_text("\n".join(rep), encoding="utf-8")

    ok = sum(1 for r in nas_rows.values() if r["对账状态"] == "OK")
    logger.close(ok=True, courses=len(nas_rows), fully_ok=ok,
                 matched_goods=len(goods_assign), matched_lives=len(live_assign),
                 gaps_A=len(gaps["A"]), gaps_B=len(gaps["B"]), gaps_C=len(gaps["C"]),
                 outputs=str(outdir))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        common.notify("camp-tutor step0 失败", str(e)[:300])
        raise
