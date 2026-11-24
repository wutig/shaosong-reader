# 恢复题库模式与山河复归

## 先用示例体验

1. 在项目根目录运行导入命令。示例课程由三道原创演示题组成，带题干、解析、原图和整份 PDF。

   ```sh
   python import_subject.py --package examples/sample-course
   ```

2. 结束此前运行的 PDF 阅读器服务，或为题库服务选另一个端口。在项目根目录运行：

   ```sh
   python server.py --study --port 8765
   ```

3. 打开 [本机复习中心](http://127.0.0.1:8765/)，点击「过河！过河！过河！」，进入「数学演示课」。可以展开原题图片、查看解析、记笔记，并用「还不会」筛出待复习的题。
4. 点击一道题的「标记本轮已复习」，等保存完成后，点课程页的「收复山河」入口，或返回首页打开地图。只导入这门示例课时，一题完成对应约 **33.3%** 的待收复区域。

地图按 **全部已保存的复习完成题数 ÷ 当前各课程总题数** 计算，并依比例更新着色面积。点开原图、阅读 PDF、展开答案和写笔记都各有用途，完成题数以「本轮已复习」的保存状态为准。撤销完成后地图重新计算。跨过相应进度会解锁北伐军报。

PDF 模式通过 `python server.py` 启动。也可以保留 PDF 阅读器在 `8765`，另开终端运行 `python server.py --study --port 8767`，同时使用两个入口。

## 导入自己的题库

复制一份 `examples/sample-course` 到 `content/` 之外并重命名，然后把示例内容换成自己的题目和附件。整理后的目录可以像这样：

```text
my-course/
├── book.json
├── worksheet.pdf
└── images/
    └── p01.png
```

编辑 [示例 book.json](examples/sample-course/book.json) 时留意这些字段：

| 字段 | 如何填写 |
| --- | --- |
| `id` | 学科的稳定 ID，例如 `my-course`。题目各有一个学科内唯一的 `id`，例如 `Q001`。ID 使用字母、数字、下划线和短横线，以字母或数字开头。以后重排只改 `number`，这样原笔记仍对应原题。 |
| `title`、`subtitle`、`scope` | 页面显示的学科名称、简介和资料范围。 |
| `questions` | 各题的 `title`、`topic`、`question`、`answer` 等。题干、解析和追问支持 Markdown，公式写成 `$…$` 或 `$$…$$`。 |
| `source_pages`、`sources` | 题目的 `source_pages` 引用 `sources` 里的 `page`。一个来源页可关联多题，也可给一道题关联多个来源页。`sources[].images` 每张填写图像路径、原始宽高和旋转角度。 |
| `sources[].images[].url` | 示例 `/content/my-course/images/p01.png`。前缀中的 `my-course` 与学科 ID 一致，后面的路径与包内文件一致。 |
| `pdf_url` | 可选。附上整份 PDF 时填 `/content/my-course/worksheet.pdf`。需要逐题展开 PDF 中的原页时，可先将对应页截成 PNG 或 JPEG，再按来源页加入 `images/`。 |

导入自己的包，刷新题库页面即可看到新课程：

```sh
python import_subject.py --package "my-course"
```

整门课会导入到 `content/<学科ID>/`。用相同学科 ID 重新导入时，提供完整的新课程包，包含要保留的题目及附件。原课程文件会留在 `.history/content/`，复习记录随原 ID 保留。已有符合格式的 `book.json` 与附件也可以用同一命令导入。

仅体验过示例，想从自己的题开始时，把 `content/sample-math/` 移到 `content/` 外再刷新页面。示例的题目会退出课程目录，示例笔记仍存在记录目录中。

## 保存与迁移

笔记、标记和完成状态保存在运行目录下的 `我的复习记录/` 中。迁移自己的学习数据时，先在新环境导入相同 ID 的课程包，再用题库页右上角「恢复备份」导入旧环境通过「备份记录」下载的 ZIP。分享项目代码给别人时，请分别保管自己的课程包、原题、PDF 和备份。

## 更换封面或地图

阅读器和题库模式共用封面，可按 [换图说明](CUSTOM_THEME.md) 放入自己的素材。

公开版的复习地图使用可再分发的简化地理数据。若已有兼容的旧版 `campaign-data.json` 且具备相应使用权限，可另存为 `web/campaign.local.json`，刷新题库模式即可恢复原地图细节。Git 默认忽略该文件。两种地图都根据题库完成比例着色。
