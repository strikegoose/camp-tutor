"""逐字稿解析与序列化(学科无关)。
格式:首行 '日期|时长',空行,'Keywords:',随后若干 'Speaker N HH:MM:SS.mmm' 块。"""
import re
from dataclasses import dataclass, field

SPK_RE = re.compile(r"^Speaker\s+(\d+)\s+(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*$")


@dataclass
class Block:
    speaker: int
    start_sec: float
    text: str


@dataclass
class Transcript:
    header: str = ""
    keywords: str = ""
    blocks: list = field(default_factory=list)


def parse(text):
    lines = text.splitlines()
    t = Transcript()
    i = 0
    if lines:
        t.header = lines[0].strip()
        i = 1
    cur = None
    while i < len(lines):
        line = lines[i]
        m = SPK_RE.match(line.strip())
        if line.strip().startswith("Keywords:"):
            kws = []
            i += 1
            while i < len(lines) and not SPK_RE.match(lines[i].strip()):
                if lines[i].strip():
                    kws.append(lines[i].strip())
                i += 1
            t.keywords = " ".join(kws)
            continue
        if m:
            if cur:
                t.blocks.append(cur)
            h, mnt, s, ms = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
            cur = Block(speaker=int(m.group(1)), start_sec=h * 3600 + mnt * 60 + s + ms / 1000, text="")
        elif cur is not None and line.strip():
            cur.text += ("\n" if cur.text else "") + line.strip()
        i += 1
    if cur:
        t.blocks.append(cur)
    return t


def serialize(t):
    out = [t.header, "", "Keywords:", t.keywords, ""]
    for b in t.blocks:
        out.append(f"Speaker {b.speaker} {fmt_ts(b.start_sec)} ")
        out.append(b.text)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def fmt_ts(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def full_text(t):
    """全部口播文本(供 span 定位校验)。"""
    return "\n".join(b.text for b in t.blocks)


def text_window(t, start_sec, end_sec):
    return "\n".join(b.text for b in t.blocks if start_sec <= b.start_sec < end_sec)
