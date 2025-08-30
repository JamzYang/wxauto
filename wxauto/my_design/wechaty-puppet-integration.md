# Wechaty Puppet Provider 集成方案（基于 wxauto）

## 1. 背景与目标
- 目标：将本地 Windows 微信 UI 自动化项目（`wxauto`）包装为 Wechaty 的 Puppet Provider，使后端以 Wechaty 统一接口接入微信的消息收发。
- 价值：统一生态与协议，解耦底层客户端差异；你的 Cloud Brain 后端只需对接 Wechaty，即可切换不同实现。

## 2. 现状概述（当前项目）
- 核心模块
  - `wxauto/wx.py`：`WeChat`（主窗口）、`Chat`（子窗口）与 `Listener` 监听框架。
  - `wxauto/ui/main.py`：`WeChatMainWnd`、`WeChatSubWnd`，封装窗口定位与会话切换。
  - `wxauto/ui/chatbox.py`：`ChatBox`，封装编辑框/发送按钮/消息列表等控件。
- 已有能力
  - 文本消息发送：`Chat.SendMsg()`（支持 `at` 参数，失败可回退为纯文本）。
  - 会话切换：`WeChat.ChatWith()`、`WeChatMainWnd.switch_chat()`。
  - 新消息监听：`Listener` 线程 + `_get_listen_messages()` / `GetNextNewMessage()`。
  - 登录态：主窗口存在与 `WeChatMainWnd.nickname`。

## 3. 你的 RPA 设计的可取之处（可增量引入）
参考 `wxauto/my_design/architecture.md`：
- 驱动抽象：`IAutomationDriver` + UIA/OCR/Mock，多层兜底与可测试。
- 策略与护栏：速率/静默、人机并发保护、只读降级、审计/黑匣子。
- 可测试性：MockDriver + 单测/离线集成测试。
- 云端协同：注册/心跳/消息上报、trace 一致性。

## 4. 改造成 Wechaty Puppet Provider 的可行性
- 结论：可行，建议以“文本消息收发 + 基础事件”的 MVP 起步，逐步扩展媒体与管理能力。
- 适配方式
  - 推荐：实现 Wechaty gRPC Puppet Service（Python 服务端），Wechaty 通过 gRPC 连接。
  - 备选：Node.js 最薄层转发到 Python（增加一个进程，但与生态更顺滑）。

## 5. 部署与系统交互
- 组件
  - Puppet Provider（本项目改造版，Python/gRPC）：在 Windows 前台驱动 WeChat 客户端。
  - Wechaty（Node.js/TS）：连接 Puppet gRPC，统一事件/命令接口。
  - Cloud Brain（你的后端）：通过 Wechaty SDK 或 Webhook 对接业务逻辑。

### 方案A：同机轻量部署（开发/单机）
- 进程（同一台 Windows 桌面机）：
  - WeChat 客户端（前台登录）
  - Python：Puppet gRPC Service（例如 127.0.0.1:8788）
  - Node.js：Wechaty Bot（连接本地 gRPC）
- 数据流：
  - 接收：WeChat → Python Puppet → Wechaty → Cloud Brain
  - 发送：Cloud Brain → Wechaty → Python Puppet → WeChat（UIA 执行）

### 方案B：分布式部署（生产/多机）
- 主机A（Windows/云桌面）：WeChat + Python Puppet（对外 gRPC）
- 主机B（Linux/任意）：Wechaty Bot + Cloud Brain
- 注意网络、TLS/mTLS 与鉴权。

## 6. 事件与命令映射（MVP）
- 事件（Puppet → Wechaty → Cloud Brain）
  - `login`：检测到已登录后一次性发出
  - `ready`：初始化完成
  - `message`：文本消息（含 room/contact 基础标识）
  - `heartbeat`：健康信号
- 命令（Cloud Brain → Wechaty → Puppet）
  - `messageSendText(room/contact, text, mentions[])` → `Chat.SendMsg(msg, at=[])`
  - `messagePayload(messageId)` → 基于本地缓存/解析的标准化消息体
  - 媒体类（image/file/voice）：初期返回 Unimplemented（计划增量）
- 标识与建模
  - `messageId`：建议基于（会话ID + 时间戳 + 递增序号）可逆/可去重的编码
  - `room/contact`：最小可用模型，按需懒加载与缓存（`SessionBox`/UI 抓取）

## 7. MVP 范围与里程碑
- MVP（1-2 周）
  - gRPC Puppet Service：`login/ready/heartbeat/message` 事件；`messageSendText`、`messagePayload(text)` 命令
  - 稳定化文本收发与群聊@（失败回退纯文本）
  - 轻量策略与审计：基本日志、最小限流/静默、人机并发保护（前台聚焦只读）
- 后续
  - OCR 兜底与选择器增强；媒体类消息收发；联系人/群成员模型完善；云端注册/心跳与 trace 透传

## 8. 风险与边界
- UI 变动敏感：WeChat 客户端升级影响选择器；需维护经验库与 OCR 兜底。
- 前台依赖：需要有效桌面会话，注意睡眠/锁屏；无头部署难度高。
- 覆盖范围：先文本路径，媒体与管理能力逐步补齐。
- 风控与稳定性：限流/静默、只读降级、错误重试与审计必需。

## 9. 增量演进建议（结合现有代码）
- P0：引入 `IAutomationDriver` 抽象（UIA 实现 + Mock 实现），将 `WeChatMainWnd/ChatBox` 调用改经 Driver。
- P0：接入轻量 `policy_manager` 与 `observability`（发送/监听打点与限流、错误上下文）。
- P1：定义 `OcrDriver` 接口与 Hook（占位），失败时记录兜底触点；后续接入 OCR 依赖。
- P1：轻量状态机（Init/Login/Idle/ErrorRecovery），管理监听启停与只读降级。
- P2：对齐云端注册/心跳/上报接口与 trace；提供“离线模式”开关。
- P2：MockDriver + `tests/test_logic.py`，实现离线集成回归。

## 10. 配置与运维要点
- 端口：gRPC 监听端口（如 8788），内网/本机；生产建议 TLS/mTLS。
- 策略：每群最小间隔、全局上限、夜间静默；截图/OCR 频控阈值。
- 日志与审计：最少化+脱敏；失败上下文含控件属性/截图/调用序列。
- 健康：gRPC health/heartbeat；Wechaty 重连；黑匣子与 trace（可选）。

## 11. 下一步行动清单
- [ ] 在本仓库新增 `puppet_service/`（Python）骨架：proto/服务/事件桥接/命令适配
- [ ] 最小事件流：`login/ready/message/heartbeat`
- [ ] 最小命令：`messageSendText`、`messagePayload(text)`
- [ ] 将 `wxauto/wx.py` 的 `Listener` 与 `Chat.SendMsg()` 接入服务
- [ ] Wechaty Bot（Node.js）连接本地 gRPC，打通“收→回”闭环
- [ ] 接入轻量策略与审计；添加 MockDriver 与离线集成测试
