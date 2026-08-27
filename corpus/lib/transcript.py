"""逐字稿解析与序列化(学科无关)。
20260826-03(T2):实现已抽取至 corpus-hub 公共组件(源文件逐字一致),
本文件为兼容 shim,camp-tutor 各步骤行为不变。"""
import os, sys
from pathlib import Path

_HUB = Path(os.path.expanduser(os.environ.get("CORPUS_HUB", "~/Claude/projects/corpus-hub")))
if str(_HUB) not in sys.path:
    sys.path.insert(0, str(_HUB))

from corpus.lib.transcript import Block, Transcript, fmt_ts, full_text, parse, serialize, text_window  # noqa: E402,F401
