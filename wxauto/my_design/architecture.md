# 微信RPA（WeChat, Windows）— 架构设计（Python 版 MVP）

## 1. 设计目标
- 前期仅支持 Windows 微信（WeChat）客户端。
- 图像视觉为主, UIAutomation 为辅。
- 本地 Agent 只负责群消息采集与自动发言，不调度业务 Executor；云端负责决策/RAG/编排。
- 满足人机并发保护、审计与可观测、速率与静默、紧急停用等安全要求。

## 2. 系统上下文
- WeChat 客户端（被自动化对象）
- 本地 `wechat-rpa` Agent（Python）：消息读取、上报与接收下发、自动发言
- 云端 Cloud Brain：决策/RAG、策略与回复下发、编排独立 Executor
- 独立 Local Executor：执行业务任务（本地进程，由云端编排触发）

## 3. 系统分层架构
本系统采纳分层、解耦与混合智能模型的设计思想，确保逻辑清晰、高度可测试和易于扩展。

- **L1 - 界面驱动层 (Driver Layer)**: 负责所有与UI“看”和“动”相关的操作，彻底封装平台相关的细节。
  - **`IAutomationDriver` (接口)**: 定义一套标准的、与平台无关的UI操作契约（如 `find_element`, `click`, `get_text`），是实现依赖倒置的关键。
  - **`UIA Driver` (实现)**: `IAutomationDriver` 的主要实现，使用 `uiautomation` 库操作真实的微信界面。
  - **`OCR Driver` (实现)**: `IAutomationDriver` 的兜底实现，在 UIA 失败时执行小范围截图和OCR。
  - **`Mock Driver` (实现)**: `IAutomationDriver` 的模拟实现，用于离线单元测试和集成测试，不执行任何真实UI操作。

- **L2 - 业务逻辑层 (Business Logic Layer)**: 系统的“大脑”，负责决策“在什么时间、什么状态下，做什么事”。
  - **顶层状态机 (FSM)**: 管理应用的宏观生命周期，如 `Initializing`, `Login`, `Idle`, `ErrorRecovery` 等互斥状态。
  - **行为树 (Behavior Tree)**: 在特定FSM状态（尤其是`Idle`状态）下，用于定义和驱动具体的、模块化的行为逻辑（如“处理新消息”、“执行定时任务”等）。

- **L3 - 任务执行层 (Task Layer)**: 系统的原子操作单元，被行为树的“动作节点”调用。
  - **`Task` (基类)**: 定义所有任务的统一接口。
  - **具体任务 (Concrete Tasks)**: 封装单一、明确的操作，如 `ReadMessageTask`, `SendMessageTask`, `ReportToCloudTask`。它们通过调用 `IAutomationDriver` 和核心服务来完成工作。

- **L4 - 核心服务层 (Core Services Layer)**: 为业务逻辑和任务执行提供支持的横切关注点。
  - **Tray/指示器**: 托盘菜单、状态提示与紧急操作。
  - **Cloud Client**: 与云端 Brain 的所有 API 交互。
  - **State Store**: 基于 SQLite/JSON 的持久化存储。
  - **Policy/RateLimiter**: 速率、静默、白名单等策略控制。
  - **Observability (Audit/Blackbox/Logging)**: 审计、黑匣子、日志与追踪。
  - **Activity Detector**: 用户活动感知，用于触发人机并发保护。
  - **Conversation Monitor & Deduper**: 会话状态（偏移量）管理与消息去重。

## 4. 核心工作流
系统的运转由一个主 **Engine** 驱动，取代了原有的 `Scheduler` 概念。

1) **Engine 启动**: 初始化所有服务、驱动，并启动顶层状态机（FSM）。
2) **FSM 状态驱动**: Engine 在其主循环（心跳）中不断调用 FSM 的 `execute()` 方法。FSM 根据预设条件在 `Initializing`, `Login`, `Idle`, `ErrorRecovery` 等状态间转换。
3) **行为树执行**: 当 FSM 进入 `Idle` 状态时，它会激活并执行一个或多个行为树（BT）。
4) **BT 遍历与决策**: 行为树从根节点开始，根据其逻辑（如 `Sequence`, `Selector`）遍历子节点，决策当前应执行何种行为。
5. **Task 执行**: 当行为树遍历到“动作节点”时，会实例化并执行对应的 `Task`（如 `CheckUnreadTask`）。
6. **Driver/服务调用**: `Task` 在执行过程中，会调用 `IAutomationDriver` 接口来操作UI，或调用 `Cloud Client` 等核心服务来完成其功能。
7. **循环与响应**: Engine 的主循环不断重复，驱动 FSM 和行为树对外部环境（如新消息、用户操作）的变化做出实时响应。

- **活动感知决策**: `Activity Detector` 服务检测到用户活跃时，行为树中的“条件节点”会判断是否应暂停自动发言，从而实现动态的人机并发保护。

## 5. 选择器与回退
- 多特征：`ControlType + Name/Text + AutomationId + 层级路径 + IsOffscreen` 组合
- 未读检测：红点/数字/加粗等候选特征，配置化权重/灰度
- @ 三段式回退：
  1) **视觉候选选择**（对浮层进行区域OCR，匹配文本后点击）
  2) 失败回退“纯文本 @姓名”（可配置发前确认）
  3) 仍高风险时“无 @ 发送 + 明确提示人工处理”，并在审计中标注
- 时间窗口增量回退：`last_processed_ts - W` 开始，窗口 W + 冗余 N 条读取，本地合并去重后推进偏移

- 语音消息处理与幂等：优先通过 UIA 调用微信气泡菜单“转文字”，获取 `transcript` 并生成 `content_hash = hash(normalize(transcript))` 作为幂等键；若转写失败，则仅上报“语音类型事件（无转写）”，幂等键退回为（会话+发送者+标准化时间+时长）的元信息组合。

## 6. 技术栈（Python）
- **核心视觉库**: `opencv-python` (用于模板匹配、图像预处理、结构分析), `rapidocr-onnxruntime` (用于离线中文OCR), `mss` (用于高效截图)
- **辅助UI库**: `pywin32` (用于获取窗口句柄、状态), `pystray` (用于系统托盘), `keyboard` (可选, 用于活动感知)
- **降级UI自动化库**: `uiautomation`, `pywinauto` (不再作为主要驱动，仅用于极少数特殊场景或调试分析)
- **应用框架与数据**: `pydantic` + `yaml`/`toml` (用于配置和数据模型), `sqlite3` (用于本地持久化存储)
- **网络与异步**: `httpx` + `tenacity` (用于HTTP客户端与重试逻辑)
- **日志/可观测**: `loguru`, `opentelemetry-sdk` (可选, 用于全链路追踪)
- **打包**: `pyinstaller` (用于打包为单文件可执行程序)

## 7. 与云端接口（对齐草案）
- Agent 注册/心跳：`POST /api/v1/agents/register`，`POST /api/v1/agents/heartbeat`
  - `agent_type=wechat-rpa`，`capabilities=[message_ingest,message_send,mentions,whitelist_control,blackbox_audit]`
  - `channel=WeChat`，`modes=read_only|read_write`，`policy_versions`，`trace_id/host_meta`
- 消息上报：`POST /api/v1/messages/ingest`
  - `tenant_id, conversation_id, sender_id, sender_display, timestamp, raw_text, attachments[], trace_id, client_meta`
- 回复下发：`POST /api/v1/messages/replies`
  - `conversation_id, reply_text, mentions[], send_mode(auto|confirm), ttl, trace_id`
- 错误策略：超时/5xx 指数退避重试（限次），本地幂等键 + 云端去重

- trace 对齐：全链路统一 `trace_id`，本地在请求发起时生成并贯穿上报/回调/审计；如云端下发 `trace_id`，则优先生效并在本地继续透传。

## 8. 非功能与护栏
- 人机并发：用户活跃即只读，当前聚焦会话不自动发言；每轮仅处理少量会话；指数退避
- 速率/静默：每群最小间隔、全局上限、夜间静默
- 安全与合规：优先 UIA Pattern（Value/Invoke），少抢焦，坐标点击/粘贴仅兜底
- 可见性/中断：托盘指示，悬浮提示（可选），一键暂停/紧急停用
- 审计与黑匣子：前100字摘要、@目标、结果、trace_id；失败上下文（控件属性、截图、调用序列）

- 前台占用与还原：必要的前台操作需在 ≤500ms 内完成并立即还原至原焦点状态。
- 截图/OCR 频控：仅在 UIA 读取失败时小范围截图；设置单会话与全局的最小截图/识别间隔、最大截图分辨率/尺寸与低置信度阈值；低置信度走降级路径并清晰标注。
- 失败阈值与只读降级：维护滑动时间窗内的连续失败计数与异常率（如 10 分钟内 ≥5 次失败或异常率≥阈值）触发自动降级为“只读”，记录审计并提示操作者。
- 日志最小化与脱敏：仅记录必要摘要（默认前100字），对敏感字段做脱敏；黑匣子文件本地加密存储，上传需显式授权与频率控制。
- 夜间静默配置：支持基于本地时区的时段配置（如 22:00–08:00），可配置节假日白/黑名单。
- 机器人自激活防循环：检测短周期内的“机器人↔机器人”往返触发模式（基于来源/签名/相似度），触发短时静默冷却。
- 可见性提示：进入自动化步骤时显示悬浮提示或托盘高亮，步骤完成或被中断时立即清除，确保操作者可见性。
- 紧急停用作用域：紧急停用仅影响“自动发言/发送能力”（自动切换为只读），不影响 executor 的运行与云端编排控制链路。

## 9. 目录结构（建议）
```
apps/wechat-rpa/agent-python/
  rpa/
    main.py                # 程序入口，初始化并运行 Engine
    engine.py              # 包含 Engine、FSM 和行为树的核心驱动逻辑
    drivers/               # 界面驱动层
      __init__.py
      base_driver.py       # 定义 IAutomationDriver 抽象接口
      uia_driver.py        # UIA 实现
      ocr_driver.py        # OCR 兜底实现
      mock_driver.py       # 离线测试用的模拟实现
    tasks/                 # 任务执行层
      __init__.py
      base_task.py         # 定义 Task 基类
      read_messages.py     # 读取消息的 Task
      send_message.py      # 发送消息的 Task
      ...
    services/              # 核心服务层
      cloud_client.py      # 云端API客户端与DTO
      state_store.py       # SQLite/仓储、配置加载
      policy_manager.py    # 策略与限流
      observability.py     # 日志、追踪、审计、黑匣子
      tray_manager.py      # 托盘与状态指示、暂停/停用
    
  configs/                 # 白名单、速率、静默、元素描述符
  elements/                # 存放图像模板等元素定位信息
  assets/                  # 托盘图标等
  tests/
    test_tasks.py          # 任务的单元测试
    test_logic.py          # 使用 MockDriver 的离线逻辑集成测试
  requirements.txt
  README.md
```

## 10. 里程碑（对齐需求文档）
- M1（~1周）：UIA 可达性冒烟；白名单会话读取与去重；基础发言（有限 @）；注册/心跳/上报打通
- M2（~1周）：稳定 @；OCR 兜底；护栏与审计/黑匣子；全链路 trace；接口联调
- M3（稳态）：选择器鲁棒性增强；配置中心；可选服务化与自动更新

## 11. 待确认
- 目标 WeChat 客户端版本范围（选择器灰度）
- OCR 模型体积与分发策略
- 云端 Base URL 与鉴权（Token/mTLS）
- 白名单与 @ 名单来源与校验策略

）
- 白名单与 @ 名单来源与校验策略

