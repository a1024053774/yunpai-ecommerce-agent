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

## 追加（2026-08-25 第三轮 WP5 复核 P0/P1 修复）

胡磊第三轮复核（head `4313557`）提出 P0/P1，本轮修复 8 项，均按“修复前截图 → 修复 →
修复后截图”留证：

1. **P0-1 历史审计 final 金额读侧脱敏**：`admin.py` 审计读取时对 `category` 为财务最终
   层的 `detail.amount` 按 `finance:final_profit:read` 权限置 None；新增有/无权限两类
   测试。截图 `m10-p01-before/after-20260825.png`（红/绿）。
2. **P0-2 信号迟到修订按当时可见值重建**：`signal_adapter.py` 每日取最早可见修订
   （`data_as_of ASC`）构造相邻因子，迟到修订不再回改历史窗口。复现 200/100=2.0 vs
   bug 0.2。截图 `m10-p02-before/after-20260825.png`。
3. **P1-1 正式利润未知粒度必须 blocked**：入账校验要求 formal 条目必填 `granularity`
   （`formal_granularity_required`）；投影对历史/直插的未声明条目兜底抛
   `granularity_undeclared`。截图 `m10-p11-before/after-20260825.png`。
4. **P1-2 模型建议数值声称必须绑定合法事实**：`basis` 中带两位小数的金额类数字必须与
   事实金额一致，否则 `basis_amount_not_bound` → `model_output_invalid`。截图
   `m10-p12-before/after-20260825.png`。
5. **P1-3 供货政策未来时间通过**：`created_at` 补上界 `<= now`（原只查下界）。截图
   `m10-p13-before/after-20260825.png`。
6. **P1-4 data_hash 未含信号门禁结果**：`data_hash` 加入 `signal_gate_result.to_evidence()`
   （freshness 重算用存储的 signal_champion_reason+candidates 还原）。截图
   `m10-p14-before/after-20260825.png`。
7. **P1-5 旧 UUID 游标兼容**：`paginated_messages` 对旧 `created_at|message_uuid` 游标
   兼容迁移到 rowid，不再静默重复第一页。截图 `m10-p15-before/after-20260825.png`。
8. **P1-6 决策台跨店请求竞态**：`m10Seq` generation token + 渲染前 scope 复核，
   A 慢响应不再覆盖 B。截图 `m10-p16-before/after-20260825.png`（修复前 A 覆盖 B 为
   “-”，修复后 B=600 保留）。

另：`scripts/verify_m10r.py` 补入漏收的 `tests/test_signal_adapter.py` 与
`tests/test_decision_advisor.py`（17 个测试进发布门禁）。

### 验证

- 受影响定向：104 passed（利润/订购/信号/建议/chat 会话）。
- 全量回归：见本文件「总验证」更新（新增测试后全量计数随提交更新）。
