"""DeepSeek LLM 客户端(anthropic 兼容端点),URL/MODEL/KEY 全走环境变量。
特性:内容寻址缓存(幂等重跑零成本)、重试、token 台账、vision 支持。"""
import base64, json, time, urllib.request, urllib.error
from pathlib import Path

from . import common

CACHE_DIR = common.DATA / "cache" / "llm"
MAX_RETRIES = 4


def _endpoint():
    base = common.env("LLM_BASE_URL", required=True).rstrip("/")
    return base + "/v1/messages"


def _api_key():
    return common.env("DEEPSEEK_API_KEY", required=True)


def _post(payload, timeout=300):
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": _api_key(),
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:500]
            last_err = RuntimeError(f"HTTP {e.code}: {body}")
            if e.code in (400, 401, 403, 404, 422):
                raise last_err
        except Exception as e:  # noqa: BLE001
            last_err = e
        time.sleep(min(2 ** attempt * 2, 30))
    raise last_err


def _extract_text(resp):
    parts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _cached_call(part, model, payload, max_tokens):
    key_src = json.dumps({"model": model, "payload": payload}, ensure_ascii=False, sort_keys=True)
    key = common.sha256_text(key_src)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        resp = json.loads(cache_file.read_text(encoding="utf-8"))
        usage = resp.get("usage", {})
        common.record_tokens(part, model, usage.get("input_tokens", 0),
                             usage.get("output_tokens", 0), cached=True)
        return _extract_text(resp), True
    payload = dict(payload, model=model, max_tokens=max_tokens)
    resp = _post(payload)
    cache_file.write_text(json.dumps(resp, ensure_ascii=False), encoding="utf-8")
    usage = resp.get("usage", {})
    common.record_tokens(part, model, usage.get("input_tokens", 0),
                         usage.get("output_tokens", 0), cached=False)
    return _extract_text(resp), False


def chat(part, prompt, system=None, model=None, max_tokens=4096, temperature=0.0):
    """文本对话。part 用于 token 台账归件。返回 (text, from_cache)。"""
    model = model or common.env("LLM_MODEL", required=True)
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if system:
        payload["system"] = system
    return _cached_call(part, model, payload, max_tokens)


def vision(part, image_path, prompt, model=None, max_tokens=1024):
    """图片理解(粗筛分类等)。返回 (text, from_cache)。"""
    model = model or common.env("VISION_MODEL", required=True)
    p = Path(image_path)
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    suffix = p.suffix.lower().lstrip(".") or "jpeg"
    if suffix == "jpg":
        suffix = "jpeg"
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": f"image/{suffix}", "data": b64}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0.0,
    }
    return _cached_call(part, model, payload, max_tokens)


def parse_json(text):
    """宽容解析模型输出的 JSON(允许 ```json 包裹与首尾杂文本)。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.endswith("```"):
            t = t[: t.rfind("```")]
    start = min([i for i in (t.find("["), t.find("{")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"模型输出不含 JSON: {text[:200]}")
    t = t[start:]
    for end_char in ("]", "}"):
        idx = t.rfind(end_char)
        if idx > 0:
            try:
                return json.loads(t[: idx + 1])
            except json.JSONDecodeError:
                continue
    return json.loads(t)
