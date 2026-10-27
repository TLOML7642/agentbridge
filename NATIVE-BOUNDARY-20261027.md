# 阶段 A 执行边界修复：桥接自有保护层

## 当前结论

`native_boundary.mjs` 为经过审核的工具增加执行保护层，不修改官方 DSH、不升级运行时。原先失败的“守卫后替换参数/工具名”检查在保护层启用时通过；原生对照保留两项失败，不删除或放宽旧断言。

这是**执行层代码与合成验证完成**，不是完整统一 Agent 已完成或已上线。worker、shim、AstrBot 和原 `/mac` 运行路径没有接入本模块，不因新增文件就获得新保护。聊天路由、人格记忆、持久审批数据库和生产灰度仍未实现或启用。

历史失败与安装模块摘要见[阶段 A 原始停止记录](NATIVE-STAGE-A-20261027.md)。

## 修复机制

- 宿主在原生 Agent setup 中绑定真实 Agent 对象及可信来源：发送者、入口、conversation、DSH session、项目、规范化工作目录与执行尝试。不能由模型传入或覆盖这些字段，同一对象不允许重新绑定。
- 原生 `tools/pre-execute` 创建私有执行记录，固化工具定义、JSON 参数、调用关联、进程 epoch、执行 nonce 与来源摘要。下游钩子的 deny/ask 不会被保护层改成 allow；即使其他钩子丢弃保护层的 deny，最终 guard 也要求私有记录已获准。
- 原生 `tools.guard` 在单调拒绝之外，把工具名、参数引用、Agent、call/root/parent 与 token 固定为不可替换字段。整个参数图单独复制冻结，不依赖提示词或关键词分类。
- 信号不能整对象冻结，因为 DSH 会替换/恢复信号。使用受控访问器，始终把原始取消和保护层撤销信号合并进去；中间件不能通过新信号丢弃取消意图。
- 只注册经过保护的冻结工具定义。`seal()` 后任何工具注册、移除、作用域覆盖或工具呈现变化都会撤销该进程执行权限，阻止“检查的是旧定义，调用的是新定义”。原生 dispatch 在进入替代工具体前看到撤销信号。
- 真正调用工具体前再次校验来源对象、会话、目录真实路径与目录 inode、实际工具定义、参数、取消、有效期及授权状态。工具体只接收冻结参数和最小可信上下文，不接收可改写的原生 execution/Agent。
- 写入默认关闭。开启后仍必须有宿主 `requestApproval` 和 `consumeApproval`；缺失、拒绝、异常、过期、摘要不符或旧 epoch 都拒绝。五分钟是上限，并同时用墙钟和单调时钟校验。
- 每个审批 ID、调用 ID 与执行实例最多使用一次。写入先等待宿主原子持久化消费，再作最后检查、调用工具体。消费后取消/过期不执行，但授权已花费，不自动再试。取消等待中的审批不必等宿主回调返回，迟来的 allow 被丢弃。
- 禁止 Code Runtime、Loader、动态 Cordis 执行器及原生 subagent 服务进入该组合；拒绝 Code Mode、动态插件、自主 Agent/Workflow 工具注册。常见 shell 工具不能标记成免确认读取，任意命令不因“看起来只读”就放行。

## 宿主接入契约

顺序固定为：

```text
建立最小原生 Context
  -> 安装 installToolBoundary（早于扩展 hook）
  -> 注册审核过的工具及 read/write 效果
  -> 在 agents.create/resume 的 setup 中 bindAgent
  -> seal
  -> 发送当前轮消息
  -> 等待结束并 flush
```

`register` 接收原生编译后的工具定义，但不会借用任意插件的 finalizer、presenter 或并发分类器。被审核的 execute 应接受 `(args, {cwd, subject, callId, signal})`；不能直接把依赖原生 Agent/可变 execution 的现有插件树全部套进去。参数校验和工具体自身必须无隐蔽旁路，read 标记由宿主审核，不由模型决定。

`requestApproval` 收到冻结的请求，返回绑定 `digest`、`epoch`、`approvalId`、`expiresAt` 的收据。`consumeApproval` 必须由后续宿主实现数据库原子校验和消费，并且仅在持久化成功后返回严格的 `true`。本模块不会把一个普通返回值伪装成数据库持久性保证。

测试的审批宿主是合成内存 stub，仅用于验证执行保护。**目前没有微信确认 API 与审批数据库接入，也未证明真实重启后的持久消费、未知外部副作用恢复或通知链路。** 进程内授权不导出；新实例生成新 epoch，不能恢复旧收据。

## 验证命令与结果含义

```sh
# 启用桥接保护，执行当前阶段完整检查
python3 -B tools/check_native_stage.py \
  --node "$HOME/.deepseek-harness/bin/node" \
  --packages "$HOME/.deepseek-harness/plugins/packages/dsh-0.1.1-rc.2/node_modules/@deepseek-ai"

# 保留原生对照，应继续显示两项失败并退出 2
python3 -B tools/check_native_stage.py --native-only \
  --node "$HOME/.deepseek-harness/bin/node" \
  --packages "$HOME/.deepseek-harness/plugins/packages/dsh-0.1.1-rc.2/node_modules/@deepseek-ai"

# 报告门槛单元测试，不调用真实 DSH 或模型
python3 -B -m unittest test_native_stage
```

保护组共 32 项检查，覆盖原生会话能力与保护层正向执行、替换/覆盖、跨对象身份、目录被替换、单次消费、取消竞态、嵌套调用、过期/断连/旧收据及执行旁路。报告额外包含保护层源码 SHA-256，拒绝将 `native-only` 结果当成保护层通过。仍使用固定 Electron Node v22.21.1、隔离 HOME/DSH_HOME、合成模型、计数工具及临时会话；不读取正式聊天或执行真实文件/命令任务。目录替换检查只重命名和新建本次临时测试目录，用于验证 inode 复检。

最终版本连续二十轮保护组全部 32/32 通过；二十轮未保护对照继续各有两项失败。报告门槛九项单元测试通过；独立临时源码及运行目录中的完整自动发现为 166 项通过，约 66.6 秒。它们分别证明合成执行边界和代码回归，不是微信端真实工作流验收。

## 不扩大结论

- 这不是在同一 Node 进程里隔离任意恶意 JavaScript 插件的沙箱。任意宿主代码本来就能直接访问系统，必须保留最小、经过审核的组合；本轮没有审计正式 DSH 的完整插件树。
- 审批协议与操作入口绑定不等于文件系统竞态已解决。实际文件工具还要处理目标解析、符号链接与文件句柄；真实 shell 仍需限制后台化/脱离监督、目录与环境等行为。
- 取消不撤销已经发生的副作用。原子消费之后进程崩溃的结果必须按未知处理，不得自动重放。
- 本轮不改变原 `/etc/` 任务分类；合成读取 `/etc/hosts` 只验证保护层不按路径关键词误要求审批，不代表正式文件查询已验收。
- 后续才接入持久审批、持续聊天调度、后台任务回填与人格学习，随后做完整新功能故障组、生产一致性备份、灰度、手机验收及三十分钟链路观察。源码推送不等于生产发布。
