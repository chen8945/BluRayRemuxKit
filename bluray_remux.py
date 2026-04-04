#!/usr/bin/env python3
"""
Blu-ray Remux Script
--------------------
批量将蓝光原盘（BDMV）或 ISO Remux 为 MKV。

主要能力：
- 自动识别正片、章节、音轨和字幕
- 整合 BDInfo 信息并支持交互式轨道编辑
- 支持批量处理和跨平台 ISO 挂载

详细参数、使用示例和 BDInfo 输入方式请见 README.md。

依赖：
- mkvmerge (MKVToolNix)
- ffprobe (FFmpeg)
- Python 包：rich, pycountry
- 可选增强输入：prompt_toolkit
"""
import os
import re
import sys
import json
import time
import copy
import shutil
import tempfile
import subprocess
import argparse
import shlex
import mimetypes
import unicodedata
import pycountry
import xml.etree.ElementTree as ET
from functools import lru_cache
from pathlib import Path
from typing import Callable, List, Dict, Literal, Optional, Tuple
from struct import unpack
from rich.console import Console
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import InMemoryHistory
except ImportError:
    PromptSession = None
    AutoSuggestFromHistory = None
    InMemoryHistory = None

# ==============================================================================
# 全局配置
# ==============================================================================

# 调试模式（设置为 True 启用调试输出）
DEBUG = False
_PROMPT_SESSION = None

# fmt: off
# 工具路径配置（优先级：自定义路径 > 脚本目录 > 系统 PATH）
CUSTOM_PATHS = {
    "mkvmerge": "",
    "ffprobe": "",
}

# 音轨编码权重（用于轨道排序）
CODEC_WEIGHTS = {
    "truehd_atmos": 7,
    "dts_x": 7,
    "dts_hd_ma": 6,
    "truehd": 6,
    "dts": 5,
    "lpcm": 4,
    "eac3": 3,
    "ac3": 2,
    "aac": 1
}

# 简繁中文智能识别配置（pycountry 不支持的自定义标签）
LANGUAGE_VARIANTS = {
    "zh-Hans": {
        "display_name": "简体中文",
        "keywords": ["简体", "简中", "简英", "国配", "中字"]
    },
    "zh-Hant": {
        "display_name": "繁体中文",
        "keywords": ["繁体", "繁中", "繁英", "港繁", "台繁", "粤语", "粤繁", "国配繁"]
    }
}

# 语言显示/字幕简称元数据（统一维护，别名交由 pycountry 归一）
LANGUAGE_LABELS = {
    "en": {"display_name": "英语", "subtitle_short": "英"},
    "ja": {"display_name": "日语", "subtitle_short": "日"},
    "ko": {"display_name": "韩语", "subtitle_short": "韩"},
    "fr": {"display_name": "法语", "subtitle_short": "法"},
    "de": {"display_name": "德语", "subtitle_short": "德"},
    "es": {"display_name": "西班牙语", "subtitle_short": "西"},
    "it": {"display_name": "意大利语", "subtitle_short": "意"},
    "ru": {"display_name": "俄语", "subtitle_short": "俄"},
    "pt": {"display_name": "葡萄牙语", "subtitle_short": "葡"},
    "th": {"display_name": "泰语"},
    "hu": {"display_name": "匈牙利语"},
    "pl": {"display_name": "波兰语"},
    "tr": {"display_name": "土耳其语"},
}

# pycountry 不支持或不适合直接显示的特殊名称
SPECIAL_LANGUAGE_NAMES = {
    "zh-Hans": "简体中文",
    "zh-Hant": "繁体中文",
    "zho": "国语",  # 通用中文（未区分简繁）
    "chi": "国语",  # 通用中文（未区分简繁）
    "und": "未知",
}

# 中文轨道描述关键词配置（音频+字幕通用）
# 用途说明：
# 1. 音频轨道智能命名：解析 BDInfo 自定义描述，生成规范化的轨道名
# 2. 音频地区权重排序：同语言、同编码时按 region 列表顺序排序
# 3. 字幕地区权重排序：同类型字幕按 region 列表顺序排序
TRACK_KEYWORDS = {
    "region": ["八一公映", "公映", "央视长译", "央视", "长译", "东影上译", "东影", "上译", "北影", "中影", "华纳", "六区", "东森", "新索"],  # 地区/版本标识（列表顺序 = 排序优先级）
    "mandarin": ["国语", "国配"],                     # 国语标识
    "dialect": ["台配", "粤配", "粤语", "港配"],        # 方言标识
    "commentary": ["导评", "评论", "commentary"]      # 导评标识
}

# fmt: on

# 非法文件名字符替换（Windows 兼容）
ILLEGAL_CHAR_MAP = {"?": "？", "*": "★", "<": "《", ">": "》", ":": "：", '"': "'", "/": "／", "\\": "／", "|": "￨"}

# 数值常量
MIN_FEATURE_DURATION = 1800  # 最小正片时长（秒），30分钟

# 正则表达式常量（用于 BDInfo 解析）
AUDIO_LANGUAGES_PATTERN = r"(English|French|German|Japanese|Chinese|Korean|Czech|Spanish|Italian|Russian|Portuguese)"
SUBTITLE_LANGUAGES_PATTERN = r"(English|Japanese|Chinese|Korean|French|German|Spanish|Italian|Russian|Portuguese|Thai|Vietnamese|Czech)"
BITRATE_KBPS_PATTERN = r"([\d,]+\.?\d*)\s*kbps?"  # 支持小数（如字幕码率：26.685 kbps），容错 kbp/kbps
BITRATE_MBPS_PATTERN = r"([\d.]+)\s*Mbps"
SDH_PATTERN = re.compile(r"(?i)\s*\(?(SDH|CC|听障|聋哑)\)?")
BDINFO_PASTE_SENTINEL = "EOF"


# ==============================================================================
# 语言代码转换函数（基于 pycountry）
# ==============================================================================


def _build_subtitle_text_markers() -> List[Tuple[str, str]]:
    """根据统一语言元数据生成字幕文本识别关键字。"""
    markers = []
    for meta in LANGUAGE_LABELS.values():
        short = meta.get("subtitle_short")
        if not short:
            continue
        markers.append((f"{short}文", short))
        markers.append((short, short))
    return markers


def _build_subtitle_shorts() -> List[str]:
    """根据统一语言元数据生成可复用的字幕简称列表。"""
    shorts = {meta["subtitle_short"] for meta in LANGUAGE_LABELS.values() if meta.get("subtitle_short")}
    return sorted(shorts, key=len, reverse=True)


SUBTITLE_TEXT_MARKERS = _build_subtitle_text_markers()
SUBTITLE_SHORTS = _build_subtitle_shorts()


@lru_cache(maxsize=256)
def _lookup_language(lang_code: str) -> Optional[object]:
    """
    使用 pycountry 查找语言对象（内部工具函数）

    优先使用 lookup() 进行模糊查找，支持 alpha_2/alpha_3/name。
    使用 LRU 缓存提高性能。

    Args:
        lang_code: 语言代码（如 "eng", "en", "jpn", "chi"）

    Returns:
        pycountry Language 对象，未找到时返回 None

    Examples:
        >>> _lookup_language("eng").alpha_2
        "en"
        >>> _lookup_language("en").alpha_3
        "eng"
        >>> _lookup_language("chi").alpha_3
        "zho"
    """
    try:
        # 使用 lookup() 进行模糊查找（支持 alpha_2/alpha_3/bibliographic）
        return pycountry.languages.lookup(lang_code)
    except LookupError:
        # pycountry 无法识别的语言，返回 None
        return None
    except Exception:
        # 其他异常（如 pycountry 未安装）
        return None


def _is_chinese_variant(lang_code: str) -> bool:
    """
    判断是否为中文变体标签（zh-Hans/zh-Hant）

    Args:
        lang_code: 语言代码

    Returns:
        如果是自定义中文变体返回 True
    """
    return lang_code in LANGUAGE_VARIANTS


def normalize_language_code(lang_code: str, target_format: str = "alpha_2") -> str:
    """
    使用 pycountry 将语言代码规范化为指定格式

    优先级：
    1. 中文变体（zh-Hans/zh-Hant）直接返回
    2. pycountry 查找（ISO 639-1/639-2/639-3）
    3. 原始代码（无法识别时返回原值）

    Args:
        lang_code: 输入语言代码（如 "eng", "en", "jpn", "zh-Hans"）
        target_format: 目标格式 ("alpha_2" | "alpha_3" | "keep_chinese")
            - "alpha_2": 返回 ISO 639-1（2字母，如 en, ja）
            - "alpha_3": 返回 ISO 639-2（3字母，如 eng, jpn）
            - "keep_chinese": 保持中文变体标签，其他转为 alpha_2

    Returns:
        规范化后的语言代码

    Examples:
        >>> normalize_language_code("eng", "alpha_2")
        "en"
        >>> normalize_language_code("zh-Hans", "keep_chinese")
        "zh-Hans"
        >>> normalize_language_code("jpn", "alpha_2")
        "ja"
    """
    # 1. 中文变体：直接返回
    if _is_chinese_variant(lang_code):
        if target_format == "keep_chinese":
            return lang_code
        elif target_format == "alpha_2":
            return "zh"  # 标准 ISO 639-1
        else:
            return lang_code  # 保持原值

    # 2. 使用统一的 pycountry 查找工具
    lang = _lookup_language(lang_code)
    if lang:
        if target_format == "alpha_2" or target_format == "keep_chinese":
            return lang.alpha_2 if hasattr(lang, "alpha_2") else lang_code
        elif target_format == "alpha_3":
            return lang.alpha_3

    # 3. 回退：返回原始代码
    return lang_code


def get_language_tag(lang_code: str, track_type: str = "subtitle") -> str:
    """
    获取语言标签（用于 mkvmerge 或表格显示）

    统一处理逻辑：
    1. 智能识别的简繁标签（zh-Hans/zh-Hant）：
       - subtitle: 保持原值
       - video/audio: 转为 zh（mkvmerge 兼容性）
    2. 其他语言：使用 pycountry 转换为 BCP 47（ISO 639-1, 2字母）

    Args:
        lang_code: 语言代码
        track_type: 轨道类型 ("video" | "audio" | "subtitle")

    Returns:
        语言标签

    Examples:
        >>> get_language_tag("zh-Hans", "subtitle")
        "zh-Hans"
        >>> get_language_tag("zh-Hans", "audio")
        "zh"
        >>> get_language_tag("eng", "audio")
        "en"
    """
    # 1. 智能识别的简繁体标签
    if _is_chinese_variant(lang_code):
        # 字幕：保持原值（zh-Hans/zh-Hant）
        if track_type == "subtitle":
            return lang_code
        # 视频/音频：转为标准 zh
        else:
            return "zh"

    # 2. 其他语言：使用统一工具转换为 BCP 47（ISO 639-1）
    return normalize_language_code(lang_code, "alpha_2")


def bdinfo_language_to_code(bdinfo_lang: str) -> str:
    """
    将 BDInfo 的英文语言名称转换为 ISO 639-2 代码

    使用 pycountry 模糊查找（通过 name 属性）

    Args:
        bdinfo_lang: BDInfo 中的语言名称（如 "English", "Japanese", "Chinese"）

    Returns:
        ISO 639-2 语言代码（如 "eng", "jpn", "chi"）

    Examples:
        >>> bdinfo_language_to_code("English")
        "eng"
        >>> bdinfo_language_to_code("Japanese")
        "jpn"
    """
    if not bdinfo_lang:
        return "und"

    lang_lower = bdinfo_lang.lower().strip()

    # 使用 pycountry 模糊查找
    try:
        # pycountry.languages.lookup() 支持通过 name 查找（不区分大小写）
        lang = pycountry.languages.lookup(lang_lower)
        if lang:
            return lang.alpha_3  # 返回 ISO 639-2 (3字母)
    except LookupError:
        # pycountry 无法识别的语言，返回小写的原始输入
        pass

    # 返回原始输入（小写）
    return lang_lower


# ==============================================================================
# 工具函数
# ==============================================================================


def debug_print(message: str, console: Optional[Console] = None) -> None:
    """
    调试信息输出（仅在 DEBUG=True 时输出）
    """
    if DEBUG:
        output_console = console if console else Console()
        output_console.print(message)


def _clean_channels_str(raw: str) -> str:
    """
    清理 BDInfo 声道字符串：去除 objects/Atmos/X 标识，保留基础声道描述

    BDInfo 的声道字段可能包含附加信息，例如 "7.1+6 objects-Atmos" 或 "5.1-X"。
    此函数去除这些附加标识，只保留基础声道数（如 "7.1"、"5.1"）。

    Args:
        raw: BDInfo 原始声道字符串（如 "7.1+6 objects-Atmos"、"5.1-X"、"2.0"）

    Returns:
        清理后的声道描述（如 "7.1"、"5.1"、"2.0"）；输入为空时返回空字符串

    Examples:
        >>> _clean_channels_str("7.1+6 objects-Atmos")
        "7.1"
        >>> _clean_channels_str("5.1-X")
        "5.1"
        >>> _clean_channels_str("2.0")
        "2.0"
        >>> _clean_channels_str("")
        ""
    """
    if not raw:
        return ""
    base = raw.split("+")[0].strip() if "+" in raw else raw.strip()
    return base.replace("-Atmos", "").replace("-X", "")


def _parse_tri_state(value: str, true_val: str, ask_val: str = "ask") -> Optional[bool]:
    """
    将 CLI 字符串参数解析为三态值：True / None(ask) / False

    用于将命令行或交互式输入的字符串选项统一转换为程序内部使用的三态布尔值。

    Args:
        value: 用户输入的字符串（如 "drop", "keep", "ask", "yes", "no"）
        true_val: 对应 True 的字符串值（如 "drop" 或 "yes"）
        ask_val: 对应 None（逐盘询问）的字符串值，默认为 "ask"

    Returns:
        True: value == true_val
        None: value == ask_val（表示需要逐盘询问）
        False: 其他情况

    Examples:
        >>> _parse_tri_state("drop", true_val="drop")
        True
        >>> _parse_tri_state("ask", true_val="drop")
        None
        >>> _parse_tri_state("keep", true_val="drop")
        False
        >>> _parse_tri_state("yes", true_val="yes")
        True
    """
    if value == true_val:
        return True
    if value == ask_val:
        return None
    return False


def clean_path(path_str: str) -> str:
    """
    清理路径字符串：移除末尾的引号和多余的路径分隔符

    解决 Windows 命令行中 'path\' 导致的引号转义问题。

    Args:
        path_str: 原始路径字符串

    Returns:
        清理后的路径字符串

    Examples:
        'G:\\Movie\\BDMV"' -> 'G:\\Movie\\BDMV'
        'G:\\Movie\\BDMV\\' -> 'G:\\Movie\\BDMV'
        'G:/Movie/BDMV/' -> 'G:/Movie/BDMV'
    """
    # 移除两端空格和引号，然后使用 pathlib 规范化路径
    cleaned = path_str.strip().strip("\"'").rstrip("\\/")
    return str(Path(cleaned))


def get_language_display_name(lang_code: str, fallback_to_english: bool = False) -> str:
    """
    获取语言的显示名称（优先中文，可选英文回退）

    优先级：
    1. 自定义特殊名称（SPECIAL_LANGUAGE_NAMES）
    2. 统一语言元数据（LANGUAGE_LABELS）
    3. pycountry 英文名称（fallback_to_english=True 时）
    4. 原始代码

    Args:
        lang_code: 语言代码
        fallback_to_english: 是否回退到英文名称（使用 pycountry）

    Returns:
        显示名称（中文优先，可选英文）

    Examples:
        >>> get_language_display_name("zh-Hans")
        "简体中文"
        >>> get_language_display_name("eng")
        "英语"
        >>> get_language_display_name("swa", fallback_to_english=True)
        "Swahili"
    """
    # 1. 优先使用特殊名称
    if lang_code in SPECIAL_LANGUAGE_NAMES:
        return SPECIAL_LANGUAGE_NAMES[lang_code]

    normalized_display_code = normalize_language_code(lang_code, "keep_chinese")
    if normalized_display_code in SPECIAL_LANGUAGE_NAMES:
        return SPECIAL_LANGUAGE_NAMES[normalized_display_code]

    normalized_code = normalize_language_code(lang_code, "alpha_2").lower()

    # 2. 统一语言元数据
    meta = LANGUAGE_LABELS.get(normalized_code)
    if meta:
        return meta["display_name"]

    # 3. 尝试使用 pycountry（英文名称）
    if fallback_to_english:
        lang = _lookup_language(normalized_code)

        if not lang and normalized_code != lang_code:
            # 尝试原始代码
            lang = _lookup_language(lang_code)

        if lang:
            return lang.name  # 返回英文名称

    # 4. 返回原始代码
    return lang_code


def get_subtitle_language_short(lang_code: str) -> str:
    """
    根据语言代码推导字幕双语简称

    例如：
    - eng -> 英
    - jpn -> 日
    - fra -> 法

    无法推导时返回空字符串。
    """
    if not lang_code or lang_code in ["und", "zho", "chi", "zh", "zh-Hans", "zh-Hant"]:
        return ""

    normalized_code = normalize_language_code(lang_code, "alpha_2").lower()

    # 1. 优先按标准语言代码映射
    meta = LANGUAGE_LABELS.get(normalized_code)
    if meta and meta.get("subtitle_short"):
        return meta["subtitle_short"]

    # 2. 尝试从中文显示名提取简称
    display_name = get_language_display_name(lang_code, fallback_to_english=False)
    for short in SUBTITLE_SHORTS:
        if short in display_name:
            return short

    return ""


def find_executable(tool_name: str) -> Optional[str]:
    """
    查找可执行文件路径
    优先级：自定义路径 > 脚本目录 > 系统 PATH
    """
    # 1. 检查自定义路径
    if CUSTOM_PATHS.get(tool_name):
        custom_path = CUSTOM_PATHS[tool_name]
        if Path(custom_path).is_file():
            return custom_path

    # 2. 检查脚本目录
    script_dir = Path(__file__).parent
    for ext in ["", ".exe"]:
        local_path = script_dir / f"{tool_name}{ext}"
        if local_path.is_file():
            return str(local_path)

    # 3. 检查系统 PATH
    return shutil.which(tool_name)


def check_tools() -> None:
    """检查必需工具是否可用"""
    console = Console()

    required = {
        "mkvmerge": "mkvmerge",
        "ffprobe": "ffprobe",
    }
    version_args = {
        "mkvmerge": ["--version"],
        "ffprobe": ["-version"],
    }

    missing_required = []
    no_permission_required = []
    broken_required = []

    for tool, name in required.items():
        tool_path = find_executable(tool)
        if not tool_path:
            missing_required.append(name)
            continue

        if not sys.platform.startswith("win") and not os.access(tool_path, os.X_OK):
            no_permission_required.append(tool_path)
            continue

        try:
            result = subprocess.run(
                [tool_path, *version_args[tool]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            tool_status = "ok" if result.returncode == 0 else "broken"
        except PermissionError:
            tool_status = "permission"
        except (OSError, subprocess.SubprocessError):
            tool_status = "broken"

        if tool_status == "permission":
            no_permission_required.append(tool_path)
        elif tool_status == "broken":
            broken_required.append(name)

    if missing_required:
        console.print(f"[red]错误：缺少必需工具 - {', '.join(missing_required)}[/red]")
        console.print("请确保对应可执行文件已安装，并且位于 CUSTOM_PATHS、脚本目录或系统 PATH 中")
        sys.exit(1)

    if no_permission_required:
        console.print("[red]错误：以下工具缺少执行权限[/red]")
        for tool_path in no_permission_required:
            console.print(f"  - {tool_path}")
        if not sys.platform.startswith("win"):
            console.print("[yellow]请使用下面的命令授予执行权限：[/yellow]")
            for tool_path in no_permission_required:
                console.print(f"chmod +x {shlex.quote(tool_path)}")
        else:
            console.print("[yellow]请检查文件权限、来源限制或安全软件拦截。[/yellow]")
        sys.exit(1)

    if broken_required:
        console.print(f"[red]错误：以下必需工具存在但无法正常运行 - {', '.join(broken_required)}[/red]")
        console.print("请检查工具文件是否损坏、依赖是否完整，或路径是否指向了错误的可执行文件")
        sys.exit(1)

    console.print("[green]✓ 工具链检查通过[/green]")


def get_display_width(text: str) -> int:
    """
    计算字符串在终端的显示宽度（全角=2，半角=1）
    """
    return sum(2 if unicodedata.east_asian_width(c) in "FWA" else 1 for c in text)


def truncate_to_display_width(text: str, max_width: int, placeholder: str = "...") -> str:
    """
    按显示宽度截断字符串
    """
    current_width = get_display_width(text)
    if current_width <= max_width:
        return text

    placeholder_width = get_display_width(placeholder)
    if max_width < placeholder_width:
        return ""

    effective_max = max_width - placeholder_width
    truncated = []
    current = 0

    for char in text:
        char_width = get_display_width(char)
        if current + char_width <= effective_max:
            truncated.append(char)
            current += char_width
        else:
            break

    return "".join(truncated) + placeholder


def sanitize_filename(filename: str) -> str:
    """替换文件名中的非法字符并清理首尾空格（Windows 兼容）"""
    return "".join(ILLEGAL_CHAR_MAP.get(char, char) for char in filename).strip()


def interactive_input(prompt_text: str = "") -> str:
    """统一交互输入，优先使用 prompt_toolkit 提供历史记录和方向键支持。"""
    global _PROMPT_SESSION

    stdin_is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    stdout_is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

    if PromptSession and InMemoryHistory and AutoSuggestFromHistory and stdin_is_tty and stdout_is_tty:
        try:
            if _PROMPT_SESSION is None:
                _PROMPT_SESSION = PromptSession(history=InMemoryHistory(), auto_suggest=AutoSuggestFromHistory())
            return _PROMPT_SESSION.prompt(prompt_text)
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception:
            pass

    return input(prompt_text)


def extract_bitrate_from_line(line: str) -> float:
    """
    从行中提取码率（支持 kbps 和 Mbps，支持小数）

    优先匹配 kbps，然后尝试 Mbps（自动转换为 kbps）。
    支持小数码率（如字幕码率：26.685 kbps）。

    Args:
        line: 待解析的文本行

    Returns:
        码率（kbps），未找到时返回 0

    Examples:
        >>> extract_bitrate_from_line("Audio: DTS-HD MA / 48 kHz / 7.1 / 24-bit (DTS Core: 5.1 / 48 kHz / 1,509 kbps)")
        1509.0
        >>> extract_bitrate_from_line("Video: MPEG-4 AVC Video / 1920x1080 / 23.976 fps / 35.5 Mbps")
        35500.0
        >>> extract_bitrate_from_line("Presentation Graphics  Chinese  26.685 kbps  简体特效")
        26.685
    """
    # 优先匹配 kbps（支持小数）
    match = re.search(BITRATE_KBPS_PATTERN, line, re.IGNORECASE)
    if match:
        bitrate_clean = match.group(1).replace(",", "")
        return float(bitrate_clean) if bitrate_clean else 0.0

    # 尝试匹配 Mbps 并转换为 kbps
    match = re.search(BITRATE_MBPS_PATTERN, line, re.IGNORECASE)
    if match:
        mbps = float(match.group(1))
        return mbps * 1000.0

    return 0.0


def has_keywords(text: str, keywords: List[str]) -> bool:
    """
    检查文本是否包含关键词列表中的任意一个

    Args:
        text: 待检查的文本
        keywords: 关键词列表

    Returns:
        如果文本包含任意关键词返回 True，否则返回 False

    Examples:
        >>> has_keywords("央视国配", ["央视", "公映"])
        True
        >>> has_keywords("", ["导评"])
        False
    """
    return text and any(kw in text for kw in keywords)


@lru_cache(maxsize=128)
def traditional_to_simplified(text: str) -> str:
    """
    繁体转简体（针对 BDInfo 常见关键词）

    仅转换常见的字幕描述关键词，保持简洁高效。
    使用 LRU 缓存避免重复转换，最多缓存 128 个结果。
    """
    # 常见繁简对照表（针对 BDInfo 字幕描述）
    trans_map = {
        # 基础字
        "繁": "繁",
        "簡": "简",
        "體": "体",
        "語": "语",
        "國": "国",
        "雙": "双",
        "臺": "台",
        "灣": "湾",
        "導": "导",
        "評": "评",
        "對": "对",
        "應": "应",
        "視": "视",
        # 常见词组
        "對應": "对应",
        "央視": "央视",
        "繁體": "繁体",
        "簡體": "简体",
        "國語": "国语",
        "國配": "国配",
        "雙語": "双语",
        "臺灣": "台湾",
        "導評": "导评",
        "繁英雙語": "繁英双语",
        "國配繁體": "国配繁体",
        # 其他
        "粵": "粤",
        "粵語": "粤语",
        "廣": "广",
        "東": "东",
    }

    # 按长度降序排列（优先匹配长词组）
    sorted_keys = sorted(trans_map.keys(), key=len, reverse=True)

    for trad in sorted_keys:
        if trad in text:
            text = text.replace(trad, trans_map[trad])

    return text


def format_duration(seconds: float) -> str:
    """格式化时长为 H:MM:SS"""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def format_size(bytes_size: int) -> str:
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_size < 1024.0:
            return f"{bytes_size:.1f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.1f} PB"


# ==============================================================================
# MPLS 二进制解析类
# ==============================================================================


class Chapter:
    """MPLS 播放列表解析类"""

    formats: dict[int, str] = {1: ">B", 2: ">H", 4: ">I", 8: ">Q"}

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.in_out_time: List[Tuple[str, int, int]] = []  # (m2ts_name, in_time, out_time)
        self.mark_info: Dict[int, List[int]] = {}  # {play_item_id: [mark_timestamps]}
        self.pid_to_lang: Dict[int, str] = {}  # {pid: language_code}

        self._parse_mpls()

    def _parse_mpls(self):
        """解析 MPLS 文件"""
        try:
            with open(self.file_path, "rb") as f:
                self.mpls_file = f
                f.seek(8)
                playlist_start_address = self._unpack_byte(4)
                playlist_mark_start_address = self._unpack_byte(4)

                # 解析 PlayItem
                f.seek(playlist_start_address)
                f.read(6)
                nb_play_items = self._unpack_byte(2)
                f.read(2)

                for _ in range(nb_play_items):
                    pos = f.tell()
                    length = self._unpack_byte(2)
                    if length != 0:
                        clip_name = f.read(5).decode("utf-8", errors="ignore")
                        f.read(7)
                        in_time = self._unpack_byte(4)
                        out_time = self._unpack_byte(4)
                        self.in_out_time.append((clip_name, in_time, out_time))
                    f.seek(pos + length + 2)

                # 解析 PlaylistMark（章节）
                f.seek(playlist_mark_start_address)
                f.read(4)
                nb_playlist_marks = self._unpack_byte(2)

                for _ in range(nb_playlist_marks):
                    f.read(2)
                    ref_to_play_item_id = self._unpack_byte(2)
                    mark_timestamp = self._unpack_byte(4)
                    f.read(6)

                    if ref_to_play_item_id in self.mark_info:
                        self.mark_info[ref_to_play_item_id].append(mark_timestamp)
                    else:
                        self.mark_info[ref_to_play_item_id] = [mark_timestamp]

        except Exception as e:
            raise RuntimeError(f"解析 MPLS 文件失败：{self.file_path}\n{e}")

    def _unpack_byte(self, n: int) -> int:
        """读取并解包 n 字节"""
        return unpack(self.formats[n], self.mpls_file.read(n))[0]

    def get_total_time(self) -> float:
        """获取总时长（秒）"""
        return sum((out - in_t) / 45000 for _, in_t, out in self.in_out_time)

    def get_total_time_no_repeat(self) -> float:
        """获取去重后总时长（秒）"""
        return sum({name: (out - in_t) / 45000 for name, in_t, out in self.in_out_time}.values())

    def get_chapter_count(self) -> int:
        """获取章节总数"""
        return len(self.get_chapter_timestamps())

    def debug_chapter_info(self) -> str:
        """调试：输出章节标记详细信息"""
        lines = []
        lines.append("章节标记调试信息")

        # PlayItem 信息
        lines.append(f"\nPlayItem 总数: {len(self.in_out_time)}")
        for i, (name, in_time, out_time) in enumerate(self.in_out_time):
            duration = (out_time - in_time) / 45000
            lines.append(f"  PlayItem {i}: {name}.m2ts")
            lines.append(f"    In: {in_time:>10} ({in_time/45000:>8.3f}s)")
            lines.append(f"    Out: {out_time:>10} ({out_time/45000:>8.3f}s)")
            lines.append(f"    Duration: {duration:>8.3f}s")

        # 章节标记信息
        lines.append(f"\n章节标记分布:")
        total_marks = 0
        filtered_marks = 0
        offset = 0.0
        last_ts = -1.0

        for ref_id in sorted(self.mark_info.keys()):
            marks = self.mark_info[ref_id]
            total_marks += len(marks)
            lines.append(f"  PlayItem {ref_id}: {len(marks)} 个标记")

            if ref_id < len(self.in_out_time):
                segment = self.in_out_time[ref_id]
                out_time = segment[2]

                for idx, mark in enumerate(marks, 1):
                    mark_sec = mark / 45000
                    timestamp = offset + (mark - segment[1]) / 45000

                    # 判定状态
                    if out_time - mark < 45000:
                        distance = (out_time - mark) / 45000
                        status = f"✗ 过滤（距结尾 {distance:.3f}s）"
                        filtered_marks += 1
                    elif mark >= out_time:
                        status = "✗ 超出"
                        filtered_marks += 1
                    elif timestamp - last_ts < 1.0:
                        status = f"✗ 去重（距上一标记 {timestamp - last_ts:.3f}s）"
                        filtered_marks += 1
                    else:
                        status = "✓"
                        last_ts = timestamp

                    lines.append(f"    标记 {idx}: {mark:>10} ({mark_sec:>8.3f}s) {status}")

                offset += (segment[2] - segment[1]) / 45000

        lines.append(f"\n标记总数: {total_marks}")
        lines.append(f"过滤标记: {filtered_marks} 个")
        lines.append(f"有效章节: {total_marks - filtered_marks} 个")

        return "[dim]" + "\n".join(lines) + "[/dim]"

    def get_chapter_timestamps(self) -> List[float]:
        """获取所有章节时间戳（秒）- 过滤结尾标记并执行相邻去重"""
        timestamps = []
        offset = 0.0
        last_ts = -1.0  # 记录上一个有效章节的时间戳

        for ref_id, marks in self.mark_info.items():
            if ref_id < len(self.in_out_time):
                segment = self.in_out_time[ref_id]
                out_time = segment[2]

                for mark in marks:
                    # 1. 过滤掉距离结束时间小于1秒的标记（45000单位 = 1秒）
                    if out_time - mark >= 45000:
                        # 计算当前标记在整部电影中的绝对时间戳
                        timestamp = offset + (mark - segment[1]) / 45000

                        # 2. 相邻去重：如果距离上一个章节不足 1 秒，判定为原盘 Authoring 冗余，直接忽略
                        if timestamp - last_ts >= 1.0:
                            timestamps.append(timestamp)
                            last_ts = timestamp

                offset += (segment[2] - segment[1]) / 45000

        return timestamps

    def get_m2ts_files(self) -> List[str]:
        """获取所有 M2TS 文件名（去重）"""
        return list(dict.fromkeys(name for name, _, _ in self.in_out_time))

    def get_pid_to_language(self):
        """提取 PID 到语言代码的映射"""
        with open(self.file_path, "rb") as f:
            self.mpls_file = f
            f.seek(8)
            playlist_start_address = self._unpack_byte(4)
            f.seek(playlist_start_address)
            f.read(6)
            nb_of_play_items = self._unpack_byte(2)
            f.read(2)

            for _ in range(nb_of_play_items):
                f.read(12)
                is_multi_angle = (self._unpack_byte(1) >> 4) % 2
                f.read(21)

                if is_multi_angle:
                    nb_of_angles = self._unpack_byte(1)
                    f.read(1)
                    for _ in range(nb_of_angles - 1):
                        f.read(10)

                f.read(4)
                nb = [self._unpack_byte(1) for _ in range(8)]
                f.read(4)

                for _ in range(sum(nb)):
                    stream_entry_length = self._unpack_byte(1)
                    stream_type = self._unpack_byte(1)

                    # 读取 PID
                    if stream_type == 1:
                        stream_pid = self._unpack_byte(2)
                        f.read(stream_entry_length - 3)
                    elif stream_type == 2:
                        f.read(2)
                        stream_pid = self._unpack_byte(2)
                        f.read(stream_entry_length - 5)
                    elif stream_type in (3, 4):
                        f.read(1)
                        stream_pid = self._unpack_byte(2)
                        f.read(stream_entry_length - 4)
                    else:
                        stream_pid = 0
                        f.read(stream_entry_length - 2)

                    # 读取流属性
                    stream_attributes_length = self._unpack_byte(1)
                    stream_coding_type = self._unpack_byte(1)

                    # 视频流
                    if stream_coding_type in (1, 2, 27, 36, 234):
                        self.pid_to_lang[stream_pid] = "und"
                        f.read(stream_attributes_length - 1)
                    # 音频流
                    elif stream_coding_type in (3, 4, 128, 129, 130, 131, 132, 133, 134, 146, 161, 162):
                        f.read(1)
                        lang = f.read(3).decode("utf-8", errors="ignore")
                        self.pid_to_lang[stream_pid] = lang
                        f.read(stream_attributes_length - 5)
                    # 字幕流
                    elif stream_coding_type in (144, 145):
                        lang = f.read(3).decode("utf-8", errors="ignore")
                        self.pid_to_lang[stream_pid] = lang
                        f.read(stream_attributes_length - 4)
                    else:
                        f.read(stream_attributes_length - 1)

                break  # 只解析第一个 PlayItem


class M2TS:
    """M2TS 文件分析类"""

    def __init__(self, filename: str):
        self.filename = filename
        self.frame_size = 192

    def get_duration(self) -> int:
        """获取时长（90kHz 单位）"""
        try:
            with open(self.filename, "rb") as f:
                self.m2ts_file = f

                # 查找第一个 PCR
                buffer_size = 256 * 1024
                buffer_size -= buffer_size % self.frame_size
                cur_pos = 0
                first_pcr_val = -1

                while cur_pos < buffer_size:
                    f.read(7)
                    first_pcr_val = self._get_pcr_val()
                    f.read(182)
                    cur_pos += self.frame_size
                    if first_pcr_val != -1:
                        break

                # 查找最后一个 PCR
                buffer_size = 256 * 1024
                last_pcr_val = self._get_last_pcr_val(buffer_size)

                while last_pcr_val == -1 and buffer_size <= 1024 * 1024:
                    buffer_size *= 4
                    last_pcr_val = self._get_last_pcr_val(buffer_size)

                return 0 if last_pcr_val == -1 else last_pcr_val - first_pcr_val

        except Exception:
            return 0

    def _get_last_pcr_val(self, buffer_size: int) -> int:
        """从文件末尾查找最后一个 PCR"""
        last_pcr_val = -1
        file_size = Path(self.filename).stat().st_size
        cur_pos = max(file_size - file_size % self.frame_size - buffer_size, 0)
        buffer_end = cur_pos + buffer_size

        while cur_pos <= buffer_end - self.frame_size:
            self.m2ts_file.seek(cur_pos + 7)
            _last_pcr_val = self._get_pcr_val()
            if _last_pcr_val != -1:
                last_pcr_val = _last_pcr_val
            cur_pos += self.frame_size

        return last_pcr_val

    def _unpack_bytes(self, n: int) -> int:
        formats = {1: ">B", 2: ">H", 4: ">I", 8: ">Q"}
        return unpack(formats[n], self.m2ts_file.read(n))[0]

    def _get_pcr_val(self) -> int:
        """读取 PCR 值"""
        af_exists = (self._unpack_bytes(1) >> 5) % 2
        adaptive_field_length = self._unpack_bytes(1)
        pcr_exist = (self._unpack_bytes(1) >> 4) % 2

        if af_exists and adaptive_field_length and pcr_exist:
            tmp = [self._unpack_bytes(1) for _ in range(4)]
            pcr = tmp[3] + (tmp[2] << 8) + (tmp[1] << 16) + (tmp[0] << 24)
            pcr_lo = self._unpack_bytes(1) >> 7
            return (pcr << 1) + pcr_lo

        return -1


# ==============================================================================
# 音频描述解析辅助函数
# ==============================================================================


def parse_audio_custom_desc(custom_desc: str, language: str) -> Optional[str]:
    """
    从 BDInfo 自定义描述中提取音频轨道显示名称

    仅针对中文音轨，通过关键词匹配生成规范化的轨道名称。
    非中文音轨返回 None，使用默认语言名称。

    规则（优先级从高到低）：
    - 第一组（地区/版本）+ 第二组（国语）：拼接，第二组统一为"国语"
      示例：[央视国配] → "央视国语"
    - 第一组（仅地区）：默认添加"国语"
      示例：[央视] → "央视国语"
    - 第三组（方言）：直接使用，"粤语" 转为 "粤配"
      示例：[粤语] → "粤配"
    - 都没命中：返回 None（使用默认语言名）

    Args:
        custom_desc: BDInfo 自定义描述（如 "央视国配", "央视", "粤语"）
        language: 语言代码（如 "chi", "zh-Hans", "eng"）

    Returns:
        轨道显示名称（如 "央视国语", "粤配"），无匹配时返回 None
    """
    if not custom_desc:
        return None

    # 仅处理中文音轨（支持 ISO 639-1 和 ISO 639-2 变体）
    if language not in ["zho", "chi", "zh-Hans", "zh-Hant", "zh"]:
        return None

    # 获取关键词配置
    region_keywords = TRACK_KEYWORDS["region"]
    mandarin_keywords = TRACK_KEYWORDS["mandarin"]
    dialect_keywords = TRACK_KEYWORDS["dialect"]

    # 检查是否命中各组关键词
    matched_region = None
    matched_dialect = None

    for keyword in region_keywords:
        if keyword in custom_desc:
            matched_region = keyword
            break

    for keyword in dialect_keywords:
        if keyword in custom_desc:
            # "粤语" 转为 "粤配"
            matched_dialect = "粤配" if keyword == "粤语" else keyword
            break

    # 匹配逻辑
    # 优先级：第一组+第二组 > 第一组（仅地区）> 第三组 > 无匹配
    if matched_region:
        # 有地区标识：央视 → 央视国语
        return f"{matched_region}国语"
    elif matched_dialect:
        # 只有方言标识：粤语 → 粤配
        return matched_dialect
    else:
        # 都没命中，返回 None（使用默认语言名）
        return None


def parse_subtitle_components(desc: str) -> Dict[str, Optional[str]]:
    """
    从字幕描述中提取所有可识别的组件

    返回字典包含:
    - region: 地区标识 (如 "央视", "六区", "公映")
    - dubbing: 配音标识 (如 "国配", "粤配", "台配")
    - script: 简繁标识 ("简体" | "繁体")
    - language: 语言标识 (如 "英", "日", "法")
    - position: 位置标识 (如 "黑边", "画内")
    - has_bilingual: 是否双语 (True/False)
    - has_feature: 是否特效字幕 (True/False)
    - has_commentary: 是否导评 (True/False)
    - commentary_num: 导评编号 (如 "1", "2")
    - has_original: 原盘字幕标识
    - raw_desc: 原始描述

    Args:
        desc: 字幕描述字符串

    Returns:
        包含所有提取组件的字典

    Examples:
        >>> parse_subtitle_components("国配简体特效（对照六区国语）")
        {
            "region": "六区",
            "dubbing": "国配",
            "script": "简体",
            "has_feature": True,
            ...
        }

        >>> parse_subtitle_components("简英双语特效")
        {
            "script": "简体",
            "language": "英",
            "has_bilingual": True,
            "has_feature": True,
            ...
        }

        >>> parse_subtitle_components("繁英特效（画面外）")
        {
            "script": "繁体",
            "language": "英",
            "position": "黑边",
            "has_bilingual": True,
            "has_feature": True,
            ...
        }
    """
    components = {
        "region": None,
        "dubbing": None,
        "script": None,
        "language": None,
        "position": None,
        "has_bilingual": False,
        "has_feature": False,
        "has_commentary": False,
        "commentary_num": None,
        "has_original": False,
        "raw_desc": desc,
    }

    if not desc:
        return components

    # 检测原盘标识
    if "原盘" in desc:
        components["has_original"] = True

    # 1. 提取导评信息
    desc_lower = desc.lower()
    if any(kw in desc_lower for kw in TRACK_KEYWORDS["commentary"]):
        components["has_commentary"] = True
        # 提取导评编号 (支持 "导评1" 或 "Commentary 2")
        if match := re.search(r"导评(\d+)|commentary\s*(\d+)", desc, re.IGNORECASE):
            components["commentary_num"] = match.group(1) or match.group(2)

    # 2. 提取地区标识 (从 TRACK_KEYWORDS 配置)
    for keyword in TRACK_KEYWORDS["region"]:
        if keyword in desc:
            components["region"] = keyword
            break  # 使用第一个匹配的(优先级最高)

    # 3. 提取配音标识
    # 优先检测方言（粤配、台配、港配、粤语）
    for keyword in TRACK_KEYWORDS["dialect"]:
        if keyword in desc:
            components["dubbing"] = keyword
            break

    # 如果不是方言，再检测是否包含国配关键词（国语、普通话能覆盖各种"对照国语/对应普通话"）
    if not components["dubbing"] and any(kw in desc for kw in ["国配", "国语", "普通话"]):
        components["dubbing"] = "国配"

    # 4. 提取简繁标识
    if "简体" in desc or "简中" in desc or "简" in desc[:3]:  # "简"需在前3个字符
        components["script"] = "简体"
    elif "繁体" in desc or "繁中" in desc or "繁" in desc[:3]:
        components["script"] = "繁体"

    # 5. 提取语言标识 (用于双语字幕)
    for lang_key, lang_short in SUBTITLE_TEXT_MARKERS:
        if lang_key in desc:
            components["language"] = lang_short
            break

    # 6. 检测双语/特效标识
    components["has_bilingual"] = "双语" in desc
    components["has_feature"] = "特效" in desc

    # 智能补充双语判定：如果没有"双语"字眼，但同时包含了外语标识（如"英"）和中文标识（"简"/"繁"/"中"）
    if components["language"] and any(char in desc for char in ["简", "繁", "中"]):
        components["has_bilingual"] = True

    # 7. 提取并统一位置标识（关键词分组）
    position_groups = {"黑边": ["黑边", "画外", "画面外"], "画内": ["画内", "画面内"]}

    for standard_pos, keywords in position_groups.items():
        if any(kw in desc for kw in keywords):
            components["position"] = standard_pos
            break

    return components


def reconstruct_subtitle_desc(components: Dict[str, Optional[str]], track_lang: str = "und") -> str:
    """
    根据提取的组件重构规范化的字幕描述

    重构规则:
    1. 导评: "导评中文{N}"
    2. 双语特效: "{简|繁}{语言}双语特效"
    3. 配音特效(有地区): "{配音}{简繁}特效（{地区}）"
    4. 配音特效(无地区): "{配音}{简繁}特效"
    5. 其他: 保持原描述

    Args:
        components: parse_subtitle_components() 返回的组件字典

    Returns:
        规范化后的描述字符串
    """
    # 提取原盘前缀
    original_disc_prefix = "原盘" if components.get("has_original") else ""

    # 构建位置后缀：如果有位置信息，统一用全角括号包起来
    pos_suffix = f"（{components['position']}）" if components.get("position") else ""

    # 1. 导评字幕
    if components["has_commentary"]:
        num = components["commentary_num"] or ""
        script = components["script"]
        lang = components["language"]

        # 优先级：简繁体 > 外语 > 兜底中文
        if script:
            return f"{original_disc_prefix}导评{script}{num}{pos_suffix}"
        elif lang:
            # 拼上"文"字，将"英"变成"导评英语"
            return f"{original_disc_prefix}导评{lang}语{num}{pos_suffix}"
        elif track_lang != "und" and track_lang not in ["zho", "chi", "zh", "zh-Hans", "zh-Hant"]:
            # 使用实际轨道语言名称（如 "eng" 会被转成 "英语"）
            real_lang = get_language_display_name(track_lang)
            return f"{original_disc_prefix}导评{real_lang}{num}{pos_suffix}"
        else:
            return f"{original_disc_prefix}导评中文{num}{pos_suffix}"

    # 2. 双语特效字幕
    if components["has_bilingual"] and components["has_feature"]:
        script_short = "简" if components["script"] == "简体" or components["script"] is None else "繁"
        lang = components["language"] or get_subtitle_language_short(track_lang) or "英"
        return f"{script_short}{lang}双语特效{pos_suffix}"

    # 3. 双语字幕（无特效）
    if components["has_bilingual"] and not components["has_feature"] and not components["has_commentary"] and not components["dubbing"]:
        script_short = "简" if components["script"] == "简体" or components["script"] is None else "繁"
        lang = components["language"] or get_subtitle_language_short(track_lang) or "英"
        return f"{original_disc_prefix}{script_short}{lang}双语{pos_suffix}"

    # 4. 配音字幕(带特效)
    if components["dubbing"] and components["has_feature"]:
        script = components["script"] or ("简体" if components["dubbing"] == "国配" else "")
        region = components["region"]

        # 构建基础描述: {配音}{简繁}特效
        desc_parts = [components["dubbing"]]
        if script:
            desc_parts.append(script)
        desc_parts.append("特效")

        base_desc = "".join(desc_parts)

        # 添加地区括号
        if region:
            return f"{base_desc}（{region}）{pos_suffix}"
        else:
            return f"{base_desc}{pos_suffix}"

    # 5. 配音字幕(不带特效)
    if components["dubbing"]:
        script = components["script"] or ("简体" if components["dubbing"] == "国配" else "")
        region = components["region"]

        desc_parts = [components["dubbing"]]
        if script:
            desc_parts.append(script)
            desc_parts.append("中文")

        base_desc = "".join(desc_parts)

        if region:
            return f"{original_disc_prefix}{base_desc}（{region}）{pos_suffix}"
        else:
            return f"{original_disc_prefix}{base_desc}{pos_suffix}"

    # 6. 普通单语特效字幕（有特效、无配音、无双语、无导评）
    if components["has_feature"] and not components["has_bilingual"] and not components["has_commentary"] and not components["dubbing"]:
        if components["script"] and not components["language"]:
            base_desc = f"{components['script']}特效"
            if components["region"]:
                return f"{original_disc_prefix}{base_desc}（{components['region']}）{pos_suffix}"
            else:
                return f"{original_disc_prefix}{base_desc}{pos_suffix}"

    # 7. 普通简繁字幕 (无特效、无配音、无双语、无导评)
    if not components["has_feature"] and not components["has_bilingual"] and not components["has_commentary"] and not components["dubbing"]:
        if components["script"]:
            base_desc = f"{components['script']}中文"  # 自动拼接出 "简体中文" / "繁体中文"
            if components["region"]:
                return f"{original_disc_prefix}{base_desc}（{components['region']}）{pos_suffix}"
            else:
                return f"{original_disc_prefix}{base_desc}{pos_suffix}"

    # 8. 其他: 保持原描述
    return components["raw_desc"]


def optimize_subtitle_desc(custom_desc: str, track_lang: str = "und") -> str:
    """
    优化字幕描述格式 (使用组件提取方案)

    新方案:
    1. 预处理: 繁简转换
    2. 提取组件
    3. 重构描述
    4. 返回规范化结果

    转换规则：
    1. 导评: "导评{N}中文" → "导评中文{N}"
    2. 配音字幕: [对应]{地区}{配音}[简繁]特效 → {配音}{简繁}特效({地区})
       - 方括号表示可选项
       - 缺少繁简标注时，国配默认为"简体"，方言配音（粤配/台配/港配）不默认
       - 有"特效"结尾输出带"特效"，无"特效"结尾输出带"中文"
       - 仅地区关键词时默认为"国配"
       - 无地区关键词时不带括号

    3. 双语字幕: 中{语言}双语{繁简}特效 → {繁简}{语言}双语特效
       - 中{语言}{繁简}特效 → {繁简}{语言}双语特效（缺"双语"）
       - 中{语言}双语特效 → 简{语言}双语特效（默认简体）
       - 中{语言}特效 → 简{语言}双语特效（不带"双语"，默认简体）
       - {繁简}中{语言}特效 → {繁简}{语言}双语特效（繁简在前）
       - {繁简}{语言}特效 → {繁简}{语言}双语特效（已有简繁标识）

    示例：
    导评字幕格式：
    - "导评1中文" → "导评中文1"
    - "导评2中文" → "导评中文2"

    配音字幕格式：
    - "对应央视国配简体特效" → "国配简体特效（央视）"
    - "央视国配特效" → "国配简体特效（央视）"
    - "公映国配特效" → "国配简体特效（公映）"
    - "六区粤配特效" → "粤配特效（六区）"
    - "国配简体特效（对照六区国语）" → "国配简体特效（六区）"

    双语字幕格式：
    - "中英双语简体特效" → "简英双语特效"
    - "中英简体特效" → "简英双语特效"
    - "中日双语特效" → "简日双语特效"
    - "中英特效" → "简英双语特效"
    - "简英特效" → "简英双语特效"
    - "繁英特效" → "繁英双语特效"
    - "简体中英特效" → "简英双语特效"
    - "中英雙語繁體特效" → "繁英双语特效"（繁体输入自动处理）

    双语及位置字幕格式：
    - "中英双语简体特效" → "简英双语特效"
    - "简英特效" → "简英双语特效"（智能识别并补全"双语"）
    - "繁英特效" → "繁英双语特效"（智能识别并补全"双语"）
    - "简英特效（黑边内）" → "简英双语特效（黑边）"（规范化位置标签）
    - "国配简体特效（画内）" → "国配简体特效（画内）"

    支持的语言：英/英文、日/日文、法/法文、德/德文、韩/韩文、西/西文、俄/俄文、意/意文、葡/葡文

    Args:
        custom_desc: 原始字幕描述（可以是繁体或简体）

    Returns:
        优化后的描述（统一为简体格式），无匹配时返回原值
    """
    if not custom_desc:
        return custom_desc

    # 预处理: 繁体转简体
    desc = traditional_to_simplified(custom_desc)

    # 提取组件
    components = parse_subtitle_components(desc)

    # 重构描述
    result = reconstruct_subtitle_desc(components, track_lang)

    return result


# ==============================================================================
# Track 数据模型
# ==============================================================================


class Track:
    """轨道数据模型"""

    def __init__(self, track_id: int, track_type: str):
        self.id = track_id
        self.type = track_type  # "video" / "audio" / "subtitle"
        self.language = "und"
        self.codec = ""
        self.bitrate = 0
        self.channels = ""
        self.sample_rate = ""
        self.name = ""
        self.custom_desc = ""  # BDInfo 自定义描述
        self.is_default = False
        self.is_atmos = False
        self.ac3_core_bitrate = 0
        self.index_in_type = 0  # 在同类型中的索引
        # mkvmerge 标志位
        self.is_commentary = False  # 评论轨标记
        self.is_original = False  # 原语言标记
        self.is_hearing_impaired = False  # SDH 听觉障碍标记
        # AC3 核心轨道标记
        self.is_ac3_core = False  # AC3 核心轨道标记
        self.parent_truehd_id = None  # 父 TrueHD 轨道 ID

    @property
    def display_id(self) -> str:
        """返回 A1/A2/S1 格式的显示 ID"""
        type_prefix = {"video": "V", "audio": "A", "subtitle": "S"}
        return f"{type_prefix[self.type]}{self.index_in_type + 1}"

    def generate_track_name(self) -> str:
        """生成轨道名称"""
        # 视频轨道不生成名称
        if self.type == "video":
            return ""

        # 自动生成轨道名
        if self.type == "audio":
            parts = []

            # 语言：优先使用 custom_desc 解析结果（仅中文）
            parsed_name = None
            if self.custom_desc and not self.is_commentary:  # 导评轨道不使用 parse_audio_custom_desc
                parsed_name = parse_audio_custom_desc(self.custom_desc, self.language)

            if parsed_name:
                # 使用解析后的名称（如 "央视国语", "粤配"）
                parts.append(parsed_name)
            else:
                # 回退到语言代码（使用智能语言名称获取）
                lang_display = get_language_display_name(self.language)

                # 如果是导评音轨，忽略原描述的杂乱词汇，统一规范为 "{语言}导评{编号}"
                if self.is_commentary:
                    # 尝试保留可能存在的编号（如"导评1"、"导评2"）
                    num_match = re.search(r"导评(\d+)", self.custom_desc) if self.custom_desc else None
                    num = num_match.group(1) if num_match else ""
                    parts.append(f"{lang_display}导评{num}")
                else:
                    parts.append(lang_display)

            # 编码
            codec_display = format_codec_display(self.codec, self.is_atmos)
            parts.append(codec_display)

            # 声道
            if self.channels:
                parts.append(self.channels)

            # 码率（与表格显示逻辑一致）
            if self.bitrate:
                # 如果有小数部分，保留3位小数；如果是整数值，不显示小数点
                if self.bitrate % 1 != 0:
                    parts.append(f"@ {self.bitrate:.3f} kbps")
                else:
                    parts.append(f"@ {int(self.bitrate)} kbps")

            return " ".join(parts)

        elif self.type == "subtitle":
            # 字幕：使用智能语言名称获取
            if self.custom_desc:
                # 第一步：优化描述格式（转换模式）
                desc = optimize_subtitle_desc(self.custom_desc, self.language)

                # 扩展简化的描述
                # 简体特效 → 简体中文特效（排除国配/台配/粤配/港配等配音字幕）
                dubbing_kws = ["国配", "台配", "粤配", "港配"]
                if "简体特效" in desc and "简体中文特效" not in desc and not any(kw in desc for kw in dubbing_kws):
                    desc = desc.replace("简体特效", "简体中文特效")
                # 繁体特效 → 繁体中文特效（排除国配/台配/粤配/港配等配音字幕）
                if "繁体特效" in desc and "繁体中文特效" not in desc and not any(kw in desc for kw in dubbing_kws):
                    desc = desc.replace("繁体特效", "繁体中文特效")

                # 简中 → 简体中文
                if "简中" in desc and "简体中文" not in desc:
                    desc = desc.replace("简中", "简体中文")
                # 繁中 → 繁体中文
                if "繁中" in desc and "繁体中文" not in desc:
                    desc = desc.replace("繁中", "繁体中文")

                # 原盘简体/繁体 → 补充“中文”
                if "原盘简体" in desc and "原盘简体中文" not in desc:
                    desc = desc.replace("原盘简体", "原盘简体中文")
                if "原盘繁体" in desc and "原盘繁体中文" not in desc:
                    desc = desc.replace("原盘繁体", "原盘繁体中文")

                # 台繁 → 台湾繁体中文
                if "台繁" in desc and "台湾繁体中文" not in desc:
                    desc = desc.replace("台繁", "台湾繁体中文")
                if "港繁" in desc and "香港繁体中文" not in desc:
                    desc = desc.replace("港繁", "香港繁体中文")
                if "粤繁" in desc and "粤语繁体中文" not in desc:
                    desc = desc.replace("粤繁", "粤语繁体中文")
            else:
                desc = get_language_display_name(self.language)

            # 处理 SDH 听觉障碍标记
            if self.is_hearing_impaired:
                # 清除原有的 SDH/CC 标识，避免出现 "英语(SDH) (SDH)"，并去掉多余空格
                desc = SDH_PATTERN.sub("", desc).strip()
                # 如果清理后为空，则回退到基础语言名称
                if not desc:
                    desc = get_language_display_name(self.language)
                desc = f"{desc}（SDH）"

            return desc

        return f"Track {self.id}"

    def to_mkvmerge_args(self) -> List[str]:
        """生成 mkvmerge 参数"""
        args = []

        # 语言（使用 pycountry 转换为 BCP 47）
        lang_code = get_language_tag(self.language, self.type)
        args.extend(["--language", f"{self.id}:{lang_code}"])

        # 轨道名（视频轨保持原始名称，不设置）
        if self.name and self.type != "video":
            args.extend(["--track-name", f"{self.id}:{self.name}"])

        # 默认标志
        args.extend(["--default-track", f"{self.id}:{'1' if self.is_default else '0'}"])

        # 评论轨标志
        if self.is_commentary:
            args.extend(["--commentary-flag", f"{self.id}:1"])

        # 原语言标志
        if self.is_original:
            args.extend(["--original-flag", f"{self.id}:1"])

        # SDH 听觉障碍标志
        if self.is_hearing_impaired:
            args.extend(["--hearing-impaired-flag", f"{self.id}:1"])

        return args


# ==============================================================================
# 地区权重计算（统一工具函数）
# ==============================================================================


def _compute_region_weight(text: str) -> int:
    """
    计算地区权重（统一音频和字幕）

    优先级：按 TRACK_KEYWORDS["region"] 列表顺序（越靠前权重越高）

    Args:
        text: 待分析的文本（轨道名或描述）

    Returns:
        权重值（0-100），无地区标识返回 0

    Examples:
        >>> _compute_region_weight("央视国语 DTS-HD MA")
        100  # "央视" 是列表第一个
        >>> _compute_region_weight("六区国语 AC3")
        50   # "六区" 权重较低
    """
    if not text:
        return 0

    # 应用繁简转换
    normalized_text = traditional_to_simplified(text)

    # 使用 TRACK_KEYWORDS 中的地区关键词列表
    region_keywords = TRACK_KEYWORDS["region"]

    # 遍历关键词，按顺序赋予递减的权重
    for idx, keyword in enumerate(region_keywords):
        if keyword in normalized_text:
            return 100 - (idx * 10)

    return 0


def validate_audio_track_indices(tracks: List[Track], console: Console, allow_cancel: bool = True) -> None:
    """
    检测音频轨道索引异常并提示用户

    Args:
        tracks: 轨道列表
        console: Rich Console 对象
        allow_cancel: 是否允许用户取消（批量模式下为False）

    Raises:
        RuntimeError: 用户选择取消处理时抛出
    """
    raw_audio_tracks = [t for t in tracks if t.type == "audio"]
    if not raw_audio_tracks:
        return

    audio_indices = sorted(t.id for t in raw_audio_tracks)
    gaps = [audio_indices[i + 1] - audio_indices[i] for i in range(len(audio_indices) - 1)]
    max_gap = max(gaps) if gaps else 0

    if max_gap > 2:
        console.print(f"\n[yellow]警告：检测到原始音频轨道索引不连续（最大间隔：{max_gap}）[/yellow]")
        console.print(f"[yellow]轨道索引：{audio_indices}[/yellow]")
        console.print("[yellow]这通常表示原盘结构异常，可能导致封装问题。[/yellow]")
        if allow_cancel and not Confirm.ask("[yellow]是否继续处理？[/yellow]", default=True):
            raise RuntimeError("用户取消处理")


# ==============================================================================
# TrackSorter 排序引擎
# ==============================================================================


class TrackSorter:
    """轨道排序引擎"""

    def __init__(self, original_lang: str = "eng", drop_commentary: bool = False, keep_best_audio: bool = False, simplify_subs: bool = True):
        self.original_lang = original_lang
        self.drop_commentary = drop_commentary
        self.keep_best_audio = keep_best_audio
        self.simplify_subs = simplify_subs

    def is_chinese(self, lang: str) -> bool:
        """判断是否为中文语言代码"""
        return lang in ["zho", "chi", "zh-Hans", "zh-Hant"]

    def _is_english(self, lang: str) -> bool:
        """判断是否为英语"""
        return lang in ["eng", "en"]

    def should_keep_track(self, lang: str) -> bool:
        """判断是否应该保留该语言的轨道（原语言、中文、英语）"""
        if lang == self.original_lang:
            return True
        if self.is_chinese(lang):
            return True
        if self._is_english(lang):
            return True
        return False

    def filter_and_sort_audio(self, tracks: List[Track]) -> List[Track]:
        """音轨过滤和排序"""
        filtered = [t for t in tracks if self.should_keep_track(t.language)]
        # 过滤：是否丢弃导评轨
        if self.drop_commentary:
            filtered = [t for t in filtered if not t.is_commentary]
        # 去重或只保留最佳
        deduplicated = self._deduplicate_audio(filtered)

        return sorted(deduplicated, key=self._audio_sort_key)

    def _normalize_custom_desc_for_dedup(self, custom_desc: str, language: str) -> str:
        """
        归一化 custom_desc 用于去重

        确保不同配音版本（如"央视国语"和"六区国语"）不会被误删。

        Args:
            custom_desc: BDInfo 自定义描述
            language: 语言代码

        Returns:
            归一化后的描述字符串（用于去重键）
        """
        return parse_audio_custom_desc(custom_desc, language) or ""

    def _deduplicate_audio(self, tracks: List[Track]) -> List[Track]:
        """去重音频轨道"""

        def _get_dedup_key(track: Track) -> Tuple:
            if track.is_commentary:
                return ("commentary", track.id)

            normalized_desc = self._normalize_custom_desc_for_dedup(track.custom_desc, track.language) if track.custom_desc else ""

            # 如果开启了"只保留最高规格"，则仅按【语言+区域描述】进行分组竞争
            if self.keep_best_audio:
                return (track.language, normalized_desc)
            # 否则按原有逻辑：必须编码、声道完全一致才去重
            else:
                return (track.language, track.codec, track.channels, track.is_atmos, normalized_desc)

        best_tracks = {}
        for track in tracks:
            key = _get_dedup_key(track)
            if key not in best_tracks:
                best_tracks[key] = track
            else:
                existing_track = best_tracks[key]
                # 开启最高规格模式：直接比较已有的排序权重（权重 tuple 越小越优秀）
                if self.keep_best_audio:
                    if self._audio_sort_key(track) < self._audio_sort_key(existing_track):
                        best_tracks[key] = track
                # 原有去重逻辑：同编码同声道下保留码率更高的
                else:
                    if track.bitrate > existing_track.bitrate:
                        best_tracks[key] = track

        result = []
        seen_keys = set()
        for track in tracks:
            key = _get_dedup_key(track)
            if key not in seen_keys and best_tracks.get(key) == track:
                result.append(track)
                seen_keys.add(key)

        return result

    def _audio_sort_key(self, track: Track) -> Tuple:
        """音轨排序键"""
        # 导评轨放在最后（通过 is_commentary 标志）
        # commentary_weight: 导评轨=0，普通轨=1
        # 取负后：导评轨=0（较大，排最后），普通轨=-1（较小，排前面）
        commentary_weight = 0 if track.is_commentary else 1

        lang_weight = self._lang_weight(track.language)
        codec_weight = self._codec_weight(track.codec, track.is_atmos)

        # AC3 核心跟随父 TrueHD（仅中文音轨）
        # 只有中文音轨的 AC3 核心使用特殊 codec_weight，使其紧跟在父 TrueHD 之后
        # 其他语言（如英文）按正常编码权重排序（TrueHD Atmos > EAC3 > AC3）
        # TrueHD: 6, AC3 Core (中文): 5.5 → AC3 Core 排在 TrueHD 后面
        if track.is_ac3_core and track.parent_truehd_id is not None:
            if self.is_chinese(track.language):
                codec_weight = 5.5
            # 其他语言保持原编码权重（AC3 = 2）

        # 地区权重：从轨道名提取地区标识（如"央视国语"、"六区国语"）
        region_weight = _compute_region_weight(track.name)

        return (
            -commentary_weight,  # 导评轨最后
            -lang_weight,
            -codec_weight,
            -region_weight,  # 地区权重（同语言同编码时按地区排序）
            -track.bitrate,
        )

    def _lang_weight(self, lang: str) -> int:
        """
        语言权重
        - 源语言为英语：英语 > 中文
        - 源语言为其他：原语言 > 中文 > 英语
        """
        # 原语言永远最高
        if lang == self.original_lang:
            return 10

        # 源语言是英语的情况
        if self._is_english(self.original_lang):
            # 英语 > 中文
            if self._is_english(lang):
                return 10  # 与原语言相同
            if self.is_chinese(lang):
                return 5
        else:
            # 源语言是其他的情况：原语言 > 中文 > 英语
            if self.is_chinese(lang):
                return 5
            if self._is_english(lang):
                return 3

        return 0

    def _codec_weight(self, codec: str, is_atmos: bool) -> int:
        """编码权重"""
        codec_lower = codec.lower()
        if codec_lower == "truehd" and is_atmos:
            return CODEC_WEIGHTS["truehd_atmos"]
        return CODEC_WEIGHTS.get(codec_lower, 0)

    def filter_and_sort_subtitle(self, tracks: List[Track]) -> List[Track]:
        """字幕过滤和排序"""
        filtered = [t for t in tracks if self.should_keep_track(t.language)]
        # 过滤：是否丢弃导评字幕
        if self.drop_commentary:
            filtered = [t for t in filtered if not t.is_commentary]

        # 先执行排序，让最优质的字幕排在最前面
        sorted_subs = sorted(filtered, key=self._subtitle_sort_key)

        # 过滤：精简外语字幕
        if self.simplify_subs:
            final_subs = []
            seen_eng = False
            seen_orig = False

            for track in sorted_subs:
                # 1. 中文字幕：永远保留
                if self.is_chinese(track.language):
                    final_subs.append(track)
                    continue

                # 2. 导评字幕：如果能走到这里，说明用户没有开启 drop_commentary。
                # 直接无条件保留，且绝对不占用正片常规字幕的名额！
                if track.is_commentary:
                    final_subs.append(track)
                    continue

                # 3. 英语常规字幕：只保留第一条（最优的正片字幕）
                if self._is_english(track.language):
                    if not seen_eng:
                        final_subs.append(track)
                        seen_eng = True
                    continue

                # 4. 原语言常规字幕（非英语且非中文）：只保留第一条
                if track.language == self.original_lang:
                    if not seen_orig:
                        final_subs.append(track)
                        seen_orig = True
                    continue

                # 5. 兜底：其他语言（安全保留）
                final_subs.append(track)

            return final_subs

        return sorted_subs

    def _subtitle_sort_key(self, track: Track) -> Tuple:
        """
        字幕排序键计算

        排序优先级（权重越大越靠前）：
        1. 位置权重 (黑边 > 画内 > 无位置)
        2. 类型权重 (双语特效 > 特效 > 原盘 > 导评)
        3. 地区权重 (八一公映 > 央视 > 六区等)
        4. 语言权重 (中文简繁 > 原语言 > 英语)
        5. SDH 权重 (正常字幕排在听障字幕前)
        6. 决胜条件 (Tie-breaker): 轨道名称的 Unicode 拼音顺序 (让前缀相同的轨道完美聚拢)
        """
        lang_weight = self._subtitle_lang_weight(track.language)
        # 使用优化后的 track.name 进行排序（已经过 optimize_subtitle_desc 处理）
        type_weight = self._subtitle_type_weight(track.name)
        # 地区权重：从描述中提取地区标识（如"央视"、"六区"）
        region_weight = _compute_region_weight(track.name)
        # SDH 权重（非 SDH 正常字幕权重为 1，SDH 字幕权重为 0）
        sdh_weight = 0 if track.is_hearing_impaired else 1
        # 字幕位置权重
        position_weight = self._subtitle_position_weight(track.name)
        # 排序优先级调整为：位置 > 类型 > 地区 > 语言 > SDH权重 > 名称拼音
        # 这样同类型的字幕会先按地区分组（如：央视简体、央视繁体、六区简体、六区繁体）
        # 在类型、地区、语言都相同的情况下，正常字幕（-1）会排在 SDH（0）前面
        return (-position_weight, -type_weight, -region_weight, -lang_weight, -sdh_weight, track.name)

    def _subtitle_lang_weight(self, lang: str) -> int:
        """
        字幕语言权重
        顺序：中文 > 原语言 > 英语
        """
        # 中文永远最高
        if self.is_chinese(lang):
            # 简体中文 > 繁体中文
            if lang == "zh-Hans":
                return 100
            elif lang == "zh-Hant":
                return 95
            else:  # zho, chi
                return 90

        # 原语言次之（但不是英语）
        if lang == self.original_lang and not self._is_english(lang):
            return 50

        # 英语
        if self._is_english(lang):
            return 30

        return 0

    def _subtitle_type_weight(self, desc: str) -> int:
        """字幕类型权重（根据描述关键词）"""
        if not desc:  # 处理 None 或空字符串
            return 0

        # 应用繁简转换（用于关键词匹配）
        desc = traditional_to_simplified(desc)

        # 双语特效（最高优先级）
        if "双语特效" in desc:
            return 10

        # 普通双语（次高优先级）
        if "双语" in desc:
            return 9

        # 单语特效（第二优先级）
        if "特效" in desc and "双语" not in desc:
            return 8

        # 其他特殊类型（港/台/粤语，但不是原盘）
        if any(kw in desc for kw in ["港", "台", "粤语"]) and "原盘" not in desc:
            return 6

        # 原盘字幕（简繁体）
        if "原盘" in desc:
            # 原盘粤语/港台 应该排在原盘简繁体之后
            if any(kw in desc for kw in ["粤语", "港", "台"]):
                return 4
            else:
                return 5

        # 导评/评论音轨字幕
        if any(kw in desc.lower() for kw in TRACK_KEYWORDS["commentary"]):
            return 3

        # 默认
        return 1

    def _subtitle_position_weight(self, desc: str) -> int:
        """字幕位置权重（黑边最高）"""
        if not desc:
            return 0
        if "黑边" in desc:
            return 2  # 黑边排在最前面
        if "画内" in desc:
            return 1  # 画内排在其次
        return 0  # 没有位置标识的排在最后


# ==============================================================================
# 辅助函数：编码器格式化和轨道匹配
# ==============================================================================


def format_codec_display(codec: str, is_atmos: bool = False) -> str:
    """
    格式化编码器名称用于显示

    映射规则：
    - dts_x → DTS:X
    - dts_hd_ma → DTS-HD MA
    - dts_hd → DTS-HD
    - dts → DTS
    - truehd → TrueHD (+ Atmos)
    - ac3 → AC3
    - eac3 → EAC3
    - hevc → HEVC
    - avc → AVC
    - pgs → PGS
    """
    codec_lower = codec.lower()

    display_map = {
        "dts_x": "DTS:X",
        "dts_hd_ma": "DTS-HD MA",
        "dts_hd": "DTS-HD",
        "dts": "DTS",
        "truehd": "TrueHD",
        "ac3": "AC3",
        "eac3": "EAC3",
        "aac": "AAC",
        "lpcm": "LPCM",
        "hevc": "HEVC",
        "avc": "AVC",
        "h264": "H.264",
        "h265": "H.265",
        "mpeg-2": "MPEG-2",
        "mpeg2video": "MPEG-2",
        "pgs": "PGS",
        "vobsub": "VobSub",
        "srt": "SRT",
    }

    formatted = display_map.get(codec_lower, codec.upper())

    if is_atmos:
        formatted += " Atmos"

    return formatted


def match_track_with_bdinfo(track: Track, bdinfo_tracks: List[Dict], used_indices: set) -> Tuple[Optional[int], Optional[Dict]]:
    """
    通过属性匹配轨道与 BDInfo 数据

    匹配优先级：
    1. 语言匹配（规范化变体）
    2. 编码器兼容（dts 兼容 dts_hd_ma）
    3. 声道匹配（如果可用）
    4. Atmos 标志匹配（TrueHD）
    5. 选择未使用的、码率最高的

    Returns: (index, bd_track) 或 (None, None)
    """
    candidates = []

    for idx, bd_track in enumerate(bdinfo_tracks):
        if idx in used_indices:
            continue  # 已被匹配，跳过

        # 语言匹配
        bd_lang = bd_track.get("language", "und")
        track_lang = track.language

        # 规范化语言（chi/zho/zh-Hans 都视为中文）
        lang_norm = {"chi": "zho", "zh-Hans": "zho", "zh-Hant": "zho", "en": "eng", "ja": "jpn", "ko": "kor"}
        bd_lang_norm = lang_norm.get(bd_lang, bd_lang)
        track_lang_norm = lang_norm.get(track_lang, track_lang)

        if bd_lang_norm != track_lang_norm and bd_lang != "und" and track_lang != "und":
            continue

        # 编码器兼容性
        bd_codec = bd_track.get("codec", "").lower()
        track_codec = track.codec.lower()

        # 检查编码器是否兼容
        codec_match = False

        # 容错：如果 BDInfo codec 解析失败（unknown），跳过 codec 检查
        if bd_codec == "unknown":
            codec_match = True  # 允许匹配，依赖其他条件（语言、声道、码率）
        elif bd_codec == track_codec:
            codec_match = True
        elif "dts" in bd_codec and "dts" in track_codec:
            codec_match = True
        elif "truehd" in bd_codec and "truehd" in track_codec:
            codec_match = True
        elif ("ac3" in bd_codec or "eac3" in bd_codec) and ("ac3" in track_codec or "eac3" in track_codec):
            codec_match = True

        if not codec_match:
            continue

        # 声道匹配（如果双方都有）
        if track.channels and bd_track.get("channels"):
            # 提取基础声道部分（去除 objects、-Atmos 和 -X 标识）
            track_channels_base = track.channels.split("+")[0].strip().replace("-Atmos", "").replace("-X", "")
            bd_channels_base = bd_track["channels"].split("+")[0].strip().replace("-Atmos", "").replace("-X", "")

            # 跳过 ffprobe 识别失败的情况（channels=0）
            if track_channels_base == "0ch":
                # ffprobe 无法识别声道信息，跳过声道验证
                pass
            elif track_channels_base != bd_channels_base:
                continue

        # TrueHD Atmos 标志匹配
        if "truehd" in track_codec:
            if track.is_atmos != bd_track.get("is_atmos", False):
                continue

        candidates.append((idx, bd_track))

    if not candidates:
        return None, None

    # 优先选择码率精确匹配的（误差在 5% 以内）
    if track.bitrate > 0:
        for idx, bd_track in candidates:
            bd_bitrate = bd_track.get("bitrate", 0)
            if bd_bitrate > 0:
                # 计算码率误差百分比
                error = abs(track.bitrate - bd_bitrate) / bd_bitrate if bd_bitrate > 0 else 1.0
                if error < 0.05:  # 5% 误差以内视为精确匹配
                    return (idx, bd_track)

    # 如果没有精确匹配，使用顺序匹配（第一个未使用的，即索引最小的）
    # 这样可以保持 BDInfo 的原始顺序
    candidates.sort(key=lambda x: x[0])  # 按 BDInfo 索引升序排列
    return candidates[0]


# ==============================================================================
# BDInfoParser 类
# ==============================================================================


class BDInfoParser:
    """BDInfo 文本解析器"""

    def __init__(self, bdinfo_path: str):
        self.path = bdinfo_path
        self.playlist_name = ""
        self.video_tracks = []
        self.audio_tracks = []
        self.subtitle_tracks = []

    def _normalize_unicode_spaces(self, text: str) -> str:
        """
        规范化 Unicode 空格和不可见字符为普通空格

        BDInfo 文件可能包含各种 Unicode 空格字符（如 EN SPACE U+2002），
        这会导致字符串匹配失败。此方法将所有 Unicode 空格类字符统一替换为普通空格。

        匹配范围：
        - \u00a0: NO-BREAK SPACE
        - \u2000-\u200f: 各种空格和格式字符（EN SPACE, EM SPACE, THIN SPACE, ZERO WIDTH SPACE 等）
        - \u2028-\u202f: 行/段落分隔符、窄空格等
        - \u205f: MEDIUM MATHEMATICAL SPACE
        - \u3000: IDEOGRAPHIC SPACE（全角空格）
        - \ufeff: ZERO WIDTH NO-BREAK SPACE (BOM)

        Args:
            text: 原始文本

        Returns:
            规范化后的文本
        """
        # 使用正则表达式匹配所有 Unicode 空格类字符
        # 包括：NO-BREAK SPACE, EN/EM SPACE, ZERO WIDTH SPACE, 全角空格等
        pattern = r"[\u00A0\u2000-\u200F\u2028-\u202F\u205F\u3000\uFEFF]"
        return re.sub(pattern, " ", text)

    def parse(self) -> Dict:
        """解析 BDInfo 文本"""
        try:
            with open(self.path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception as e:
            raise RuntimeError(f"无法读取 BDInfo 文件：{self.path}\n{e}")

        # 规范化 Unicode 空格字符（修复 EN SPACE 等特殊字符导致的匹配失败）
        content = self._normalize_unicode_spaces(content)

        # 提取 PLAYLIST 名称
        if match := re.search(r"Name:\s+(\d+\.MPLS)", content, re.IGNORECASE):
            self.playlist_name = match.group(1)

        # 解析 VIDEO 部分
        video_section = self._extract_section(content, "VIDEO:")
        if video_section:
            self.video_tracks = self._parse_video_tracks(video_section)

        # 解析 AUDIO 部分
        audio_section = self._extract_section(content, "AUDIO:")
        if audio_section:
            self.audio_tracks = self._parse_audio_tracks(audio_section)

        # 解析 SUBTITLES 部分
        subtitle_section = self._extract_section(content, "SUBTITLES:")
        if subtitle_section:
            self.subtitle_tracks = self._parse_subtitle_tracks(subtitle_section)

        return {"playlist": self.playlist_name, "video": self.video_tracks, "audio": self.audio_tracks, "subtitle": self.subtitle_tracks}

    def _extract_section(self, content: str, marker: str) -> str:
        """提取 AUDIO/SUBTITLES 段落"""
        pattern = rf"{re.escape(marker)}.*?(?=\n\n[A-Z]+:|$)"
        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        return match.group(0) if match else ""

    def _should_skip_line(self, line: str, section_marker: str) -> bool:
        """
        判断是否应跳过解析行（表头、分隔线、段落标题）

        Args:
            line: 待检查的行
            section_marker: 段落标记（如 "VIDEO:", "AUDIO:", "SUBTITLES:"）

        Returns:
            如果应跳过返回 True，否则返回 False
        """
        return not line.strip() or "Codec" in line or "-----" in line or section_marker in line

    def _parse_video_tracks(self, section: str) -> List[Dict]:
        """解析视频轨道"""
        tracks = []
        lines = section.split("\n")

        for line in lines:
            # 跳过表头、分隔线和段落标题
            if self._should_skip_line(line, "VIDEO:"):
                continue

            track = {}

            # Codec（从行内容判断）
            if "MPEG-2" in line or "mpeg2" in line.lower():
                track["codec"] = "mpeg-2"
            elif "AVC" in line or "H.264" in line:
                track["codec"] = "avc"
            elif "HEVC" in line or "H.265" in line:
                track["codec"] = "hevc"
            elif "VC-1" in line:
                track["codec"] = "vc-1"
            else:
                track["codec"] = "unknown"

            # Bitrate（使用统一的码率提取函数）
            track["bitrate"] = extract_bitrate_from_line(line)

            tracks.append(track)

        return tracks

    def _parse_audio_tracks(self, section: str) -> List[Dict]:
        """解析音频轨道"""
        tracks = []
        lines = section.split("\n")

        # ⚠ 顺序敏感：长关键词必须排在短关键词之前（如 "DTS:X" 先于 "DTS"）
        codec_rules = [
            ("TrueHD", "truehd"),
            ("Atmos", "truehd"),
            ("DTS:X", "dts_x"),
            ("DTS-HD Master", "dts_hd_ma"),
            ("DTS-HD MA", "dts_hd_ma"),
            ("LPCM", "lpcm"),
            ("Dolby Digital Plus", "eac3"),
            ("E-AC-3", "eac3"),
            ("DTS", "dts"),
            ("AC3", "ac3"),
            ("Dolby Digital", "ac3"),
            ("AAC", "aac"),
        ]

        for line in lines:
            # 跳过表头、分隔线和段落标题
            if self._should_skip_line(line, "AUDIO:"):
                continue

            track = {}

            # Codec（从行内容判断）
            # 注意：content 已在 parse() 方法中规范化过 Unicode 空格
            track["codec"] = "unknown"
            for keyword, codec in codec_rules:
                if keyword in line:
                    track["codec"] = codec
                    if codec == "truehd":
                        track["is_atmos"] = "Atmos" in line
                    break

            # Language（使用统一的正则常量和 bdinfo_language_to_code 函数）
            lang_match = re.search(AUDIO_LANGUAGES_PATTERN, line, re.IGNORECASE)
            if lang_match:
                lang_name = lang_match.group(1)
                track["language"] = bdinfo_language_to_code(lang_name)
            else:
                track["language"] = "und"

            # Bitrate（使用统一的码率提取函数）
            bitrate = extract_bitrate_from_line(line)
            track["bitrate"] = bitrate
            track["full_bitrate"] = bitrate  # 保存完整码率

            # 提取声道（2.0 / 5.1 / 7.1 / 7.1-Atmos / 7.1-X / 7.1+11 objects）
            channels_match = re.search(r"(\d+\.\d+(?:-Atmos|-X)?(?:\+\d+\s+objects)?)\s*/", line)
            if channels_match:
                track["channels"] = channels_match.group(1)
            else:
                track["channels"] = ""

            # TrueHD 特殊处理：提取净码率和 AC3 核心码率
            if track["codec"] == "truehd":
                # 格式：7.1+11 objects / 48 kHz / 3455 kbps / 16-bit (AC3 Core: 5.1-EX / 48 kHz / 448 kbps)
                # 或者：7.1 / 48 kHz / 3086 kbps / 16-bit (AC3 Embedded: 5.1 / 48 kHz / 448 kbps / DN -31dB)
                # 需要提取第二个 kHz 后面的码率（TrueHD 净码率）
                truehd_bitrate_match = re.search(r"/\s*\d+\s*kHz\s*/\s*(\d+)\s*kbps", line)
                if truehd_bitrate_match:
                    net_bitrate = int(truehd_bitrate_match.group(1))
                    track["net_bitrate"] = net_bitrate  # 保存净码率
                    # 暂时不覆盖bitrate，后续根据完整码率和净码率的关系判断
                else:
                    track["net_bitrate"] = track["bitrate"]  # 如果提取失败，净码率=完整码率
                    # 调试：TrueHD 码率匹配失败
                    debug_print(f"TrueHD 净码率匹配失败: {line}")

                # 提取 AC3 核心码率（支持 "AC3 Core" 和 "AC3 Embedded" 两种格式）
                ac3_core_match = re.search(r"AC3 (?:Core|Embedded):.*?(\d+)\s*kbps", line, re.IGNORECASE)
                if ac3_core_match:
                    track["ac3_core_bitrate"] = int(ac3_core_match.group(1))
                else:
                    track["ac3_core_bitrate"] = 0

            # 提取自定义描述（兼容方括号和无方括号格式）
            # 格式1: ... / 448 kbps / DN -31dB [央视国配]
            # 格式2: ... / 448 kbps / DN -31dB 央视国配
            custom_desc_match = re.search(r"\[(.*?)\]", line)
            if custom_desc_match:
                # 有方括号，提取方括号内的内容
                track["custom_desc"] = custom_desc_match.group(1)
            else:
                # 无方括号，提取行尾的中文描述
                chinese_desc_match = re.search(r"([\u4e00-\u9fff]+.*?)\s*$", line)
                if chinese_desc_match:
                    track["custom_desc"] = chinese_desc_match.group(1).strip()
                else:
                    track["custom_desc"] = ""

            # 检测导评轨道标识
            track["is_commentary"] = False
            if track["custom_desc"]:
                desc_normalized = traditional_to_simplified(track["custom_desc"]).lower()
                if any(keyword in desc_normalized for keyword in TRACK_KEYWORDS["commentary"]):
                    track["is_commentary"] = True
                    debug_print(f"检测到导评音轨: custom_desc='{track['custom_desc']}', is_commentary=True")
            # 同时检查原始行是否包含关键词（覆盖英文 BDInfo 的 Commentary 行）
            if not track["is_commentary"] and any(keyword in line.lower() for keyword in TRACK_KEYWORDS["commentary"]):
                track["is_commentary"] = True
                debug_print("检测到导评标识: line contains commentary keyword")

            # 调试：输出每个音轨的解析结果
            if track["custom_desc"]:
                debug_print(
                    f"音轨解析: codec={track['codec']}, lang={track['language']}, bitrate={track.get('bitrate', 0)}, custom_desc='{track['custom_desc']}', is_commentary={track['is_commentary']}"
                )

            tracks.append(track)

        return tracks

    def _parse_subtitle_tracks(self, section: str) -> List[Dict]:
        """解析字幕轨道"""
        tracks = []
        lines = section.split("\n")

        for line in lines:
            # 跳过表头、分隔线和段落标题
            if self._should_skip_line(line, "SUBTITLES:"):
                continue

            track = {}

            # Codec（通常是 "Presentation Graphics"）
            track["codec"] = "pgs"

            # Language（使用统一的正则常量和 bdinfo_language_to_code 函数）
            lang_match = re.search(SUBTITLE_LANGUAGES_PATTERN, line, re.IGNORECASE)
            if lang_match:
                lang_name = lang_match.group(1)
                track["language"] = bdinfo_language_to_code(lang_name)
            else:
                track["language"] = "und"

            # Bitrate（使用统一的码率提取函数）
            track["bitrate"] = extract_bitrate_from_line(line)

            # Description（清理描述中不需要的信息）
            desc_match = re.search(r"kbps\s+(.+?)\s*$", line, re.IGNORECASE)
            if desc_match:
                raw_desc = desc_match.group(1).strip()
                clean_desc = re.sub(r"\d+x\d+\s*(?:/\s*\d+\s*Captions?)?\s*(?:\(\d+\s*Forced\s*Captions?\))?", "", raw_desc, flags=re.IGNORECASE)
                clean_desc = clean_desc.replace("/", "").strip()

                track["custom_desc"] = clean_desc

                # 繁简转换（保留原始描述，转换用于识别）
                desc_normalized = traditional_to_simplified(track["custom_desc"])

                # 根据自定义描述智能设置语言标签（使用 LANGUAGE_CONFIG 中的关键词）
                chinese_config = LANGUAGE_VARIANTS
                if any(kw in desc_normalized for kw in chinese_config["zh-Hant"]["keywords"]):
                    track["language"] = "zh-Hant"
                elif any(kw in desc_normalized for kw in chinese_config["zh-Hans"]["keywords"]):
                    track["language"] = "zh-Hans"
            else:
                track["custom_desc"] = ""

            tracks.append(track)

        return tracks


def infer_original_lang_from_bdinfo(bdinfo_path: Path) -> str:
    """
    从 BDInfo 文件推断原语言

    策略：
    1. 解析 BDInfo 的 AUDIO 部分
    2. 获取第一个音轨的语言
    3. 如果失败，返回 "eng" 作为默认值

    Args:
        bdinfo_path: BDInfo 文件路径

    Returns:
        语言代码（如 "eng", "jpn", "chi"）
    """
    try:
        parser = BDInfoParser(str(bdinfo_path))
        data = parser.parse()

        if data.get("audio") and len(data["audio"]) > 0:
            first_audio = data["audio"][0]
            lang = first_audio.get("language", "eng")
            # 如果语言是 "und"（未定义），使用默认值
            return lang if lang != "und" else "eng"
        else:
            # 没有音轨，使用默认值
            return "eng"

    except Exception:
        # 解析失败，使用默认值
        return "eng"


# ==============================================================================
# 轨道 ID 解析器（支持范围语法）
# ==============================================================================


class IDParser:
    """
    轨道 ID 解析器

    支持批量 ID 语法：
    - 单个：S1
    - 逗号分隔：S1,S2,S3
    - 范围：S1-S5
    - 组合：S1,S3-S5,S7
    """

    @staticmethod
    def parse_ids(id_spec: str, tracks: List[Track]) -> List[str]:
        """
        解析批量轨道 ID 规范

        Args:
            id_spec: ID规范字符串（如 "S1-S5,S7"）
            tracks: 轨道列表（用于验证 ID 有效性）

        Returns:
            ID 字符串列表

        Raises:
            ValueError: 无效的 ID 规范

        Examples:
            >>> IDParser.parse_ids("S1", tracks)
            ["S1"]
            >>> IDParser.parse_ids("S1,S2,S3", tracks)
            ["S1", "S2", "S3"]
            >>> IDParser.parse_ids("S1-S5", tracks)
            ["S1", "S2", "S3", "S4", "S5"]
            >>> IDParser.parse_ids("S1,S3-S5,S7", tracks)
            ["S1", "S3", "S4", "S5", "S7"]
        """
        result = []
        seen = set()

        # 按逗号分割
        parts = [p.strip() for p in id_spec.split(",")]

        for part in parts:
            if "-" in part:
                # 范围处理：S1-S5
                range_parts = part.split("-")
                if len(range_parts) != 2:
                    raise ValueError(f"无效的范围格式：{part}（应为 ID1-ID2）")

                start_id = range_parts[0].strip()
                end_id = range_parts[1].strip()

                if not start_id or not end_id:
                    raise ValueError(f"范围不能为空：{part}")

                if start_id[0].upper() != end_id[0].upper():
                    raise ValueError(f"范围跨越不同类型：{part}（{start_id} 和 {end_id}）")

                try:
                    start_idx = int(start_id[1:])
                    end_idx = int(end_id[1:])
                except ValueError:
                    raise ValueError(f"无效的范围编号：{part}")

                if start_idx > end_idx:
                    raise ValueError(f"范围起始大于结束：{part}")

                # 生成范围内的所有 ID
                track_type_prefix = start_id[0].upper()
                for idx in range(start_idx, end_idx + 1):
                    track_id = f"{track_type_prefix}{idx}"
                    if track_id not in seen:
                        result.append(track_id)
                        seen.add(track_id)
            else:
                # 单个 ID
                if part and part not in seen:
                    result.append(part)
                    seen.add(part)

        if not result:
            raise ValueError(f"未找到匹配的轨道：{id_spec}")

        return result


# ==============================================================================
# InteractiveCLI 类
# ==============================================================================


class InteractiveCLI:
    """交互式命令行界面"""

    def __init__(self):
        self.console = Console()

    def _print_operation_result(self, action: str, target_tracks: List[Track], custom_msg: Optional[str] = None) -> None:
        """
        打印操作结果消息（单个/批量操作统一处理）

        Args:
            action: 操作动词（如 "重命名", "修改语言", "删除"）
            target_tracks: 目标轨道列表
            custom_msg: 自定义消息（可选，优先级高于 action）
        """
        if len(target_tracks) == 1:
            msg = custom_msg or f"已{action} {target_tracks[0].display_id}"
            self.console.print(f"[green]✓ {msg}[/green]")
        else:
            ids = ", ".join(t.display_id for t in target_tracks)
            msg = custom_msg or f"已批量{action} {len(target_tracks)} 个轨道 ({ids})"
            self.console.print(f"[green]✓ {msg}[/green]")

    def display_tracks(self, tracks: List[Track], title: str = "轨道配置", filtered_ids: Optional[set] = None, source_name: Optional[str] = None):
        """显示轨道表格

        Args:
            tracks: 要显示的轨道列表
            title: 表格标题
            filtered_ids: 被脚本筛选/当前未在工作集合中的轨道 id 集合（这些行将以灰色、带 "×" 前缀显示）
            source_name: 原盘名称（可选，用于在表格底部注脚显示，提示当前处理的原盘）
        """
        if not tracks:
            self.console.print(f"[yellow]没有轨道可显示[/yellow]")
            return

        # 预先计算格式列的最大宽度
        max_format_len = max((len(format_codec_display(t.codec, t.is_atmos if t.type == "audio" else False)) for t in tracks), default=0)

        # 设置格式列宽度（最小8，最大15）
        format_width = min(max(max_format_len + 2, 8), 15)

        caption = f"[dim]当前编辑原盘：{source_name}[/dim]" if source_name else None
        table = Table(title=title, show_header=True, header_style="bold cyan", caption=caption, caption_justify="right")
        table.add_column("#", style="dim", width=3, justify="right")
        table.add_column("ID", style="yellow", width=4)
        table.add_column("类型", width=6)
        table.add_column("格式", width=format_width)
        table.add_column("语言", width=10)
        table.add_column("轨道名", width=40)
        table.add_column("码率", width=12, justify="right")
        table.add_column("默认", width=4, justify="center")

        # 类型中文映射
        type_display = {"video": "视频", "audio": "音频", "subtitle": "字幕"}

        # 计算每种类型的索引
        type_counters = {"video": 0, "audio": 0, "subtitle": 0}
        for idx, track in enumerate(tracks, start=1):
            track.index_in_type = type_counters[track.type]
            type_counters[track.type] += 1

            track_name = truncate_to_display_width(track.name, 40, "...")

            # 格式列：显示编码信息
            format_str = format_codec_display(track.codec, track.is_atmos if track.type == "audio" else False)

            # 码率列：添加单位并格式化显示
            if track.bitrate:
                # 如果有小数部分，保留3位小数；如果是整数值，不显示小数点
                if track.bitrate % 1 != 0:
                    bitrate_str = f"{track.bitrate:.3f} kbps"
                else:
                    bitrate_str = f"{int(track.bitrate)} kbps"
            else:
                bitrate_str = "-"

            # 语言显示：字幕保持智能识别标签，音频/视频使用 pycountry 转换
            display_lang = get_language_tag(track.language, track.type)

            # 被过滤的轨道使用灰色和 "×" 前缀标记
            is_filtered = filtered_ids is not None and track.id in filtered_ids
            if is_filtered:
                id_cell = f"[dim]×{track.display_id}[/dim]"
                type_cell = f"[dim]{type_display.get(track.type, track.type)}[/dim]"
                format_cell = f"[dim]{format_str}[/dim]"
                lang_cell = f"[dim]{display_lang}[/dim]"
                name_cell = f"[dim]{track_name}[/dim]"
                bitrate_cell = f"[dim]{bitrate_str}[/dim]"
                default_cell = "[dim]✓[/dim]" if track.is_default else ""
            else:
                id_cell = track.display_id
                type_cell = type_display.get(track.type, track.type)
                format_cell = format_str
                lang_cell = display_lang
                name_cell = track_name
                bitrate_cell = bitrate_str
                default_cell = "✓" if track.is_default else ""

            table.add_row(
                str(idx),
                id_cell,
                type_cell,
                format_cell,
                lang_cell,
                name_cell,
                bitrate_cell,
                default_cell,
            )

        self.console.print(table)

    def _handle_move(self, parts: List[str], tracks: List[Track]) -> None:
        """
        处理移动轨道命令（m <ID> <位置>）

        Args:
            parts: 命令拆分后的部分列表（如 ["m", "A2", "1"]）
            tracks: 当前工作轨道列表（in-place 修改）

        Raises:
            ValueError: 参数不足或位置超出范围
        """
        if len(parts) < 3:
            raise ValueError("移动命令格式：m <ID> <位置>")
        track_id = parts[1]
        new_pos = int(parts[2]) - 1

        if new_pos < 0 or new_pos >= len(tracks):
            raise ValueError(f"位置超出范围（1-{len(tracks)}）")

        track = self._find_track(tracks, track_id)
        old_idx = tracks.index(track)
        tracks.insert(new_pos, tracks.pop(old_idx))
        self.console.print(f"[green]✓ 已移动 {track_id} 到位置 {new_pos + 1}[/green]")

    def _handle_rename(self, parts: List[str], tracks: List[Track]) -> None:
        """
        处理重命名轨道命令（r <ID> <名称>，支持批量 ID）

        Args:
            parts: 命令拆分后的部分列表（如 ["r", "S1,S2", "简中特效"]）
            tracks: 当前工作轨道列表（in-place 修改）

        Raises:
            ValueError: 参数不足
        """
        if len(parts) < 2:
            raise ValueError("重命名命令格式：r <ID> <名称>")
        id_spec = parts[1]
        new_name = parts[2] if len(parts) > 2 else ""
        track_ids = IDParser.parse_ids(id_spec, tracks)

        target_tracks = [self._find_track(tracks, tid) for tid in track_ids]
        for track in target_tracks:
            track.name = new_name

        if len(target_tracks) == 1:
            self._print_operation_result("重命名", target_tracks, f'已重命名 {target_tracks[0].display_id} 为 "{new_name}"')
        else:
            self._print_operation_result("重命名", target_tracks)

    def _handle_lang(self, parts: List[str], tracks: List[Track]) -> None:
        """
        处理修改语言标签命令（lang <ID_SPEC> <语言>，支持批量 ID）

        Args:
            parts: 命令拆分后的部分列表（如 ["lang", "S1,S3-S5", "zh-Hans"]）
            tracks: 当前工作轨道列表（in-place 修改）

        Raises:
            ValueError: 参数不足
        """
        if len(parts) < 3:
            raise ValueError("语言命令格式：lang <ID> <语言代码>")
        id_spec = parts[1]
        new_lang = parts[2]
        track_ids = IDParser.parse_ids(id_spec, tracks)

        target_tracks = [self._find_track(tracks, tid) for tid in track_ids]
        for track in target_tracks:
            track.language = new_lang
            if not track.custom_desc:
                track.name = track.generate_track_name()

        if len(target_tracks) == 1:
            self._print_operation_result("修改语言", target_tracks, f"已修改 {target_tracks[0].display_id} 语言为 {new_lang}")
        else:
            custom_msg = f"已批量修改 {len(target_tracks)} 个轨道语言为 {new_lang}"
            self._print_operation_result("修改语言", target_tracks, custom_msg)

    def _handle_delete(self, parts: List[str], tracks: List[Track]) -> None:
        """
        处理删除轨道命令（d <ID_SPEC>，支持批量 ID）

        Args:
            parts: 命令拆分后的部分列表（如 ["d", "S7-S10"]）
            tracks: 当前工作轨道列表（in-place 修改）

        Raises:
            ValueError: 参数不足
        """
        if len(parts) < 2:
            raise ValueError("删除命令格式：d <ID>")
        id_spec = parts[1]
        track_ids = IDParser.parse_ids(id_spec, tracks)

        target_tracks = [self._find_track(tracks, tid) for tid in track_ids]
        for track in target_tracks:
            tracks.remove(track)

        self._print_operation_result("删除", target_tracks)

    def _handle_default(self, parts: List[str], tracks: List[Track]) -> None:
        """
        处理设置/取消默认轨道命令（default <ID_SPEC>，支持批量 ID，切换逻辑）

        Args:
            parts: 命令拆分后的部分列表（如 ["default", "A1"]）
            tracks: 当前工作轨道列表（in-place 修改）

        Raises:
            ValueError: 参数不足
        """
        if len(parts) < 2:
            raise ValueError("默认命令格式：default <ID>")
        id_spec = parts[1]
        track_ids = IDParser.parse_ids(id_spec, tracks)

        # 更新所有轨道的 index_in_type
        type_counters = {"video": 0, "audio": 0, "subtitle": 0}
        for track in tracks:
            track.index_in_type = type_counters[track.type]
            type_counters[track.type] += 1

        target_tracks = [self._find_track(tracks, tid) for tid in track_ids]
        target_track_ids = {t.id for t in target_tracks}

        # 批量设置/取消默认（切换逻辑）
        operations = []
        for track in target_tracks:
            if track.is_default:
                operations.append((track, "cancel"))
            else:
                operations.append((track, "set"))

        # 收集需要设置默认的类型
        types_to_set = set()
        for track, op in operations:
            if op == "set":
                types_to_set.add(track.type)

        # 清除同类型的其他默认标记
        cleared_tracks = []
        for track_type in types_to_set:
            for t in tracks:
                if t.type == track_type and t.is_default and t.id not in target_track_ids:
                    t.is_default = False
                    cleared_tracks.append(t)

        # 执行所有操作
        results = []
        for track, op in operations:
            if op == "cancel":
                track.is_default = False
                results.append(f"取消 {track.display_id}")
            else:
                track.is_default = True
                results.append(f"设置 {track.display_id}")

        for track in cleared_tracks:
            results.append(f"取消 {track.display_id}")

        total_affected = len(target_tracks) + len(cleared_tracks)
        if total_affected == 1:
            self.console.print(f"[green]✓ 已{results[0]}[/green]")
        else:
            self.console.print(f"[green]✓ 已批量处理 {total_affected} 个轨道：{', '.join(results)}[/green]")

    def edit_loop(self, tracks: List[Track], view_data: Optional[Dict] = None, source_name: Optional[str] = None) -> Optional[List[Track]]:
        """交互式编辑循环

        Args:
            tracks: 当前工作轨道列表（视频+音频+字幕，已排序）
            view_data: 视图辅助数据（包含 sorted_all / unsorted_all / 排序键函数）
            source_name: 原盘名称（可选，向下传递给表格用于显示）

        Returns:
            修改后的轨道列表；当用户选择返回上一阶段时返回 None
        """
        if not tracks:
            return tracks

        # 使用深拷贝保存初始状态
        original_tracks = copy.deepcopy(tracks)

        # 撤销历史栈（每次成功编辑前压入快照）
        history_stack: List[List[Track]] = []

        # 从视图数据中提取完整列表和排序键
        sorted_all: Optional[List[Track]] = None
        unsorted_all: Optional[List[Track]] = None
        audio_sort_key = None
        subtitle_sort_key = None
        current_view = "sorted"

        if view_data:
            sorted_all = view_data.get("sorted_all")
            unsorted_all = view_data.get("unsorted_all")
            audio_sort_key = view_data.get("audio_sort_key")
            subtitle_sort_key = view_data.get("subtitle_sort_key")
            # 若无排序后视图但有原始视图，则默认视图为 orig
            if not sorted_all and unsorted_all:
                current_view = "orig"

        self.console.print("\n[bold cyan]轨道交互编辑模式[/bold cyan]")
        self.console.print("命令：")
        self.console.print("  m <ID> <位置>     - 移动轨道（如：m A2 1）")
        self.console.print("  r <ID> <名称>     - 重命名轨道（如：r S1 简中特效）")
        self.console.print("  lang <ID> <语言>  - 修改语言标签（如：lang S1 zh-Hans）")
        self.console.print("  d <ID>            - 删除轨道（如：d A3）")
        self.console.print("  default <ID>      - 设置/取消默认轨道（如：default A1）")
        if view_data:
            self.console.print("  all               - 查看排序后完整轨道（含被筛选的，可 add 恢复）")
            self.console.print("  orig              - 切换为原始顺序视图（编辑原始顺序）")
            self.console.print("  sorted            - 切换为排序后顺序视图（编辑排序结果）")
            self.console.print("  view              - 在原始顺序 / 排序后顺序之间切换")
        self.console.print("  undo              - 撤销上一步操作")
        self.console.print("  reset             - 重置为初始排序")
        self.console.print("  back              - 返回上一阶段")
        self.console.print("  done 或直接回车   - 完成编辑")
        self.console.print("\n[dim]批量操作（支持 d, r, lang, default 命令）：[/dim]")
        self.console.print("[dim]  - 逗号分隔：d S1,S2,S3[/dim]")
        self.console.print("[dim]  - 范围匹配：d S7-S10[/dim]")
        self.console.print("[dim]  - 组合使用：lang S1,S3-S5 zh-Hans[/dim]")
        self.console.print("[dim]  - 切换默认：default S1,S2（已默认则取消）[/dim]\n")

        while True:
            self.console.print()
            self.display_tracks(tracks, source_name=source_name)

            cmd = interactive_input("\n>>> ").strip()

            # 结束或返回
            if not cmd or cmd.lower() == "done":
                break
            if cmd.lower() == "back":
                return None

            # 撤销
            if cmd.lower() == "undo":
                if not history_stack:
                    self.console.print("[yellow]没有可撤销的操作[/yellow]")
                    continue
                tracks = history_stack.pop()
                self.console.print("[green]✓ 已撤销上一步操作[/green]")
                continue

            # 重置
            if cmd.lower() == "reset":
                history_stack.append(copy.deepcopy(tracks))
                tracks = copy.deepcopy(original_tracks)
                self.console.print("[green]✓ 已重置为初始排序[/green]")
                continue

            # 视图相关命令
            lower_cmd = cmd.lower()
            # 匹配 orig, sorted, view 以及所有以 all 开头的命令 (如 all, all orig, all sorted)
            if view_data and (lower_cmd in ("orig", "sorted", "view") or lower_cmd.startswith("all")):

                # 1. 处理进入全量视图 (all 及其组合命令)
                if lower_cmd.startswith("all"):
                    if "orig" in lower_cmd:
                        initial_view = "orig"
                    elif "view" in lower_cmd:
                        initial_view = "orig" if current_view == "sorted" else "sorted"
                    else:
                        initial_view = "sorted"
                    self._show_all_view(
                        working_tracks=tracks,
                        sorted_all=sorted_all,  # 传入排序后列表
                        unsorted_all=unsorted_all,  # 传入原始列表
                        history_stack=history_stack,
                        audio_sort_key=audio_sort_key,
                        subtitle_sort_key=subtitle_sort_key,
                        title="完整轨道",
                        initial_view=initial_view,  # 告知子循环初始显示哪个视图
                        source_name=source_name,  # 传递给子视图
                    )
                    continue

                # 2. orig / sorted / view：仅切换当前工作列表的顺序视图
                if lower_cmd in ("orig", "sorted", "view"):
                    # view：在当前视图的原始/排序之间切换
                    if lower_cmd == "view":
                        lower_cmd = "orig" if current_view == "sorted" else "sorted"

                    if lower_cmd == "orig":
                        if not unsorted_all:
                            self.console.print("[yellow]当前无原始顺序视图可用[/yellow]")
                            continue
                        order_map = {t.id: idx for idx, t in enumerate(unsorted_all)}
                        tracks.sort(key=lambda t: order_map.get(t.id, len(order_map) + 1))
                        current_view = "orig"
                        self.console.print("[green]✓ 已切换为原始顺序视图[/green]")
                        continue

                    if lower_cmd == "sorted":
                        if not sorted_all:
                            self.console.print("[yellow]当前无排序后视图可用[/yellow]")
                            continue
                        order_map = {t.id: idx for idx, t in enumerate(sorted_all)}
                        tracks.sort(key=lambda t: order_map.get(t.id, len(order_map) + 1))
                        current_view = "sorted"
                        self.console.print("[green]✓ 已切换为排序后顺序视图[/green]")
                        continue

            # 常规编辑命令：执行前保存快照，失败时回滚
            try:
                history_stack.append(copy.deepcopy(tracks))

                parts = cmd.split(maxsplit=2)
                if len(parts) == 0:
                    raise ValueError("空命令")

                action = parts[0].lower()

                # 编辑命令分发字典
                cmd_handlers = {
                    "m": self._handle_move,
                    "r": self._handle_rename,
                    "lang": self._handle_lang,
                    "d": self._handle_delete,
                    "default": self._handle_default,
                }

                handler = cmd_handlers.get(action)
                if not handler:
                    raise ValueError(f"未知命令：{action}")

                handler(parts, tracks)

            except Exception as e:
                # 命令失败时回滚快照
                if history_stack:
                    history_stack.pop()
                self.console.print(f"[red]✗ 错误：{e}[/red]")

        return tracks

    def _resort_working_tracks(self, tracks: List[Track], audio_sort_key, subtitle_sort_key) -> None:
        """按排序键对当前工作列表中的音频/字幕轨道重新排序（原地修改）。"""
        videos = [t for t in tracks if t.type == "video"]
        audios = [t for t in tracks if t.type == "audio"]
        subs = [t for t in tracks if t.type == "subtitle"]

        if audio_sort_key:
            audios = sorted(audios, key=audio_sort_key)
        if subtitle_sort_key:
            subs = sorted(subs, key=subtitle_sort_key)

        tracks.clear()
        tracks.extend(videos + audios + subs)

    def _show_all_view(
        self,
        working_tracks: List[Track],
        sorted_all: List[Track],
        unsorted_all: List[Track],
        history_stack: List[List[Track]],
        audio_sort_key,
        subtitle_sort_key,
        title: str,
        initial_view: str = "sorted",
        source_name: Optional[str] = None,
    ) -> None:
        """all 视图子循环，支持 add 命令恢复被过滤/删除的轨道。

        Args:
            working_tracks: 当前可编辑的工作轨道列表（会被原地修改）
            sorted_all: 排序后的完整参考列表
            unsorted_all: 原始顺序的完整参考列表
            history_stack: 撤销历史栈
            audio_sort_key: 音轨排序键函数
            subtitle_sort_key: 字幕排序键函数
            title: 表格标题
            initial_view: 初始显示的视图类型 ("sorted" 或 "orig")
            source_name: 原盘名称（可选，向下传递给表格用于显示）
        """
        current_view = initial_view

        while True:
            self.console.print()

            all_tracks = sorted_all if current_view == "sorted" else unsorted_all
            view_title = f"{title} [{'已排序' if current_view == 'sorted' else '原始顺序'}]"

            working_ids = {t.id for t in working_tracks}
            current_filtered_ids = {t.id for t in all_tracks if t.id not in working_ids}

            self.display_tracks(all_tracks, view_title, filtered_ids=current_filtered_ids, source_name=source_name)
            self.console.print("[dim]命令: add <ID> (恢复轨道) | orig / sorted / view (切换视图) | 回车 (返回)[/dim]")

            cmd = interactive_input(">>> ").strip().lower()
            if not cmd or cmd in ("done", "quit", "exit", "back"):
                break

            # 内部无缝切换视图
            if cmd == "orig":
                current_view = "orig"
                continue
            elif cmd == "sorted":
                current_view = "sorted"
                continue
            elif cmd == "view":
                current_view = "orig" if current_view == "sorted" else "sorted"
                continue

            # 只处理 add 命令，支持批量 ID 语法
            if not cmd.startswith("add "):
                self.console.print("[yellow]提示: 请输入 add <ID>，或输入 orig/sorted 切换视图，直接回车返回[/yellow]")
                continue

            id_spec = cmd.split(maxsplit=1)[1].strip()
            try:
                # 按照当前选中的视图(all_tracks)重构动态 ID 映射
                type_counters = {"video": 0, "audio": 0, "subtitle": 0}
                for t in all_tracks:
                    t.index_in_type = type_counters[t.type]
                    type_counters[t.type] += 1

                track_ids = IDParser.parse_ids(id_spec, all_tracks)

                target_tracks: List[Track] = []
                for tid in track_ids:
                    target = self._find_track(all_tracks, tid)
                    if target.id in working_ids:
                        self.console.print(f"[yellow]轨道 {tid} 已在当前配置中[/yellow]")
                        continue
                    target_tracks.append(target)

                if not target_tracks:
                    continue

                # 保存撤销快照
                history_stack.append(copy.deepcopy(working_tracks))

                for target in target_tracks:
                    new_track = copy.deepcopy(target)
                    working_tracks.append(new_track)
                    working_ids.add(new_track.id)

                # 按排序键重新排序工作列表
                self._resort_working_tracks(working_tracks, audio_sort_key, subtitle_sort_key)

                self.console.print(f"[green]✓ 已恢复 {', '.join(t.display_id for t in target_tracks)} 并重新排序[/green]")

            except Exception as e:
                self.console.print(f"[red]✗ 错误：{e}[/red]")

    def _find_track(self, tracks: List[Track], display_id: str) -> Track:
        """通过 A1/S2 查找轨道"""
        type_map = {"A": "audio", "S": "subtitle", "V": "video"}

        if len(display_id) < 2 or display_id[0].upper() not in type_map:
            raise ValueError(f"无效的轨道 ID：{display_id}")

        track_type = type_map[display_id[0].upper()]
        try:
            index = int(display_id[1:]) - 1
        except ValueError:
            raise ValueError(f"无效的轨道编号：{display_id}")

        same_type_tracks = [t for t in tracks if t.type == track_type]

        if index < 0 or index >= len(same_type_tracks):
            raise ValueError(f"轨道 {display_id} 不存在")

        return same_type_tracks[index]

    def select_from_candidates(self, candidates: List[Dict], prompt: str, auto_skip: bool = True) -> int:
        """从候选列表中选择"""
        if not candidates:
            return -1

        if auto_skip and len(candidates) == 1:
            return 0

        # 显示候选表格
        table = Table(title=prompt, show_header=True, header_style="bold cyan")
        table.add_column("ID", style="yellow", width=4)
        table.add_column("MPLS文件", width=15)
        table.add_column("时长", width=12)
        table.add_column("大小", width=12)
        table.add_column("章节", width=6, justify="right")
        table.add_column("M2TS文件", width=20)

        for i, cand in enumerate(candidates):
            table.add_row(
                str(i + 1),
                cand.get("mpls_name", ""),
                format_duration(cand.get("duration", 0)),
                format_size(cand.get("size", 0)),
                str(cand.get("chapters", 0)),
                ",".join(cand.get("m2ts_files", [])[:3]) + ("..." if len(cand.get("m2ts_files", [])) > 3 else ""),
            )

        self.console.print(table)

        # 用户选择
        while True:
            choice = interactive_input(f"\n请选择正片 [1-{len(candidates)}，默认 1，back 返回]: ").strip()
            if choice.lower() == "back":
                return -2  # 触发 back 返回上一级
            if not choice:
                return 0

            try:
                idx = int(choice) - 1
                if 0 <= idx < len(candidates):
                    return idx
                else:
                    self.console.print(f"[red]请输入 1-{len(candidates)} 之间的数字[/red]")
            except ValueError:
                self.console.print("[red]请输入有效的数字，或输入 back 返回[/red]")


# ==============================================================================
# 元数据提取函数
# ==============================================================================


def extract_metadata(bdmv_path: Path) -> Dict:
    """从 BDMV/META/DL 提取元数据"""
    meta_path = bdmv_path / "META" / "DL"
    metadata = {"title": "", "cover_path": None}

    if not meta_path.exists():
        # 使用 BDMV 父目录名作为标题
        metadata["title"] = bdmv_path.parent.name
        return metadata

    # 提取封面（选择最大的图片）
    cover_size = 0
    for img_ext in [".jpg", ".JPG", ".jpeg", ".JPEG", ".png", ".PNG"]:
        for img_file in meta_path.glob(f"*{img_ext}"):
            size = img_file.stat().st_size
            if size > cover_size:
                metadata["cover_path"] = img_file
                cover_size = size

    # 提取标题（优先级：bdmt_eng.xml > bdmt_zho.xml > 其他）
    priority = ["bdmt_eng.xml", "bdmt_zho.xml"]
    xml_files = list(meta_path.glob("*.xml"))

    # 按优先级排序
    def xml_priority(xml_path):
        name = xml_path.name.lower()
        if name in priority:
            return priority.index(name)
        return len(priority)

    xml_files.sort(key=xml_priority)

    for xml_file in xml_files:
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
            ns = {"di": "urn:BDA:bdmv;discinfo"}
            title_elem = root.find(".//di:name", ns)
            if title_elem is not None and title_elem.text:
                metadata["title"] = title_elem.text.strip()
                break
        except Exception:
            continue

    # 如果没有找到标题，使用目录名
    if not metadata["title"]:
        metadata["title"] = bdmv_path.parent.name

    return metadata


def generate_ogm_chapters(chapter: Chapter, output_path: str) -> str:
    """生成 OGM 格式的章节文件"""
    timestamps = chapter.get_chapter_timestamps()

    if not timestamps:
        return None

    chapter_lines = []
    for i, ts in enumerate(timestamps, start=1):
        minutes, seconds = divmod(ts, 60)
        hours, minutes = divmod(int(minutes), 60)

        # OGM 格式：CHAPTER01=00:00:00.000
        time_str = f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"
        chapter_lines.append(f"CHAPTER{i:02d}={time_str}")
        chapter_lines.append(f"CHAPTER{i:02d}NAME=Chapter {i:02d}")

    # 写入临时章节文件
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(chapter_lines))

    return output_path


def find_bdmv_in_mount(mount_point: Path) -> Path:
    """
    在 ISO 挂载点中查找 BDMV 目录

    搜索顺序：
    1. mount_point/BDMV（直接）
    2. mount_point/*/BDMV（一层嵌套）

    Returns: BDMV 目录的 Path 对象
    Raises: RuntimeError 如果未找到
    """
    # 检查直接 BDMV
    if (mount_point / "BDMV" / "PLAYLIST").exists():
        return mount_point / "BDMV"

    # 检查一层嵌套（例如 ISO 内有包装文件夹）
    try:
        for subdir in mount_point.iterdir():
            if subdir.is_dir() and (subdir / "BDMV" / "PLAYLIST").exists():
                return subdir / "BDMV"
    except Exception:
        pass

    raise RuntimeError(f"未在 ISO 中找到有效的 BDMV 结构\n" f"  挂载点：{mount_point}\n" f"  请检查 ISO 文件是否为标准蓝光格式")


# ==============================================================================
# mkvmerge 命令构建函数
# ==============================================================================


def guess_attachment_mime_type(path: Path) -> str:
    """为 mkvmerge 附件推断 MIME type。"""
    mime_type, _ = mimetypes.guess_type(str(path))
    if mime_type:
        return mime_type

    suffix_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }
    return suffix_map.get(path.suffix.lower(), "application/octet-stream")


def build_mkvmerge_command(
    output_path: str, mpls_path: str, tracks: List[Track], title: str, cover_path: Optional[Path] = None, chapters_file: Optional[str] = None
) -> List[str]:
    """
    构建 mkvmerge 命令

    Args:
        output_path: 输出文件路径
        mpls_path: MPLS 文件路径
        tracks: 轨道列表
        title: MKV 标题
        cover_path: 封面路径（可选）
        chapters_file: 章节文件路径（可选）

    Returns:
        mkvmerge 命令参数列表
    """
    cmd = [find_executable("mkvmerge") or "mkvmerge", "-o", output_path]

    # 标题
    if title:
        cmd.extend(["--title", title])

    # 章节
    if chapters_file and Path(chapters_file).exists():
        cmd.extend(["--chapters", chapters_file])

    # 轨道选择
    video_ids = sorted(t.id for t in tracks if t.type == "video")
    audio_ids = sorted(t.id for t in tracks if t.type == "audio")
    subtitle_ids = sorted(t.id for t in tracks if t.type == "subtitle")

    if video_ids:
        cmd.extend(["--video-tracks", ",".join(str(id) for id in video_ids)])
    else:
        cmd.append("--no-video")

    if audio_ids:
        cmd.extend(["--audio-tracks", ",".join(str(id) for id in audio_ids)])
    else:
        cmd.append("--no-audio")

    if subtitle_ids:
        cmd.extend(["--subtitle-tracks", ",".join(str(id) for id in subtitle_ids)])
    else:
        cmd.append("--no-subtitles")

    # 轨道排序（明确指定顺序）
    track_order = ",".join(f"0:{t.id}" for t in tracks)
    cmd.extend(["--track-order", track_order])

    # 轨道属性
    for track in tracks:
        cmd.extend(track.to_mkvmerge_args())

    # 封面附件
    if cover_path and cover_path.exists():
        cmd.extend(
            [
                "--attachment-mime-type",
                guess_attachment_mime_type(cover_path),
                "--attachment-name",
                "Cover.jpg",
                "--attach-file",
                str(cover_path),
            ]
        )

    cmd.append(mpls_path)
    return cmd


def run_mkvmerge_with_progress(cmd: List[str]) -> bool:
    """执行 mkvmerge 并显示进度，兼容警告状态码，使用 tmp 缓存写入并清理空文件夹"""
    console = Console()

    # 动态拦截并修改输出路径为 .tmp
    final_output = None
    tmp_output = None
    try:
        out_idx = cmd.index("-o") + 1
        final_output = Path(cmd[out_idx])
        tmp_output = final_output.with_suffix(final_output.suffix + ".tmp")
        cmd[out_idx] = str(tmp_output)  # 篡改传给 mkvmerge 的路径
    except (ValueError, IndexError):
        pass

    # 内部清理辅助函数：删文件 + 删空文件夹
    def _cleanup_tmp():
        if tmp_output and tmp_output.exists():
            try:
                tmp_output.unlink()
                console.print(f"  [dim]已清理未完成的临时文件: {tmp_output.name}[/dim]")
                parent_dir = tmp_output.parent
                if parent_dir.exists() and not any(parent_dir.iterdir()):
                    parent_dir.rmdir()
                    console.print(f"  [dim]已清理空文件夹: {parent_dir.name}[/dim]")
            except Exception:
                pass

    max_retries = 2
    for attempt in range(max_retries):
        try:
            result = subprocess.run(cmd, check=False)  ## 直接运行 mkvmerge，不捕获输出（让它直接输出到终端）

            # mkvmerge 状态码: 0=完美, 1=有警告但成功, 2=严重错误
            if result.returncode in (0, 1):
                if result.returncode == 1:
                    console.print("  [yellow]提示：mkvmerge 报告了非致命警告，但文件已成功生成。[/yellow]")

                # 执行成功，重命名回原格式
                if tmp_output and tmp_output.exists():
                    tmp_output.replace(final_output)
                return True
            else:
                # 返回码 >= 2 才是真正的失败
                if attempt < max_retries - 1:
                    console.print("[yellow]⚠ 遇到错误或文件被占用，等待 2 秒后重试...[/yellow]")
                    time.sleep(2)
                    continue

                _cleanup_tmp()
                return False

        except KeyboardInterrupt:
            console.print(f"\n[yellow]检测到手动中断，正在清理...[/yellow]")
            _cleanup_tmp()
            raise

        except Exception as e:
            console.print(f"\n[red]✗ 执行 mkvmerge 时出错：{e}[/red]")
            _cleanup_tmp()
            return False

    return False


# ==============================================================================
# MPLS 扫描和正片判定
# ==============================================================================


def scan_bluray_sources(root_dir: Path) -> List[Dict]:
    """
    扫描目录中的所有蓝光原盘和 ISO

    Args:
        root_dir: 根目录路径

    Returns:
        原盘列表：[{
            "path": Path,           # 原盘/ISO 路径
            "type": "dir|iso",      # 类型
            "name": str,            # 名称（用于匹配 BDInfo）
            "bdmv_path": Path       # BDMV 目录（仅 dir 类型，ISO 需挂载后获取）
        }]
    """
    sources = []
    seen = set()  # 去重（避免同一原盘被多次检测）

    # 递归遍历
    for item in root_dir.rglob("*"):
        try:
            # 跳过 BACKUP 目录（蓝光原盘的备份目录，避免误识别）
            if item.is_dir() and item.name.upper() == "BACKUP":
                continue

            # ISO 文件
            if item.is_file() and item.suffix.lower() == ".iso":
                name = item.stem
                if name not in seen:
                    sources.append({"path": item, "type": "iso", "name": name, "bdmv_path": None})  # ISO 需挂载后才能获取
                    seen.add(name)

            # 目录（检查 BDMV/PLAYLIST）
            elif item.is_dir():
                if (item / "PLAYLIST").exists():
                    # item 本身就是 BDMV 目录
                    name = item.parent.name
                    if name not in seen:
                        sources.append({"path": item.parent, "type": "dir", "name": name, "bdmv_path": item})
                        seen.add(name)
                elif (item / "BDMV" / "PLAYLIST").exists():
                    # item 是影片目录，BDMV 在子目录中
                    name = item.name
                    if name not in seen:
                        sources.append({"path": item, "type": "dir", "name": name, "bdmv_path": item / "BDMV"})
                        seen.add(name)
        except (PermissionError, OSError):
            # 跳过无权限或损坏的目录/文件
            continue

    return sources


def find_bdinfo_for_source(source: Dict, bdinfo_dir: Optional[Path] = None) -> Optional[Path]:
    """
    为原盘查找对应的 BDInfo 文件

    匹配策略（按优先级）：
    1. 统一目录优先：bdinfo_dir/{name}.txt 或 bdinfo_dir/{name}_bdinfo.txt
    2. 本地目录同名匹配：
       - 若为文件夹：同时查找【文件夹内】与【文件夹同级】的 {name}.txt
       - 若为 ISO：查找【ISO 同级】的 {name}.txt
    3. 兜底回溯：从原盘目录向上查找通用名称 bdinfo.txt
    """
    path, name = Path(source["path"]), source["name"]
    names = [f"{name}.txt", f"{name}_bdinfo.txt"]
    candidates = []

    # 1. 统一 BDInfo 目录 (最高优先级)
    if bdinfo_dir:
        candidates.extend(bdinfo_dir / n for n in names)

    # 2 & 3. 原盘本地目录匹配
    if path.exists():
        parent = path.parent

        if path.is_dir():
            # 针对文件夹：检查内部专属文本及通用 bdinfo.txt
            candidates.extend(path / n for n in names + ["bdinfo.txt"])
            # 针对文件夹：检查同级专属文本
            candidates.extend(parent / n for n in names)
        else:
            # 针对 ISO：检查同级专属文本及通用 bdinfo.txt
            candidates.extend(parent / n for n in names + ["bdinfo.txt"])

        # 4. 向上回溯查找通用 bdinfo.txt (最多回溯 3 层)
        for _ in range(3):
            if parent.parent == parent:
                break
            parent = parent.parent
            candidates.append(parent / "bdinfo.txt")

    # 统一验证：利用 next() 结合生成器进行懒加载验证，返回第一个存在的文件
    return next((c for c in candidates if c.is_file()), None)


def prompt_for_missing_bdinfo_text(
    source_name: str,
    console: Console,
    cache_dir: Optional[Path] = None,
    input_fn: Callable[[str], str] = input,
) -> Optional[Path]:
    """
    本地缺少 BDInfo 文本时，引导用户在控制台粘贴完整内容并缓存为临时 txt

    使用方式：
    1. 将完整 BDInfo 文本直接粘贴到控制台
    2. 单独输入一行 EOF 结束
    3. 第一行直接回车表示取消
    """
    console.print(f"[yellow]未找到 BDInfo 文本：{source_name}[/yellow]")
    console.print("  [cyan]请将完整 BDInfo 文本粘贴到控制台[/cyan]")
    console.print(f"  [dim]粘贴完成后，单独输入 {BDINFO_PASTE_SENTINEL} 结束；第一行直接回车则跳过该原盘[/dim]")

    lines = []
    while True:
        try:
            line = input_fn("")
        except EOFError:
            if not lines:
                return None
            break

        if not lines and not line.strip():
            return None

        if line.strip() == BDINFO_PASTE_SENTINEL:
            break

        lines.append(line)

    content = "\n".join(lines).strip()
    if not content:
        return None

    target_dir = cache_dir or (Path(tempfile.gettempdir()) / "bluray_remux_bdinfo")
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_name = sanitize_filename(source_name) or "bdinfo"
    bdinfo_path = target_dir / f"{safe_name}_pasted_bdinfo.txt"
    bdinfo_path.write_text(content + "\n", encoding="utf-8")

    console.print(f"[green]✓ 已缓存粘贴的 BDInfo：{bdinfo_path}[/green]")
    return bdinfo_path


def scan_mpls_files(bdmv_path: Path) -> List[Dict]:
    """扫描所有 MPLS 文件并计算候选正片"""
    console = Console()
    playlist_path = bdmv_path / "PLAYLIST"
    stream_path = bdmv_path / "STREAM"

    if not playlist_path.exists():
        console.print(f"[red]错误：未找到 PLAYLIST 目录：{playlist_path}[/red]")
        return []

    mpls_files = list(playlist_path.glob("*.mpls"))
    if not mpls_files:
        console.print(f"[red]错误：未找到任何 MPLS 文件[/red]")
        return []

    console.print(f"扫描到 {len(mpls_files)} 个 MPLS 文件...")

    candidates = []

    for mpls_file in mpls_files:
        try:
            chapter = Chapter(str(mpls_file))
            chapter.get_pid_to_language()

            duration = chapter.get_total_time_no_repeat()
            chapter_count = chapter.get_chapter_count()

            # 计算 M2TS 文件总大小
            total_size = 0
            m2ts_files = chapter.get_m2ts_files()
            for m2ts_name in m2ts_files:
                m2ts_path = stream_path / f"{m2ts_name}.m2ts"
                if m2ts_path.exists():
                    total_size += m2ts_path.stat().st_size
                else:
                    console.print(f"[yellow]警告：M2TS 文件不存在：{m2ts_path.name}[/yellow]")

            # 正片判定指标
            indicator = (
                duration * (1 + chapter_count / 5) * mpls_file.stat().st_size * total_size  # 时长（秒）  # 章节加权  # MPLS 文件大小  # M2TS 总大小
            )

            # 筛选条件
            if duration >= MIN_FEATURE_DURATION and chapter_count >= 3:
                candidates.append(
                    {
                        "mpls_path": mpls_file,
                        "mpls_name": mpls_file.name,
                        "duration": duration,
                        "size": total_size,
                        "chapters": chapter_count,
                        "m2ts_files": m2ts_files,
                        "indicator": indicator,
                        "chapter": chapter,
                    }
                )

        except Exception as e:
            console.print(f"[yellow]警告：解析 {mpls_file.name} 失败 - {e}[/yellow]")
            continue

    # 按指标降序排序
    candidates.sort(key=lambda x: x["indicator"], reverse=True)

    return candidates


# ==============================================================================
# 音轨扫描（ffprobe）
# ==============================================================================


# 声道数 → 显示文本映射
_CHANNEL_MAP = {1: "1.0", 2: "2.0", 6: "5.1", 7: "6.1", 8: "7.1"}


def scan_tracks_with_ffprobe(m2ts_path: Path, chapter: Chapter) -> List[Track]:
    """使用 ffprobe 扫描轨道"""
    cmd = [find_executable("ffprobe") or "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(m2ts_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, encoding="utf-8")
        data = json.loads(result.stdout)
    except Exception as e:
        raise RuntimeError(f"ffprobe 扫描失败：{e}")

    tracks = []
    stream_data = data.get("streams", [])

    for stream in stream_data:
        stream_type = stream.get("codec_type")
        if stream_type not in ["video", "audio", "subtitle"]:
            continue

        track = Track(stream["index"], stream_type)

        # PID 映射到语言
        pid = int(stream.get("id", "0x0"), 16) if "id" in stream else 0
        if pid in chapter.pid_to_lang:
            track.language = chapter.pid_to_lang[pid]

        # 编码名称映射
        codec_name = stream.get("codec_name", "").lower()

        # 标准化编码名称
        codec_map = {
            # 音频编码
            "pcm_bluray": "lpcm",
            "pcm_s16be": "lpcm",
            "pcm_s24be": "lpcm",
            "pcm_s16le": "lpcm",
            "pcm_s24le": "lpcm",
            # 视频编码
            "mpeg2video": "mpeg-2",
            "vc1": "vc-1",
            "h264": "avc",
            "hevc": "hevc",
            # 字幕编码
            "hdmv_pgs_subtitle": "pgs",
            "dvd_subtitle": "vobsub",
            "subrip": "srt",
        }
        track.codec = codec_map.get(codec_name, codec_name)

        # DTS-HD MA/DTS-HD 识别（通过 profile 字段）
        if codec_name == "dts":
            profile = stream.get("profile", "")
            if "DTS:X" in profile:
                track.codec = "dts_x"
            elif "DTS-HD MA" in profile or "DTS-HD Master Audio" in profile:
                track.codec = "dts_hd_ma"
            elif "DTS-HD" in profile:
                track.codec = "dts_hd"

        # 视频轨道码率（不使用 max_bitrate，等待从 BDInfo 获取）
        if stream_type == "video":
            bit_rate = int(stream.get("bit_rate", 0))
            if bit_rate > 0:
                track.bitrate = bit_rate // 1000

        # TrueHD Atmos 识别
        elif track.codec == "truehd":
            profile = stream.get("profile", "")
            if "Atmos" in profile:
                track.is_atmos = True

            # 提取 TrueHD 实际码率和 AC3 核心码率
            bit_rate = int(stream.get("bit_rate", 0))
            if bit_rate > 0:
                track.bitrate = bit_rate // 1000

                # TrueHD 净码率
                if "tags" in stream and "BPS-eng" in stream["tags"]:
                    truehd_bps = int(stream["tags"]["BPS-eng"])
                    track.bitrate = truehd_bps // 1000
                    track.ac3_core_bitrate = (bit_rate - truehd_bps) // 1000

        # 其他轨道码率（音频/字幕）
        elif stream_type in ("audio", "subtitle"):
            bit_rate = int(stream.get("bit_rate", 0))
            if bit_rate > 0:
                track.bitrate = bit_rate // 1000

        # 声道数
        if stream_type == "audio":
            channels = stream.get("channels", 0)
            layout = stream.get("channel_layout", "")
            track.channels = _CHANNEL_MAP.get(channels, f"{channels}ch")

            sample_rate = stream.get("sample_rate", "")
            if sample_rate:
                track.sample_rate = f"{int(sample_rate) // 1000} kHz"

        # 生成轨道名
        track.name = track.generate_track_name()

        tracks.append(track)

    return tracks


# ==============================================================================
# ISO 挂载管理
# ==============================================================================


class ISOmountManager:
    """
    跨平台 ISO 挂载和清理管理器

    功能：
    - 检测平台（Windows/Linux/macOS）
    - 执行平台特定的挂载命令
    - 追踪挂载点用于清理
    - 优雅处理权限错误
    - 确保退出时清理
    """

    def __init__(self):
        self.platform = sys.platform
        self.mounted_isos = []  # List of (iso_path, mount_point) tuples
        self.console = Console()
        self._cleanup_msg_shown = False  # 标志：是否已显示清理提示

        # 平台处理器映射
        self._platform_handlers = {
            "win32": {"mount": self._mount_windows, "unmount": self._unmount_windows},
            "linux": {"mount": self._mount_linux, "unmount": self._unmount_linux},
            "darwin": {"mount": self._mount_macos, "unmount": self._unmount_macos},
        }

    def _get_platform_key(self) -> str:
        """获取平台键（处理 linux 变体）"""
        if self.platform.startswith("linux"):
            return "linux"
        return self.platform

    def _get_handler(self, action: str):
        """获取平台处理器"""
        platform_key = self._get_platform_key()
        if platform_key not in self._platform_handlers:
            raise RuntimeError(f"不支持的平台: {self.platform}")
        return self._platform_handlers[platform_key][action]

    def __enter__(self):
        """进入 Context Manager"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出 Context Manager 时自动清理"""
        self.unmount_all()
        return False  # 不抑制异常

    def mount(self, iso_path: Path) -> Path:
        """挂载 ISO 并返回挂载点 Path 对象"""
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=self.console) as progress:
            task = progress.add_task(f"正在挂载 ISO: {iso_path.name}...", total=None)

            # 使用字典映射获取平台处理器
            mount_handler = self._get_handler("mount")
            mount_point = mount_handler(iso_path)

            self.mounted_isos.append((iso_path, mount_point))
            progress.update(task, description=f"[green]✓ 已挂载到: {mount_point}")

        return mount_point

    def _check_interrupt_msg(self) -> None:
        """检测当前是否处于 KeyboardInterrupt 上下文，若是则显示一次清理提示"""
        exc_info = sys.exc_info()
        is_interrupted = exc_info[0] is KeyboardInterrupt
        if is_interrupted and not self._cleanup_msg_shown:
            self.console.print("\n[yellow]用户中断操作，正在清理...[/yellow]")
            self._cleanup_msg_shown = True

    def unmount_last(self):
        """卸载最后挂载的 ISO（用于循环中逐个处理）"""
        if not self.mounted_isos:
            return

        self._check_interrupt_msg()

        iso_path, mount_point = self.mounted_isos.pop()
        try:
            unmount_handler = self._get_handler("unmount")
            if self._get_platform_key() == "win32":
                unmount_handler(iso_path)
            else:
                unmount_handler(mount_point)

            self.console.print(f"[green]✓ 已卸载: {iso_path.name}[/green]")
        except Exception as e:
            self.console.print(f"[yellow]⚠ 卸载失败: {iso_path.name} - {e}[/yellow]")

    def _mount_windows(self, iso_path: Path) -> Path:
        """Windows 挂载（通过 PowerShell Mount-DiskImage）"""

        # 构建一段极速的微型 PowerShell 脚本
        # 逻辑：检查挂载 -> 如果没挂载则挂载 -> 获取盘符
        ps_script = f"""
        $ErrorActionPreference = 'Stop'
        $imgPath = '{iso_path.absolute()}'
        
        try {{
            $img = Get-DiskImage -ImagePath $imgPath
            if (-not $img.Attached) {{
                Mount-DiskImage -ImagePath $imgPath | Out-Null
                # 挂载后重新获取一下状态
                $img = Get-DiskImage -ImagePath $imgPath
            }}
            $drive = ($img | Get-Volume).DriveLetter
            if (-not $drive) {{
                throw "NoDriveLetter"
            }}
            Write-Output $drive
        }} catch {{
            Write-Error $_.Exception.Message
            exit 1
        }}
        """

        # 1. -NoProfile 禁用配置文件，启动速度提升数倍
        # 2. -NonInteractive 非交互模式，避免任何卡死的确认弹窗
        # 3. -Command "-" 通过标准输入读取脚本，完美避开路径里的引号转义 Bug
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"]
        result = subprocess.run(
            cmd, input=ps_script, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )

        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "access is denied" in stderr or "拒绝访问" in stderr:
                self.console.print("[yellow]权限不足提示：[/yellow]")
                self.console.print("  Windows 10/11 通常不需要管理员权限")
                self.console.print("  如果挂载失败，请尝试：")
                self.console.print("  1. 右键脚本 → 以管理员身份运行")
                self.console.print("  2. 或手动双击 ISO 后使用挂载盘符")
                raise PermissionError("ISO 挂载失败：权限不足")
            elif "corrupted" in stderr or "损坏" in stderr:
                raise ValueError(f"ISO 文件损坏或格式无效: {iso_path.name}")
            else:
                raise RuntimeError(f"挂载失败: {result.stderr.strip()}")

        drive_letter = result.stdout.strip()
        if not drive_letter or drive_letter == "NoDriveLetter":
            raise RuntimeError("无法获取挂载盘符（可能所有盘符 A-Z 已被占用）")

        return Path(f"{drive_letter}:/")

    def _mount_linux(self, iso_path: Path) -> Path:
        """Linux 挂载（通过 mount）"""

        # 创建唯一挂载点
        mount_point = Path(f"/tmp/bluray_remux_{os.getpid()}_{int(time.time())}")
        mount_point.mkdir(parents=True, exist_ok=True)

        mount_cmd = ["mount", "-o", "loop,ro", str(iso_path.absolute()), str(mount_point)]

        try:
            result = subprocess.run(mount_cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            mount_point.rmdir()  # 清理

            if "permission denied" in e.stderr.lower():
                self.console.print("[yellow]权限不足提示：[/yellow]")
                self.console.print("  Linux 挂载需要 root 权限")
                self.console.print("  请尝试：")
                self.console.print(f"    sudo mount -o loop,ro '{iso_path}' /mnt/bluray")
                self.console.print(f"    python bluray_remux.py /mnt/bluray")
                raise PermissionError("ISO 挂载失败：需要 sudo 权限")
            else:
                raise RuntimeError(f"挂载失败: {e.stderr}")

        return mount_point

    def _mount_macos(self, iso_path: Path) -> Path:
        """macOS 挂载（通过 hdiutil）"""

        mount_point = Path(f"/tmp/bluray_remux_{os.getpid()}")
        mount_point.mkdir(parents=True, exist_ok=True)

        mount_cmd = ["hdiutil", "attach", "-readonly", "-mountpoint", str(mount_point), "-nobrowse", str(iso_path.absolute())]

        try:
            result = subprocess.run(mount_cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            mount_point.rmdir()  # 清理

            if "permission denied" in e.stderr.lower():
                raise PermissionError("ISO 挂载失败：权限不足")
            else:
                raise RuntimeError(f"挂载失败: {e.stderr}")

        return mount_point

    def unmount_all(self):
        """卸载所有已追踪的 ISO"""
        if not self.mounted_isos:
            return

        self._check_interrupt_msg()

        for iso_path, mount_point in self.mounted_isos:
            try:
                unmount_handler = self._get_handler("unmount")
                if self._get_platform_key() == "win32":
                    unmount_handler(iso_path)
                else:
                    unmount_handler(mount_point)

                self.console.print(f"[green]✓ 已卸载: {iso_path.name}[/green]")

            except Exception as e:
                self.console.print(f"[yellow]⚠ 卸载失败: {iso_path.name} - {e}[/yellow]")

        self.mounted_isos.clear()

    def _unmount_windows(self, iso_path: Path):
        """Windows 卸载（通过 Dismount-DiskImage）"""
        unmount_cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Dismount-DiskImage -ImagePath '{iso_path.absolute()}' | Out-Null"]

        try:
            subprocess.run(unmount_cmd, capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        except subprocess.CalledProcessError:
            # 不抛出异常 - 允许优雅降级
            pass

    def _unmount_linux(self, mount_point: Path):
        """Linux 卸载（通过 umount）"""
        try:
            subprocess.run(["umount", str(mount_point)], capture_output=True, check=True)
            mount_point.rmdir()
        except:
            pass

    def _unmount_macos(self, mount_point: Path):
        """macOS 卸载（通过 hdiutil detach）"""
        try:
            subprocess.run(["hdiutil", "detach", str(mount_point)], capture_output=True, check=True)
            mount_point.rmdir()
        except:
            pass


def _process_iso_source(source: Dict, mount_manager: ISOmountManager, console: Console) -> Path:
    """
    统一处理 ISO 挂载和 BDMV 查找逻辑

    Args:
        source: 原盘信息字典 (type, path, bdmv_path)
        mount_manager: ISO 挂载管理器
        console: Rich Console 对象

    Returns:
        BDMV 目录路径
    """
    if source["type"] == "iso":
        mount_point = mount_manager.mount(source["path"])
        bdmv_path = find_bdmv_in_mount(mount_point)
        return bdmv_path
    else:
        return source["bdmv_path"]


# ==============================================================================
# 工作流公共函数
# ==============================================================================


def select_main_playlist(
    bdmv_path: Path, console: Console, context_name: Optional[str] = None, auto_skip: bool = True
) -> Tuple[Optional[Chapter], Optional[Path]]:
    """
    扫描 MPLS 文件并选择正片

    统一 workflow 和 batch 流程的 MPLS 扫描逻辑。

    Args:
        bdmv_path: BDMV 目录路径
        console: Rich Console 对象
        context_name: 上下文名称（用于批处理提示，如 "电影A"）

    Returns:
        (Chapter对象, MPLS文件路径)，当用户选择返回时返回 (None, None)

    Raises:
        RuntimeError: 未找到符合条件的正片
    """
    # 扫描 MPLS 文件
    candidates = scan_mpls_files(bdmv_path)

    if not candidates:
        raise RuntimeError("未找到符合条件的正片 MPLS")

    prompt = "选择正片播放列表"
    if context_name:
        prompt += f" - {context_name}"

    # 用户选择
    cli = InteractiveCLI()
    selected_idx = cli.select_from_candidates(candidates, prompt, auto_skip=auto_skip)

    if selected_idx == -2:
        return None, None

    selected = candidates[selected_idx]

    console.print(f"[green]✓ 已选择：{selected['mpls_name']}[/green]")
    if DEBUG:
        console.print(selected["chapter"].debug_chapter_info())

    return selected["chapter"], selected["mpls_path"]


def scan_main_tracks(bdmv_path: Path, chapter: Chapter, console: Console, verbose: bool = True) -> List[Track]:
    """
    扫描主要轨道信息（基于第一个 M2TS）

    统一 workflow 和 batch 流程的轨道扫描逻辑。

    Args:
        bdmv_path: BDMV 目录路径
        chapter: Chapter 对象
        console: Rich Console 对象
        verbose: 是否输出详细信息

    Returns:
        原始轨道列表

    Raises:
        RuntimeError: M2TS 文件不存在
    """
    first_m2ts = bdmv_path / "STREAM" / f"{chapter.get_m2ts_files()[0]}.m2ts"

    if not first_m2ts.exists():
        raise RuntimeError(f"M2TS 文件不存在：{first_m2ts}")

    if verbose:
        console.print(f"扫描文件：{first_m2ts.name}")

    tracks = scan_tracks_with_ffprobe(first_m2ts, chapter)

    if verbose:
        console.print(f"[green]✓ 扫描完成[/green]")
        console.print(f"  视频轨：{sum(1 for t in tracks if t.type == 'video')} 个")
        console.print(f"  音频轨：{sum(1 for t in tracks if t.type == 'audio')} 个")
        console.print(f"  字幕轨：{sum(1 for t in tracks if t.type == 'subtitle')} 个\n")

    return tracks


def parse_bdinfo_optional(bdinfo_path: Optional[Path], mpls_name: str, console: Console, verify_match: bool = True) -> Optional[Dict]:
    """
    解析 BDInfo 文本（可选，失败时返回 None）

    统一 workflow 和 batch 流程的 BDInfo 解析逻辑。

    Args:
        bdinfo_path: BDInfo 文件路径
        mpls_name: 已选择的 MPLS 文件名（用于验证）
        console: Rich Console 对象
        verify_match: 是否验证 PLAYLIST 匹配

    Returns:
        BDInfo 数据字典，失败或不存在时返回 None
    """
    if not bdinfo_path or not bdinfo_path.exists():
        return None

    try:
        parser = BDInfoParser(str(bdinfo_path))
        bdinfo_data = parser.parse()

        # 验证 PLAYLIST 匹配（可选）
        if verify_match and bdinfo_data["playlist"].lower() != mpls_name.lower():
            console.print(f"[yellow]警告：BDInfo PLAYLIST ({bdinfo_data['playlist']}) 与所选 MPLS ({mpls_name}) 不匹配[/yellow]")

            # 使用 Prompt 提供三个选项
            choice = Prompt.ask(
                "[yellow]请选择操作[/yellow]\n  [cyan]1[/cyan]: 重新选择 MPLS (默认)\n  [cyan]2[/cyan]: 跳过该原盘\n  [cyan]3[/cyan]: 强制继续处理\n输入选择",
                choices=["1", "2", "3"],
                default="1",
            )

            if choice == "1":
                raise ValueError("RESELECT_MPLS")
            elif choice == "2":
                raise ValueError("SKIP_DISC")
            # choice == "3" 时什么都不做，顺理成章往下走

        return bdinfo_data

    except ValueError as e:
        raise e
    except Exception as e:
        console.print(f"[yellow]警告：BDInfo 解析失败 - {e}[/yellow]")
        return None


# ==============================================================================
# 主流程工作流阶段函数
# ==============================================================================


def workflow_phase1_scan_mpls(bdmv_path: Path, console: Console, auto_skip: bool = True) -> Tuple[Optional[Chapter], Optional[Path]]:
    """阶段1：扫描 MPLS 文件并选择正片

    Args:
        bdmv_path: BDMV 目录路径
        console: Rich Console 对象

    Returns:
        (Chapter对象, MPLS文件路径)，用户在选择界面选择返回时返回 None

    Raises:
        RuntimeError: 未找到符合条件的正片
    """
    console.print("\n[bold cyan]阶段 1：扫描 MPLS 文件[/bold cyan]")

    # 调用公共函数
    chapter, mpls_path = select_main_playlist(bdmv_path, console, auto_skip=auto_skip)
    if chapter is None or mpls_path is None:
        return None, None

    return chapter, mpls_path


def workflow_phase2_parse_bdinfo(bdinfo_path: Optional[Path], mpls_name: str, console: Console) -> Optional[Dict]:
    """
    阶段2：解析 BDInfo 文本（可选）

    Args:
        bdinfo_path: BDInfo 文件路径
        mpls_name: 已选择的 MPLS 文件名（用于验证）
        console: Rich Console 对象

    Returns:
        BDInfo 数据字典，失败时返回 None
    """
    if not bdinfo_path or not bdinfo_path.exists():
        return None

    console.print("\n[bold cyan]阶段 2：解析 BDInfo 文本[/bold cyan]")

    # 调用公共函数（启用 PLAYLIST 匹配验证）
    bdinfo_data = parse_bdinfo_optional(bdinfo_path, mpls_name, console, verify_match=True)

    # 显示解析成功的详细信息
    if bdinfo_data:
        console.print(f"[green]✓ 已解析 BDInfo[/green]")
        console.print(f"  PLAYLIST：{bdinfo_data['playlist']}")
        console.print(f"  音轨：{len(bdinfo_data['audio'])} 个")
        console.print(f"  字幕：{len(bdinfo_data['subtitle'])} 个\n")

    return bdinfo_data


def workflow_phase3_scan_tracks(bdmv_path: Path, chapter: Chapter, console: Console) -> List[Track]:
    """
    阶段3：扫描轨道信息

    Args:
        bdmv_path: BDMV 目录路径
        chapter: Chapter 对象
        console: Rich Console 对象

    Returns:
        原始轨道列表

    Raises:
        RuntimeError: M2TS 文件不存在
    """
    console.print("\n[bold cyan]阶段 3：扫描轨道信息[/bold cyan]")

    # 调用公共函数（启用详细输出）
    return scan_main_tracks(bdmv_path, chapter, console, verbose=True)


def _set_track_flags(track: Track, original_lang: str):
    """
    设置轨道标志位（导评/原语言/SDH）

    Args:
        track: 轨道对象
        original_lang: 原始语言代码
    """
    # 导评/评论轨标记
    if track.custom_desc:
        desc_lower = track.custom_desc.lower()
        if has_keywords(desc_lower, TRACK_KEYWORDS["commentary"]):
            track.is_commentary = True

    # 原语言标记
    if track.language == original_lang:
        track.is_original = True

    # SDH 听觉障碍标记（仅字幕）
    if track.type == "subtitle" and track.custom_desc:
        if SDH_PATTERN.search(track.custom_desc):
            track.is_hearing_impaired = True


def _debug_unmatched_track(track: Track, bdinfo_audio: List[Dict], used_indices: set) -> None:
    """
    调试输出：轨道无法匹配 BDInfo 时的诊断信息

    仅在 DEBUG=True 时输出。显示当前轨道的属性和所有可用的 BDInfo 候选项，
    方便排查匹配失败的原因。

    Args:
        track: 无法匹配的轨道对象
        bdinfo_audio: BDInfo 音频轨道数据列表
        used_indices: 已被占用的 BDInfo 索引集合
    """
    debug_print(f"  [yellow]Track {track.id} 无法匹配 BDInfo[/yellow]")
    debug_print(f"    [dim]track: lang={track.language}, codec={track.codec}, " f"channels={track.channels}, bitrate={track.bitrate}[/dim]")
    candidates = [i for i in range(len(bdinfo_audio)) if i not in used_indices]
    debug_print(f"    [dim]可用 BDInfo 候选: {len(candidates)} 个[/dim]")
    for idx in candidates:
        bd = bdinfo_audio[idx]
        debug_print(
            f"      [dim]BDInfo[{idx}]: lang={bd.get('language', 'N/A')}, "
            f"codec={bd.get('codec', 'N/A')}, channels={bd.get('channels', 'N/A')}, "
            f"bitrate={bd.get('bitrate', 0)}, desc=\"{bd.get('custom_desc', '')}\"[/dim]"
        )


def _apply_bdinfo_to_track(track: Track, bd_track: Dict, original_lang: str) -> None:
    """
    将 BDInfo 匹配结果应用到轨道（公共后处理逻辑）

    在轨道成功匹配到 BDInfo 数据后，统一处理自定义描述、导评标志、
    标志位设置和轨道名称生成。码率/声道/语言等差异化逻辑由调用方
    在调用此函数前单独处理。

    Args:
        track: 待更新的轨道对象
        bd_track: 匹配到的 BDInfo 数据字典
        original_lang: 原始语言代码（用于判断 is_original 等标志）
    """
    # 自定义描述
    if bd_track.get("custom_desc"):
        track.custom_desc = bd_track["custom_desc"]

    # 导评标志（若 BDInfo 已标记）
    if bd_track.get("is_commentary"):
        track.is_commentary = bd_track["is_commentary"]
        debug_print(f"  [magenta]Track {track.id} 标记为导评: custom_desc='{track.custom_desc}'[/magenta]")

    # 根据 custom_desc/语言等设置 is_commentary/is_original/SDH 标记
    _set_track_flags(track, original_lang)

    # 调试：显示标志位设置结果
    if track.is_commentary:
        debug_print(f"  [magenta]Track {track.id} 最终 is_commentary=True[/magenta]")

    # 重新生成轨道名
    track.name = track.generate_track_name()


def _resolve_truehd_bitrate(track: Track, bd_track: Dict, console: Console) -> None:
    """
    解析 TrueHD 轨道的净码率与 AC3 核心码率，填充到 Track 上

    TrueHD 的码率处理比较复杂：BDInfo 可能提供完整码率（full_bitrate）、净码率（net_bitrate）
    和 AC3 Core 码率（ac3_core_bitrate）。需要根据这些值的关系判断正确的 TrueHD 净码率。

    处理逻辑：
    1. 无净码率 → 退回使用完整码率
    2. 完整码率 == 净码率 且有 AC3 Core → 手动减去 AC3 Core 得到真实净码率
    3. 完整码率 == 净码率 但无 AC3 Core → 使用净码率并警告（可能不准确）
    4. 完整码率 != 净码率 → 直接使用净码率（正常情况）

    Args:
        track: TrueHD 轨道对象（in-place 修改 bitrate 和 ac3_core_bitrate）
        bd_track: 匹配到的 BDInfo 数据字典
        console: Rich Console 对象（用于输出警告信息）
    """
    full = bd_track.get("full_bitrate", 0)
    net = bd_track.get("net_bitrate", 0)
    ac3 = bd_track.get("ac3_core_bitrate", 0)

    if net <= 0:
        # 没有净码率信息，退回使用完整码率
        track.bitrate = bd_track.get("bitrate", 0)
        debug_print(f"  [yellow]TrueHD 净码率缺失，使用完整码率: {track.bitrate} kbps[/yellow]")
        return

    if full > 0 and full == net and ac3 > 0:
        # 情况1：完整码率 == 净码率 且有 AC3 Core → 手动计算净码率
        true_net = net - ac3
        debug_print(f"  [dim]TrueHD 码率计算: {net} kbps (总码率) - {ac3} kbps (AC3 Core) = {true_net} kbps (净码率)[/dim]")
        track.bitrate = true_net
        track.ac3_core_bitrate = ac3
        return

    if full > 0 and full == net and ac3 == 0:
        # 情况2：完整码率 == 净码率 但 AC3 Core 为0 → 警告
        track.bitrate = net
        console.print(f"  [yellow]TrueHD 警告: 完整码率({full})=净码率({net})，但AC3 Core未解析，使用净码率（可能包含AC3）[/yellow]")
        return

    # 情况3：完整码率 != 净码率 → 直接使用净码率（正常情况）
    track.bitrate = net
    if ac3 > 0:
        track.ac3_core_bitrate = ac3
        debug_print(f"  [dim]TrueHD 码率: {net} kbps (净码率), AC3 Core: {ac3} kbps[/dim]")
    else:
        debug_print(f"  [dim]TrueHD 码率: {net} kbps (净码率)[/dim]")


def _detect_and_bind_ac3_core(
    track: Track,
    audio_tracks_by_index: List[Track],
    claimed_truehd_ids: set,
) -> Tuple[bool, Optional[Track]]:
    """
    检测当前 AC3/EAC3 轨道是否为前面 TrueHD 的内嵌核心轨

    通过以下条件判断：
    1. 前面存在同语言的 TrueHD 轨道
    2. 该 TrueHD 尚未被其他 AC3 核心占用（一对一原则）
    3. TrueHD 已解析出 ac3_core_bitrate（在第一阶段处理）
    4. 码率差异 ≤ 50 kbps 且轨道索引间距 ≤ 2

    Args:
        track: 当前待检测的 AC3/EAC3 轨道
        audio_tracks_by_index: 按 id 排序的完整音频轨道列表
        claimed_truehd_ids: 已被分配 AC3 核心的 TrueHD 轨道 ID 集合（in-place 更新）

    Returns:
        (is_core, parent_truehd) 二元组：
        - is_core=True, parent_truehd=Track: 检测为 AC3 核心，返回父 TrueHD 轨道
        - is_core=False, parent_truehd=None: 检测为独立轨道

    Examples:
        >>> is_core, parent = _detect_and_bind_ac3_core(ac3_track, all_audio, claimed)
        >>> if is_core:
        ...     ac3_track.is_ac3_core = True
        ...     ac3_track.parent_truehd_id = parent.id
    """
    prev_tracks = [t for t in audio_tracks_by_index if t.id < track.id]
    for prev_track in reversed(prev_tracks):
        if prev_track.codec.lower() != "truehd" or prev_track.language != track.language:
            continue
        if prev_track.id in claimed_truehd_ids:
            continue

        if prev_track.ac3_core_bitrate > 0:
            bitrate_diff = abs(track.bitrate - prev_track.ac3_core_bitrate)
            index_gap = track.id - prev_track.id

            if bitrate_diff <= 50 and index_gap <= 2:
                claimed_truehd_ids.add(prev_track.id)
                return True, prev_track

    return False, None


def _integrate_video_tracks(video_tracks: List[Track], bdinfo_video: List[Dict]) -> None:
    """
    整合视频轨道的 BDInfo 数据

    按 track.id 排序后逐一匹配 BDInfo，应用码率信息。

    Args:
        video_tracks: 视频轨道列表（in-place 修改）
        bdinfo_video: BDInfo 视频轨道数据列表
    """
    used_bdinfo_indices_video = set()
    video_tracks_by_index = sorted(video_tracks, key=lambda t: t.id)

    for track in video_tracks_by_index:
        bd_idx, bd_track = match_track_with_bdinfo(track, bdinfo_video, used_bdinfo_indices_video)
        if bd_track is None:
            continue
        used_bdinfo_indices_video.add(bd_idx)
        if bd_track.get("bitrate"):
            track.bitrate = bd_track["bitrate"]


def _integrate_audio_main_tracks(
    audio_tracks: List[Track],
    bdinfo_audio: List[Dict],
    used_indices: set,
    original_lang: str,
    console: Console,
) -> None:
    """
    音频轨第一阶段整合：处理非 AC3/EAC3 轨道

    优先处理 TrueHD、DTS-HD MA、DTS:X、LPCM 等编码，匹配 BDInfo 数据，
    处理码率（含 TrueHD 特殊的净码率/AC3 Core 计算）和声道信息。
    跳过 AC3/EAC3 轨道留给第二阶段处理。

    Args:
        audio_tracks: 音频轨道列表（in-place 修改）
        bdinfo_audio: BDInfo 音频轨道数据列表
        used_indices: 已占用的 BDInfo 索引集合（in-place 更新）
        original_lang: 原始语言代码
        console: Rich Console 对象（用于 TrueHD 码率警告输出）
    """
    audio_tracks_by_index = sorted(audio_tracks, key=lambda t: t.id)

    # 调试：显示匹配前的轨道信息
    debug_print("\n[dim]调试信息：音频轨道 BDInfo 匹配过程[/dim]")
    for track in audio_tracks_by_index:
        debug_print(f"  [dim]Track {track.id}: lang={track.language}, codec={track.codec}, channels={track.channels}[/dim]")

    debug_print("\n[dim]第一阶段：匹配非 AC3/E-AC3 轨道[/dim]")
    for track in audio_tracks_by_index:
        if track.codec.lower() in ("ac3", "eac3"):
            continue  # 暂时跳过 AC3/E-AC3

        bd_idx, bd_track = match_track_with_bdinfo(track, bdinfo_audio, used_indices)
        if bd_track is None:
            _debug_unmatched_track(track, bdinfo_audio, used_indices)
            continue
        used_indices.add(bd_idx)

        # 调试：显示匹配成功信息
        debug_print(f"  [dim]Track {track.id} → BDInfo[{bd_idx}]: {bd_track.get('custom_desc', 'N/A')}[/dim]")

        # 调试：显示 TrueHD 轨道的详细信息
        if track.codec.lower() == "truehd":
            debug_print(f"  [cyan]TrueHD 轨道详细信息:[/cyan]")
            debug_print(f"    track.bitrate (处理前): {track.bitrate}")
            debug_print(f"    bd_track.bitrate: {bd_track.get('bitrate', 'N/A')}")
            debug_print(f"    bd_track.full_bitrate: {bd_track.get('full_bitrate', 'N/A')}")
            debug_print(f"    bd_track.net_bitrate: {bd_track.get('net_bitrate', 'N/A')}")
            debug_print(f"    bd_track.ac3_core_bitrate: {bd_track.get('ac3_core_bitrate', 'N/A')}")

        # 码率处理
        if bd_track.get("bitrate"):
            if track.codec.lower() == "truehd":
                _resolve_truehd_bitrate(track, bd_track, console)
            elif track.codec.lower() in ("dts_x", "dts_hd_ma", "dts_hd", "lpcm"):
                track.bitrate = bd_track["bitrate"]
            elif track.bitrate == 0:
                track.bitrate = bd_track["bitrate"]

        # 调试：显示 TrueHD 码率处理结果
        if track.codec.lower() == "truehd":
            debug_print(f"  [green]TrueHD 码率处理完成: track.bitrate = {track.bitrate} kbps[/green]")

        # 声道信息
        if bd_track.get("channels"):
            track.channels = _clean_channels_str(bd_track["channels"])

        # 公共后处理：自定义描述 + 标志位 + 生成轨道名
        _apply_bdinfo_to_track(track, bd_track, original_lang)


def _integrate_audio_ac3_tracks(
    audio_tracks: List[Track],
    bdinfo_audio: List[Dict],
    used_indices: set,
    original_lang: str,
) -> None:
    """
    音频轨第二阶段整合：处理 AC3/EAC3 轨道

    检测每个 AC3/EAC3 轨道是否为前面 TrueHD 的内嵌核心（通过码率匹配和索引间距判断），
    核心轨继承父 TrueHD 的描述和标志，独立轨正常匹配 BDInfo。

    内部维护 claimed_truehd_ids 集合，确保 TrueHD 与 AC3 核心的一对一配对。

    Args:
        audio_tracks: 音频轨道列表（in-place 修改，需要第一阶段已处理完毕）
        bdinfo_audio: BDInfo 音频轨道数据列表
        used_indices: 已占用的 BDInfo 索引集合（in-place 更新）
        original_lang: 原始语言代码
    """
    audio_tracks_by_index = sorted(audio_tracks, key=lambda t: t.id)
    debug_print("\n[dim]第二阶段：处理 AC3/E-AC3 轨道[/dim]")

    claimed_truehd_ids = set()

    for track in audio_tracks_by_index:
        if track.codec.lower() not in ("ac3", "eac3"):
            continue

        is_ac3_core, parent_truehd = _detect_and_bind_ac3_core(track, audio_tracks_by_index, claimed_truehd_ids)

        if is_ac3_core:
            # 标记为 AC3 核心，继承 TrueHD 的描述
            track.is_ac3_core = True
            track.parent_truehd_id = parent_truehd.id
            track.custom_desc = parent_truehd.custom_desc
            track.is_commentary = parent_truehd.is_commentary
            debug_print(f"  [cyan]Track {track.id} 检测为 AC3 核心 (父 TrueHD: Track {parent_truehd.id})[/cyan]")
            debug_print(f"    [dim]继承描述: {track.custom_desc}[/dim]")
            debug_print(f"    [dim]bitrate: {track.bitrate} kbps (TrueHD ac3_core: {parent_truehd.ac3_core_bitrate} kbps)[/dim]")

            # 设置标志位并生成轨道名
            _set_track_flags(track, original_lang)
            track.name = track.generate_track_name()
        else:
            # 独立的 AC3/E-AC3 轨道，正常匹配 BDInfo
            debug_print(f"  [dim]Track {track.id} 检测为独立 AC3 轨道，匹配 BDInfo[/dim]")
            bd_idx, bd_track = match_track_with_bdinfo(track, bdinfo_audio, used_indices)
            if bd_track is None:
                _debug_unmatched_track(track, bdinfo_audio, used_indices)
                continue
            used_indices.add(bd_idx)

            # 调试：显示匹配成功信息
            debug_print(f"  [dim]Track {track.id} → BDInfo[{bd_idx}]: {bd_track.get('custom_desc', 'N/A')}[/dim]")

            # 码率处理（AC3/E-AC3 直接使用 BDInfo 码率）
            if bd_track.get("bitrate") and track.bitrate == 0:
                track.bitrate = bd_track["bitrate"]

            # 声道信息
            if bd_track.get("channels"):
                track.channels = _clean_channels_str(bd_track["channels"])

            # 公共后处理：自定义描述 + 标志位 + 生成轨道名
            _apply_bdinfo_to_track(track, bd_track, original_lang)


def _integrate_subtitle_tracks(
    subtitle_tracks: List[Track],
    bdinfo_subtitle: List[Dict],
    original_lang: str,
) -> None:
    """
    整合字幕轨道的 BDInfo 数据

    匹配 BDInfo 并应用码率、语言标签（包括基于 custom_desc 的 zh-Hans/zh-Hant 识别）、
    自定义描述和标志位。

    Args:
        subtitle_tracks: 字幕轨道列表（in-place 修改）
        bdinfo_subtitle: BDInfo 字幕轨道数据列表
        original_lang: 原始语言代码
    """
    used_bdinfo_indices_sub = set()
    subtitle_tracks_by_index = sorted(subtitle_tracks, key=lambda t: t.id)

    for track in subtitle_tracks_by_index:
        bd_idx, bd_track = match_track_with_bdinfo(track, bdinfo_subtitle, used_bdinfo_indices_sub)
        if bd_track is None:
            continue
        used_bdinfo_indices_sub.add(bd_idx)

        # 赋值码率
        if bd_track.get("bitrate"):
            track.bitrate = bd_track["bitrate"]

        # 语言标签
        if bd_track.get("language"):
            track.language = bd_track["language"]

        # 公共后处理：自定义描述 + 标志位 + 生成轨道名
        _apply_bdinfo_to_track(track, bd_track, original_lang)


def integrate_and_prepare_tracks(
    tracks: List[Track],
    bdinfo_data: Optional[Dict],
    original_lang: str,
    drop_commentary: bool = False,
    keep_best_audio: bool = False,
    simplify_subs: bool = True,
) -> Tuple[List[Track], List[Track], List[Track], Dict]:
    """
    整合 BDInfo 数据并准备轨道（排序、设置默认）

    Args:
        tracks: 原始轨道列表
        bdinfo_data: BDInfo 数据字典（可选）
        original_lang: 原始语言代码
        drop_commentary: 是否丢弃导评轨道
        keep_best_audio: 是否仅保留最高规格音轨
        simplify_subs: 是否精简外语字幕（仅保留一条最优）

    Returns:
        (视频轨列表, 音频轨列表, 字幕轨列表, 视图数据字典) - 已排序并设置默认
    """
    sorter = TrackSorter(original_lang, drop_commentary, keep_best_audio, simplify_subs)

    audio_tracks = [t for t in tracks if t.type == "audio"]
    subtitle_tracks = [t for t in tracks if t.type == "subtitle"]
    video_tracks = [t for t in tracks if t.type == "video"]

    # 如果有 BDInfo，整合自定义描述
    if bdinfo_data:
        console = Console()
        _integrate_video_tracks(video_tracks, bdinfo_data["video"])

        used_bdinfo_indices = set()
        _integrate_audio_main_tracks(audio_tracks, bdinfo_data["audio"], used_bdinfo_indices, original_lang, console)
        _integrate_audio_ac3_tracks(audio_tracks, bdinfo_data["audio"], used_bdinfo_indices, original_lang)
        _integrate_subtitle_tracks(subtitle_tracks, bdinfo_data["subtitle"], original_lang)

    # 排序和过滤
    debug_print("\n[dim]调试信息：排序前的音频轨道[/dim]")
    for track in audio_tracks:
        debug_print(
            f"  [dim]Track {track.id}: lang={track.language}, codec={track.codec}, is_commentary={track.is_commentary}, name='{track.name}'[/dim]"
        )

    # 视图数据：在过滤前后保存完整列表快照
    # 注意：video_tracks 在此阶段已完成 BDInfo 整合，但尚未设置默认、原语言标志
    unsorted_all_audio = copy.deepcopy(audio_tracks)
    unsorted_all_subtitle = copy.deepcopy(subtitle_tracks)
    unsorted_all = copy.deepcopy(video_tracks) + unsorted_all_audio + unsorted_all_subtitle

    sorted_all_audio = sorted(copy.deepcopy(audio_tracks), key=sorter._audio_sort_key)
    sorted_all_subtitle = sorted(copy.deepcopy(subtitle_tracks), key=sorter._subtitle_sort_key)
    sorted_all = copy.deepcopy(video_tracks) + sorted_all_audio + sorted_all_subtitle

    # 工作列表：过滤 + 排序
    audio_tracks = sorter.filter_and_sort_audio(audio_tracks)

    debug_print("\n[dim]调试信息：排序后的音频轨道[/dim]")
    for track in audio_tracks:
        debug_print(
            f"  [dim]Track {track.id}: lang={track.language}, codec={track.codec}, is_commentary={track.is_commentary}, name='{track.name}'[/dim]"
        )

    subtitle_tracks = sorter.filter_and_sort_subtitle(subtitle_tracks)

    # 设置默认轨道
    if video_tracks:
        video_tracks[0].is_default = True
    if audio_tracks:
        audio_tracks[0].is_default = True
    if subtitle_tracks:
        subtitle_tracks[0].is_default = True

    # 统一设置原语言标志
    for track in audio_tracks + subtitle_tracks:
        if track.language == original_lang and not track.is_original:
            track.is_original = True

    view_data: Dict = {
        "sorted_all": sorted_all,
        "unsorted_all": unsorted_all,
        "audio_sort_key": sorter._audio_sort_key,
        "subtitle_sort_key": sorter._subtitle_sort_key,
    }

    return video_tracks, audio_tracks, subtitle_tracks, view_data


def workflow_phase4_integrate_bdinfo(
    tracks: List[Track],
    bdinfo_data: Optional[Dict],
    original_lang: str,
    drop_commentary: bool,
    keep_best_audio: bool,
    simplify_subs: bool,
    console: Console,
) -> Tuple[List[Track], List[Track], List[Track], Dict]:
    """阶段4：整合 BDInfo 并排序

    Args:
        tracks: 原始轨道列表
        bdinfo_data: BDInfo 数据字典（可选）
        original_lang: 原始语言代码
        drop_commentary: 是否丢弃导评轨道
        keep_best_audio: 是否仅保留最高规格音轨
        simplify_subs: 是否精简外语字幕
        console: Rich Console 对象

    Returns:
        (视频轨列表, 音频轨列表, 字幕轨列表, 视图数据字典)
    """
    console.print("\n[bold cyan]阶段 4：自动排序轨道[/bold cyan]")

    # 检测原始音频轨道索引异常（在过滤前检测）
    validate_audio_track_indices(tracks, console, allow_cancel=True)

    # 调用统一的整合函数
    video_tracks, audio_tracks, subtitle_tracks, view_data = integrate_and_prepare_tracks(
        tracks, bdinfo_data, original_lang, drop_commentary, keep_best_audio, simplify_subs
    )

    # 输出调试信息
    debug_print("\n[dim]调试信息：过滤后的音频轨道[/dim]")
    for track in audio_tracks:
        debug_print(f"  [dim]Index {track.id}: lang={track.language} codec={track.codec} channels={track.channels} name={track.name}[/dim]")

    console.print(f"[green]✓ 排序完成（原始语言：{original_lang}）[/green]\n")

    return video_tracks, audio_tracks, subtitle_tracks, view_data


def workflow_phase6_extract_metadata(bdmv_path: Path, chapter: Chapter, output_dir: Path, console: Console) -> Tuple[str, Dict, Optional[str]]:
    """
    阶段6：提取元数据

    Args:
        bdmv_path: BDMV 目录路径
        chapter: Chapter 对象
        output_dir: 输出目录
        console: Rich Console 对象

    Returns:
        (标题, 元数据字典, 章节文件路径)
    """
    console.print("\n[bold cyan]阶段 6：提取元数据[/bold cyan]")
    metadata = extract_metadata(bdmv_path)

    # 获取标题（优先使用 output_dir.name，避免 ISO 挂载盘符问题）
    # 批处理模式：output_dir = base_output_dir / source["name"]，已包含正确名称
    # 单文件模式：output_dir 用户指定，使用 output_dir.name 作为标题
    # ISO 特殊处理：检测盘符根目录，回退到 output_dir.name
    if bdmv_path.parent.parent == bdmv_path.parent:
        # 盘符根目录（ISO 挂载），使用 output_dir.name
        title = sanitize_filename(output_dir.name)
        console.print(f"  [dim]检测到 ISO 挂载，使用输出目录名作为标题[/dim]")
    else:
        # 正常目录结构，保持原逻辑
        # 但优先使用 output_dir.name（如果不是 "." 或 "output" 等默认值）
        if output_dir.name and output_dir.name not in [".", "output", "remux"]:
            title = sanitize_filename(output_dir.name)
        else:
            title = sanitize_filename(bdmv_path.parent.name)

    console.print(f"  标题：{title}")

    if metadata["cover_path"]:
        console.print(f"  封面：{metadata['cover_path'].name} ({format_size(metadata['cover_path'].stat().st_size)})")
    else:
        console.print("  封面：未找到")

    console.print(f"  章节：{chapter.get_chapter_count()} 个")

    # 如果MPLS没有章节，尝试生成OGM章节文件
    chapters_file = None
    if chapter.get_chapter_count() == 0:
        console.print("  [yellow]警告：MPLS没有章节信息，尝试生成章节文件[/yellow]")
        timestamps = chapter.get_chapter_timestamps()
        if timestamps:
            output_dir.mkdir(parents=True, exist_ok=True)
            chapters_file = str(output_dir / f"{title}_chapters.txt")
            generate_ogm_chapters(chapter, chapters_file)
            console.print(f"  章节文件：{Path(chapters_file).name}")
    console.print()

    return title, metadata, chapters_file


def workflow_phase7_remux(
    output_dir: Path, mpls_path: Path, tracks: List[Track], title: str, metadata: Dict, chapters_file: Optional[str], console: Console
) -> bool:
    """
    阶段7：执行 Remux

    Args:
        output_dir: 输出目录
        mpls_path: MPLS 文件路径
        tracks: 轨道列表
        title: 标题
        metadata: 元数据字典
        chapters_file: 章节文件路径
        console: Rich Console 对象

    Returns:
        是否成功
    """
    console.print("\n[bold cyan]阶段 7：执行 Remux[/bold cyan]")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"{title}.mkv"

    console.print(f"输出文件：{output_file}")

    # 构建命令
    cmd = build_mkvmerge_command(
        output_path=str(output_file),
        mpls_path=str(mpls_path),
        tracks=tracks,
        title=title,
        cover_path=metadata["cover_path"],
        chapters_file=chapters_file,
    )

    console.print("\nmkvmerge 命令：")
    console.print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    console.print()

    # 执行
    return run_mkvmerge_with_progress(cmd)


# ==============================================================================
# 主流程控制
# ==============================================================================


def main_workflow(
    bdmv_path: Path,
    output_dir: Path,
    bdinfo_path: Optional[Path] = None,
    original_lang: str = "eng",
    skip_interactive: bool = False,
    preconfirmed_config: Optional[Dict] = None,
    drop_commentary: bool = False,
    keep_best_audio: bool = False,
    simplify_subs: bool = True,  # 新增
    source_name: Optional[str] = None,
):
    """主流程封装

    Args:
        bdmv_path: BDMV 目录的 Path 对象
        output_dir: 最终 MKV 输出目录
        bdinfo_path: 对应的 BDInfo 文本路径（可选）
        original_lang: 原语言代码（默认 "eng"）
        skip_interactive: 是否跳过交互式编辑阶段
        preconfirmed_config: 预确认的配置字典（用于预确认模式跳过前面的扫描和交互）
        drop_commentary: 是否全局剔除导评轨
        keep_best_audio: 是否每种语言仅保留最优音轨
        simplify_subs: 是否精简外语字幕（英语/原语言仅保留排序最优的一条）
        source_name: 原盘名称（可选，传入以便在交互界面的表格中提示用户）
    """
    console = Console()

    # 如果提供了预确认配置，直接跳到 Remux 阶段
    if preconfirmed_config:
        console.print("[dim]→ 使用预确认配置，跳过正片和轨道选择[/dim]")

        # 从预确认配置中提取信息
        old_mpls_path = preconfirmed_config["mpls_path"]
        mpls_path = bdmv_path / "PLAYLIST" / old_mpls_path.name
        chapter = preconfirmed_config["chapter"]
        final_tracks = preconfirmed_config["final_tracks"]
    else:
        # 正常流程：阶段 1-5（可重试循环）
        auto_skip_phase1 = True

        while True:
            # 阶段 1：扫描 MPLS 并选择正片
            result = workflow_phase1_scan_mpls(bdmv_path, console, auto_skip=auto_skip_phase1)
            if result == (None, None):  # back
                console.print("[yellow]已经是第一步，无法返回[/yellow]")
                auto_skip_phase1 = False
                continue
            chapter, mpls_path = result

            # 阶段 2：BDInfo 整合（可选）
            try:
                bdinfo_data = workflow_phase2_parse_bdinfo(bdinfo_path, mpls_path.name, console)
            except ValueError as e:
                if str(e) == "RESELECT_MPLS":
                    console.print("[yellow]→ 返回重新选择正片 MPLS...[/yellow]")
                    auto_skip_phase1 = False
                    continue
                elif str(e) == "SKIP_DISC":
                    console.print("[yellow]→ 已手动跳过该原盘[/yellow]")
                    return "skipped"
                else:
                    raise

            # 阶段 3：扫描轨道
            tracks = workflow_phase3_scan_tracks(bdmv_path, chapter, console)

            # 阶段 4：轨道排序
            video_tracks, audio_tracks, subtitle_tracks, view_data = workflow_phase4_integrate_bdinfo(
                tracks, bdinfo_data, original_lang, drop_commentary, keep_best_audio, simplify_subs, console
            )
            final_tracks = video_tracks + audio_tracks + subtitle_tracks

            # 阶段 5：交互式编辑
            cli = InteractiveCLI()
            if not skip_interactive:
                console.print("\n[bold cyan]阶段 5：交互式编辑[/bold cyan]")
                result = cli.edit_loop(final_tracks, view_data, source_name=source_name)
                if result is None:  # back
                    console.print("[yellow]已返回上一阶段，将重新选择正片和轨道[/yellow]")
                    # 从交互阶段退回时，关闭静默跳过，强制显示表格！
                    auto_skip_phase1 = False
                    continue  # 重新从阶段 1 开始
                final_tracks = result
            else:
                console.print("\n[bold cyan]阶段 5：跳过交互式编辑[/bold cyan]")
                cli.display_tracks(final_tracks, "最终轨道配置", source_name=source_name)

            break  # 正常完成，退出选择循环

    # 阶段 6：提取元数据
    title, metadata, chapters_file = workflow_phase6_extract_metadata(bdmv_path, chapter, output_dir, console)

    # 阶段 7：Remux 执行
    success = workflow_phase7_remux(output_dir, mpls_path, final_tracks, title, metadata, chapters_file, console)

    # 阶段 8：输出摘要
    if success:
        output_file = output_dir / f"{title}.mkv"
        console.print("\n[bold green]✓ Remux 完成！[/bold green]")
        console.print(f"输出文件：{output_file}")
        console.print(f"文件大小：{format_size(output_file.stat().st_size)}")
        console.print(f"视频轨：{sum(1 for t in final_tracks if t.type == 'video')} 个")
        console.print(f"音频轨：{sum(1 for t in final_tracks if t.type == 'audio')} 个")
        console.print(f"字幕轨：{sum(1 for t in final_tracks if t.type == 'subtitle')} 个")
        console.print(f"章节数：{chapter.get_chapter_count()} 个\n")
        return "success"
    else:
        console.print("\n[bold red]✗ Remux 失败[/bold red]\n")
        return "failed"


# ==============================================================================
# 命令行参数解析
# ==============================================================================


def parse_arguments():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Blu-ray Batch Remux Script - 批量蓝光原盘 Remux 工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 批量处理目录中的所有原盘
  python bluray_remux.py -i /path/to/BluRays --bdinfo-dir /path/to/bdinfos -o /output

  # 跳过交互式编辑（全自动）
  python bluray_remux.py -i /path/to/BluRays -o /output --skip-interactive

  # 自动丢弃导评，并且每种语言只保留最高规格音轨
  python bluray_remux.py -i /path/to/BluRays -o /output --commentary drop --best-audio yes
        """,
    )

    # 必选参数
    parser.add_argument("-i", "--input", type=str, required=True, help="包含所有原盘的根目录（会递归扫描子目录）")
    # 可选参数
    parser.add_argument("--bdinfo-dir", type=str, default=None, help="统一 BDInfo 目录（可选，也支持同名 bdinfo.txt）")
    parser.add_argument("-o", "--output", type=str, default="./output", help="输出基础目录（默认：./output）")
    parser.add_argument("--skip-interactive", action="store_true", help="跳过交互式编辑（全自动批量处理）")
    parser.add_argument("--continue-on-error", action="store_true", help="遇到错误时继续处理下一个原盘")
    parser.add_argument("--commentary", type=str, choices=["keep", "drop", "ask"], default=None, help="导评策略 (keep保留/drop丢弃/ask单盘询问)")
    parser.add_argument(
        "--best-audio", type=str, choices=["no", "yes", "ask"], default=None, help="精简最高规格音轨 (no保留所有/yes仅留最高/ask单盘询问)"
    )
    parser.add_argument(
        "--simplify-subs", type=str, choices=["no", "yes", "ask"], default=None, help="精简外语字幕 (yes仅留最优/no保留所有/ask单盘询问)"
    )
    return parser.parse_args()


# ==============================================================================
# 批处理阶段函数
# ==============================================================================


def batch_phase1_scan_sources(root_dir: Path, console: Console) -> List[Dict]:
    """
    批量阶段1：扫描原盘

    Args:
        root_dir: 根目录路径
        console: Rich Console对象

    Returns:
        原盘列表

    Raises:
        SystemExit: 未找到任何原盘或ISO
    """
    console.print("\n[bold cyan]阶段 1：扫描原盘[/bold cyan]")
    sources = scan_bluray_sources(root_dir)

    if not sources:
        console.print("[red]错误：未找到任何原盘或 ISO 文件[/red]")
        sys.exit(1)

    console.print(f"[green]✓ 找到 {len(sources)} 个原盘/ISO[/green]\n")
    return sources


def batch_phase2_match_bdinfo(sources: List[Dict], bdinfo_dir: Optional[Path], continue_on_error: bool, console: Console) -> List[Dict]:
    """
    批量阶段2：匹配BDInfo和推断原语言

    Args:
        sources: 原盘列表
        bdinfo_dir: BDInfo目录（可选）
        continue_on_error: 遇到错误是否继续
        console: Rich Console对象

    Returns:
        任务列表

    Raises:
        SystemExit: 所有原盘都缺少BDInfo
    """
    console.print("[bold cyan]阶段 2：匹配 BDInfo 和推断原语言[/bold cyan]")

    tasks = []
    stdin_is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()

    for source in sources:
        bdinfo_path = find_bdinfo_for_source(source, bdinfo_dir)

        if bdinfo_path is None:
            if stdin_is_tty:
                bdinfo_path = prompt_for_missing_bdinfo_text(source["name"], console)

            if bdinfo_path is None:
                console.print(f"[yellow]警告：未找到 BDInfo - {source['name']}[/yellow]")
                console.print("  [dim]→ 已自动忽略该原盘，将不参与本次后续处理[/dim]")
                continue

        original_lang = infer_original_lang_from_bdinfo(bdinfo_path)

        tasks.append({"source": source, "bdinfo_path": bdinfo_path, "original_lang": original_lang, "status": "pending"})

    if not tasks:
        console.print("[red]错误：没有可处理的任务（所有原盘均缺少 BDInfo）[/red]")
        sys.exit(1)

    console.print(f"[green]✓ 匹配完成，准备处理 {len(tasks)} 个原盘[/green]\n")
    return tasks


def batch_phase3_confirm_tasks(tasks: List[Dict], skip_interactive: bool, console: Console) -> str:
    """
    批量阶段3：任务确认和原语言修改

    Args:
        tasks: 任务列表（会被原地修改）
        skip_interactive: 跳过交互式编辑
        console: Rich Console对象

    Returns:
        批量模式（"sequential"或"preconfirm"）

    Raises:
        SystemExit: 用户取消批量处理
    """
    console.print("[bold cyan]阶段 3：任务列表确认[/bold cyan]")

    # 显示任务列表的辅助函数
    def display_task_table():
        table = Table(title="批量处理任务列表", show_header=True, header_style="bold cyan")
        table.add_column("#", style="dim", width=3, justify="right")
        table.add_column("原盘名称", width=40)
        table.add_column("类型", width=6, justify="center")
        table.add_column("BDInfo", width=6, justify="center")
        table.add_column("原语言", width=6, justify="center")

        for idx, task in enumerate(tasks, start=1):
            source = task["source"]
            # 转换原语言为 BCP 47 格式（2 字母代码）（解除下面被注释的两行，并注释 task["original_lang"]）
            # original_lang_display = normalize_language_code(task["original_lang"], "alpha_2")
            table.add_row(
                str(idx),
                truncate_to_display_width(source["name"], 40, "..."),
                source["type"].upper(),
                "✓" if task["bdinfo_path"] else "✗",
                task["original_lang"],
                # original_lang_display,
            )

        console.print(table)

    display_task_table()

    # 交互式修改原语言
    if not skip_interactive:
        console.print("\n[yellow]提示：原语言已自动推断（基于 BDInfo 第一个音轨）[/yellow]")
        console.print("[dim]命令：[/dim]")
        console.print("[dim]  lang <编号> <语言代码>  - 修改指定原盘的原语言（如：lang 1 eng）[/dim]")
        console.print("[dim]  done 或直接回车         - 确认并继续批量处理[/dim]")
        console.print("[dim]  cancel                  - 取消批量处理[/dim]\n")

        while True:
            cmd = interactive_input(">>> ").strip()

            if not cmd or cmd.lower() == "done":
                break
            elif cmd.lower() == "cancel":
                console.print("[red]已取消批量处理[/red]")
                sys.exit(0)
            elif cmd.lower().startswith("lang "):
                try:
                    parts = cmd.split()
                    if len(parts) != 3:
                        console.print("[red]格式错误，请使用：lang <编号> <语言代码>[/red]")
                        continue

                    task_idx = int(parts[1]) - 1
                    new_lang = parts[2]

                    if task_idx < 0 or task_idx >= len(tasks):
                        console.print(f"[red]错误：编号超出范围（1-{len(tasks)}）[/red]")
                        continue

                    old_lang = tasks[task_idx]["original_lang"]
                    tasks[task_idx]["original_lang"] = new_lang
                    console.print(f"[green]✓ 已将 #{task_idx + 1} 的原语言从 {old_lang} 修改为 {new_lang}[/green]\n")

                    display_task_table()
                    console.print()

                except ValueError:
                    console.print("[red]错误：编号必须是数字[/red]")
                except Exception as e:
                    console.print(f"[red]错误：{e}[/red]")
            else:
                console.print("[red]未知命令，请输入 lang <编号> <语言代码>、done 或 cancel[/red]")

        console.print("[green]✓ 任务确认完成[/green]\n")

        # 模式选择
        console.print("[bold cyan]批量处理模式选择[/bold cyan]")
        console.print("[yellow]请选择批量处理模式：[/yellow]")
        console.print("  [cyan]1[/cyan] - 逐个确认模式（每个原盘处理前确认正片和轨道）")
        console.print("  [cyan]2[/cyan] - 统一预确认模式（提前确认所有原盘，然后自动处理）")
        console.print()

        while True:
            mode_choice = interactive_input("请选择模式 [1/2] (默认 1): ").strip()
            if not mode_choice:
                mode_choice = "1"

            if mode_choice in ["1", "2"]:
                break
            else:
                console.print("[red]无效选择，请输入 1 或 2[/red]")

        batch_mode = "sequential" if mode_choice == "1" else "preconfirm"

        if batch_mode == "sequential":
            console.print("[green]✓ 已选择：逐个确认模式[/green]\n")
        else:
            console.print("[green]✓ 已选择：统一预确认模式[/green]\n")
    else:
        batch_mode = "sequential"

    return batch_mode


def _preconfirm_single_disc(
    task: Dict,
    disc_index: int,
    total: int,
    tasks: List[Dict],
    mount_manager: Optional[ISOmountManager],
    global_drop_commentary: Optional[bool],
    global_keep_best_audio: Optional[bool],
    global_simplify_subs: Optional[bool],
    console: Console,
) -> Literal["next", "prev", "error"]:
    """
    预确认单个原盘的轨道配置

    处理单个原盘的完整预确认流程：询问过滤策略 → 挂载 ISO（如需要）→ 选择正片 →
    解析 BDInfo → 扫描轨道 → 整合排序 → 交互编辑。支持内部重试（back 返回重选正片）
    和跨盘回退（back 返回上一个原盘）。

    Args:
        task: 任务字典（含 source, bdinfo_path, original_lang 等，
              成功时写入 preconfirm_config，失败时写入 preconfirm_error）
        disc_index: 当前原盘在任务列表中的索引（用于判断是否可以回退）
        total: 任务总数（用于显示进度）
        tasks: 完整任务列表（回退时需要清理上一个任务的配置）
        mount_manager: ISO 挂载管理器（处理 ISO 文件时使用）
        global_drop_commentary: 全局导评策略（True=丢弃, False=保留, None=逐盘询问）
        global_keep_best_audio: 全局精简策略（True=精简, False=不精简, None=逐盘询问）
        global_simplify_subs: 全局外语字幕精简策略（True=精简, False=保留, None=逐盘询问）
        console: Rich Console 对象

    Returns:
        "next": 成功完成或出错，前进到下一个原盘
        "prev": 用户选择返回上一个原盘
        "error": 发生不可恢复错误（由调用方决定是否中止）
    """
    source = task["source"]
    console.print(f"\n[bold yellow]=== 配置确认 {disc_index + 1}/{total}: {source['name']} ===[/bold yellow]")

    disc_drop_commentary = global_drop_commentary
    if disc_drop_commentary is None:
        ans = Confirm.ask(f"[yellow]是否保留导评音轨和字幕？[/yellow]", default=True)
        disc_drop_commentary = not ans

    disc_keep_best_audio = global_keep_best_audio
    if disc_keep_best_audio is None:
        disc_keep_best_audio = Confirm.ask(f"[yellow]是否为每种语言仅保留一条最高规格音轨？[/yellow]", default=False)

    disc_simplify_subs = global_simplify_subs
    if disc_simplify_subs is None:
        disc_simplify_subs = Confirm.ask(f"[yellow]是否精简外语字幕（仅保留一条英语/原语言字幕）？[/yellow]", default=True)

    # 处理 ISO 或目录源
    bdmv_path = _process_iso_source(source, mount_manager, console) if source["type"] == "iso" else source["bdmv_path"]

    auto_skip_mpls = True
    while True:
        # 1. 扫描 MPLS 并选择正片
        console.print("\n[cyan]→ 扫描 MPLS 文件[/cyan]")
        try:
            chapter, mpls_path = select_main_playlist(bdmv_path, console, source["name"], auto_skip=auto_skip_mpls)
        except RuntimeError:
            console.print(f"[red]✗ {source['name']} - 未找到符合条件的正片 MPLS[/red]")
            task["preconfirm_error"] = "未找到正片 MPLS"
            return "next"

        # 用户在正片选择界面输入 back
        if chapter is None or mpls_path is None:
            if disc_index == 0:
                console.print("[yellow]已经是第一个原盘，无法返回上一原盘[/yellow]")
                continue  # 留在当前盘重试

            # 回退到上一个原盘
            prev_task = tasks[disc_index - 1]
            prev_task.pop("preconfirm_config", None)
            prev_task.pop("preconfirm_error", None)
            console.print(f"[yellow]返回上一个原盘：{prev_task['source']['name']}[/yellow]")
            return "prev"

        # 2. 解析 BDInfo
        try:
            bdinfo_data = parse_bdinfo_optional(task["bdinfo_path"], mpls_path.name, console, verify_match=True)
        except ValueError as e:
            if str(e) == "RESELECT_MPLS":
                console.print("[yellow]→ 返回重新选择正片 MPLS...[/yellow]")
                auto_skip_mpls = False
                continue
            elif str(e) == "SKIP_DISC":
                console.print("[yellow]→ 已手动跳过该原盘[/yellow]")
                task["preconfirm_error"] = "播放列表不匹配，用户选择跳过"
                return "next"
            else:
                raise

        if bdinfo_data:
            console.print(f"[green]✓ 已解析 BDInfo[/green]")
            console.print(f"  PLAYLIST：{bdinfo_data['playlist']}")
            console.print(f"  音轨：{len(bdinfo_data['audio'])} 个")
            console.print(f"  字幕：{len(bdinfo_data['subtitle'])} 个\n")

        # 3. 扫描轨道
        console.print("[cyan]→ 扫描轨道信息[/cyan]")
        try:
            tracks = scan_main_tracks(bdmv_path, chapter, console, verbose=False)
            console.print(f"[green]✓ 扫描完成[/green]")
            console.print(f"  视频轨：{sum(1 for t in tracks if t.type == 'video')} 个")
            console.print(f"  音频轨：{sum(1 for t in tracks if t.type == 'audio')} 个")
            console.print(f"  字幕轨：{sum(1 for t in tracks if t.type == 'subtitle')} 个\n")
        except RuntimeError as e:
            console.print(f"[red]✗ {source['name']} - {e}[/red]")
            task["preconfirm_error"] = str(e)
            return "next"

        # 4. 整合 BDInfo 并准备轨道
        try:
            validate_audio_track_indices(tracks, console, allow_cancel=True)
        except RuntimeError:
            console.print("[red]已取消处理[/red]")
            task["preconfirm_error"] = "用户取消处理"
            return "next"

        video_tracks, audio_tracks, subtitle_tracks, view_data = integrate_and_prepare_tracks(
            tracks, bdinfo_data, task["original_lang"], disc_drop_commentary, disc_keep_best_audio, disc_simplify_subs
        )

        console.print(f"[green]✓ 排序完成（原始语言：{task['original_lang']}）[/green]\n")

        # 合并轨道列表
        final_tracks = video_tracks + audio_tracks + subtitle_tracks

        # 5. 交互式编辑轨道
        console.print("\n[cyan]→ 轨道配置确认[/cyan]")
        cli = InteractiveCLI()
        result = cli.edit_loop(final_tracks, view_data, source_name=source["name"])
        if result is None:
            # 用户选择返回：在当前原盘内重新选择正片和轨道
            console.print("[yellow]已返回上一阶段，将重新选择正片和轨道[/yellow]")
            auto_skip_mpls = False
            continue  # 留在当前盘重选
        final_tracks = result

        # 6. 保存预确认的配置
        task["preconfirm_config"] = {
            "mpls_path": mpls_path,
            "chapter": chapter,
            "final_tracks": final_tracks,
            "bdinfo_data": bdinfo_data,
        }

        console.print(f"[green]✓ {source['name']} - 配置确认完成[/green]")
        return "next"


def batch_phase3_5_preconfirm(
    tasks: List[Dict],
    continue_on_error: bool,
    global_drop_commentary: Optional[bool],
    global_keep_best_audio: Optional[bool],
    global_simplify_subs: Optional[bool],
    console: Console,
) -> None:
    """
    批量阶段3.5：统一预确认所有原盘配置（修改 tasks in-place）

    Args:
        tasks: 任务列表（会被原地修改，添加 preconfirm_config）
        continue_on_error: 遇到错误是否继续
        global_drop_commentary: 全局导评策略
        global_keep_best_audio: 全局音轨精简策略
        global_simplify_subs: 全局外语字幕精简策略
        console: Rich Console对象

    Notes:
        此阶段仅在 batch_mode="preconfirm" 时执行
        为每个任务扫描MPLS、解析BDInfo、整合轨道并交互式编辑
        配置保存到 task["preconfirm_config"] 用于后续自动处理
    """
    console.print("[bold cyan]阶段 3.5：统一预确认所有原盘配置[/bold cyan]")
    console.print("[yellow]开始依次确认每个原盘的正片和轨道配置...[/yellow]\n")

    mount_manager_preconf = None

    try:
        disc_index = 0
        total = len(tasks)

        while disc_index < total:
            task = tasks[disc_index]
            source = task["source"]

            # 延迟初始化 ISO 挂载管理器
            if mount_manager_preconf is None and source["type"] == "iso":
                mount_manager_preconf = ISOmountManager()

            try:
                action = _preconfirm_single_disc(
                    task,
                    disc_index,
                    total,
                    tasks,
                    mount_manager_preconf,
                    global_drop_commentary,
                    global_keep_best_audio,
                    global_simplify_subs,
                    console,
                )

                if action == "next":
                    disc_index += 1
                elif action == "prev":
                    disc_index -= 1

            except Exception as e:
                console.print(f"[red]✗ {source['name']} - 配置确认失败：{e}[/red]")
                task["preconfirm_error"] = str(e)
                if not continue_on_error:
                    console.print("[red]中止预确认过程（使用 --continue-on-error 继续）[/red]")
                    break
                disc_index += 1

            finally:
                # 仅在外层推进到上一盘/下一盘时，才会执行卸载
                if source["type"] == "iso" and mount_manager_preconf:
                    mount_manager_preconf.unmount_last()

    finally:
        # 清理所有挂载
        if mount_manager_preconf:
            mount_manager_preconf.unmount_all()

    console.print("\n[green]✓ 所有原盘配置确认完成，开始批量处理[/green]\n")


def _run_single_remux_task(
    task: Dict,
    output_dir: Path,
    skip_interactive: bool,
    global_drop_commentary: Optional[bool],
    global_keep_best_audio: Optional[bool],
    global_simplify_subs: Optional[bool],
    mount_manager: Optional[ISOmountManager],
    console: Console,
) -> Literal["success", "failed", "skipped"]:
    """
    执行单个原盘的 Remux 任务

    包括挂载 ISO（如需要）、调用 main_workflow、卸载 ISO。
    如果任务有 preconfirm_config，使用预确认配置跳过交互。
    否则执行正常的（交互或静默）流程。

    Args:
        task: 任务字典（含 source, bdinfo_path, original_lang 和可选的 preconfirm_config）
        output_dir: 输出基础目录
        skip_interactive: 是否跳过交互式编辑
        global_drop_commentary: 全局导评策略（True=丢弃, False=保留, None=逐盘询问）
        global_keep_best_audio: 全局音轨精简策略（True=精简, False=不精简, None=逐盘询问）
        global_simplify_subs: 全局外语字幕精简策略（True=精简, False=保留, None=逐盘询问）
        mount_manager: ISO 挂载管理器
        console: Rich Console 对象

    Returns:
        "success": Remux 成功
        "failed": Remux 失败
        "skipped": 任务被跳过（如预确认阶段已标记错误）
    """
    source = task["source"]

    # 跳过预确认失败的任务
    if "preconfirm_error" in task:
        console.print(f"[yellow]⊘ 跳过（配置确认失败：{task['preconfirm_error']}）[/yellow]")
        task["status"] = "skipped"
        return "skipped"

    task["status"] = "processing"

    # 处理 ISO 或目录源
    bdmv_path = _process_iso_source(source, mount_manager, console) if source["type"] == "iso" else source["bdmv_path"]

    movie_output_dir = output_dir / sanitize_filename(source["name"])

    # 判断是否使用预确认配置
    if "preconfirm_config" in task:
        console.print("[dim]使用预确认配置...[/dim]")
        config = task["preconfirm_config"]

        result = main_workflow(
            bdmv_path=bdmv_path,
            output_dir=movie_output_dir,
            bdinfo_path=task["bdinfo_path"],
            original_lang=task["original_lang"],
            skip_interactive=True,
            preconfirmed_config=config,
            drop_commentary=False,
            keep_best_audio=False,
            simplify_subs=False,
            source_name=source["name"],
        )
    else:
        # --- 动态解析单盘过滤策略 ---
        disc_drop_commentary = global_drop_commentary
        if disc_drop_commentary is None and not skip_interactive:
            console.print(f"\n[cyan]→ {source['name']} 轨道过滤策略[/cyan]")
            ans = Confirm.ask("[yellow]是否保留该原盘的导评音轨和字幕？[/yellow]", default=True)
            disc_drop_commentary = not ans
        elif disc_drop_commentary is None:
            disc_drop_commentary = False

        disc_keep_best_audio = global_keep_best_audio
        if disc_keep_best_audio is None and not skip_interactive:
            disc_keep_best_audio = Confirm.ask("[yellow]是否为该原盘每种语言仅保留一条最高规格音轨？[/yellow]", default=False)
        elif disc_keep_best_audio is None:
            disc_keep_best_audio = False

        disc_simplify_subs = global_simplify_subs
        if disc_simplify_subs is None and not skip_interactive:
            disc_simplify_subs = Confirm.ask("[yellow]是否精简该原盘的外语字幕（仅保留一条最优）？[/yellow]", default=True)
        elif disc_simplify_subs is None:
            disc_simplify_subs = True

        # 正常流程（逐个确认模式）
        result = main_workflow(
            bdmv_path=bdmv_path,
            output_dir=movie_output_dir,
            bdinfo_path=task["bdinfo_path"],
            original_lang=task["original_lang"],
            skip_interactive=skip_interactive,
            drop_commentary=disc_drop_commentary,
            keep_best_audio=disc_keep_best_audio,
            simplify_subs=disc_simplify_subs,
            source_name=source["name"],
        )

    if result == "success":
        task["status"] = "success"
        console.print(f"[green]✓ {source['name']} 处理成功[/green]")
        return "success"
    elif result == "skipped":
        task["status"] = "skipped"
        console.print(f"[yellow]⊘ {source['name']} 已跳过处理[/yellow]")
        return "skipped"
    else:
        task["status"] = "failed"
        console.print(f"[red]✗ {source['name']} 处理失败[/red]")
        return "failed"


def batch_phase4_remux(
    tasks: List[Dict],
    output_dir: Path,
    skip_interactive: bool,
    continue_on_error: bool,
    global_drop_commentary: Optional[bool],
    global_keep_best_audio: Optional[bool],
    global_simplify_subs: Optional[bool],  # 【新增参数】
    console: Console,
) -> Tuple[int, int, int]:
    """
    批量阶段4：批量 Remux 处理

    Args:
        tasks: 任务列表（会被原地修改，更新 status 字段）
        output_dir: 输出基础目录
        skip_interactive: 跳过交互式编辑
        continue_on_error: 遇到错误是否继续
        global_drop_commentary: 全局导评策略
        global_keep_best_audio: 全局音轨精简策略
        global_simplify_subs: 全局外语字幕精简策略
        console: Rich Console对象

    Returns:
        (成功数, 失败数, 跳过数)

    Notes:
        遍历所有任务并执行 main_workflow()
        如果任务有 preconfirm_config，使用预确认配置
        否则执行正常的交互式流程
    """
    console.print("\n[bold cyan]阶段 4：批量 Remux[/bold cyan]")

    mount_manager = None
    success_count = 0
    failed_count = 0
    skipped_count = 0

    try:
        for idx, task in enumerate(tasks, start=1):
            source = task["source"]
            console.print(f"\n[bold yellow]=== 处理 {idx}/{len(tasks)}: {source['name']} ===[/bold yellow]")

            # 延迟初始化 ISO 挂载管理器
            if mount_manager is None and source["type"] == "iso":
                mount_manager = ISOmountManager()

            try:
                status = _run_single_remux_task(
                    task, output_dir, skip_interactive, global_drop_commentary, global_keep_best_audio, global_simplify_subs, mount_manager, console
                )

                if status == "success":
                    success_count += 1
                elif status == "failed":
                    failed_count += 1
                    if not continue_on_error:
                        console.print("[red]中止批量处理（使用 --continue-on-error 继续）[/red]")
                        break
                else:
                    skipped_count += 1

            except Exception as e:
                task["status"] = "failed"
                failed_count += 1
                console.print(f"[red]✗ {source['name']} 出错：{e}[/red]")

                if not continue_on_error:
                    console.print("[red]中止批量处理（使用 --continue-on-error 继续）[/red]")
                    break

            finally:
                # 卸载当前 ISO（如果已挂载）
                if source["type"] == "iso" and mount_manager:
                    mount_manager.unmount_last()

    except KeyboardInterrupt:
        if mount_manager and not mount_manager._cleanup_msg_shown:
            console.print("\n[yellow]用户中断操作，正在清理...[/yellow]")
            mount_manager._cleanup_msg_shown = True
    finally:
        # 清理挂载
        if mount_manager:
            mount_manager.unmount_all()

    return success_count, failed_count, skipped_count


def batch_phase5_report(tasks: List[Dict], success_count: int, failed_count: int, skipped_count: int, console: Console) -> None:
    """
    批量阶段5：生成处理报告

    Args:
        tasks: 任务列表
        success_count: 成功数量
        failed_count: 失败数量
        skipped_count: 跳过数量
        console: Rich Console对象
    """
    console.print("\n[bold cyan]阶段 5：批量处理报告[/bold cyan]")

    report_table = Table(title="处理结果", show_header=True, header_style="bold cyan")
    report_table.add_column("原盘名称", width=40)
    report_table.add_column("状态", width=10)

    for task in tasks:
        status_display = {
            "success": "[green]✓ 成功[/green]",
            "failed": "[red]✗ 失败[/red]",
            "skipped": "[yellow]⊘ 跳过[/yellow]",
            "pending": "[dim]- 未处理[/dim]",
        }
        report_table.add_row(truncate_to_display_width(task["source"]["name"], 40, "..."), status_display.get(task["status"], task["status"]))

    console.print(report_table)

    console.print(f"\n[bold]总计：[/bold]")
    console.print(f"  成功：{success_count}")
    console.print(f"  失败：{failed_count}")
    console.print(f"  跳过：{skipped_count}")
    console.print(f"  总数：{len(tasks)}\n")


def main():
    """主函数 - 批量处理模式"""
    console = Console()

    # 检查工具链
    check_tools()
    print()

    # 解析参数
    args = parse_arguments()

    # 清理路径参数
    root_dir = Path(clean_path(args.input))
    output_dir = Path(clean_path(args.output))
    bdinfo_dir = Path(clean_path(args.bdinfo_dir)) if args.bdinfo_dir else None

    # 验证路径
    if not root_dir.exists():
        console.print(f"[red]错误：根目录不存在：{root_dir}[/red]")
        sys.exit(1)

    if bdinfo_dir and not bdinfo_dir.exists():
        console.print(f"[red]错误：BDInfo 目录不存在：{bdinfo_dir}[/red]")
        sys.exit(1)

    # 执行批量处理流程
    try:
        # 阶段 1：扫描原盘
        sources = batch_phase1_scan_sources(root_dir, console)

        # 阶段 2：匹配 BDInfo 和推断原语言
        tasks = batch_phase2_match_bdinfo(sources, bdinfo_dir, args.continue_on_error, console)

        # 优先使用命令行参数，如果未指定则设为安全默认值 (保留导评，不精简音轨，只使用脚本默认过滤规则)
        # 解析全局过滤策略 (True=处理, False=不处理, None=单盘询问)
        global_drop_commentary = False
        global_keep_best_audio = False

        if args.skip_interactive:
            # 静默模式下，ask 会退化为安全默认值 (keep / no)
            global_drop_commentary = args.commentary == "drop"
            global_keep_best_audio = args.best_audio == "yes"
            global_simplify_subs = args.simplify_subs != "no"
        else:
            if args.commentary is None or args.best_audio is None or args.simplify_subs is None:
                console.print("\n[bold cyan]阶段 2.5：全局轨道过滤策略配置[/bold cyan]")
                console.print()

                if args.commentary is None:
                    choice = Prompt.ask(
                        "[yellow]导评轨道处理策略[/yellow]\n  [cyan]keep[/cyan]: 全局保留 (默认)\n  [cyan]drop[/cyan]: 全局剔除\n  [cyan]ask[/cyan] : 每个原盘单独询问\n请选择",
                        choices=["keep", "drop", "ask"],
                        default="keep",
                    )
                    global_drop_commentary = _parse_tri_state(choice, true_val="drop")
                else:
                    global_drop_commentary = _parse_tri_state(args.commentary, true_val="drop")

                if args.best_audio is None:
                    choice = Prompt.ask(
                        "[yellow]最高规格音轨精简策略[/yellow]\n  [cyan]no[/cyan]  : 按脚本默认规则保留音轨 (默认)\n  [cyan]yes[/cyan] : 全局每种语言仅留一条最高规格\n  [cyan]ask[/cyan] : 每个原盘单独询问\n请选择",
                        choices=["no", "yes", "ask"],
                        default="no",
                    )
                    global_keep_best_audio = _parse_tri_state(choice, true_val="yes")
                else:
                    global_keep_best_audio = _parse_tri_state(args.best_audio, true_val="yes")

                if args.simplify_subs is None:
                    choice = Prompt.ask(
                        "[yellow]外语字幕精简策略[/yellow]\n  [cyan]yes[/cyan]: 英语/原语言仅保留一条最优 (默认)\n  [cyan]no[/cyan] : 保留所有支持的外语字幕\n  [cyan]ask[/cyan] : 每个原盘单独询问\n请选择",
                        choices=["yes", "no", "ask"],
                        default="yes",
                    )
                    global_simplify_subs = _parse_tri_state(choice, true_val="yes")
                else:
                    global_simplify_subs = _parse_tri_state(args.simplify_subs, true_val="yes")

                console.print()
            else:
                global_drop_commentary = _parse_tri_state(args.commentary, true_val="drop")
                global_keep_best_audio = _parse_tri_state(args.best_audio, true_val="yes")
                global_simplify_subs = _parse_tri_state(args.simplify_subs, true_val="yes")

        # 阶段 3：任务确认和原语言修改
        batch_mode = batch_phase3_confirm_tasks(tasks, args.skip_interactive, console)

        # 阶段 3.5：统一预确认
        if not args.skip_interactive and batch_mode == "preconfirm":
            batch_phase3_5_preconfirm(tasks, args.continue_on_error, global_drop_commentary, global_keep_best_audio, global_simplify_subs, console)

        # 阶段 4：批量 Remux
        success_count, failed_count, skipped_count = batch_phase4_remux(
            tasks,
            output_dir,
            args.skip_interactive,
            args.continue_on_error,
            global_drop_commentary,
            global_keep_best_audio,
            global_simplify_subs,
            console,
        )

        # 阶段 5：生成报告
        batch_phase5_report(tasks, success_count, failed_count, skipped_count, console)

        sys.exit(0 if failed_count == 0 else 1)

    except KeyboardInterrupt:
        console.print("\n[yellow]用户中断操作[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    main()
