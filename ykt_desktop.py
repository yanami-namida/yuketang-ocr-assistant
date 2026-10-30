"""雨课堂助手 Windows 图形界面及打包进程入口。"""
from __future__ import annotations

import copy
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from tools import set_api_key
from ykt_llm import normalize_base_url
from ykt_paths import data_dir, resource_dir
from ykt_version import VERSION


def worker_command(role: str, args: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--worker", role, *args]
    return [sys.executable, str(Path(__file__).resolve()), "--worker", role, *args]


def monitor_arguments(mode: str, duration: str, window: str, lesson: str) -> tuple[str, list[str]]:
    try:
        minutes = float(duration)
    except ValueError:
        raise ValueError("监控时长请填写大于 0 的分钟数") from None
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError("监控时长请填写大于 0 的分钟数")
    args = ["--duration", str(minutes)]
    if mode == "HTTP 模式":
        if window.strip():
            raise ValueError("HTTP 模式请使用监控时长；定时时间窗适用于浏览器模式")
        if lesson.strip():
            if not lesson.strip().isdigit():
                raise ValueError("课堂 ID 必须是数字，或留空自动选择")
            args += ["--lesson-id", lesson.strip()]
        return "http", args
    if window.strip():
        if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d\s*-\s*(?:[01]?\d|2[0-3]):[0-5]\d", window.strip()):
            raise ValueError("时间窗格式为 08:00-09:40；留空则立即开始")
        args += ["--window", window.strip()]
    return "browser", args


def update_settings(cfg: dict, base: str, model: str, key: str, reasoning: str,
                    duration: str, auto_fill: bool, auto_submit: bool) -> dict:
    monitor_arguments("浏览器模式", duration, "", "")
    if not model.strip():
        raise ValueError("请填写模型名称")
    cfg = copy.deepcopy(cfg)
    llm = set_api_key.get_llm(cfg)
    llm.update(base_url=normalize_base_url(base), model=model.strip(), api_key=key.strip(),
               reasoning_effort=None if reasoning == "默认" else reasoning,
               allow_dsh_credentials=False)
    cfg.update(duration_min=float(duration), auto_fill=bool(auto_fill or auto_submit),
               auto_submit=bool(auto_submit))
    return cfg


class DesktopApp:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        self.tk, self.ttk, self.root = tk, ttk, root
        self.root.title(f"雨课堂助手 · {VERSION}")
        self.root.geometry("1000x790")
        self.root.minsize(900, 740)
        self.root.configure(bg="#f3f6fa")
        self.directory = data_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.cfg = set_api_key.load_raw_config()
        llm = set_api_key.get_llm(self.cfg)
        self.process = None
        self.stop_file = None
        self.stopping = False
        self.closing = False
        self.checking = False
        self.log_offset = 0
        self.log_buffer = b""
        self.events = queue.Queue()
        self.log_path = self.directory / "desktop-session.log"
        self.providers = {v[0]: (v[1], v[2]) for v in set_api_key.PROVIDERS.values()}
        current = next((name for name, (base, _) in self.providers.items()
                        if base == llm.get("base_url")), "自定义")
        self.provider = tk.StringVar(value=current)
        self.base = tk.StringVar(value=llm.get("base_url", ""))
        self.model = tk.StringVar(value=llm.get("model", ""))
        self.key = tk.StringVar(value=llm.get("api_key", ""))
        self.reasoning = tk.StringVar(value=llm.get("reasoning_effort") or "默认")
        self.duration = tk.StringVar(value=str(self.cfg.get("duration_min", 45)))
        self.mode = tk.StringVar(value="浏览器模式")
        self.window = tk.StringVar()
        self.lesson = tk.StringVar()
        self.fill = tk.BooleanVar(value=bool(self.cfg.get("auto_fill", False)))
        self.submit = tk.BooleanVar(value=bool(self.cfg.get("auto_submit", False)))
        self.show_key = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="就绪 · 首次使用请先配置模型，再扫码登录")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f6fa")
        style.configure("TLabel", background="#f3f6fa", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(12, 7))
        style.configure("TCheckbutton", background="#f3f6fa", font=("Microsoft YaHei UI", 10))
        style.configure("TLabelframe", background="#f3f6fa")
        style.configure("TLabelframe.Label", background="#f3f6fa", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Primary.TButton", foreground="white", background="#2467dc")
        style.map("Primary.TButton", background=[("active", "#1955b9"), ("disabled", "#98abc5")])
        outer = ttk.Frame(root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="雨课堂助手", font=("Microsoft YaHei UI", 23, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Windows OCR 识题  /  自定义模型  /  本地保存配置", foreground="#596b83").pack(anchor="w", pady=(4, 18))
        columns = ttk.Frame(outer)
        columns.pack(fill="x")
        columns.columnconfigure(0, weight=3)
        columns.columnconfigure(1, weight=2)
        api = ttk.LabelFrame(columns, text=" 1  模型配置 ", padding=16)
        api.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        api.columnconfigure(1, weight=1)
        def field(parent, row, label, variable, values=None, masked=False):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=6)
            control = (ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
                       if values else ttk.Entry(parent, textvariable=variable, show="●" if masked else ""))
            control.grid(row=row, column=1, sticky="ew", pady=6)
            return control
        provider_box = field(api, 0, "提供方", self.provider, list(self.providers) + ["自定义"])
        provider_box.bind("<<ComboboxSelected>>", self.choose_provider)
        field(api, 1, "API 地址", self.base)
        field(api, 2, "模型名称", self.model)
        self.key_entry = field(api, 3, "API 密钥", self.key, masked=True)
        ttk.Checkbutton(api, text="显示密钥", variable=self.show_key,
                        command=lambda: self.key_entry.configure(show="" if self.show_key.get() else "●")).grid(row=4, column=1, sticky="w")
        field(api, 5, "推理强度", self.reasoning, ["默认", "none", "low", "high", "max"])
        api_actions = ttk.Frame(api)
        api_actions.grid(row=6, column=0, columnspan=2, sticky="w", pady=(10, 3))
        ttk.Button(api_actions, text="保存配置", command=self.save).pack(side="left")
        self.check_button = ttk.Button(api_actions, text="测试连接", command=self.check_connection)
        self.check_button.pack(side="left", padx=8)
        ttk.Label(api, text="支持基础地址或完整接口地址。\n连接测试会发起一次小型模型请求，可能产生少量费用。",
                  foreground="#596b83", font=("Microsoft YaHei UI", 9)).grid(row=7, column=0, columnspan=2, sticky="w", pady=(9, 0))
        task = ttk.LabelFrame(columns, text=" 2  登录与监控 ", padding=16)
        task.grid(row=0, column=1, sticky="nsew")
        task.columnconfigure(1, weight=1)
        field(task, 0, "运行模式", self.mode, ["浏览器模式", "HTTP 模式"])
        field(task, 1, "时长 / 分钟", self.duration)
        field(task, 2, "定时时间窗", self.window)
        field(task, 3, "HTTP 课堂 ID", self.lesson)
        ttk.Label(task, text="时间窗如 08:00-09:40，可留空。\nHTTP 课堂 ID 留空时选择唯一活动课堂。",
                  foreground="#596b83", font=("Microsoft YaHei UI", 9)).grid(row=4, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Checkbutton(task, text="浏览器自动填选项", variable=self.fill).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(task, text="自动提交答案（会真实提交）", variable=self.submit).grid(row=6, column=0, columnspan=2, sticky="w", pady=5)
        self.action_buttons = []
        actions = ttk.Frame(task)
        actions.grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
        for label, callback, primary in [("扫码登录", self.login, False), ("开始监控", self.start, True)]:
            button = ttk.Button(actions, text=label, command=callback, style="Primary.TButton" if primary else "TButton")
            button.pack(side="left", padx=(0, 8))
            self.action_buttons.append(button)
        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(16, 10))
        demo_button = ttk.Button(toolbar, text="离线演示", command=self.demo)
        demo_button.pack(side="left")
        self.action_buttons.append(demo_button)
        self.stop_button = ttk.Button(toolbar, text="停止任务", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(toolbar, text="打开数据目录", command=lambda: os.startfile(self.directory)).pack(side="right")
        ttk.Label(outer, textvariable=self.status, foreground="#24569b").pack(anchor="w", pady=(0, 8))
        self.logs = ScrolledText(outer, height=9, wrap="word", font=("Microsoft YaHei UI", 9),
                                 bg="#172334", fg="#dce6f3", insertbackground="white", relief="flat", padx=12, pady=10)
        self.logs.pack(fill="both", expand=True)
        self.logs.configure(state="disabled")
        ttk.Label(outer, text=f"v{VERSION} · 数据保存在 {self.directory}", foreground="#66758a",
                  font=("Microsoft YaHei UI", 8)).pack(anchor="w", pady=(10, 0))
        self.append("欢迎使用。密钥保存在本机配置中；升级程序无需重新填写。")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(200, self.poll)

    def append(self, text):
        secret = self.key.get().strip()
        if secret:
            text = text.replace(secret, "[密钥已隐藏]")
        self.logs.configure(state="normal")
        self.logs.insert("end", text.rstrip() + "\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 1600:
            self.logs.delete("1.0", "400.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def error(self, exc):
        from tkinter import messagebox
        messagebox.showerror("请检查配置", str(exc), parent=self.root)

    def choose_provider(self, _event=None):
        name = self.provider.get()
        if name in self.providers:
            base, model = self.providers[name]
            self.base.set(base)
            self.model.set(model)
            self.reasoning.set("none" if "MaaS" in name else "默认")
            if "Ollama" in name:
                self.key.set("ollama")
            else:
                # 避免将上一家提供方的密钥发送给新选的服务。
                self.key.set("")

    def save(self, notify=True):
        try:
            cfg = update_settings(self.cfg, self.base.get(), self.model.get(), self.key.get(),
                                  self.reasoning.get(), self.duration.get(), self.fill.get(), self.submit.get())
            set_api_key.save_raw_config(cfg)
            self.cfg = cfg
            if notify:
                self.append("配置已保存。")
            return cfg
        except (ValueError, OSError) as exc:
            self.error(exc)
            return None

    def check_connection(self):
        if self.checking:
            return
        cfg = self.save(False)
        if cfg is None:
            return
        key, _ = set_api_key.resolve(cfg)
        if not key:
            self.error(ValueError("请先填写 API 密钥；本地 Ollama 可填写 ollama"))
            return
        self.checking = True
        self.check_button.configure(state="disabled")
        self.append("正在测试模型连接…")
        def run():
            try:
                ok, message = set_api_key.check_key(cfg, key)
            except Exception as exc:
                ok, message = False, f"连接测试失败：{type(exc).__name__}"
            self.events.put((ok, message))
        threading.Thread(target=run, daemon=True).start()

    def launch(self, role, args, label, demo=False):
        if self.process is not None:
            return
        if not demo and self.save(False) is None:
            return
        self.log_path.write_text("", encoding="utf-8")
        self.log_offset, self.log_buffer = 0, b""
        self.stop_file = self.directory / ("desktop-stop-" + uuid.uuid4().hex)
        env = dict(os.environ, YKT_WORKER_LOG=str(self.log_path), YKT_STOP_FILE=str(self.stop_file))
        if demo:
            env["YKT_DATA_DIR"] = str(self.directory / "demo")
        try:
            self.process = subprocess.Popen(worker_command(role, args), cwd=self.directory, env=env,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self.error(exc)
            return
        self.stopping = False
        self.status.set(label)
        self.append(label)
        for button in self.action_buttons:
            button.configure(state="disabled")
        self.stop_button.configure(state="normal")

    def login(self):
        self.launch("browser", ["--login"], "等待扫码登录 · 请在 Edge 窗口中完成")

    def start(self):
        try:
            role, args = monitor_arguments(self.mode.get(), self.duration.get(), self.window.get(), self.lesson.get())
            if self.submit.get() and role == "http":
                args.append("--auto-submit")
            self.launch(role, args, "正在监控 · " + self.mode.get())
        except ValueError as exc:
            self.error(exc)

    def demo(self):
        self.launch("demo", ["--mock", "--duration", "1"], "本地演示 · 不登录、不调用模型，1 分钟后结束", demo=True)

    def stop(self):
        if self.process is None or self.stopping:
            return
        self.stopping = True
        self.stop_file.touch()
        self.status.set("正在停止任务…")
        self.root.after(10000, lambda process=self.process: self.force_stop(process))

    def force_stop(self, process):
        if self.process is process and self.stopping and self.process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def poll(self):
        while not self.events.empty():
            ok, message = self.events.get_nowait()
            self.checking = False
            self.check_button.configure(state="normal")
            self.append(("连接成功：" if ok else "连接失败：") + message)
        if self.process is not None:
            try:
                with self.log_path.open("rb") as stream:
                    stream.seek(self.log_offset)
                    new = stream.read()
                    self.log_offset += len(new)
                self.log_buffer += new
                parts = self.log_buffer.split(b"\n")
                self.log_buffer = parts.pop()
                for line in parts:
                    self.append(line.decode("utf-8", errors="replace"))
            except OSError:
                pass
            code = self.process.poll()
            if code is not None:
                if self.log_buffer:
                    self.append(self.log_buffer.decode("utf-8", errors="replace"))
                self.process = None
                self.stop_file.unlink(missing_ok=True)
                self.status.set("任务已停止" if self.stopping else ("任务已结束" if code == 0 else "任务退出，请查看日志"))
                for button in self.action_buttons:
                    button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(200, self.poll)

    def close(self):
        if self.process is not None:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()


def self_test(output: str) -> None:
    """只访问合成页面，验证打包后的 Tcl、浏览器驱动和 Windows OCR。"""
    import tempfile
    import tkinter as tk
    from PIL import Image, ImageDraw, ImageFont
    from playwright.sync_api import sync_playwright
    import ocr_win
    root = tk.Tk()
    root.withdraw()
    root.update()
    root.destroy()
    assert (resource_dir() / "config.example.json").is_file()
    assert (resource_dir() / "mock_quiz.html").is_file()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        page.goto((resource_dir() / "mock_quiz.html").as_uri())
        assert "主键" in page.locator("body").inner_text()
        browser.close()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ocr.png"
        image = Image.new("RGB", (800, 180), "white")
        font = ImageFont.truetype(str(Path(os.environ["WINDIR"]) / "Fonts" / "arial.ttf"), 52)
        ImageDraw.Draw(image).text((40, 50), "OCR TEST 12345", fill="black", font=font)
        image.save(path)
        boxes = ocr_win.ocr_boxes(path)
        recognized = " ".join(b["text"] for b in boxes)
        if boxes:
            assert "12345" in recognized, recognized
    Path(output).write_text(json.dumps({"version": VERSION, "tk": True, "edge": True,
        "ocr_available": bool(boxes), "ocr_text": recognized, "ocr_errors": ocr_win.OCR_ERRORS},
        ensure_ascii=False, indent=2), encoding="utf-8")


def run_worker(role: str, args: list[str]) -> int:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    os.environ.update(TEMP=str(temporary), TMP=str(temporary))
    import tempfile
    tempfile.tempdir = None
    log_path = Path(os.environ.get("YKT_WORKER_LOG", str(directory / "desktop-session.log")))
    with log_path.open("a", encoding="utf-8", buffering=1) as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            if role == "selftest":
                self_test(args[0])
            elif role == "http":
                import ykt_http
                return ykt_http.main(args) or 0
            else:
                if role == "demo":
                    cfg = set_api_key.load_raw_config()
                    cfg.update(auto_fill=False, auto_submit=False)
                    cfg["llm"].update(api_key="", api_key_env="YKT_DEMO_DISABLED", allow_dsh_credentials=False)
                    os.environ.pop("YKT_DEMO_DISABLED", None)
                    set_api_key.save_raw_config(cfg)
                import ykt_watcher
                sys.argv = ["ykt_watcher", *args]
                ykt_watcher.main()
            return 0
        except SystemExit as exc:
            if exc.code and not isinstance(exc.code, int):
                print(str(exc.code))
            return exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        except Exception:
            traceback.print_exc()
            return 1


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--worker":
        return run_worker(sys.argv[2], sys.argv[3:])
    import tkinter as tk
    from tkinter import messagebox
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    root = tk.Tk()
    mutex = None
    if os.name == "nt":
        import ctypes
        import hashlib
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex_name = "Local\\YuketangOCRAssistant-" + hashlib.sha256(str(data_dir()).encode()).hexdigest()[:16]
        mutex = kernel.CreateMutexW(None, False, mutex_name)
        if mutex and ctypes.get_last_error() == 183:
            root.withdraw()
            messagebox.showinfo("雨课堂助手", "程序已经在运行，请使用已打开的窗口。", parent=root)
            root.destroy()
            kernel.CloseHandle(mutex)
            return 0
    try:
        DesktopApp(root)
    except (Exception, SystemExit) as exc:
        root.withdraw()
        messagebox.showerror("无法启动雨课堂助手", str(exc), parent=root)
        root.destroy()
        return 1
    root.mainloop()
    if mutex:
        kernel.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
