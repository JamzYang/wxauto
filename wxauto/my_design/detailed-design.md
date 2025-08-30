# 微信RPA（WeChat, Windows）— 详细设计（Python 版 MVP）

> 说明：本详细设计与 `apps/wechat-rpa/docs/architecture.md`、`requirements.md` 对齐，面向落地实现与测试。所有类与方法在实现阶段需补充中文注释，准确描述功能与职责。

## 0. 核心挑战与设计原则

本次自动化面临的核心技术挑战源于**微信PC客户端的自定义渲染机制**（其窗口内容由 `MMUIRenderSubWindowHW` 整体渲染）。该机制导致标准的UI自动化工具（如UIAutomation, a11y tools）无法探测或交互其内部的任何UI控件。

这一根本性挑战，迫使我们做出以下核心设计决策：

1.  **放弃UIA路线**：承认UIA在本项目中几乎完全无效，其作用被严格限定在获取顶层窗口句柄/状态等最外层操作的辅助角色上。
2.  **确立视觉驱动为核心**: 整个界面驱动层必须基于**视觉分析**（模板匹配、区域OCR、结构与颜色分析）来构建，这是一个比UIA更复杂但唯一可行的方案。
3.  **强调解耦与可测试性**: 为了应对视觉方案的“脆弱性”和复杂性，架构上必须通过依赖倒置（`IAutomationDriver`接口）和模拟驱动（`MockDriver`）将业务逻辑与UI操作彻底解耦，确保核心逻辑可以在无UI的环境下进行快速、可靠的自动化测试。

后续所有详细设计，均是围绕这一核心挑战和应对原则展开。

## 1. 总览
- 目标：在 Windows 上，以**视觉驱动为核心**，实现微信群消息“只读采集 + 自动发言（含 @）”，并接入云端决策/编排。
- 限制：仅处理白名单会话与白名单 @ 对象；严格人机并发保护；必须具备审计与黑匣子回溯能力；紧急停用仅影响自动发言。
- 路线：M1（PoC）→ M2（MVP）→ M3（稳态增强）。

## 2. 架构与模块划分
系统遵循分层架构，核心由 **Engine** 驱动，通过 **FSM** 管理宏观状态，在具体状态下由 **行为树 (BT)** 执行业务逻辑。

- **Engine**: 应用主循环，负责初始化所有模块，并按固定频率“tick”顶层状态机，驱动整个系统运转。
- **业务逻辑层 (FSM + BT)**
  - **顶层状态机 (FSM)**: 管理 `Initializing`, `Login`, `Idle`, `ErrorRecovery` 等状态。
  - **行为树 (BT)**: 在 `Idle` 状态下运行，通过 `Sequence`, `Selector`, `Condition`, `Action` 节点的组合，实现如“轮询处理未读消息”、“执行云端指令”等复杂逻辑。
- **任务执行层 (Tasks)**
  - 具体的原子操作单元，如 `CheckUnreadTask`, `ReadMessagesTask`, `SendMessageTask`。由行为树的 `Action` 节点调用。
- **界面驱动层 (Drivers)**
  - **`IAutomationDriver`**: 统一接口。
  - **`Vision Driver`**: 核心实现，基于视觉分析。
  - **`UIA Driver`**: 辅助实现，仅用于获取顶层窗口等。
  - **`Mock Driver`**: 用于离线测试的模拟实现。
- **核心服务 (Services)**
  - **Cloud Client**: 与云端 API 交互，并处理指令的生命周期。
  - **State Store**: 数据持久化。
  - **Policy/RateLimiter**: 策略与流控。
  - **Observability**: 日志、审计、黑匣子。
  - **Activity Detector**: 用户活动感知。
  - **Tray Manager**: 托盘图标与交互。

## 3. 核心工作流与逻辑模型

### 3.1 主工作流
1) **Engine** 启动，初始化所有服务和 `IAutomationDriver` 的具体实例（如 `Vision Driver`）。
2) **Engine** 启动顶层状态机（FSM），初始状态为 `Initializing`。
3) **FSM** 在 `Initializing` 状态下执行环境检查、冒烟测试等任务，成功后切换到 `Idle` 状态。
4) 进入 `Idle` 状态后，**Engine** 在每次 `tick` 时都会执行该状态下的主行为树。
5) **行为树** 从根节点开始遍历，根据当前环境（如是否有未读消息、是否收到云端指令）决策执行路径。
6. **Task 执行**: 行为树的动作节点会创建并执行具体的 `Task`。`Task` 内部调用 `IAutomationDriver` 和其他服务来完成工作。

### 3.2 行为树示例：处理未读消息
以下是一个简化的行为树，用于描述在 `Idle` 状态下如何处理一条新消息。

```
? (Selector: 主选择器)
|--> -> (Sequence: 处理微信群消息)
|    |--> (Condition: 有未读的白名单会话吗?)
|    |--> (Action: 读取新消息 -> ReadMessagesTask)
|    |--> (Action: 本地去重 -> DedupeTask)
|    |--> (Action: 上报云端 -> ReportToCloudTask)
|    |--> ? (Selector: 需要回复吗?)
|    |    |--> -> (Sequence: 自动回复)
|    |    |    |--> (Condition: 云端下发了回复指令?)
|    |    |    |--> (Condition: 人机并发检测通过?)
|    |    |    |--> (Condition: 速率和静默策略检查通过?)
|    |    |    |--> (Action: 执行发送 -> SendMessageTask)
|    |--> (Action: 更新会话处理偏移量 -> UpdateOffsetTask)
```

### 3.3 顶层状态机 (FSM)
- **Initializing**: 启动、环境检查、冒烟测试。成功 -> `Idle`，失败 -> `ErrorRecovery`。
- **Login**: （可选）处理需要人工介入的登录流程。
- **Idle**: 核心工作状态，循环执行行为树。
- **ErrorRecovery**: 发生严重错误时进入，尝试重启、报警，或在多次失败后进入永久挂起状态。

## 4. 视觉元素库 (elements.yaml)
为实现视觉逻辑与代码的解耦，并方便维护，所有视觉定位符（图片模板、区域坐标等）都必须在外部的 `elements.yaml` 文件中进行统一管理。

### 4.1 设计原则
- **版本化**: 元素库应支持版本号，以应对微信UI的迭代。
- **语义化命名**: 每个元素的key都应有清晰的业务含义（如`send_button`），而不是描述其外观。
- **多模态定义**: 支持多种类型的视觉元素定义。
- **可维护性**: 当微信UI更新时，理想情况下应只需更新此文件和相关的图片资源，无需改动代码。

### 4.2 文件结构示例
```yaml
version: "3.8.0"  # 对应微信客户端版本或内部版本

elements:
  # 全局元素
  global_wechat_logo:
    type: image_template
    path: "assets/elements/wechat_logo.png"
    threshold: 0.9
    description: "微信窗口左上角的Logo，用于确认窗口"

  # 会话列表区域的元素
  conversation_list_panel:
    type: roi
    area: [0, 60, 280, 800] # [x, y, width, height]
    description: "左侧会话列表的整体区域"

  unread_red_dot:
    type: image_template
    path: "assets/elements/unread_dot.png"
    threshold: 0.85
    description: "未读消息小红点"

  # 聊天窗口区域的元素
  chat_title_area:
    type: roi
    area: [281, 0, 500, 40]
    description: "聊天窗口顶部标题区域，用于识别'对方正在输入'"
  
  message_history_panel:
    type: roi
    area: [281, 41, 500, 650]
    description: "聊天记录区域"

  chat_input_box:
    type: roi
    area: [281, 690, 450, 80]
    description: "聊天输入框区域"

  send_button:
    type: image_template
    path: "assets/elements/send_button.png"
    threshold: 0.9
    description: "发送按钮"
  
  mention_popup_panel:
    type: color_block # 可通过颜色或轮廓检测其出现
    area: [281, 500, 200, 190] # 大致出现区域
    color_range: [[245, 245, 245], [255, 255, 255]] # 示例颜色范围
    description: "@提及候选人浮层"
```
### 4.3 匹配与使用流程
`VisionDriver` 在执行 `find_element("send_button")` 这类操作时，其内部流程为：
1.  加载并解析 `elements.yaml` 文件。
2.  根据key (`send_button`) 查找到其定义。
3.  根据 `type` (`image_template`) 选择相应的识别策略（模板匹配）。
4.  在 `find_element` 方法指定的区域（或全局）内执行识别算法（如 `cv2.matchTemplate`），找到元素在屏幕上的具体位置并返回。

## 5. 读取窗口与增量回退
- 正常：仅读取“未读区间最近 N 条”（N 默认 20，可配）。
- 异常：若未读标记不可靠或异常升高，则从 `last_processed_ts - W` 开始做“窗口 W + 冗余 N 条”的增量扫描；本地合并去重后推进 `last_processed_ts`。

## 6. @ 三段式回退
1) **视觉候选选择**：输入@后，检测到候选浮层出现。对浮层区域进行 **OCR**，获取所有候选人文本列表。在文本列表中匹配目标姓名，计算其相对坐标并点击。
2) 失败回退：直接发送“纯文本 @姓名”（可配置发前确认）。
3) 仍高风险：无 @ 发送并在审计标注原因，提示人工处理。

## 7. 语音消息处理
- 识别语音气泡与元信息（发送者、时间、时长、未读）。
- **视觉操作菜单“转文字”**：模拟右键点击语音气泡，检测出现的上下文菜单浮层。对浮层进行 **OCR**，在识别出的文本中定位“转文字”菜单项并点击，获取 `transcript`；生成幂等键 `content_hash = hash(normalize(transcript))`。
- 转写失败：仅上报“语音类型事件（无转写）”，幂等键退回为（会话+发送者+标准化时间+时长）。

## 8. 视觉识别核心流程
- **触发条件**: 所有需要与UI交互的操作均会触发。
- **核心技术**: 综合运用模板匹配（`opencv-python`）、区域OCR（`rapidocr-onnxruntime`）、结构与颜色分析。
- **截图**: 使用 `mss` 捕获目标控件的限定区域（ROI）或全屏截图。
- **频控与性能**:
  - 严格限定截图范围是提升性能的关键。
  - 对高频操作（如循环检测）的结果进行缓存。
  - 对OCR等耗时操作设置频次限制。

## 9. 活动感知与只读切换
- 探测：前台窗口是否为 WeChat、输入活跃度（按键/鼠标活动）、焦点频繁变更。
- 策略：命中任一条件 → 本轮只读（不发言），下一轮指数退避（2→4→8s）。
- 前台占用预算：发生必要前台操作时 ≤ 500ms，并立即还原至前态。

## 10. 策略/限流
- 白名单：仅白名单群进入轮询；@ 对象须在白名单成员内（精确匹配），非白名单阻断或发前确认（可配）。
- 速率：每群最小间隔（如 ≥ 5s）、全局上限（每分钟 ≤ X）。
- 静默：夜间静默（如 22:00–08:00，本地时区），节假日白/黑名单。
- 机器人自激活：检测短周期往返触发（文本相似度/签名），触发冷却静默（如 5–15 分钟）。

## 11. 错误处理与降级
- 重试：`tenacity` 指数退避（基于异常类型白名单）。
- 降级：滑动窗口内连续失败≥阈值（如 10 分钟 ≥5 次）或异常率≥阈值 → 自动只读；托盘提示与审计记录。
- 兜底：视觉识别失败后，应有明确的失败路径（如记录黑匣子、提示人工介入），而不是无限重试。

## 12. 数据模型（Pydantic）
- MessageIngest
  - tenant_id, conversation_id, sender_id, sender_display, timestamp, raw_text, attachments[], trace_id, client_meta
- MessageReply
  - conversation_id, reply_text, mentions[], send_mode(auto|confirm), ttl, trace_id
- AgentRegister/Heartbeat
  - agent_type=wechat-rpa, capabilities[], channel=WeChat, modes, policy_versions, trace_id, host_meta
- TaskProgress（可选）
  - task_id, status, progress, result_summary, artifacts[], conversation_id?, trace_id

## 13. SQLite 表结构（建议）
- tables
  - conversations
    - id (pk), display, last_processed_ts, last_window_w, last_n, unread_strategy_version
  - messages_idempotency
    - id (pk), conversation_id, idempotency_key, created_at
  - selectors_stats
    - id (pk), element_key, selector_version, hits, fails, last_error
  - policies
    - id (pk), version, data(json)
  - audits
    - id (pk), ts, conversation_id, action, summary, mentions(json), result, trace_id
  - blackbox
    - id (pk), ts, type, control_snapshot(json), screenshot_path(enc_path), error_stack, trace_id

## 14. 配置结构（YAML/TOML 示例）
- base_url, auth: token/mTLS
- whitelist: conversations[], mentions[]
- rate_limit: per_conversation_interval, global_qps
- quiet_hours: start, end, holidays_whitelist[], holidays_blacklist[]
- vision:
  - elements_path: "configs/elements.yaml"
  - ocr_model_path: "models/rapidocr"
  - screenshot_max_size: [1920, 1080]
  - ocr_confidence_threshold: 0.6
- backoff: initial, factor, max
- logging: level, rolling, redact_rules[], blackbox_encrypt_key_path

## 15. HTTP 与重试策略
- 客户端：`httpx`（连接池、超时：连接 1s/读 3s/写 3s）。
- 重试：`tenacity`（指数退避，最大尝试 3–5 次，幂等请求）。
- 超时治理：局部覆盖；网络异常与 5xx 才重试；4xx 不重试。
- 幂等：本地幂等键 + 云端去重；请求附带 `trace_id`。

## 15.5 指令失效与重评估机制
为处理“待处理指令上下文失效”（即机器人在等待发送回复期间，用户又发来可能使该回复过时的新消息）的边缘场景，系统在`CloudClient`服务中实现一套冲突检测与重评估机制。

### 15.5.1 核心流程
1.  **指令暂存**: 当云端下发的回复指令因速率/静默控制而无法立即发送时，该指令连同其`conversation_id`被暂存在`CloudClient`的内存队列中。
2.  **冲突检测**: 当`ReportToCloudTask`准备上报一条新消息时，会调用`CloudClient`。`CloudClient`在发送前，必须检查“暂存队列”中是否存在与新消息`conversation_id`相同的待处理指令。
3.  **指令失效**: 如果检测到冲突，`CloudClient`**必须立即移除或标记失效**队列中的待处理指令，确保它永远不会被发送。
4.  **升级上报**: `CloudClient`会中止常规的消息上报（`ingest`），转而调用一个专为此场景设计的API（如`ingest_with_context`），该API的载荷会同时包含**新消息内容**和**被失效指令的上下文**（如`trace_id`）。

### 15.5.2 API约定（示例）
**常规上报**: `POST /api/v1/messages/ingest`
```json
{ "message": { ... } }
```
**冲突后的重评估上报**: `POST /api/v1/messages/ingest_with_context`
```json
{
  "new_message": { ... },
  "invalidated_context": {
    "reason": "PENDING_REPLY_CANCELLED_BY_NEW_MESSAGE",
    "pending_reply_trace_id": "abc-123-xyz"
  }
}
```
### 15.5.3 云端决策
云端服务接收到`ingest_with_context`请求后，可以结合新旧信息，做出更智能的决策（如：更新并重新下发指令、取消原任务等），从而避免发送过时信息。

## 16. 日志、追踪、审计、黑匣子
- 日志：`loguru` 滚动，结构化字段包含 trace_id、conversation_id、action、latency。
- 追踪：可选 OpenTelemetry（OTLP）；本地生成或云端下发的 `trace_id` 统一贯穿。
- 审计：仅记录前100字摘要、@ 目标、结果状态；脱敏规则应用于敏感字段。
- 黑匣子：控件属性快照、局部截图（受频控）、错误栈、操作序列；本地加密存储，受控上传（显式授权与频控）。

## 17. 性能预算与监控指标
- CPU：视觉分析是主要开销，OCR 触发率和截图范围是关键控制点。
- 延迟：单次发言端到端 < 2s（不含云端决策）；前台占用 ≤ 500ms。
- 指标：
  - vision.locate.qps, vision.locate.success_rate
  - ocr.trigger.rate, ocr.confidence.avg
  - ingest.qps, reply.send.success_rate
  - read_only.switch.count, emergency_stop.count
  - element.ab.hit_ratio, failure.window.rate

## 18. 冒烟用例（启动后 ≤10s 内）
- **Driver 可达性测试**: `Vision Driver` 尝试定位微信核心元素（如通过`elements.yaml`中定义的`global_wechat_logo`），确保屏幕环境和配置正确。
- **Engine 初始化测试**: 检查 `Engine` 能否成功加载配置，并驱动 FSM 进入 `Idle` 状态。
- 失败则切换到 `ErrorRecovery` 状态，在托盘提示并记录审计。

## 19. 测试策略
新架构的核心优势在于其高度可测试性。

- **单元测试**: 对独立的 `Task` 类、策略函数、工具函数进行测试，验证其内部逻辑的正确性。
- **离线逻辑集成测试 (核心)**: 这是本架构的关键优势。通过在测试环境中将 `IAutomationDriver` 接口注入为 `Mock Driver` 的实例，我们可以在**完全不依赖微信UI**的情况下，对整个业务逻辑层进行快速、可靠的自动化测试。可以验证：
  - FSM 是否在正确的条件下进行了状态转换。
  - 行为树是否根据预设的模拟数据（如模拟的未读消息）执行了正确的逻辑分支。
  - 整个流程是否按预期顺序调用了 `Mock Driver` 的接口。
- **真实环境集成测试**: 使用 `Vision Driver`，在真实的微信环境中测试单个完整的业务流程（如收发一条消息）。
- **端到端 (E2E) 测试**: 在真实环境中进行完整的自动化流程测试，覆盖人机并发、异常恢复等复杂场景。

## 20. 打包与运行
- 打包：`pyinstaller` 单文件（含运行时），资源放置 `assets/`，配置 `configs/`。
- 启动：`main.py` 作为主入口，启动 `Engine`。应用作为前台进程 + 托盘运行；后续可拓展为 Windows Service（M3）。
- 更新：手动替换或后续接入自动更新渠道（待定）。

## 21. 运维与故障处理（Runbook）
- 常见问题：
  - 视觉定位失败：检查 `elements.yaml` 配置、更新图片模板、调整识别阈值。
  - OCR 置信度低：调整截图区域、进行图像预处理（如灰度化、二值化）。
  - 频繁只读：检查活跃感知与失败阈值；查看黑匣子。
- 应急：
  - 托盘紧急停用（切只读）。
  - 导出黑匣子并上报。

## 22. 安全与合规
- 最小化与脱敏：日志仅保留必要摘要；敏感字段脱敏。
- 传输：HTTPS + Token/mTLS（按云端策略）。
- 权限：必要时申请管理员权限（全局热键/活动感知）。

## 23. 风险与对策
- 微信UI更新：这是最大的风险。应对策略是及时更新 `elements.yaml` 和图片模板，并通过版本号进行管理。
- OCR波动：通过图像预处理和置信度阈值控制，对低置信度结果走失败/人工介入路径。
- @误命中：强校验与发前确认；审计标注。
- 前台打扰：严格预算 ≤ 500ms；活跃时只读。

## 24. 架构原则总结
- **分层解耦**: 严格分离UI操作、业务逻辑和原子任务，通过接口进行通信。
- **逻辑驱动**: 采用 FSM + 行为树模型，使业务流程的定义和修改变得清晰、模块化。
- **面向测试**: 通过依赖倒置和模拟对象（Mock Driver），实现对核心业务逻辑的全面、快速的自动化测试。
�务逻辑的全面、快速的自动化测试。
