# -*- coding: utf-8 -*-
"""
@提及风格聊天事件模拟器（无需微信）

功能：
- 复用仓内 WebSocket 客户端（RpaWsClient）连接后端
- 控制台交互：输入“@某某 我的内容”或“at某某 我的内容”即发送 wechat_message 事件
- 事件 JSON 与仓内协议一致：type=event，payload.eventType=wechat_message
- 内置“说话人(from)”与“chatId”，可通过命令行参数覆盖
- 自动处理下行 command：发送 ack，并根据 action 返回 command_result

用法示例：
    export WS_URL=ws://127.0.0.1:8080/ws
    export DEVICE_ID=wxrpa-001
    PYTHONPATH=$(pwd) python tools/ws_mock_chat_at.py \
        --from-name "杨" \
        --chat-id "全能战士测试群" \
        --type text

交互示例（控制台）：
- @杨杨杨 河南云知企科技有限公司
- at张三 明早10点开会
- /help   显示帮助
- /quit   退出
"""
from __future__ import annotations

import os
import sys
import json
import time
import uuid
import threading
import argparse
from typing import Dict, Any, Optional
import logging

# 仅使用网络层，不导入任何 Win32/WeChat 相关模块
# 尝试将仓库根目录加入 sys.path，支持未安装包的直接运行
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

RpaWsClient = None  # type: ignore
import importlib.util
_RPA_CLIENT_PATH = os.path.join(_REPO_ROOT, "wxauto", "rpa_client.py")
if os.path.isfile(_RPA_CLIENT_PATH):
    _spec = importlib.util.spec_from_file_location("wxauto_rpa_client_standalone", _RPA_CLIENT_PATH)
    if _spec and _spec.loader:
        _mod = importlib.util.module_from_spec(_spec)
        try:
            _spec.loader.exec_module(_mod)  # type: ignore
        except ModuleNotFoundError as e:
            if e.name in ("websocket", "websocket-client", "requests"):
                print("[!] 依赖缺失：请先安装 websocket-client 与 requests\n    python3 -m pip install websocket-client requests")
                raise SystemExit(1)
            else:
                raise
        RpaWsClient = getattr(_mod, "RpaWsClient")
if RpaWsClient is None:
    try:
        from wxauto.rpa_client import RpaWsClient  # type: ignore
    except ModuleNotFoundError as e:
        if e.name in ("comtypes",):
            print("[!] 检测到 Windows 专属依赖 comtypes。macOS 上无需安装它。请直接安装网络依赖：\n    python3 -m pip install websocket-client requests")
            raise SystemExit(1)
        else:
            raise


def now_ms() -> int:
    """当前毫秒时间戳"""
    return int(time.time() * 1000)


class AtChatMock:
    """@提及风格聊天事件模拟器核心类

    职责：
    - 维护 WebSocket 连接（RpaWsClient）
    - 控制台输入解析与事件发送（@某某 我的内容 | at某某 我的内容）
    - 下行 command 自动应答（ack + command_result）
    - 统一封装协议 Envelope
    """

    def __init__(
        self,
        url: str,
        device_id: str,
        from_name: str = "杨",
        chat_id: str = "全能战士测试群",
        default_msg_type: str = "text",
        device_in: str = "query",
        header_name: str = "X-Device-Id",
        tick: int = 0,
    ) -> None:
        """初始化

        参数：
        - url: WebSocket 服务端 URL
        - device_id: 设备 ID（出现在协议 envelop 中）
        - from_name: 说话人（data.from）
        - chat_id: 会话 ID（data.chatId）
        - default_msg_type: 默认消息类型（text）
        - device_in: 设备标识放置位置（query/header），RpaWsClient 始终会加 header
        - tick: 周期性事件（心跳/演示）间隔（秒），0 表示关闭
        """
        self.url = url
        self.device_id = device_id
        self.from_name = from_name
        self.chat_id = chat_id
        self.default_msg_type = (default_msg_type or "text").strip() or "text"
        self.device_in = device_in
        self.header_name = header_name
        self.tick = max(0, int(tick))

        # 统一网络层日志到 STDOUT
        logger = logging.getLogger("wxauto4.ws")
        logger.handlers.clear()
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)

        # 根据 device_in 决定是否在 URL 中附加 deviceId（RpaWsClient 始终发送 X-Device-Id 头）
        url_with_device = self.url
        if self.device_in == "query":
            sep = '&' if '?' in url_with_device else '?'
            url_with_device = f"{url_with_device}{sep}deviceId={self.device_id}"

        # 建立连接
        self.ws = RpaWsClient(
            url=url_with_device,
            device_id=self.device_id,
            token=None,
            on_message=self._on_ws_message,
            on_open=self._on_ws_open,
        )
        self._running = threading.Event()

    # -------------------- 生命周期 --------------------
    def start(self) -> None:
        """启动连接与交互线程"""
        self._running.set()
        self.ws.start()
        if self.tick > 0:
            threading.Thread(target=self._auto_event_loop, daemon=True).start()
        threading.Thread(target=self._stdin_loop, daemon=True).start()
        self._print_banner()
        print(f"[i] deviceId={self.device_id} deviceIn={self.device_in} url_effective={self.ws.url}")

        try:
            while self._running.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self) -> None:
        """停止运行"""
        self._running.clear()
        try:
            self.ws.stop()
        except Exception:
            pass

    # -------------------- 上行：事件发送 --------------------
    def _send_wechat_message(self, content: str, msg_type: Optional[str] = None) -> None:
        """发送一条 wechat_message 上行事件"""
        mtype = (msg_type or self.default_msg_type).strip() or "text"
        data = self._build_wechat_message(content=content, msg_type=mtype)
        trace_id = str(uuid.uuid4())
        self._send_envelope("event", {"eventType": "wechat_message", "data": data}, trace_id=trace_id)
        print(f"[↑ event] traceId={trace_id} wechat_message: type={mtype}, from={self.from_name}, chatId={self.chat_id}, content={content}")

    def _build_wechat_message(self, content: str = "@某某 你好", msg_type: str = "text") -> Dict[str, Any]:
        """构造后端协议要求的 data 部分"""
        return {
            "messageId": f"wxmsg-{uuid.uuid4()}",
            "from": self.from_name,
            "chatId": self.chat_id,
            "msgType": msg_type,  # text|image|file|system
            "content": content,
            "raw": {},
        }

    # -------------------- 下行：处理 command --------------------
    def _on_command(self, envelope: Dict[str, Any]) -> None:
        """处理 type=command（收到后已在 _on_ws_message 中发送 ack）"""
        if envelope.get("type") != "command":
            print(f"[↓ other ] {json.dumps(envelope, ensure_ascii=False)}")
            return
        trace_id = envelope.get("traceId")
        payload = envelope.get("payload") or {}
        command_id = payload.get("commandId")
        action = payload.get("action")
        params = payload.get("params") or {}
        timeout_ms = payload.get("timeoutMs")

        print(f"[↓ command] traceId={trace_id} commandId={command_id} action={action} params={json.dumps(params, ensure_ascii=False)} timeoutMs={timeout_ms}")

        # 模拟超时
        if timeout_ms and timeout_ms < 100:
            time.sleep(timeout_ms / 1000.0 + 0.2)
            self._send_envelope("command_result", {"commandId": command_id, "status": "timeout", "result": {}, "error": None}, trace_id=trace_id)
            print(f"[↑ result] traceId={trace_id} commandId={command_id} status=timeout")
            return

        # 简单映射：统一 success
        if action in ("send_text", "send_files", "chat_with", "add_listener",
                      "remove_listener", "start_listening", "stop_listening",
                      "quote", "forward"):
            result = {"echoParams": params, "messageId": f"mock-{uuid.uuid4()}"}
            self._send_envelope("command_result", {"commandId": command_id, "status": "success", "result": result, "error": None}, trace_id=trace_id)
            print(f"[↑ result] traceId={trace_id} commandId={command_id} status=success")
        else:
            self._send_envelope(
                "command_result",
                {
                    "commandId": command_id,
                    "status": "rejected",
                    "result": {},
                    "error": {"code": "INVALID_ACTION", "message": f"未知指令: {action}"},
                },
                trace_id=trace_id,
            )
            print(f"[↑ result] traceId={trace_id} commandId={command_id} status=rejected")

    # -------------------- WS 回调与通用发送 --------------------
    def _on_ws_open(self) -> None:
        """WS 连接建立回调：上报运行状态"""
        try:
            self._send_envelope("status", {"running": True, "version": "v1", "lastError": None})
        except Exception:
            pass

    def _on_ws_message(self, message: str) -> None:
        """WS 消息回调：解析 command，先 ack 再交由 _on_command 处理"""
        try:
            envelope = json.loads(message)
        except Exception:
            print(f"[↓ other ] {message}")
            return
        if envelope.get("type") == "command":
            trace_id = envelope.get("traceId")
            payload = envelope.get("payload") or {}
            command_id = payload.get("commandId")
            if command_id:
                self._send_envelope("ack", {"forType": "command", "forId": command_id}, trace_id=trace_id)
            self._on_command(envelope)
        else:
            print(f"[↓ other ] {json.dumps(envelope, ensure_ascii=False)}")

    def _send_envelope(self, typ: str, payload: Dict[str, Any], trace_id: Optional[str] = None) -> None:
        """发送通用协议 Envelope 到服务端"""
        data = {
            "type": typ,
            "traceId": trace_id or str(uuid.uuid4()),
            "deviceId": self.device_id,
            "timestamp": now_ms(),
            "payload": payload or {},
        }
        self.ws.send_json(data)

    # -------------------- 辅助：输入解析与交互 --------------------
    def _print_banner(self) -> None:
        print(
            """
================= @提及风格聊天事件 =================
- 输入“@某某 我的内容”或“at某某 我的内容”发送 wechat_message 事件
- /help                        显示帮助
- /quit                        退出
====================================================
""".strip()
        )

    def _normalize_at_line(self, line: str) -> str:
        """将以 at 开头的输入规范化为以 @ 开头
        例如："at张三 你好" -> "@张三 你好"
        """
        if not line:
            return line
        s = line.strip()
        if s.startswith("@"):  # 已经是 @
            return s
        # 常见误输入：以 at 开头，无空格
        if s.startswith("at") and not s.startswith("at "):
            return "@" + s[2:]
        # 常见误输入：以 at 空格 开头
        if s.startswith("at "):
            return "@" + s[3:]
        return s

    def _stdin_loop(self) -> None:
        """控制台输入循环：仅需输入“@某某 内容”即可发送"""
        while self._running.is_set():
            try:
                line = sys.stdin.readline()
            except Exception:
                break
            if not line:
                time.sleep(0.1)
                continue
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", ":q", "exit"):
                self.stop()
                break
            if line in ("/help", "-h", "--help"):
                self._print_banner()
                continue

            # 规范化 at 输入并发送
            content = self._normalize_at_line(line)
            self._send_wechat_message(content, None)

    # -------------------- 自动事件循环（可选） --------------------
    def _auto_event_loop(self) -> None:
        while self._running.is_set():
            try:
                self._send_wechat_message(content="@机器人 自动心跳", msg_type=self.default_msg_type)
            except Exception:
                pass
            t0 = time.time()
            while self._running.is_set() and time.time() - t0 < self.tick:
                time.sleep(0.2)


def main() -> None:
    parser = argparse.ArgumentParser(description="@提及风格聊天事件模拟器（使用 WsClient）")
    parser.add_argument("--ws-url", type=str, default="ws://127.0.0.1:8080/ws", help="WebSocket 服务端地址，如 ws://127.0.0.1:8080/ws")
    parser.add_argument("--device-id", type=str, default=os.getenv("DEVICE_ID", "wxrpa-001"))
    parser.add_argument("--from-name", type=str, default=os.getenv("FROM_NAME", "杨"), help="说话人名称（data.from）")
    parser.add_argument("--chat-id", type=str, default=os.getenv("CHAT_ID", "全能战士测试群"), help="会话标识（data.chatId）")
    parser.add_argument("--type", dest="msg_type", type=str, default="text", help="默认消息类型")
    parser.add_argument("--tick", type=int, default=0, help="周期性事件发送间隔（秒），0 表示关闭")
    args = parser.parse_args()

    ws_url = args.ws_url or os.getenv("WS_URL")
    if not ws_url:
        raise SystemExit("缺少 WS_URL，可通过 --ws-url 或环境变量 WS_URL 提供")

    mock = AtChatMock(
        url=ws_url,
        device_id=args.device_id,
        from_name=args.from_name,
        chat_id=args.chat_id,
        default_msg_type=args.msg_type,
        tick=args.tick,
    )
    mock.start()


if __name__ == "__main__":
    main()
