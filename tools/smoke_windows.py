"""验证冻结程序、独立数据目录及界面启动/停止后台任务，不访问真实账号或模型。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    import tkinter as tk
    import ykt_desktop as desktop
    executable = ROOT / "dist" / "windows" / "YuketangAssistant.exe"
    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="desktop-smoke-", dir=ROOT / "build") as tmp:
        temporary = Path(tmp)
        data = temporary / "data"
        data.mkdir()
        env = dict(os.environ, LOCALAPPDATA=str(temporary), YKT_DATA_DIR=str(data))
        result_path = temporary / "result.json"
        result = subprocess.run([str(executable), "--worker", "selftest", str(result_path)],
                                env=env, timeout=60)
        if result.returncode:
            log = data / "desktop-session.log"
            raise RuntimeError(log.read_text(encoding="utf-8") if log.exists() else "EXE bootstrap failed")
        report = json.loads(result_path.read_text(encoding="utf-8"))
        with patch.dict(os.environ, env), patch.object(sys, "frozen", True, create=True), \
                patch.object(sys, "executable", str(executable)), \
                patch.object(desktop.set_api_key, "CONFIG_FILE", data / "config.json"):
            root = tk.Tk()
            root.withdraw()
            app = desktop.DesktopApp(root)
            def wait_until(predicate, seconds=35):
                deadline = time.monotonic() + seconds
                while not predicate():
                    root.update()
                    if time.monotonic() > deadline:
                        raise TimeoutError("Desktop worker did not finish")
                    time.sleep(0.1)
            try:
                app.launch("demo", ["--mock", "--once", "--headless"], "自动测试", demo=True)
                wait_until(lambda: app.process is None)
                assert app.status.get() == "任务已结束", app.logs.get("1.0", "end")
                assert "主键" in app.logs.get("1.0", "end")
                app.launch("demo", ["--mock", "--duration", "1", "--headless"], "停止测试", demo=True)
                wait_until(lambda: "已加载" in app.log_path.read_text(encoding="utf-8"))
                app.stop()
                wait_until(lambda: app.process is None)
                assert app.status.get() == "任务已停止", app.logs.get("1.0", "end")
                report.update(gui_start=True, gui_stop=True, isolated_demo=True)
            finally:
                if app.process is not None:
                    app.stopping = True
                    app.force_stop(app.process)
                    app.process.wait(timeout=10)
                root.destroy()
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
