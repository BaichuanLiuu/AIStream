import asyncio
import re
import pyvts
from websockets.exceptions import ConnectionClosed

REQUEST_TIMEOUT_SECONDS = 5
MAX_RECONNECT_ATTEMPTS = 3
RECONNECT_BASE_DELAY_SECONDS = 0.6

"""
textToMotion.py
用途：
    1. 接收一段文本（之后可由语音转文字传入）
    2. 通过简单规则 / 你的AI模型分析文本情绪
    3. 使用 VTube Studio 的 API 触发对应的动作 / 表情（通过热键）

使用前准备：
    1. 安装依赖： pip install pyvts
    2. 打开 VTube Studio，在设置里开启 API（默认端口 8001）
    3. 可直接使用默认模型；程序会自动读取当前模型热键并做关键词匹配
    4. 如果自动匹配不理想，可在 EMOTION_TO_VTS_HOTKEY_OVERRIDE 里手动覆盖
"""

# ========== 配置区：根据你自己的 VTS 设置修改这里 ==========

VTS_CONFIG = {
    "plugin_name": "AI_Text_Emotion_Controller",
    "developer": "YourName",
    "authentication_token_path": "./vts_token.txt",
}

# 情绪关键词（用于自动匹配默认模型热键）
EMOTION_KEYWORDS = {
    "happy": ["happy", "smile", "joy", "开心", "高兴", "笑"],
    "sad": ["sad", "cry", "tears", "难过", "伤心", "哭"],
    "angry": ["angry", "mad", "rage", "生气", "愤怒"],
    "surprised": ["surprise", "wow", "惊讶", "震惊"],
    "neutral": ["neutral", "normal", "idle", "默认", "清除", "reset"],
}

# 手动覆盖映射（可选）
# 如果你知道默认模型里某个热键名，写在这里会优先使用
EMOTION_TO_VTS_HOTKEY_OVERRIDE = {
    # "happy": "Happy",
    # "sad": "Sad",
    # "angry": "Angry",
    # "surprised": "Surprised",
    # "neutral": "ClearAll",
}

# ============================================================


class VTSController:
    def __init__(self):
        self.vts = None
        self.authenticated = False
        self.available_hotkeys = []
        self.emotion_hotkey_map = {}
        self._request_lock = asyncio.Lock()

    @staticmethod
    def _normalize(s: str) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s.lower())

    def _build_emotion_hotkey_map(self, hotkey_names):
        """根据当前模型热键名自动匹配情绪 -> 热键"""
        mapping = {}

        for emotion, override_name in EMOTION_TO_VTS_HOTKEY_OVERRIDE.items():
            if override_name in hotkey_names:
                mapping[emotion] = override_name

        for emotion, keywords in EMOTION_KEYWORDS.items():
            if emotion in mapping:
                continue

            for hk_name in hotkey_names:
                hk_norm = self._normalize(hk_name)
                if any(self._normalize(k) in hk_norm for k in keywords):
                    mapping[emotion] = hk_name
                    break

        return mapping

    async def connect(self):
        """连接并授权 VTube Studio（兼容 pyvts 0.3.x）"""
        self.vts = pyvts.vts(plugin_info=VTS_CONFIG)
        await asyncio.wait_for(self.vts.connect(), timeout=REQUEST_TIMEOUT_SECONDS)

        # 先尝试读取已有 token；没有则请求新 token
        await asyncio.wait_for(self.vts.read_token(), timeout=REQUEST_TIMEOUT_SECONDS)
        if not self.vts.authentic_token:
            print("首次运行，正在向 VTS 请求插件授权，请切回 VTS 并点击“允许”...")
            await asyncio.wait_for(self.vts.request_authenticate_token(), timeout=REQUEST_TIMEOUT_SECONDS)

        ok = await asyncio.wait_for(self.vts.request_authenticate(), timeout=REQUEST_TIMEOUT_SECONDS)
        if not ok:
            raise RuntimeError("VTube Studio 授权失败，请在 VTS 插件授权中允许该插件后重试")

        self.authenticated = True
        print("✅ 已成功连接并授权 VTube Studio")

        # 读取当前模型热键（pyvts 0.3.x 需手动构造请求）
        hotkeys_req = self.vts.vts_request.requestHotKeyList()
        hotkeys_resp = await asyncio.wait_for(self.vts.request(hotkeys_req), timeout=REQUEST_TIMEOUT_SECONDS)
        hotkeys_data = hotkeys_resp.get("data", {})
        hotkeys = hotkeys_data.get("availableHotkeys", [])

        self.available_hotkeys = [h.get("name") for h in hotkeys if h.get("name")]
        print("当前模型可用热键：", self.available_hotkeys)

        self.emotion_hotkey_map = self._build_emotion_hotkey_map(self.available_hotkeys)
        print("自动匹配到的情绪映射：", self.emotion_hotkey_map)
        if not self.emotion_hotkey_map:
            print("⚠️ 没有匹配到任何情绪热键。请检查默认模型是否有热键，或在 EMOTION_TO_VTS_HOTKEY_OVERRIDE 里手动配置。")

    async def reconnect(self) -> bool:
        """连接断开后尝试重连（指数退避）"""
        self.authenticated = False

        for attempt in range(1, MAX_RECONNECT_ATTEMPTS + 1):
            try:
                if self.vts:
                    try:
                        await self.vts.close()
                    except Exception:
                        pass

                print(f"检测到连接异常，开始第 {attempt}/{MAX_RECONNECT_ATTEMPTS} 次重连...")
                await self.connect()
                print("重连成功")
                return True
            except Exception as e:
                if attempt >= MAX_RECONNECT_ATTEMPTS:
                    print(f"重连失败，已达到最大重试次数：{e}")
                    return False

                delay = RECONNECT_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                print(f"重连失败：{e}，{delay:.1f}s 后重试")
                await asyncio.sleep(delay)

        return False

    async def trigger_motion_by_emotion(self, emotion: str):
        """根据情绪触发 VTS 热键（串行发送，避免并发冲突）"""
        async with self._request_lock:
            if not self.authenticated:
                print("⚠️ 未连接/未授权 VTS，无法触发动作")
                return

            emotion = emotion.lower().strip()
            hotkey_name = self.emotion_hotkey_map.get(emotion)
            if not hotkey_name:
                print(f"⚠️ 情绪 [{emotion}] 未匹配到当前模型热键，可在 EMOTION_TO_VTS_HOTKEY_OVERRIDE 中手动指定")
                return

            print(f"🎬 触发情绪 [{emotion}] 对应的 VTS 热键: {hotkey_name}")
            trigger_req = self.vts.vts_request.requestTriggerHotKey(hotkey_name)
            try:
                await asyncio.wait_for(self.vts.request(trigger_req), timeout=REQUEST_TIMEOUT_SECONDS)
            except (ConnectionClosed, asyncio.TimeoutError) as e:
                print(f"⚠️ VTS 请求失败：{e}")
                ok = await self.reconnect()
                if ok:
                    # 重连后热键映射可能更新，重新取一次
                    hotkey_name = self.emotion_hotkey_map.get(emotion)
                    if hotkey_name:
                        print(f"🔁 重试触发热键: {hotkey_name}")
                        retry_req = self.vts.vts_request.requestTriggerHotKey(hotkey_name)
                        try:
                            await asyncio.wait_for(self.vts.request(retry_req), timeout=REQUEST_TIMEOUT_SECONDS)
                        except Exception as retry_err:
                            print(f"⚠️ 重试仍失败：{retry_err}")


# ================= 文本 → 情绪 的简单分析逻辑 =================
# 这里你可以替换为你原来 textToMotion.py 里的大模型 / AI 推理逻辑
# 现在仅做示例：用关键词 + 简单规则判断情绪

def analyze_emotion_from_text(text: str) -> str:
    """
    输入：任意文本（以后可以是语音转文字的结果）
    输出：情绪标签字符串：happy / sad / angry / surprised / neutral
    """
    t = text.lower()

    # 英文关键字
    if re.search(r"\b(happy|glad|excited|joy|lol|lmao)\b", t):
        return "happy"
    if re.search(r"\b(sad|unhappy|depressed|cry|upset)\b", t):
        return "sad"
    if re.search(r"\b(angry|mad|furious|annoyed)\b", t):
        return "angry"
    if re.search(r"\b(wow|omg|what\?|surprised|shocked)\b", t):
        return "surprised"

    # 中文关键字（非常粗略，你可以继续补）
    if any(k in text for k in ["开心", "高兴", "激动", "兴奋", "笑死"]):
        return "happy"
    if any(k in text for k in ["难过", "伤心", "沮丧", "想哭", "崩溃"]):
        return "sad"
    if any(k in text for k in ["生气", "愤怒", "火大", "气死", "烦死"]):
        return "angry"
    if any(k in text for k in ["惊讶", "吓", "震惊", "卧槽", "我靠"]):
        return "surprised"

    # 默认情绪
    return "neutral"


# =================== 主流程：从文本到动作 ===================


class TextEmotionMotionService:
    """可嵌入主程序的服务接口：start / submit_text / stop"""

    def __init__(self, queue_size: int = 32):
        self.controller = VTSController()
        self.emotion_queue = asyncio.Queue(maxsize=queue_size)
        self.worker_task: asyncio.Task | None = None
        self.started = False

    async def _emotion_worker(self):
        """后台情绪处理：异步消费任务，不阻塞主流程（口型可继续由其它通道驱动）"""
        while True:
            emotion = await self.emotion_queue.get()
            try:
                if emotion is None:  # 结束信号
                    return
                await self.controller.trigger_motion_by_emotion(emotion)
            except Exception as e:
                print(f"⚠️ 情绪动作处理异常：{e}")
            finally:
                self.emotion_queue.task_done()

    async def start(self) -> bool:
        if self.started:
            return True

        try:
            await self.controller.connect()
        except Exception as e:
            print(f"❌ 无法连接 VTube Studio：{e}")
            print("请确认 VTube Studio 已启动，并在设置中开启 API（默认端口 8001）。")
            return False

        self.worker_task = asyncio.create_task(self._emotion_worker())
        self.started = True

        print("\n--- 文本 → 情绪 → VTS 动作 系统已启动 ---")
        print("提示：已适配 VTube Studio 默认模型（基于热键自动匹配）")
        print("提示：情绪触发走后台队列，不会阻塞其它实时能力（如口型）")
        return True

    def submit_text(self, text: str) -> bool:
        """提交文本（通常由 STT 回调调用）。返回是否成功入队。"""
        if not self.started:
            print("⚠️ 服务尚未启动，请先调用 await start()")
            return False

        text = (text or "").strip()
        if not text:
            return False

        emotion = analyze_emotion_from_text(text)
        print(f"🔍 文本分析结果：[{text}] -> 情绪: [{emotion}]")

        try:
            self.emotion_queue.put_nowait(emotion)
            return True
        except asyncio.QueueFull:
            print("⚠️ 情绪队列繁忙，已丢弃本次情绪触发")
            return False

    async def stop(self):
        if not self.started:
            return

        await self.emotion_queue.join()
        await self.emotion_queue.put(None)
        if self.worker_task:
            await self.worker_task

        if self.controller.vts:
            await self.controller.vts.close()

        self.started = False
        print("程序结束，已断开与 VTS 的连接")


async def run_text_to_motion_loop():
    """命令行演示入口。正式接入时建议直接使用 TextEmotionMotionService。"""
    service = TextEmotionMotionService()
    started = await service.start()
    if not started:
        return

    print("输入 exit / quit 可退出程序\n")

    try:
        while True:
            text = (await asyncio.to_thread(input, "请输入一句文本（模拟语音识别结果）：")).strip()
            if text.lower() in ("exit", "quit"):
                break

            service.submit_text(text)
    finally:
        await service.stop()


if __name__ == "__main__":
    # 运行前请确保：已 pip install pyvts 且 VTS 已打开并启用 API
    asyncio.run(run_text_to_motion_loop())