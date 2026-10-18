"""Recognise a chat message that asks for *pictures* ("库里有哪些图片", "有风景画吗").

Such a question is answered from the picture index, with no model call: a text
model can only read text references, so it would answer "nothing found" about
pictures that exist.

The rule is deliberately narrow, because a wrong match hides a real text answer
behind a row of pictures. A message counts only when **all** of these hold:

* it is short (a question, not a pasted document);
* it names a picture (图片, 照片, 风景画, photo, screenshot ...) **at the end** of
  the request, so "有没有图像识别的文档" (a document *about* image recognition) and
  "怎么压缩图片" (a how-to) do not match;
* it has the shape of a request to see or find (找, 有没有, 有哪些, show me, any ...);
* it is not a how-to, an editing task or a question about a particular document.

The result is either ``list`` (no subject: "what pictures are there") or ``search``
with the subject to look for ("风景画", "海边"). Anything unsure returns ``None`` and
goes down the normal text path, so the cost of a miss is only the old behaviour.

Pure functions, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_MESSAGE_CHARS = 80
MAX_SUBJECT_CHARS = 30


@dataclass(frozen=True)
class PictureIntent:
    kind: str  # "list" | "search"
    subject: str  # what to look for; "" for a list
    # True for "有风景图吗" style requests, where 图 can also mean a diagram or chart in a
    # document. These are answered with pictures only when the best match is a real one;
    # otherwise the question goes down the text path as before.
    soft: bool = False


# --------------------------------------------------------------------------- Chinese

# Nouns that carry meaning by themselves: "有风景画吗" looks for 风景画.
_ZH_SPECIFIC = (
    "风景画", "山水画", "水墨画", "简笔画", "油画", "国画", "壁画", "漫画", "素描",
    "插画", "插图", "画作", "海报", "截图",
)  # fmt: skip
# Plain words for "a picture": dropped from the subject.
_ZH_GENERIC = ("图片", "照片", "相片", "图像", "配图", "图集", "相册")
_ZH_NOUN = "|".join(sorted((*_ZH_SPECIFIC, *_ZH_GENERIC), key=len, reverse=True))

_ZH_TRAILING = re.compile(r"[吗嘛么呢吧啊呀的\s?？!！。.，,~～]+$")
_ZH_NOUN_AT_END = re.compile(rf"^(?P<pre>.*?)(?P<noun>{_ZH_NOUN})$")

# "图片有哪些", "所有图片列表": the noun comes first, nothing else is asked.
_ZH_NOUN_FIRST = re.compile(
    rf"^(?:知识库|资料库|库里|库中|我的|所有的?|全部的?)*(?:{'|'.join(_ZH_GENERIC)})"
    r"(?:都|一共)?(?:有哪些|有什么|有多少|有几张|列表|清单|目录)$"
)

# A request to look, as opposed to a statement ("产品有图片").
_ZH_STRONG_SHAPE = re.compile(
    r"找|搜|查|看|瞧|有没有|有无|有哪些|有什么|有些什么|有多少|有几|列出|列一下|展示|显示"
    r"|给我|发我|来几张|来张|拿出|出示"
)
# A bare 有 is a request only when it is phrased as a question.
_ZH_QUESTION_END = re.compile(r"[吗嘛么呢?？]\s*$")

# How-to, editing and technical questions that merely mention a picture.
_ZH_NEGATIVE = re.compile(
    r"怎么|怎样|如何|为什么|为啥|教程|方法|步骤|是什么|什么是|代码|函数|接口|算法|软件|工具"
    r"|格式|大小|尺寸|像素|分辨率|上传|下载|删除|压缩|转换|识别|处理|修改|编辑|制作|设计|生成|创作|画一|帮我画"
)
# The subject points at a particular document or earlier turn, not at the picture index.
_ZH_REFERS_ELSEWHERE = re.compile(r"文档|文件|合同|报告|PDF|pdf|这份|这个|那个|这张|那张|上面|以上|刚才|上述|下面")

_ZH_FILLER = re.compile(
    "|".join(
        sorted(
            [
                "知识库里", "知识库中", "知识库", "资料库里", "资料库中", "资料库", "库里", "库中", "空间里", "空间中",
                "空间", "这里", "这边", "里面", "其中", "帮我", "帮忙", "请问", "请", "麻烦", "一下子", "一下",
                "给我", "发我", "我想要", "我想", "我要", "想要", "需要", "能不能", "可不可以", "可以", "有没有",
                "有无", "有哪些", "有什么", "有些什么", "有多少张", "有多少", "有几张", "有几", "列出", "列一下", "展示", "显示",
                "来几张", "来张", "拿出", "出示", "所有", "全部", "一些", "一点", "几张", "一张", "两张", "三张",
                "几幅", "一幅", "相关的", "相关", "关于", "有关", "看看", "找找", "搜搜", "查查", "找", "搜", "查",
                "看", "瞧", "有", "都", "还", "我们", "我",
            ],
            key=len,
            reverse=True,
        )
    )
)  # fmt: skip


def _classify_zh(text: str) -> PictureIntent | None:
    question = bool(_ZH_QUESTION_END.search(text))
    core = _ZH_TRAILING.sub("", text)
    if not core or _ZH_NEGATIVE.search(core):
        return None
    if _ZH_NOUN_FIRST.match(core):
        return PictureIntent("list", "")
    m = _ZH_NOUN_AT_END.match(core)
    if m is None:
        return _classify_zh_bare_tu(core, question=question)
    pre, noun = m.group("pre"), m.group("noun")
    if not _ZH_STRONG_SHAPE.search(pre) and not (question and "有" in pre):
        return None
    subject = _ZH_FILLER.sub("", pre).strip("的 ,，、")
    if subject and _ZH_REFERS_ELSEWHERE.search(subject):
        return None
    if noun in _ZH_SPECIFIC:
        subject = f"{subject}{noun}" if subject else noun
    if not subject:
        return PictureIntent("list", "")
    if len(subject) > MAX_SUBJECT_CHARS:
        return None
    return PictureIntent("search", subject)


def _classify_zh_bare_tu(core: str, *, question: bool) -> PictureIntent | None:
    """"有风景图吗", "有没有螺丝的图": a bare 图 after a subject. Never a bare "有图吗"."""

    if not core.endswith("图"):
        return None
    pre = core[:-1]
    if not _ZH_STRONG_SHAPE.search(pre) and not (question and "有" in pre):
        return None
    subject = _ZH_FILLER.sub("", pre).strip("的 ,，、")
    if not subject or _ZH_REFERS_ELSEWHERE.search(subject) or len(subject) > MAX_SUBJECT_CHARS:
        return None
    return PictureIntent("search", subject, soft=True)


# --------------------------------------------------------------------------- English

_EN_SPECIFIC = {"screenshot", "drawing", "painting", "illustration", "wallpaper", "poster"}
_EN_NOUN = (
    r"(?:pictures?|photos?|photographs?|images?|screenshots?|drawings?|paintings?"
    r"|illustrations?|wallpapers?|posters?)"
)
_EN_SHAPE = (
    r"(?:show(?: me)?|find(?: me)?|search(?: for)?|look for|list|display|give me|get me|pull up|see"
    r"|do (?:we|you) have|have (?:we|you) got|is there|are there|any|which|what)"
)
_EN_LEAD = r"(?:(?:please|can you|could you|would you|i want to|i need to|i'd like to|let me)\s+)*"
_EN_RE = re.compile(
    rf"^{_EN_LEAD}(?P<shape>{_EN_SHAPE})\b(?P<mid>.*?)\b(?P<noun>{_EN_NOUN})\b(?P<rest>.*)$"
)
_EN_MID_OK = {
    "me", "us", "the", "all", "any", "some", "a", "an", "few", "my", "our", "of", "every", "kb",
    "knowledge", "base", "library", "there", "we", "have", "you", "got", "do", "are", "is", "available",
    "stored", "saved", "uploaded", "here", "in", "from", "many", "what", "which", "pictures", "photos",
    "images", "to",
}  # fmt: skip
_EN_REST = re.compile(r"^\s*(?:of|about|with|showing|featuring|depicting|containing|from|in|for)\s+(?P<sub>.+)$")
_EN_NEGATIVE = re.compile(
    r"\b(?:how|why|docker|container|disk|iso|vm|base image|machine image|resize|compress|convert"
    r"|upload|download|delete|edit|generate|create|draw|format|size|resolution|recogni[sz]e|recognition"
    r"|processing|code|api)\b"
)
_EN_REFERS_ELSEWHERE = re.compile(r"\b(?:this|that|these|those|above|previous|document|file|pdf|report|contract)\b")
_EN_TRAILING = re.compile(r"[\s?!.,]+$")
# "what pictures do we have", "which images are there": the question word order, no subject.
_EN_TAIL = re.compile(r"^\s*(?:do (?:we|you) have|have we got|are there|are available|are in (?:the )?(?:kb|knowledge base|library)|exist)\s*$")


def _classify_en(text: str) -> PictureIntent | None:
    core = _EN_TRAILING.sub("", text.lower())
    if not core or _EN_NEGATIVE.search(core):
        return None
    m = _EN_RE.match(core)
    if m is None:
        return None
    if any(tok not in _EN_MID_OK for tok in m.group("mid").replace("'", " ").split()):
        return None
    noun = re.sub(r"s$", "", m.group("noun"))
    rest = m.group("rest").strip()
    sub = ""
    if rest and not _EN_TAIL.match(rest):
        rm = _EN_REST.match(rest)
        if rm is None:
            return None  # "image processing pipeline": the noun is part of something else
        sub = rm.group("sub").strip()
        if _EN_REFERS_ELSEWHERE.search(sub):
            return None
    if sub and len(sub) > MAX_SUBJECT_CHARS:
        return None
    if noun in _EN_SPECIFIC:
        return PictureIntent("search", f"{noun} of {sub}" if sub else noun)
    if sub:
        return PictureIntent("search", sub)
    return PictureIntent("list", "")


# --------------------------------------------------------------------------- entry

_CJK = re.compile(r"[\u4e00-\u9fff]")


def classify(text: str | None) -> PictureIntent | None:
    """``None`` for an ordinary question, else what the person wants to see."""

    value = " ".join((text or "").split())
    if not value or len(value) > MAX_MESSAGE_CHARS:
        return None
    return _classify_zh(value) if _CJK.search(value) else _classify_en(value)
