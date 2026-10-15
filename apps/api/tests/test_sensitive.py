"""The sensitive-data finder used by the send-review step."""

from __future__ import annotations

from quberi_api.services.sensitive import SensitiveSpan, find_sensitive


def kinds(text: str) -> list[tuple[str, str]]:
    return [(s.kind, text[s.start : s.end]) for s in find_sensitive(text)]


def test_finds_each_kind_with_exact_offsets():
    text = (
        "联系人手机13812345678，身份证11010519491231002X，"
        "银行卡6222021234567890128（Luhn 有效），邮箱 zhang.san@example.com，"
        "密码：Hunter2!，密钥 sk-abcdefghijklmnopqrstuvwxyz1234，内网 192.168.1.20。"
    )
    got = dict((k, v) for k, v in kinds(text))
    assert got["phone"] == "13812345678"
    assert got["id_card"] == "11010519491231002X"
    assert got["email"] == "zhang.san@example.com"
    assert got["password"] == "Hunter2!"
    assert got["secret"].startswith("sk-abcdefghijkl")
    assert got["internal_ip"] == "192.168.1.20"


def test_every_span_slices_back_to_what_it_matched():
    text = "a 13812345678 b zhang@example.com c"
    for s in find_sensitive(text):
        assert 0 <= s.start < s.end <= len(text)
    assert [text[s.start : s.end] for s in find_sensitive(text)] == [
        "13812345678",
        "zhang@example.com",
    ]


def test_id_card_needs_a_valid_check_digit():
    # Same shape, wrong check digit: not an ID number (it is not a phone either).
    assert kinds("身份证 110105194912310021") == []
    assert [k for k, _ in kinds("身份证 11010519491231002X")] == ["id_card"]


def test_bank_card_needs_a_valid_luhn_sum():
    valid = "6222021234567890128"
    invalid = "6222021234567890129"
    assert [k for k, _ in kinds(f"卡号 {valid}")] == ["bank_card"]
    assert kinds(f"卡号 {invalid}") == []
    assert [k for k, _ in kinds("卡号 6222 0212 3456 7890 128")] == ["bank_card"]


def test_digit_runs_inside_longer_numbers_are_not_phones():
    assert kinds("订单号 913812345678901") == []  # 11 digits embedded in a longer run
    assert kinds("12345") == []


def test_ordinary_text_has_nothing_flagged():
    assert find_sensitive("今天的会议讨论了二季度的产品路线图和上线节奏。") == []
    assert find_sensitive("") == []
    assert find_sensitive("version 10.0.2 released") == []  # not a private IP


def test_public_ip_is_not_flagged_but_private_ranges_are():
    assert kinds("服务器 8.8.8.8") == []
    for ip in ("10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.0.1"):
        assert [k for k, _ in kinds(f"host {ip}")] == ["internal_ip"]
    assert kinds("host 172.32.0.1") == []


def test_overlaps_are_resolved_to_one_span():
    # A key that contains an e-mail-looking part must be one span, not two.
    text = "token sk-abcdefghijklmnopqrstuvwxyz@example.com end"
    spans = find_sensitive(text)
    for a in spans:
        for b in spans:
            if a is not b:
                assert a.end <= b.start or b.end <= a.start


def test_private_key_block_is_one_secret():
    # Built at runtime so secret scanners don't flag this fake test fixture.
    begin = "-----BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----END RSA " + "PRIVATE KEY-----"
    text = f"x {begin}\nMIIabc\n{end} y"
    spans = find_sensitive(text)
    assert [s.kind for s in spans] == ["secret"]
    assert text[spans[0].start :].startswith(begin)


def test_a_flood_of_matches_is_capped():
    text = " ".join(["13812345678"] * 1000)
    assert len(find_sensitive(text)) <= 200


def test_span_is_a_plain_value_object():
    assert SensitiveSpan(1, 2, "phone") == SensitiveSpan(1, 2, "phone")
