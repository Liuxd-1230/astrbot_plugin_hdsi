"""Layered colored task-timeline logging. Port of src/logging.ts.

Keeps kaomoji actions, tree fields, dark/light palettes and phase labels so
operator-facing output stays recognizable after the AstrBot migration.
"""

from __future__ import annotations

import re
from typing import Any, Optional

KAOMOJI = {
    "receive": "(*^▽^*)",
    "send": "(・ω・)ノ",
    "processing": "(•̀ᴗ•́)و",
    "complete": "(ﾉ´ヮ`)ﾉ*: ･ﾟ",
    "trigger": "(๑•̀ㅂ•́)و✧",
    "emotion": "(*>ω<*)",
    "memory": "₍ᐢ- ˕ -ᐢ₎zzZ",
    "advance": "(⊙ω⊙)",
    "agency": "ᕙ( •̀ ᗜ •́ )ᕗ",
    "group": "(´▽｀)ノ",
    "error": "(˶ˊᜊˋ˶)",
    "retry": "(ง •̀_•́)ง",
    "warning": "(´･_･`)",
    "waiting": "(っ˘ω˘ς )",
    "system": "(^_^)/",
}

SYMBOLS = {
    "receive": "←", "send": "→", "processing": "⋯", "complete": "✓", "trigger": "⚡",
    "emotion": "★", "memory": "◈", "advance": "⟳", "agency": "◇", "group": "◎",
    "error": "✗", "retry": "↻", "warning": "!", "waiting": "…", "system": "•",
}

FIELD_LABELS = {
    "任务": "任务", "模型": "模型", "参与者": "参与者", "时间段": "时间段", "到期计划": "到期计划",
    "耗时": "耗时", "剧本文字": "剧本文字", "回复模式": "回复模式", "成功": "成功", "可见消息": "可见消息",
    "合并消息": "合并消息", "数量": "数量", "数值": "数值", "累计": "累计", "阈值": "阈值", "方向": "方向",
    "强度": "强度", "描述": "描述", "权重": "权重", "错误": "错误", "群聊": "群聊", "发送者": "发送者",
    "模式": "模式", "条目": "条目", "字符": "字符", "长期事实": "长期事实", "状态变更": "状态变更",
    "时间": "时间", "间隔": "间隔", "等待": "等待", "已投递": "已投递", "原因": "原因", "请求": "请求",
    "故事": "故事", "平台": "平台", "用户": "用户", "机器人": "机器人",
}

COLOR_PALETTES: dict[str, dict[str, int]] = {
    "dark": {
        "protagonist": 159, "detail": 250, "body": 255, "user": 81, "success": 114,
        "alter": 219, "memory": 111, "warning": 222, "error": 210,
    },
    "light": {
        "protagonist": 24, "detail": 240, "body": 236, "user": 25, "success": 28,
        "alter": 90, "memory": 25, "warning": 130, "error": 160,
    },
}

PHASE_LABELS = {
    "user-message": "用户消息",
    "conversation-follow-up": "对话后续",
    "advance": "自动推进",
    "intent-due": "到期意图",
}


def phase_label(phase: Optional[str]) -> str:
    if not phase:
        return "系统"
    return PHASE_LABELS.get(phase, "系统")


def detect_log_action(message: str, level: str) -> str:
    if level == "error":
        return "error"
    if re.search(r"重试|再次尝试", message):
        return "retry"
    if re.search(r"模型调用失败|主叙事失败|消息投递失败", message):
        return "error"
    if level == "warn" or re.search(r"警告|拦截|不可用|失败", message):
        return "warning"
    if re.search(r"Alter.*(?:触发|超过阈值)|累积触发", message):
        return "trigger"
    if re.search(r"(?:模型调用|情绪偏移生成|记忆整理|后台扫描|剧本推进).*完成", message):
        return "complete"
    if re.search(r"情绪偏移|Alter", message):
        return "emotion"
    if re.search(r"Agency|主动联系判断|主动联系重查", message):
        return "agency"
    if re.search(r"记忆|压缩|Overlay", message):
        return "memory"
    if re.search(r"群消息|群聊|群发言", message):
        return "group"
    if re.search(r"投递|发送", message):
        return "send"
    if re.search(r"收到|接收|入队", message):
        return "receive"
    if re.search(r"模型调用开始|分析开始|读取开始|整理开始", message):
        return "processing"
    if re.search(r"完成|成功|已就绪|已启动", message):
        return "complete"
    if re.search(r"推进|后台扫描", message):
        return "advance"
    if re.search(r"等待|计时器|排队", message):
        return "waiting"
    return "system"


_FIELD_PATTERN = re.compile(
    r"(?:^|\s)([\w\u4e00-\u9fff-]+)=([^=]*?)(?=\s+[\w\u4e00-\u9fff-]+=|$)"
)


def extract_fields(text: str) -> tuple[str, list[dict[str, str]]]:
    if "\n" in text:
        return text, []
    fields: list[dict[str, str]] = []
    first = -1
    for match in _FIELD_PATTERN.finditer(text):
        if first < 0:
            first = match.start()
        label = match.group(1)
        value = match.group(2).strip()
        if not value:
            continue
        fields.append({"label": FIELD_LABELS.get(label, label), "value": value})
    summary = text[:first].strip().rstrip("：:,，") if first >= 0 else text
    return summary, fields


def is_root_log(summary: str, action: str, level: str, standalone: bool) -> bool:
    if standalone or level == "error" or action == "error":
        return True
    if action == "trigger":
        return True
    if action == "memory" and "开始" in summary:
        return True
    if action == "advance" and re.search(r"(?:开始|即将执行)", summary):
        return True
    if action == "receive" and re.search(r"(?:收到|接收)", summary):
        return True
    if action == "group" and "收到" in summary:
        return True
    return False


def is_final_branch(summary: str, action: str) -> bool:
    if action == "send":
        return True
    if action == "complete" and "模型调用完成" not in summary:
        return True
    return bool(re.search(r"写作回合完成|扫描完成|整理完成|已注入", summary))


def log_category(action: str, phase: Optional[str], standalone: bool, message: str) -> str:
    if action in ("trigger", "emotion") or re.search(r"Alter|情绪偏移", message):
        return "[情绪追踪]"
    if action == "agency" or "Agency" in message:
        return "[主体节奏]"
    if action == "memory" or re.search(r"记忆|压缩|Overlay", message):
        return "[记忆整理]"
    if action == "group" or re.search(r"群聊|群消息", message):
        return "[群聊]"
    if action == "retry":
        return "[自动重试]"
    if standalone:
        return "[系统]"
    return f"[{phase_label(phase)}]"


def category_color(action: str, phase: Optional[str], message: str, palette: dict[str, int]) -> int:
    if action == "error":
        return palette["error"]
    if action in ("warning", "retry"):
        return palette["warning"]
    if action in ("trigger", "emotion") or re.search(r"Alter|情绪偏移", message):
        return palette["alter"]
    if action == "agency" or "Agency" in message:
        return palette["user"]
    if action == "memory" or re.search(r"记忆|压缩|Overlay", message):
        return palette["memory"]
    if action == "complete":
        return palette["success"]
    if phase == "advance":
        return palette["memory"]
    return palette["user"]


def action_color(action: str, palette: dict[str, int]) -> int:
    if action == "error":
        return palette["error"]
    if action in ("warning", "retry"):
        return palette["warning"]
    if action in ("complete", "send"):
        return palette["success"]
    if action in ("trigger", "emotion"):
        return palette["alter"]
    if action in ("memory", "advance"):
        return palette["memory"]
    if action == "agency":
        return palette["user"]
    return palette["user"]


def summary_color(action: str, level: str, palette: dict[str, int]) -> int:
    if level == "error":
        return palette["error"]
    if level == "warn":
        return palette["warning"]
    if action == "complete":
        return palette["success"]
    return palette["body"]


def paint(value: str, code: int, enabled: bool = True) -> str:
    if not enabled or not value:
        return value
    basic_ansi = 30 <= code <= 37 or 90 <= code <= 97
    sequence = str(code) if basic_ansi else f"38;5;{code}"
    return f"\u001b[{sequence}m{value}\u001b[0m"


def render_log_message(message: str, args: tuple[Any, ...] | list[Any] = ()) -> str:
    if not args:
        return message
    try:
        rendered_args = [
            item if isinstance(item, Exception) else item for item in args
        ]
        return message % tuple(rendered_args)
    except (TypeError, ValueError):
        parts = [message]
        for arg in args:
            parts.append(str(arg))
        return " ".join(parts)


class LayeredLogInput:
    __slots__ = (
        "level", "phase", "protagonist", "message", "args", "colors",
        "color_theme", "kaomoji", "standalone",
    )

    def __init__(
        self,
        level: str,
        message: str,
        args: tuple[Any, ...] | list[Any] = (),
        phase: Optional[str] = None,
        protagonist: Optional[str] = None,
        colors: bool = True,
        color_theme: str = "dark",
        kaomoji: bool = True,
        standalone: bool = False,
    ) -> None:
        self.level = level
        self.message = message
        self.args = tuple(args)
        self.phase = phase
        self.protagonist = protagonist
        self.colors = colors
        self.color_theme = color_theme
        self.kaomoji = kaomoji
        self.standalone = standalone


def format_layered_log(input_: LayeredLogInput) -> str:
    text = render_log_message(input_.message, input_.args)
    action = detect_log_action(text, input_.level)
    summary_from_fields, fields = extract_fields(text)
    summary = summary_from_fields or text
    root = is_root_log(summary, action, input_.level, input_.standalone)
    branch = "" if root else ("└─" if is_final_branch(summary, action) else "├─")
    category = log_category(action, input_.phase, input_.standalone, text)
    face = SYMBOLS[action] if input_.kaomoji is False else KAOMOJI[action]
    palette = COLOR_PALETTES.get(input_.color_theme, COLOR_PALETTES["dark"])
    header = (
        f"{paint(category, category_color(action, input_.phase, text, palette), input_.colors)} "
        f"{paint(input_.protagonist or 'HDSI', palette['protagonist'], input_.colors)}"
        if root else branch
    )
    main = (
        f"{header}{' ' if header else ''}{face} "
        f"{paint(summary, summary_color(action, input_.level, palette), input_.colors)}"
    ).rstrip()
    if not fields:
        return main
    lines = []
    for index, field in enumerate(fields):
        connector = "└─" if index == len(fields) - 1 else "├─"
        indent = "" if root else "   "
        lines.append(
            f"{indent}{connector} {paint(field['label'] + ':', palette['detail'], input_.colors)} {field['value']}"
        )
    return "\n".join([main, *lines])
