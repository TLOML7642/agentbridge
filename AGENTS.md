# AgentBridge collaboration rules

- Prefer concise Chinese for Chinese requests. Read README.md and only the files relevant to the task.
- Before functional experimentation, verify the requested GitHub baseline backup exists. Do not equate a local commit or bundle with a successful remote push.
- Every progress checkpoint must update CHANGELOG.md with changes, actual verification results, known gaps, and deployment status. Use descriptive commit subjects and include test results in commit bodies.
- After checkpoint verification, commit and push to the configured GitHub repository. Verify the remote commit. If authentication or connectivity blocks a push, report it and preserve local work; do not claim it uploaded.
- Push does not authorize deployment. Preserve existing production approval gates, rollback requirements, and private consistent backups. Never force-push, rewrite published history, or overwrite live SQLite with old data.
- Keep the explicit source allowlist in .gitignore. Review each newly allowed path. Never commit credentials, databases, chats, runtime configuration, raw logs, or private backups. Audit history as well as the working tree before public upload.
- Run tests in isolated temporary source/runtime directories with synthetic backends. Never use production tasks, credentials, databases, or real destructive operations for fault injection. Keep failures and do not weaken assertions to obtain a pass.
- Preserve the existing WeChat account, DSH installation, permission boundaries, network architecture, and unrelated changes. Do not change model routing, persona, SSH policy, VPN, proxies, or passwords without scoped authorization.
- State the distinction between acceptance, process completion, verified user-goal completion, notification API return, and phone receipt.
- Do not spawn sub-agents unless the user explicitly requests delegation.
