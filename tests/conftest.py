import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from svg_embroidery.document import parse_svg  # noqa: E402


def svg(body: str, attrs: str = 'width="12cm" height="12cm" viewBox="0 0 120 120"') -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" {attrs}>{body}</svg>'


@pytest.fixture
def make_doc():
    def _make(body: str, attrs: str = 'width="12cm" height="12cm" viewBox="0 0 120 120"', path=None):
        return parse_svg(svg(body, attrs), path=path)

    return _make


@pytest.fixture
def examples_dir() -> Path:
    return ROOT / "examples"
