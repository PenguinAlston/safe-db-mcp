# 贡献指南

在修改安全敏感代码前，请先阅读 [AGENTS.md](AGENTS.md) 和 [SECURITY.md](SECURITY.md)。

使用 \`pip install -e ".[dev]"\` 安装开发依赖。提交 Pull Request 前，请运行 \`ruff check src tests\` 和 \`pytest -q\`。

针对报告的问题以及相关的拒绝、竞争条件和故障路径添加回归测试。单元测试不等同于真实数据库集成测试。请准确说明你实际测试过的数据库引擎和驱动版本。

绝不要提交凭证、数据库 URL、客户数据、生产 SQL 或敏感查询结果。不要记录 SQL、绑定参数值、URL、密钥或驱动异常字符串。

涉及 SQL 解析、数据库适配器、Worker 任务认领、审核员审批、连接池或结果处理的变更，必须在 Pull Request 中说明安全影响；并根据需要更新 \`README.md\` 和/或 \`SECURITY.md\`。
