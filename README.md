# 山河阅卷 · 绍宋风 PDF 阅读器

以朱砂、宣纸和水墨为主色的本地 PDF 阅读器，保留红袍人物、滚动诗句、沉浸全屏和原复习中心的阅读界面。可以直接从设备选择 PDF，翻页、跳页和缩放。

## 启动

1. 在 GitHub 点击 **Code → Download ZIP** 并解压，或克隆仓库。
2. 安装 Python 3.10 或更新版本。在含 `server.py` 的目录打开终端，运行：

   ```sh
   python server.py
   ```

   Windows 可用 `py -3 server.py`，macOS 和 Linux 可用 `python3 server.py`。默认端口 `8765` 已被占用时，运行 `python server.py --port 8767`。

3. 用较新的浏览器打开 [本地阅读器](http://127.0.0.1:8765/)，点击打开 PDF。仓库里的 [示例 PDF](examples/sample-course/worksheet.pdf) 可以用来试翻页和缩放。

PDF 由当前浏览器读取和渲染。启动命令会在本机提供页面和所需资源，关闭服务可在终端按 `Ctrl+C`。

## 更换封面

可使用一张完整封面，也可保留诗词从人物身后滚过的分层效果。文件放入本地素材目录后刷新即可，路径及可直接交给本机 AI 的指令见 [换图说明](CUSTOM_THEME.md)。

## 题库与山河复归

项目也保留了题库模式，支持逐题查看原图和解析、标记「还不会」、保存笔记和完成状态，以及按全科复习进度收复山河。开启方式和导入示例见 [恢复题库模式](RESTORE_STUDY_MODE.md)。

正式题库初始为空。`examples/` 是独立的原创演示素材，手动导入后才会出现在题库中。源码许可见 [LICENSE](LICENSE)，第三方素材和依赖见 [NOTICE](NOTICE)。
