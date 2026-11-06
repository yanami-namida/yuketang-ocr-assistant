# 第三方依赖与参考来源

本仓库源码采用 [MIT 许可证](LICENSE)。第三方组件仍适用各自的许可证；
本仓库未打包浏览器、Windows OCR 引擎或以下依赖的源码。

Windows EXE 版本内置 Python、Tcl/Tk 及运行依赖。下载 Windows ZIP 时请保留随附的
`licenses/` 目录，其中包含依赖版本清单和从构建环境收集的许可证、版权及第三方声明。
Python 及 Tcl/Tk 按各自许可证分发；PyInstaller 引导程序适用其 GPL 许可证及
[引导程序分发例外](https://pyinstaller.org/en/stable/license.html)，本项目源码仍采用 MIT。

## 运行依赖

- [Playwright for Python](https://github.com/microsoft/playwright-python)：浏览器控制。
- [Pillow](https://github.com/python-pillow/Pillow)：图片处理。
- [Requests](https://github.com/psf/requests)：调用模型服务（`/chat/completions`）。
- Windows `Windows.Media.Ocr` 和 Microsoft Edge 由用户自行安装，适用其原有条款。

## 演示素材与名称

`docs/overlay-demo.png` 来自本项目本地模拟页面；`docs/demo-question.svg` 是合成题目，
不包含真实课堂截图或账号信息。
雨课堂、长江雨课堂、Microsoft、DeepSeek 等名称归各自权利人所有；本项目为非官方工具。
