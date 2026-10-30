# 第三方依赖与参考来源

本仓库源码采用 [MIT 许可证](LICENSE)。第三方组件仍适用各自的许可证；
本仓库未打包浏览器、Windows OCR 引擎或以下依赖的源码。

## 运行依赖

- [Playwright for Python](https://github.com/microsoft/playwright-python)：浏览器控制。
- [Pillow](https://github.com/python-pillow/Pillow)：图片处理。
- [Requests](https://github.com/psf/requests)：HTTP 请求。
- [websocket-client](https://github.com/websocket-client/websocket-client)：HTTP 版课堂事件监听。
- Windows `Windows.Media.Ocr` 和 Microsoft Edge 由用户自行安装，适用其原有条款。

## 协议参考

HTTP 版开发过程中参考了以下项目展示的接口字段、消息和题型映射：

- [chlchi/RainClassroomAssitant-standalone](https://github.com/chlchi/RainClassroomAssitant-standalone/blob/main/Scripts/Classes.py)。
- [AI 雨课堂助手（JS 版）](https://greasyfork.org/en/scripts/531469-ai%E9%9B%A8%E8%AF%BE%E5%A0%82%E5%8A%A9%E6%89%8B-js%E7%89%88/code)
  （页面声明 MIT，命名空间为 `ZaytsevZY/yuketang-helper-auto`）。

这些链接用于说明协议参考来源，未将其应用程序或用户脚本打包进本项目。
本项目的许可证不代表对这些外部项目重新授权。

## 演示素材与名称

`docs/overlay-demo.png` 来自本项目本地模拟页面；`docs/demo-question.svg` 是合成题目，
不包含真实课堂截图或账号信息。
雨课堂、长江雨课堂、Microsoft、DeepSeek 等名称归各自权利人所有；本项目为非官方工具。
