"""M10-R WP1-WP4 验收复跑入口（可移植、无服务依赖）。

覆盖五类准备度 / 信号门禁 / 预测产品化 / 订购单 / 利润 ledger /
写操作审计的定向测试与 compileall。全量回归请另跑 `python -m pytest -q`。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGETED_TESTS = [
    "tests/test_readiness.py",
    "tests/test_signal_adapter.py",
    "tests/test_signal_gate.py",
    "tests/test_forecasting_run_service.py",
    "tests/test_product.py",
    "tests/test_purchase_order.py",
    "tests/test_profit.py",
    "tests/test_ordering_profit_audit.py",
    "tests/test_decision_advisor.py",
]


def main() -> int:
    subprocess.run(
        [sys.executable, "-m", "compileall", "-q", str(ROOT / "src")],
        check=True,
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *TARGETED_TESTS],
        cwd=ROOT,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
