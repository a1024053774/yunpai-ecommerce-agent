$env:PYTHONPATH = 'D:\yunpai\yunpai-complete-20260816\.worktrees\m10r-wp4-profit-ledger\src'
$env:DATA_DIR = 'D:\yunpai\yunpai-complete-20260816\.worktrees\m10r-wp4-profit-ledger\data'
$env:ADMIN_AUTH_REQUIRED = 'false'
$env:MODEL_ENABLED = 'false'
$env:FINAL_PROFIT_READ_ADMIN_IDS = 'local-admin'
& 'C:\Users\admin123\AppData\Local\yunpai-venv\Scripts\python.exe' -m ecommerce_agent.cli serve --host 127.0.0.1 --port 8081
