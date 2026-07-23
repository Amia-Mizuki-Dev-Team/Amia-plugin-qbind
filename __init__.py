from nonebot import on_command
from nonebot.plugin import PluginMetadata
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, GroupMessageEvent, MessageSegment
from nonebot.adapters import Message
from nonebot import get_driver
from typing import Optional, List, Dict, Any
import httpx
import time
import asyncio
import re
import json
import os

# ==================== Gensokyo Markdown 开关 ====================
USE_MARKDOWN = True

# ==================== 插件元数据 ====================
__plugin_meta__ = PluginMetadata(
    name="账号绑定",
    description="账号绑定指令（仅在用户发送绑定相关指令时响应）",
    usage="qbind <你的QQ号> / qunbind <你的QQ号> / 导出: is_bound(), get_real_qq(), ensure_bound()",
    extra={
        "menu_hide": True,
        "hide": True
    }
)

# ==================== Gensokyo Markdown 构建工具 ====================

def _normalize_button(btn: Dict[str, Any]) -> Dict[str, Any]:
    """将扁平键名的按钮字典转换为嵌套字典"""
    nested: Dict[str, Any] = {}
    for key, value in btn.items():
        parts = key.split(".")
        current = nested
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return nested

def _build_button_row(buttons: List[Dict[str, Any]]) -> dict:
    """构建单行按钮"""
    btn_list = []
    for btn in buttons:
        b = _normalize_button(btn)
        rd = b.setdefault("render_data", {})
        label = rd.get("label", "按钮")
        rd.setdefault("visited_label", label)
        rd.setdefault("style", 1)
        action = b.setdefault("action", {})
        action.setdefault("type", 2)
        action.setdefault("permission", {"type": 2, "specify_user_ids": []})
        action.setdefault("data", "")
        action.setdefault("unsupport_tips", "欸，当前客户端不支持该文本捏，更新一下试试吧~")
        if action["type"] == 2:
            action.setdefault("reply", False)
            action.setdefault("enter", False)
            action.setdefault("anchor", 0)
        elif action["type"] == 0:
            action["enter"] = True
            action.pop("reply", None)
        if "id" not in b:
            b["id"] = f"btn_{hash(label) & 0xffff}"
        btn_list.append(b)
    return {"buttons": btn_list}

def _build_keyboard(buttons_config: List[List[Dict[str, Any]]]) -> dict:
    """构建完整键盘布局"""
    rows = [_build_button_row(row) for row in buttons_config]
    return {"content": {"rows": rows}}

def _build_markdown_msg(content: str, buttons: Optional[List[List[Dict[str, Any]]]] = None) -> Message:
    """构建 Gensokyo Markdown 消息"""
    md_data = {"markdown": {"content": content}}
    if buttons:
        md_data["keyboard"] = _build_keyboard(buttons)
    return Message(MessageSegment(type="markdown", data={"data": md_data}))


# ==================== 持久化绑定存储 ====================
# 用于存储 session_id → 真实QQ 的映射，供其他插件导入检查
# 其他插件使用方式：
#   qbind = require("qbind")
#   qbind.is_bound(), qbind.get_real_qq(), qbind.ensure_bound()
#   if not is_bound(event.get_user_id()):
#       await event.finish("请先发送 qbind <你的QQ号> 完成绑定！")

_BINDS_FILE = os.path.join(os.path.dirname(__file__), "binds.json")
_binds: Dict[str, str] = {}  # session_id → real_qq

def _load_binds():
    """从 JSON 文件加载持久化绑定记录"""
    global _binds
    try:
        if os.path.exists(_BINDS_FILE):
            with open(_BINDS_FILE, "r", encoding="utf-8") as f:
                _binds = json.load(f)
        else:
            _binds = {}
    except Exception:
        _binds = {}

def _save_binds():
    """将绑定记录保存到 JSON 文件"""
    try:
        with open(_BINDS_FILE, "w", encoding="utf-8") as f:
            json.dump(_binds, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# 插件加载时自动读取已有绑定
_load_binds()


# ==================== 导出函数（供其他插件使用） ====================

def is_bound(session_id: str) -> bool:
    """
    检查某个会话 ID 是否已完成绑定。
    同时检查键（虚拟ID）和值（真实QQ号），因为绑定后 Gensokyo 可能将用户标识切换为真实 QQ 号。
    """
    return session_id in _binds or session_id in _binds.values()

def get_real_qq(session_id: str) -> Optional[str]:
    """
    获取某个会话 ID 绑定的真实 QQ 号，未绑定返回 None。
    支持正向查找（虚拟ID → QQ）和反向查找（QQ 自身就是值）。
    """
    if session_id in _binds:
        return _binds[session_id]
    if session_id in _binds.values():
        return session_id  # session_id 自身就是绑定值（真实 QQ 号）
    return None

async def ensure_bound(event: MessageEvent, bot: Bot) -> bool:
    """
    在插件 handler 开头调用，如果未绑定则发送提示并返回 False。
    使用示例：
        if not await ensure_bound(event, bot):
            return
    """
    if not is_bound(event.get_user_id()):
        await _send_bind_prompt_once(event, bot)
        return False
    return True


# ==================== 绑定提示去重 ====================
# 仅用于 ensure_bound() 的显式调用，不注册全局 run_preprocessor。
# qbind 不应因为普通聊天消息而主动触发绑定提示。

_BIND_PROMPT_TTL_SECONDS = 30
_PROMPT_CACHE_KEY = "_amia_qbind_bind_prompt_cache"


def _bind_prompt_key(event: MessageEvent) -> str:
    """Build a stable event key shared by duplicate qbind module imports."""
    return ":".join((
        str(getattr(event, "self_id", "")),
        str(getattr(event, "message_id", "")),
        event.get_user_id(),
    ))


async def _send_bind_prompt_once(event: MessageEvent, bot: Bot) -> bool:
    """Send at most one bind prompt for each incoming message event."""
    app_driver = get_driver()
    now = time.monotonic()
    cache = getattr(app_driver, _PROMPT_CACHE_KEY, None)
    if cache is None:
        cache = {}
        setattr(app_driver, _PROMPT_CACHE_KEY, cache)

    for key, expires_at in tuple(cache.items()):
        if expires_at <= now:
            cache.pop(key, None)

    key = _bind_prompt_key(event)
    if key in cache:
        return False
    cache[key] = now + _BIND_PROMPT_TTL_SECONDS

    if USE_MARKDOWN:
        await bot.send(
            event,
            _build_markdown_msg(
                "### 🔒 请先完成绑定\n\n"
                "> 你还未绑定真实 QQ 号，无法使用 Bot 功能。\n\n"
                "> 请发送 `qbind <你的QQ号>` 完成绑定。",
                [[{"render_data.label": "发起绑定", "action.data": "qbind"}]],
            ),
        )
    else:
        await bot.send(event, "🔒 请先完成绑定，发送 `qbind <你的QQ号>` 完成绑定。")
    return True

# ==================== 频率限制 ====================
# 防止同一用户短时间内重复请求，打爆后端
_last_cmd_time: Dict[str, float] = {}
_RATE_LIMIT_SECONDS = 5  # 同一用户两次操作的最小间隔（秒）

def _check_rate_limit(uid: str) -> Optional[int]:
    """检查频率限制，返回 None 通过，返回 int 表示需等待秒数"""
    now = time.time()
    last = _last_cmd_time.get(uid)
    if last and now - last < _RATE_LIMIT_SECONDS:
        return int(_RATE_LIMIT_SECONDS - (now - last)) + 1
    _last_cmd_time[uid] = now
    return None


# ==================== 待确认绑定缓存 ====================
# key: "user:{user_id}", value: {"value": str, "timestamp": float}
_pending_binds: Dict[str, Dict[str, Any]] = {}
_PENDING_TIMEOUT = 120  # 二次确认有效期（秒）

def _pending_key(user_id: str) -> str:
    return f"user:{user_id}"

async def _cleanup_expired_pending():
    """后台任务：每分钟清理一次超时的待确认绑定，防止内存泄漏"""
    while True:
        await asyncio.sleep(60)
        now = time.time()
        expired = [k for k, v in list(_pending_binds.items())
                   if now - v["timestamp"] > _PENDING_TIMEOUT]
        for k in expired:
            _pending_binds.pop(k, None)

driver = get_driver()

@driver.on_startup
async def _start_cleanup():
    asyncio.create_task(_cleanup_expired_pending())


# ==================== QQ 号校验 ====================
def _validate_qq(value: str) -> Optional[str]:
    """
    校验用户输入的真实 QQ 号。
    必须为 5-11 位纯数字，不能以 0 开头。
    """
    if not re.match(r'^[1-9]\d{4,10}$', value):
        return "QQ 号格式不正确！请输入真实的 QQ 号（5-11 位数字）"
    return None


# ==================== 工具函数 ====================
def _build_api_url(session_id: str, real_qq: str) -> str:
    """
    构建 Gensokyo 绑定 API URL
    oldRowValue = 用户的会话虚拟 ID（event.get_user_id()）
    newRowValue = 用户输入的绑定参数（真实 QQ 号）
    type = 5（设置绑定值）
    """
    return f"http://127.0.0.1:15630/getid?oldRowValue={session_id}&newRowValue={real_qq}&type=5"

def _build_unbind_api_url(session_id: str) -> str:
    """
    构建 Gensokyo 解绑 API URL
    newRowValue = 0（清除绑定值）
    """
    return f"http://127.0.0.1:15630/getid?oldRowValue={session_id}&newRowValue=0&type=5"


# ==================== qbind 指令（第一步：发起绑定） ====================
# 注意：QQ 官方 Bot 的 event.get_user_id() 返回的是随机会话 ID，不是真实 QQ 号。
# 因此必须让用户手动输入自己的真实 QQ 号来完成绑定。

bind = on_command("qbind", priority=5, block=False)

@bind.handle()
async def bind_handler(event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    session_id = event.get_user_id()
    real_qq = arg.extract_plain_text().strip()

    # 频率限制（用会话ID限制）
    wait = _check_rate_limit(session_id)
    if wait:
        if USE_MARKDOWN:
            await bind.finish(_build_markdown_msg(
                f"### ⏱ 操作过于频繁\n\n> 请 {wait} 秒后再试。"
            ))
        else:
            await bind.finish(f"⏱ 操作过于频繁，请 {wait} 秒后再试。")

    # 检查参数是否为空
    if not real_qq:
        if USE_MARKDOWN:
            await bind.finish(_build_markdown_msg(
                "### ❌ 绑定失败\n\n> 请输入你的真实 QQ 号！\n\n> 格式：`qbind <你的QQ号>`\n\n"
                "> 💡 由于 QQ 官方 Bot 无法获取你的真实 QQ 号，需要你手动输入。",
                [
                    [{"render_data.label": "发起绑定", "action.data": "qbind "}],
                ]
            ))
        else:
            await bind.finish("❌ 绑定失败\n请输入你的真实 QQ 号！\n格式：qbind <你的QQ号>")

    # QQ 号格式校验
    err = _validate_qq(real_qq)
    if err:
        if USE_MARKDOWN:
            await bind.finish(_build_markdown_msg(
                f"### ❌ 绑定失败\n\n> {err}\n\n> 格式：`qbind <你的QQ号>`",
                [
                    [{"render_data.label": "重新绑定", "action.data": "qbind "}],
                ]
            ))
        else:
            await bind.finish(f"❌ 绑定失败：{err}")

    # 存入待确认缓存
    key = _pending_key(session_id)
    _pending_binds[key] = {"qq": real_qq, "timestamp": time.time()}

    # 发送二次确认消息（按钮仅发起者可用）
    if USE_MARKDOWN:
        await bind.finish(_build_markdown_msg(
            "### ⚠️ 确认绑定\n\n"
            f"> 真实 QQ：`{real_qq}`\n\n"
            "> 确认这是你的真实 QQ 号吗？绑定后 Bot 将使用该 QQ 获取你的昵称和头像。\n\n"
            "> ⏰ 请在两分钟内确认，超时自动取消\n"
            "> 请确认无误后再点击「确认绑定」",
            [
                [
                    {
                        "render_data.label": "✅ 确认绑定",
                        "action.data": "confirm_bind",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [session_id],
                    },
                    {
                        "render_data.label": "❌ 取消绑定",
                        "action.data": "cancel_bind",
                        "action.enter": True,
                        "action.permission.type": 0,
                        "action.permission.specify_user_ids": [session_id],
                    },
                ],
            ]
        ))
    else:
        await bind.finish(
            f"⚠️ 确认绑定\n\n"
            f"真实QQ：{real_qq}\n\n"
            f"请在两分钟内确认，超时自动取消\n"
            f"确认请回复：confirm_bind\n"
            f"取消请回复：cancel_bind"
        )


# ==================== confirm_bind（第二步：确认绑定） ====================

confirm = on_command("confirm_bind", priority=5, block=False)

@confirm.handle()
async def confirm_handler(event: MessageEvent, bot: Bot):
    user_id = event.get_user_id()
    key = _pending_key(user_id)

    # 频率限制
    wait = _check_rate_limit(user_id)
    if wait:
        if USE_MARKDOWN:
            await confirm.finish(_build_markdown_msg(
                f"### ⏱ 操作过于频繁\n\n> 请 {wait} 秒后再试。"
            ))
        else:
            await confirm.finish(f"⏱ 操作过于频繁，请 {wait} 秒后再试。")

    # 检查是否有待确认的绑定
    pending = _pending_binds.get(key)
    if not pending:
        if USE_MARKDOWN:
            await confirm.finish(_build_markdown_msg(
                "### ❌ 无待确认请求\n\n> 你当前没有待确认的绑定请求。\n\n> 请先发送 `qbind <你的QQ号>` 发起绑定。",
                [
                    [{"render_data.label": "发起绑定", "action.data": "qbind "}],
                ]
            ))
        else:
            await confirm.finish("❌ 你当前没有待确认的绑定请求，请先发送 `qbind <你的QQ号>` 发起绑定。")

    # 检查是否超时
    if time.time() - pending["timestamp"] > _PENDING_TIMEOUT:
        _pending_binds.pop(key, None)
        if USE_MARKDOWN:
            await confirm.finish(_build_markdown_msg(
                "### ⏰ 确认已超时\n\n> 绑定确认已超时（2分钟）。\n\n> 请重新发送 `qbind <你的QQ号>` 发起绑定。",
                [
                    [{"render_data.label": "重新绑定", "action.data": "qbind "}],
                ]
            ))
        else:
            await confirm.finish("⏰ 绑定确认已超时（2分钟），请重新发送 `qbind <你的QQ号>`。")

    real_qq = pending["qq"]
    session_id = event.get_user_id()

    # 执行绑定请求：直接调用 type=5 设置绑定值
    try:
        async with httpx.AsyncClient() as client:
            url = f"http://127.0.0.1:15630/getid?oldRowValue={session_id}&newRowValue={real_qq}&type=5"
            resp = await client.get(url)

            if resp.status_code == 200:
                data = resp.json()
                if "error" in data:
                    err_msg = data["error"]
                    _pending_binds.pop(key, None)
                    if "已存在" in err_msg:
                        hint = (
                            "> 这个 QQ 号已被其他用户绑定。\n"
                            "> 如果这是你的 QQ 号，请先解绑再重新绑定。\n"
                            "> 发送 `qunbind` 开始解绑流程。"
                        )
                        btns = [[{"render_data.label": "开始解绑", "action.data": "qunbind "}]]
                    else:
                        hint = f"> 请检查你的真实 QQ 号后重试\n> 格式：`qbind <你的QQ号>`"
                        btns = [[{"render_data.label": "重新绑定", "action.data": "qbind "}]]

                    if USE_MARKDOWN:
                        await confirm.finish(_build_markdown_msg(
                            f"### ❌ 绑定失败\n\n> {err_msg}\n\n{hint}", btns
                        ))
                    else:
                        await confirm.finish(f"❌ 绑定失败：{err_msg}")
                else:
                    _pending_binds.pop(key, None)
                    # 保存持久化绑定记录（同时存两份：虚拟ID→QQ 和 QQ→QQ）
                    _binds[session_id] = real_qq
                    _binds[real_qq] = real_qq  # 避免绑定后 Gensokyo 切换标识
                    _save_binds()
                    if USE_MARKDOWN:
                        await confirm.finish(_build_markdown_msg(
                            "### ✅ 绑定成功\n\n"
                            f"> 真实 QQ：`{real_qq}`\n\n"
                            "> 绑定完成，现在 Bot 将使用你的真实 QQ 获取昵称和头像！\n\n"
                            "> 现在你可以正常使用所有 Bot 功能了。",
                            [[{"render_data.label": "查看帮助", "action.data": "帮助"}]]
                        ))
                    else:
                        await confirm.finish(f"✅ 绑定成功！\n真实QQ：{real_qq}\n现在你可以正常使用所有 Bot 功能了。")
            else:
                _pending_binds.pop(key, None)
                err_detail = ""
                try:
                    err_data = resp.json()
                    err_detail = err_data.get("error", "")
                except Exception:
                    pass

                if "已存在" in err_detail:
                    hint = "这个 QQ 号已被其他用户绑定，请先解绑再重新绑定。"
                    btns = [[{"render_data.label": "开始解绑", "action.data": "qunbind "}]]
                else:
                    hint = f"请求后端 API 时发生错误（HTTP {resp.status_code}）\n请稍后重试或联系管理员"
                    btns = [[{"render_data.label": "重新绑定", "action.data": "qbind "}]]

                if USE_MARKDOWN:
                    await confirm.finish(_build_markdown_msg(
                        f"### ❌ 绑定失败\n\n> {hint}", btns
                    ))
                else:
                    await confirm.finish(f"❌ 绑定失败：{hint}")

    except httpx.RequestError as e:
        _pending_binds.pop(key, None)
        if USE_MARKDOWN:
            await confirm.finish(_build_markdown_msg(
                "### ❌ 绑定失败\n\n"
                "> 无法连接到后端 API（Gensokyo）\n"
                "> 请确保后端服务正在运行\n\n"
                f"> 错误详情：`{e}`",
                [[{"render_data.label": "重新绑定", "action.data": "qbind "}]]
            ))
        else:
            await confirm.finish(f"❌ 绑定失败：无法连接到后端 API。{e}")


# ==================== cancel_bind（取消绑定） ====================

cancel_cmd = on_command("cancel_bind", priority=5, block=False)

@cancel_cmd.handle()
async def cancel_handler(event: MessageEvent, bot: Bot):
    key = _pending_key(event.get_user_id())
    pending = _pending_binds.pop(key, None)

    if not pending:
        if USE_MARKDOWN:
            await cancel_cmd.finish(_build_markdown_msg(
                "### ❌ 无待取消请求\n\n> 你当前没有待取消的绑定请求。\n\n> 请先发送 `qbind <你的QQ号>` 发起绑定。",
                [
                    [{"render_data.label": "发起绑定", "action.data": "qbind "}],
                ]
            ))
        else:
            await cancel_cmd.finish("❌ 你当前没有待取消的绑定请求。")

    if USE_MARKDOWN:
        await cancel_cmd.finish(_build_markdown_msg(
            "### ✅ 已取消绑定\n\n"
            "> 绑定请求已取消，你可以随时重新发送 `qbind <你的QQ号>` 发起新的绑定。"
        ))
    else:
        await cancel_cmd.finish("✅ 已取消绑定，你可以随时重新发起绑定。")


# ==================== qunbind 指令（解绑） ====================

unbind = on_command("qunbind", priority=5, block=False)

@unbind.handle()
async def unbind_handler(event: MessageEvent, bot: Bot, arg: Message = CommandArg()):
    session_id = event.get_user_id()
    full_text = arg.extract_plain_text().strip()

    # 频率限制
    wait = _check_rate_limit(session_id)
    if wait:
        if USE_MARKDOWN:
            await unbind.finish(_build_markdown_msg(
                f"### ⏱ 操作过于频繁\n\n> 请 {wait} 秒后再试。"
            ))
        else:
            await unbind.finish(f"⏱ 操作过于频繁，请 {wait} 秒后再试。")

    # 解析参数：最后一段是 "确认" 则为确认操作，前面是真实QQ
    parts = full_text.rsplit(" ", 1)
    is_confirm = len(parts) > 1 and parts[1] == "确认"
    real_qq = parts[0] if is_confirm else full_text

    # 验证真实QQ号格式
    err = _validate_qq(real_qq)
    if err:
        if USE_MARKDOWN:
            await unbind.finish(_build_markdown_msg(
                f"### ❌ 解绑失败\n\n> {err}\n\n> 格式：`qunbind <你的QQ号>`"
            ))
        else:
            await unbind.finish(f"❌ {err}")

    # 未加 "确认" 参数 → 发送确认消息
    if not is_confirm:
        if USE_MARKDOWN:
            await unbind.finish(_build_markdown_msg(
                "### ⚠️ 确认解绑\n\n"
                f"> 真实 QQ：`{real_qq}`\n\n"
                "> 解绑后将无法使用 Bot 功能，需要重新绑定才能继续使用。\n\n"
                f"> 确认请发送：`qunbind {real_qq} 确认`",
                [
                    [
                        {
                            "render_data.label": "⚠️ 确认解绑",
                            "action.data": f"qunbind {real_qq} 确认",
                            "action.enter": True,
                            "action.permission.specify_user_ids": [session_id],
                        },
                    ],
                ]
            ))
        else:
            await unbind.finish(
                f"⚠️ 确认解绑\n\n"
                f"真实QQ：{real_qq}\n\n"
                f"确认解绑请发送：qunbind {real_qq} 确认"
            )
        return

    # 执行解绑：调用 type=5 用 newRowValue=0 清除绑定值
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:15630/getid?oldRowValue={session_id}&newRowValue=0&type=5"
            )

            # 无论 API 解绑是否成功，都清除本地绑定记录（清除两份映射）
            # 注意：用户已在命令行中指定了要解绑的 QQ 号（real_qq），优先用它释放占位
            # 不能用 _binds.get(session_id) —— session_id 可能是未绑定的虚拟 ID
            release_qq = _binds.get(session_id) or real_qq
            # 先释放 qq 号在 idmap 中的占位（避免其他用户绑定同一 QQ 时提示"已存在"）
            if release_qq:
                try:
                    await client.get(
                        f"http://127.0.0.1:15630/getid?oldRowValue={release_qq}&newRowValue=0&type=5"
                    )
                except Exception:
                    pass
            _binds.pop(session_id, None)
            if release_qq:
                _binds.pop(release_qq, None)
            _save_binds()

            if resp.status_code == 200:
                data = resp.json()
                if "error" not in data:
                    if USE_MARKDOWN:
                        await unbind.finish(_build_markdown_msg(
                            "### ✅ 已解绑\n\n"
                            f"> 会话：`{session_id}`\n\n"
                            "> 账号已解绑，需要重新使用请发送 `qbind <你的QQ号>`。",
                            [[{"render_data.label": "重新绑定", "action.data": "qbind"}]]
                        ))
                    else:
                        await unbind.finish("✅ 已解绑，需要重新使用请发送 qbind <你的QQ号>。")

            # API 返回非预期响应，但本地已清除
            if USE_MARKDOWN:
                await unbind.finish(_build_markdown_msg(
                    "### ⚠️ 已解绑（本地）\n\n"
                    f"> 本地绑定记录已清除。\n\n"
                    "> 如果遇到问题，请联系管理员。",
                    [[{"render_data.label": "重新绑定", "action.data": "qbind"}]]
                ))
            else:
                await unbind.finish("⚠️ 本地绑定记录已清除，可重新绑定。")

    except httpx.RequestError as e:
        if USE_MARKDOWN:
            await unbind.finish(_build_markdown_msg(
                "### ❌ 解绑失败\n\n"
                "> 无法连接到后端 API（Gensokyo）\n"
                "> 请确保后端服务正在运行\n\n"
                f"> 错误详情：`{e}`",
                [[{"render_data.label": "重试解绑", "action.data": "qunbind 确认"}]]
            ))
        else:
            await unbind.finish(f"❌ 解绑失败：无法连接到后端 API。{e}")


# ==================== amia-core Registry Registration ====================
from nonebot import require

_core = require("amia_core")
registry = _core.registry
from .identity import QbindIdentityResolver

@driver.on_startup
async def _register_identity_resolver():
    registry.register_identity_resolver(QbindIdentityResolver(), replace=True)
