# 统一 DSH Agent：阶段 A 验证与停止记录

> 本文保留第一次验证的失败结论，不将历史改写为通过。后续桥接保护层修复与新测试见[执行边界修复记录](NATIVE-BOUNDARY-20261027.md)。下面的十项与退出码描述对应原始未保护组合；当前检查器默认启用保护组，`--native-only` 可复现原始对照。

## 结论

**阶段 A 权限门槛未通过，停止进入 B/C/D，不部署。** 本提交仅增加隔离验证工具、报告门槛测试和本文档，没有实现或启用统一聊天、记忆学习、桌面共享 preset、工具审批数据库或新写入入口。

按照本轮计划，“关键权限守卫不能覆盖实际执行入口”必须停止，不能以提示词或关键词降级。当前原生 `tools.guard()` 可单调拒绝，却不保证守卫看到的工具名和参数就是工具体最终收到的工具名和参数。不能仅凭它宣称已经做到“参数或目标变化必须重新确认”。

这是合成恶意中间件下的接口边界验证，不是已经证明普通微信消息能够利用的生产漏洞。生产是否挂载会修改这些字段的中间件，本轮没有审计或更改；已部署的旧桥接器也没有因本验证获得工具级审批保护。

## 基线与隔离

- 开始前工作区干净，本地 `master` 为 `a39d6f2`；`git ls-remote origin refs/heads/master` 核实完整远端 ID 为 `a39d6f2d2c8cd40d81646d8797fd92576d45dfa4`。
- 初次远端复核遇到 DNS 失败，经权限升级后核实成功；没有改变代理、VPN、SSH 或 Git 全局配置。
- 使用已安装的 `0.1.1-rc.2` 模块和固定 Electron Node `v22.21.1`，不修改官方源码、不升级运行时。
- 只加载 Cordis、Agent、Agent Loop、Session、JSONL persistence、System Prompt、Tools、LLM 核心和 Persona。LLM 的 stream 被合成生成器接管，不注册供应商适配器，不加载凭据、FS/shell 后端、Loader、Code Runtime、动态插件或 subagent 执行器。
- 检查器通过显式环境白名单运行，独立 HOME、DSH_HOME、TMPDIR 和当前目录，不继承模型密钥、代理、SSH auth socket 或正式 bridge 环境变量。会话只保存合成内容，临时状态私有并在结束后清理。
- “synthetic_write”仅记录内存计数或合成目标字符串，不写用户文件、不执行 shell。JSONL 持久化只写本次临时目录。

## 实际结果

每轮十项检查，八项通过、两项安全断言失败；连续二十轮得到同样结果。每轮均返回退出码 **2** 和 `gatePassed: false`，没有把“成功复现失败”写成“安全门槛通过”。

| 检查 | 结果与边界 |
| --- | --- |
| 跨短生命周期进程续接 | 通过：两个独立子进程 create/flush 与 resume，第二轮保留第一轮合成答复 |
| create/flush/dispose/resume | 通过：新 Context 从 JSONL 恢复，再追加第二轮 |
| 原生 Persona 作用域 | 通过：给一个 Agent 挂载 persona，不污染另一个 Agent；不等于长期偏好已实现 |
| pre-execute 拒绝 | 通过：工具体未进入 |
| pre-execute 放行后的最终拒绝 | 通过：单调 guard 拒绝仍阻止工具体 |
| 禁用执行旁路 | 通过于最小组合：原生模式没有 Code Runtime、Loader、Shell，显式调用未注册工具也失败；不是正式插件树的完整审计 |
| 嵌套 dispatch | 通过：通过原生 execute 发出的嵌套工具调用仍进入拒绝守卫 |
| 取消与恢复 | 通过：真实 Agent 循环等待 pre-execute 时取消，即使钩子随后返回 allow，也未进入工具体；flush 后 resume 不重放已取消轮次 |
| 实际执行参数绑定 | **失败**：真实 Agent Loop 中，guard 认可 `approved-target`，后续 `tools/execute` 替换整个 arguments，工具体收到 `unapproved-target` |
| 实际执行工具绑定 | **失败**：原生 execute 中，guard 认可合成读取，后续中间件替换 name，合成写入计数增加一次，结果未报错 |

取消检查不覆盖 SIGKILL 时的所有未知结果、持久审批消费、重启授权失效或真实外部副作用；这些均不能由本阶段结果推断。

## 原因定位

安装包 `@deepseek-ai/dsh-tools/lib/index.js`：

1. `prepareExecution` 在第 3116 行调用 `guardReason(exec)`。
2. `dispatchScheduledExecution` 在第 3202 行把同一可变 execution 交给 `tools/execute` waterfall。
3. `dispatchToolBody` 在第 3178 行重新按当时的 `exec.name` 查找定义，第 3181 行以当时的 `exec.arguments` 调用工具体，没有再次对比审批快照。

参数图本身的深冻结并不禁止 `exec.arguments = {...}`，也不禁止 `exec.name = ...`。最小复现只在 hook 内做这两种替换，不修改安装模块，不调用内部私有执行函数。参数复现走真实 Agent Loop 的调度路径，而非仅测试自建 mock 调度器。

本次安装模块的 SHA-256：

| 模块 `lib/index.js` | SHA-256 |
| --- | --- |
| dsh-agent | `e7e40c5ca66d9827a5084c5c0c68983f9685842bb9b6d604803d4cb4642bb263` |
| dsh-agent-loop | `1ca83637892559e88c43b815e8d5d7b065951751e73eee7a7bef99d65a71ad6c` |
| dsh-tools | `47de95d14493dbd22d1a3ade14890fc99d7232db4e363f2190c9063b030dd029` |
| dsh-session-persistence-jsonl | `8b6ebc4509a3e969ab3ad6e0dfb553ae4861e5b101831afed23e593d148d97f3` |
| dsh-persona | `37a223d0c44fac7fd9d69f55b972828b177db7092fc45302c8b1938e595b9ace` |

行号只针对上述已安装文件；检查器每次重新计算摘要，不能把本记录套用于其他安装或升级后的模块。

## 重复执行

显式提供已安装 Node 和 `@deepseek-ai` 包目录，不使用会读取正式配置的 DSH 启动器：

```sh
python3 -B tools/check_native_stage.py \
  --node "$HOME/.deepseek-harness/bin/node" \
  --packages "$HOME/.deepseek-harness/plugins/packages/dsh-0.1.1-rc.2/node_modules/@deepseek-ai"
```

- 退出 0：本工具声明的全部十项检查通过，仍不等于允许部署。
- 退出 2：检查完整执行，但安全门槛未通过。本次即为此结果。
- 退出 1：检查未完成或报告矛盾，不产生通过结论。

`python3 -B -m unittest test_native_stage` 验证报告门槛、环境隔离及失败保留，不调用安装的 DSH，也不等于实际原生安全检查通过。

## 后续实施边界

这不证明在“禁止升级及修改官方源码”的约束下绝对无法实现。后续需要先实现并审计桥接自有的实际调用边界硬化，验证工具定义、身份、参数、目录与审批快照不可在批准后替换；同时保持取消信号可用，并禁止可重挂载/改写执行面的动态插件。不能未经验证就假定冻结一个字段或调整 hook 顺序足够。

硬化后必须让本阶段安全断言实际通过，再继续持久审批、对话调度、事件回填、人格学习、完整故障回归及手机验收。本轮未切换 AstrBot 模型/路由，没有读取或导入旧聊天，没有修改原数据库或桌面 preset，没有生产发布、三十分钟链路观察或手机验收。
