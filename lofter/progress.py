"""进度文件与磁盘读写。

原脚本把「阶段是否完成」编码成**某个文件是否存在**，例如
``format_blogs_info.json`` 存在即阶段1 完成。这里把这个约定显式建模，并统一
UTF-8 编码——原 ``l4_author_img.file_update`` / ``is_file_in`` 是 ``open(path, "w")``
不带 ``encoding``，在中文 Windows 上会用 GBK 写中文，导致下一次读取或他人接手时乱码。
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any, Optional

__all__ = [
    "read_json",
    "write_json",
    "read_text",
    "write_text",
    "write_bytes",
    "remove_quietly",
    "rmtree_quietly",
    "list_files",
]


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8", errors="replace") as fp:
        text = fp.read()
    if not text.strip():
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def write_json(path: str, obj: Any, indent: int = 4) -> None:
    ensure_parent(path)
    with open(path, "w", encoding="utf-8", errors="ignore") as fp:
        fp.write(json.dumps(obj, ensure_ascii=False, indent=indent))


def read_text(path: str, default: str = "", encoding: str = "utf-8") -> str:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding=encoding, errors="replace") as fp:
        return fp.read()


def write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    ensure_parent(path)
    with open(path, "w", encoding=encoding, errors="ignore") as fp:
        fp.write(text)


def write_bytes(path: str, data: bytes) -> None:
    ensure_parent(path)
    with open(path, "wb") as fp:
        fp.write(data)


def remove_quietly(path: str) -> bool:
    """删文件，不存在就返回 False，不抛异常。"""
    try:
        os.remove(path)
        return True
    except (FileNotFoundError, PermissionError, OSError):
        return False


def rmtree_quietly(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    try:
        shutil.rmtree(path)
        return True
    except (PermissionError, OSError):
        shutil.rmtree(path, ignore_errors=True)
        return True


def list_files(path: str) -> list[str]:
    """列出目录下的**文件**（不含子目录），用于「重置进度」。"""
    if not os.path.isdir(path):
        return []
    return [name for name in os.listdir(path) if os.path.isfile(os.path.join(path, name))]


def ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


# --------------------------------------------------------------- 阶段进度约定
class StageFiles:
    """把「文件存在 = 阶段完成」的老约定收拢到一处，便于阅读和测试。"""

    #: 阶段1 结束标记（原始数据已抓取并解析为 dict 列表）
    FORMATTED = "format_blogs_info.json"
    #: 阶段1 中间产物（原始 DWR 文本，按 split_line 分隔）
    RAW = "blogs_info"
    #: 阶段2 结束标记（分类 + key tag 完成）
    CLASSIFIED = "classified_blogs_info.json"
    #: 阶段4 图片保存进度
    IMG_PROGRESS = "img_save_info.json"

    def __init__(self, base_dir: str) -> None:
        self.base_dir = base_dir

    def path(self, name: str) -> str:
        return os.path.join(self.base_dir, name)

    def exists(self, name: str) -> bool:
        return os.path.exists(self.path(name))

    def remove(self, name: str) -> bool:
        return remove_quietly(self.path(name))

    def read_json(self, name: str, default=None):
        return read_json(self.path(name), default)

    def write_json(self, name: str, obj) -> None:
        write_json(self.path(name), obj)

    def reset_all_files(self) -> list[str]:
        """清掉目录下所有**文件**（保留子目录），返回被删列表。"""
        removed = []
        for name in list_files(self.base_dir):
            if remove_quietly(self.path(name)):
                removed.append(name)
        return removed

    def describe(self) -> dict:
        return {
            "阶段1_原始": self.exists(self.RAW),
            "阶段1_完成": self.exists(self.FORMATTED),
            "阶段2_完成": self.exists(self.CLASSIFIED),
            "阶段4_图片进度": self.exists(self.IMG_PROGRESS),
        }


def next_available(path: str) -> Optional[str]:
    """同名时返回可用的 ``name(2)`` 形式路径，供不关心内容只关心不覆盖的场景使用。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    num = 2
    while True:
        candidate = "{}({}){}".format(stem, num, ext)
        if not os.path.exists(candidate):
            return candidate
        num += 1
