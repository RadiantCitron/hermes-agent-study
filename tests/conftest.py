"""tests 전체 공용 설정: 파일명이 `_pty.py`로 끝나는 시험에 `pty` 마커를 자동으로 붙인다."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if item.path.name.endswith("_pty.py"):
            item.add_marker(pytest.mark.pty)
