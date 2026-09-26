"""lofter App 端接口客户端（``api.lofter.com``）。

为什么要单独一层
----------------
网页端（``www.lofter.com``）和 App 端（``api.lofter.com``）是两套完全不同的接口：

* 网页端靠 DWR 协议翻页，只能拿「归档页」时间线，**拿不到合集**；
* App 端有 ``postCollection.api``，能按 ``collectionId`` 直接列出合集里的全部章节，
  而且返回的 ``items[].post.content`` **就是完整正文 HTML**——一次请求 50 篇。

所以合集下载走 App 接口，**一次请求拿 50 篇正文**，既不需要 Selenium，也不需要逐篇再请求。

踩坑记录（交叉验证 + 实测得来，改代码前先看一眼）
------------------------------------------------
1. **绝对不要声明 ``Accept-Encoding: br``。** 参考实现里写的是 ``br,gzip``，但它们都额外
   依赖 ``brotli`` 包。本项目不引入 brotli，一旦服务端真的返回 brotli 正文，
   ``requests`` 解不出来会直接抛错。这里显式写死 ``gzip, deflate``。
2. **响应信封不统一**，三种形态都出现过：``{"response": {...}}``、
   ``{"code": 0, "data": {...}}``、以及**顶层直接带业务字段**。统一由
   :func:`unwrap` 处理。
3. ``postCount`` 有时是字符串；``items`` 有时缺失；``blogInfo`` 有时嵌在 ``post`` 里。
   全部容错，不要裸取。
4. ``api.lofter.com`` 的证书链在部分网络环境下校验不过，所以给 ``verify_ssl`` 开关，
   关掉时抑制 urllib3 的 ``InsecureRequestWarning``，否则日志会被刷屏。
5. 合集是公开内容，**通常不需要登录**；带 ``lofter-phone-login-auth`` 只是让服务端
   认得你是 App 用户，能降低被限流的概率。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator, Optional

import requests

from .errors import NetworkError, ParseError

__all__ = [
    "APP_UA",
    "APP_PRODUCT",
    "API_BASE",
    "COLLECTION_API",
    "POST_DETAIL_API",
    "TAG_POSTS_API",
    "SUBSCRIBE_API",
    "default_app_headers",
    "unwrap",
    "to_int",
    "find_collection_id",
    "AppApi",
]

APP_UA = "LOFTER-Android 8.0.12 (LM-V409N; Android 15; null) WIFI"
APP_PRODUCT = "lofter-android-7.6.12"
API_BASE = "https://api.lofter.com"
COLLECTION_API = "{}/v1.1/postCollection.api".format(API_BASE)
POST_DETAIL_API = "{}/oldapi/post/detail.api".format(API_BASE)
TAG_POSTS_API = "{}/newapi/tagPosts.json".format(API_BASE)
NEWTAG_API = "{}/v1.1/newTag.api".format(API_BASE)
SUBSCRIBE_API = "{}/newapi/subscribeCollection/list.json".format(API_BASE)

# 从任意文本（网页 HTML / 接口正文 / 分享链接）里找 collectionId
_COLLECTION_ID_PATTERNS = (
    re.compile(r'collection[Ii]d["\']?\s*[:=]\s*["\']?(\d{4,})'),
    re.compile(r'[?&]collectionId=(\d{4,})'),
    re.compile(r'/collection/(\d{4,})'),
)

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def default_app_headers() -> dict:
    """App 接口的最小可用请求头。

    **不要在这里加 ``br``**：见模块文档第 1 条。
    """
    return {
        "User-Agent": APP_UA,
        "Accept-Encoding": "gzip, deflate",
        "Accept": "*/*",
        "Connection": "Keep-Alive",
        "Host": "api.lofter.com",
        "lofproduct": APP_PRODUCT,
    }


def unwrap(payload: Any) -> dict:
    """剥掉响应信封，返回业务字段所在的字典。

    兼容三种形态：``response`` 包裹、``data`` 包裹、顶层直接带字段。
    """
    if not isinstance(payload, dict):
        return {}
    for key in ("response", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def to_int(value: Any, default: int = 0) -> int:
    """把可能是字符串/None 的数字转成 int。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def find_collection_id(text: str) -> str:
    """从网页 HTML / 分享链接 / 接口正文里抠出 ``collectionId``，找不到返回空串。"""
    if not text:
        return ""
    for pattern in _COLLECTION_ID_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return ""


def _normalize_collection_list(raw) -> list:
    """把「合集列表」类响应的条目归一成 ``[{"id","name","valid","post_count"}]``。

    两种接口字段名不一致：作者合集给 ``id``，订阅列表给 ``collectionId``；
    订阅还多一个 ``valid`` 标记合集是否已失效。统一在这里兜掉，
    调用方就不用各自判空、各自猜字段名。
    """
    out: list = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        cid = entry.get("id") or entry.get("collectionId")
        if not cid:
            continue
        out.append({
            "id": str(cid),
            "name": str(entry.get("name") or "").strip(),
            "valid": bool(entry.get("valid", True)),
            "post_count": to_int(entry.get("postCount") or entry.get("itemCount")),
        })
    return out


class AppApi:
    """App 接口客户端。

    :param login_auth: ``login_auth``（cookie 值）。可留空，合集是公开内容。
    :param extra_headers: 界面里填的额外请求头，会覆盖默认值。
    :param extra_cookies: 界面里填的额外 cookie。
    :param verify_ssl: 证书校验开关（部分网络环境下必须关掉）。
    :param max_retries: 网络类失败的重试次数。
    """

    def __init__(self, login_auth: str = "",
                 login_key: str = "LOFTER-PHONE-LOGIN-AUTH",
                 extra_headers: Optional[dict] = None,
                 extra_cookies: Optional[dict] = None,
                 verify_ssl: bool = True,
                 max_retries: int = 3,
                 timeout: int = 20,
                 reporter=None) -> None:
        self.verify_ssl = bool(verify_ssl)
        self.max_retries = max(1, int(max_retries))
        self.timeout = timeout
        self.reporter = reporter

        self.session = requests.Session()
        headers = default_app_headers()
        if login_auth:
            # 服务端只认小写的这个头，大小写不敏感但保持参考实现的一致写法
            headers["lofter-phone-login-auth"] = login_auth
        for key, value in (extra_headers or {}).items():
            headers[str(key)] = str(value)
        self.session.headers = headers
        for name, value in (extra_cookies or {}).items():
            self.session.cookies.set(str(name), str(value), domain="api.lofter.com")

        if not self.verify_ssl:
            try:
                import urllib3

                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            except Exception:  # pragma: no cover - urllib3 一定存在
                pass

    # ------------------------------------------------------------------ 底层
    def _log(self, message: str, level: int = 10) -> None:
        if self.reporter is not None:
            self.reporter.log(message, level)

    def _request(self, method: str, url: str, *,
                 params: Optional[dict] = None,
                 data: Optional[dict] = None) -> dict:
        """发请求 + 重试 + 解 JSON。返回**剥过信封**的字典。"""
        last_error = ""
        for attempt in range(1, self.max_retries + 1):
            if self.reporter is not None:
                self.reporter.check_cancel()
            try:
                if method == "GET":
                    resp = self.session.get(url, params=params, timeout=self.timeout,
                                            verify=self.verify_ssl)
                else:
                    resp = self.session.post(url, params=params, data=data,
                                             timeout=self.timeout, verify=self.verify_ssl)
            except requests.RequestException as exc:
                last_error = "{}: {}".format(type(exc).__name__, exc)
                self._log("请求失败（第 {}/{} 次）：{}".format(
                    attempt, self.max_retries, last_error), 30)
                if attempt < self.max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise NetworkError(
                    "请求 {} 失败：{}\n"
                    "排查建议：检查网络 / 是否需要代理；若报证书错误，"
                    "把「校验 SSL 证书」关掉再试。".format(url, last_error)) from exc

            if resp.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                self._log("服务端返回 {}，{} 秒后重试".format(resp.status_code, 2 ** attempt), 30)
                time.sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code >= 400:
                raise NetworkError("{} 返回状态码 {}".format(url, resp.status_code))

            text = resp.content.decode("utf-8", errors="replace")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ParseError(
                    "{}\n接口返回的不是 JSON（前 200 字）：{}".format(
                        "解析 App 接口响应失败：", text[:200])) from exc

            code = payload.get("code") if isinstance(payload, dict) else None
            if code not in (None, 0, "0"):
                message = payload.get("msg") or payload.get("message") or ""
                raise ParseError("接口返回 code={} {}".format(code, message))
            return unwrap(payload)

        raise NetworkError("请求 {} 重试 {} 次仍然失败：{}".format(url, self.max_retries, last_error))

    # -------------------------------------------------------------- 合集相关
    def collection_page(self, collection_id: str, offset: int = 0,
                        limit: int = 50, order: int = 1) -> dict:
        """取一页合集内容（含合集元数据与当前页的章节列表）。"""
        return self._request(
            "POST", COLLECTION_API,
            params={"product": APP_PRODUCT},
            data={
                "method": "getCollectionDetail",
                "offset": int(offset),
                "limit": int(limit),
                "collectionid": str(collection_id),
                "order": int(order),
            },
        )

    def collection_meta(self, collection_id: str) -> dict:
        """取合集元信息。

        返回 ``{id, name, post_count, blog_id, author, tags, description}``。
        为了省一次请求，这里 limit=1；元数据与列表在同一接口里。
        """
        resp = self.collection_page(collection_id, offset=0, limit=1, order=1)
        info = resp.get("collection")
        if not isinstance(info, dict):
            raise ParseError(
                "接口没有返回合集信息，collectionId={} 可能不正确，"
                "或者该合集已被作者删除 / 设为私密。".format(collection_id))
        blog_info = resp.get("blogInfo") or {}
        raw_tags = info.get("tags") or ""
        return {
            "id": str(info.get("id") or collection_id),
            "name": str(info.get("name") or "").strip(),
            "post_count": to_int(info.get("postCount")),
            "blog_id": str(info.get("blogId") or ""),
            "author": str(blog_info.get("blogNickName") or ""),
            "tags": [t for t in str(raw_tags).split(",") if t.strip()],
            "description": str(info.get("description") or ""),
        }

    def iter_collection_pages(self, collection_id: str, *,
                              order: int = 1, page_size: int = 50,
                              post_count: int = 0, max_items: int = 0):
        """按页迭代合集章节列表，逐页 ``yield``。

        :param post_count: 合集总篇数；为 0 时不预知总量，翻到空页为止。
        :param max_items: 只要前 N 篇（0 = 全部），用于「先抓 10 篇试试」。
        """
        limit = max(1, int(page_size))
        offset = 0
        yielded = 0
        while True:
            if self.reporter is not None:
                self.reporter.check_cancel()
            resp = self.collection_page(collection_id, offset=offset, limit=limit, order=order)
            items = resp.get("items")
            if not isinstance(items, list) or not items:
                break

            if max_items:
                room = max_items - yielded
                if room <= 0:
                    break
                items = items[:room]

            yielded += len(items)
            yield items

            if len(items) < limit:
                break
            if post_count and yielded >= post_count:
                break
            if max_items and yielded >= max_items:
                break
            offset += limit
            time.sleep(0.3)

    def collection_items(self, collection_id: str, *, order: int = 1,
                         page_size: int = 50, post_count: int = 0,
                         max_items: int = 0) -> list:
        """把合集章节列表一次性抓完（小合集够用，大合集建议用 ``iter_collection_pages``）。"""
        items: list = []
        for page in self.iter_collection_pages(collection_id, order=order,
                                               page_size=page_size,
                                               post_count=post_count,
                                               max_items=max_items):
            items.extend(page)
        return items

    @staticmethod
    def page_count(post_count: int, page_size: int) -> int:
        """按总篇数与页大小算需要翻几页（至少 1 页）。"""
        size = max(1, int(page_size))
        return max(1, (max(0, int(post_count)) + size - 1) // size)

    # -------------------------------------------------------------- 文章相关
    def post_detail_json(self, blog_id: str, post_id: str) -> dict:
        """取单篇详情，返回**原始 response**（未再往下剥一层）。

        ``phone_tag`` 需要把原始 JSON 存盘，所以保留这一层。
        """
        return self._request(
            "POST", POST_DETAIL_API,
            params={"product": APP_PRODUCT},
            data={
                "targetblogid": str(blog_id),
                "supportposttypes": "1,2,3,4,5,6",
                "blogId": str(blog_id),
                "offset": "0",
                "requestType": "1",
                "postdigestnew": "1",
                "postid": str(post_id),
                "checkpwd": "1",
                "needgetpoststat": "1",
            },
        )

    def post_detail(self, blog_id: str, post_id: str) -> dict:
        """取单篇详情并剥到 ``post`` 那一层。

        用途之一是**反查 collectionId**：只知道文章链接时，靠它判断这篇属于哪个合集。
        """
        resp = self.post_detail_json(blog_id, post_id)
        posts = resp.get("posts")
        if isinstance(posts, list) and posts:
            first = posts[0]
            if isinstance(first, dict):
                return first.get("post") or {}
        return resp

    # -------------------------------------------------------------- tag 相关
    def refresh_si(self) -> None:
        """刷新 ``NTESwebSI``。

        手机端 tag 接口有个隐含约束：**一个 NTESwebSI 只能用来请求一组 tag 页数据**，
        所以每轮翻页前先打一次 ``newTag.api`` 换一个新的。失败不影响主流程
        （旧值通常还能凑合用），所以这里只记日志不抛异常。
        """
        try:
            self.session.post(NEWTAG_API, timeout=self.timeout, verify=self.verify_ssl)
        except requests.RequestException as exc:
            self._log("刷新 NTESwebSI 失败（继续用旧值）：{}".format(exc), 30)

    def tag_posts(self, tag: str, *, list_type: str = "total", offset: int = 0,
                  post_types: str = "", timelimit: str = "",
                  recent_day: str = "0") -> dict:
        """手机端 tag 列表接口。返回剥过信封的字典（含 ``offset`` 与 ``list``）。"""
        return self._request(
            "POST", TAG_POSTS_API,
            params={"product": APP_PRODUCT},
            data={
                "product": APP_PRODUCT,
                "postTypes": post_types,
                "offset": str(int(offset)),
                "postYm": timelimit,
                "recentDay": recent_day,
                "protectedFlag": "0",
                "range": "0",
                "firstpermalink": "null",
                "style": "0",
                "tag": tag,
                "type": list_type,
            },
        )

    def author_collections(self, blog_domain: str) -> list:
        """取某个作者的全部合集。

        归一成 ``[{"id", "name", "valid", "post_count"}]``——
        不同版本的接口字段名不一致（``id`` / ``collectionId``），
        在这里统一掉，调用方就不用各自兜底。
        """
        resp = self._request(
            "POST", COLLECTION_API,
            params={"product": APP_PRODUCT},
            data={
                "method": "getCollectionList",
                "blogdomain": blog_domain,
                "needViewCount": "1",
            },
        )
        return _normalize_collection_list(resp.get("collections"))

    def subscriptions(self, offset: int = 0, limit: int = 50,
                      max_pages: int = 20) -> list:
        """取当前账号**订阅**的全部合集（需要 ``login_auth``）。

        分页靠响应里的 ``subscribeCollectionCount``。
        ``valid=False`` 的失效合集**原样返回、不在数据层悄悄丢掉**——
        由调用方决定跳过并计入统计，避免出现「上层以为下了、其实没下」。
        """
        collected: list = []
        total = 0
        cursor = max(0, int(offset))
        for _page in range(max(1, int(max_pages))):
            resp = self._request("GET", SUBSCRIBE_API,
                                 params={"offset": cursor, "limit": int(limit)})
            items = resp.get("collections")
            if not isinstance(items, list) or not items:
                break
            collected.extend(_normalize_collection_list(items))
            total = to_int(resp.get("subscribeCollectionCount"), total)
            cursor += len(items)
            if total and len(collected) >= total:
                break
            time.sleep(0.2)
        return collected
