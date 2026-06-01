"""
每日分析插件常量配置

集中管理分析逻辑相关的可调参数。图片样式相关的颜色/布局已迁移到
HTML/CSS 模板（templates/scrapbook/），不再需要旧 PIL 版本的配色与布局常量。
"""


class AnalysisConfig:
    """分析参数配置"""

    # 用户称号 / 画像分析
    MIN_MESSAGES_FOR_TITLE: int = 5  # 最少发言数才参与称号/画像分析
    MAX_USERS_FOR_TITLE: int = 8     # 群友画像最多分析用户数

    # 金句提取
    MIN_QUOTE_LENGTH: int = 5        # 金句最小长度
    MAX_QUOTE_LENGTH: int = 100      # 金句最大长度
    MIN_QUOTES: int = 3              # 最少金句数
    MAX_QUOTES: int = 5              # 最多金句数

    # LLM 返回内容长度校验
    MAX_REASON_LENGTH: int = 200     # 理由最大长度（容纳 80 字 + 标点）
    MAX_TITLE_LENGTH: int = 10       # 称号最大长度

    # 炫压抑评级展示配置（默认值，可被 config.toml 覆盖）
    MAX_DEPRESSION_DISPLAY: int = 6          # 炫压抑评级最多展示人数
    DEPRESSION_SHOW_BOTTOM_HALF: bool = True  # 是否展示倒数排名
