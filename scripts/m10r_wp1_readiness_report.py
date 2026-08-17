"""M10-R WP1 准备度报告脚本。

用法：
    python scripts/m10r_wp1_readiness_report.py \
        --tenant-id <tenant> --store-id <store> \
        --db data/agent.sqlite3 \
        --json-out readiness-report.json --md-out readiness-report.md

输出一份 JSON（结构化）与一份 Markdown（人读）准备度报告，供 WP5 验收复跑。
候选信号类输入统一标注“未使用（WP2 接线）”。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ecommerce_agent.database import Database
from ecommerce_agent.forecasting.readiness import (
    ReadinessCategory,
    SignalReadinessService,
)


CATEGORY_LABELS = {
    ReadinessCategory.FORECAST_TARGET.value: "预测目标",
    ReadinessCategory.CANDIDATE_SIGNAL.value: "候选信号",
    ReadinessCategory.SUPPLY_CONSTRAINT.value: "供给约束",
    ReadinessCategory.DELIVERY_CONSTRAINT.value: "交付约束",
    ReadinessCategory.MASTER_DATA.value: "执行主数据",
}


def _markdown(items: list, summary: dict) -> str:
    lines = ["# M10-R WP1 准备度报告", ""]
    lines.append("## 汇总")
    lines.append("")
    for category, label in CATEGORY_LABELS.items():
        counts = summary.get(category, {})
        rendered = "、".join(f"{state} {count}" for state, count in sorted(counts.items()))
        lines.append(f"- {label}：{rendered or '无'}")
    lines.append("")
    lines.append("## 输入明细")
    lines.append("")
    current_category: str | None = None
    for item in items:
        if item.category.value != current_category:
            current_category = item.category.value
            lines.append(f"### {CATEGORY_LABELS[current_category]}")
            lines.append("")
        note = "未使用（WP2 接线）" if item.category is ReadinessCategory.CANDIDATE_SIGNAL else ""
        fields = [
            item.label,
            f"证据={item.evidence_state.value}",
            f"来源={item.source_kind.value if item.source_kind else '-'}",
            f"data_as_of={item.data_as_of or '-'}",
            f"粒度={item.granularity.value if item.granularity else '-'}",
            f"SKU覆盖={item.sku_coverage if item.sku_coverage is not None else '-'}",
        ]
        if item.missing_reason:
            fields.append(f"缺失原因={item.missing_reason}")
        if note:
            fields.append(note)
        lines.append("- " + "；".join(fields))
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="M10-R WP1 readiness report")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--store-id", required=True)
    parser.add_argument("--db", default="data/agent.sqlite3")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--md-out", default=None)
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(db_path)
    if not db_path.exists() or db_path.stat().st_size == 0:
        db.initialize()

    service = SignalReadinessService(db)
    items = service.project(tenant_id=args.tenant_id, store_id=args.store_id)
    summary = service.summary(tenant_id=args.tenant_id, store_id=args.store_id)

    payload = {
        "tenant_id": args.tenant_id,
        "store_id": args.store_id,
        "summary": summary,
        "inputs": [
            {
                "input_key": item.input_key,
                "field_key": item.field_key,
                "category": item.category.value,
                "label": item.label,
                "evidence_state": item.evidence_state.value,
                "source_kind": item.source_kind.value if item.source_kind else None,
                "data_as_of": item.data_as_of,
                "granularity": item.granularity.value if item.granularity else None,
                "sku_coverage": item.sku_coverage,
                "missing_reason": item.missing_reason,
                "source_reference": item.source_reference,
            }
            for item in items
        ],
    }

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if args.md_out:
        Path(args.md_out).write_text(
            _markdown(items, summary), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
