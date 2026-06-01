"""
每日分析插件（MaiBot 1.0 / maibot_sdk 2.x）

功能：
- /summary [今天|昨天]      生成群聊整体总结图片
- /mysummary [今天|昨天]    生成自己的个人总结图片
- /mysummary @某人 [今天|昨天] / /mysummary QQ号 [今天|昨天]  查看他人总结（需权限）
- 每日定时自动生成群聊总结

所有宿主能力通过 ctx.* 调用；图片由宿主内置 render.html2png 渲染（无需自带浏览器）。
"""

import asyncio
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Tuple
from collections import Counter

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

from .core import AnalysisService, SummaryRenderer


# ==================== 模块选项（WebUI 中文下拉）====================

# 群聊总结可选模块；"无"=该槽位不显示任何模块
GroupModuleOption = Literal[
    "无", "24H活跃轨迹", "今日话题", "群友画像", "语出惊人", "炫压抑评级"
]
# 个人总结可选模块；额外提供"并排"组合项以保留横向并排能力
PersonalModuleOption = Literal[
    "无", "3H活跃轨迹", "群友画像", "炫压抑评级", "语出惊人", "群友画像+炫压抑评级(并排)"
]

# 中文模块名 → 渲染器内部代码
_GROUP_MODULE_MAP = {
    "24H活跃轨迹": "24H",
    "今日话题": "Topics",
    "群友画像": "Portraits",
    "语出惊人": "Quotes",
    "炫压抑评级": "Rankings",
}
_PERSONAL_MODULE_MAP = {
    "3H活跃轨迹": "3H",
    "群友画像": "Portraits",
    "炫压抑评级": "Rankings",
    "语出惊人": "Quotes",
    "群友画像+炫压抑评级(并排)": "Portraits,Rankings",
}

# 定时自动总结：单个群处理的整体超时（秒），避免某群卡住拖垮整轮
_AUTO_SUMMARY_PER_GROUP_TIMEOUT = 120


def _slots_to_display_order(slots, mapping: dict) -> List[str]:
    """把若干下拉槽位（中文模块名，"无"表示不显示）按顺序转成渲染器用的代码列表，按模块去重。

    组合项（如 "Portraits,Rankings"）按其成员逐个去重：若某成员已在前面出现过，则跳过该槽位，
    避免同一模块在"独立"和"并排"中重复渲染。
    """
    order: List[str] = []
    seen = set()
    for slot in slots:
        code = mapping.get(slot)
        if not code:
            continue
        members = code.split(",")
        if any(member in seen for member in members):
            continue
        seen.update(members)
        order.append(code)
    return order


# ==================== 配置模型 ====================


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    enabled: bool = Field(
        default=False,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(
        default="2.2.0",
        description="配置文件版本，用于兼容性校验，请勿手动修改",
        json_schema_extra={"label": "配置版本", "disabled": True},
    )


class SummarySection(PluginConfigBase):
    __ui_label__ = "群聊总结"
    __ui_icon__ = "message-square"
    __ui_order__ = 1
    # 5 个下拉槽位，按槽位顺序从上到下显示；某槽位选"无"即隐藏该位置
    slot_1: GroupModuleOption = Field(
        default="24H活跃轨迹",
        description="第 1 个显示的模块（选『无』则此位置不显示）",
        json_schema_extra={"label": "显示模块 1"},
    )
    slot_2: GroupModuleOption = Field(
        default="今日话题",
        description="第 2 个显示的模块",
        json_schema_extra={"label": "显示模块 2"},
    )
    slot_3: GroupModuleOption = Field(
        default="群友画像",
        description="第 3 个显示的模块",
        json_schema_extra={"label": "显示模块 3"},
    )
    slot_4: GroupModuleOption = Field(
        default="语出惊人",
        description="第 4 个显示的模块",
        json_schema_extra={"label": "显示模块 4"},
    )
    slot_5: GroupModuleOption = Field(
        default="炫压抑评级",
        description="第 5 个显示的模块",
        json_schema_extra={"label": "显示模块 5"},
    )
    max_depression_display: int = Field(
        default=6,
        description="炫压抑评级最多展示人数",
        json_schema_extra={"label": "炫压抑最多展示人数"},
    )
    depression_show_bottom: bool = Field(
        default=True,
        description="是否展示倒数排名（开启：前N/2名+后N/2名；关闭：只展示前N名）",
        json_schema_extra={"label": "展示倒数排名"},
    )
    highlight_time_mode: Literal["消息时间跨度", "最活跃时段"] = Field(
        default="消息时间跨度",
        description="图片顶部 Highlight Time 的显示方式：消息时间跨度=今日最早消息到生成前最晚消息；最活跃时段=发言最多的那一小时",
        json_schema_extra={"label": "Highlight Time 显示"},
    )


class UserSummarySection(PluginConfigBase):
    __ui_label__ = "个人总结"
    __ui_icon__ = "user"
    __ui_order__ = 2
    enabled: bool = Field(
        default=True,
        description="是否启用个人总结功能（关闭后所有人都无法使用 /mysummary）",
        json_schema_extra={"label": "启用个人总结"},
    )
    view_others_mode: Literal["白名单", "黑名单"] = Field(
        default="白名单",
        description="查看他人总结的名单模式：白名单=仅名单内用户可看他人；黑名单=名单内用户禁止看他人，其余人可看",
        json_schema_extra={"label": "查看他人名单模式"},
    )
    allowed_users: List[str] = Field(
        default_factory=list,
        description="配合上面的名单模式控制谁能查看他人总结。所有人始终可以查看自己。",
        json_schema_extra={
            "label": "查看他人名单",
            "hint": "为空时所有人都能查看他人；白名单=仅名单内可看他人；黑名单=名单内禁止看他人",
        },
    )
    # 4 个下拉槽位，按槽位顺序显示；选"无"隐藏；含"并排"组合项
    slot_1: PersonalModuleOption = Field(
        default="3H活跃轨迹",
        description="第 1 个显示的模块（选『无』则此位置不显示）",
        json_schema_extra={"label": "显示模块 1"},
    )
    slot_2: PersonalModuleOption = Field(
        default="群友画像+炫压抑评级(并排)",
        description="第 2 个显示的模块（『…并排』表示两个模块横向并排）",
        json_schema_extra={"label": "显示模块 2"},
    )
    slot_3: PersonalModuleOption = Field(
        default="语出惊人",
        description="第 3 个显示的模块",
        json_schema_extra={"label": "显示模块 3"},
    )
    slot_4: PersonalModuleOption = Field(
        default="无",
        description="第 4 个显示的模块",
        json_schema_extra={"label": "显示模块 4"},
    )


class AutoSummarySection(PluginConfigBase):
    __ui_label__ = "自动总结"
    __ui_icon__ = "clock"
    __ui_order__ = 3
    enabled: bool = Field(
        default=False,
        description="是否启用每日自动总结",
        json_schema_extra={"label": "启用每日自动总结"},
    )
    time: str = Field(
        default="23:00",
        description="每日自动总结时间（HH:MM，24小时制）",
        json_schema_extra={"label": "执行时间"},
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="时区设置（IANA 名称，如 Asia/Shanghai）",
        json_schema_extra={"label": "时区"},
    )
    min_messages: int = Field(
        default=10,
        description="生成总结所需的最少消息数量",
        json_schema_extra={"label": "最少消息数"},
    )
    target_chats: List[str] = Field(
        default_factory=list,
        description="目标群聊 QQ 号（为空则对所有活跃群生效）",
        json_schema_extra={"label": "目标群聊"},
    )


class CommandPermissionSection(PluginConfigBase):
    __ui_label__ = "命令权限"
    __ui_icon__ = "shield"
    __ui_order__ = 4
    mode: Literal["黑名单", "白名单"] = Field(
        default="黑名单",
        description="群聊命令权限模式：黑名单=列表中的群禁用命令；白名单=只有列表中的群可用命令",
        json_schema_extra={"label": "权限模式"},
    )
    target_chats: List[str] = Field(
        default_factory=list,
        description="黑/白名单群号列表",
        json_schema_extra={"label": "名单群聊"},
    )
    admin_users: List[str] = Field(
        default_factory=list,
        description="/summary 管理员 QQ 号（有值时仅列表内用户可用 /summary）",
        json_schema_extra={"label": "/summary 管理员", "hint": "无添加则所有人皆可使用"},
    )


class AdvancedSection(PluginConfigBase):
    __ui_label__ = "高级"
    __ui_icon__ = "settings"
    __ui_order__ = 5
    model_task: Literal["utils", "planner", "replyer"] = Field(
        default="utils",
        description="生成总结/分析使用的模型任务。注意：宿主对插件的单次模型调用有约 30 秒硬超时，"
        "建议用 utils 或 planner（通常是快速非思考模型）；replyer 是主回复模型，若它被配置为思考型模型会很慢、极易超时",
        json_schema_extra={"label": "模型任务"},
    )
    inject_memory: bool = Field(
        default=False,
        description="实验性：把生成的总结注入麦麦的会话上下文，供其记忆系统吸收。"
        "群聊总结注入到该群；个人总结注入为对该用户的记忆。",
        json_schema_extra={
            "label": "总结注入麦麦记忆（实验性）",
            "hint": "实验功能，默认关闭；开启后总结内容会进入麦麦记忆",
        },
    )


class DailyAnalysisConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    summary: SummarySection = Field(default_factory=SummarySection)
    user_summary: UserSummarySection = Field(default_factory=UserSummarySection)
    auto_summary: AutoSummarySection = Field(default_factory=AutoSummarySection)
    command_permission: CommandPermissionSection = Field(default_factory=CommandPermissionSection)
    advanced: AdvancedSection = Field(default_factory=AdvancedSection)


# ==================== 插件主类 ====================


class DailyAnalysisPlugin(MaiBotPlugin):
    """每日分析插件"""

    config_model = DailyAnalysisConfig

    def __init__(self) -> None:
        super().__init__()
        self._service: Optional[AnalysisService] = None
        self._renderer: Optional[SummaryRenderer] = None
        self._scheduler_task: Optional[asyncio.Task] = None
        self._last_auto_date: Optional[date] = None
        # 正在生成总结的任务标识，防止同一会话/用户并发刷命令
        self._generating: set = set()

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._service = AnalysisService(self.ctx, self.config.advanced.model_task)
        self._renderer = SummaryRenderer(self.ctx)
        self._start_scheduler()
        self.ctx.logger.info(
            f"每日分析插件已加载（分析模型任务: {self.config.advanced.model_task}）"
        )

    async def on_unload(self) -> None:
        await self._stop_scheduler()
        self.ctx.logger.info("每日分析插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope != "self":
            return
        # 同步分析模型任务
        if self._service is not None:
            self._service.model = self.config.advanced.model_task
        # 自动总结配置可能变化，重启调度器
        await self._stop_scheduler()
        self._start_scheduler()

    # ---------- WebUI 布局：每个配置节一个标签页（分页） ----------

    def get_webui_config_schema(self, **kwargs: Any) -> Dict[str, Any]:
        """在 SDK 自动生成的配置 Schema 基础上，把布局改为「每个配置节一个标签页」。

        这样 WebUI 会把『插件 / 群聊总结 / 个人总结 / 自动总结 / 命令权限 / 高级』
        分别渲染成可切换的页签，而不是堆在一页里。
        """
        try:
            schema = super().get_webui_config_schema(**kwargs)
        except Exception:
            # SDK 基类方法缺失/签名不兼容时，交还宿主走兜底，绝不让配置页崩
            return {}
        try:
            if isinstance(schema, dict):
                sections = schema.get("sections")
                if isinstance(sections, dict) and sections:
                    ordered = sorted(
                        sections.items(),
                        key=lambda kv: (kv[1].get("order", 0) if isinstance(kv[1], dict) else 0),
                    )
                    tabs = []
                    for name, sec in ordered:
                        sec = sec if isinstance(sec, dict) else {}
                        tabs.append(
                            {
                                "id": name,
                                "title": sec.get("title") or name,
                                "icon": sec.get("icon"),
                                "order": sec.get("order", 0),
                                "sections": [name],
                            }
                        )
                    schema["layout"] = {"type": "tabs", "tabs": tabs}
        except Exception:
            # 任何异常都不应影响配置 Schema 的返回，保持 SDK 原样
            return schema
        return schema

    # ---------- 工具：能力返回解析 ----------

    @staticmethod
    def _extract_list(result: Any, key: str) -> List[dict]:
        """从能力返回中稳健提取列表（兼容 list 或 {"success", key:[...]} envelope）"""
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            if result.get("success") is False:
                return []
            val = result.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]
        return []

    @staticmethod
    def _as_id_set(values: Any) -> set:
        """把配置中的 QQ/群号列表归一化为字符串集合"""
        out = set()
        for v in values or []:
            s = str(v).strip()
            if s:
                out.add(s)
        return out

    # ---------- 消息查询与归一化 ----------

    @staticmethod
    def _normalize_message(m: dict) -> Optional[dict]:
        """把新 SDK 的嵌套消息 dict 归一化为分析层需要的扁平结构"""
        if not isinstance(m, dict):
            return None
        info = m.get("message_info") or {}
        uinfo = info.get("user_info") or {}
        ginfo = info.get("group_info") or {}
        try:
            ts = float(m.get("timestamp") or 0)
        except (ValueError, TypeError):
            ts = 0.0
        # 丢弃非法/缺失时间戳的消息：datetime.fromtimestamp(<=0) 在 Windows 会抛 OSError
        if ts <= 0:
            return None
        return {
            "user_id": str(uinfo.get("user_id") or ""),
            "user_nickname": uinfo.get("user_nickname") or "未知用户",
            "user_cardname": uinfo.get("user_cardname") or "",
            "processed_plain_text": m.get("processed_plain_text") or "",
            "time": ts,
            "is_command": bool(m.get("is_command")),
            "is_notify": bool(m.get("is_notify")),
            "group_id": str((ginfo or {}).get("group_id") or ""),
        }

    async def _get_messages(self, stream_id: str, start_ts: float, end_ts: float) -> List[dict]:
        """查询某个聊天流在 [start_ts, end_ts) 内的有效消息（已归一化、按时间正序）"""
        if not stream_id:
            return []
        try:
            result = await self.ctx.message.get_by_time_in_chat(
                stream_id,
                start_time=float(start_ts),
                end_time=float(end_ts),
                limit=0,
                limit_mode="earliest",
                filter_mai=False,
                filter_command=True,
            )
        except Exception as e:
            self.ctx.logger.error(f"查询消息失败 (stream={stream_id}): {e}", exc_info=True)
            return []

        raw_messages = self._extract_list(result, "messages")
        out: List[dict] = []
        for m in raw_messages:
            norm = self._normalize_message(m)
            if norm is None:
                continue
            # filter_command 已在宿主侧过滤命令；这里再排除通知类
            if norm["is_notify"]:
                continue
            out.append(norm)

        out.sort(key=lambda x: x["time"])
        return out

    # ---------- 时间范围 ----------

    @staticmethod
    def _parse_time_range(time_range: str) -> Tuple[Optional[float], Optional[float], Optional[datetime]]:
        """解析 今天/昨天 为 (start_ts, end_ts, target_date)，不支持返回 (None, None, None)"""
        now = datetime.now()
        today_start = datetime(now.year, now.month, now.day)
        if time_range in ("今天", ""):
            return today_start.timestamp(), now.timestamp(), now
        if time_range == "昨天":
            yesterday_start = today_start - timedelta(days=1)
            return yesterday_start.timestamp(), today_start.timestamp(), now - timedelta(days=1)
        return None, None, None

    # ---------- 权限 ----------

    def _check_group_permission(self, group_id: str) -> bool:
        """群聊黑/白名单权限检查（True=允许）"""
        cfg = self.config.command_permission
        target_chats = self._as_id_set(cfg.target_chats)
        gid = str(group_id)
        if cfg.mode == "白名单":
            # 白名单：列表为空则全部禁用；否则仅列表内允许
            return bool(target_chats) and gid in target_chats
        # 黑名单：列表内禁用，其余允许
        return gid not in target_chats

    # ==================== 命令：群聊总结 ====================

    @Command("summary", description="生成群聊总结", pattern=r"^/summary(?:\s+(?P<args>.*))?$")
    async def cmd_summary(
        self, stream_id: str = "", group_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, bool]:
        try:
            if not self.config.plugin.enabled:
                return False, "插件未启用", False

            if not group_id:
                return True, "非群聊消息，跳过 /summary", False

            if not self._check_group_permission(group_id):
                return False, f"群 {group_id} 无 /summary 权限", False

            # 管理员限制
            admin_users = self._as_id_set(self.config.command_permission.admin_users)
            if admin_users and str(user_id) not in admin_users:
                return False, f"用户 {user_id} 非管理员", False

            args = ((kwargs.get("matched_groups") or {}).get("args") or "").strip()
            time_range = args if args in ("今天", "昨天") else "今天"

            start_ts, end_ts, target_date = self._parse_time_range(time_range)
            if start_ts is None:
                await self.ctx.send.text("只支持查询今天或昨天的记录哦", stream_id)
                return True, f"不支持的时间范围: {args}", True

            messages = await self._get_messages(stream_id, start_ts, end_ts)
            if not messages:
                await self.ctx.send.text(f"{time_range}没有聊天记录呢", stream_id)
                return True, "没有聊天记录", True

            guard_key = f"g:{stream_id}"
            if guard_key in self._generating:
                await self.ctx.send.text("上一份群聊总结还在生成中，请稍候~", stream_id)
                return True, "重复请求，生成中", True
            self._generating.add(guard_key)
            try:
                await self.ctx.send.text(f"⏳ 正在分析{time_range}的聊天记录，请稍候...", stream_id)

                summary = await self._service.analyze_group_summary(messages, len(messages))
                if not summary:
                    self.ctx.logger.error("群聊总结文本生成失败")
                    return False, "生成总结失败", True

                image_base64 = await self._build_group_summary_image(
                    messages, summary, time_range, target_date
                )
                if not image_base64:
                    # 按需求：不发文字兜底，仅在 MaiBot 端报错
                    self.ctx.logger.error(f"群 {group_id} 的群聊总结图片渲染失败")
                    return False, "图片渲染失败", True

                await self.ctx.send.image(image_base64, stream_id)
                if self.config.advanced.inject_memory:
                    await self._inject_memory(
                        stream_id, f"【{time_range}群聊总结】{summary}", "plugin:daily_analysis:group"
                    )
                return True, "已生成群聊总结", True
            finally:
                self._generating.discard(guard_key)

        except Exception as e:
            self.ctx.logger.error(f"执行 /summary 出错: {e}", exc_info=True)
            return False, f"执行出错: {e}", True

    # ==================== 命令：个人总结 ====================

    @Command("mysummary", description="生成个人总结", pattern=r"^/mysummary(?:\s+(?P<args>.*))?$")
    async def cmd_mysummary(
        self, stream_id: str = "", group_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, bool]:
        try:
            if not self.config.plugin.enabled:
                return False, "插件未启用", False

            if not group_id:
                return False, "非群聊消息", False

            if not self._check_group_permission(group_id):
                return False, f"群 {group_id} 无 /mysummary 权限", False

            if not self.config.user_summary.enabled:
                return False, "个人总结功能已关闭", False

            current_user_id = str(user_id)
            args = ((kwargs.get("matched_groups") or {}).get("args") or "").strip()
            message_dict = kwargs.get("message") or {}

            # 解析时间范围
            time_range = "昨天" if "昨天" in args else "今天"

            # 解析目标用户：优先 @ 消息段，其次纯数字 QQ 号
            target_user_id = ""
            target_user_name = ""
            at_targets = self._extract_at_targets(message_dict)
            if at_targets:
                target_user_id, target_user_name = at_targets[0]
            else:
                for token in args.split():
                    if token.isdigit():
                        target_user_id = token
                        break

            # 决定查看对象与权限（所有人始终可看自己；看他人受名单模式控制）
            allowed_users = self._as_id_set(self.config.user_summary.allowed_users)
            view_mode = self.config.user_summary.view_others_mode
            if target_user_id and target_user_id != current_user_id:
                if allowed_users:
                    in_list = current_user_id in allowed_users
                    # 白名单：不在名单→禁止；黑名单：在名单→禁止
                    denied = (not in_list) if view_mode == "白名单" else in_list
                    if denied:
                        return False, f"用户 {current_user_id} 无查看他人总结权限（{view_mode}）", False
                query_user_id = target_user_id
                query_user_name = target_user_name or f"用户{target_user_id}"
            else:
                query_user_id = current_user_id
                query_user_name = ""  # 稍后从消息记录补全

            start_ts, end_ts, target_date = self._parse_time_range(time_range)
            if start_ts is None:
                await self.ctx.send.text("只支持查询今天或昨天的记录哦", stream_id)
                return True, f"不支持的时间范围: {args}", True

            all_messages = await self._get_messages(stream_id, start_ts, end_ts)
            if not all_messages:
                await self.ctx.send.text(f"{time_range}群里没有聊天记录呢", stream_id)
                return True, "没有聊天记录", True

            user_messages = AnalysisService.filter_user_messages(all_messages, query_user_id)

            # 从消息记录补全用户名
            if user_messages:
                first = user_messages[0]
                query_user_name = (
                    first.get("user_cardname") or first.get("user_nickname") or query_user_name
                )
            if not query_user_name:
                query_user_name = f"用户{query_user_id}"

            is_self = query_user_id == current_user_id

            if not user_messages:
                who = "你" if is_self else query_user_name
                tail = "，多说说话吧~" if is_self else "~"
                await self.ctx.send.text(f"{time_range}{who}没有发言记录呢{tail}", stream_id)
                return True, "用户没有发言记录", True

            if len(user_messages) < 3:
                if is_self:
                    await self.ctx.send.text(
                        f"{time_range}你只发了{len(user_messages)}条消息，发言太少啦，多聊聊天再来总结吧~",
                        stream_id,
                    )
                else:
                    await self.ctx.send.text(
                        f"{time_range}{query_user_name}只发了{len(user_messages)}条消息，发言太少无法生成总结~",
                        stream_id,
                    )
                return True, "用户发言太少", True

            guard_key = f"u:{stream_id}:{query_user_id}"
            if guard_key in self._generating:
                await self.ctx.send.text("上一份个人总结还在生成中，请稍候~", stream_id)
                return True, "重复请求，生成中", True
            self._generating.add(guard_key)
            try:
                await self.ctx.send.text(
                    f"⏳ 正在分析{query_user_name}的{time_range}发言记录，请稍候...", stream_id
                )

                image_base64, user_summary_text = await self._build_user_summary_image(
                    user_messages, query_user_name, query_user_id, target_date
                )
                if not image_base64:
                    self.ctx.logger.error(f"用户 {query_user_id} 的个人总结图片渲染失败")
                    return False, "图片渲染失败", True

                await self.ctx.send.image(image_base64, stream_id)
                if self.config.advanced.inject_memory and user_summary_text:
                    note = f"【关于 {query_user_name}（QQ{query_user_id}）{time_range}的个人总结】{user_summary_text}"
                    await self._inject_memory(
                        stream_id, note, f"plugin:daily_analysis:user:{query_user_id}"
                    )
                return True, "已生成个人总结", True
            finally:
                self._generating.discard(guard_key)

        except Exception as e:
            self.ctx.logger.error(f"执行 /mysummary 出错: {e}", exc_info=True)
            return False, f"执行出错: {e}", True

    @staticmethod
    def _extract_at_targets(message_dict: dict) -> List[Tuple[str, str]]:
        """从消息的 raw_message 段中提取 @ 目标 (user_id, nickname) 列表"""
        targets: List[Tuple[str, str]] = []
        if not isinstance(message_dict, dict):
            return targets
        for seg in message_dict.get("raw_message", []) or []:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            uid = str(data.get("target_user_id") or "")
            name = data.get("target_user_nickname") or data.get("target_user_cardname") or ""
            if uid:
                targets.append((uid, name))
        return targets

    async def _inject_memory(self, stream_id: str, text: str, source_kind: str) -> None:
        """实验性：把总结注入会话上下文，供麦麦记忆系统吸收。失败不影响主流程。"""
        if not text or not stream_id:
            return
        try:
            await self.ctx.maisaka.context.append(
                stream_id=stream_id,
                segments=[{"type": "text", "content": text}],
                visible_text=text[:200],
                source_kind=source_kind,
            )
            self.ctx.logger.info(f"已注入总结到麦麦记忆: {source_kind}")
        except Exception as e:
            self.ctx.logger.warning(f"注入麦麦记忆失败: {e}")

    # ==================== 总结生成（复用逻辑） ====================

    async def _build_group_summary_image(
        self, messages: List[dict], summary: str, time_range: str, target_date: datetime
    ) -> Optional[str]:
        """分析各模块数据并渲染群聊总结图片，返回 base64"""
        service = self._service
        participants = {msg.get("user_nickname", "") for msg in messages if msg.get("user_nickname")}
        user_stats = service.analyze_user_stats(messages)

        # 24 小时发言分布：直接汇总各用户已统计的 hours，避免对全量消息再遍历一趟
        hourly_counter: Counter = Counter()
        for stats in user_stats.values():
            hourly_counter.update(stats.get("hours", {}))
        hourly_distribution: Dict[int, int] = {h: hourly_counter.get(h, 0) for h in range(24)}

        # 真实表情统计与总字数（汇总各用户，替代旧版用消息数/总结文案长度的估算）
        emoji_count = sum(stats.get("emoji_count", 0) for stats in user_stats.values())
        total_characters = sum(stats.get("char_count", 0) for stats in user_stats.values())

        # Highlight Time：按配置决定显示方式
        # - "消息时间跨度"：今日最早消息 → 渲染前最晚消息（messages 已按时间升序）
        # - "最活跃时段"：传 None，渲染器回退到发言最多的那一小时
        highlight_time = None
        if self.config.summary.highlight_time_mode == "消息时间跨度" and messages:
            try:
                first_t = datetime.fromtimestamp(messages[0].get("time", 0))
                last_t = datetime.fromtimestamp(messages[-1].get("time", 0))
                highlight_time = f"{first_t:%H:%M}-{last_t:%H:%M}"
            except (ValueError, OSError, OverflowError):
                highlight_time = None

        # 并发执行各项 LLM 分析（彼此独立），缩短整体耗时；单项异常不拖垮整图
        results = await asyncio.gather(
            service.analyze_topics(messages),
            service.analyze_user_titles(messages, user_stats),
            service.analyze_golden_quotes(messages),
            service.analyze_depression_index(messages, user_stats),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                self.ctx.logger.error(f"群聊分析子任务异常: {r}", exc_info=r)
        topics = results[0] if isinstance(results[0], list) else []
        user_titles = results[1] if isinstance(results[1], list) else []
        golden_quotes = results[2] if isinstance(results[2], list) else []
        depression_index = results[3] if isinstance(results[3], list) else []

        return await self._renderer.generate_summary_image(
            title=f"{time_range}的群聊总结",
            summary_text=summary,
            time_info=target_date.strftime("%Y-%m-%d"),
            message_count=len(messages),
            participant_count=len(participants),
            emoji_count=emoji_count,
            total_characters=total_characters,
            topics=topics,
            user_titles=user_titles,
            golden_quotes=golden_quotes,
            depression_index=depression_index,
            hourly_distribution=hourly_distribution,
            display_order=_slots_to_display_order(
                [
                    self.config.summary.slot_1,
                    self.config.summary.slot_2,
                    self.config.summary.slot_3,
                    self.config.summary.slot_4,
                    self.config.summary.slot_5,
                ],
                _GROUP_MODULE_MAP,
            ),
            target_date=target_date,
            max_depression_display=self.config.summary.max_depression_display,
            depression_show_bottom=self.config.summary.depression_show_bottom,
            highlight_time=highlight_time,
        )

    async def _build_user_summary_image(
        self, user_messages: List[dict], user_name: str, user_id: str, target_date: datetime
    ) -> Tuple[Optional[str], Optional[str]]:
        """分析个人各模块并渲染个人总结图片，返回 (base64, summary_text)"""
        service = self._service
        user_stats = service.analyze_single_user_stats(user_messages)

        results = await asyncio.gather(
            service.analyze_single_user_summary(user_messages, user_name, user_id),
            service.analyze_single_user_portrait(user_messages, user_name, user_id),
            service.analyze_single_user_depression(user_messages, user_name, user_id),
            service.analyze_single_user_quotes(user_messages, user_name, user_id),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                self.ctx.logger.error(f"个人分析子任务异常: {r}", exc_info=r)
        summary_text = results[0] if isinstance(results[0], str) else None
        portrait_data = results[1] if isinstance(results[1], dict) else None
        depression_data = results[2] if isinstance(results[2], dict) else None
        golden_quotes = results[3] if isinstance(results[3], list) else None

        display_order = _slots_to_display_order(
            [
                self.config.user_summary.slot_1,
                self.config.user_summary.slot_2,
                self.config.user_summary.slot_3,
                self.config.user_summary.slot_4,
            ],
            _PERSONAL_MODULE_MAP,
        )

        image_base64 = await self._renderer.generate_user_summary_image(
            user_name=user_name,
            user_id=user_id,
            summary_text=summary_text or "",
            message_count=user_stats["message_count"],
            total_characters=user_stats["char_count"],
            emoji_count=user_stats["emoji_count"],
            hourly_distribution=user_stats["hourly_distribution"],
            user_title=portrait_data.get("title", "") if portrait_data else "",
            user_mbti=portrait_data.get("mbti", "") if portrait_data else "",
            portrait_data=portrait_data,
            depression_data=depression_data,
            golden_quotes=golden_quotes,
            display_order=display_order,
            target_date=target_date,
        )
        return image_base64, summary_text

    # ==================== 定时自动总结 ====================

    def _start_scheduler(self) -> None:
        if self._scheduler_task is not None and not self._scheduler_task.done():
            return
        if not self.config.plugin.enabled or not self.config.auto_summary.enabled:
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        self.ctx.logger.info(
            f"定时总结调度器已启动 - 执行时间: {self.config.auto_summary.time}"
        )

    async def _stop_scheduler(self) -> None:
        task = self._scheduler_task
        self._scheduler_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _timezone_now(self) -> datetime:
        tz_str = self.config.auto_summary.timezone or "Asia/Shanghai"
        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(tz_str))
        except Exception as e:
            self.ctx.logger.warning(f"时区 {tz_str} 处理失败，使用系统时间: {e}")
            return datetime.now()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                now = self._timezone_now()
                time_str = self.config.auto_summary.time or "23:00"
                try:
                    hour, minute = map(int, time_str.split(":"))
                except ValueError:
                    self.ctx.logger.error(f"无效的时间格式: {time_str}，使用 23:00")
                    hour, minute = 23, 0

                today_schedule = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if now >= today_schedule:
                    today_schedule += timedelta(days=1)

                wait_seconds = max(1.0, (today_schedule - now).total_seconds())
                self.ctx.logger.info(
                    f"下次自动总结: {today_schedule.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"(等待 {int(wait_seconds // 3600)}小时{int((wait_seconds % 3600) // 60)}分钟)"
                )
                await asyncio.sleep(wait_seconds)

                current_date = self._timezone_now().date()
                if self._last_auto_date == current_date:
                    continue

                self.ctx.logger.info(f"开始执行每日自动总结 - {current_date}")
                await self._generate_daily_summaries()
                self._last_auto_date = current_date
                self.ctx.logger.info("每日自动总结执行完成")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"定时任务执行出错: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _generate_daily_summaries(self) -> None:
        """为所有（或指定）群聊生成今日总结并发送"""
        # 使用配置时区计算"今天"窗口，避免宿主系统时区与配置时区不一致时取错区间
        now = self._timezone_now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_ts = today_start.timestamp()
        end_ts = now.timestamp()

        # 获取所有群聊流
        try:
            streams_result = await self.ctx.chat.get_group_streams(platform="qq")
        except Exception as e:
            self.ctx.logger.error(f"获取群聊列表失败: {e}", exc_info=True)
            return
        streams = self._extract_list(streams_result, "streams")
        if not streams:
            self.ctx.logger.info("没有可用的群聊流，跳过自动总结")
            return

        target_chats = self._as_id_set(self.config.auto_summary.target_chats)
        min_messages = self.config.auto_summary.min_messages

        for stream in streams:
            session_id = str(stream.get("session_id") or stream.get("stream_id") or "")
            group_id = str(stream.get("group_id") or "")
            if not session_id:
                continue
            if target_chats and group_id not in target_chats:
                continue

            try:
                # 单群整体超时兜底：避免某个群卡住（如上游慢）拖垮后续所有群
                await asyncio.wait_for(
                    self._generate_one_group_summary(
                        session_id, group_id, start_ts, end_ts, now, min_messages
                    ),
                    timeout=_AUTO_SUMMARY_PER_GROUP_TIMEOUT,
                )
                await asyncio.sleep(2)
            except asyncio.TimeoutError:
                self.ctx.logger.error(f"群 {group_id} 自动总结超时（>{_AUTO_SUMMARY_PER_GROUP_TIMEOUT}s），跳过")
            except Exception as e:
                self.ctx.logger.error(f"群 {group_id} 自动总结失败: {e}", exc_info=True)

    async def _generate_one_group_summary(
        self, session_id: str, group_id: str, start_ts: float, end_ts: float,
        now: datetime, min_messages: int,
    ) -> None:
        """为单个群生成并发送今日总结（供定时任务逐群调用，带超时兜底）。"""
        messages = await self._get_messages(session_id, start_ts, end_ts)
        if len(messages) < min_messages:
            return

        summary = await self._service.analyze_group_summary(messages, len(messages))
        if not summary:
            self.ctx.logger.warning(f"群 {group_id} 自动总结文本生成失败")
            return

        image_base64 = await self._build_group_summary_image(messages, summary, "今天", now)
        if not image_base64:
            self.ctx.logger.error(f"群 {group_id} 自动总结图片渲染失败")
            return

        await self.ctx.send.image(image_base64, session_id)
        if self.config.advanced.inject_memory:
            await self._inject_memory(
                session_id, f"【今日群聊总结】{summary}", "plugin:daily_analysis:group"
            )


def create_plugin() -> DailyAnalysisPlugin:
    return DailyAnalysisPlugin()
