"""回答のデコードで入力を黙って書き換えないための境界。"""

import pytest

from tools.agent_runner.adapters.strict_json import decode


def test_nested_escaped_duplicate_key_is_rejected() -> None:
    """ネストしたエスケープキーが同名と分からないと、指摘の属性が黙って上書きされる。"""
    with pytest.raises(ValueError):
        decode(r'{"findings": [{"id": "残す指摘", "\u0069d": "別の指摘"}]}')


def test_nonfinite_constant_is_rejected() -> None:
    """JSONでない数値を受け付けると、後続の判断が不正な値を含む回答を黙って採用する。"""
    with pytest.raises(ValueError):
        decode('{"findings": [], "count": NaN}')
