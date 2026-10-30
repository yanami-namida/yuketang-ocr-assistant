#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""雨课堂 HTTP 上传版：HTTP 获取课件/上传答案，WebSocket 接收课堂消息，无 DOM/浏览器依赖。"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

try:
    import requests
    from ykt_llm import ask_llm_blocking, clip, deep_merge, normalize_llm_answer, resolve_api_key
except ImportError as exc:
    raise SystemExit("缺少依赖，请运行 python -m pip install -r requirements-http.txt") from exc

# 雨课堂的随堂题正文是空的：题干和选项都画在幻灯片图片上（body=""、options[].value=""，
# 只给一份 bullets 坐标）。所以文本只能靠 OCR 那张 cover 图。
try:
    from ocr_win import OCR_ERRORS, ocr_boxes as _ocr_boxes, parse_ocr_question as _ocr_parse
except Exception:  # 非 Windows / 缺组件时退化为「只显示图片路径」
    _ocr_boxes = _ocr_parse = None
    OCR_ERRORS = []

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = {
    "duration_min": 45,
    "llm": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat",
            "api_key_env": "DEEPSEEK_API_KEY", "allow_dsh_credentials": False,
            "temperature": 0.2, "max_tokens": 600, "timeout": 60},
    "http": {"base": "https://changjiang.yuketang.cn", "lesson_id": "",
             "state_file": "ykt_state.json", "timeout": 15, "reconnect_delay": 3,
             "journal": "http_submissions.jsonl", "answers_file": "http_answers.jsonl",
             "min_confidence": 0.7,
             "type_map": {"1": "单选", "2": "多选", "3": "投票", "4": "填空", "5": "简答"}},
}


class ApiError(RuntimeError):
    pass


class AuthExpired(ApiError):
    pass


def local_path(value):
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


def log(message):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    print(line, flush=True)
    try:
        with (BASE_DIR / "ykt_http.log").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


def append_json(path, record):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    # 模型设置继承原版，旧 api 中猜测的端点/题型不参与 HTTP 版配置。
    for candidate in [BASE_DIR / "config.json", Path(path) if path else BASE_DIR / "config.http.json"]:
        if candidate.exists():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (ValueError, OSError) as exc:
                raise ApiError(f"配置文件无法读取：{candidate.name}") from exc
            if not isinstance(data, dict):
                raise ApiError(f"配置必须是 JSON 对象：{candidate.name}")
            cfg = deep_merge(cfg, data)
        elif path and candidate == Path(path):
            raise ApiError(f"配置文件不存在：{path}")
    return cfg


def validate_base(base):
    url = urlsplit(base)
    host = url.hostname or ""
    if (url.scheme != "https" or not (host == "yuketang.cn" or host.endswith(".yuketang.cn"))
            or url.username or url.password or url.query or url.fragment
            or url.path not in ("", "/") or url.port not in (None, 443)):
        raise ApiError("http.base 必须是雨课堂 HTTPS 根地址，例如 https://changjiang.yuketang.cn")
    return f"https://{host}"


def make_session(base, state_file):
    if not state_file.exists():
        raise ApiError("缺少登录态，请先运行 python ykt_watcher.py --login，或用 --state 指定 storage_state JSON")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ApiError("登录态文件无法读取，请重新登录") from exc
    session = requests.Session()
    host = urlsplit(base).hostname
    for cookie in state.get("cookies", []):
        domain = str(cookie.get("domain", ""))
        bare = domain.lstrip(".")
        if not (bare == host or domain.startswith(".") and host.endswith("." + bare)):
            continue
        expiry = cookie.get("expires", -1)
        expiry = int(expiry) if expiry is not None and expiry >= 0 else None
        if expiry is not None and expiry <= time.time():
            continue
        session.cookies.set(cookie["name"], cookie["value"], domain=domain,
                            path=cookie.get("path") or "/", secure=bool(cookie.get("secure")), expires=expiry)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36",
        "Accept": "application/json", "Origin": base, "Referer": base + "/web/",
        "xtbz": "ykt", "X-Client": "h5"})
    prepared = session.prepare_request(requests.Request("GET", base + "/api/v3/user/basic-info"))
    if "sessionid=" not in prepared.headers.get("Cookie", ""):
        session.close()
        raise AuthExpired("没有适用于当前站点的有效 sessionid，请重新登录或检查 http.base")
    return session


def successful(data):
    return isinstance(data, dict) and type(data.get("code")) in (int, str) and data["code"] in (0, "0")


class Client:
    def __init__(self, cfg, session=None):
        self.settings = cfg["http"]
        self.base = validate_base(self.settings["base"])
        self.timeout = float(self.settings["timeout"])
        self.session = session or make_session(self.base, local_path(self.settings["state_file"]))

    def request(self, method, route, **kwargs):
        url = self.base + route
        prepared = self.session.prepare_request(requests.Request("GET", url))
        csrf = re.search(r"(?:^|;\s*)csrftoken=([^;]+)", prepared.headers.get("Cookie", ""))
        headers = {"X-CSRFToken": csrf.group(1)} if csrf else {}
        try:
            return self.session.request(method, url, headers=headers, timeout=self.timeout,
                                        allow_redirects=False, **kwargs)
        except requests.RequestException as exc:
            raise ApiError(f"{method} {route.split('?')[0]} 网络异常：{type(exc).__name__}") from exc

    def checked(self, method, route, **kwargs):
        response = self.request(method, route, **kwargs)
        if response.status_code in (301, 302, 303, 307, 308, 401, 403):
            raise AuthExpired("登录态失效或接口重定向，请重新运行 ykt_watcher.py --login")
        if response.status_code != 200:
            raise ApiError(f"接口返回 HTTP {response.status_code}：{route}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ApiError("接口未返回 JSON，可能是登录失效或平台响应变化") from exc
        if not successful(data):
            raise ApiError(f"接口拒绝请求，code={data.get('code') if isinstance(data, dict) else '缺失'}")
        return data.get("data"), response

    def user(self):
        data, _ = self.checked("GET", "/api/v3/user/basic-info")
        if not isinstance(data, dict) or data.get("id") is None:
            raise ApiError("用户信息缺少 id")
        return data

    def lessons(self):
        data, _ = self.checked("GET", "/api/v3/classroom/on-lesson")
        if not isinstance(data, dict) or not isinstance(data.get("onLessonClassrooms"), list):
            raise ApiError("课程列表格式变化：缺少 onLessonClassrooms")
        return data["onLessonClassrooms"]

    def checkin(self, lesson_id):
        data, response = self.checked("POST", "/api/v3/lesson/checkin",
                                      json={"source": 5, "lessonId": lesson_id})
        if response.headers.get("Set-Auth"):
            self.session.headers["Authorization"] = "Bearer " + response.headers["Set-Auth"]
        token = data.get("lessonToken") if isinstance(data, dict) else None
        if not token:
            raise ApiError("进入课堂失败：响应中缺少 lessonToken")
        return token

    def presentation(self, presentation_id):
        data, _ = self.checked("GET", "/api/v3/lesson/presentation/fetch",
                               params={"presentation_id": str(presentation_id)})
        if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
            raise ApiError("课件响应缺少 slides")
        return data

    def submit(self, payload):
        # POST 只请求一次；超时、5xx、非 JSON 均不能证明服务端未写入。
        try:
            response = self.request("POST", "/api/v3/lesson/problem/answer", json=payload)
        except ApiError:
            return {"status": "unknown", "message": "上传网络异常，请到网页核对；不会自动重试"}
        if response.status_code != 200:
            return {"status": "rejected" if 400 <= response.status_code < 500 else "unknown",
                    "message": f"上传返回 HTTP {response.status_code}",
                    "auth_failed": response.status_code in (401, 403)}
        try:
            data = response.json()
        except ValueError:
            return {"status": "unknown", "message": "上传返回非 JSON，请到网页核对"}
        if not isinstance(data, dict) or "code" not in data:
            return {"status": "unknown", "message": "上传响应缺少 code，无法确认成功"}
        if successful(data):
            return {"status": "submitted", "message": "服务器确认答案已上传", "code": data["code"]}
        return {"status": "rejected", "message": "服务器拒绝答案", "code": data["code"]}


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "div"):
            self.parts.append(" ")
        if tag == "img":
            self.parts.append(dict(attrs).get("alt", ""))


def plain(value):
    parser = PlainText()
    parser.feed(str(value or ""))
    return re.sub(r"\s+", " ", unescape("".join(parser.parts))).strip()


def to_question(problem, cfg):
    pid, ptype = problem.get("problemId", problem.get("id")), problem.get("problemType")
    if pid is None or isinstance(ptype, bool):
        raise ApiError("题目缺少 problemId/problemType")
    try:
        ptype = int(ptype)
    except (ValueError, TypeError) as exc:
        raise ApiError("problemType 必须是整数") from exc
    options = []
    raw_options = problem.get("options") or problem.get("choices") or []
    if isinstance(raw_options, dict):
        raw_options = [{"key": k, "value": v} for k, v in raw_options.items()]
    if not isinstance(raw_options, list):
        raise ApiError("题目选项必须是列表或字典")
    for index, option in enumerate(raw_options):
        if isinstance(option, dict):
            label = str(option.get("key") or option.get("label") or chr(65 + index)).strip().upper()
            text = plain(option.get("content") or option.get("text") or option.get("value"))
        else:
            label, text = chr(65 + index), plain(option)
        options.append({"label": label, "text": text})
    body = next((problem[k] for k in ("body", "description", "title", "stem", "content")
                 if isinstance(problem.get(k), str) and problem[k].strip()), "")
    labels = [option["label"] for option in options]
    if len(set(labels)) != len(labels):
        raise ApiError("选项标签重复，无法可靠映射答案")
    blanks = problem.get("blanks") or []
    return {"problem_id": str(pid), "problem_type": ptype, "signature": str(pid),
            "kind": cfg["http"]["type_map"].get(str(ptype), "未知"),
            "stem": plain(body), "options": options, "countdown": None,
            "has_images": bool(re.search(r"<img\b", str(problem), re.I) or problem.get("images") or problem.get("image")),
            "blank_count": len(blanks) if isinstance(blanks, list) else 0,
            "answered": problem.get("result") is not None}


def answer_result(q, answer):
    if answer is None or isinstance(answer, (dict, bool)):
        raise ApiError("答案必须是文本或答案列表")
    kind = q["kind"]
    if kind in ("单选", "多选", "投票", "判断"):
        if isinstance(answer, list):
            picked = [str(item).strip().upper() for item in answer]
        else:
            value = str(answer).strip()
            if value in ("正确", "错误", "对", "错", "√", "×"):
                synonyms = {"正确", "对", "√", "true"} if value in ("正确", "对", "√") else {"错误", "错", "×", "false"}
                picked = [o["label"] for o in q["options"] if o["text"].lower() in synonyms]
            else:
                value = re.sub(r"[\s,，、;；]", "", value).upper()
                if not re.fullmatch(r"[A-Z]+", value):
                    raise ApiError("选择题答案必须是选项字母，例如 B 或 ACD")
                picked = list(value)
        labels = {o["label"] for o in q["options"]}
        if not picked or any(not re.fullmatch(r"[A-Z]", item) for item in picked):
            raise ApiError("答案为空或包含无效选项")
        if labels and not set(picked) <= labels:
            raise ApiError("答案包含不存在的选项")
        picked = sorted(set(picked))
        if kind != "多选" and len(picked) != 1:
            raise ApiError("此题只能提交一个选项")
        return picked
    if kind == "填空":
        values = answer if isinstance(answer, list) else re.split(r"[；;\n]", str(answer))
        values = [str(value).strip() for value in values]
        if not values or any(not value for value in values):
            raise ApiError("填空答案不能为空")
        if q.get("blank_count") and len(values) != q["blank_count"]:
            raise ApiError("答案数量与填空数量不符")
        return values
    if kind == "简答":
        if not isinstance(answer, str):
            raise ApiError("简答答案必须是文本")
        text = str(answer).strip()
        if not text:
            raise ApiError("简答答案不能为空")
        return {"content": text, "pics": []}
    raise ApiError("未知题型，请校准 http.type_map；不会猜测上传格式")


def make_payload(q, answer):
    if not q.get("problem_id"):
        raise ApiError("缺少题目 ID")
    return {"problemId": str(q["problem_id"]), "problemType": int(q["problem_type"]),
            "dt": int(time.time() * 1000), "result": answer_result(q, answer)}


class Journal:
    """写请求前持久化；重启后也不重发结果未知的请求。"""
    def __init__(self, path):
        self.path, self.entries = path, {}
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        record = json.loads(line)
                        self.entries[record["key"]] = record
            except (ValueError, KeyError, TypeError) as exc:
                raise ApiError("上传记录损坏，停止自动提交；请先核对 http_submissions.jsonl") from exc

    def upload(self, client, uid, lesson_id, payload):
        key = json.dumps([client.base, str(uid), str(lesson_id), payload["problemId"]], separators=(",", ":"))
        if key in self.entries:
            previous = self.entries[key]["status"]
            return {"status": "duplicate", "previous_status": previous,
                    "message": f"已有上传尝试记录（{previous}），跳过重复提交"}
        record = {"key": key, "time": datetime.now().isoformat(timespec="seconds"),
                  "status": "attempting", "payload": payload}
        append_json(self.path, record)  # 写盘失败时 POST 尚未发生。
        self.entries[key] = record
        result = client.submit(payload)
        record = {**record, **result}
        append_json(self.path, record)
        self.entries[key] = record
        return result


@contextmanager
def submission_lock(path):
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ApiError("另一个 HTTP 上传进程正在运行，请先关闭它") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def remaining(event):
    """probleminfo 的 limit/dt/now 均为毫秒；-1 表示不限时。"""
    try:
        limit = float(event["limit"])
        if limit == -1:
            return None
        seconds = (limit - (float(event["now"]) - float(event["dt"]))) / 1000
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError("题目时间信息不完整，暂不作答") from exc


class Monitor:
    def __init__(self, client, cfg, uid, lesson_id, args, journal=None):
        self.client, self.cfg, self.uid, self.lesson_id = client, cfg, uid, lesson_id
        self.args, self.journal = args, journal
        self.api_key = resolve_api_key(cfg, args.api_key) if not args.once else None
        self.cache, self.active, self.jobs, self.pending, self.done = {}, {}, {}, {}, set()
        self.raw_problems = []
        self.covers, self.ocr_cache = {}, {}
        self.ocr_dir = BASE_DIR / "shots"
        self.ocr_dir.mkdir(exist_ok=True)
        self.presentations, self.snapshot_waiting = set(), set()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.ws = None
        self.finished, self.got_hello = False, False
        self.msgid, self.connection = 0, 0

    def send(self, obj):
        self.ws.send(json.dumps(obj))

    def refresh(self, pid):
        if pid is None or str(pid) in ("", "0", "None"):
            return
        for slide in self.client.presentation(pid)["slides"]:
            raw = slide.get("problem") if isinstance(slide, dict) else None
            if isinstance(raw, dict):
                q = to_question(raw, self.cfg)
                self.cache[q["problem_id"]] = q
                # cover 是原图（png/jpeg），coverAlt 常被 CDN 转成 webp —— Windows OCR 不认 webp
                cover = slide.get("cover") or slide.get("coverAlt")
                if cover:
                    self.covers[q["problem_id"]] = cover
                if getattr(self.args, "dump_raw", False):
                    self.raw_problems.append(slide)
        self.presentations.add(str(pid))

    def query(self, pid):
        self.msgid += 1
        self.send({"op": "probleminfo", "lessonid": self.lesson_id,
                   "problemid": str(pid), "msgid": self.msgid})

    def hydrate(self, q):
        """题目文本在图片里：下载 cover 图 → Windows OCR → 补全 stem/options。"""
        if q.get("stem") and any(o["text"] for o in q.get("options") or []):
            return q
        pid = q["problem_id"]
        if pid in self.ocr_cache:
            return dict(q, **self.ocr_cache[pid])
        if _ocr_boxes is None or _ocr_parse is None or getattr(self.args, "no_ocr", False):
            return q
        cover = self.covers.get(pid)
        if not cover:
            return q
        try:
            url = cover.replace("format/webp", "format/png")
            resp = self.client.session.get(url, timeout=25)
            resp.raise_for_status()
            png = self.ocr_dir / f"http_{pid}.png"
            png.write_bytes(resp.content)
            boxes = _ocr_boxes(str(png))
            n_opt = len(q.get("options") or []) or None
            parsed = _ocr_parse(boxes, n_opt)
        except Exception as exc:
            log(f"题目图片识别失败 {pid}：{type(exc).__name__}: {clip(str(exc), 80)}")
            return q
        if not parsed or not parsed.get("stem"):
            extra = f"（{OCR_ERRORS[-1]}）" if OCR_ERRORS else ""
            log(f"题目 {pid} 的图片没辨认出文字{extra}")
            return q
        texts = [o.get("text") or "" for o in parsed["options"]]
        options = q.get("options") or []
        if texts and len(texts) >= len(options) >= 2:
            options = [dict(o, text=texts[i]) for i, o in enumerate(options)]
        elif texts:
            options = [{"label": chr(65 + i), "text": t} for i, t in enumerate(texts)]
        patch = {"stem": parsed["stem"], "options": options,
                 "kind": q["kind"] if q.get("kind") not in (None, "", "未知") else parsed["kind"],
                 "has_images": True, "ocr": True}
        self.ocr_cache[pid] = patch
        return dict(q, **patch)

    def receive(self, event):
        op = event.get("op")
        if op == "hello":
            self.got_hello = True
            ids = {str(item["pres"]) for item in event.get("timeline", [])
                   if isinstance(item, dict) and item.get("type") == "slide" and item.get("pres") is not None}
            if event.get("presentation") is not None:
                ids.add(str(event["presentation"]))
            for pid in ids:
                self.refresh(pid)
            self.snapshot_waiting = {str(pid) for pid in event.get("unlockedproblem", [])}
            for pid in self.snapshot_waiting.copy():
                self.query(pid)
            log(f"课堂连接成功，已缓存 {len(self.cache)} 道题，已发布 {len(self.snapshot_waiting)} 道题")
            if getattr(self.args, "dump_questions", False):
                if getattr(self.args, "dump_raw", False):
                    print(json.dumps(self.raw_problems, ensure_ascii=False, indent=2))
                else:
                    print(json.dumps([self.hydrate(q) for q in self.cache.values()],
                                     ensure_ascii=False, indent=2))
                self.finished = True
        elif op in ("presentationupdated", "presentationcreated"):
            self.refresh(event.get("presentation"))
        elif op == "unlockproblem":
            problem = event.get("problem") or {}
            self.refresh(problem.get("pres"))
            if problem.get("sid") is not None:
                # 统一查询带 now/dt 的 probleminfo，避免混用 unlockproblem 的时间单位。
                self.query(problem["sid"])
        elif op == "probleminfo":
            pid = str(event.get("problemid", ""))
            self.snapshot_waiting.discard(pid)
            if pid not in self.cache:
                for pres in self.presentations.copy():
                    self.refresh(pres)
            q = self.cache.get(pid)
            if not q:
                log(f"题目 {pid} 不在课件缓存中，等待课件更新")
                return
            cd = remaining(event)
            q = self.hydrate(dict(q, countdown=None if cd is None else int(cd)))
            self.active[pid] = {"question": q, "deadline": None if cd is None else time.monotonic() + cd,
                                "connection": self.connection}
            if cd == 0 or q["answered"] or event.get("result") is not None:
                self.done.add(pid)
                self.pending.pop(pid, None)
                return
            if self.args.once:
                return
            if pid in self.pending:
                self.finish_answer(q, self.pending.pop(pid))
            elif pid not in self.done and pid not in self.jobs:
                log(f"新题 [{q['kind']}] {pid}：{clip(q['stem'], 100)}")
                if self.api_key and (q["stem"] or any(o["text"] for o in q["options"])):
                    self.jobs[pid] = self.pool.submit(ask_llm_blocking, self.cfg, q, self.api_key)
                else:
                    log("未配置模型 Key 或缺少题目文本，仅展示题目")
                    self.done.add(pid)
        elif op == "lessonfinished":
            self.finished = True
            self.pending.clear()
            log("课堂已结束")

    def finish_answer(self, q, advice):
        pid = q["problem_id"]
        self.done.add(pid)
        active = self.active.get(pid)
        if not active or active["connection"] != self.connection or self.finished:
            return
        if active["deadline"] is not None and time.monotonic() >= active["deadline"]:
            log(f"题目 {pid} 已超时，跳过上传")
            return
        if q["kind"] in ("单选", "多选", "投票", "判断") and not q["options"]:
            log(f"题目 {pid} 缺少选项，跳过自动作答")
            return
        try:
            payload = make_payload(q, advice["answer"])
        except ApiError as exc:
            log(f"题目 {pid} 不上传：{exc}")
            return
        append_json(local_path(self.cfg["http"]["answers_file"]),
                    {"time": datetime.now().isoformat(), "lesson_id": self.lesson_id,
                     "question": q, "advice": advice, "payload": payload})
        log(f"AI 答案 {advice['answer']}，置信度 {advice['confidence']:.0%}：{advice['reason']}")
        if not self.args.auto_submit:
            log("HTTP 预览：" + json.dumps(payload, ensure_ascii=False))
        elif q["has_images"]:
            log("题目包含图片，当前为纯文本模型，跳过自动上传")
        elif advice["confidence"] < float(self.cfg["http"]["min_confidence"]):
            log("置信度不足，跳过自动上传")
        else:
            result = self.journal.upload(self.client, self.uid, self.lesson_id, payload)
            log(f"题目 {pid}：{result['message']} [{result['status']}]")
            if result.get("auth_failed"):
                raise AuthExpired("上传时登录态失效，请重新登录")

    def collect(self):
        for pid, future in list(self.jobs.items()):
            if not future.done():
                continue
            del self.jobs[pid]
            try:
                result = future.result()
                if not result.get("ok") or not isinstance(result.get("data"), dict):
                    log(f"题目 {pid} 模型调用失败，跳过")
                    self.done.add(pid)
                    continue
                q = self.cache[pid]
                if not math.isfinite(float(result["data"].get("confidence", 0.5))):
                    raise ApiError("模型置信度不是有限数值")
                advice = normalize_llm_answer(result["data"], q)
                # 先校验原答案，避免 normalize 将 ACX 过滤为 AC 后上传。
                answer_result(q, result["data"].get("answer", ""))
                advice["answer"] = result["data"].get("answer", "")
                self.pending[pid] = advice
                self.query(pid)  # 模型完成后重新确认题目未结束、未作答。
            except (ApiError, ValueError, TypeError, KeyError) as exc:
                log(f"题目 {pid} 答案无效：{type(exc).__name__}")
                self.done.add(pid)

    def run(self):
        try:
            import websocket
        except ImportError as exc:
            raise ApiError("缺少 websocket-client，请安装 requirements-http.txt") from exc
        until, failures = time.monotonic() + self.args.duration * 60, 0
        try:
            while time.monotonic() < until and not self.finished:
                try:
                    token = self.client.checkin(self.lesson_id)
                    cookie = self.client.session.prepare_request(requests.Request("GET", self.client.base + "/wsapp/")).headers.get("Cookie", "")
                    headers = {k: v for k, v in self.client.session.headers.items()
                               if k.lower() in ("authorization", "user-agent", "xtbz", "x-client")}
                    self.ws = websocket.create_connection(self.client.base.replace("https://", "wss://") + "/wsapp/",
                        header=headers, cookie=cookie, origin=self.client.base, timeout=self.client.timeout, redirect_limit=0)
                    self.ws.settimeout(1)
                    self.connection += 1
                    self.active.clear()
                    self.got_hello = False
                    self.send({"op": "hello", "userid": self.uid, "role": "student", "auth": token, "lessonid": self.lesson_id})
                    last_ping, started = time.monotonic(), time.monotonic()
                    while time.monotonic() < until and not self.finished:
                        try:
                            message = self.ws.recv()
                            if not message:
                                raise websocket.WebSocketConnectionClosedException()
                            event = json.loads(message)
                            if isinstance(event, dict):
                                self.receive(event)
                        except websocket.WebSocketTimeoutException:
                            pass
                        self.collect()
                        if self.args.once and self.got_hello and not self.snapshot_waiting:
                            print(json.dumps([a["question"] for a in self.active.values()], ensure_ascii=False, indent=2))
                            return
                        if (not self.got_hello or self.args.once) and time.monotonic() - started > self.client.timeout:
                            raise ApiError("课堂握手超时")
                        if time.monotonic() - last_ping > 20:
                            self.ws.ping()
                            last_ping = time.monotonic()
                except AuthExpired:
                    raise
                except (OSError, websocket.WebSocketException, ApiError, ValueError) as exc:
                    failures += 1
                    detail = str(exc) if isinstance(exc, ApiError) else type(exc).__name__
                    log(f"课堂连接中断：{detail}，准备重连")
                    if self.args.once and failures >= 3:
                        raise ApiError("连续三次无法读取课堂，请检查登录态、课堂 ID 和网络") from exc
                    self.active.clear()
                finally:
                    if self.ws:
                        self.ws.close()
                        self.ws = None
                delay = min(float(self.cfg["http"]["reconnect_delay"]) * min(failures, 5), 15, max(0, until-time.monotonic()))
                time.sleep(delay)
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", help="HTTP 配置，默认 config.http.json；模型配置继承 config.json")
    ap.add_argument("--state", help="Playwright storage_state JSON 路径")
    ap.add_argument("--base", help="站点根地址，例如 https://changjiang.yuketang.cn")
    ap.add_argument("--lesson-id", help="课堂 ID；未指定时仅在唯一课堂进行中时自动选择")
    ap.add_argument("--list-lessons", action="store_true", help="只读列出正在上课的课堂")
    ap.add_argument("--once", action="store_true", help="读取一次已发布题目，不调用模型、不上传")
    ap.add_argument("--dump-questions", action="store_true",
                    help="连上课堂后打印课件里的全部题目（含未发布的），用于核对解析结果")
    ap.add_argument("--dump-raw", action="store_true",
                    help="配合 --dump-questions：打印原始题目 JSON（看图片/坐标等字段）")
    ap.add_argument("--no-ocr", action="store_true",
                    help="不下载题目图片、不做 OCR（题目文本为空时只显示题号）")
    ap.add_argument("--duration", type=float, help="监控分钟数，默认 45")
    ap.add_argument("--auto-submit", action="store_true", help="启用真实 HTTP 上传，默认只预览")
    ap.add_argument("--problem-id", help="直接上传的题目 ID，配合 --problem-type 和 --answer")
    ap.add_argument("--problem-type", type=int, help="1 单选、2 多选、3 投票、4 填空、5 简答")
    ap.add_argument("--answer", help="单题答案，例如 B、ACD、第一空；第二空")
    ap.add_argument("--api-key", help="模型 API Key，优先使用环境变量")
    ap.add_argument("--model", help="覆盖模型名")
    ap.add_argument("--base-url", help="覆盖模型 OpenAI 兼容接口地址")
    return ap


def execute(cfg, args):
    client = Client(cfg)
    try:
        user = client.user()
        log("登录态有效")
        if args.list_lessons:
            lessons = client.lessons()
            if not lessons:
                log("当前没有正在上课的课堂")
            for lesson in lessons:
                print(f"lessonId={lesson.get('lessonId')}  {lesson.get('courseName', '')}")
            return
        lesson_id = args.lesson_id or cfg["http"].get("lesson_id")
        if not lesson_id:
            lessons = client.lessons()
            if len(lessons) != 1:
                raise ApiError(f"当前有 {len(lessons)} 个课堂，请用 --list-lessons 查看并通过 --lesson-id 指定")
            lesson_id = lessons[0]["lessonId"]
        lesson_id = str(lesson_id)
        if not lesson_id.isdigit():
            raise ApiError("课堂 ID 必须是数字")
        if args.auto_submit:
            journal_path = local_path(cfg["http"]["journal"])
            with submission_lock(journal_path.with_suffix(".lock")):
                journal = Journal(journal_path)
                if args.problem_id:
                    client.checkin(lesson_id)
                    q = to_question({"problemId": args.problem_id, "problemType": args.problem_type}, cfg)
                    result = journal.upload(client, user["id"], lesson_id, make_payload(q, args.answer))
                    log(result["message"] + " [" + result["status"] + "]")
                    confirmed = result["status"] == "submitted" or result.get("previous_status") == "submitted"
                    if not confirmed:
                        raise ApiError("未确认上传成功，请到网页核对")
                else:
                    log("HTTP 自动上传已开启")
                    Monitor(client, cfg, user["id"], lesson_id, args, journal).run()
        else:
            log("HTTP 预览模式：模型答案只打印，不上传")
            Monitor(client, cfg, user["id"], lesson_id, args).run()
    finally:
        client.session.close()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        for arg, field in ((args.state, "state_file"), (args.base, "base")):
            if arg is not None:
                cfg["http"][field] = arg
        for arg, field in ((args.model, "model"), (args.base_url, "base_url")):
            if arg is not None:
                cfg["llm"][field] = arg
        args.duration = cfg["duration_min"] if args.duration is None else args.duration
        for value, name in ((args.duration, "duration"), (cfg["http"]["timeout"], "http.timeout"),
                            (cfg["http"]["reconnect_delay"], "http.reconnect_delay")):
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ApiError(f"{name} 必须大于零且为有限数值")
        confidence = float(cfg["http"]["min_confidence"])
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise ApiError("http.min_confidence 必须介于 0 和 1")
        if args.once and (args.auto_submit or args.problem_id):
            raise ApiError("--once 仅用于读取，请移除上传参数")
        if args.list_lessons and (args.auto_submit or args.problem_id):
            raise ApiError("--list-lessons 不与上传参数组合")
        if args.problem_id is not None:
            if not args.problem_id.isdigit() or args.problem_type is None or args.answer is None:
                raise ApiError("直接上传需要数字 --problem-id、--problem-type 和 --answer")
            q = to_question({"problemId": args.problem_id, "problemType": args.problem_type}, cfg)
            payload = make_payload(q, args.answer)
            if not args.auto_submit:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 0
            if not (args.lesson_id or cfg["http"].get("lesson_id")):
                raise ApiError("真实单题上传必须明确指定 --lesson-id")
        elif args.answer is not None or args.problem_type is not None:
            raise ApiError("--answer / --problem-type 必须配合 --problem-id")
        execute(cfg, args)
        return 0
    except KeyboardInterrupt:
        log("已停止")
        return 130
    except (ApiError, OSError, ValueError, TypeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
