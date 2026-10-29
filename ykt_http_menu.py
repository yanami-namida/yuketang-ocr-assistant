"""HTTP 版双击入口的中文菜单，避免 CMD 的中文编码差异。"""
import os
import subprocess
import sys
from pathlib import Path

from ykt_http import main

ROOT = Path(__file__).resolve().parent


def menu():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        while True:
            print("\n雨课堂 HTTP 答案上传版\n"
                  "  1. 查看正在上课的课堂\n"
                  "  2. 读取一次课堂题目\n"
                  "  3. 监控并预览模型答案\n"
                  "  4. 监控并通过 HTTP 自动上传答案\n"
                  "  5. 运行本地模拟测试\n"
                  "  0. 退出")
            choice = input("请选择：").strip()
            if choice == "0":
                return 0
            if choice == "1":
                main(["--list-lessons"])
            elif choice in ("2", "3", "4"):
                lesson = input("课堂 ID（回车自动选择唯一正在上课的课堂）：").strip()
                if lesson and not lesson.isdigit():
                    print("课堂 ID 必须是数字")
                    continue
                args = ["--lesson-id", lesson] if lesson else []
                if choice == "2":
                    args.append("--once")
                else:
                    args.extend(["--duration", "45"])
                if choice == "4":
                    print("HTTP 自动上传已开启，将向平台提交模型答案。")
                    args.append("--auto-submit")
                main(args)
            elif choice == "5":
                env = dict(os.environ)
                tmp = ROOT / ".venv-http" / "tmp"
                if tmp.is_dir():
                    env.update(TEMP=str(tmp), TMP=str(tmp))
                subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, env=env)
            else:
                print("请输入菜单中的数字")
    except (EOFError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    raise SystemExit(menu())
