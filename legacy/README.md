# legacy —— 重构前的原始脚本

这里的 12 个文件是 v2.0 重构**之前**的原样代码，用 `git mv` 搬过来，git 历史完整保留。

**放这儿的目的**：对照新旧行为、出问题时退回去用。

## 怎么跑

这些脚本依赖相对路径与同目录的 `login_info.py`，所以**必须在本目录下运行**：

```bash
cd legacy
python l13_like_share_tag.py
```

参数仍然写在每个文件末尾的 `if __name__ == '__main__':` 里，用编辑器改。

## 依赖说明

| 脚本 | 额外依赖 |
| --- | --- |
| `l9_author_txt.py` | **需要 `pip install numpy`**（v2.0 已经把 numpy 从主依赖里去掉，因为它在废弃函数 `merge_chapter_al` 里只用了 `np.where` 一次） |
| 其他脚本 | 与主项目相同：`requests` / `lxml` / `html2text` |

所以如果你只是因为新版某个任务不好用、想临时退回老脚本：

```bash
pip install -r ../requirements.txt      # 新版依赖（3.12 可用）
pip install numpy                       # 只有 l9 需要
```

## 别用旧版 requirements

仓库里的 `requirements.txt` 已经是**适配 Python 3.12 之后**的版本。
老版本的 `lxml==4.8.0` 在 3.12 上装不上（PyPI 没有 cp312 轮子），
不要为了跑 legacy 把依赖退回去。

## 已知差异（老脚本相对新版）

* 在 Python 3.12 下会出现 `SyntaxWarning: invalid escape sequence`（46 处，正则在普通字符串里
  没转义）。只是告警，不影响运行；
* `useragentutil.get_headers()` 返回的是全局 dict 本身，调用方加 `Referer` 会污染后续请求；
* `l13` 用 `eval()` 解析抓回来的图片列表；标题解码的 `errors="ignore "` 多了一个空格；
* `l4` 的进度文件 `open()` 没指定编码，中文 Windows 上会写成 GBK；
* `l14` 构造 Host 头时误用了全局变量；
* `l9` 文本保存目录只在已存在时才创建。

以上问题在新版 `lofter/` 包里都已修复，详细清单见仓库根目录的 [REFACTOR.md](../REFACTOR.md) 第四节与第六节。
