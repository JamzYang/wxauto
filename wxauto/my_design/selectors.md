# selectors.md

本文件说明 `wxauto/my_design/elements.yaml` 的字段语义、匹配优先级、使用方式与调试建议。

## 1. 结构
- `version`：配置版本。
- `app`：应用名（WeChat）。
- `modules`：按功能域分组的选择器集合：
  - `main`：主窗口与子聊天窗口。
  - `navigation`：左侧导航栏按钮与红点视觉检测。
  - `session`：会话列表、搜索、折叠群聊、会话项结构与正则。
  - `chatbox`：消息列表、输入框、发送按钮、工具栏、新消息按钮、@ 菜单、入口与分隔符。
  - `menus`：右键菜单与菜单项。
  - `components`：选择联系人窗口、网络异常条、图片/视频预览等。
- `localization`：本地化键与默认中文值。

## 2. 通用字段语义
- `key`：选择器唯一键，使用 `模块.key` 进行引用（例如 `session.session_list`）。
- `role`：抽象角色（window/button/edit/list/listitem/menu/menuitem/pane/toolbar/visual/regex/filter）。
- `strategy`：主定位策略，常见字段：
  - `classname`：窗口/控件类名（如 `WeChatMainWndForPC`, `ChatWnd`）。
  - `name`：控件显示名（受语言影响）。
  - `regex_name`：名称的正则匹配键或表达式。
  - `control_type`：控件类型（UIA 语义，如 `ListControl`、`EditControl`）。
  - `search_depth`：搜索深度限制。
  - `parent`：依赖父节点（填入另一个 `key` 的全名）。
  - `relative`：相对定位（of/direction/hops）。
  - `type: pixel`：视觉检测（region_of/color/threshold）。
  - `path`：结构路径（用于层级索引）。
- `lang_key`：本地化键映射，Driver 应以 `localization` 覆盖 `strategy.name/regex_name`。
- `fallbacks`：兜底策略数组（structure/control/relative/ocr 等）。
- `notes`：备注，记录稳定性与场景依赖。
- `children/fields`：用于描述子结构或列表项字段提取方式。

## 3. 匹配优先级建议
1) id（若存在）
2) classname + name（或 regex_name）
3) name/regex_name 单独
4) control_type + 结构/相对定位
5) 视觉/OCR 兜底（尽量避免常态依赖）

## 4. 语言与本地化
- 运行时根据 `localization.default`（如 `zh-CN`）与 `lang_key` 将 `strategy.name/regex_name` 替换为实际文案。
- 若未提供 `lang_key`，则直接使用 `strategy` 中的原值。

## 5. 示例：如何在 Driver 中使用（伪代码）
```python
sel = elements["chatbox"]["msg_list"]
name = i18n.resolve(sel.lang_key.name, default=sel.strategy.get("name"))
ctrl = uia.find(name=name, control_type="ListControl")
```

获取输入框（无 Name，采用相对定位）：
```python
msg_list = resolve("chatbox.msg_list")
input_sel = resolve("chatbox.input_edit")
ctrl = driver.find_relative(of=msg_list, direction="below", hops=1, control_type="EditControl")
```

新消息红点检测（像素）：
```python
btn = resolve("navigation.btn_chat")
region = driver.bounding_rect(btn)
score = vision.has_red_dot(region, threshold=0.6)
```

## 6. 会话项字段解析
- `session.session_item.fields` 定义了从 `ListItemControl` 子层级提取的字段：
  - `title`: 第一个 `TextControl`。
  - `time`: 最后一个 `TextControl`。
  - `mute`: 是否存在 `PaneControl` 路径 `[4,1]`。
  - `unread`: 路径 `[2,2]` 的存在与名称可解析未读数（结合 `session.unread_count_pattern`）。

## 7. 调试与排障建议
- 打印每次匹配所用的最终 name/regex 与 control_type，记录失败原因（未找到/多于1个/类型不符）。
- 为关键路径（发送、切换、解析）至少提供两套策略（名称与结构/相对），并进行 A/B 统计。
- DPI/缩放改变时，避免使用像素检测；或调整 `threshold` 并加开关。
- 版本升级后优先验证 `main/main_window` 与三大区域入口是否仍然稳定。

## 8. 与现有代码的对应关系
- `WeChatMainWnd`（`wxauto/ui/main.py`）→ `modules.main.*`。
- `NavigationBox`（`wxauto/ui/navigationbox.py`）→ `modules.navigation.*`。
- `SessionBox`（`wxauto/ui/sessionbox.py`）→ `modules.session.*`。
- `ChatBox`（`wxauto/ui/chatbox.py`）→ `modules.chatbox.*`。
- 菜单与组件（`wxauto/ui/component.py`）→ `modules.menus.*` 和 `modules.components.*`。

## 9. 后续扩展建议
- 增加 `official_account`、`service_account`、`group_member_count` 等能力相关的二级选择器。
- 将 `regex_name` 统一从语言表中解析为真正的正则表达式，以便替换微信版本差异。
- 为 `elements.yaml` 增加 `targets: [stable, beta]` 字段，支持多版本差异化配置与灰度。
