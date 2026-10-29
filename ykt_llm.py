# -*- coding: utf-8 -*-
"""DOM 版与 HTTP 版共用的纯文本模型调用；无需浏览器依赖。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def mmss(seconds: int | None) -> str:
    if seconds is None:
        return "未知"
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


SYSTEM_PROMPT = """你是一名严谨、务实的大学课程助教。用户会给你一道课堂题的题型、题干和选项（来自雨课堂，可能有 OCR/排版噪音或缺少图片）。

请独立判断正确答案，只输出一个 JSON 对象，不要输出 JSON 以外的任何文字：
{"answer":"B","confidence":0.85,"reason":"不超过60字的判断依据"}

规则：
1. 单选题：answer 为一个选项字母，如 "B"。
2. 多选题：answer 为按字母升序拼接的字母串，如 "ACD"（至少两个）。
3. 判断题：answer 为 "正确" 或 "错误"。
4. 填空题/简答题：answer 为尽量简短的答案文本（分空用 "；" 分隔）。
5. 如果题干信息不足以判断（例如关键公式是图片、选项被截断），把 confidence 压到 0.4 以下，并在 reason 里直接说明缺什么；不要编造。
6. 不要输出解析过程、不要输出选项原文，reason 只写最关键的判断依据。
"""


def load_dsh_key(key_name: str = "DEEPSEEK_API_KEY") -> str | None:
    """从 DSH 的凭据文件里读取 key（本机已登录 DSH 时可直接复用）。"""
    path = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh" / ".credentials.yaml"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m = re.search(rf"{re.escape(key_name)}\s*:\s*['\"]?([A-Za-z0-9_\-\.]+)", text)
    if not m:
        return None
    return m.group(1)


def resolve_api_key(cfg: dict, explicit: str | None) -> str | None:
    if explicit:
        return explicit.strip()
    llm = cfg["llm"]
    env_name = llm.get("api_key_env") or "DEEPSEEK_API_KEY"
    key = os.environ.get(env_name)
    if key:
        return key.strip()
    if llm.get("allow_dsh_credentials", True):
        return load_dsh_key(env_name)
    return None


def build_user_prompt(q: dict) -> str:
    lines = [f"题型：{q['kind']}"]
    if q.get("countdown") is not None:
        lines.append(f"剩余作答时间：约 {mmss(q['countdown'])}")
    lines.append("题干：")
    lines.append(q["stem"] or "（题干为空，仅有选项）")
    if q["options"]:
        lines.append("选项：")
        for o in q["options"]:
            lines.append(f"{o['label']}. {o['text']}")
    else:
        lines.append("选项：无（填空/主观题）")
    if q.get("has_images"):
        lines.append("注意：题目中还包含图片/公式，纯文本可能不完整。")
    if q.get("ocr"):
        lines.append("说明：上面的文字是系统 OCR 从题目图片里识别出来的，可能有错字、多字或空格，"
                     "请按最合理的意思理解；选项字母是按图片里的顺序补的。")
    return "\n".join(lines)


def parse_llm_json(content: str) -> dict:
    content = (content or "").strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S).strip()
    try:
        return json.loads(content)
    except Exception:
        pass
    m = re.search(r"\{.*\}", content, flags=re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # 最次：从文本里抠答案
    m = re.search(r"[Aa]nswer\s*[:：]\s*[\"']?([A-H正确错误]+)", content)
    return {"answer": m.group(1) if m else "", "confidence": 0.3, "reason": clip(content, 80)}


def ask_llm_blocking(cfg: dict, q: dict, api_key: str) -> dict:
    llm = cfg["llm"]
    base = llm["base_url"].rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": llm["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(q)},
        ],
        "temperature": llm.get("temperature", 0.2),
        "max_tokens": llm.get("max_tokens", 600),
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_exc = None
    for attempt in range(2):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=llm.get("timeout", 60))
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            return {"ok": True, "data": parse_llm_json(content), "raw": content}
        except Exception as exc:  # 网络抖动重试一次
            last_exc = exc
            time.sleep(1.2)
    return {"ok": False, "error": str(last_exc)}


def normalize_llm_answer(data: dict, q: dict) -> dict:
    answer = str(data.get("answer", "") or "").strip()
    try:
        confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    reason = clip(str(data.get("reason", "")), 120)

    kind = q["kind"]
    labels = [o["label"] for o in q["options"]]
    selected: list[str] = []

    if kind in ("单选", "多选", "投票") or (labels and re.fullmatch(r"[A-Ha-h ,，、;；]+", answer or "")):
        letters = re.findall(r"[A-Ha-h]", answer.upper())
        picked = [l for l in dict.fromkeys(letters) if not labels or l in labels]
        if picked:
            selected = sorted(picked)
            answer = "".join(selected)
        elif kind == "单选" and answer:
            answer = answer.upper()[:1]
    elif kind == "判断":
        if re.search(r"正确|对|√", answer):
            answer = "正确"
        elif re.search(r"错误|错|×", answer):
            answer = "错误"
        if labels:
            selected = [o["label"] for o in q["options"] if o["text"][:1] in answer] or []
    elif kind in ("填空", "简答"):
        selected = []

    if not selected and answer and labels:
        # 模型答的是选项原文而不是字母
        for o in q["options"]:
            if o["text"] and (answer == o["text"] or answer.startswith(o["text"][:6])):
                selected = [o["label"]]
                answer = o["label"]
                break

    answer_text = "；".join(
        o["text"] for o in q["options"] if o["label"] in selected
    )
    return {
        "answer": answer,
        "answer_text": answer_text,
        "selected": selected,
        "confidence": confidence,
        "reason": reason,
    }
