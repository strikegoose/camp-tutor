"""共享基础:环境加载、运行目录(版本化)、日志、token 台账、企微通知。
学科无关:不含任何课程/术语硬编码。"""
import json, os, subprocess, sys, time, datetime, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
CONFIG = ROOT / "config"
LATEST_LINK = DATA / "latest"

_env_loaded = False


def load_env():
    """加载项目 .env(不覆盖已存在的环境变量)。"""
    global _env_loaded
    if _env_loaded:
        return
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    _env_loaded = True


def env(key, default=None, required=False):
    load_env()
    v = os.environ.get(key, default)
    if required and not v:
        raise RuntimeError(f"环境变量 {key} 未配置(见 .env)")
    return v


def new_run_dir():
    """创建 data/runs/<ts>/ 版本目录并更新 data/latest 软链。"""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rd = DATA / "runs" / ts
    rd.mkdir(parents=True, exist_ok=False)
    if LATEST_LINK.is_symlink() or LATEST_LINK.exists():
        LATEST_LINK.unlink()
    LATEST_LINK.symlink_to(rd.relative_to(DATA))
    return rd


def get_run_dir():
    """取当前运行目录:CAMP_TUTOR_RUN_DIR 指定优先,否则 data/latest。"""
    override = os.environ.get("CAMP_TUTOR_RUN_DIR")
    if override:
        rd = Path(override)
        if not rd.is_absolute():
            rd = DATA / "runs" / override
        rd.mkdir(parents=True, exist_ok=True)
        return rd
    if LATEST_LINK.exists():
        return LATEST_LINK.resolve()
    return new_run_dir()


class StepLogger:
    """每步日志:控制台 + logs/<step>_<run>.log,记录输入输出摘要/耗时。"""

    def __init__(self, step):
        self.step = step
        self.t0 = time.time()
        LOGS.mkdir(exist_ok=True)
        run = get_run_dir().name
        self.path = LOGS / f"{step}_{run}.log"
        self.fh = open(self.path, "a", encoding="utf-8")
        self.log(f"=== {step} start {datetime.datetime.now():%F %T} run={run}")

    def log(self, msg):
        line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()

    def summary(self, **kv):
        self.log("SUMMARY " + json.dumps(kv, ensure_ascii=False, default=str))

    def close(self, ok=True, **kv):
        self.summary(status="OK" if ok else "FAIL", elapsed_s=round(time.time() - self.t0, 1), **kv)
        self.fh.close()


LEDGER = DATA / "token-ledger.jsonl"


def record_tokens(part, model, in_toks, out_toks, cached=False):
    DATA.mkdir(exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "part": part, "model": model,
            "input_tokens": in_toks, "output_tokens": out_toks,
            "cached": cached,
        }, ensure_ascii=False) + "\n")


def notify(subject, message):
    """失败路径通知企微:try 包裹,任何异常都不阻塞主流程,落本地日志。
    标题按 HQ 报警格式标准(2026-08-26 拍板):【级别】项目中文名·单号｜一句人话;开发期失败默认知悉。"""
    script = os.path.expanduser("~/Claude/scripts/notify-wecom.sh")
    subject = f"【知悉】训练营AI助教·20260826-01｜{subject}"
    LOGS.mkdir(exist_ok=True)
    try:
        r = subprocess.run([script, subject, message], capture_output=True, text=True, timeout=30)
        status = f"rc={r.returncode}"
    except Exception as e:  # noqa: BLE001 - 通知失败不得影响主流程
        status = f"exception={e!r}"
    with open(LOGS / "notify.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%F %T} | {subject} | {message} | {status}\n")
    return status


NOTIFY_STATE = DATA / "last_notify.json"


def notify_dedup(key, subject, message):
    """同 key 内容(标题+正文 JSON 序列化)与上次推送一致则跳过,变化才推(AIHQ 2026-08-27 走查①)。
    状态存 data/last_notify.json;告警语义不变,FAIL 仍推,同内容不重复推。"""
    state = {}
    if NOTIFY_STATE.exists():
        try:
            state = json.loads(NOTIFY_STATE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 状态文件损坏不阻塞告警
            state = {}
    sig = sha256_text(json.dumps({"s": subject, "m": message}, ensure_ascii=False, sort_keys=True))
    if state.get(key) == sig:
        with open(LOGS / "notify.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%F %T} | {subject} | {message} | dedup-skip(同 content 已推过)\n")
        return "dedup-skip"
    status = notify(subject, message)
    state[key] = sig
    try:
        NOTIFY_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 状态写失败不阻塞主流程
        pass
    return status


def sha256_text(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
