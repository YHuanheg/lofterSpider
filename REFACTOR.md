# 重构说明（v1.x → v2.0）

本文回答四件事：**整体方案**、**模块调整**、**Python 3.12 兼容性改动点**、**GUI 的功能范围与实现思路**，
最后是**验证方式**。

---

## 一、为什么要重构

v1.x 是 7 个独立脚本，每个都能跑，但有几个结构性问题使「加 GUI」和「上 3.12」都做不了：

| # | 问题 | 证据（重构前） |
| --- | --- | --- |
| 1 | **参数写在源码里** | 每个文件末尾 `if __name__ == '__main__':` 里一堆常量，`l13` 有 17 个，改参数＝改源码，GUI 无从下手 |
| 2 | **交互靠 `input()` 硬阻塞** | `l13` 有 2 处（阶段3 的 `ok` 闸门、结尾的 `yes/no`），`l4` 有 2 处（启动闸门、作者名兜底），`l9` 有 2 处（通用模板确认、合并日期排序），GUI 线程里调 `input()` 会直接卡死界面 |
| 3 | **失败即 `exit()`** | 全部脚本共用 `exit()`，GUI 里等于直接杀进程且没有任何可展示的原因 |
| 4 | **逻辑重复** | 文件名清洗链在 `l4/l8/l9/l10/l13` 各抄了一份；归档页解析在 `l4` 和 `l9` 各写一份；l8/l10 是同一种用法却分成两个文件 |
| 5 | **十来个参数的函数** | `l13.run(url, mode, save_mode, classify_by_tag, prior_tags, agg_non_prior_tag, login_info, start_time, tag_filt_num, min_hot, print_level, save_img_in_text, base_path)` |

同时确实有几处**真 bug**（见第六节），挑出来一起修。

---

## 二、整体方案

分三层，依赖方向单向：**GUI / CLI → 核心包（lofter）→ 第三方库**。
核心包不知道界面的存在，所以三个入口可以共用同一份抓取逻辑。

```
        ┌──────────────┐        ┌──────────────┐
        │  gui/        │        │  cli/run.py  │
        │  tkinter     │        │  argparse    │
        └──────┬───────┘        └──────┬───────┘
               │  Reporter 接口          │  Reporter 接口
               └───────────┬────────────┘
                           ▼
        ┌────────────────────────────────────────┐
        │           lofter/（核心包，无界面依赖）      │
        │  config  net  archive  templates  utils │
        │  progress  reporter  errors             │
        │  └── tasks/  七个抓取任务                  │
        └────────────────────┬───────────────────┘
                             ▼
              requests / lxml / html2text / tkinter(仅 GUI)
```

三个关键抽象：

1. **`Field` 声明**（`lofter/config.py`）
   每个配置项声明 `name / label / kind / choices / help / group`。
   GUI 用它自动生成表单、CLI 用它生成 `--set` 校验、`show-config` 用它生成帮助文本。
   → **一份声明三处使用**，新增任务不需要写界面代码。

2. **`Reporter` 接口**（`lofter/reporter.py`）
   任务只调用 `ctx.log / ctx.progress / ctx.stage / ctx.confirm / ctx.check_cancel`。
   `CliReporter` 打到终端、`QueueReporter` 投递到 GUI 队列、`NullReporter` 供测试。
   → **同一份任务代码三种运行方式**，`input()` 彻底消失。

3. **`TaskContext`**（`lofter/tasks/base.py`）
   任务能触达的全部外部能力：账号、日志通道、输出目录、Session 工厂、取消令牌。

---

## 三、模块调整对照

| 重构前 | 重构后 | 说明 |
| --- | --- | --- |
| `l13_like_share_tag.py`（1170 行） | `lofter/tasks/like_share_tag.py` | 拆成 请求参数 / 抓取 / 解析 / 分类 / 保存 五段，删掉 3 处重复的目录决策逻辑（抽成 `article_folder()`） |
| `l4_author_img.py` | `lofter/tasks/author_img.py` | 归档页解析抽到 `lofter/archive.py` |
| `l9_author_txt.py` | `lofter/tasks/author_txt.py` | 同上；评论抓取独立成 `fetch_comments()` |
| `l8_blogs_img.py` + `l10_blogs_txt.py` | `lofter/tasks/blogs.py`（合成一个） | 两者用法一致，用 `kind=img/txt` 区分 |
| `l14_default_homepage_extract.py` | `lofter/tasks/homepage.py` | 修掉全局变量 bug |
| `l15_phone_tag.py` | `lofter/tasks/phone_tag.py` | 保留实验性定位，补上进度/日志/错误处理 |
| `parse_template.py` | `lofter/templates.py` | 正则改 raw string；删掉写 `test.txt` 的副作用 |
| `useragentutil.py` | `lofter/net.py` 的 `get_headers()` | 返回字典**副本** |
| `tool.py` | 删除（功能并入 `lofter/progress.py`） | 原来只有 2 个未被调用的写文件函数 |
| `tags_tolist.py` | `legacy/` 保留 | 逻辑已被 `prior_tags.txt` + 界面取代 |
| `login_info.py` | `lofter/config.py` + `config/account.json` | 首次运行自动迁移 |
| — | `lofter/errors.py` `lofter/reporter.py` `lofter/progress.py` | 新增：异常、反馈通道、断点语义 |
| — | `cli/run.py` `main.py` `gui/` | 新增：两个前端 |
| — | `tests/smoke_test.py` | 新增：33 个离线用例 |
| 其余旧脚本 | `legacy/`（`git mv`，历史保留） | 可随时对照/回退 |

命令行入口有三条：

```bash
python main.py                    # 图形界面
python main.py --list             # 列任务
python main.py --task <id> --set 键=值   # 跑任务
```

---

## 四、Python 3.12 兼容性改动点

### 4.1 依赖层（这是 3.12 上真正的阻塞点）

实测环境：`Python 3.12.13 (MSC v.1944 64bit)`。

| 旧声明 | 新声明 | 为什么 |
| --- | --- | --- |
| `lxml==4.8.0` | `lxml>=5.2.0` | **硬阻塞**。实测 `pip download lxml==4.8.0 --no-deps --only-binary=:all:` 直接 `ERROR: No matching distribution found`（该版本 2022-03 发布，PyPI 上没有 cp312 轮子）；放开二进制限制就必须本地编译 libxml2/libxslt，Windows 上基本不可能成功 |
| `requests==2.27.1` | `requests>=2.32.0` | 旧版把 `urllib3` 钉在 `<1.27`，与 3.12 生态脱节；2.32 起官方支持 3.12 |
| `urllib3==1.26.9` | 删除 | 交给 requests 传递依赖，不再显式钉版 |
| `json5` | 删除 | **全项目从未 import**（已 grep 确认） |
| `numpy` | 删除 | 只在 `l9_author_txt.py:437-439` 的废弃函数 `merge_chapter_al` 里用了 `np.where`，改为纯 Python 枚举 |
| — | `tkinter` | 图形界面用标准库，不需要额外安装 |

实测安装结果（3.12.13）：`requests 2.34.2` / `lxml 6.1.3` / `urllib3 2.8.0` / `html2text 2025.4.15`。

### 4.2 语法层：46 处非法转义序列

Python 3.12 把字符串里的无效转义从 `DeprecationWarning` 升级成 **`SyntaxWarning`**，
再往后会变成 `SyntaxError`。重构前用 3.12 编译全部脚本，实测 **46 处告警**：

| 文件 | 处数 | 典型例子 |
| --- | --- | --- |
| `l13_like_share_tag.py` | 21 | `'s\d{1,5}.title="(.*?)"'`、`'originPhotoLinks="(\[.*?\])"'` |
| `l9_author_txt.py` | 22 | `'\.blogNickName='` 系列 |
| `l10_blogs_txt.py` | 2 | `re.search("\d{4}[.\\\/-]\d{2}...")` |
| `l4_author_img.py` | 4 | `re.search("[1649]{2}[x,y][1649]{2}", ...)` |
| `l8_blogs_img.py` | 2 | 同上 |
| `parse_template.py` | 1 | `re.split("\s评论\s", content)` |

处理方式：**正则模式一律改成 raw string**（`r'...'`），语义不变，告警清零。
另外把 l4 的 `re.search(r"[1649]{2}[x,y][1649]{2}")` 里 `[x,y]` 的字符类保留原样
（它的原意是「匹配 x 或 y 或逗号」，并非 bug，只是写法怪）。

### 4.3 编码层：Windows 上的中文乱码隐患

| 位置 | 原来 | 现在 |
| --- | --- | --- |
| `l4.file_update()` | `open(file, "w")` —— 不指定编码，中文 Windows 上按 GBK 写 | `encoding="utf-8"` |
| `l4.is_file_in()` / `get_file_contetn()` | `open(file, "r")` 同样不指定 | `encoding="utf-8"` |
| `l4.deal_file()` 建进度文件 | 同上 | 同上 |
| `l13.filename_check()` | 读 `txt` 指定了 utf-8，读图片用 `"rb"`（正确） | 统一显式声明 |
| 新增的进度读写 | — | `lofter/progress.py` 全部显式 utf-8，并有专门用例守护 |

### 4.4 运行期行为层

| 位置 | 原来 | 现在 | 影响 |
| --- | --- | --- | --- |
| `useragentutil.get_headers()` | 返回 `user_agent_datas[0]` —— **列表里那个 dict 本身** | 返回新字典副本 | 原来调用方 `tmp_headers["Referer"] = ...` 会永久污染全局常量，后续请求（甚至别的作者）都带着上一个 Referer |
| `l13` 图片列表解析 | `eval(urls_str)` 执行抓回来的字符串 | `ast.literal_eval()` | 远端内容不再被当代码执行 |
| `l13` 标题解码 | `.decode("unicode_escape", errors="ignore ")` —— handler 名多了个尾空格 | `errors="replace"` | 原来一旦走到错误分支会抛 `LookupError` 被裸 `except` 吞掉，标题变空串 → 文章被误判成文本。现已修正 |
| `js_unescape_latin()` | 非 ASCII 内容 `encode("latin-1")` 失败后被 `except` 丢掉 | 退回原串 | 内容不再凭空消失 |
| `l14.homepage_extract()` | 构造 Host 头时用的是**全局变量 `url`** 而不是参数 `homepage_url` | 用参数 | 只是碰巧在 `__main__` 里两者同名才没出事，界面里换链接就会算错 Host |
| 所有 `exit()` | 直接杀进程 | 领域异常（`ConfigError` / `AuthError` / `NetworkError` / `ParseError`） | CLI 打印后退非 0，GUI 弹窗并写日志 |
| 所有 `input()` | 阻塞 | `Reporter.ask/confirm`，且 `Reporter.interactive=False` 时走默认值不阻塞 | 可无人值守 |
| HTTP 状态码 | 不看，继续往下走，最后在解析阶段报看不懂的错 | 非 2xx 抛 `NetworkError` | 报错更早、更准 |
| 单条博客字段缺失 | `re.search(...).group(1)` 直接 `AttributeError`，整轮 8000 条一起崩 | 跳过该条并告警 | 一条坏数据不再毁掉整个任务 |
| `l9` 文本保存目录 | 只在目录已存在时 `makedirs`（缩进 bug） | 无条件 `_reset_dir()` | 修复潜在崩溃 |

---

## 五、GUI：功能范围与实现思路

### 5.1 功能范围

| 区域 | 能做什么 |
| --- | --- |
| **任务列表**（左） | 列出全部 6 个任务 + 一句话说明，点击切换 |
| **参数设置**（右·页1） | 按任务自动生成分组表单。支持的控件：单行文本、整数（非法输入直接拦住）、勾选框、下拉框、多行列表、JSON 文本框、目录选择器（带「浏览…」）、密码框（`login_auth` 打码，可勾选显示明文） |
| **运行日志**（右·页2） | 分级着色（调试灰 / 普通黑 / 警告橙 / 错误红）、自动滚到最新、开始任务时自动切到本页 |
| **进度区**（底部） | 确定型进度条（知道总数时显示 `当前/总数`）；不知道总数时自动切成跑马灯；右侧实时显示当前阶段 |
| **按钮** | 开始 / 取消 / 保存配置 / 登录信息 / 打开输出文件夹 |
| **菜单** | 打开输出文件夹、打开配置文件夹、关于 |
| **弹窗确认** | 任务需要人工确认时弹模态框（例如 l9 匹配到通用模板）；需要人工输入时弹输入框（例如作者名解析失败） |
| **记忆** | 参数、窗口大小位置、上次选的任务都会持久化，下次打开就在原地 |

**不做的事**（明确划界）：不做抓取结果的预览/浏览界面、不做定时任务、不做账号多开。
这些都超出「把老脚本变好用」的范围，需要时另说。

### 5.2 交互流程

```
启动
 ├─ 自动载入 config/settings.json（首次则尝试从 legacy/login_info.py 迁移登录信息）
 ├─ 未填 login_auth → 顶部红字提示
 └─ 选任务
      ├─ 右侧表单按 FIELDS 生成，标题带默认值
      ├─ 点「保存配置」→ 校验（校验失败弹窗指出是哪个字段）
      └─ 点「开始」
           ├─ 校验：参数合法性 → 登录信息 → 输出目录
           ├─ 后台线程开跑，界面切到「运行日志」
           ├─ 日志/阶段/进度实时刷新（每 80ms 拉一次事件队列）
           ├─ 需要确认 → 弹窗 → 回答回传子线程
           ├─ 点「取消」→ 置取消标志 → 任务在下个检查点抛 TaskCancelled
           └─ 结束 → 进度条收尾，失败弹错误框；点「打开输出文件夹」看结果
```

**断点续跑**：再点一次「开始」即可。断点文件的语义与老脚本完全一致
（`format_blogs_info.json` = 阶段1 完成、`classified_blogs_info.json` = 阶段2 完成、
`img_save_info.json` = 图片保存进度）。

### 5.3 实现思路（三个技术要点）

**要点 1：表单不手写，由 `FIELDS` 生成。**
`gui/widgets.py` 的 `FormPanel._make_widget()` 按 `Field.kind` 分发到 9 种控件；
读值时不自己校验，而是把原始字符串交给 `Field.coerce()`——**校验逻辑只有一份**，
出错时抛出的 `ConfigError` 里已经带了中文的字段名与期望类型，界面直接弹出来。

**要点 2：线程与界面严格分离。**
tkinter 不是线程安全的，所有控件操作必须回主线程，所以：

* 任务在子线程跑（`gui/worker.py` 的 `BackgroundRunner`）；
* 日志/阶段/进度作为事件塞进 `queue.Queue`；
* 主线程用 `after(80, self._pump)` 周期消费。

**要点 3：把 `input()` 变成跨线程握手。**
老脚本的 6 处 `input()` 是 GUI 化的最大障碍。`QueueReporter.ask()/confirm()` 的做法是：
把问题连同一条一次性 `reply` 队列投给主线程 → 主线程弹模态框 → 把答案放回队列 →
子线程取到答案继续。等待期间用 `reply.get(timeout=0.2)` 轮询并检查取消标志，
**所以用户点了「取消」不会把任务卡死在等回答上**（有专门用例覆盖）。

### 5.4 兼容与降级

* 没有 tkinter（精简版 Python）→ `gui.launch()` 打印可操作的提示并返回退出码 3，不崩；
* 无显示环境（远程会话）→ GUI 用例自动跳过，其余用例照跑；
* 默认无参数才开 GUI，带任何参数走 CLI，所以「双击」和「写脚本」互不干扰；
* Windows 用户可直接双击 `run_gui.bat`（ASCII 文件名与内容，避免编码问题）。

---

## 六、与老版本的行为差异（需要知道的）

| # | 差异 | 原因 |
| --- | --- | --- |
| 1 | `l13` 阶段3→阶段4 之间的强制 `input("ok")` 变成开关 `pause_before_save`（默认关） | 否则无法无人值守 |
| 2 | `l13` 结尾的 `input("yes/no")` 变成开关 `reset_after_save`（默认关） | 同上；老行为等价于「每次跑完手动输 yes」 |
| 3 | `l4` 启动前的 `input("ok")` 去掉 | 界面上的「开始」按钮就是这个闸门 |
| 4 | `l9` 通用模板确认、作者名兜底输入：可交互时照旧弹窗，无人值守时用默认值并告警 | 保留能力但不再阻塞 |
| 5 | `l8` 与 `l10` 合成一个任务（`kind` 切换） | 两者用法一致，分开只是历史原因 |
| 6 | 输出目录默认仍为 `./dir`，但相对路径**按项目根解析**而不是当前工作目录 | 换个目录启动不会再找不到断点文件 |
| 7 | 进度文件与产物**文件名、目录结构、JSON 结构全部不变** | 老用户的 `dir/` 可以直接拿来续跑 |
| 8 | 单条博客字段缺失时跳过并告警，不再整轮崩溃 | 见 4.4 |
| 9 | 长文章插图、正文插图的文件名与内容保持不变 | — |

> 界面上的 `img urls` 过滤会产生一个尾随 `?` 的图片链接（`a.jpg?`），这是**老脚本原有行为**
> （`img_url.split("imageView")[0]` 的副产物），HTTP 语义等价，为保持抓取结果一致**故意没有改**。

---

## 七、验证方式

### 7.1 一键离线验证（推荐，不需要登录）

```bash
<你的 python3.12> tests/smoke_test.py          # 33 个用例
<你的 python3.12> tests/smoke_test.py --no-gui # 无显示环境
```

全部用例**不发任何真实请求、不需要 login_auth**，覆盖：

| 组 | 用例 |
| --- | --- |
| 环境 | 依赖可导入；`lofter/ cli/ gui/` 全部文件在 3.12 下编译零 `SyntaxWarning` |
| 纯函数 | 文件名清洗、同名去重、图片过滤（含 `&amp;` / 缩略图 / 去重 / 去裁剪参数）、tag 过滤 in/out、DWR 转义还原、`literal_list` 安全性、时间戳换算 |
| 解析 | 模板 1~7 与通用兜底；归档页图片/文本两套正则；l13 原始 DWR **手工构造样本**解析（标题/作者/tag 小写化/图片挑选） |
| 业务逻辑 | l13 分类优先级、`key tag` 三种分支、目录拼接、tag 频次统计、DWR 请求参数与翻页更新 |
| **合集** | appapi 三种响应信封 / `to_int` 容错 / `collectionId` 三种写法；条目归一化（标题兜底 `noticeLinkTitle`、嵌套 `blogInfo`、图片型 `photoLinks` 三种形态）；正文转换（中文长段**不折行**、txt 降级成 `[图片] url`、md 保留 `![]()`）；章节排序三模式 + 文件名补零与非法字符清洗；元信息容错与缺字段报错；分页切分 / offset 递进 / `max_items` 提前停止；**离线端到端**（假接口 → 真落盘 → 目录/JSON/合并版 → 断点续跑 → 标题过滤）；配置校验四类非法输入 |
| 配置 | `FIELDS` 与 dataclass 字段**完全一致**（防止加参数忘了声明）、往返一致性、字符串→int/bool/list 强制、非法值拦截、未知字段忽略、落盘读写、**旧 `login_info.py` 用 `ast` 静态迁移（并验证文件里的可执行语句不会被执行）** |
| 断点 | `StageFiles` 语义、重置时保留子目录、UTF-8 中文无损 |
| 并发 | `Reporter` 日志等级/问答默认值/取消；**GUI 跨线程提问握手**（含取消时不卡死） |
| 端到端 | l13 阶段2/3/4 在临时目录里纯本地跑完，并断言分类产物、`key tag`、`prior_tags.txt` 与阶段事件；取消令牌生效 |
| 入口 | CLI `--help/--list/--show-config/未知任务/未知键/非法枚举` 的退出码与输出 |
| 界面 | 真正构建一次窗口：切遍 **7 个任务**、每个任务表单控件数与 `FIELDS` 一致、事件泵与目录标签正常 |

预期输出（`✓` 列表 + `通过 N/N，失败 0`，退出码 0）。

### 7.2 分层验证

| 层 | 命令 | 期望 |
| --- | --- | --- |
| 依赖 | `pip install -r requirements.txt` | 3.12 下全部装上（实测 requests 2.34.2 / lxml 6.1.3） |
| 语法 | 冒烟测试的第 2 个用例 | 0 告警 |
| CLI | `python main.py --list` / `--show-config like_share_tag` | 6 个任务、参数表带中文说明 |
| 界面 | `python main.py` 或双击 `run_gui.bat` | 窗口打开、左右布局、切任务表单跟着变 |
| 真抓 | 填好 `login_auth` 后跑一个小 tag | 日志滚动、进度条前进、`dir/tag_file/<tag>/` 出现产物 |

### 7.3 人工验收清单（需要真实登录）

1. **登录**：点「登录信息」填入手机登录的 `LOFTER-PHONE-LOGIN-AUTH`，保存后顶部变绿字「已登录」；
2. **小规模真跑**：跑 `like_share_tag` 的 `tag` 模式，url 用一个小 tag 的 `total` 榜；
3. **看产物**：`dir/tag_file/<tag>/format_blogs_info.json`（阶段1）、`classified_blogs_info.json`（阶段2）、
   `article/` `text/` `img/`（阶段4）；
4. **验断点**：跑到一半点「取消」，再点「开始」，应显示「阶段1在之前的运行中已完成」并接着往下走；
5. **验迁移**：删掉 `config/account.json`，确认程序能从 `legacy/login_info.py` 自动恢复；
6. **验失败路径**：故意填错 `login_auth`，应看到红字错误而不是闪退。

---

## 八、GUI 结构与 wallpaper 的对应关系（v2.1）

本次把 GUI 按另一个项目（`D:\Work\Agent\windows\wallpaper`）的结构对齐了一遍。
**只借结构与交互习惯，不借依赖**——那边用 customtkinter，这里继续用标准库 ttk。

| wallpaper 的做法 | 本项目的落地 | 位置 |
| --- | --- | --- |
| `core/` + `commands/` + `cli.py` + `gui/` 四层，`__main__.py` GUI 优先 CLI 回退 | 已有同样的分层：`lofter/` + `lofter/tasks/` + `cli/run.py` + `gui/`，`main.py` 无参数开 GUI | 原本就是，两边天然一致 |
| `run_*()` 返回 `SpotlightStats` / `BingStats` 统计对象 | 新增 `TaskResult`（`task/ok/message/output_dir/elapsed/stats`），`run_task()` 统一计时与兜底；GUI 底部渲染成「统计」一行 | `lofter/tasks/base.py`、`gui/app.py:_render_stats` |
| 工具栏「主题: dark/light/system」SegmentedButton，切换即生效并持久化 | 工具栏「主题:」下拉（跟随系统/浅色/深色），自维护调色板 + 整体配置 ttk `clam`；持久化到 `config/settings.json` 的 `_app.theme` | `gui/theme.py`、`gui/app.py:_change_theme` |
| `LogPanel`：可折叠 + 清空 | 日志面板补上折叠 / 清空，并额外做了**自动滚动开关**与**导出日志**（抓取动辄跑几十分钟，用户要留证据） | `gui/widgets.py:LogPanel` |
| 底部状态栏（输出路径 + 状态） | 底部四行：输出目录 / 进度条+状态 / 统计 / **最近一条日志** | `gui/app.py:_build_layout` |
| 设置里记忆窗口几何 + 收回工作区 | 保存几何时按屏幕夹取，解析不出来就居中（防止窗口跑到已断开的副屏） | `gui/app.py:_apply_saved_geometry` |
| 关闭时遍历组件调 `release()` / `detach_log_handlers()` | 同样做了统一释放循环，便于以后加托盘 / 文件监视 | `gui/app.py:_release_resources` |
| 严格的布尔解析（`_to_bool`，绝不 `bool("false")`） | `config.to_bool()` 用中英文白名单；顺带修掉「手改 settings.json 写 `"false"` 反而打开开关」的坑 | `lofter/config.py:to_bool` |
| 标签页懒构建 | 原本就是「切任务才建表单」，保持一致 | `gui/app.py:select_task` |

**有意没有跟着做的两件事**：

1. **不引入 customtkinter**。它好看，但会给用户多加一个安装步骤，而且它对本项目
   的「声明式表单」没有增益——表单是数据驱动的，不靠控件外观。用 ttk + 自己那套
   调色板，深色模式也能做得完整（代价是要手动给 `Canvas/Text/Listbox` 重绘）。
2. **不改用 pathlib**。那边全面用 `pathlib.Path`，而本项目既有代码大量使用
   `os.path`。混用两套风格比统一用老的更糟，所以新模块继续 `os.path`，
   保证 `dir/` 里既有进度文件名与拼接结果一字不差。

## 九、合集下载（v2.1 新增）

### 9.1 问题与选型

网页端看不到合集内容。参考实现 `Bueer99/Lofter_Passage_Get` 用 Selenium 打开文章页、
点「上一篇」逐篇跳。本项目**没有沿用它的实现**，只借了「从一篇文章出发把整本走完」这个思路。

| 维度 | Selenium 逐篇点（参考实现） | App 接口（本实现） |
| --- | --- | --- |
| 额外依赖 | Chrome + ChromeDriver（版本易错配）+ selenium + bs4 | 无（就用现有 requests） |
| 每篇请求数 | 至少 1 次页面加载，正文靠 JS 渲染后取 DOM | **一次请求拿 50 篇的完整正文 HTML** |
| 速度 | 每篇等 2-3 秒 | 每 50 篇一次请求 |
| 章节顺序 | 靠「上一篇」的 href 顺序，方向还可能抓反 | 接口自带顺序，另有发布时间可兜底排序 |
| 重试 / 断点续传 | 都没有（中断即从头） | 有重试；`skip_existing` 断点续传 |
| 失效风险 | 页面改版即失效 | 接口可能一变全变（但有两个独立项目在用） |

接口事实（交叉验证自 `SrakhiuMeow/lofter-getter` 与 `123ssdss/lofter-downloader`）：

```
POST https://api.lofter.com/v1.1/postCollection.api?product=lofter-android-7.6.12
Content-Type: application/x-www-form-urlencoded
（可选）lofter-phone-login-auth: <login_auth>

method=getCollectionDetail&offset=0&limit=50&collectionid=<ID>&order=1

→ {"response": {
     "collection": {"id","name","postCount","blogId","tags","description"},
     "blogInfo":   {"blogNickName": ...},
     "items": [ {"post": {"title","noticeLinkTitle","content","type",
                          "photoLinks","tagList","blogPageUrl","publishTime"},
                 "blogInfo": {...}} ]}}
```

**三个必须处理的坑**（都写进了 `lofter/appapi.py` 的注释）：

1. **不要声明 `Accept-Encoding: br`**。参考实现写的是 `br,gzip`，但它们都额外装了
   `brotli`；本项目不引入它，一旦服务端真返回 brotli，`requests` 解不出来直接报错。
   这里显式写死 `gzip, deflate`。
2. 响应信封不统一：`response` / `data` / 顶层直接带字段三种都出现过，统一走 `unwrap()`。
3. `postCount` 可能是字符串、`items` 可能缺失、`blogInfo` 可能嵌在 `post` 里，
   全部容错（`to_int` / `parse_item`）。

### 9.2 三级发现策略

| `source` | 做什么 | 可靠性 |
| --- | --- | --- |
| `collection_id` | 直接给合集 ID 走接口列表 | 最高，推荐 |
| `article` | 给一篇合集内的文章链接，依次尝试：链接参数 → 文章页 HTML → 文章详情接口，反查所属合集 ID | 高；全失败时给出「怎么在 App 里拿 ID」的操作指引 |
| `follow_links` | 顺着文章页里的「上一篇」逐篇走（**参考实现的思路**） | 兜底/实验性，**依赖页面结构** |

`follow_links` 明确标注为实验性，原因写在模块头：lofter 文章页的部分区域是 JS 渲染的，
导航链接不一定出现在服务端返回的 HTML 里；而这个模式只认「下一篇」方向
（参考实现把 `a.prev` 和 `a.next` 一起抓，容易反向或原地打转，只能靠 visited 兜底）。

### 9.3 输出

```
dir/collection/<合集名>/
├── 001_第一章.txt      按章节顺序，文件名补零便于排序
├── images/             正文图片（可关）
├── <合集名> 目录.txt    章节列表 + 简介 + tag
├── <合集名>.json       原始数据
└── <合集名> 完整版.txt  可选，全部章节拼接
```

### 9.4 复用了既有模块（没有另起炉灶）

`utils.pick_best_img_url` / `detect_img_type` / `sanitize_filename` /
`templates.matcher` + `get_content`（`follow_links` 兜底路径的正文抽取）/ `net.fetch_bytes`。
新增依赖 **0 个**：`html2text` 本来就是 l13 的依赖。

## 十、已知未完成 / 后续可做

* `phone_tag`（原 l15）依旧是**实验性**：tag 列表能拿，正文提取仍未实现；
  它需要 App 侧凭证，顺手也收敛到了 `lofter/appapi.py`（共享请求头 / 重试 / 证书开关）；
* `follow_links` 兜底模式的导航选择器**没有真机验证过**（本地无法登录实测），
  只做了「抓不到时给出明确指引」的兜底，不假装它能用；
* 合集接口的 `order` 参数语义（1 是正序还是倒序）以两个参考项目的用法为准取 `1`，
  如果实际反了，用界面上的「章节顺序」按发布时间重排即可；
* `l9` 的章节合并 v2（`merge_chapter_al`）保留但未接入界面；
* `l4` 跑完仍会删除自己的进度文件（沿用老行为）；
* 没有做 EPUB / PDF 导出（参考项目有），那属于功能扩展而非本次范围。
