"""在 Windows x64 上构建带图形配置界面的单文件 EXE 与分发 ZIP。"""
from __future__ import annotations

import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from ykt_version import VERSION


def collect_licenses(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    packages = sorted(dist.metadata["Name"] for dist in metadata.distributions())
    versions = {}
    for name in packages:
        dist = metadata.distribution(name)
        versions[name] = dist.version
        for relative in dist.files or []:
            value = str(relative).lower()
            if any(word in Path(value).name for word in ("license", "notice", "copying")):
                source = Path(dist.locate_file(relative))
                if source.is_file():
                    target = destination / name / Path(relative)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
    base = Path(sys.base_prefix)
    for source in [*base.glob("LICENSE*"), *base.glob("tcl/*/license.terms")]:
        if source.is_file():
            target = destination / "python" / source.relative_to(base)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    (destination / "BUNDLED_DEPENDENCIES.json").write_text(
        json.dumps({"python": sys.version.split()[0], **versions}, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if os.name != "nt":
        raise SystemExit("Windows EXE 必须在 Windows 上构建")
    work = ROOT / "build" / "windows"
    output = ROOT / "dist" / "windows"
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    licenses = work / "bundled-licenses"
    collect_licenses(licenses)
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
               "--windowed", "--noupx", "--name", "YuketangAssistant", "--collect-all", "playwright",
               "--runtime-tmpdir", r"%LOCALAPPDATA%\YuketangOCRAssistant\runtime",
               "--distpath", str(output), "--workpath", str(work), "--specpath", str(work)]
    command += ["--add-data", str(licenses) + os.pathsep + "licenses"]
    for source, dest in [("config.example.json", "."), ("config.http.example.json", "."),
                         ("mock_quiz.html", "."), ("mock_image.html", "."),
                         ("docs/demo-question.svg", "docs"), ("LICENSE", "."),
                         ("THIRD_PARTY_NOTICES.md", ".")]:
        command += ["--add-data", str(ROOT / source) + os.pathsep + dest]
    command.append(str(ROOT / "ykt_desktop.py"))
    subprocess.run(command, cwd=ROOT, check=True)
    bundle = work / "release-content"
    bundle.mkdir(parents=True, exist_ok=True)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "SECURITY.md"):
        shutil.copyfile(ROOT / name, bundle / name)
    shutil.copyfile(ROOT / "docs" / "desktop.md", bundle / "使用说明.md")
    shutil.copytree(licenses, bundle / "licenses", dirs_exist_ok=True)
    exe = output / "YuketangAssistant.exe"
    archive = output / f"yuketang-ocr-assistant-v{VERSION}-windows-x64.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        package.write(exe, "YuketangAssistant.exe")
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                package.write(path, str(path.relative_to(bundle)))
    checksums = []
    for path in (exe, archive):
        checksums.append(hashlib.sha256(path.read_bytes()).hexdigest() + "  " + path.name)
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")
    print(f"Windows build ready: {archive}")


if __name__ == "__main__":
    main()
