"""
聊天分析服务

把聊天记录交给 LLM 做各类分析（话题、群友称号、金句、炫压抑评级、个人画像等）。
所有 LLM 调用通过宿主注入的 ``ctx.llm`` 能力完成；纯数据处理保持为静态方法。

消息字典遵循插件运行时的扁平结构（由 plugin.py 的归一化层提供）：
    {
        "user_id": str, "user_nickname": str, "user_cardname": str,
        "processed_plain_text": str, "time": float,
        "is_command": bool, "is_notify": bool,
    }
"""

import re
import json
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional
from collections import Counter

from .constants import AnalysisConfig


# LLM 各任务的输出 token 上限。
# maibot-sdk 2.x 新版支持 timeout_ms 透传，可自定义 RPC 超时（默认 30 秒）。
# 命令使用后台任务模式执行，不受 60 秒命令超时限制，可设置宽松的超时。
_LLM_TIMEOUT_MS = 120000  # 120 秒，后台任务无时间压力
_SUMMARY_MAX_TOKENS = 1200
_JSON_MAX_TOKENS = 2500
# 多用户 JSON（群友称号/炫压抑评级）输出较长，恢复到 2500
_MULTI_USER_JSON_MAX_TOKENS = 2500

# LLM 输入消息上限：取最近 N 条参与总结/话题/金句，避免超大群 prompt 过长拖慢生成
_MAX_INPUT_MESSAGES = 300

# 并发 LLM 调用上限。设为 2：兼顾速度与上游模型池并发能力。
_LLM_MAX_CONCURRENCY = 2

# 默认模型任务：utils 对应快速非思考模型
_DEFAULT_MODEL_TASK = "utils"


class AnalysisService:
    """聊天记录分析服务（绑定插件 ctx，统一走 ctx.llm / ctx.logger）"""

    # Emoji 正则（精确匹配，避免误伤中文字符）
    EMOJI_PATTERN = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags
        "\U00002702-\U000027B0"  # dingbats
        "\U0001F900-\U0001F9FF"  # supplemental symbols
        "\U0001FA00-\U0001FA6F"  # chess symbols
        "\U0001FA70-\U0001FAFF"  # symbols and pictographs extended-A
        "\U00002600-\U000026FF"  # misc symbols
        "\U0000FE00-\U0000FE0F"  # variation selectors
        "\U0001F000-\U0001F02F"  # mahjong tiles
        "\U0001F0A0-\U0001F0FF"  # playing cards
        "]+",
        flags=re.UNICODE,
    )

    def __init__(self, ctx: Any, model: str = _DEFAULT_MODEL_TASK):
        self.ctx = ctx
        self.logger = ctx.logger
        # 模型任务名（可由插件配置覆盖）。默认 utils=快速模型，确保 30 秒内返回
        self.model = model or _DEFAULT_MODEL_TASK
        # 限制并发 LLM 调用数。设为 2：兼顾速度与上游模型池并发能力。
        # timeout_ms 透传已解决 30 秒硬限制，信号量仅做流控而非超时规避。
        self._llm_semaphore = asyncio.Semaphore(_LLM_MAX_CONCURRENCY)

    # ==================== LLM 调用封装 ====================

    async def _llm(
        self,
        prompt: str,
        *,
        request_type: str,
        max_tokens: int = _JSON_MAX_TOKENS,
        temperature: float = 0.7,
        _retries: int = 2,
    ) -> Optional[str]:
        """调用宿主 LLM 能力，成功返回文本，失败返回 None。

        利用 maibot-sdk 2.x 的 timeout_ms 透传能力，主动设置较长的 RPC 超时，
        给 LLM 推理预留充足时间。内置重试机制：超时时自动重试一次（截断 prompt）。
        """
        attempt = 0
        current_prompt = prompt
        current_max_tokens = max_tokens

        while attempt < _retries:
            try:
                async with self._llm_semaphore:
                    # 通过 **kwargs 透传 timeout_ms 到 call_capability → call_host_method → _rpc_call
                    result = await self.ctx.llm.generate(
                        current_prompt,
                        model=self.model,
                        temperature=temperature,
                        max_tokens=current_max_tokens,
                        timeout_ms=_LLM_TIMEOUT_MS,
                    )
            except Exception as e:
                err_str = str(e)
                is_timeout = "E_TIMEOUT" in err_str or "超时" in err_str or "timed out" in err_str.lower()
                attempt += 1
                if is_timeout and attempt < _retries:
                    # 超时重试：截断 prompt 至 60%，降低 max_tokens，等待 3 秒后重试
                    truncated_len = int(len(current_prompt) * 0.6)
                    current_prompt = current_prompt[:truncated_len]
                    current_max_tokens = int(current_max_tokens * 0.75)
                    self.logger.warning(
                        f"LLM 超时 ({request_type})，第 {attempt} 次重试 "
                        f"(prompt 截至 {len(current_prompt)} 字符, max_tokens={current_max_tokens})"
                    )
                    await asyncio.sleep(3)
                    continue
                self.logger.error(f"LLM 调用异常 ({request_type}): {e}", exc_info=True)
                return None

            # 成功拿到响应
            if not isinstance(result, dict) or not result.get("success", False):
                err = result.get("error") if isinstance(result, dict) else result
                self.logger.error(f"LLM 生成失败 ({request_type}): {err}")
                return None

            response = result.get("response")
            if not response:
                self.logger.error(f"LLM 返回空内容 ({request_type})")
                return None
            return str(response)

        return None

    # ==================== 纯数据处理（静态） ====================

    @staticmethod
    def format_messages(messages: List[dict]) -> str:
        """格式化聊天记录为文本"""
        formatted = []
        for msg in messages:
            timestamp = msg.get("time", 0)
            time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
            nickname = msg.get("user_nickname", "未知用户")
            cardname = msg.get("user_cardname", "")
            display_name = cardname if cardname else nickname
            text = msg.get("processed_plain_text") or ""

            if text:
                formatted.append(f"[{time_str}] {display_name}: {text}")

        return "\n".join(formatted)

    @classmethod
    def count_emojis(cls, text: str) -> int:
        """统计文本中的 emoji 数量"""
        return len(cls.EMOJI_PATTERN.findall(text))

    @classmethod
    def analyze_user_stats(cls, messages: List[dict]) -> Dict[str, Dict]:
        """分析用户统计数据

        Returns:
            {user_id: {user_id, nickname, message_count, char_count, emoji_count, hours}}
        """
        user_stats: Dict[str, Dict] = {}

        for msg in messages:
            user_id = str(msg.get("user_id", ""))
            if not user_id:
                continue

            nickname = msg.get("user_nickname", "未知用户")
            text = msg.get("processed_plain_text") or ""

            if user_id not in user_stats:
                user_stats[user_id] = {
                    "user_id": user_id,
                    "nickname": nickname,
                    "message_count": 0,
                    "char_count": 0,
                    "emoji_count": 0,
                    "hours": Counter(),
                }

            stats = user_stats[user_id]
            stats["message_count"] += 1
            stats["char_count"] += len(text)
            stats["emoji_count"] += cls.count_emojis(text)

            timestamp = msg.get("time", 0)
            hour = datetime.fromtimestamp(timestamp).hour
            stats["hours"][hour] += 1

        return user_stats

    @staticmethod
    def filter_user_messages(messages: List[dict], user_id: str) -> List[dict]:
        """过滤出指定用户的消息"""
        user_id_str = str(user_id)
        return [msg for msg in messages if str(msg.get("user_id", "")) == user_id_str]

    @classmethod
    def analyze_single_user_stats(cls, messages: List[dict]) -> Dict:
        """分析单个用户的统计数据（只使用该用户的消息）"""
        if not messages:
            return {
                "message_count": 0,
                "char_count": 0,
                "emoji_count": 0,
                "hours": Counter(),
                "hourly_distribution": {},
            }

        message_count = 0
        char_count = 0
        emoji_count = 0
        hours = Counter()

        for msg in messages:
            text = msg.get("processed_plain_text") or ""
            message_count += 1
            char_count += len(text)
            emoji_count += cls.count_emojis(text)

            timestamp = msg.get("time", 0)
            hour = datetime.fromtimestamp(timestamp).hour
            hours[hour] += 1

        hourly_distribution = {h: hours.get(h, 0) for h in range(24)}

        return {
            "message_count": message_count,
            "char_count": char_count,
            "emoji_count": emoji_count,
            "hours": hours,
            "hourly_distribution": hourly_distribution,
        }

    # ==================== 群聊整体分析（LLM） ====================

    async def analyze_group_summary(self, messages: List[dict], message_count: int) -> Optional[str]:
        """调用 LLM 生成群聊故事化总结文本（自动读取机器人人设）。"""
        try:
            # 取最近若干条参与总结，避免超大群 prompt 过长导致生成超时
            recent = messages[-_MAX_INPUT_MESSAGES:]
            chat_text = self.format_messages(recent)

            bot_name = await self.ctx.config.get("bot.nickname", "麦麦")
            personality = await self.ctx.config.get("personality.personality", "")
            reply_style = await self.ctx.config.get("personality.reply_style", "")

            participants = {
                msg.get("user_nickname", "") for msg in messages if msg.get("user_nickname")
            }

            prompt = f"""你是{bot_name}。{personality}
{reply_style}

以下是群聊记录（{message_count}条消息，{len(participants)}人参与）：
{chat_text}

请像给朋友讲故事一样复述群里发生了什么。

要求：
1. 按时间顺序讲，保持连贯性
2. 精彩内容详细说，平淡内容略过
3. 对话要说清谁说了什么、谁怎么回的
4. 必须有具体人名和具体内容，不要抽象描述
5. 口语化，不要用"首先""其次""然后""总之"这类词
6. 控制在 300 字以内，简明扼要

直接开始，不要标题。"""

            summary = await self._llm(
                prompt, request_type="plugin.chat_summary", max_tokens=_SUMMARY_MAX_TOKENS
            )
            return summary.strip() if summary else None
        except Exception as e:
            self.logger.error(f"生成群聊总结文本出错: {e}", exc_info=True)
            return None

    async def analyze_topics(self, messages: List[dict]) -> List[Dict]:
        """使用 LLM 分析聊天话题，返回 [{topic, contributors, detail}, ...]"""
        try:
            if not messages:
                return []

            messages = messages[-_MAX_INPUT_MESSAGES:]
            text_messages = []
            for msg in messages:
                nickname = msg.get("user_nickname", "未知用户")
                cardname = msg.get("user_cardname", "")
                display_name = cardname if cardname else nickname
                text = msg.get("processed_plain_text") or ""
                timestamp = msg.get("time", 0)
                time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M")

                text = re.sub(r"@[^<\s]+<\d+>\s*", "", text).strip()

                if len(text) > 2 and not text.startswith("/"):
                    text_messages.append({"sender": display_name, "time": time_str, "content": text})

            if not text_messages:
                return []

            messages_text = "\n".join(
                f"[{m['time']}] {m['sender']}: {m['content']}" for m in text_messages
            )

            prompt = f"""从群聊记录中提取3-5个热门话题。

群聊记录：
{messages_text}

要求：
1. 话题标题4-8个字，简洁明了
2. 参与者列表包含2-5个主要发言人
3. 详情描述50-80字，说明讨论了什么、有什么有趣的观点
4. 只提取有实质内容的话题，避免简单问候、闲聊
5. 话题按热度排序（参与人数多、讨论深入的优先）

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "topic": "话题标题",
    "contributors": ["参与者1", "参与者2"],
    "detail": "话题详情描述"
  }}
]"""

            result = await self._llm(prompt, request_type="plugin.chat_summary.topics")
            if result is None:
                return []
            return self._validate_topics(self._parse_llm_json(result))

        except Exception as e:
            self.logger.error(f"分析话题失败: {e}", exc_info=True)
            return []

    async def analyze_user_titles(self, messages: List[dict], user_stats: Dict) -> List[Dict]:
        """使用 LLM 分析群友称号（含 MBTI），返回 [{name, title, mbti, reason, user_id}, ...]"""
        try:
            active_users = {
                uid: stats
                for uid, stats in user_stats.items()
                if stats["message_count"] >= AnalysisConfig.MIN_MESSAGES_FOR_TITLE
            }
            if not active_users:
                return []

            users_text = []
            user_samples: Dict[str, List[str]] = {}

            for msg in messages:
                user_id = str(msg.get("user_id", ""))
                if user_id not in active_users:
                    continue
                text = msg.get("processed_plain_text") or ""
                if len(text) < 5:
                    continue
                user_samples.setdefault(user_id, [])
                if len(user_samples[user_id]) < 5:
                    user_samples[user_id].append(text[:60])

            for user_id, stats in sorted(
                active_users.items(), key=lambda x: x[1]["message_count"], reverse=True
            )[: AnalysisConfig.MAX_USERS_FOR_TITLE]:
                night_messages = sum(stats["hours"][h] for h in range(0, 6))
                avg_chars = stats["char_count"] / stats["message_count"] if stats["message_count"] else 0
                emoji_ratio = stats["emoji_count"] / stats["message_count"] if stats["message_count"] else 0
                night_ratio = night_messages / stats["message_count"] if stats["message_count"] else 0

                samples = user_samples.get(user_id, [])
                samples_text = "\n  ".join(f"- {s}" for s in samples) if samples else "  (无有效样本)"

                users_text.append(
                    f"【{stats['nickname']}】\n"
                    f"  发言{stats['message_count']}条, 平均{avg_chars:.1f}字, "
                    f"表情比例{emoji_ratio:.2f}, 夜间发言比例{night_ratio:.2f}\n"
                    f"  发言样本：\n  {samples_text}"
                )

            users_info = "\n\n".join(users_text)

            prompt = f"""根据群友数据创造有趣的称号，并判断MBTI类型。

用户数据：
{users_info}

要求：
1. 称号2-4个汉字
2. MBTI类型基于发言特征判断（如ENFP、INTJ等16种之一）
   - E/I: 外向(话多、互动多) vs 内向(话少、深度思考)
   - S/N: 实感(具体事实) vs 直觉(抽象概念)
   - T/F: 思考(逻辑理性) vs 情感(感性表达)
   - J/P: 判断(有条理) vs 知觉(随性自由)
3. 基于真实数据，不要编造
4. 避免重复类型（不要多个"龙王""话痨"）
5. 有创意，避免陈词滥调
6. **理由必须写满60-80字，引用具体数据说明为什么（发言数、平均字数、表情比例、夜间比例等），不要空洞，要详细**

参考分类：活跃度（龙王、潜水员）、时间特征（夜猫子）、内容风格（段子手）、表情/情绪（表情帝）、互动特征（接梗高手）

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "name": "用户名",
    "title": "称号（2-4字）",
    "mbti": "MBTI类型（如ENFP）",
    "reason": "获得理由,必须60-80字,引用数据"
  }}
]"""

            result = await self._llm(
                prompt, request_type="plugin.chat_summary.titles",
                max_tokens=_MULTI_USER_JSON_MAX_TOKENS,
            )
            if result is None:
                return []
            return self._validate_titles(self._parse_llm_json(result), user_stats)

        except Exception as e:
            self.logger.error(f"分析群友称号失败: {e}", exc_info=True)
            return []

    async def analyze_golden_quotes(self, messages: List[dict]) -> List[Dict]:
        """使用 LLM 提取群聊金句，返回 [{content, sender, reason}, ...]"""
        try:
            messages = messages[-_MAX_INPUT_MESSAGES:]
            interesting_messages = []
            for msg in messages:
                nickname = msg.get("user_nickname", "未知用户")
                cardname = msg.get("user_cardname", "")
                display_name = cardname if cardname else nickname
                text = msg.get("processed_plain_text") or ""
                timestamp = msg.get("time", 0)
                time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M")

                text = re.sub(r"@[^<\s]+<\d+>\s*", "", text).strip()

                if (
                    AnalysisConfig.MIN_QUOTE_LENGTH <= len(text) <= AnalysisConfig.MAX_QUOTE_LENGTH
                    and not text.startswith(("http", "www", "/"))
                ):
                    interesting_messages.append({"sender": display_name, "time": time_str, "content": text})

            if not interesting_messages:
                return []

            messages_text = "\n".join(
                f"[{m['time']}] {m['sender']}: {m['content']}" for m in interesting_messages
            )

            prompt = f"""从群聊记录中挑选3-5句最有趣的金句。

优先级（从高到低）：
1. 神回复、接梗高手（优先选择回复的那句，不是发起的）
2. 有上下文才有笑点的梗
3. 精彩吐槽或离谱观点
4. 高/低情商发言

要求：
- 每个金句来自不同发言人
- 避免平淡陈述句、问候语
- 内容水可以只返回2-3个
- 理由严格控制在50-70字，说明为什么有趣、回应了什么

群聊记录：
{messages_text}

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "content": "金句原文",
    "sender": "发言人",
    "reason": "选择理由（50-70字）"
  }}
]"""

            result = await self._llm(prompt, request_type="plugin.chat_summary.quotes")
            if result is None:
                return []
            return self._validate_quotes(self._parse_llm_json(result))

        except Exception as e:
            self.logger.error(f"分析金句失败: {e}", exc_info=True)
            return []

    async def analyze_depression_index(self, messages: List[dict], user_stats: Dict) -> List[Dict]:
        """使用 LLM 分析群友炫压抑指数，返回按 score 降序的 [{name, user_id, rank, score, comment}, ...]"""
        try:
            active_users = {
                uid: stats
                for uid, stats in user_stats.items()
                if stats["message_count"] >= AnalysisConfig.MIN_MESSAGES_FOR_TITLE
            }
            if not active_users:
                return []

            user_messages: Dict[str, List[str]] = {}
            for msg in messages:
                user_id = str(msg.get("user_id", ""))
                if user_id not in active_users:
                    continue
                text = msg.get("processed_plain_text") or ""
                if len(text) < 5:
                    continue
                user_messages.setdefault(user_id, [])
                if len(user_messages[user_id]) < 20:
                    user_messages[user_id].append(text)

            if not user_messages:
                return []

            users_sample = []
            for user_id in sorted(
                user_messages.keys(), key=lambda uid: active_users[uid]["message_count"], reverse=True
            ):
                nickname = active_users[user_id]["nickname"]
                sample_texts = user_messages[user_id][:10]
                users_sample.append(
                    f"【{nickname}】\n" + "\n".join(f"  - {t[:60]}" for t in sample_texts)
                )

            users_info = "\n\n".join(users_sample)

            prompt = f"""分析群友的"炫压抑"指数（娱乐向）。炫压抑=性欲望强烈但表达受抑制的失衡状态。

用户发言样本：
{users_info}

评级标准（分数越高越压抑）：
- S级(121-150分)：想色色但欲言又止,或疯狂发涩图/开黄腔(过度补偿)。150分=极度压抑爆发，121分=明显压抑
- A级(91-120分)：经常想开车但克制扭捏。120分=频繁压抑，91分=较常压抑
- B级(61-90分)：偶尔开车,表达自然。90分=偶尔有想法，61分=基本正常
- C级(31-60分)：很少提及或表达健康。60分=偶尔提及，31分=几乎不提
- D级(0-30分)：完全回避性话题。30分=刻意回避，0分=完全无关

要求：
1. 对所有用户进行评级，评价25-30字，采用文言文风格，文雅而有趣
2. 每个用户必须给出一个0-150的精确分数(score)，用于排名
3. 分数要能区分同等级内的差异，例如同为S级，更压抑的给145分，稍轻的给125分
4. 按分数从高到低排序返回

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "name": "用户名",
    "rank": "S/A/B/C/D",
    "score": 0-150的整数分数,
    "comment": "简短评价"
  }}
]"""

            result = await self._llm(
                prompt, request_type="plugin.chat_summary.depression",
                max_tokens=_MULTI_USER_JSON_MAX_TOKENS,
            )
            if result is None:
                return []
            return self._validate_depression_index(self._parse_llm_json(result), user_stats)

        except Exception as e:
            self.logger.error(f"分析炫压抑指数失败: {e}", exc_info=True)
            return []

    # ==================== 单用户分析（LLM） ====================

    async def analyze_single_user_summary(
        self, user_messages: List[dict], user_name: str, user_id: str
    ) -> Optional[str]:
        """生成单用户 AI 总结（只使用该用户的消息）"""
        try:
            if not user_messages:
                return None

            formatted_messages = []
            for msg in user_messages:
                timestamp = msg.get("time", 0)
                time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M")
                text = msg.get("processed_plain_text") or ""
                if text:
                    formatted_messages.append(f"[{time_str}] {text}")

            if not formatted_messages:
                return None

            if len(formatted_messages) > 50:
                mid = len(formatted_messages) // 2
                sample_messages = (
                    formatted_messages[:20]
                    + formatted_messages[mid - 5 : mid + 5]
                    + formatted_messages[-20:]
                )
            else:
                sample_messages = formatted_messages

            messages_text = "\n".join(sample_messages)

            prompt = f"""请根据以下聊天记录，为用户"{user_name}"生成一段今日总结。

这是{user_name}今天在群里的发言记录：
{messages_text}

要求：
1. 总结这个用户今天聊了什么话题、表达了什么观点
2. 描述用户今天的活跃程度和情绪状态
3. 用轻松有趣的语气，像朋友聊天一样
4. 字数控制在80-150字
5. 不要使用emoji
6. 直接输出总结文本，不要加任何前缀或标题"""

            result = await self._llm(
                prompt, request_type="plugin.chat_summary.single_user_summary",
                max_tokens=_SUMMARY_MAX_TOKENS,
            )
            if result is None:
                return None
            return result.strip()

        except Exception as e:
            self.logger.error(f"生成单用户总结失败: {e}", exc_info=True)
            return None

    async def analyze_single_user_portrait(
        self, user_messages: List[dict], user_name: str, user_id: str
    ) -> Optional[Dict]:
        """生成单用户群友画像（只使用该用户的消息）"""
        try:
            if not user_messages or len(user_messages) < 3:
                return None

            samples = []
            for msg in user_messages:
                text = msg.get("processed_plain_text") or ""
                if len(text) >= 5 and len(samples) < 10:
                    samples.append(text[:80])

            if not samples:
                return None

            samples_text = "\n".join(f"- {s}" for s in samples)

            stats = self.analyze_single_user_stats(user_messages)
            avg_chars = stats["char_count"] / stats["message_count"] if stats["message_count"] else 0
            emoji_ratio = stats["emoji_count"] / stats["message_count"] if stats["message_count"] else 0
            hours = stats["hours"]
            night_messages = sum(hours[h] for h in range(0, 6))
            night_ratio = night_messages / stats["message_count"] if stats["message_count"] else 0

            prompt = f"""根据用户数据生成群友画像。

用户：{user_name}
发言数：{stats['message_count']}条
平均字数：{avg_chars:.1f}字/条
表情比例：{emoji_ratio:.2f}
夜间发言比例：{night_ratio:.2f}

发言样本：
{samples_text}

要求：
1. title: 称号（2-4个汉字），有趣且贴切
2. mbti: MBTI类型（如ENFP），基于发言特征判断
3. reason: 画像描述（60-80字），引用具体数据，有趣但不失真实

返回JSON（不要markdown代码块，不要emoji）：
{{
  "name": "{user_name}",
  "title": "称号",
  "mbti": "MBTI类型",
  "reason": "画像描述"
}}"""

            result = await self._llm(prompt, request_type="plugin.chat_summary.single_user_portrait")
            if result is None:
                return None

            data = self._parse_llm_json_object(result)
            if not data:
                return None

            return {
                "name": str(data.get("name", user_name))[:50],
                "title": str(data.get("title", ""))[: AnalysisConfig.MAX_TITLE_LENGTH],
                "mbti": str(data.get("mbti", "")).upper().strip()[:8],
                "reason": str(data.get("reason", ""))[: AnalysisConfig.MAX_REASON_LENGTH],
                "user_id": user_id,
            }

        except Exception as e:
            self.logger.error(f"生成单用户画像失败: {e}", exc_info=True)
            return None

    async def analyze_single_user_depression(
        self, user_messages: List[dict], user_name: str, user_id: str
    ) -> Optional[Dict]:
        """生成单用户炫压抑评级（只使用该用户的消息）"""
        try:
            if not user_messages or len(user_messages) < 3:
                return None

            samples = []
            for msg in user_messages:
                text = msg.get("processed_plain_text") or ""
                if len(text) >= 5 and len(samples) < 15:
                    samples.append(text[:100])

            if not samples:
                return None

            samples_text = "\n".join(f"- {s}" for s in samples)

            prompt = f"""分析用户的"炫压抑"指数（娱乐向）。炫压抑=性欲望强烈但表达受抑制的失衡状态。

用户：{user_name}
发言样本：
{samples_text}

评级标准：
- S级(121-150分)：想色色但欲言又止,或疯狂发涩图/开黄腔(过度补偿)
- A级(91-120分)：经常想开车但克制扭捏
- B级(61-90分)：偶尔开车,表达自然
- C级(31-60分)：很少提及或表达健康
- D级(0-30分)：完全回避性话题

要求：
1. 评价25-30字，采用文言文风格，文雅而有趣
2. 必须给出一个0-150的精确分数(score)

返回JSON（不要markdown代码块，不要emoji）：
{{
  "name": "{user_name}",
  "rank": "S/A/B/C/D",
  "score": 0-150的整数分数,
  "comment": "简短评价"
}}"""

            result = await self._llm(prompt, request_type="plugin.chat_summary.single_user_depression")
            if result is None:
                return None

            data = self._parse_llm_json_object(result)
            if not data:
                return None

            # 校验 rank（必须是 S/A/B/C/D，否则模板 rank-{{rank}} 的样式会丢失）
            rank = str(data.get("rank", "C")).upper().strip()
            if rank not in ("S", "A", "B", "C", "D"):
                rank = "C"
            try:
                score = int(data.get("score", 0))
                score = max(0, min(150, score))
            except (ValueError, TypeError):
                rank_default_scores = {"S": 135, "A": 105, "B": 75, "C": 45, "D": 15}
                score = rank_default_scores.get(rank, 75)

            return {
                "name": str(data.get("name", user_name))[:50],
                "rank": rank,
                "score": score,
                "comment": str(data.get("comment", ""))[:80],
                "user_id": user_id,
            }

        except Exception as e:
            self.logger.error(f"生成单用户炫压抑评级失败: {e}", exc_info=True)
            return None

    async def analyze_single_user_quotes(
        self, user_messages: List[dict], user_name: str, user_id: str
    ) -> Optional[List[Dict]]:
        """提取单用户金句（只使用该用户的消息）"""
        try:
            if not user_messages or len(user_messages) < 5:
                return None

            valid_messages = []
            for msg in user_messages:
                text = msg.get("processed_plain_text") or ""
                if 8 <= len(text) <= 100:
                    valid_messages.append(text)

            if len(valid_messages) < 5:
                return None

            if len(valid_messages) > 30:
                valid_messages = valid_messages[:30]

            messages_text = "\n".join(f"- {m}" for m in valid_messages)

            prompt = f"""从用户发言中挑选1-2条最有趣/最有深度/最搞笑的金句。

用户：{user_name}
发言列表：
{messages_text}

要求：
1. 挑选真正有趣、有梗、有深度的发言
2. 每条金句配一个简短的点评理由（10-15字）
3. 如果没有特别出彩的发言，可以只返回1条或空数组

返回JSON（不要markdown代码块，不要emoji）：
[
  {{
    "content": "金句内容",
    "reason": "点评理由"
  }}
]"""

            result = await self._llm(prompt, request_type="plugin.chat_summary.single_user_quotes")
            if result is None:
                return None

            data = self._parse_llm_json(result)
            if not data:
                return None

            quotes = []
            for item in data[:2]:
                if isinstance(item, dict) and item.get("content") and item.get("reason"):
                    quotes.append(
                        {
                            "content": str(item["content"])[:100],
                            "reason": str(item["reason"])[:30],
                            "sender": user_name,
                        }
                    )

            return quotes if quotes else None

        except Exception as e:
            self.logger.error(f"生成单用户金句失败: {e}", exc_info=True)
            return None

    # ==================== 校验函数（静态） ====================

    @staticmethod
    def _validate_topics(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if not all(key in item for key in ["topic", "contributors", "detail"]):
                continue

            topic = str(item["topic"])[:30]
            detail = str(item["detail"])[:200]
            contributors = item.get("contributors", [])
            if not isinstance(contributors, list):
                contributors = []
            contributors = [str(c).strip()[:20] for c in contributors if c and str(c).strip()][:5]

            if not topic or not detail or not contributors:
                continue

            validated.append({"topic": topic, "contributors": contributors, "detail": detail})

        return validated[:5]

    @staticmethod
    def _validate_titles(
        data: List[Dict[str, Any]], user_stats: Optional[Dict[str, Dict]] = None
    ) -> List[Dict[str, Any]]:
        valid_mbti_types = {
            "INTJ", "INTP", "ENTJ", "ENTP",
            "INFJ", "INFP", "ENFJ", "ENFP",
            "ISTJ", "ISFJ", "ESTJ", "ESFJ",
            "ISTP", "ISFP", "ESTP", "ESFP",
        }

        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if not all(key in item for key in ["name", "title", "mbti", "reason"]):
                continue

            name = str(item["name"])[:50]
            title = str(item["title"])[: AnalysisConfig.MAX_TITLE_LENGTH]
            mbti = str(item["mbti"]).upper().strip()
            reason = str(item["reason"])[: AnalysisConfig.MAX_REASON_LENGTH]

            if mbti not in valid_mbti_types:
                mbti = "ENFP"

            if not name or not title or not reason:
                continue

            user_id = AnalysisService._match_user_id(name, user_stats)

            validated.append(
                {"name": name, "title": title, "mbti": mbti, "reason": reason, "user_id": user_id}
            )

        return validated

    @staticmethod
    def _match_user_id(name: str, user_stats: Optional[Dict[str, Dict]]) -> str:
        """把 LLM 回传的名字匹配回 user_id（用于头像）。精确→去空白→互相包含，逐级放宽。"""
        if not user_stats or not name:
            return ""
        target = str(name).strip()
        # 1) 精确匹配
        for uid, stats in user_stats.items():
            if stats.get("nickname") == name:
                return uid
        # 2) 去空白后匹配
        for uid, stats in user_stats.items():
            if str(stats.get("nickname") or "").strip() == target:
                return uid
        # 3) 互相包含（LLM 截断/加书名号等）
        for uid, stats in user_stats.items():
            nick = str(stats.get("nickname") or "").strip()
            if nick and (nick in target or target in nick):
                return uid
        return ""

    @staticmethod
    def _validate_quotes(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if not all(key in item for key in ["content", "sender", "reason"]):
                continue

            content = str(item["content"])[:200]
            sender = str(item["sender"])[:50]
            reason = str(item["reason"])[: AnalysisConfig.MAX_REASON_LENGTH]

            content = re.sub(r"@[^<\s]+<\d+>\s*", "", content).strip()

            if not content or not sender or not reason:
                continue

            validated.append({"content": content, "sender": sender, "reason": reason})

        return validated

    @staticmethod
    def _validate_depression_index(
        data: List[Dict[str, Any]], user_stats: Optional[Dict[str, Dict]] = None
    ) -> List[Dict[str, Any]]:
        validated = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if not all(key in item for key in ["name", "rank", "comment"]):
                continue

            name = str(item["name"])[:50]
            rank = str(item["rank"]).upper().strip()
            comment = str(item["comment"])[:60]

            if rank not in ["S", "A", "B", "C", "D"]:
                continue
            if not name or not rank or not comment:
                continue

            try:
                score = int(item.get("score", 0))
                score = max(0, min(150, score))
            except (ValueError, TypeError):
                rank_default_scores = {"S": 135, "A": 105, "B": 75, "C": 45, "D": 15}
                score = rank_default_scores.get(rank, 75)

            user_id = ""
            if user_stats:
                for uid, stats in user_stats.items():
                    if stats.get("nickname") == name:
                        user_id = uid
                        break

            validated.append(
                {"name": name, "rank": rank, "score": score, "comment": comment, "user_id": user_id}
            )

        validated.sort(key=lambda x: x["score"], reverse=True)
        return validated

    # ==================== LLM JSON 解析（静态） ====================

    @classmethod
    def _parse_llm_json_object(cls, result: str) -> Optional[Dict[str, Any]]:
        """解析 LLM 返回的 JSON 对象（非数组）"""
        try:
            result = result.strip()
            if result.startswith("```"):
                parts = result.split("```")
                if len(parts) >= 2:
                    result = parts[1]
                    if result.startswith("json"):
                        result = result[4:]
            result = result.strip()

            start_idx = result.find("{")
            end_idx = result.rfind("}")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                result = result[start_idx : end_idx + 1]

            data = json.loads(result)
            if not isinstance(data, dict):
                return None
            return data

        except json.JSONDecodeError:
            try:
                result_cleaned = cls.EMOJI_PATTERN.sub("", result)
                start_idx = result_cleaned.find("{")
                end_idx = result_cleaned.rfind("}")
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    result_cleaned = result_cleaned[start_idx : end_idx + 1]
                result_cleaned = re.sub(
                    r"([一-鿿])\s+([一-鿿])", r"\1\2", result_cleaned
                )
                data = json.loads(result_cleaned)
                return data if isinstance(data, dict) else None
            except Exception:
                return None
        except Exception:
            return None

    @classmethod
    def _parse_llm_json(cls, result: str) -> List[Dict[str, Any]]:
        """解析 LLM 返回的 JSON 数组（带 emoji 清理与截断修复 fallback）"""
        result_cleaned = ""
        try:
            result = result.strip()
            if result.startswith("```"):
                parts = result.split("```")
                if len(parts) >= 2:
                    result = parts[1]
                    if result.startswith("json"):
                        result = result[4:]
            result = result.strip()

            start_idx = result.find("[")
            end_idx = result.rfind("]")
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                result = result[start_idx : end_idx + 1]

            data = json.loads(result)
            if not isinstance(data, list):
                return []
            if data and not all(isinstance(item, dict) for item in data):
                return []
            return data

        except json.JSONDecodeError:
            try:
                result_cleaned = cls.EMOJI_PATTERN.sub("", result)
                start_idx = result_cleaned.find("[")
                end_idx = result_cleaned.rfind("]")
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    result_cleaned = result_cleaned[start_idx : end_idx + 1]
                elif start_idx != -1:
                    result_cleaned = result_cleaned[start_idx:]

                result_cleaned = re.sub(
                    r"([一-鿿])\s+([一-鿿])", r"\1\2", result_cleaned
                )
                result_cleaned = re.sub(r"([一-鿿])\s+([\d])", r"\1\2", result_cleaned)
                result_cleaned = re.sub(r"([\d])\s+([一-鿿])", r"\1\2", result_cleaned)

                try:
                    data = json.loads(result_cleaned)
                except json.JSONDecodeError:
                    result_fixed = cls._fix_truncated_json_array(result_cleaned)
                    if result_fixed:
                        data = json.loads(result_fixed)
                    else:
                        raise

                if not isinstance(data, list):
                    return []
                if data and not all(isinstance(item, dict) for item in data):
                    return []
                return data
            except Exception:
                return []
        except Exception:
            return []

    @staticmethod
    def _fix_truncated_json_array(json_str: str) -> Optional[str]:
        """尝试修复被截断的 JSON 数组"""
        try:
            brace_positions = []
            in_string = False
            escape_next = False

            for i, char in enumerate(json_str):
                if escape_next:
                    escape_next = False
                    continue
                if char == "\\":
                    escape_next = True
                    continue
                if char == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if char == "}" and not in_string:
                    brace_positions.append(i)

            for pos in reversed(brace_positions):
                candidate = json_str[: pos + 1].rstrip()
                if candidate.endswith(","):
                    candidate = candidate[:-1]
                candidate = candidate + "\n]"
                try:
                    data = json.loads(candidate)
                    if isinstance(data, list) and len(data) > 0:
                        return candidate
                except json.JSONDecodeError:
                    continue

            return None
        except Exception:
            return None
