"""Which chat messages count as a request to see pictures (pure, no database)."""

from __future__ import annotations

import pytest

from quberi_api.services.rag.picture_intent import PictureIntent, classify

LIST = PictureIntent("list", "")


def _search(subject: str, *, soft: bool = False) -> PictureIntent:
    return PictureIntent("search", subject, soft=soft)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # the two questions from the bug report
        ("库里有哪些图片", LIST),
        ("有风景画吗", _search("风景画")),
        # lists
        ("有什么图片", LIST),
        ("列出所有图片", LIST),
        ("图片有哪些", LIST),
        ("知识库里有多少张照片？", LIST),
        ("有没有图片", LIST),
        ("What pictures do we have?", LIST),
        ("list all images", LIST),
        ("Are there any photos?", LIST),
        # searches
        ("有没有海边的照片", _search("海边")),
        ("帮我找一下关于猫的图片", _search("猫")),
        ("请问知识库里有没有星空的照片？", _search("星空")),
        ("给我看看山水画", _search("山水画")),
        ("有海报吗", _search("海报")),
        ("show me photos of the beach", _search("the beach")),
        ("find pictures of dogs", _search("dogs")),
        ("any screenshots?", _search("screenshot")),
        ("Do we have any paintings of mountains?", _search("painting of mountains")),
        # a bare 图 after a subject: answered with pictures only when one really matches
        ("有风景图吗", _search("风景", soft=True)),
        ("有没有螺丝的图", _search("螺丝", soft=True)),
        ("请问有没有柱状图？", _search("柱状", soft=True)),
    ],
)
def test_requests_for_pictures_are_recognised(text, expected):
    assert classify(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # about pictures, not a request to see them
        "有没有图像识别的文档",
        "怎么压缩图片",
        "如何查看图片",
        "图片格式有哪些要求",
        "产品有图片",
        "我们有哪些图片需求",
        "上传图片失败怎么办",
        "帮我画一张风景画",
        "生成一张海报",
        # points at a particular document or an earlier turn
        "这份文档里有图片吗",
        "查一下合同里的图片",
        "刚才那张图片是什么",
        # no request shape
        "海报",
        "风景画",
        "图片",
        # English look-alikes
        "Do we have a docker image for redis?",
        "show me the image processing pipeline",
        "how do I resize an image",
        "is there an image upload api",
        # a bare 图 needs a subject and a request
        "有图吗",
        "有没有图",
        "怎么画流程图",
        "产品有流程图",
        "这份文档里有架构图吗",
        # not a short question
        "",
        "   ",
        "有没有图片" + "，请结合上下文详细说明" * 10,
    ],
)
def test_other_messages_are_left_to_the_text_path(text):
    assert classify(text) is None


def test_none_is_not_a_request():
    assert classify(None) is None


def test_a_subject_that_is_too_long_is_not_trusted():
    assert classify("有没有" + "很长的描述" * 8 + "的图片") is None
