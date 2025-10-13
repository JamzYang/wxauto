from wxauto.wx import WeChat
from wxauto.rpa_client import start_rpa_bridge

# 1. 启动并登录 Windows WeChat 3.9（本机）
wx = WeChat(debug=True)  # 会自动 Show，并开启监听线程

# 2. 启动 RPA 代理（连接服务端 WS）
agent = start_rpa_bridge(
    wx=wx,
    server_ws_url="ws://localhost:8080/ws/rpa",
    device_id="wxrpa-001",
    token=None  # 如有鉴权，传入 Bearer Token
)

# 3. 保活主线程（可选）
wx.KeepRunning()