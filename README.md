[![plus](https://plus.wxauto.org/images/wxauto_plus_logo3.png)](https://plus.wxauto.org)

# wxautoV2版本

**文档**：
[使用文档](https://docs.wxauto.org) |
[云服务器wxauto部署指南](https://docs.wxauto.org/other/deploy)

|  环境  | 版本 |
| :----: | :--: |
|   OS   | [![Windows](https://img.shields.io/badge/Windows-10\|11\|Server2016+-white?logo=windows&logoColor=white)](https://www.microsoft.com/)  |
|  微信  | [![Wechat](https://img.shields.io/badge/%E5%BE%AE%E4%BF%A1-3.9.X-07c160?logo=wechat&logoColor=white)](https://pan.baidu.com/s/1FvSw0Fk54GGvmQq8xSrNjA?pwd=vsmj) |
| Python | [![Python](https://img.shields.io/badge/Python-3.9\+-blue?logo=python&logoColor=white)](https://www.python.org/)|



[![Star History Chart](https://api.star-history.com/svg?repos=cluic/wxauto&type=Date)](https://star-history.com/#cluic/wxauto)


## 使用示例

### 1. 基本使用

```python
from wxauto import WeChat

# 初始化微信实例
wx = WeChat()

# 发送消息
wx.SendMsg("你好", who="张三")

# 获取当前聊天窗口消息
msgs = wx.GetAllMessage()
for msg in msgs:
    print(f"消息内容: {msg.content}, 消息类型: {msg.type}")
```

### 2. 监听消息

```python
from wxauto import WeChat
from wxauto.msgs import FriendMessage
import time

wx = WeChat()

# 消息处理函数
def on_message(msg, chat):
    text = f'[{msg.type} {msg.attr}]{chat} - {msg.content}'
    print(text)
    with open('msgs.txt', 'a', encoding='utf-8') as f:
        f.write(text + '\n')

    if msg.type in ('image', 'video'):
        print(msg.download())

    if isinstance(msg, FriendMessage):
        time.sleep(len(msg.content))
        msg.quote('收到')

    ...# 其他处理逻辑，配合Message类的各种方法，可以实现各种功能

# 添加监听，监听到的消息用on_message函数进行处理
wx.AddListenChat(nickname="张三", callback=on_message)

# ... 程序运行一段时间后 ...

# 移除监听
wx.RemoveListenChat(nickname="张三")

## 交互式协议模拟器（无需微信）

本工具脚本：`tools/ws_mock_with_client.py`

- **功能**
  - 使用仓内 `RpaWsClient` 直连后端 WebSocket，无需本机微信
  - 控制台交互：输入文本上报上行事件 `wechat_message`
  - 自动处理下行 `command`：先发送 `ack`，再回 `command_result`
  - 支持周期性发送事件（`--tick` 秒）用于联调

- **启动**
  - 设置环境变量（或使用命令行参数）
    - `WS_URL`：如 `ws://127.0.0.1:8080/ws/rpa`
    - `DEVICE_ID`：如 `wxrpa-001`
  - 运行：
    ```bash
    PYTHONPATH=$(pwd) python tools/ws_mock_with_client.py --tick 0
    # 或显式：python tools/ws_mock_with_client.py --ws-url ws://127.0.0.1:8080/ws/rpa --device-id wxrpa-001 --tick 0
    ```

- **控制台命令**
  - 直接输入文本：发送 `wechat_message` 事件，content=该文本
  - `/event <msgType> <text...>`：显式发送事件，`msgType=text|image|file|system`
  - `/set msgType <type>`：设置默认消息类型
  - `/json <envelope_json>`：发送自定义原始 JSON 报文
  - `/help`：显示帮助；`/quit`：退出

- **参数说明**
  - `--ws-url`：WebSocket 服务端地址（未传时读取 `WS_URL`）
  - `--device-id`：设备 ID（默认读取 `DEVICE_ID`，否则 `wxrpa-macos`）
  - `--tick`：周期性事件间隔（秒），0 表示关闭
  - `--type`：默认消息类型，默认 `text`

- **端点与设备标识**
  - 默认会将 `deviceId` 追加到 URL 查询参数（如 `/ws/rpa?deviceId=xxx`）
  - `RpaWsClient` 同时会在 Header 携带 `X-Device-Id: <device_id>`（服务端需支持其一）

- **msg示例**
    ```json
    {"eventType": "wechat_message", "data": {"messageId": "4212483504218", "from": "杨", "chatId": "全能战士测试群", "msgType": "text", "content": "@杨杨杨 开个完税证明", "raw": {"group_member_count": 3, "chat_type": "group", "chat_name": "全能战士测试群", "class": "FriendTextMessage", "id": "4212483504218", "type": "text", "attr": "friend", "content": "@杨杨杨 开个完税证明"}}}
    ```
  

## 最后
如果对您有帮助，希望可以帮忙点个Star，如果您正在使用这个项目，可以将右上角的 Unwatch 点为 Watching，以便在我更新或修复某些 Bug 后即使收到反馈，感谢您的支持，非常感谢！

## 免责声明
代码仅用于对UIAutomation技术的交流学习使用，禁止用于实际生产项目，请勿用于非法用途和商业用途！如因此产生任何法律纠纷，均与作者无关！