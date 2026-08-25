# M10-R Bug 修复记录（2026-08-25）

> 记录人：缪海南；证据编号沿用 E-20260825-001（追加）。
> 本次共修复 3 个 bug：模型建议空内容、决策台建议残留、交期证据空白来源绕过。

## BUG-1：模型建议接口真实模型返回空内容

- **现象**：`POST /v1/decision/suggestions` 用真实 DeepSeek 模型调用时返回
  `model_error`，网关报 `model returned empty content`；旧契约（嵌套对象数组）
  模型无法稳定生成。
- **根因**：输出 JSON 契约过复杂（`amount_refs` 为 `{field,value}` 对象数组），
  模型生成空内容。
- **修复方式**：`decision_advisor.py` 把 `amount_refs` 改为 `"字段点路径=数值"`
  的字符串数组（如 `profit.sales.amount=500.00`），服务端 `partition("=")` 解析后
  与事实精确比对；简化 SYSTEM_PROMPT 后真实模型稳定返回 3 条建议。
- **验证**：`tests/test_decision_advisor.py` 10 passed（含引用不在 catalog、
  数值不匹配、格式非法反例）；真实模型 smoke 返回 3 条建议且引用全部合法。
- **修复前截图**：`screenshots/m10-bug1-before-20260825.png`（模型建议不可用 model_error）。
- **修复后截图**：`screenshots/m10-fix-suggestions-evidence-20260825.png`（3 条建议、含“证据：”引用行）。

## BUG-2：决策台切换店铺/期间后旧经营建议残留

- **现象**：在店铺 A 生成经营建议后，切到店铺 B 并刷新决策台，建议面板仍显示
  店铺 A 的建议（用注入标记复现确认残留）。
- **根因**：`loadM10Decision` 只在建议面板为空时才重置提示，刷新只刷事实不清建议。
- **修复方式**：`admin-console.html` 改为每次刷新决策台都无条件把建议面板重置为
  “点击‘生成经营建议’…”提示；切换店铺/期间不再串显示旧建议。
- **验证**：Playwright 注入标记后切换店铺 → 标记消失、面板回到提示。
- **修复前截图**：`screenshots/m10-bug2-before-20260825.png`（切换店铺后旧建议残留）。
- **修复后截图**：`screenshots/m10-fix-suggestions-reset-20260825.png`（切换店铺后已重置为提示）。

## BUG-3：交期证据 source_reference 纯空白可绕过

- **现象**：供货/交期 Gate 只判断 `source_reference != ''`，而 `"   " != ""` 为真，
  纯空白的来源引用能通过交期证据检查。
- **根因**：`ordering/gate.py` 未对来源引用做非空白校验。
- **修复方式**：改为 `length(trim(source_reference)) > 0`；新增 red-first 反例
  `test_formal_gate_blocks_blank_source_transport_evidence`（空白来源 → Gate 阻断）。
- **验证**：`tests/test_purchase_order.py` 22 passed；全量回归
  **1130 passed / 0 failed**（29:46），compileall / git diff --check 通过。
- **修复前截图**：`screenshots/m10-bug3-before-20260825.png`（反例测试红：空白来源未阻断）。
- **修复后截图**：`screenshots/m10-bug3-after-20260825.png`（反例测试绿：空白来源被阻断）。

## 总验证

- 定向：101 passed（修复后相关文件 22+10 passed）。
- 全量回归：1130 passed / 0 failed，24 warnings（既有 traffic_lab 告警）。
- 分支：`feature/m10r-wp4-profit-ledger`（PR #24），本次修复已推送。
