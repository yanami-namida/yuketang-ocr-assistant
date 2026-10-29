#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
长江雨课堂 · 课堂题自动抓取 + AI 参考作答
====================================================================
在指定时间内持续盯着「长江雨课堂」网页端：
  1. 每 2~3 秒探测一次页面，判断是否弹出了课堂题（单选/多选/判断/填空/投票…）；
  2. 抓到题干、选项、题型、剩余作答时间；
  3. 把题目转发给大模型（默认 DeepSeek，任何 OpenAI 兼容接口都行），
     让它给出参考答案 + 一句理由；
  4. 把结果显示在页面右上角的浮层里（同时写入日志和 answers.jsonl），
     并可在剩余时间不足时响铃提醒。

默认只给参考；显式开启 --auto-fill / --auto-submit 后才会点选、提交。

--------------------------------------------------------------------
一、安装（只需一次）
    python -m pip install playwright requests
    # 用系统已有的 Edge，无需再下载 Chromium

二、扫码登录（只需一次，浏览器会记住登录态）
    python ykt_watcher.py --login

三、开始监控
    python ykt_watcher.py                       # 默认盯 45 分钟
    python ykt_watcher.py --window 08:00-09:40  # 只在课堂时间段内盯（也支持 8:00~9:40）
    python ykt_watcher.py --duration 90         # 盯 90 分钟

四、没登录也能先试跑（本地模拟题页面，验证抓取与模型链路）
    python ykt_watcher.py --mock

五、抓不准时（页面改版/选择器失效），先存一份现场快照
    python ykt_watcher.py --dump

常用参数见 python ykt_watcher.py -h
====================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import traceback
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    sys.exit("缺少 playwright，请先执行：python -m pip install playwright")

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("缺少 requests，请先执行：python -m pip install requests")


from ykt_llm import (
    SYSTEM_PROMPT, api_key_source, ask_llm_blocking, build_user_prompt, clip, deep_merge,
    load_dsh_key, mmss, normalize_llm_answer, parse_llm_json, resolve_api_key,
)

# 图片题识别（雨课堂的随堂题常常是 PPT 图片，DOM 里没有文字）
try:
    from ocr_win import ocr_boxes, parse_ocr_question
    import ocr_win
except Exception:  # pragma: no cover - 缺文件时退化为纯 DOM 模式
    ocr_boxes = None
    parse_ocr_question = None
    ocr_win = None


# 控制台统一 UTF-8，避免中文在 Windows 终端里乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "ykt_state.json"
PROFILE_DIR = BASE_DIR / "edge_profile"
LOG_FILE = BASE_DIR / "ykt_watch.log"
ANSWERS_FILE = BASE_DIR / "answers.jsonl"
SHOT_DIR = BASE_DIR / "shots"
DUMP_DIR = BASE_DIR / "dump"


DEFAULT_CONFIG = {
    # 学生端入口；也可以换成你某节课的听课页链接
    "url": "https://changjiang.yuketang.cn/web/",
    "poll_interval": 2.5,          # 轮询间隔（秒）
    "heartbeat": 0,                # 心跳日志间隔（秒），0 表示不打（默认关：日志只在有事件时出现）
    "auto_enter": True,            # 用官方接口发现自己正在上的课，自动跳进上课页
    "auto_fill": False,            # 把 AI 建议点到网页选项上（只填，不交）
    "auto_submit": False,          # 填完再点「提交」（真提交，成绩会记录）
    "ocr": True,                   # 图片题：用 Windows 自带 OCR 把题目图片转成文字
    "fill_gap": 0.18,              # 多选项之间点击间隔（秒）
    "submit_delay": 0.4,           # 填完到提交之间的缓冲（秒）
    "duration_min": 45,            # 默认监控时长
    "state_repeat": 300,           # 停在非课堂页时，多久再提醒一次（秒）
    "dump_on_miss": 45,            # 课堂页连续这么久没识别到题，就自动存现场快照（秒，0=关闭）
    "dump_cooldown": 180,          # 两次自动快照之间的最短间隔（秒）
    "question_gone_reset": 20,     # 题目消失多少秒后允许同一题再次触发
    "answer_cache_ttl": 1800,      # 同一道题的答案缓存多久（秒）
    "low_time_warn": [120, 60, 30],  # 剩余时间低于这些秒数时提醒一次
    "sound": True,
    "save_screenshots": True,
    "overlay": True,

    # 题目容器候选选择器（雨课堂改版后可在这里补；抓不准就用 --dump 校准）
    "selectors": [
        "[class*='question']",
        "[class*='problem']",
        "[class*='exercise']",
        "[class*='quiz']",
        "[class*='subject']",
        "[class*='topic']",
        "[class*='answer']",
        "[class*='el-dialog']",
        "[class*='ant-modal']",
    ],

    # 大模型（默认 DeepSeek；任何 OpenAI 兼容接口都可以）
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "api_key_env": "DEEPSEEK_API_KEY",
        "allow_dsh_credentials": True,   # 没设环境变量时，读取 ~/.dsh/.credentials.yaml 里的 DEEPSEEK_API_KEY
        "temperature": 0.2,
        "max_tokens": 600,
        "timeout": 60,
        "reasoning_effort": None,        # 仅对部分推理模型有效，一般不用填
    },
}


# ====================================================================
# 配置 / 日志 / 小工具
# ====================================================================


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_FILE
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8"))
            cfg = deep_merge(cfg, user_cfg)
        except Exception as exc:
            print(f"[配置] 读取 {path} 失败，改用默认配置：{exc}")
    return cfg


def save_config_if_missing(cfg: dict) -> None:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def log(msg: str, echo: bool = True) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    if echo:
        print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def beep(times: int = 2) -> None:
    try:
        import winsound

        for _ in range(times):
            winsound.Beep(1100 if times > 2 else 900, 180)
            time.sleep(0.08)
    except Exception:
        print("\a", end="", flush=True)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")






# ====================================================================
# 页面探测脚本：在页面/每个 iframe 里跑，尽量把题目结构抠出来
# ====================================================================
PROBE_JS = r"""
(cfg) => {
  const clean = s => (s || '').replace(/\u00a0/g, ' ').replace(/\r/g, '');
  const isActionText = s => /^(提交|提交答案|确定|交卷|提交本题|下一题|上一题|已完成|已提交)$/.test(s.trim());

  // Vue 换题/重绘后旧锚点不能继续指向上一题。
  for (const el of document.querySelectorAll('[data-ykt-opt],[data-ykt-submit],[data-ykt-question]')) {
    el.removeAttribute('data-ykt-opt');
    el.removeAttribute('data-ykt-submit');
    el.removeAttribute('data-ykt-question');
  }

  const isVisible = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 80 && r.height > 30;
  };
  const areaOf = el => { const r = el.getBoundingClientRect(); return r.width * r.height; };
  const textOf = el => clean(el.innerText || '').trim();
  const isControlVisible = el => {
    if (!el || el.closest('#ykt-ai-overlay')) return false;
    const st = getComputedStyle(el), r = el.getBoundingClientRect();
    return st.display !== 'none' && st.visibility !== 'hidden' &&
      parseFloat(st.opacity || '1') > 0 && r.width >= 10 && r.height >= 10 &&
      el.getClientRects().length > 0;
  };

  const optionRe = /^([A-Ha-h])\s*[.、．:：\)）]\s*(.+)$/;
  const splitInline = /(?<=\s)(?=[A-H][.、．:：\)）])/;

  // 自动作答用：元素中心点 + 是否已被选中
  const boxOf = el => {
    const r = el.getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height };
  };
  const isChecked = el => {
    if (!el) return false;
    if (el.matches('input[type=radio],input[type=checkbox]') && el.checked) return true;
    const cls = String(el.className || '');
    if (/(^|[-_ ])(selected|active|checked|chosen|cur|is-checked?)([-_ ]|$)/i.test(cls)) return true;
    if (el.getAttribute) {
      if (el.getAttribute('aria-checked') === 'true') return true;
      if (el.getAttribute('aria-selected') === 'true') return true;
    }
    const inp = el.querySelector ? el.querySelector('input[type=radio],input[type=checkbox]') : null;
    if (inp && inp.checked) return true;
    const lab = el.querySelector ? el.querySelector('[class*="label"],[class*="index"],[class*="key"]') : null;
    if (lab && /(selected|active|checked)/i.test(String(lab.className || ''))) return true;
    // 有的页面把选中态加在选项外面那层（只包住当前这一个选项），往上找 3 层
    let p = el.parentElement, lvl = 0;
    while (p && lvl++ < 3) {
      const pcls = String(p.className || '');
      if (/(^|[-_ ])(selected|active|checked|chosen)([-_ ]|$)/i.test(pcls) &&
          p.querySelectorAll('[data-ykt-opt]').length <= 1) return true;
      p = p.parentElement;
    }
    return false;
  };

  function parseOptionsByText(text) {
    const out = [];
    const seen = new Set();
    const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      // 选项标签单独占一行：A / B / C，正文在下一行
      if (/^[A-Ha-h]$/.test(line) && lines[i + 1] && !/^[A-Ha-h]$/.test(lines[i + 1])) {
        const label = line.toUpperCase();
        const body = lines[i + 1].replace(/[；;，,]$/, '').trim();
        if (body && !isActionText(body) && body.length <= 400 && !seen.has(label)) {
          seen.add(label);
          out.push({ label, text: body, el: null, checked: false });
        }
        continue;
      }
      const pieces = line.split(splitInline);
      for (const piece of pieces) {
        let m = piece.trim().match(optionRe);
        if (!m) m = piece.trim().match(/^([A-Ha-h])[ \u3000]+(.{2,})$/);  // "A 选项"（无标点）
        if (!m) continue;
        const label = m[1].toUpperCase();
        const body = m[2].trim().replace(/[；;，,]$/, '');
        if (!body || isActionText(body) || body.length > 400) continue;
        if (seen.has(label)) continue;
        seen.add(label);
        out.push({ label, text: body, el: null, checked: false });
      }
    }
    out.sort((a, b) => a.label.localeCompare(b.label));
    return out;
  }

  // 结构化解析：真实页面里 A/B/C/D 常是独立 span，innerText 会变成 "A\n选项内容"
  function parseOptionsByDom(root, fullText) {
    const sel = '[class*="option"],[class*="Option"],[class*="choice"],[class*="answer-item"],' +
                '[class*="radio"],[class*="checkbox"],li,button,[role=radio],[role=checkbox],' +
                '[role=button],label,span,div,p,section,i,b,strong';
    let nodes = [];
    try { nodes = Array.from(root.querySelectorAll(sel)); } catch (e) { return []; }
    const out = [];
    const seen = new Set();
    for (const n of nodes) {
      // 图片题的可点元素往往只有 50px 的 A/B/C/D 圆钮，不能用题目容器的 80px 阈值。
      if (!isControlVisible(n)) continue;
      if (areaOf(n) > areaOf(root) * 0.95) continue;
      const cls = String(n.className || '');
      if (/list|group|wrap|container/i.test(cls)) continue;
      const t = clean(n.innerText || '').trim();
      if (!t || t.length > 600) continue;
      if (isActionText(t)) continue;
      const bare = /^[A-Ha-h]$/.test(t);
      // 容器会把 A/B/C/D 全吞进来，跳过"文本里还夹着其它选项标签"的节点
      if (/\n\s*[B-H]\s*\n/.test(t)) continue;
      if (n.querySelectorAll('li, [class*="option-item"], [class*="item"]').length > 1) continue;
      let label = bare ? t.toUpperCase() : '';
      const lab = n.querySelector('[class*="label"],[class*="index"],[class*="key"],[class*="num"],[class*="prefix"]');
      if (lab) {
        const l = clean(lab.innerText || '').trim();
        if (/^[A-Ha-h]$/.test(l)) label = l.toUpperCase();
      }
      let body = t;
      if (label) {
        body = t.replace(new RegExp('^\\s*' + label + '\\s*[.、．:：\\)）]?\\s*'), '');
      } else {
        const m = t.match(/^([A-Ha-h])\s*[.、．:：\)）]?\s*([\s\S]+)$/);
        if (!m) continue;
        label = m[1].toUpperCase();
        body = m[2];
      }
      body = body.replace(/\s+/g, ' ').trim().replace(/[；;，,]$/, '');
      if ((!body && !bare) || isActionText(body) || body.length > 300) continue;
      if (seen.has(label)) continue;
      // 选项正文不应该长到接近整段题目
      if (body.length > fullText.length * 0.8) continue;
      seen.add(label);
      out.push({ label, text: body, el: n, checked: isChecked(n) });
    }
    out.sort((a, b) => a.label.localeCompare(b.label));
    return out;
  }

  function parseOptions(root, text) {
    let opts = [];
    try { opts = parseOptionsByDom(root, text); } catch (e) { opts = []; }
    if (opts.length < 2) opts = parseOptionsByText(text);
    // 只保留 A/B/C… 连续的一组（防止把其它无关带字母的块拼进来）
    if (opts.length > 8) opts = opts.slice(0, 8);
    return opts;
  }

  function findCountdown(root) {
    const hits = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = walker.nextNode())) {
      const t = clean(node.textContent || '').trim();
      if (!t || t.length > 30) continue;
      const parent = node.parentElement;
      if (!parent) continue;
      const cls = String(parent.className || '') + ' ' + String(node.parentElement.parentElement?.className || '');
      if (!/(count|timer|time|remain|left|down)/i.test(cls) && !/(剩余|倒计时|作答时间|距离结束)/.test(t)) continue;
      const m = t.match(/(\d{1,3})\s*[:：]\s*(\d{1,2})/);
      if (m) { hits.push({ sec: parseInt(m[1], 10) * 60 + parseInt(m[2], 10), text: t }); continue; }
      const s = t.match(/^\s*(\d{1,4})\s*(?:秒|s)\s*$/i);
      if (s) hits.push({ sec: parseInt(s[1], 10), text: t });
    }
    hits.sort((a, b) => a.sec - b.sec);
    return hits.length ? hits[0] : null;
  }

  function detectKind(text) {
    if (/多选题|多项选择|多选/.test(text)) return '多选';
    if (/单选题|单项选择|单选/.test(text)) return '单选';
    if (/判断题|正确还是错误|对错/.test(text)) return '判断';
    if (/填空题|填空/.test(text)) return '填空';
    if (/投票/.test(text)) return '投票';
    if (/简答题|主观题|问答题/.test(text)) return '简答';
    return '未知';
  }

  function evaluateElement(el) {
    if (!el || !isVisible(el)) return null;
    const text = textOf(el);
    if (!text || text.length < 8) return null;
    const options = parseOptions(el, text);
    const cd = findCountdown(el);
    const hasOptWords = /单选|多选|判断|填空|投票|简答|主观/.test(text);
    const hasSubmit = /提交|确定|作答/.test(text);
    let score = 0;
    if (options.length >= 2) score += 5;
    else if (options.length === 1) score += 1;
    if (hasOptWords) score += 2;
    if (hasSubmit) score += 2;
    if (cd) score += 2;
    if (text.length > 12 && text.length < 3000) score += 1;
    if (el === document.body) score -= 4;
    if (!options.length && !cd && !hasOptWords) score -= 3;

    let kind = detectKind(text);
    if (kind === '未知' && options.length === 2 &&
        options.every(o => /^(正确|错误|对|错|是|否)/.test(o.text))) kind = '判断';

    // 题干 = 第一个选项之前的内容；去掉题号、"单选题"、倒计时、按钮文字
    let stem = text;
    const firstOpt = text.search(/^[ \t]*(?:[A-Ha-h][.、．:：\)）]|[A-Ha-h][ \t]*$)/m);
    if (firstOpt >= 0) stem = text.slice(0, firstOpt);
    stem = stem.split('\n')
      .map(s => s.trim())
      .filter(s => s &&
        !/^(单选|多选|判断|填空|投票|简答|主观)题?$/.test(s) &&
        !/(剩余时间|倒计时|距离结束|作答时间)/.test(s) &&
        !/^(提交|提交答案|确定|下一题|上一题)$/.test(s))
      .join('\n');
    stem = stem.replace(/^\s*\d+\s*[.、．]\s*/, '').trim();
    stem = stem.split('\n').filter(s => s.length > 0).slice(0, 12).join('\n');

    const hasImages = el.querySelectorAll('img').length > 0;

    // 提交按钮：自动提交时点它
    let submitEl = null;
    try {
      const buttons = Array.from(el.querySelectorAll('button, [role=button], a, span, div, input[type=button], input[type=submit]'));
      buttons.sort((a, b) => Number(b.matches('button,input,[role=button]')) - Number(a.matches('button,input,[role=button]')) || areaOf(a) - areaOf(b));
      for (const b of buttons) {
        const bt = clean(b.textContent || b.value || '').trim();
        if (!/^(提交|提交答案|确定|交卷|提交本题)$/.test(bt)) continue;
        if (!isControlVisible(b)) continue;
        if (!b.matches('button,input,[role=button],a') &&
            !/btn|button|submit/i.test(String(b.className || '')) &&
            getComputedStyle(b).cursor !== 'pointer' && !b.onclick) continue;
        submitEl = b;
        break;
      }
    } catch (e) {}

    return {
      kind, stem, options, countdown: cd ? cd.sec : null,
      score, raw: text, hasImages, el, submitEl,
      area: areaOf(el),
      rect: (() => { const r = el.getBoundingClientRect();
                     return { left: r.left, right: r.right, top: r.top,
                              width: r.width, height: r.height }; })(),
    };
  }

  const candidates = new Set();
  for (const sel of (cfg.selectors || [])) {
    let nodes = [];
    try { nodes = document.querySelectorAll(sel); } catch (e) { continue; }
    for (const el of nodes) candidates.add(el);
  }

  // 从"提交"按钮向上找 1~4 层容器（弹窗题目往往只有按钮文字好认）
  for (const el of document.querySelectorAll('button, a, div, span')) {
    const t = clean(el.textContent || '').trim();
    if (!/^(提交|提交答案|确定|交卷)$/.test(t)) continue;
    let p = el;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      if (p === document.body) break;
      if (areaOf(p) > innerWidth * innerHeight * 0.85) continue;
      candidates.add(p);
    }
  }

  // 文本锚点兜底：真实站点改版后类名可能全变，只靠"A. xxx / B. xxx"的文本特征找容器。
  // 只在常规手段不够好时才跑，避免每次轮询都扫描整页。
  function collectTextAnchors() {
    const seeds = [];
    const nodes = document.querySelectorAll('div, section, article, form, li, ul, ol');
    const limit = Math.min(nodes.length, 1500);
    for (let i = 0; i < limit; i++) {
      const el = nodes[i];
      const t = clean(el.innerText || '').trim();
      if (t.length < 6 || t.length > 4000) continue;
      const labels = new Set();
      for (const ln of t.split('\n')) {
        const s = ln.trim();
        const m = s.match(/^([A-Ha-h])(?:\s*[.、．:：\)）]|\s*$)/);
        if (m) labels.add(m[1].toUpperCase());
        if (labels.size >= 4) break;
      }
      if (labels.size >= 2) seeds.push(el);
    }
    seeds.sort((a, b) => areaOf(a) - areaOf(b));
    return seeds.slice(0, 30);
  }

  let best = null;
  for (const el of candidates) {
    let r = null;
    try { r = evaluateElement(el); } catch (e) { continue; }
    if (!r) continue;
    if (!best || r.score > best.score || (r.score === best.score && r.area < best.area)) best = r;
  }

  if (!best || best.score < 6) {
    let anchors = [];
    try { anchors = collectTextAnchors(); } catch (e) { anchors = []; }
    for (const el of anchors) {
      let r = null;
      try { r = evaluateElement(el); } catch (e) { continue; }
      if (!r) continue;
      if (!best || r.score > best.score || (r.score === best.score && r.area < best.area)) best = r;
    }
  }

  // 兜底：整个 body 里也有明显的题目特征（比如弹窗被挂在奇怪的容器里）
  if (!best || best.score < 4) {
    try {
      const bodyRes = evaluateElement(document.body);
      if (bodyRes && (!best || bodyRes.score > best.score)) best = bodyRes;
    } catch (e) {}
  }

  if (!best || best.score < 3) return { ok: false, score: best ? best.score : 0, frameUrl: location.href };

  // 给「自动作答」打锚点：只标当前这道题里的元素，避免误点到别的题
  try {
    best.el.setAttribute('data-ykt-question', '1');
    for (const o of (best.options || [])) {
      if (o.el) o.el.setAttribute('data-ykt-opt', o.label);
    }
    if (best.submitEl) best.submitEl.setAttribute('data-ykt-submit', '1');
  } catch (e) {}

  return {
    ok: true,
    kind: best.kind,
    stem: best.stem,
    options: (best.options || []).map(o => ({
      label: o.label,
      text: o.text,
      checked: !!o.checked,
      box: o.el ? boxOf(o.el) : null,
    })),
    countdown: best.countdown,
    score: best.score,
    raw: best.raw.slice(0, 6000),
    hasImages: best.hasImages,
    rect: best.rect || null,
    hasSubmit: !!best.submitEl,
    submitBox: best.submitEl ? boxOf(best.submitEl) : null,
    frameUrl: location.href,
  };
}
"""


# ====================================================================
# 浮层：在网页右上角显示题目识别结果与 AI 建议（Shadow DOM 隔离样式）
# ====================================================================
SUBMIT_FALLBACK_JS = r"""
() => {
  // 兜底：题目容器外层的「提交答案」按钮（真实课堂页见过）。只认提交类文字，
  // 且要求可见、有尺寸、未禁用，避免误点「确定/下一题」。
  const ok = /^(提交|提交答案|交卷|提交本题)$/;
  for (const el of document.querySelectorAll('button,[role=button],a,div,span')) {
    const t = (el.innerText || '').trim();
    if (!ok.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.pointerEvents === 'none') continue;
    if (el.closest('[disabled],[aria-disabled="true"],.disabled,.is-disabled')) continue;
    el.setAttribute('data-ykt-submit', '1');
    return true;
  }
  return false;
}
"""


OVERLAY_JS = r"""
(payload) => {
  const ID = 'ykt-ai-overlay';
  let host = document.getElementById(ID);
  if (!host) {
    host = document.createElement('div');
    host.id = ID;
    // pointer-events:none —— 宿主绝不拦截网页点击（否则会挡住选项/提交按钮）
    host.style.cssText = 'position:fixed;top:14px;right:14px;z-index:2147483647;pointer-events:none;';
    const shadow = host.attachShadow({ mode: 'open' });
    shadow.innerHTML = `
      <style>
        :host { all: initial; }
        .card { pointer-events: auto; width: 360px; max-height: 82vh; overflow: auto; box-sizing: border-box;
          background: rgba(22,26,34,.97); color: #e8ecf3; border: 1px solid #3a4252;
          border-radius: 12px; box-shadow: 0 12px 32px rgba(0,0,0,.45);
          font: 13px/1.55 "Microsoft YaHei", "Segoe UI", system-ui, sans-serif; padding: 12px 14px; }
        .top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
        .brand { font-size: 12px; color: #8fa3c0; letter-spacing: .5px; }
        .dot { width: 8px; height: 8px; border-radius: 50%; background: #6b7688; display: inline-block; margin-right: 6px; }
        .dot.on { background: #35d07f; box-shadow: 0 0 8px #35d07f; }
        .dot.think { background: #f5b74a; animation: pulse 1s infinite; }
        .dot.err { background: #ef5b5b; }
        @keyframes pulse { 50% { opacity: .35; } }
        .cd { font: 600 22px/1.2 Consolas, monospace; color: #ffd166; }
        .cd.low { color: #ff6b6b; }
        .kind { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 10px;
          background: #2b3a55; color: #9fc3ff; margin-left: 6px; vertical-align: 2px; }
        .ans { font: 700 30px/1.2 Consolas, monospace; color: #4ade80; margin: 4px 0 2px; word-break: break-all; }
        .ans.small { font-size: 17px; font-family: "Microsoft YaHei", sans-serif; }
        .reason { color: #c3ccda; font-size: 12.5px; margin-bottom: 8px; }
        .stem { color: #dfe6f0; font-size: 12.5px; background: #1b2130; border-left: 3px solid #3d5a80;
          padding: 6px 8px; border-radius: 4px; margin: 6px 0; max-height: 110px; overflow: auto; white-space: pre-wrap; }
        .opts { margin: 4px 0 6px; }
        .opt { padding: 3px 8px; border-radius: 5px; color: #c9d3e2; font-size: 12.5px; }
        .opt.sel { background: #1f3b2c; color: #86efac; font-weight: 600; }
        .opt b { color: #9fc3ff; margin-right: 6px; }
        .opt.sel b { color: #4ade80; }
        .meta { color: #7f8ea6; font-size: 11px; border-top: 1px solid #2e3644; padding-top: 6px; margin-top: 6px;
          display: flex; justify-content: space-between; gap: 8px; }
        .warn { color: #ffb4a2; }
        .hint { color: #8b97a8; font-size: 11px; }
      </style>
      <div class="card">
        <div class="top">
          <span class="brand"><span class="dot" id="dot"></span>雨课堂 AI 参考</span>
          <span class="cd" id="cd">--:--</span>
        </div>
        <div><span class="kind" id="kind">—</span> <span class="hint" id="status">等待题目…</span></div>
        <div class="ans" id="ans"></div>
        <div class="reason" id="reason"></div>
        <div class="stem" id="stem"></div>
        <div class="opts" id="opts"></div>
        <div class="meta"><span id="model"></span><span id="ts"></span></div>
      </div>`;
    document.documentElement.appendChild(host);
  }

  // 拖动：按住顶部标题条移动，双击复位到右上角
  if (!host.dataset.dragBound) {
    host.dataset.dragBound = '1';
    const bar = host.shadowRoot.querySelector('.top');
    bar.style.cursor = 'move';
    bar.title = '拖动移动 / 双击复位';
    let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
    bar.addEventListener('mousedown', (e) => {
      const r = host.getBoundingClientRect();
      dragging = true; sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
      host.dataset.dragging = '1';
      host.style.right = 'auto';
      host.style.left = ox + 'px';
      host.style.top = oy + 'px';
      e.preventDefault();
    });
    window.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      host.style.left = Math.max(0, ox + e.clientX - sx) + 'px';
      host.style.top = Math.max(0, oy + e.clientY - sy) + 'px';
    });
    window.addEventListener('mouseup', () => {
      if (!dragging) return;
      dragging = false;
      host.dataset.dragging = '0';
      try {
        localStorage.setItem('ykt-overlay-pos',
          JSON.stringify({ left: host.style.left, top: host.style.top }));
      } catch (e) {}
    });
    bar.addEventListener('dblclick', () => {
      host.style.left = 'auto';
      host.style.top = '14px';
      host.style.right = '14px';
      try { localStorage.removeItem('ykt-overlay-pos'); } catch (e) {}
    });
  }

  // 位置：拖过的以拖到的位置为准；否则按题目面板位置自动左右避让（每次更新都校正）
  try {
    if (host.dataset.dragging !== '1') {
      const pos = JSON.parse(localStorage.getItem('ykt-overlay-pos') || 'null');
      if (pos && pos.left) {
        host.style.right = 'auto';
        host.style.left = pos.left;
        host.style.top = pos.top || '14px';
      } else {
        const r = payload && payload.rect;
        const toLeft = !!(r && r.right > innerWidth * 0.55 && r.width < innerWidth * 0.8);
        host.style.left = toLeft ? '14px' : 'auto';
        host.style.right = toLeft ? 'auto' : '14px';
        host.style.top = '14px';
      }
    }
  } catch (e) {}

  const $ = (id) => host.shadowRoot.getElementById(id);
  const setText = (el, v) => { if (el && el.textContent !== v) el.textContent = v; };

  $('dot').className = 'dot ' + (payload.dot || '');
  const cdEl = $('cd');
  if (payload.countdown) {
    setText(cdEl, payload.countdown);
    cdEl.className = 'cd' + (payload.low ? ' low' : '');
    cdEl.style.display = '';
  } else {
    cdEl.style.display = 'none';   // 没有题目时别显示「未知」
  }
  setText($('kind'), payload.kind || '—');
  setText($('status'), payload.status || '');

  const ansEl = $('ans');
  setText(ansEl, payload.answer || '');
  ansEl.className = 'ans' + (payload.answer && payload.answer.length > 4 ? ' small' : '');
  ansEl.style.display = payload.answer ? '' : 'none';

  const reasonEl = $('reason');
  setText(reasonEl, payload.reason || '');
  reasonEl.style.display = payload.reason ? '' : 'none';

  const stemEl = $('stem');
  setText(stemEl, payload.stem || '');
  stemEl.style.display = payload.stem ? '' : 'none';   // 空题干不留空框

  setText($('model'), payload.model || '');
  setText($('ts'), payload.ts || '');

  const optsEl = $('opts');
  const key = JSON.stringify(payload.options || []) + '|' + (payload.selected || []).join(',');
  if (optsEl.dataset.key !== key) {
    optsEl.dataset.key = key;
    optsEl.innerHTML = '';
    const sel = new Set(payload.selected || []);
    for (const o of (payload.options || [])) {
      const d = document.createElement('div');
      d.className = 'opt' + (sel.has(o.label) ? ' sel' : '');
      const b = document.createElement('b'); b.textContent = o.label;
      d.appendChild(b);
      d.appendChild(document.createTextNode(o.text || ''));
      optsEl.appendChild(d);
    }
  }
}
"""


# ====================================================================
# 题目抽取（遍历主页面与所有 iframe）
# ====================================================================
# 首页找「正在上的课」：只做识别，auto_enter 打开时点第一个
PAGE_STATE_JS = r"""
() => {
  const has = s => { try { return !!document.querySelector(s); } catch (e) { return false; } };
  return {
    hasPlayer: has('video, canvas, [class*="player"], [class*="live"], [class*="slide"], [class*="courseware"]'),
    hasQuestionish: has('[class*="question"], [class*="quiz"], [class*="exercise"], [class*="problem"], [class*="answer"]'),
    frames: window.frames.length,
    title: (document.title || '').slice(0, 80),
  };
}
"""


def classify_page(url: str, js: dict | None = None) -> str:
    """把当前页面粗分成 登录页 / 课程首页 / 课堂页 / 其它。"""
    u = url or ""
    js = js or {}
    if re.search(r"login|passport|oauth|/qr", u, re.I):
        return "login"
    if re.search(r"/(v2/)?web/(index|home)?/?$", u, re.I):
        return "index"
    # 学生端上课视图（左侧课堂动态 + 右侧放映幻灯片）就是真正的课堂页
    if re.search(r"/lesson/(fullscreen|student)/", u, re.I):
        return "classroom"
    if re.search(r"studentLog|/v2/web/student-lesson-report", u, re.I):
        return "logpage"
    if js.get("hasPlayer") or js.get("hasQuestionish") or int(js.get("frames") or 0) > 1:
        return "classroom"
    if re.search(r"lesson|classroom|live|course|exam|quiz|answer", u, re.I):
        return "classroom"
    return "unknown"


PAGE_HINT = {
    "login": "还没登录雨课堂：先运行 python ykt_watcher.py --login 扫码一次（一次就够）。",
    "index": "现在停在课程首页，还没进课堂页——请在浏览器窗口里点进正在上的那节课，"
             "脚本会自动跟过去（也可以下次用 --url 直接给听课页地址）。",
    "unknown": "当前页面不像是课堂/答题页：{url}。如果老师已经开课，请进课堂页；"
               "如果页面上明明有题却没识别出来，把窗口截图发我，我按真实页面校准。",
    "classroom": "已进入课堂页，等老师推题…",
    "logpage": "这是课堂日志页（学习记录），不是上课直播页——脚本会自己找正在上的课并跳过去；"
               "跳不过去就把日志里「正在上课」那节课的链接发我。",
}


async def current_page_state(page, cache: dict, now_ts: float) -> str:
    """带缓存：URL 没变且 10 秒内不重复执行 JS。"""
    if cache.get("url") == page.url and now_ts - float(cache.get("ts") or 0) < 10:
        return cache.get("state") or ""
    js = {}
    try:
        js = await page.main_frame.evaluate(PAGE_STATE_JS)
    except Exception:
        js = {}
    st = classify_page(page.url, js if isinstance(js, dict) else {})
    cache.update(url=page.url, ts=now_ts, state=st)
    return st


FIND_LIVE_JS = r"""
() => {
  // 可以放心点的「进课堂」文案
  const JOIN = /(进行中|正在上课|上课中|正在直播|直播中|进入课堂|进入课程|继续学习|去上课|进入直播)/;
  // 只提示、绝不自动点的（「开启授课」点了会新开一堂课）
  const HINT = /(进行中|正在上课|上课中|正在直播|直播中|进入课堂|进入课程|继续学习|去上课|进入直播|开启授课|授课)/;
  const SKIP = /(创建课程|加入班级|群发公告|我的归档|返回旧版|课程班级|资源库)/;
  const nodes = document.querySelectorAll('a,button,li,div,span,h1,h2,h3,h4,p');
  const picked = [];
  for (const el of nodes) {
    if (el.closest('#ykt-ai-overlay')) continue;
    const t = (el.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length < 2 || t.length > 40) continue;
    if (SKIP.test(t) || !HINT.test(t)) continue;
    if (picked.some((o) => o.el === el || o.el.contains(el) || el.contains(o.el))) continue;
    picked.push({ el, t });
    if (picked.length >= 8) break;
  }
  window.__yktLiveEls = picked.map((o) => o.el);
  window.__yktLiveJoin = picked.map((o) => JOIN.test(o.t));
  return picked.map((o) => {
    const r = o.el.getBoundingClientRect();
    const a = o.el.closest('a[href]');
    return {
      text: o.t.slice(0, 40),
      tag: o.el.tagName.toLowerCase(),
      href: a ? a.getAttribute('href') : '',
      join: JOIN.test(o.t),
      visible: r.width > 0 && r.height > 0,
    };
  }).filter((o) => o.visible);
}
"""


async def find_live_courses(page) -> list[dict]:
    """在当前页面（一般是首页）找可能正在进行的课程；找不到就返回空列表。"""
    try:
        return await page.evaluate(FIND_LIVE_JS) or []
    except Exception:
        return []


async def click_live_course(page, idx: int = 0) -> bool:
    try:
        return bool(await page.evaluate(
            "(i) => { const e = window.__yktLiveEls && window.__yktLiveEls[i];"
            " if (!e) return false; e.scrollIntoView({block: 'center'}); e.click(); return true; }", idx))
    except Exception:
        return False


def _frame(page, idx: int):
    try:
        return page.frames[int(idx)]
    except Exception:
        return page.main_frame


async def click_option(page, frame_index: int, label: str, selectors: list[str] | None = None) -> bool:
    """按标签点选项（靠 probe 阶段打的 data-ykt-opt 锚点，避免坐标漂移）。"""
    frame = _frame(page, frame_index)
    sel = f'[data-ykt-opt="{label}"]'
    try:
        disabled = await frame.evaluate(
            "sel => { const e = document.querySelector(sel); return !!(e && "
            "e.closest('[disabled],[aria-disabled=\"true\"],.disabled,.is-disabled')); }", sel)
        if disabled:
            log(f"选项 {label} 当前不可选，停止点击")
            return False
        await frame.locator(sel).first.click(timeout=2500)
    except Exception as exc:
        first = (str(exc).splitlines() or [""])[0]
        log(f"点击选项 {label} 失败：{clip(first, 120)}")
        return False
    try:
        # 事件发出不等于选中；重读 DOM，同时给 Vue 重绘出来的新元素重新打锚点。
        for _ in range(5):
            live = await frame.evaluate(PROBE_JS, {"selectors": selectors or DEFAULT_CONFIG["selectors"]})
            if any(o.get("label") == label and o.get("checked") for o in live.get("options", [])):
                return True
            await asyncio.sleep(0.1)
    except Exception as exc:
        log(f"检查选项 {label} 状态失败：{clip(str(exc), 120)}")
    log(f"选项 {label} 已点击，但未检测到选中状态，停止提交")
    return False


async def click_submit(page, frame_index: int, selectors: list[str] | None = None) -> bool:
    frame = _frame(page, frame_index)
    sel = '[data-ykt-submit="1"]'
    try:
        # 选择答案后，提交按钮才可能出现/启用；不能只使用出题时的旧锚点。
        await frame.evaluate(PROBE_JS, {"selectors": selectors or DEFAULT_CONFIG["selectors"]})
    except Exception:
        pass
    try:
        loc = frame.locator(sel).first
        if await frame.locator(sel).count() == 0:
            # 有的页面把「提交答案」放在题目容器外面，PROBE_JS 的锚点打不到；
            # 按可见文字兜底（只认提交类按钮，不碰「确定/下一题」）。
            await frame.evaluate(SUBMIT_FALLBACK_JS)
            loc = frame.locator(sel).first
        await loc.click(timeout=2500)
        return True
    except Exception as exc:
        first = (str(exc).splitlines() or [""])[0]
        log(f"点击提交失败：{clip(first, 120)}")
        return False


async def auto_answer(page, q: dict, advice: dict, cfg: dict) -> str:
    """把建议答案点到页面上，返回一行给人看的说明。绝不输入文字、绝不点无关按钮。"""
    labels = advice.get("selected") or []
    pre = [o.get("label") for o in q.get("options", []) if o.get("checked")]
    if pre:
        return f"自动作答：页面已有选择（{''.join(pre)}），跳过，免得覆盖你手选的答案"
    if not labels:
        return f"自动作答：{q.get('kind')}没有可点的选项，需要你手动作答"
    available = {o.get("label") for o in q.get("options", []) if o.get("box")}
    if any(lb not in available for lb in labels):
        return "自动作答：参考答案没有对应的网页选项按钮，未点击、未提交"
    idx = int(q.get("frame_index") or 0)
    ok, bad = [], []
    for lb in labels:
        (ok if await click_option(page, idx, lb, cfg.get("selectors")) else bad).append(lb)
        await asyncio.sleep(float(cfg.get("fill_gap", 0.18)))
    picked = "".join(ok)
    if bad:
        return f"自动作答：点到了 {picked or '无'}，但 {'/'.join(bad)} 没点到，未提交"
    if not cfg.get("auto_submit"):
        return f"已自动填入：{picked}（未提交，确认无误后自己点提交）"
    await asyncio.sleep(float(cfg.get("submit_delay", 0.4)))
    if await click_submit(page, idx, cfg.get("selectors")):
        return f"已自动填入并点击提交：{picked}（是否提交成功以网页反馈为准）"
    return f"已自动填入：{picked}，提交按钮没点到，请手动提交"


# 官方接口：哪节课「正在上课」。比在页面文字里找「正在上课」靠谱得多。
async def dismiss_blockers(page) -> list[str]:
    """关掉「浏览器不兼容/须知」这类挡路的弹窗，否则直播画面根本加载不出来。"""
    done: list[str] = []
    for fr in page.frames:
        for label in ("我知道了", "同意 ( 3 s )", "同意", "继续访问"):
            try:
                loc = fr.get_by_text(label, exact=False)
                if await loc.count() == 0:
                    continue
                await loc.first.click(timeout=2000)
                done.append(label)
                await asyncio.sleep(0.4)
                break
            except Exception:
                continue
    return done


ON_LESSON_API = "https://changjiang.yuketang.cn/api/v3/classroom/on-lesson"


async def current_live_lesson(page) -> dict | None:
    """查当前正在进行的课；返回 {url, course, classroom, lesson_id}。"""
    try:
        resp = await page.request.get(ON_LESSON_API, timeout=15000)
        if not resp.ok:
            return None
        data = await resp.json()
    except Exception:
        return None
    items = ((data or {}).get("data") or {}).get("onLessonClassrooms") or []
    for item in items:
        cid = str(item.get("classroomId") or "").strip()
        lid = str(item.get("lessonId") or "").strip()
        if not cid:
            continue
        return {
            # 学生端上课视图：左侧课堂动态 + 右侧正在放映的幻灯片（题目也在这里出现）
            "url": (f"https://changjiang.yuketang.cn/lesson/fullscreen/v3/{lid}"
                    if lid else
                    f"https://changjiang.yuketang.cn/v2/web/studentLog/{cid}?classroom_id={cid}"),
            "course": str(item.get("courseName") or ""),
            "classroom": str(item.get("classroomName") or ""),
            "lesson_id": lid,
        }
    return None


# 图片题：雨课堂的随堂题往往是一张 PPT 图片（qn-*.yuketang.cn），
# DOM 里没有文字，所以先把题目图片截图，再交给系统 OCR。
SLIDE_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight, out = [];
  const nodes = [...document.querySelectorAll("img, canvas")];
  const root = document.querySelector('[data-ykt-question="1"]');
  // 真实网页可能把图片和答题控件放在相邻容器，不能仅保留控件的子图片。
  const imageRoot = root && (root.querySelector('img,canvas') ? root : root.closest('.page-exercise,[class*=exercise],main'));
  nodes.forEach((el, i) => {
    if (imageRoot && !imageRoot.contains(el)) return;
    let r;
    try { r = el.getBoundingClientRect(); } catch (e) { return; }
    if (!r || r.width < 240 || r.height < 150) return;
    if (r.width * r.height < vw * vh * 0.15) return;
    if (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw) return;
    const src = el.currentSrc || el.src || "";
    let score = 0;
    if (/qn-|yuketang|slide|ppt|cover|problem/i.test(src)) score += 4;
    if (el.tagName === "IMG") score += 1;
    if (r.width * r.height > vw * vh * 0.45) score += 1;
    out.push({index: i, src: String(src).slice(0, 200), score,
              cx: r.left + r.width / 2, cy: r.top + r.height / 2, w: r.width, h: r.height});
  });
  out.sort((a, b) => b.score - a.score || b.w * b.h - a.w * a.h);
  return out.slice(0, 4);
}
"""


CD_JS = r"""
() => {
  const txt = (document.body && document.body.innerText) || "";
  let m = txt.match(/剩余(?:时间|作答时间)?[^0-9]{0,4}(\d{1,3})\s*[:：]\s*(\d{2})/);
  if (m) return parseInt(m[1], 10) * 60 + parseInt(m[2], 10);
  m = txt.match(/(?:倒计时|剩余)[^0-9]{0,4}(\d{1,4})\s*秒/);
  if (m) return parseInt(m[1], 10);
  return null;
}
"""


OVERLAY_ID = "ykt-ai-overlay"


async def _set_overlay_visible(page, visible: bool) -> None:
    """截图/OCR 前先把浮层藏起来，免得把我们自己的字也 OCR 进去。"""
    try:
        await page.evaluate(
            """(args) => {
                 const h = document.getElementById(args.id);
                 if (h) h.style.display = args.visible ? "" : "none";
             }""",
            {"id": OVERLAY_ID, "visible": visible},
        )
    except Exception:
        pass


# OCR 结果缓存：同一张图 8 秒内不重复跑 PowerShell（每次要 1~2 秒）
_OCR_CACHE: dict[tuple, tuple] = {}
_PROBE_DETAILS: dict[int, dict] = {}


def _ocr_engine() -> str:
    """当前 OCR 实际用的识别语言；只有 zh-* 才应该读出中文。"""
    return getattr(ocr_win, "OCR_ENGINE", "") if ocr_win is not None else ""


def _is_ocr_noise(text: str) -> bool:
    text = text.strip()
    return bool(
        re.search(r"置信度|已给出参考|等老师推|AI 生成中|deepseek|已自动填入|雨课堂 AI 参考", text)
        or re.fullmatch(r"提交(?:答案|本题)?|确定|交卷|已完成|已提交|下一题|上一题", text)
        or re.match(r"^(?:倒计时|剩余(?:时间)?)[\s\d:：秒]+$", text)
        or re.fullmatch(r"20\d{2}[年/丿.-][\d月日/丿.m-]{2,12}", text)
    )


def _read_question_image(path: Path, n_options: int | None) -> tuple:
    """先剔除页脚/控件文字；漏项时放大重识别，并保留重识别原因供诊断。"""
    def read(image_path):
        boxes = [b for b in ocr_boxes(image_path) if not _is_ocr_noise(b["text"])]
        return parse_ocr_question(boxes, n_options), boxes

    parsed, boxes = read(path)
    count = len((parsed or {}).get("options") or [])
    incomplete = (not parsed or not parsed.get("stem") or
                  (n_options is not None and count != n_options) or
                  (n_options is None and count in (0, 1, 3)))
    retried = False
    if incomplete:
        try:
            from PIL import Image
            with TemporaryDirectory(prefix="ykt-ocr-") as folder:
                target = Path(folder) / "large.png"
                with Image.open(path) as image:
                    image.resize((image.width * 2, image.height * 2)).save(target)
                enlarged, large_boxes = read(target)
                retried = True
                large_count = len((enlarged or {}).get("options") or [])
                if enlarged and enlarged.get("stem") and (
                    large_count == n_options or (n_options is None and large_count > count)
                ):
                    parsed, boxes = enlarged, large_boxes
        except ImportError:
            pass  # Pillow 为可选增强；基础 OCR 仍可工作。
    return parsed, [b["text"] for b in boxes], retried


async def probe_image_question(page, cfg: dict, fallback_kind: str | None = None,
                               controls: dict | None = None) -> dict | None:
    """题目是一张图时：截图 → Windows OCR → 解析题干和选项。"""
    if not cfg.get("ocr") or ocr_boxes is None or parse_ocr_question is None:
        return None
    _PROBE_DETAILS.setdefault(id(page), {})
    ttl = float(cfg.get("ocr_cache", 8))
    for idx, frame in enumerate(page.frames):
        if controls and idx != controls.get("frame_index"):
            continue
        try:
            cands = await frame.evaluate(SLIDE_JS)
        except Exception:
            continue
        for cand in cands or []:
            dom_options = (controls or {}).get("options") or []
            n_options = len(dom_options) if len(dom_options) >= 2 else None
            key = (frame.url, cand["src"], int(cand["w"]), int(cand["h"]), n_options)
            cached = _OCR_CACHE.get(key)
            if cached and time.time() - cached[0] < ttl:
                parsed, lines, rel = cached[1], cached[2], cached[3]
                _PROBE_DETAILS[id(page)]["ocr"] = {
                    "image": rel, "expected_options": n_options,
                    "labels": [o["label"] for o in (parsed or {}).get("options", [])],
                    "stem": (parsed or {}).get("stem", "")[:150], "cached": True,
                    "engine": _ocr_engine(), "raw": "\n".join(lines or [])[:300],
                }
            else:
                try:
                    el = frame.locator("img, canvas").nth(int(cand["index"]))
                    SHOT_DIR.mkdir(exist_ok=True)
                    png = SHOT_DIR / f"img_{datetime.now():%H%M%S}_{idx}.png"
                    await _set_overlay_visible(page, False)
                    try:
                        await el.screenshot(path=str(png), timeout=8000)
                    finally:
                        await _set_overlay_visible(page, True)
                except Exception:
                    continue
                # 保留 OCR 坐标，避免逐字结果被当成几十个独立选项。
                parsed, lines, retried = await asyncio.to_thread(_read_question_image, png, n_options)
                _PROBE_DETAILS[id(page)]["ocr"] = {
                    "image": png.name, "expected_options": n_options,
                    "labels": [o["label"] for o in (parsed or {}).get("options", [])],
                    "stem": (parsed or {}).get("stem", "")[:150], "enlarged_retry": retried,
                    "engine": _ocr_engine(), "raw": "\n".join(lines or [])[:300],
                }
                try:
                    rel = str(png.relative_to(BASE_DIR))
                except ValueError:
                    rel = str(png)
                _OCR_CACHE[key] = (time.time(), parsed, lines, rel)
            if not parsed or len(parsed.get("stem") or "") < 4:
                continue
            kind = parsed["kind"] if parsed["kind"] != "未知" else (fallback_kind or "未知")
            # 幻灯片也是大图，必须确认这张图「像一道题」，否则会把每页 PPT 都当成题
            _stem = parsed["stem"]
            _opts = parsed["options"]
            _blank = bool(re.search(r"（\s*）|\(\s*\)|＿{2,}|_{3,}", _stem))
            looks_like_question = bool(
                (kind != "未知" and len(_opts) >= 2)
                or (kind in ("判断", "填空", "简答") and (_blank or len(_opts) >= 2))
            )
            if not looks_like_question:
                continue
            options = parsed["options"]
            if dom_options:
                # OCR 提供正文，DOM 提供点击目标；必须是同一 frame 的同一组选项。
                if [o["label"] for o in options] != [o["label"] for o in dom_options]:
                    _PROBE_DETAILS[id(page)]["rejected"] = "OCR 选项标签与 DOM 按钮不一致"
                    continue
                options = [{**o, "checked": d.get("checked", False), "box": d.get("box")}
                           for o, d in zip(options, dom_options)]
            sig_src = ("ocr|" + kind + "|" + parsed["stem"] + "|" +
                       "|".join(f'{o["label"]}.{o["text"]}' for o in parsed["options"]))
            countdown = None
            for fr in page.frames:
                try:
                    countdown = await fr.evaluate(CD_JS)
                except Exception:
                    countdown = None
                if countdown is not None:
                    break
            return {
                "kind": kind,
                "stem": parsed["stem"],
                "options": options,
                "countdown": controls.get("countdown") if controls and controls.get("countdown") is not None else countdown,
                "has_images": True,
                "ocr": True,
                "image_path": rel,
                "score": 6,
                "rect": {"left": cand["cx"], "right": cand["cx"],
                         "top": cand["cy"], "width": cand["w"], "height": cand["h"]},
                "frame_index": idx,
                "has_submit": bool((controls or {}).get("has_submit")),
                "submit_box": (controls or {}).get("submit_box"),
                "frame_url": frame.url,
                "raw": "\n".join(lines)[:500],
                "signature": hashlib.md5(sig_src.encode("utf-8")).hexdigest()[:16],
            }
    return None


async def probe_page(page, cfg: dict) -> dict | None:
    _PROBE_DETAILS[id(page)] = {}
    best = None
    best_idx = 0
    for idx, frame in enumerate(page.frames):
        try:
            res = await frame.evaluate(PROBE_JS, {"selectors": cfg["selectors"]})
        except Exception:
            continue
        if not res or not res.get("ok"):
            continue
        if best is None or res.get("score", 0) > best.get("score", 0):
            res["frameUrl"] = frame.url
            best = res
            best_idx = idx

    _PROBE_DETAILS[id(page)]["dom"] = {
        "frame_index": best_idx,
        "labels": [o["label"] for o in (best or {}).get("options", [])],
        "stem": (best or {}).get("stem", "")[:150],
        "has_submit": bool((best or {}).get("hasSubmit")),
    }

    if not best:
        return await probe_image_question(page, cfg)

    stem = re.sub(r"[ \t]+", " ", best.get("stem") or "").strip()
    options = best.get("options") or []
    # 判断题如果只有"正确/错误"文字，补成两个选项
    if best.get("kind") == "判断" and len(options) < 2 and "正确" in best.get("raw", ""):
        options = [{"label": "A", "text": "正确"}, {"label": "B", "text": "错误"}]

    if not stem and not options:
        return await probe_image_question(page, cfg)

    sig_src = best.get("kind", "") + "|" + stem + "|" + "|".join(
        f'{o["label"]}.{o["text"]}' for o in options
    )
    q = {
        "kind": best.get("kind", "未知"),
        "stem": stem,
        "options": options,
        "countdown": best.get("countdown"),
        "has_images": bool(best.get("hasImages")),
        "score": best.get("score", 0),
        "rect": best.get("rect"),
        "frame_index": best_idx,
        "has_submit": bool(best.get("hasSubmit")),
        "submit_box": best.get("submitBox"),
        "frame_url": best.get("frameUrl", ""),
        "raw": best.get("raw", ""),
        "signature": hashlib.md5(sig_src.encode("utf-8")).hexdigest()[:16],
    }

    # 只有真选项才算题目；没有选项、又不是判断/填空/简答/投票的，
    # 多半是作业封面、成绩列表这类页面文字（之前会误报成「有题」）。
    plausible = bool(stem) and (
        (len(options) >= 2 and all(o.get("text", "").strip() for o in options))
        or (not options and q["kind"] in ("判断", "填空", "简答", "投票"))
    )
    if not plausible:
        img = await probe_image_question(page, cfg, best.get("kind"), controls=q)
        if img:
            if best.get("countdown") is not None:
                img["countdown"] = best["countdown"]
            return img
        return None
    return q


# ====================================================================
# 大模型：题目 -> 参考答案
# ====================================================================












async def ask_llm(cfg: dict, q: dict, api_key: str) -> dict:
    return await asyncio.to_thread(ask_llm_blocking, cfg, q, api_key)




# ====================================================================
# 浏览器
# ====================================================================
def browser_channel() -> str:
    return "msedge"


async def launch_context(p, headless: bool):
    PROFILE_DIR.mkdir(exist_ok=True)
    context = await p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel=browser_channel(),
        headless=headless,
        viewport={"width": 1440, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return context, page


def profile_holders() -> list[int]:
    """找出正占着 edge_profile 的 msedge 进程（Playwright 起的浏览器或上次没退干净的）。"""
    if os.name != "nt":
        return []
    try:
        ps = subprocess.run(["powershell", "-NoProfile", "-Command",
                             "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
                             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
                            capture_output=True, text=True, timeout=25,
                            encoding="utf-8", errors="ignore")
        data = json.loads((ps.stdout or "").strip() or "[]")
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    target = str(PROFILE_DIR).lower()
    pids = []
    for item in data:
        cmd = str((item or {}).get("CommandLine") or "").lower()
        if target in cmd or PROFILE_DIR.name.lower() in cmd:
            try:
                pids.append(int(item.get("ProcessId")))
            except (TypeError, ValueError):
                pass
    return pids


def kill_profile_holders() -> int:
    killed = 0
    for pid in profile_holders():
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=10)
            killed += 1
        except Exception:
            pass
    return killed


async def open_browser(p, headless: bool, kill_stale: bool = False):
    """启动浏览器；如果资料目录被占用，给出可执行的清理提示（或自动清理）。"""
    pids = profile_holders()
    if pids and kill_stale:
        killed = kill_profile_holders()
        log(f"已清理 {killed} 个占用浏览器资料目录的残留进程（PID: {', '.join(map(str, pids))}）。")
        await asyncio.sleep(1.5)
        pids = profile_holders()

    if pids:
        log(f"注意：还有 {len(pids)} 个 Edge 进程占着 {PROFILE_DIR.name}\\（PID: "
            f"{', '.join(map(str, pids))}）。如果启动失败，先关掉另一个正在跑的监控窗口，"
            "或加 --kill-stale 让脚本自动清理。")

    try:
        return await launch_context(p, headless)
    except Exception as exc:
        brief = (str(exc).strip().splitlines() or [""])[0][:240]
        log(f"启动浏览器失败：{type(exc).__name__}: {brief}")
        if pids:
            log("原因很可能是资料目录被占用（上一个监控窗口没退干净，或另一个窗口正在跑）。")
            log(f'手动清理：Get-CimInstance Win32_Process -Filter "Name=\'msedge.exe\'" | '
                f"Where-Object {{ $_.CommandLine -like '*{PROFILE_DIR.name}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
            log("或者在命令后加 --kill-stale，让脚本自己清理后重试。")
        raise SystemExit(2) from exc


async def wait_login(page, timeout: int = 300) -> bool:
    """轮询到登录完成：页面不再有扫码/登录提示，且出现了站内导航。最多等 timeout 秒。"""
    deadline = time.time() + timeout
    start_url = page.url
    qr_hint = re.compile(r"扫码|二维码|微信登录|请用微信|登录后|密码登录|验证码")
    logged_hint = re.compile(r"我的课堂|我的课程|退出登录|个人中心|学习记录|我的班级|课程表|全部课程")
    ok_streak = 0
    while time.time() < deadline:
        await asyncio.sleep(2)
        url = page.url
        if re.search(r"login|passport|register|authorize", url, re.I):
            ok_streak = 0
            continue
        try:
            text = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 5000)")
        except Exception:
            text = ""
        if qr_hint.search(text or ""):
            ok_streak = 0
            continue
        # 站内导航出现，或（没有扫码提示且已经离开了登录初始页）连续两次成立就认为登录完成
        strong = bool(logged_hint.search(text or ""))
        weak = (url != start_url) and bool((text or "").strip())
        ok_streak = ok_streak + 1 if (strong or weak) else 0
        if ok_streak >= 2:
            await asyncio.sleep(1.5)   # 等页面把登录后的接口都跑完，cookie 落盘
            log(f"检测到登录成功，当前页面：{url}")
            return True
    return False


async def do_login(cfg: dict, args) -> None:
    timeout = int(getattr(args, "login_timeout", 300) or 300)
    async with async_playwright() as p:
        context, page = await open_browser(p, headless=False,
                                           kill_stale=getattr(args, "kill_stale", False))
        await page.goto(cfg["url"], wait_until="domcontentloaded")
        log(f"已打开长江雨课堂，请用微信扫码登录。登录完成后会自动继续（最多等 {timeout} 秒）。")
        ok = await wait_login(page, timeout)
        if ok:
            try:
                await context.storage_state(path=str(STATE_FILE))
            except Exception:
                pass
            log(f"登录态已保存（浏览器资料目录：{PROFILE_DIR}），之后运行无需再扫码。")
        else:
            log("未检测到登录完成。可确认浏览器里是否已登录，或增大等待时间后重试。")
        if not args.keep_open:
            await asyncio.sleep(1)
            await context.close()


# ====================================================================
# 监控主循环
# ====================================================================
def question_signature(q: dict) -> str:
    return q["signature"]


def record_answer(q: dict, advice: dict) -> None:
    row = {
        "time": now_str(),
        "kind": q["kind"],
        "stem": q["stem"],
        "options": q["options"],
        "countdown_at_detect": q["countdown"],
        "answer": advice.get("answer"),
        "answer_text": advice.get("answer_text"),
        "confidence": advice.get("confidence"),
        "reason": advice.get("reason"),
        "signature": q["signature"],
    }
    try:
        with open(ANSWERS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


async def set_overlay(page, cfg: dict, payload: dict) -> None:
    if not cfg.get("overlay"):
        return
    try:
        await page.evaluate(OVERLAY_JS, payload)
    except Exception:
        pass


def overlay_payload(q: dict | None, advice: dict | None, status: str, dot: str,
                    countdown: int | None, model: str, extra: str = "") -> dict:
    selected = advice.get("selected") if advice else []
    answer = (advice or {}).get("answer", "")
    reason = (advice or {}).get("reason", "")
    if advice and advice.get("confidence") is not None and advice.get("answer"):
        reason = f"[置信度 {advice['confidence']:.0%}] " + reason
    if extra:
        reason = (reason + " " + extra).strip()
    return {
        "dot": dot,
        "status": status,
        "kind": (q or {}).get("kind", ""),
        "stem": (q or {}).get("stem", ""),
        "options": (q or {}).get("options", []),
        "answer": answer,
        "selected": selected,
        "reason": reason,
        "countdown": mmss(countdown) if countdown is not None else "",
        "low": countdown is not None and countdown <= 60,
        "model": model,
        "ts": now_str(),
        "rect": (q or {}).get("rect"),
    }


async def watch(cfg: dict, args) -> None:
    url = cfg["url"]
    if args.mock:
        query = getattr(args, "mock_query", None) or "t=90&auto=1"
        url = (BASE_DIR / "mock_quiz.html").resolve().as_uri() + "?" + query.lstrip("?")

    end_time = None
    if args.window:
        start, end = args.window, args.end_time
        wait = (start - datetime.now()).total_seconds()
        if wait > 1:
            log(f"等待监控开始：{mmss(int(wait))} 后（{start:%H:%M}）")
            await asyncio.sleep(wait)
        end_time = end
        log(f"进入时间窗口监控：{start:%H:%M} - {end:%H:%M}")
    else:
        end_time = datetime.now() + timedelta(minutes=args.duration)
        log(f"开始监控，持续 {args.duration} 分钟，间隔 {cfg['poll_interval']} 秒。")
        if cfg.get("auto_fill"):
            log("⚠ 自动作答已开启：" + ("填完会自动点「提交」（真提交）" if cfg.get("auto_submit")
                                         else "只把选项点上，不点「提交」"))

    api_key = resolve_api_key(cfg, args.api_key)
    if not api_key:
        log("未找到模型 API Key：只抓题、不生成答案。可设置环境变量 "
            f"{cfg['llm']['api_key_env']}、运行 设置APIKey.bat，或使用 --api-key。")
    else:
        log(f"模型：{cfg['llm']['model']} @ {cfg['llm']['base_url']}"
            f"（Key 来源：{api_key_source(cfg, args.api_key)}）")

    async with async_playwright() as p:
        context, page = await open_browser(p, headless=args.headless,
                                           kill_stale=getattr(args, "kill_stale", False))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log(f"打开页面失败：{exc}")
        log(f"已加载：{url}")
        if not args.mock and re.search(r"login|passport|qr", page.url, re.I):
            log("看起来还没登录。请先运行：python ykt_watcher.py --login")
        await asyncio.sleep(1.5)

        # 一上来就把浮层挂上去：用户能直观看到「已连接、正在等题」
        async def push_idle(status: str = "已连接，等待题目…", dot: str = "idle") -> None:
            await set_overlay(page, cfg, overlay_payload(
                None, None, status, dot, None, cfg["llm"]["model"]))

        prev_url = page.url
        probe_count = 0
        last_beat = time.time()
        last_live_scan = 0.0
        last_live_url = ""
        state_cache: dict = {"url": "", "ts": 0.0, "state": ""}
        last_page_state = ""
        last_state_ts = 0.0

        if not args.once:
            await push_idle()
            log(f"当前页面：{prev_url}")
            log("提示：脚本抓的是浏览器「当前页面」。停在首页时，请在窗口里点进正在上的课，"
                "或下次用 --url 直接给听课页地址。")


        current_sig: str | None = None
        last_seen: float = 0.0
        cache: dict[str, dict] = {}          # signature -> {ts, advice}
        warned: set[tuple[str, int]] = set()
        task: asyncio.Task | None = None
        last_cd_sec: int | None = None
        last_overlay_second: int | None = None
        # 浮层当前该显示的内容：倒计时每秒变化时据此重绘，不必等新答案
        state = {"advice": None, "status": "", "dot": "idle", "extra": ""}
        filled_sigs: set[str] = set()

        async def maybe_auto_answer(qq: dict, adv: dict | None) -> str:
            """按开关把答案点到页面上；返回一句给日志/浮层的说明（空串表示没做事）。"""
            if not adv or not cfg.get("auto_fill"):
                return ""
            if qq["signature"] in filled_sigs:
                return ""
            filled_sigs.add(qq["signature"])
            try:
                line = await auto_answer(page, qq, adv, cfg)
            except Exception as exc:
                line = f"自动作答异常：{type(exc).__name__}: {exc}"
            log(line)
            return line

        miss_since = None          # 课堂页「一直没题」的起点
        last_census = 0.0          # 上次自动快照时间
        last_unblock = 0.0         # 上次尝试关弹窗时间
        last_hidden_warn = 0.0     # 上次提醒「窗口被最小化」的时间
        last_probe_miss = ""
        last_probe_capture = ""

        def time_left() -> float:
            return (end_time - datetime.now()).total_seconds()

        while time_left() > 0:
            try:
                if page.is_closed():
                    log("页面被关闭，重新打开。")
                    page = await context.new_page()
                    await page.goto(url, wait_until="domcontentloaded")

                q = await probe_page(page, cfg)
                now_ts = time.time()
                probe_count += 1

                # 题目可能弹在新标签页/新窗口里：顺手把同一浏览器的其它页面也探一遍
                if not q and not args.mock:
                    others = [pg for pg in context.pages
                              if pg is not page and not pg.is_closed()]
                    for other in others:
                        try:
                            q_other = await probe_page(other, cfg)
                        except Exception:
                            q_other = None
                        if q_other:
                            log(f"[页面] 题目出现在另一个标签页：{clip(other.url, 70)}")
                            page = other
                            q = q_other
                            now_ts = time.time()
                            break

                if args.once:
                    print(json.dumps(q, ensure_ascii=False, indent=2) if q else "未识别到题目")
                    break

                # 用户手动在浏览器里切页面（首页 → 听课页）时立刻告知，并重挂空态浮层
                cur_url = page.url
                if cur_url != prev_url:
                    prev_url = cur_url
                    log(f"[页面] 现在监控：{cur_url}（frame {len(page.frames)} 个）")
                    if not q:
                        await push_idle()

                if not q and "/exercise/" in cur_url:
                    detail = _PROBE_DETAILS.get(id(page), {})
                    dom, image = detail.get("dom", {}), detail.get("ocr", {})
                    raw = " ".join((image.get("raw") or "").split())
                    summary = (f"DOM 按钮={''.join(dom.get('labels', [])) or '无'}，"
                               f"OCR 选项={''.join(image.get('labels', [])) or '无'}，"
                               f"题干={'有' if image.get('stem') else '无'}；"
                               f"{detail.get('rejected') or '题目内容尚不完整'}")
                    if image:
                        summary += (f"｜引擎={image.get('engine') or '?'}"
                                    f"，OCR 首段={clip(raw, 60) if raw else '空'}")
                    miss_key = cur_url + summary
                    if miss_key != last_probe_miss:
                        log(f"[抓题诊断] 已进入题目页但未识别成功：{summary}")
                        last_probe_miss = miss_key
                    if cur_url != last_probe_capture:
                        last_probe_capture = cur_url
                        name = await dump_census(page, cfg, "question_miss")
                        log(f"[抓题诊断] 已保存题目现场 dump\\{name}")

                # 停在首页/日志页且没题时，提示（或自动进）正在进行的课
                cur_is_lesson = bool(re.search(r"/lesson/(fullscreen|student)", cur_url or "", re.I))
                cur_is_hub = bool(re.search(r"/(web|v2/web)/|studentlog|student-lesson",
                                            cur_url or "", re.I))
                if not q and cur_is_hub and not cur_is_lesson:
                    if now_ts - last_live_scan > 25 or cur_url != last_live_url:
                        last_live_scan = now_ts
                        last_live_url = cur_url

                        # 先问官方接口：现在到底有没有课在上
                        live_api = await current_live_lesson(page)
                        if live_api:
                            log(f"接口显示正在上课：{clip(live_api['course'], 20)} · "
                                f"{clip(live_api['classroom'], 24)}")
                            if cfg.get("auto_enter"):
                                log("自动进入直播间…")
                                try:
                                    await page.goto(live_api["url"], wait_until="domcontentloaded",
                                                    timeout=60000)
                                    await asyncio.sleep(2.0)
                                    closed = await dismiss_blockers(page)
                                    if closed:
                                        log(f"关掉了挡路的弹窗：{'/'.join(closed)}")
                                        await asyncio.sleep(1.0)
                                    prev_url = page.url
                                    await push_idle("已进入直播间，等待题目…")
                                    continue
                                except Exception as exc:
                                    log(f"进入直播间失败：{type(exc).__name__}: {clip(str(exc), 80)}")
                            else:
                                log("想看这节课就打开：" + live_api["url"])
                                log("（或加 --auto-enter 让它自己进）")

                        live = await find_live_courses(page)
                        if live:
                            names = " | ".join(c["text"] for c in live[:3])
                            log(f"首页发现相关入口：{clip(names, 120)}")
                            joinable = [i for i, c in enumerate(live) if c.get("join")]
                            if cfg.get("auto_enter"):
                                if not joinable:
                                    log("没找到能安全点击的「进课堂」入口（「开启授课」这类按钮不敢自动点，"
                                        "点了会新开课），请手动点一下。")
                                else:
                                    ok = await click_live_course(page, joinable[0])
                                    log(f"已尝试点进「{clip(live[joinable[0]]['text'], 30)}」"
                                        f"{'，1.5 秒后重新探测' if ok else '，但点击失败，请手动进入'}")
                                    if ok:
                                        await asyncio.sleep(1.5)
                                        continue

                # 取回模型结果
                if q:
                    last_seen = now_ts
                    last_cd_sec = q.get("countdown")

                    async def push() -> None:
                        """按当前 state 重绘浮层（倒计时变了也走这里）。"""
                        if not q:
                            return
                        await set_overlay(page, cfg, overlay_payload(
                            q, state["advice"], state["status"], state["dot"],
                            last_cd_sec, cfg["llm"]["model"], extra=state["extra"]))

                    if q["signature"] != current_sig:
                        current_sig = q["signature"]
                        cached = cache.get(current_sig)
                        fresh = bool(cached and now_ts - cached["ts"] < cfg["answer_cache_ttl"])

                        opt_desc = " / ".join(f'{o["label"]}.{clip(o["text"], 24)}' for o in q["options"][:6])
                        tag = "〔图片题·OCR〕" if q.get("ocr") else ""
                        log(f"★ 新题目 {tag}[{q['kind']}] 剩余 {mmss(q['countdown'])} | "
                            f"{clip(q['stem'], 70)}" + (f" | 选项：{opt_desc}" if opt_desc else ""))
                        if q.get("ocr"):
                            log(f"   OCR 原文：{clip(q.get('raw', ''), 120)}")

                        # 一出现就已经低于阈值的不必再提醒，等真正跨过阈值时再响
                        cd0 = q.get("countdown")
                        if cd0 is not None:
                            for th in cfg.get("low_time_warn", []):
                                if th >= cd0:
                                    warned.add((current_sig, th))

                        if fresh:
                            log("命中缓存，直接复用上一次的答案。")
                            extra = await maybe_auto_answer(q, cached["advice"])
                            state.update(advice=cached["advice"], status="已给出参考（缓存）",
                                         dot="on", extra=extra)
                            record_answer(q, cached["advice"])
                        elif not api_key:
                            state.update(advice=None, status="未配置模型 Key", dot="err", extra="")
                        else:
                            if task and not task.done():
                                task.cancel()
                            state.update(advice=None, status="AI 生成中…", dot="think", extra="")
                            task = asyncio.create_task(ask_llm(cfg, q, api_key))

                        await push()

                        if cfg.get("save_screenshots"):
                            try:
                                SHOT_DIR.mkdir(exist_ok=True)
                                shot = SHOT_DIR / f"q_{datetime.now():%H%M%S}_{q['signature'][:6]}.png"
                                await page.screenshot(path=str(shot))
                                log(f"已截图：{shot.name}")
                            except Exception:
                                pass

                        if q.get("has_images"):
                            log("注意：题目含图片/公式，纯文本可能不全，建议对照浮层/截图。")

                    # 倒计时与低时间提醒
                    cd = q.get("countdown")
                    if cd is not None:
                        for th in cfg.get("low_time_warn", []):
                            if cd <= th and (current_sig, th) not in warned:
                                warned.add((current_sig, th))
                                log(f"⏰ 剩余时间不足 {th} 秒（当前 {mmss(cd)}），抓紧！")
                                if cfg.get("sound"):
                                    beep(3)

                else:
                    if current_sig and now_ts - last_seen > cfg["question_gone_reset"]:
                        current_sig = None
                        last_cd_sec = None
                        state.update(advice=None, status="", dot="idle", extra="")
                        log("题目已消失，回到等待状态。")
                        await push_idle("等待题目…")

                # 取回模型结果
                if task and task.done():
                    exc = task.exception()
                    result = None
                    if exc is not None:
                        log(f"模型调用异常：{exc}")
                    else:
                        result = task.result()
                    task = None
                    if q and q["signature"] == current_sig:
                        if result and result.get("ok"):
                            advice = normalize_llm_answer(result["data"], q)
                            cache[current_sig] = {"ts": now_ts, "advice": advice}
                            log(f"AI 建议：{advice['answer'] or '（空）'}"
                                f"{(' / ' + clip(advice['answer_text'], 40)) if advice.get('answer_text') else ''}"
                                f" 置信度 {advice['confidence']:.0%} | {advice['reason']}")
                            record_answer(q, advice)
                            extra = await maybe_auto_answer(q, advice)
                            state.update(advice=advice,
                                         status="已点击提交，等待网页确认" if "并点击提交" in extra else "参考已生成",
                                         dot="on", extra=extra)
                            await push()
                        else:
                            err = (result or {}).get("error", "未知错误")
                            log(f"模型调用失败：{clip(err, 200)}")
                            state.update(advice=None, status="模型调用失败", dot="err",
                                         extra=clip(err, 60))
                            await push()

                # 倒计时每秒刷新浮层（答案内容保持不变）
                if q and last_cd_sec is not None and last_overlay_second != last_cd_sec:
                    last_overlay_second = last_cd_sec
                    await push()

                # 手动触发：在程序目录里新建一个 dump_now 文件，3 秒内会存一份现场快照
                trigger = BASE_DIR / "dump_now"
                if not args.mock and trigger.exists():
                    try:
                        trigger.unlink()
                        name = await dump_census(page, cfg, "manual")
                        log(f"[诊断] 已按你的要求存现场快照 dump\\{name}")
                    except Exception as exc:
                        log(f"[诊断] 手动快照失败：{type(exc).__name__}: {exc}")

                # 页面状态提示：不做周期性心跳，只在「状态变化」或「长时间没进课堂页」时说话
                if not args.mock:
                    st = await current_page_state(page, state_cache, now_ts)
                    if st and st != last_page_state:
                        last_page_state = st
                        last_state_ts = now_ts
                        log(PAGE_HINT.get(st, "").format(url=clip(page.url, 70)))
                    elif st and st != "classroom" and now_ts - last_state_ts > float(cfg.get("state_repeat", 300)):
                        last_state_ts = now_ts
                        log("仍在非课堂页｜" + PAGE_HINT.get(st, "").format(url=clip(page.url, 70)))
                    if st == "classroom" and not q:
                        last_state_ts = now_ts if last_page_state != "classroom" else last_state_ts
                        # 窗口最小化时 Chromium 会暂停页面渲染（canvas/rAF），图片题可能截到旧画面
                        if not args.headless and now_ts - last_hidden_warn > 300:
                            try:
                                hidden = await page.evaluate("document.visibilityState === 'hidden'")
                            except Exception:
                                hidden = False
                            if hidden:
                                last_hidden_warn = now_ts
                                log("⚠ 浏览器窗口被最小化了：文字题照常抓，图片题可能截到旧画面。"
                                    "建议把窗口还原（被别的窗口盖住没关系），或改用 --headless / HTTP 版。")
                        if now_ts - last_unblock >= float(cfg.get("unblock_gap", 15)):
                            last_unblock = now_ts
                            try:
                                closed = await dismiss_blockers(page)
                            except Exception:
                                closed = []
                            if closed:
                                log(f"关掉了挡路的弹窗：{'/'.join(closed)}")
                        if miss_since is None:
                            miss_since = now_ts
                        window = float(cfg.get("dump_on_miss") or 0)
                        if (window > 0 and now_ts - miss_since >= window
                                and now_ts - last_census >= float(cfg.get("dump_cooldown") or 0)):
                            last_census = now_ts
                            try:
                                name = await dump_census(page, cfg, "miss")
                                log(f"[诊断] 课堂页连续 {int(now_ts - miss_since)} 秒没识别到题目，"
                                    f"已存现场快照 dump\\{name}（可发我校准选择器）")
                            except Exception as exc:
                                log(f"[诊断] 快照保存失败：{type(exc).__name__}: {exc}")
                    else:
                        miss_since = None

                # 心跳：默认关闭（heartbeat=0），需要时用 --heartbeat N 打开
                beat = float(cfg.get("heartbeat") or 0)
                if beat > 0 and time.time() - last_beat >= beat:
                    last_beat = time.time()
                    if q:
                        log(f"[心跳] 探测 {probe_count} 次 | 当前 [{q['kind']}] 剩余 "
                            f"{mmss(q['countdown'])} | {clip(q['stem'], 30)}")
                    else:
                        log(f"[心跳] 探测 {probe_count} 次 | frame {len(page.frames)} 个 | "
                            f"未发现题目 | {clip(page.url, 62)}")

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"本轮探测异常（忽略继续）：{type(exc).__name__}: {exc}")
                if args.debug:
                    traceback.print_exc()

            await asyncio.sleep(cfg["poll_interval"])

        log("监控时段结束，退出。")
        try:
            await context.close()
        except Exception:
            pass


CENSUS_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = {
    url: location.href.slice(0, 200),
    title: (document.title || '').slice(0, 80),
    shadowHosts: 0, canvases: 0, radios: 0, checks: 0, textInputs: 0,
    buttons: [], blocks: [], optionElements: [],
  };
  const all = document.querySelectorAll('*');
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (el.shadowRoot) out.shadowHosts++;
    if (!/^[A-H]$/.test(clean(el.innerText || ''))) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 10 || r.height < 10 || out.optionElements.length >= 24) continue;
    out.optionElements.push({tag: el.tagName, cls: String(el.className || ''),
      text: clean(el.innerText), x: r.x, y: r.y, width: r.width, height: r.height,
      html: el.outerHTML.slice(0, 600)});
  }
  out.canvases = document.querySelectorAll('canvas').length;
  out.radios = document.querySelectorAll('input[type=radio]').length;
  out.checks = document.querySelectorAll('input[type=checkbox]').length;
  out.textInputs = document.querySelectorAll('input[type=text],input:not([type]),textarea').length;

  const btns = document.querySelectorAll('button,[role=button],a,input[type=submit]');
  for (let i = 0; i < btns.length && out.buttons.length < 15; i++) {
    const t = clean(btns[i].innerText || btns[i].value || '');
    if (t && t.length <= 14) out.buttons.push(t);
  }

  const cands = document.querySelectorAll('div,section,article,form,li,ul,ol,td,main');
  const picked = [];
  for (let i = 0; i < cands.length; i++) {
    const el = cands[i];
    const r = el.getBoundingClientRect();
    if (r.width < 120 || r.height < 60) continue;
    const txt = (el.innerText || '').trim();
    if (txt.length < 6 || txt.length > 5000) continue;
    const opts = (txt.match(/(?:^|[\s\u3000])[A-Ha-h]\s*[.、．)）]/g) || []).length;
    const bare = (txt.match(/(?:^|[\s\u3000])[A-Ha-h][\s\u3000]+[^\s\u3000]/g) || []).length;
    picked.push({
      tag: el.tagName.toLowerCase(),
      cls: (el.className || '').toString().slice(0, 90),
      id: (el.id || '').slice(0, 40),
      area: Math.round(r.width * r.height),
      options: opts, bareOptions: bare,
      text: clean(txt).slice(0, 220),
    });
  }
  picked.sort((a, b) => (b.options + b.bareOptions) - (a.options + a.bareOptions) || a.area - b.area);
  out.blocks = picked.slice(0, 14);
  return out;
}
"""


async def dump_census(page, cfg: dict, reason: str = "miss") -> str:
    """把当前页（含所有 frame）的结构快照写进 dump\，用来按真实页面校准选择器。"""
    DUMP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    frames = []
    for fr in page.frames:
        item = {"url": fr.url[:200]}
        try:
            item["census"] = await fr.evaluate(CENSUS_JS)
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"[:200]
        frames.append(item)
    try:
        probe = await probe_page(page, cfg)
    except Exception as exc:
        probe = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    shot = SHOT_DIR / f"census_{stamp}_{reason}.png"
    SHOT_DIR.mkdir(exist_ok=True)
    try:
        await page.screenshot(path=str(shot), full_page=False)
    except Exception:
        shot = None
    data = {
        "reason": reason,
        "page_url": page.url,
        "at": datetime.now().isoformat(timespec="seconds"),
        "frame_count": len(page.frames),
        "probe": probe,
        "probe_details": _PROBE_DETAILS.get(id(page), {}),
        "frames": frames,
        "screenshot": shot.name if shot else None,
    }
    path = DUMP_DIR / f"census_{stamp}_{reason}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.name


# ====================================================================
# 现场快照：抓不到题目时用这个校准选择器
# ====================================================================
async def dump(cfg: dict, args) -> None:
    DUMP_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        context, page = await open_browser(p, headless=False,
                                           kill_stale=getattr(args, "kill_stale", False))
        await page.goto(cfg["url"], wait_until="domcontentloaded")
        print(">> 请在浏览器里进入「正在上课/有题目弹出」的页面。")
        print(">> 准备好后回到本终端按回车，程序会保存截图、HTML，并用当前规则试解析。")
        try:
            await asyncio.get_event_loop().run_in_executor(None, input)
        except Exception:
            await asyncio.sleep(5)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot = DUMP_DIR / f"shot_{stamp}.png"
        html = DUMP_DIR / f"page_{stamp}.html"
        try:
            await page.screenshot(path=str(shot), full_page=False)
            html.write_text(await page.content(), encoding="utf-8")
        except Exception as exc:
            log(f"保存失败：{exc}")
        res = await probe_page(page, cfg)
        if res:
            log("解析结果：")
            print(json.dumps(res, ensure_ascii=False, indent=2)[:3000])
        else:
            log("没解析出题目。请把该页面的截图发我，或查看 HTML 里题目的 class 名。")
        log(f"已保存：{shot.name} / {html.name}")
        await asyncio.sleep(1)
        await context.close()


# ====================================================================
# 命令行
# ====================================================================
def parse_window(value: str) -> tuple[datetime, datetime]:
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*[-~—到至]\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*", value)
    if not m:
        raise argparse.ArgumentTypeError(
            "时间窗口格式应为 08:00-09:40（也支持 8:00~9:40、08:00到09:40）")
    h1, m1, h2, m2 = map(int, m.groups())
    if not (0 <= h1 < 24 and 0 <= m1 < 60 and 0 <= h2 < 24 and 0 <= m2 < 60):
        raise argparse.ArgumentTypeError("时间窗口里的时/分不合法")
    now = datetime.now()
    start = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
    end = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
    if end <= start:          # 跨零点，如 23:30-00:30
        end += timedelta(days=1)
    if now >= end:            # 今天这段已经过完了：别傻等到明天
        raise argparse.ArgumentTypeError(
            f"今天 {h1:02d}:{m1:02d}-{h2:02d}:{m2:02d} 已经结束；想立刻开始请改用 "
            f"--duration 45")
    if now > start:           # 已经在窗口中间：立刻开始，到 end 结束
        start = now
    return start, end


def main() -> None:
    ap = argparse.ArgumentParser(
        description="长江雨课堂：定时抓题 + AI 参考作答（不自动提交）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--login", action="store_true", help="打开浏览器扫码登录并保存登录态")
    ap.add_argument("--login-timeout", type=int, default=300, help="扫码登录等待秒数，默认 300")
    ap.add_argument("--dump", action="store_true", help="调试：保存当前页面截图/HTML 并试解析")
    ap.add_argument("--mock", action="store_true", help="用本地模拟题页面试跑（无需登录）")
    ap.add_argument("--once", action="store_true", help="只探测一次，打印抓到的题目 JSON")
    ap.add_argument("--window", type=parse_window, metavar="08:00-09:40",
                    help="只在指定时间窗口内监控，如 --window 08:00-09:40")
    ap.add_argument("--duration", type=float, default=None, help="监控时长（分钟，可小数），默认 45")
    ap.add_argument("--mock-query", default=None,
                    help="配合 --mock 使用，覆盖模拟页查询串，如 't=45&auto=1'")
    ap.add_argument("--interval", type=float, default=None, help="轮询间隔（秒），默认 2.5")
    ap.add_argument("--url", default=None, help="覆盖入口 URL（如某节课的听课页）")
    ap.add_argument("--headless", action="store_true", help="无头模式（不显示浏览器窗口）")
    ap.add_argument("--api-key", default=None, help="直接指定模型 API Key（默认读环境变量/DSH 凭据）")
    ap.add_argument("--model", default=None, help="覆盖模型名，如 deepseek-chat")
    ap.add_argument("--base-url", default=None, help="覆盖 OpenAI 兼容接口地址")
    ap.add_argument("--no-overlay", action="store_true", help="不在网页里显示浮层")
    ap.add_argument("--no-sound", action="store_true", help="关闭提示音")
    ap.add_argument("--debug", action="store_true", help="打印异常堆栈")
    ap.add_argument("--keep-open", action="store_true", help="登录后不自动关闭浏览器")
    ap.add_argument("--kill-stale", action="store_true",
                    help="启动前清理正占着 edge_profile 的残留 Edge 进程（上次异常退出的情况）")
    ap.add_argument("--config", default=None, help="指定配置文件路径")
    ap.add_argument("--heartbeat", type=float, default=None,
                    help="心跳日志间隔（秒），默认 20；0 表示关闭")
    ap.add_argument("--auto-enter", action="store_true", default=True,
                    help="用官方接口发现正在上的课后自动跳进上课页（默认开）")
    ap.add_argument("--no-auto-enter", dest="auto_enter", action="store_false",
                    help="不自动跳转，只打印正在上的课")
    ap.add_argument("--auto-fill", action="store_true",
                    help="把 AI 建议点到网页选项上（只填不交；默认只看不点）")
    ap.add_argument("--auto-submit", action="store_true",
                    help="配合 --auto-fill：填完自动点「提交」（会真实提交，谨慎）")
    ap.add_argument("--no-ocr", action="store_true",
                    help="关闭图片题 OCR（默认开启，用 Windows 自带 OCR 读题目图片）")
    args = ap.parse_args()

    cfg = load_config(Path(args.config) if args.config else None)
    if args.url:
        cfg["url"] = args.url
    if args.interval:
        cfg["poll_interval"] = args.interval
    if args.heartbeat is not None:
        cfg["heartbeat"] = args.heartbeat
    if args.auto_enter:
        cfg["auto_enter"] = True
    if args.auto_fill:
        cfg["auto_fill"] = True
    if args.auto_submit:
        cfg["auto_fill"] = True          # 提交必须建立在填过的基础上
        cfg["auto_submit"] = True
    if args.model:
        cfg["llm"]["model"] = args.model
    if args.base_url:
        cfg["llm"]["base_url"] = args.base_url
    if args.no_ocr:
        cfg["ocr"] = False
    if args.no_overlay:
        cfg["overlay"] = False
    if args.no_sound:
        cfg["sound"] = False
    cfg["duration_min"] = args.duration or cfg["duration_min"]
    if args.duration is None:
        args.duration = cfg["duration_min"]

    if args.window:
        # argparse 的 type=parse_window 已经把字符串解析成 (start, end)
        args.window, args.end_time = args.window
    else:
        args.end_time = None

    save_config_if_missing(cfg)

    if args.login:
        asyncio.run(do_login(cfg, args))
    elif args.dump:
        asyncio.run(dump(cfg, args))
    elif args.once:
        asyncio.run(watch(cfg, args))
    else:
        asyncio.run(watch(cfg, args))


if __name__ == "__main__":
    main()
