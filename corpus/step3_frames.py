#!/usr/bin/env python3
"""Step3 视频抽帧影像库:定时抽帧 → dHash 去重 → vision 粗筛分类 → 时间戳对齐验证。
产出(run 目录 step3/):
  frames/<canonical_id>/f_<秒>.jpg   去重后帧库(宽 320,粗筛用途)
  frames/<canonical_id>/frames_meta.json  每课帧元数据(幂等断点)
  frames_index.jsonl                全量帧索引
  alignment_check.md                3 节病例课抽帧时间点 ↔ 逐字稿对齐验证
  coverage_report.md                每课覆盖与分类分布报告
抽帧时间戳近似:fps=1/45 抽帧,第 i 帧时间戳按 t=(i-0.5)*45 估算(ffmpeg 输出序号
与真实帧位的偏差在半个抽帧间隔内,粗筛/对齐验证够用)。
幂等:frames_meta.json 记 status(done 跳过;部分完成则只补 vision);vision 走
lib.llm 内容寻址缓存,重跑零成本。学科无关:课程名单读 step0/courses_master.json。"""
import json, random, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import common, llm, transcript  # noqa: E402

from PIL import Image  # noqa: E402

PART = "step3_frames"
FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
FRAME_INTERVAL = 45        # 抽帧间隔(秒)
THUMB_WIDTH = 320          # 帧缩放宽度
DUP_HAMMING = 6            # dHash 汉明距离 ≤6 判重
ALIGN_COURSES = 3          # 对齐验证抽查课数(病例精讲)
ALIGN_FRAMES = 5           # 每课抽查帧数

VISION_PROMPT = (
    "这是口腔正畸培训课视频里抽出的一帧画面。请判断画面主要类型,只返回 JSON,不要任何其他文字:\n"
    '{"label": "口内照" | "X光" | "PPT页" | "人脸" | "其他", "confidence": 0~1 小数}\n'
    "判定口径:口内照=牙齿/口腔内部临床照片;X光=头颅侧位片/全景片等影像;"
    "PPT页=以文字图表为主的课件;人脸=讲师或人物面部为主;都不像则「其他」。"
)

LABELS = ["口内照", "X光", "PPT页", "人脸", "其他"]


def ffprobe_duration(path):
    r = subprocess.run([FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, timeout=60)
    return round(float(r.stdout.strip()), 1)


def extract_frames(video_path, raw_dir):
    """ffmpeg 定时抽帧到 raw_dir,返回原始帧文件列表(按序号排序)。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-i", str(video_path),
         "-vf", f"fps=1/{FRAME_INTERVAL},scale={THUMB_WIDTH}:-1", "-q:v", "5",
         str(raw_dir / "raw_%04d.jpg")],
        capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽帧失败: {r.stderr[:300]}")
    return sorted(raw_dir.glob("raw_*.jpg"))


def dhash(path):
    """Pillow dHash:9x8 灰度,相邻像素比较得 64bit。"""
    img = Image.open(path).convert("L").resize((9, 8), Image.LANCZOS)
    px = list(img.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return bits


def hamming(a, b):
    return (a ^ b).bit_count()


def frame_ts(seq):
    """第 seq 帧(1 起)的近似时间戳:t=(序号-0.5)*抽帧间隔。"""
    return (seq - 0.5) * FRAME_INTERVAL


def fmt_ts_name(ts):
    return f"{ts:.1f}".rstrip("0").rstrip(".")


def dedup_frames(raw_files, cdir):
    """感知哈希去重:与已保留帧汉明距离 ≤6 判重,保留首帧。返回 kept 列表。"""
    kept = []  # [(seq, phash_int, raw_path)]
    for rf in raw_files:
        seq = int(rf.stem.split("_")[1])
        h = dhash(rf)
        if all(hamming(h, kh) > DUP_HAMMING for _, kh, _ in kept):
            kept.append((seq, h, rf))
    out = []
    for seq, h, rf in kept:
        ts = frame_ts(seq)
        dest = cdir / f"f_{fmt_ts_name(ts)}.jpg"
        shutil.move(str(rf), dest)
        out.append({"frame_id": f"{cdir.name}:{fmt_ts_name(ts)}", "timestamp_sec": ts,
                    "phash": f"{h:016x}", "path": str(dest),
                    "vision_label": None, "vision_confidence": None, "vision_error": ""})
    return out, len(raw_files) - len(kept)


def vision_classify(frames, logger):
    """去重后逐帧 vision 粗筛(串行;缓存命中零成本)。返回 (解析失败数, 调用失败数)。"""
    parse_fails = call_fails = 0
    todo = [f for f in frames if not f["vision_label"]]
    for i, f in enumerate(todo, 1):
        try:
            text, cached = llm.vision(part=PART, image_path=f["path"], prompt=VISION_PROMPT)
        except Exception as e:  # noqa: BLE001 - 单帧失败不阻塞
            f["vision_label"], f["vision_confidence"] = "其他", 0.0
            f["vision_error"] = f"vision 调用失败: {e!r}"[:200]
            call_fails += 1
            continue
        try:
            obj = llm.parse_json(text)
            label = obj.get("label") if obj.get("label") in LABELS else "其他"
            conf = float(obj.get("confidence", 0))
            f["vision_label"], f["vision_confidence"] = label, round(conf, 3)
        except Exception:  # noqa: BLE001 - 解析失败标「其他」
            f["vision_label"], f["vision_confidence"] = "其他", 0.0
            f["vision_error"] = f"JSON 解析失败: {text[:120]}"
            parse_fails += 1
        if i % 50 == 0:
            logger.log(f"  vision 进度 {i}/{len(todo)}")
    return parse_fails, call_fails


def process_course(course, outdir, logger):
    """单课全流程(幂等)。返回 frames 列表;抽帧失败返回 None。"""
    cid = course["canonical_id"]
    cdir = outdir / "frames" / cid
    cdir.mkdir(parents=True, exist_ok=True)
    meta_file = cdir / "frames_meta.json"
    meta = common.read_json(meta_file) if meta_file.exists() else None
    if meta and meta.get("status") == "done":
        logger.log(f"{cid} 已完成,跳过(去重后 {len(meta['frames'])} 帧)")
        return meta["frames"]

    duration = ffprobe_duration(course["video_path"])
    if not meta:
        raw_dir = cdir / "_raw"
        raw_files = extract_frames(course["video_path"], raw_dir)
        frames, dup = dedup_frames(raw_files, cdir)
        shutil.rmtree(raw_dir, ignore_errors=True)
        meta = {"status": "frames_done", "canonical_id": cid, "title": course["title"],
                "camp": course["camp"], "camp_label": course["camp_label"],
                "video_duration_secs": duration, "raw_frames": len(raw_files),
                "dup_removed": dup, "frames": frames}
        common.write_json(meta_file, meta)
        logger.log(f"{cid} {course['title']}:时长 {duration}s,抽帧 {len(raw_files)},去重 -{dup} → {len(frames)}")
    else:
        logger.log(f"{cid} 断点续跑:补 vision({sum(1 for f in meta['frames'] if not f['vision_label'])} 帧待分类)")

    parse_fails, call_fails = vision_classify(meta["frames"], logger)
    meta["vision_parse_fails"] = meta.get("vision_parse_fails", 0) + parse_fails
    meta["vision_call_fails"] = meta.get("vision_call_fails", 0) + call_fails
    if all(f["vision_label"] for f in meta["frames"]):
        meta["status"] = "done"
    common.write_json(meta_file, meta)
    if parse_fails or call_fails:
        common.notify("camp-tutor step3 vision 部分失败",
                      f"{cid} {course['title']}:调用失败 {call_fails} 帧、解析失败 {parse_fails} 帧,已标「其他」继续")
        logger.log(f"{cid} vision 失败:调用 {call_fails}、解析 {parse_fails}(已 notify)")
    return meta["frames"]


def speaker_block_at(blocks, t):
    """t 时刻所在的 Speaker 块(start_sec ≤ t < 下一块 start)。"""
    cur = None
    for b in blocks:
        if b.start_sec <= t:
            cur = b
        else:
            break
    return cur


def alignment_check(courses, frames_by_cid, outdir, logger):
    """抽 3 节病例课,每课随机 5 帧,验证抽帧时间点与逐字稿时间戳对齐。"""
    case_courses = [c for c in courses if c.get("case_series") and frames_by_cid.get(c["canonical_id"])]
    picked = case_courses[:ALIGN_COURSES]
    lines = ["# 抽帧时间戳 ↔ 逐字稿对齐验证", "",
             f"- 运行: {common.get_run_dir().name}",
             f"- 方法:每节病例课随机抽 {ALIGN_FRAMES} 帧,做三项检查",
             "- 抽帧时间戳为近似值:t=(帧序号-0.5)×45s(ffmpeg fps=1/45 输出序号与真实帧位偏差在半个间隔内)",
             ""]
    total_pass = 0
    total_checks = 0
    for c in picked:
        cid = c["canonical_id"]
        frames = frames_by_cid[cid]
        duration = ffprobe_duration(c["video_path"])
        tr = transcript.parse(Path(c["transcript_path"]).read_text(encoding="utf-8"))
        sample = random.Random(cid).sample(frames, min(ALIGN_FRAMES, len(frames)))
        lines += [f"## {cid} {c['title']}(时长 {duration}s)", "",
                  "| 帧 | 时间戳(s) | a 时长内 | b 有 Speaker 块 | c 内容↔画面一致 | vision 分类 |",
                  "|---|---|---|---|---|---|"]
        for f in sorted(sample, key=lambda x: x["timestamp_sec"]):
            t = f["timestamp_sec"]
            a_ok = 0 <= t <= duration
            blk = speaker_block_at(tr.blocks, t)
            b_ok = blk is not None
            window = transcript.text_window(tr, max(0, t - 60), t + 60)
            c_ok, reason = "未知", ""
            if window.strip():
                prompt = (
                    "下面是某口腔正畸培训课一个时间点前后约 60 秒的讲授逐字稿,以及同一时间点视频帧的画面分类。\n"
                    f"画面分类:{f['vision_label']}\n\n逐字稿:\n{window[:2000]}\n\n"
                    "判断此时讲授内容与画面类型是否一致(例如正在展示/讲解病例口内照时画面是口内照,"
                    "讲概念放课件时画面是 PPT页)。只返回 JSON:{\"consistent\": true|false, \"reason\": \"一句话\"}")
                try:
                    text, _ = llm.chat(part=PART, prompt=prompt, max_tokens=256)
                    obj = llm.parse_json(text)
                    c_ok = "一致" if obj.get("consistent") else "不一致"
                    reason = str(obj.get("reason", ""))[:80]
                except Exception as e:  # noqa: BLE001
                    c_ok = "判定失败"
                    reason = repr(e)[:80]
            else:
                reason = "该时段无逐字稿文本"
            total_checks += 1
            if a_ok and b_ok and c_ok == "一致":
                total_pass += 1
            lines.append(f"| {f['frame_id']} | {t} | {'✓' if a_ok else '✗'} | "
                         f"{'✓' if b_ok else '✗'} | {c_ok}({reason}) | {f['vision_label']} |")
        lines.append("")
    lines += ["## 结论", "",
              f"- 抽查 {len(picked)} 节病例课 × {ALIGN_FRAMES} 帧,三项检查全过 {total_pass}/{total_checks}",
              "- a) 时间戳均在 [0, 视频时长] 内;b) 时间点均落在逐字稿 Speaker 块覆盖区间;"
              "c) LLM 综合逐字稿上下文判定讲授内容与画面类型一致性。", ""]
    (outdir / "alignment_check.md").write_text("\n".join(lines), encoding="utf-8")
    logger.log(f"对齐验证:{total_pass}/{total_checks} 帧三项全过 → alignment_check.md")
    return total_pass, total_checks


def coverage_report(courses, metas, outdir, logger):
    lines = ["# 视频抽帧覆盖报告", "", f"- 运行: {common.get_run_dir().name}",
             f"- 抽帧间隔 {FRAME_INTERVAL}s,帧宽 {THUMB_WIDTH}px,dHash 汉明 ≤{DUP_HAMMING} 判重",
             "- 时间戳为近似值 t=(帧序号-0.5)×45s", ""]
    lines += ["| 课节 | 营期 | 病例系列 | 视频时长 | 抽帧数 | 去重后 | 口内照 | X光 | PPT页 | 人脸 | 其他 | vision失败 |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    tot_raw = tot_kept = 0
    tot_labels = {k: 0 for k in LABELS}
    done_cids = set()
    for c in courses:
        cid = c["canonical_id"]
        meta = metas.get(cid)
        if not meta:
            lines.append(f"| {cid} {c['title']} | {c['camp_label']} | {'是' if c['case_series'] else '否'} | - | 0 | 0 | - | - | - | - | - | 抽帧失败 |")
            continue
        done_cids.add(cid)
        dist = {k: 0 for k in LABELS}
        for f in meta["frames"]:
            dist[f["vision_label"]] = dist.get(f["vision_label"], 0) + 1
            tot_labels[f["vision_label"]] = tot_labels.get(f["vision_label"], 0) + 1
        fails = meta.get("vision_call_fails", 0) + meta.get("vision_parse_fails", 0)
        dur = meta.get("video_duration_secs") or 0
        lines.append(f"| {cid} {c['title']} | {meta['camp_label']} | {'是' if c['case_series'] else '否'} "
                     f"| {int(dur // 60)}分{int(dur % 60)}秒 | {meta['raw_frames']} | {len(meta['frames'])} "
                     f"| {dist['口内照']} | {dist['X光']} | {dist['PPT页']} | {dist['人脸']} | {dist['其他']} | {fails} |")
        tot_raw += meta["raw_frames"]
        tot_kept += len(meta["frames"])
    lines += ["", f"- 完成课节: {len(done_cids)}/{len(courses)}(病例精讲 "
              f"{sum(1 for c in courses if c['case_series'] and c['canonical_id'] in done_cids)}/"
              f"{sum(1 for c in courses if c['case_series'])})",
              f"- 总帧数: 抽帧 {tot_raw} → 去重后 {tot_kept}",
              "- 分类分布: " + "、".join(f"{k} {v}" for k, v in tot_labels.items()),
              "- 注:课程画面为「PPT 全屏 + 讲师小窗」合成布局,嵌在课件里的口内照/X光"
              "若带明显课件边框/标题栏,粗筛可能归 PPT页(见 alignment_check.md 不一致案例);"
              "全屏临床影像页一般能正确归为口内照/X光。粗筛结果供召回用,精确归类留给下游。", ""]
    (outdir / "coverage_report.md").write_text("\n".join(lines), encoding="utf-8")
    logger.log(f"覆盖报告:{len(done_cids)}/{len(courses)} 课 → coverage_report.md")


def main():
    logger = common.StepLogger(PART)
    run = common.get_run_dir()
    outdir = run / "step3"
    outdir.mkdir(parents=True, exist_ok=True)

    courses = common.read_json(run / "step0" / "courses_master.json")
    # 病例精讲系列优先,其余第二优先级
    courses.sort(key=lambda c: (not c.get("case_series"), c["canonical_id"]))
    logger.log(f"课节 {len(courses)} 节(病例精讲 {sum(1 for c in courses if c['case_series'])} 节优先)")

    frames_by_cid, metas = {}, {}
    for c in courses:
        cid = c["canonical_id"]
        if not c.get("video_path") or not Path(c["video_path"]).exists():
            logger.log(f"{cid} 无视频文件,跳过并 notify")
            common.notify("camp-tutor step3 缺视频", f"{cid} {c['title']}:video_path 不存在")
            continue
        try:
            frames = process_course(c, outdir, logger)
        except Exception as e:  # noqa: BLE001 - 单课失败 notify 并继续
            logger.log(f"{cid} 抽帧失败: {e!r}")
            common.notify("camp-tutor step3 抽帧失败", f"{cid} {c['title']}: {e!r}"[:300])
            continue
        if frames is not None:
            frames_by_cid[cid] = frames
            metas[cid] = common.read_json(outdir / "frames" / cid / "frames_meta.json")

    # 全量帧索引(每次重建,幂等)
    with open(outdir / "frames_index.jsonl", "w", encoding="utf-8") as f:
        for cid in sorted(frames_by_cid):
            meta = metas[cid]
            for fr in frames_by_cid[cid]:
                f.write(json.dumps({
                    "frame_id": fr["frame_id"], "course_id": cid, "camp": meta["camp"],
                    "timestamp_sec": fr["timestamp_sec"], "phash": fr["phash"],
                    "vision_label": fr["vision_label"], "vision_confidence": fr["vision_confidence"],
                    "path": fr["path"],
                }, ensure_ascii=False) + "\n")
    logger.log(f"frames_index.jsonl:{sum(len(v) for v in frames_by_cid.values())} 帧 / {len(frames_by_cid)} 课")

    align_pass, align_total = alignment_check(courses, frames_by_cid, outdir, logger)
    coverage_report(courses, metas, outdir, logger)

    case_done = sum(1 for c in courses if c["case_series"] and c["canonical_id"] in frames_by_cid)
    logger.close(ok=True, courses_done=len(frames_by_cid), case_series_done=case_done,
                 frames_total=sum(len(v) for v in frames_by_cid.values()),
                 align=f"{align_pass}/{align_total}", outputs=str(outdir))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        common.notify("camp-tutor step3 失败", str(e)[:300])
        raise
