"""Chinese segmentation for the local search index.

SQLite FTS5's unicode61 tokenizer does not segment Chinese. A run of Han
characters becomes one token. This module segments first, folds traditional
characters to simplified only in the index token, and keeps original offsets.
The FTS table then indexes the space-separated folded tokens.
"""

from __future__ import annotations

SEGMENT_VERSION = "zh-seg-1"

# Traditional character to one simplified character. Stored source text is never
# rewritten; only the index token is folded.
_FOLD_PAIRS = (
    ("華", "华"), ("為", "为"), ("臺", "台"), ("灣", "湾"), ("開", "开"), ("會", "会"),
    ("個", "个"), ("們", "们"), ("這", "这"), ("時", "时"), ("間", "间"), ("說", "说"),
    ("對", "对"), ("與", "与"), ("從", "从"), ("來", "来"), ("後", "后"), ("過", "过"),
    ("還", "还"), ("學", "学"), ("習", "习"), ("經", "经"), ("濟", "济"), ("網", "网"),
    ("絡", "络"), ("體", "体"), ("現", "现"), ("實", "实"), ("業", "业"), ("產", "产"),
    ("員", "员"), ("問", "问"), ("題", "题"), ("點", "点"), ("線", "线"), ("際", "际"),
    ("關", "关"), ("應", "应"), ("該", "该"), ("麼", "么"), ("裡", "里"), ("見", "见"),
    ("觀", "观"), ("眾", "众"), ("聲", "声"), ("聞", "闻"), ("聽", "听"), ("請", "请"),
    ("謝", "谢"), ("認", "认"), ("識", "识"), ("議", "议"), ("論", "论"), ("記", "记"),
    ("錄", "录"), ("報", "报"), ("導", "导"), ("師", "师"), ("醫", "医"), ("療", "疗"),
    ("藥", "药"), ("衛", "卫"), ("運", "运"), ("動", "动"), ("場", "场"), ("館", "馆"),
    ("區", "区"), ("縣", "县"), ("鄉", "乡"), ("鎮", "镇"), ("廣", "广"), ("東", "东"),
    ("亞", "亚"), ("歐", "欧"), ("麗", "丽"), ("強", "强"), ("獨", "独"), ("權", "权"),
    ("無", "无"), ("發", "发"), ("機", "机"), ("電", "电"), ("話", "话"), ("門", "门"),
    ("車", "车"), ("書", "书"), ("長", "长"), ("國", "国"), ("語", "语"), ("雲", "云"),
    ("風", "风"), ("飛", "飞"), ("馬", "马"), ("鳥", "鸟"), ("魚", "鱼"), ("頁", "页"),
    ("係", "系"), ("於", "于"), ("並", "并"), ("餘", "余"),
)

FOLD = {traditional: simplified for traditional, simplified in _FOLD_PAIRS}

# Simplified words. Matching compares the folded form, so traditional text can
# hit these entries without changing the stored original.
DICTIONARY = frozenset({
    "华为", "深圳", "发布", "手机", "台湾", "开会", "北京大学", "北京", "大学",
    "合作", "记者", "李明", "到场", "嘉宾", "出席", "公司", "本地", "记忆",
    "检索", "帖子", "一条", "可以", "模拟", "备忘",
})
_MAX_WORD = max(len(word) for word in DICTIONARY)


def fold_char(char: str) -> str:
    return FOLD.get(char, char)


def fold_token(text: str) -> str:
    return "".join(fold_char(char) for char in text).casefold()


def is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
    )


def _is_ascii_word(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char in "._+-")


def segment(text: str) -> list[dict]:
    """Return tokens with original offsets. Folding is not applied to offsets."""

    tokens: list[dict] = []
    length = len(text)
    index = 0
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if _is_ascii_word(char):
            end = index + 1
            while end < length and _is_ascii_word(text[end]):
                end += 1
            original = text[index:end]
            tokens.append({
                "token": original,
                "folded": fold_token(original),
                "start": index,
                "end": end,
            })
            index = end
            continue
        if is_cjk(char):
            matched = _longest_word(text, index)
            if matched:
                original = text[index:matched]
                tokens.append({
                    "token": original,
                    "folded": fold_token(original),
                    "start": index,
                    "end": matched,
                })
                index = matched
                continue
            if index + 1 < length and is_cjk(text[index + 1]):
                original = text[index:index + 2]
                tokens.append({
                    "token": original,
                    "folded": fold_token(original),
                    "start": index,
                    "end": index + 2,
                })
                index += 1
                continue
            tokens.append({
                "token": char,
                "folded": fold_token(char),
                "start": index,
                "end": index + 1,
            })
            index += 1
            continue
        index += 1
    return tokens


def _longest_word(text: str, index: int) -> int:
    limit = min(len(text), index + _MAX_WORD)
    for end in range(limit, index + 1, -1):
        if fold_token(text[index:end]) in DICTIONARY:
            return end
    return 0


def segmented_text(text: str) -> str:
    """Space-separated folded tokens. This is the only string FTS5 should see."""

    return " ".join(item["folded"] for item in segment(text) if item["folded"])


def search_tokens(text: str) -> list[str]:
    """Folded query tokens.

    An unknown two-character name is indexed as an overlapping bigram, and the
    segmenter then looks at the second character again. On a two-character
    query that second character would become its own required token, which the
    longer source text does not store. A token whose span sits inside a longer
    token from the same query is not required.
    """

    tokens = [item for item in segment(text) if item["folded"]]
    kept: list[str] = []
    for item in tokens:
        span = item["end"] - item["start"]
        covered = any(
            other is not item
            and other["start"] <= item["start"]
            and other["end"] >= item["end"]
            and (other["end"] - other["start"]) > span
            for other in tokens
        )
        if covered or item["folded"] in kept:
            continue
        kept.append(item["folded"])
    return kept
