"""Marcacao por ambiente: UI precisa de node/playwright (host);
core/api rodam em qualquer lugar (container incluso)."""
import shutil

import pytest

pytestmark_ui = pytest.mark.skipif(
    not shutil.which("node"),
    reason="UI exige node (rode no host)")
