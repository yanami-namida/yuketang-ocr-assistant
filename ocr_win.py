"""用 Windows 自带 OCR（Windows.Media.Ocr）读雨课堂的图片题。

雨课堂的随堂题目在网页上往往是一张 PPT 图片（qn-*.yuketang.cn），
DOM 里没有题干和选项文字，所以先用系统 OCR 转成文本，再交给大模型。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from ykt_paths import data_dir

BASE_DIR = data_dir()
PS1 = BASE_DIR / "win_ocr.ps1"
# 明确用 64 位 PowerShell（32 位进程里 WinRT OCR 不可用）
PS_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                      "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
if not os.path.exists(PS_EXE):
    PS_EXE = "powershell.exe"

PS1_CODE = r"""param([Parameter(Mandatory=$true)][string]$Path)

# 强制 UTF-8 输出。父进程如果在 GBK 控制台（cmd 936）里启动，PowerShell 默认
# 会按控制台代码页写 stdout，Python 侧再按 UTF-8 解码就会把中文整段丢掉，
# 只剩 A/B 这类 ASCII 字母——2026-10-29 两题漏抓就是这个原因。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType=WindowsRuntime] | Out-Null

# 不走 StorageFile（某些目录会被 WinRT 拒之门外），直接把文件字节读进内存流再解码
$bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $Path).Path)
$stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
$writer = [Windows.Storage.Streams.DataWriter]::new($stream)
$writer.WriteBytes($bytes)
Await ($writer.StoreAsync()) ([uint32]) | Out-Null
$writer.DetachStream() | Out-Null
$stream.Seek(0)
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) {
    $lang = [Windows.Globalization.Language]::new("zh-Hans-CN")
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
}
if ($null -eq $engine) { Write-Output "__NO_OCR_ENGINE__"; exit 3 }
Write-Output ("__ENGINE__" + $engine.RecognizerLanguage.LanguageTag)

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
# 逐词输出：行号<TAB>x<TAB>y<TAB>宽<TAB>高<TAB>文字
# 给出词级坐标，Python 侧才能按间距判断「L oa d」这些其实是一个单词
$li = 0
foreach ($line in $result.Lines) {
    foreach ($w in $line.Words) {
        $r = $w.BoundingRect
        $t = ($w.Text -replace "[`t`r`n]", " ")
        Write-Output ("{0}`t{1}`t{2}`t{3}`t{4}`t{5}" -f $li, [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height, $t)
    }
    $li++
}
"""

CJK = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
# 单字之间被 OCR 插进来的空格：中文之间、中文与标点之间都要删掉
_CJK_SPACE = re.compile(rf"(?<=[{CJK}，。、；：？！（）《》])[ \t]+|(?<=[ \t])[{CJK}]")
_SPACE_CHARS = re.compile(rf"(?<=[{CJK}])[ \t]+(?=[{CJK}])")

KIND_WORDS = (
    ("多选", "多选"), ("单选", "单选"), ("不定项", "多选"), ("判断", "判断"),
    ("填空", "填空"), ("简答", "简答"), ("问答", "简答"), ("投票", "投票"),
)


def ensure_ps1() -> Path:
    PS1.parent.mkdir(parents=True, exist_ok=True)
    if not PS1.exists() or PS1.read_text(encoding="utf-8", errors="ignore") != PS1_CODE:
        PS1.write_text(PS1_CODE, encoding="utf-8")
    return PS1


def clean_line(text: str) -> str:
    """去掉中文之间被 OCR 插入的空格，修几个常见误识。"""
    s = (text or "").replace("\u3000", " ").strip()
    s = _SPACE_CHARS.sub("", s)
    s = re.sub(rf"(?<=[{CJK}])\s+(?=[A-Za-z0-9])", "", s)
    s = re.sub(rf"(?<=[A-Za-z0-9])\s+(?=[{CJK}])", "", s)
    s = re.sub(r"\blnternet\b|\blnternet", "Internet", s)
    s = re.sub(r"Li\s*n\s*u\s*[x×]", "Linux", s)
    s = re.sub(r"\s+([，。、；：？！）》)])", r"\1", s)
    s = re.sub(r"([（(])\s+", r"\1", s)
    # "L oa d" / "L D R" 这类被 OCR 拆开的英文单词：把单字母碎片粘回去
    for _ in range(2):
        s = re.sub(r"\b([A-Za-z])\s+([A-Za-z]{1,})\b", r"\1\2", s)
    s = fix_ocr_text(s)
    s = re.sub(r"\s{2,}", " ", s)
    return s.strip()


def ocr_boxes(image_path: str | Path, timeout: int = 90) -> list[dict]:
    """调 PowerShell 走 Windows OCR，返回 [{text,x,y,w,h}]（失败返回空列表）。"""
    global OCR_ENGINE
    if sys.platform != "win32":
        return []
    path = Path(image_path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    ensure_ps1()
    try:
        res = subprocess.run(
            [PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(PS1), "-Path", str(path)],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        OCR_ERRORS.append(f"{type(exc).__name__}: {exc}")
        return []
    out = _decode_ps_output(res.stdout)
    if "__NO_OCR_ENGINE__" in out:
        OCR_ERRORS.append("没有可用的 OCR 引擎")
        return []
    lines_text = []
    for raw in out.splitlines():
        if raw.startswith("__ENGINE__"):
            OCR_ENGINE = raw[len("__ENGINE__"):].strip()
            continue
        lines_text.append(raw)
    # 按行号聚合词，再按词间距拼回整行
    lines: dict[int, list[dict]] = {}
    for raw in lines_text:
        parts = raw.split("\t")
        if len(parts) < 6:
            if raw.strip():
                OCR_ERRORS.append(f"OCR 输出格式异常：{raw[:80]}")
            continue
        try:
            idx = int(parts[0])
            word = {"text": parts[5].strip(), "x": int(parts[1]), "y": int(parts[2]),
                    "w": int(parts[3]), "h": int(parts[4])}
        except ValueError:
            continue
        if word["text"]:
            lines.setdefault(idx, []).append(word)
    boxes = []
    for idx in sorted(lines):
        words = sorted(lines[idx], key=lambda z: z["x"])
        h = max((w["h"] for w in words), default=20)
        text = _join_words(words, h)
        if not text:
            continue
        x = min(w["x"] for w in words)
        y = min(w["y"] for w in words)
        right = max(w["x"] + w["w"] for w in words)
        bottom = max(w["y"] + w["h"] for w in words)
        boxes.append({"text": text, "x": x, "y": y, "w": right - x, "h": bottom - y})
    if not boxes and res.stderr:
        err = _decode_ps_output(res.stderr).strip()
        if err:
            OCR_ERRORS.append(err.splitlines()[0][:200])
    return boxes


def _decode_ps_output(raw: bytes) -> str:
    """PowerShell 的 stdout 可能是 UTF-8（脚本已强制），也可能是控制台代码页
    （GBK）或 UTF-16；按可能性依次尝试，别让解码把中文整段吞掉。"""
    raw = raw or b""
    if not raw:
        return ""
    if b"\x00" in raw:
        for enc in ("utf-16-le", "utf-16"):
            try:
                return raw.decode(enc).lstrip("\ufeff")
            except UnicodeDecodeError:
                pass
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore").replace("\ufeff", "")


def _join_words(words: list[dict], h: int) -> str:
    """词与词之间：间距大于行高 0.4 倍且不是中文时才补空格。"""
    out = ""
    prev = None
    for w in words:
        if prev is not None:
            gap = w["x"] - (prev["x"] + prev["w"])
            cjk = bool(re.search(rf"[{CJK}]", w["text"])) or bool(re.search(rf"[{CJK}]", prev["text"]))
            if gap > 0.4 * max(h, 1) and not cjk:
                out += " "
        out += w["text"]
        prev = w
    return out


OCR_ERRORS: list[str] = []
# 最近一次 OCR 实际用的识别语言（判断是否误用了英文引擎）
OCR_ENGINE = ""


def ocr_lines(image_path: str | Path, timeout: int = 90) -> list[str]:
    """只要每行文字（兼容旧调用）。"""
    return [b["text"] for b in ocr_boxes(image_path, timeout)]


def merge_line_boxes(boxes: list[dict], gap_ratio: float = 0.5) -> list[dict]:
    """把同一水平行上相邻的方块并回一行（OCR 有时把中文按字拆开返回）。"""
    if not boxes:
        return []
    rows = sorted(boxes, key=lambda b: (b["y"], b["x"]))
    lines: list[list[dict]] = []
    for b in rows:
        h = b.get("h") or 20
        target = None
        for ln in reversed(lines[-3:]):
            top = min(w["y"] for w in ln)
            bottom = max(w["y"] + (w.get("h") or 20) for w in ln)
            overlap = min(bottom, b["y"] + h) - max(top, b["y"])
            if overlap >= 0.6 * min(h, bottom - top):
                target = ln
                break
        if target is None:
            lines.append([b])
        else:
            target.append(b)
    merged = []
    for ln in lines:
        words = sorted(ln, key=lambda z: z["x"])
        h = max((w.get("h") or 20) for w in words)
        x = min(w["x"] for w in words)
        y = min(w["y"] for w in words)
        right = max(w["x"] + (w.get("w") or 0) for w in words)
        bottom = max(w["y"] + (w.get("h") or 0) for w in words)
        merged.append({"text": _join_words(words, h), "x": x, "y": y,
                       "w": right - x, "h": bottom - y})
    return merged


def group_blocks(boxes: list[dict], gap_ratio: float = 0.75) -> list[dict]:
    """把 OCR 行按纵向间距合并成块（一行选项折行时会被并回同一块）。

    只有「紧贴上一行（行距小于行高的 0.75 倍）且水平大量重叠」才算折行；
    选项之间行距更大、题干与选项之间更大，都会自然断开。
    """
    blocks: list[dict] = []
    for b in sorted(boxes, key=lambda z: (z["y"], z["x"])):
        h = b.get("h") or 20
        w = b.get("w") or 0
        y2 = b["y"] + h
        if blocks:
            last = blocks[-1]
            gap = b["y"] - last["y2"]
            overlap = min(last["x2"], b["x"] + w) - max(last["x"], b["x"])
            narrow = max(1, min(w, last["x2"] - last["x"]))
            if gap < gap_ratio * min(h, last["h"]) and overlap > 0.3 * narrow:
                last["text"] = _join(last["text"], b["text"])
                last["y2"] = max(last["y2"], y2)
                last["x2"] = max(last["x2"], b["x"] + w)
                continue
        blocks.append({"text": b["text"], "x": b["x"], "y": b["y"], "y2": y2,
                       "h": h, "x2": b["x"] + w})
    return blocks


def _join(a: str, b: str) -> str:
    """拼接折行：中文直接接，英文加空格。"""
    if not a:
        return b
    if re.search(rf"[{CJK}]$", a) or re.match(rf"^[{CJK}]", b):
        return a + b
    return f"{a} {b}"


NOISE = re.compile(r"^[\d\s/分.．%()-]{1,12}$")


def tidy_stem(stem: str) -> str:
    """题干收尾修饰：OCR 常把「（）」读成 0/O，"Arm" 读成 "A r m"。"""
    s = stem or ""
    s = re.sub(r"[（(]\s*[）)]", "（）", s)
    s = re.sub(r"([是为])\s*[0Oo]\s*$", r"\1（）", s)
    s = re.sub(r"\b([A-Z])\s+([a-z]{1,3})\b", r"\1\2", s)
    return s.strip()


# 图上文字常见的 OCR 误识（偏旁拆分）
OCR_FIX = {"扌旨": "指", "扌": "扌", "冫夬": "决", "目艮": "眼",
           # IP 协议：OCR 常把 I 读成竖线（丨 U+4E28 / |）
           "丨P": "IP", "|P": "IP"}


def _strip_circle_prefix(text: str) -> str:
    """去掉被并进正文的圆钮残留：'0不可靠的…' / 'O数据报…'。"""
    return re.sub(rf"^[0Oo](?=[{CJK}])", "", text or "").strip()


def fix_ocr_text(text: str) -> str:
    s = text or ""
    for bad, good in OCR_FIX.items():
        s = s.replace(bad, good)
    return s


def parse_ocr_question(lines, n_options: int | None = None) -> dict | None:
    """把 OCR 结果整理成 {kind, stem, options}。

    lines 可以是字符串列表，也可以是 ocr_boxes() 的方块列表；
    给了 n_options（接口里选项个数已知）就按「最后 n 块是选项」切。
    """
    if lines and isinstance(lines[0], dict):
        boxes = lines
    else:
        boxes = [{"text": t, "x": 0, "y": i * 40, "w": 0, "h": 20}
                 for i, t in enumerate(lines)]

    # 圆形按钮经常被识别成 0/O，且框高明显大于正文。先去掉，避免其大框
    # 把相邻两个选项合并成一块；分块后才去噪会导致选项数和题干错位。
    # 2026-10-29 实测：圆钮 52x52、正文行高 41，1.3 倍阈值卡不住（53.3 > 52），
    # 圆钮被并进选项正文变成「0不可靠的面向连接的」。改成「近似正方形 + 1.1 倍」。
    heights = sorted(b["h"] for b in boxes)
    normal_height = heights[len(heights) // 2] if heights else 0

    def _is_circle(b: dict) -> bool:
        text = (b.get("text") or "").strip()
        if not re.fullmatch(r"[0Oo]", text):
            return False
        h = b.get("h") or 0
        w = b.get("w") or 0
        if h <= normal_height * 1.1:
            return False
        return not (w and h) or 0.7 <= w / h <= 1.45

    boxes = [b for b in boxes if not _is_circle(b)]
    blocks = group_blocks(merge_line_boxes(boxes))
    for blk in blocks:
        blk["text"] = _strip_circle_prefix(clean_line(blk["text"]))
    blocks = [b for b in blocks if b["text"] and not NOISE.fullmatch(b["text"])
              and not re.search(r"置信度|deepseek|已生成|参考", b["text"])
              # 页脚按钮（「提交答案」等）不是选项，别让它顶掉最后一个选项
              and not re.fullmatch(r"提交(?:答案|本题)?|确定|交卷|下一题|上一题|已完成|已提交",
                                   b["text"])
              # OCR 常把圆圈选项符读成 "O"/"0" 这种单字符噪声，剔掉
              and not (len(b["text"]) <= 2 and not re.fullmatch(r"[A-Ha-h]", b["text"]))]
    if not blocks:
        return None

    # 题型行（"单选题"、"多选 2 分"）单独剔除，不作为题干
    kind = "未知"
    kept: list[dict] = []
    for blk in blocks:
        t = blk["text"]
        if len(t) <= 14:
            hit = next((name for word, name in KIND_WORDS if word in t), None)
            if hit:
                kind = hit
                continue
        kept.append(blk)
    if not kept:
        return None

    if n_options and len(kept) >= n_options + 1:
        opts = kept[-n_options:]
        stem_blocks = kept[: -n_options]
        options = [{"label": chr(ord("A") + i), "text": o["text"]}
                   for i, o in enumerate(opts)]
        stem = "\n".join(s["text"] for s in stem_blocks).strip()
        stem = tidy_stem(stem)
        return {"kind": kind, "stem": stem, "options": options, "source": "ocr"}

    # 1) 带字母的选项：A. xxx / B、xxx / C) xxx
    lettered: list[dict] = []
    first_opt = None
    for i, blk in enumerate(kept):
        row = blk["text"]
        m = (re.match(r"^([A-Ha-h])\s*[.、．:：)）]\s*(.+)$", row)
             or re.match(r"^([A-Ha-h])[ \t]+(.{2,})$", row))
        if m and len(row) <= 160:
            if first_opt is None:
                first_opt = i
            lettered.append({"label": m.group(1).upper(), "text": m.group(2).strip()})
    if len(lettered) >= 2 and first_opt is not None:
        stem = "\n".join(b["text"] for b in kept[:first_opt]).strip()
        return {"kind": kind, "stem": stem, "options": lettered[:8], "source": "ocr"}

    # 2) 没有字母：末尾几块短文本当选项
    tail: list[str] = []
    for blk in reversed(kept):
        row = blk["text"]
        short = (2 <= len(row) <= 40
                 and not NOISE.fullmatch(row)
                 and not row.endswith(("？", "?", "：", ":")))
        if short and len(tail) < 6:
            tail.append(row)
        else:
            break
    tail.reverse()
    if len(tail) >= 2:
        options = [{"label": chr(ord("A") + i), "text": t} for i, t in enumerate(tail)]
        stem_rows = kept[: len(kept) - len(tail)]
    else:
        options, stem_rows = [], kept
    stem = "\n".join(b["text"] for b in stem_rows).strip()
    if not stem and not options:
        return None
    return {"kind": kind, "stem": stem, "options": options, "source": "ocr"}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else str(BASE_DIR / "shots" / "_cover.png")
    n = int(sys.argv[2]) if len(sys.argv) > 2 else None
    boxes = ocr_boxes(path)
    print("---- 原始 OCR（带坐标）----")
    for b in boxes:
        print(f"  y={b['y']:>4} x={b['x']:>4} {b['text']}")
    print("---- 分块 ----")
    for g in group_blocks(boxes):
        print(f"  y={g['y']:>4}-{g['y2']:<4} {g['text']}")
    parsed = parse_ocr_question(boxes, n)
    print("---- 解析 ----")
    if parsed:
        print("题型:", parsed["kind"])
        print("题干:", parsed["stem"])
        for o in parsed["options"]:
            print(f"  {o['label']}. {o['text']}")
    else:
        print("无")
    if OCR_ERRORS:
        print("---- OCR 错误 ----")
        for e in OCR_ERRORS:
            print(" ", e)
