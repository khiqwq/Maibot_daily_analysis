"""
聊天总结图片渲染器

使用 Jinja2 渲染 HTML 模板，再通过宿主内置的 ``ctx.render.html2png`` 能力
把 HTML 渲染为 PNG（直接返回 base64，无需临时文件、无需自带 Playwright/Chromium）。

设计要点：
- 手写字体（ZCOOL KuaiLe / Patrick Hand）以 @font-face base64 形式内嵌，离线可用，
  不依赖 Google Fonts CDN（在国内/服务器环境更稳定）。
- QQ 头像在渲染前由 Python 进程预下载并转成 base64 data URL 内嵌进 HTML，
  渲染时 ``allow_network=False`` 完全离线，避免浏览器联网拉头像导致 ``load`` 事件
  迟迟不触发而整图渲染超时。头像拉不到时优雅降级（置空），模板走 SVG 占位。
- 显式传入 ``timeout_ms``，避免宿主默认 0（无限等待）挂死。
"""

import os
import time
import base64
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .constants import AnalysisConfig

# 可选 HTTP 库：优先 httpx（MaiBot 主程序在用），其次 aiohttp。两者皆无则禁用预取。
try:
    import httpx  # type: ignore

    _HTTP_BACKEND = "httpx"
except Exception:  # pragma: no cover
    httpx = None  # type: ignore
    try:
        import aiohttp  # type: ignore

        _HTTP_BACKEND = "aiohttp"
    except Exception:  # pragma: no cover
        aiohttp = None  # type: ignore
        _HTTP_BACKEND = ""

# 目录定位
_PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATE_DIR = os.path.join(_PLUGIN_DIR, "templates", "scrapbook")
_FONTS_DIR = os.path.join(_PLUGIN_DIR, "fonts")

# 渲染参数
_VIEWPORT_WIDTH = 1000
_VIEWPORT_HEIGHT = 800
_DEVICE_SCALE = 2.0
# 略低于宿主能力调用的 ~30 秒 RPC 硬超时，让 Playwright 先干净失败而非触发 RPC 超时
_RENDER_TIMEOUT_MS = 25000

# 打包字体：family -> 文件名
_BUNDLED_FONTS = [
    ("ZCOOL KuaiLe", "ZCOOLKuaiLe-Regular.woff2"),
    ("Patrick Hand", "PatrickHand-Regular.woff2"),
]


def _build_font_face_css() -> str:
    """把打包字体编码为 @font-face base64 CSS（模块加载时构建一次）。"""
    faces = []
    for family, filename in _BUNDLED_FONTS:
        path = os.path.join(_FONTS_DIR, filename)
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            faces.append(
                f"@font-face{{font-family:'{family}';"
                f"src:url(data:font/woff2;base64,{b64}) format('woff2');"
                f"font-weight:normal;font-style:normal;font-display:swap;}}"
            )
        except Exception:
            # 字体缺失时静默跳过，模板会回退到系统字体
            pass
    if not faces:
        return ""
    return "<style>" + "".join(faces) + "</style>"


# 内嵌字体 CSS（仅构建一次）
_FONT_FACE_CSS = _build_font_face_css()


# ==================== QQ 头像预取（下载为 base64 data URL，离线内嵌） ====================

# QQ 头像 URL 模板
_AVATAR_URL_TMPL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
# 单个头像请求超时（秒）
_AVATAR_REQUEST_TIMEOUT = 4.0
# 一批头像预取的总超时（秒），超时后未拿到的头像降级置空
_AVATAR_TOTAL_TIMEOUT = 8.0
# 进程内头像缓存上限与过期时间，避免每次渲染都重下全部头像
_AVATAR_CACHE_TTL = 6 * 60 * 60  # 6 小时
_AVATAR_CACHE_MAX = 2000

# 模块级缓存：qq -> (data_url, 写入时间戳)。data_url 为 "" 表示该 QQ 已尝试但失败。
_AVATAR_CACHE: Dict[str, "tuple[str, float]"] = {}


def _cache_get(qq: str) -> Optional[str]:
    """命中且未过期返回 data_url（可能为空串表示已知失败），否则返回 None。"""
    item = _AVATAR_CACHE.get(qq)
    if item is None:
        return None
    data_url, ts = item
    if time.time() - ts > _AVATAR_CACHE_TTL:
        _AVATAR_CACHE.pop(qq, None)
        return None
    return data_url


def _cache_put(qq: str, data_url: str) -> None:
    """写入缓存；超过上限时清理最旧的若干条目。"""
    if len(_AVATAR_CACHE) >= _AVATAR_CACHE_MAX:
        # 简单清理：按时间戳删除最旧的 10%
        try:
            oldest = sorted(_AVATAR_CACHE.items(), key=lambda kv: kv[1][1])
            for k, _ in oldest[: max(1, _AVATAR_CACHE_MAX // 10)]:
                _AVATAR_CACHE.pop(k, None)
        except Exception:
            _AVATAR_CACHE.clear()
    _AVATAR_CACHE[qq] = (data_url, time.time())


def _bytes_to_data_url(raw: bytes, content_type: str = "") -> str:
    """把图片字节转成 data URL；根据 content-type 猜测 mime，默认为 jpeg。"""
    mime = "image/jpeg"
    ct = (content_type or "").lower()
    if "png" in ct:
        mime = "image/png"
    elif "gif" in ct:
        mime = "image/gif"
    elif "webp" in ct:
        mime = "image/webp"
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{mime};base64,{b64}"


async def _fetch_one_avatar_httpx(client: Any, qq: str) -> str:
    url = _AVATAR_URL_TMPL.format(qq=qq)
    resp = await client.get(url)
    resp.raise_for_status()
    raw = resp.content
    if not raw:
        return ""
    return _bytes_to_data_url(raw, resp.headers.get("content-type", ""))


async def _fetch_one_avatar_aiohttp(session: Any, qq: str) -> str:
    url = _AVATAR_URL_TMPL.format(qq=qq)
    timeout = aiohttp.ClientTimeout(total=_AVATAR_REQUEST_TIMEOUT)  # type: ignore
    async with session.get(url, timeout=timeout) as resp:
        resp.raise_for_status()
        raw = await resp.read()
    if not raw:
        return ""
    return _bytes_to_data_url(raw, resp.headers.get("content-type", ""))


async def prefetch_avatars(qqs: List[str], logger: Any = None) -> Dict[str, str]:
    """并发预下载一批 QQ 头像并转成 base64 data URL。

    - 每个请求有独立超时，整批有总超时；失败/超时的头像不收录（调用方据此置空）。
    - 命中进程内缓存的不再重复下载；成功与失败结果均写入缓存（失败缓存空串）。
    - 任何异常都被吞掉，绝不抛出、绝不挂死，保证渲染主流程不受影响。

    返回 {qq: data_url}，仅包含成功拿到非空 data URL 的条目。
    """
    result: Dict[str, str] = {}
    # 去重 + 规整为字符串
    unique: List[str] = []
    seen = set()
    for q in qqs:
        s = str(q or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        unique.append(s)

    if not unique:
        return result

    # 先吃缓存
    to_fetch: List[str] = []
    for qq in unique:
        cached = _cache_get(qq)
        if cached is None:
            to_fetch.append(qq)
        elif cached:
            result[qq] = cached
        # cached == "" 表示已知失败，跳过

    if not to_fetch:
        return result

    if not _HTTP_BACKEND:
        if logger:
            logger.warning("无可用 HTTP 库（httpx/aiohttp），跳过头像预取，头像将走占位")
        return result

    async def _fetch(qq: str, fetch_callable) -> None:
        try:
            data_url = await asyncio.wait_for(
                fetch_callable(qq), timeout=_AVATAR_REQUEST_TIMEOUT
            )
        except Exception:
            data_url = ""
        _cache_put(qq, data_url)
        if data_url:
            result[qq] = data_url

    try:
        if _HTTP_BACKEND == "httpx":
            timeout = httpx.Timeout(_AVATAR_REQUEST_TIMEOUT)  # type: ignore
            async with httpx.AsyncClient(  # type: ignore
                timeout=timeout, follow_redirects=True
            ) as client:
                tasks = [
                    _fetch(qq, lambda q: _fetch_one_avatar_httpx(client, q))
                    for qq in to_fetch
                ]
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_AVATAR_TOTAL_TIMEOUT,
                )
        else:  # aiohttp
            async with aiohttp.ClientSession() as session:  # type: ignore
                tasks = [
                    _fetch(qq, lambda q: _fetch_one_avatar_aiohttp(session, q))
                    for qq in to_fetch
                ]
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_AVATAR_TOTAL_TIMEOUT,
                )
    except asyncio.TimeoutError:
        if logger:
            logger.warning(
                f"头像预取总超时（{_AVATAR_TOTAL_TIMEOUT}s），已拿到 {len(result)}/{len(to_fetch)} 张，"
                "其余走占位"
            )
    except Exception as e:
        if logger:
            logger.warning(f"头像预取异常（已忽略，走占位）: {e}")

    return result


class SummaryRenderer:
    """聊天总结图片渲染器（绑定插件 ctx）"""

    def __init__(self, ctx: Any):
        self.ctx = ctx
        self.logger = ctx.logger
        self.env = Environment(
            loader=FileSystemLoader(_TEMPLATE_DIR),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ==================== 渲染基础设施 ====================

    def _render_template(self, template_name: str, **context: Any) -> str:
        try:
            return self.env.get_template(template_name).render(**context)
        except Exception as e:
            self.logger.error(f"渲染模板 {template_name} 失败: {e}", exc_info=True)
            return ""

    @staticmethod
    def _inject_fonts(html: str) -> str:
        """在 <head> 后插入内嵌字体 CSS。"""
        if _FONT_FACE_CSS and "<head>" in html:
            return html.replace("<head>", "<head>" + _FONT_FACE_CSS, 1)
        return html

    async def _render_png_base64(self, html_content: str) -> Optional[str]:
        """通过宿主能力把 HTML 渲染为 PNG，返回纯 base64（失败返回 None）。"""
        html_content = self._inject_fonts(html_content)
        try:
            result = await self.ctx.render.html2png(
                html=html_content,
                selector="body",
                viewport={"width": _VIEWPORT_WIDTH, "height": _VIEWPORT_HEIGHT},
                device_scale_factor=_DEVICE_SCALE,
                full_page=True,
                wait_until="load",
                allow_network=False,
                timeout_ms=_RENDER_TIMEOUT_MS,
            )
        except Exception as e:
            self.logger.error(f"调用渲染能力异常: {e}", exc_info=True)
            return None

        image_base64 = self._extract_image_base64(result)
        if not image_base64:
            err = result.get("error") if isinstance(result, dict) else result
            self.logger.error(f"HTML 渲染失败或缺少 image_base64: {err}")
            return None
        return image_base64

    @staticmethod
    def _extract_image_base64(result: Any) -> Optional[str]:
        """从渲染能力返回中稳健提取纯 base64。

        兼容 SDK 是否解包 envelope 的多种形态：
        - "<base64>"（已解包为字符串）
        - {"success": True, "result": {"image_base64": "..."}}（宿主原始结构）
        - {"image_base64": "..."} / {"data": {...}} / {"value": {...}}（部分解包）
        """
        if isinstance(result, str):
            return result or None
        if not isinstance(result, dict):
            return None
        if result.get("success") is False:
            return None

        # 直接命中
        direct = result.get("image_base64")
        if isinstance(direct, str) and direct:
            return direct

        # 嵌套在 result/data/value 之中
        for key in ("result", "data", "value"):
            nested = result.get(key)
            if isinstance(nested, dict):
                b64 = nested.get("image_base64")
                if isinstance(b64, str) and b64:
                    return b64
            elif isinstance(nested, str) and nested and key != "result":
                return nested
        return None

    # ==================== 群聊总结图片 ====================

    async def generate_summary_image(
        self,
        title: str,
        summary_text: str,
        time_info: str = "",
        message_count: int = 0,
        participant_count: int = 0,
        emoji_count: Optional[int] = None,
        total_characters: Optional[int] = None,
        topics: Optional[list] = None,
        user_titles: Optional[list] = None,
        golden_quotes: Optional[list] = None,
        depression_index: Optional[list] = None,
        hourly_distribution: Optional[dict] = None,
        display_order: Optional[list] = None,
        target_date: Optional[datetime] = None,
        max_depression_display: Optional[int] = None,
        depression_show_bottom: Optional[bool] = None,
        highlight_time: Optional[str] = None,
    ) -> Optional[str]:
        """生成群聊总结图片，返回 PNG 的 base64（失败返回 None）。"""
        topics = topics or []
        user_titles = user_titles or []
        golden_quotes = golden_quotes or []
        hourly_distribution = hourly_distribution or {}
        if display_order is None:
            display_order = ["24H", "Topics", "Portraits", "Quotes", "Rankings"]
        if max_depression_display is None:
            max_depression_display = AnalysisConfig.MAX_DEPRESSION_DISPLAY
        if depression_show_bottom is None:
            depression_show_bottom = AnalysisConfig.DEPRESSION_SHOW_BOTTOM_HALF

        if target_date is None:
            target_date = datetime.now()
        current_date = target_date.strftime("%Y年%m月%d日")

        # 优先使用传入的真实总字数；未提供时回退到总结文案长度
        if total_characters is None:
            total_characters = len(summary_text)
        # 优先使用真实 emoji 统计；未提供时回退到按消息数估算
        if emoji_count is None:
            emoji_count = int(message_count * 0.1)

        # Highlight Time：优先用传入的"最早消息→最晚消息"时间跨度；否则回退到最活跃时段
        if highlight_time:
            most_active_period = highlight_time
        elif hourly_distribution:
            max_hour = max(hourly_distribution, key=hourly_distribution.get)
            most_active_period = f"{max_hour:02d}:00-{(max_hour + 1) % 24:02d}:00"
        else:
            most_active_period = "未知"

        # ===== 24小时活跃图表 =====
        hourly_chart_html = ""
        if "24H" in display_order and hourly_distribution:
            max_count = max(hourly_distribution.values()) if hourly_distribution.values() else 1
            chart_data = []
            for hour in range(24):
                count = hourly_distribution.get(hour, 0)
                percentage = int((count / max_count) * 100) if max_count > 0 else 0
                chart_data.append({"hour": hour, "count": count, "percentage": percentage})
            hourly_chart_html = self._render_template("activity_chart_section.html", chart_data=chart_data)

        # ===== 话题列表 =====
        topics_html = ""
        if "Topics" in display_order and topics:
            topic_list = []
            for idx, topic_item in enumerate(topics[:5], start=1):
                topic_data = topic_item.get("topic", "")
                detail = topic_item.get("detail", "")
                contributors = topic_item.get("contributors", [])
                topic_dict = {"topic": topic_data, "detail": detail} if isinstance(topic_data, str) else topic_data
                topic_list.append(
                    {
                        "index": idx,
                        "topic": topic_dict,
                        "detail": detail,
                        "contributors": "、".join(contributors[:5]),
                    }
                )
            topics_html = self._render_template("topic_item.html", topics=topic_list)

        # ===== 群友画像 =====
        portraits_html = ""
        if "Portraits" in display_order and user_titles:
            # 渲染前先把要用到的 QQ 头像预下载为 base64 data URL（离线内嵌，避免渲染联网超时）
            avatar_qqs = [
                str(t.get("user_id", "") or "")
                for t in user_titles[:6]
                if not t.get("avatar_data") and t.get("user_id")
            ]
            avatar_map = await prefetch_avatars(avatar_qqs, self.logger) if avatar_qqs else {}
            title_list = []
            for title_item in user_titles[:6]:
                uid = str(title_item.get("user_id", "") or "")
                # 优先用已有 avatar_data；否则用预取到的 base64 data URL；拿不到则置空走 SVG 占位
                avatar_data = title_item.get("avatar_data", "") or avatar_map.get(uid, "")
                title_list.append(
                    {
                        "name": title_item.get("name", ""),
                        "title": title_item.get("title", ""),
                        "mbti": title_item.get("mbti", ""),
                        "reason": title_item.get("reason", ""),
                        "avatar_data": avatar_data,
                    }
                )
            portraits_html = self._render_template("user_title_item.html", titles=title_list)

        # ===== 金句 =====
        quotes_html = ""
        if "Quotes" in display_order and golden_quotes:
            quote_list = []
            for quote_item in golden_quotes[:4]:
                quote_list.append(
                    {
                        "content": quote_item.get("content", ""),
                        "sender": quote_item.get("sender", ""),
                        "reason": quote_item.get("reason", ""),
                    }
                )
            quotes_html = self._render_template("quote_item.html", quotes=quote_list)

        # ===== 炫压抑评级 =====
        rankings_html = ""
        if "Rankings" in display_order:
            depression_rankings = self._build_depression_rankings(
                depression_index or [], max_depression_display, depression_show_bottom
            )
            rankings_html = self._render_template(
                "depression_index_item.html", depression_rankings=depression_rankings
            )

        # ===== 按 display_order 组装 =====
        module_map = {
            "24H": hourly_chart_html,
            "Topics": topics_html,
            "Portraits": portraits_html,
            "Quotes": quotes_html,
            "Rankings": rankings_html,
        }
        modules_html = "\n".join(
            module_map[name] for name in display_order if module_map.get(name)
        )

        html_content = self._render_template(
            "image_template.html",
            current_date=current_date,
            message_count=message_count,
            participant_count=participant_count,
            emoji_count=emoji_count,
            total_characters=total_characters,
            most_active_period=most_active_period,
            modules_html=modules_html,
        )
        if not html_content:
            self.logger.error("群聊总结主模板渲染为空")
            return None

        return await self._render_png_base64(html_content)

    @staticmethod
    def _build_depression_rankings(
        depression_index: list, max_depression_display: int, depression_show_bottom: bool
    ) -> List[Dict]:
        """根据展示配置构建炫压抑评级渲染数据。"""
        if not depression_index:
            return []

        total_count = len(depression_index)
        rankings: List[Dict] = []

        if total_count <= max_depression_display:
            for i, entry in enumerate(depression_index, 1):
                rankings.append(
                    {
                        "name": entry.get("name", ""),
                        "rank": entry.get("rank", ""),
                        "comment": entry.get("comment", ""),
                        "position": i,
                    }
                )
            return rankings

        if depression_show_bottom:
            # 正数优先：6→前3+后3, 7→前4+后3, 8→前4+后4
            if max_depression_display % 2 == 0:
                top_count = max_depression_display // 2
                bottom_count = max_depression_display // 2
            else:
                top_count = max_depression_display // 2 + 1
                bottom_count = max_depression_display // 2
            for i, entry in enumerate(depression_index[:top_count], 1):
                rankings.append(
                    {
                        "name": entry.get("name", ""),
                        "rank": entry.get("rank", ""),
                        "comment": entry.get("comment", ""),
                        "position": i,
                    }
                )
            bottom_entries = depression_index[-bottom_count:]
            for i, entry in enumerate(bottom_entries, 1):
                rankings.append(
                    {
                        "name": entry.get("name", ""),
                        "rank": entry.get("rank", ""),
                        "comment": entry.get("comment", ""),
                        "position": f"倒{bottom_count - i + 1}",
                    }
                )
        else:
            for i, entry in enumerate(depression_index[:max_depression_display], 1):
                rankings.append(
                    {
                        "name": entry.get("name", ""),
                        "rank": entry.get("rank", ""),
                        "comment": entry.get("comment", ""),
                        "position": i,
                    }
                )
        return rankings

    # ==================== 个人总结图片 ====================

    async def generate_user_summary_image(
        self,
        user_name: str,
        user_id: str,
        summary_text: str = "",
        message_count: int = 0,
        total_characters: int = 0,
        emoji_count: int = 0,
        hourly_distribution: Optional[dict] = None,
        user_title: str = "",
        user_mbti: str = "",
        portrait_data: Optional[dict] = None,
        depression_data: Optional[dict] = None,
        golden_quotes: Optional[list] = None,
        display_order: Optional[list] = None,
        target_date: Optional[datetime] = None,
    ) -> Optional[str]:
        """生成个人总结图片，返回 PNG 的 base64（失败返回 None）。"""
        hourly_distribution = hourly_distribution or {}
        golden_quotes = golden_quotes or []
        if display_order is None:
            display_order = ["3H", "Portraits", "Rankings", "Quotes"]

        if target_date is None:
            target_date = datetime.now()
        current_date = target_date.strftime("%Y年%m月%d日")

        # 渲染前预下载头像为 base64 data URL（离线内嵌，避免渲染联网超时）；拿不到则置空走 SVG 占位
        uid = str(user_id or "")
        avatar_data = ""
        if uid:
            avatar_map = await prefetch_avatars([uid], self.logger)
            avatar_data = avatar_map.get(uid, "")

        # ===== 3H 活跃轨迹 =====
        activity_3h_html = ""
        if "3H" in display_order and hourly_distribution:
            max_hour = max(hourly_distribution, key=hourly_distribution.get)
            max_count = hourly_distribution[max_hour]
            prev_hour = (max_hour - 1) % 24
            next_hour = (max_hour + 1) % 24

            three_hours = [
                {"time_label": f"{prev_hour:02d}:00-{max_hour:02d}:00", "count": hourly_distribution.get(prev_hour, 0), "percentage": 0},
                {"time_label": f"{max_hour:02d}:00-{(max_hour + 1) % 24:02d}:00", "count": max_count, "percentage": 100},
                {"time_label": f"{next_hour:02d}:00-{(next_hour + 1) % 24:02d}:00", "count": hourly_distribution.get(next_hour, 0), "percentage": 0},
            ]
            if max_count > 0:
                three_hours[0]["percentage"] = int((three_hours[0]["count"] / max_count) * 100)
                three_hours[2]["percentage"] = int((three_hours[2]["count"] / max_count) * 100)

            activity_3h_html = self._render_template("user_3h_activity.html", chart_data=three_hours)

        # 群友画像头像兜底
        if portrait_data and not portrait_data.get("avatar_data"):
            portrait_data["avatar_data"] = avatar_data

        # 金句模块
        quotes_html = ""
        if golden_quotes:
            quote_list = []
            for quote_item in golden_quotes[:4]:
                quote_list.append(
                    {
                        "content": quote_item.get("content", ""),
                        "sender": user_name,
                        "reason": quote_item.get("reason", ""),
                    }
                )
            quotes_html = self._render_template("quote_item.html", quotes=quote_list)

        # ===== 解析 display_order 动态组装 =====
        modules_html_list = []
        for order_item in display_order:
            if "," in order_item:
                # 组合模块：横向排列，每个 span 6
                combined_html = ""
                for module_name in (n.strip() for n in order_item.split(",")):
                    if module_name == "Portraits" and portrait_data:
                        combined_html += self._render_template(
                            "user_portrait_module.html", portrait=portrait_data, grid_span=6
                        )
                    elif module_name == "Rankings" and depression_data:
                        combined_html += self._render_template(
                            "user_depression_module.html", depression=depression_data, grid_span=6
                        )
                if combined_html:
                    modules_html_list.append(combined_html)
            else:
                module_name = order_item.strip()
                if module_name == "3H":
                    if activity_3h_html:
                        modules_html_list.append(activity_3h_html)
                elif module_name == "Portraits" and portrait_data:
                    modules_html_list.append(
                        self._render_template("user_portrait_module.html", portrait=portrait_data, grid_span=12)
                    )
                elif module_name == "Rankings" and depression_data:
                    modules_html_list.append(
                        self._render_template("user_depression_module.html", depression=depression_data, grid_span=12)
                    )
                elif module_name == "Quotes" and quotes_html:
                    modules_html_list.append(quotes_html)

        modules_html = "\n".join(modules_html_list)

        # Quotes 完全由 display_order 控制显隐：不在列表中则不显示（不再做尾部强制兜底），
        # 这样用户可以通过从 display_order 中移除 "Quotes" 来隐藏金句模块。
        html_content = self._render_template(
            "user_summary_template.html",
            user_name=user_name,
            current_date=current_date,
            avatar_data=avatar_data,
            user_title=user_title,
            user_mbti=user_mbti,
            message_count=message_count,
            total_characters=total_characters,
            emoji_count=emoji_count,
            summary_text=summary_text,
            modules_html=modules_html,
            quotes_html="",
        )
        if not html_content:
            self.logger.error("个人总结主模板渲染为空")
            return None

        return await self._render_png_base64(html_content)
