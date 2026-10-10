"""`tools.test_support.texts` の自由記述サンプルが実際の形を持つことを固定する。

C-H1 の教訓: 識別子向けの検査を改行・タブを含む自由記述に適用すると、
受け付けるべき入力を拒否する欠陥になる。サンプルは識別子として解釈
できない文字列でなければならない。
"""

from tools.test_support.texts import FINDING_CONTENT, FINDING_REASON


def test_finding_content_is_free_form_text() -> None:
    assert "\n" in FINDING_CONTENT
    assert "\t" in FINDING_CONTENT
    assert "\r\n" in FINDING_CONTENT
    assert any(ord(ch) > 127 for ch in FINDING_CONTENT)
    assert not FINDING_CONTENT.isidentifier()


def test_finding_reason_is_free_form_and_distinct() -> None:
    assert "\n" in FINDING_REASON
    assert "\t" in FINDING_REASON
    assert "\r\n" in FINDING_REASON
    assert any(ord(ch) > 127 for ch in FINDING_REASON)
    assert not FINDING_REASON.isidentifier()
    assert FINDING_REASON != FINDING_CONTENT
