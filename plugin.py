"""Neko TTS — per-reply opt-in speech, without chat-scoped intent state."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import hashlib
import time
from typing import Any, Literal

from maibot_sdk import (CONFIG_RELOAD_SCOPE_SELF, Command, Field, HookHandler,
                        MaiBotPlugin, PluginConfigBase, ReplyExtension)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

try:
    from .audio import SpeechEngine, split_text, voice_message
except ImportError:
    from audio import SpeechEngine, split_text, voice_message


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=False, description="启用插件；上线前停用其他自动 TTS 插件")
    config_version: str = Field(default="0.2.0")


class GeneralConfig(PluginConfigBase):
    timeout: int = Field(default=120, ge=5, le=180, description="整条回复合成期限（含排队）")
    max_text_length: int = Field(default=500, ge=50, le=2000, description="每段最大字符数；按句分段，不截断")
    max_total_length: int = Field(default=2000, ge=50, le=6000, description="总长度上限；超过则保留文字")
    max_segments: int = Field(default=8, ge=1, le=12, description="分段上限；超过则保留文字")


class VoiceConfig(PluginConfigBase):
    voice_dir: str = Field(default="", description="参考音频目录；预置音色不使用")
    preferred_reference_file: str = Field(default="", description="固定单个参考文件名；空时自动选择有效有声时长较长的一段")
    reference_strategy: Literal["best_single", "balanced", "full_merge"] = Field(default="best_single", description="单段、每段均衡片段或完整拼接")
    clone_prompt: str = Field(default="保持参考音色，自然清晰地说话。", description="基础合成提示；本轮 style 追加于其后")
    sample_rate: Literal[16000, 24000, 44100, 48000] = 24000
    max_clip_duration: float = Field(default=15.0, ge=3.0, le=30.0)
    ffmpeg_path: str = Field(default="", description="FFmpeg 路径；空时从 PATH 查找")


class MimoConfig(PluginConfigBase):
    api_key: str = Field(default="", description="MiMo API Key，仅存放于本机配置")
    api_base_url: str = Field(default="https://api.xiaomimimo.com/v1")
    synthesis_mode: Literal["voiceclone", "preset"] = "voiceclone"
    model: str = Field(default="mimo-v2.5-tts-voiceclone", description="克隆模型；预置模式固定使用 mimo-v2.5-tts")
    preset_voice: Literal["冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean", "mimo_default"] = "冰糖"
    audio_format: Literal["mp3", "wav"] = "mp3"


class OutputConfig(PluginConfigBase):
    mode: Literal["text_and_voice", "text_then_voice", "voice_only"] = Field(
        default="text_and_voice",
        description="同一批发送、文字发送后后台补发、或只发语音",
    )


class CommandConfig(PluginConfigBase):
    allowed_user_ids: list[str] = Field(default_factory=list, description="/neko-tts 测试命令白名单；空表示不开放")


class Config(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    mimo: MimoConfig = Field(default_factory=MimoConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    command: CommandConfig = Field(default_factory=CommandConfig)


class NekoTTS(MaiBotPlugin):
    config_model = Config

    def __init__(self):
        super().__init__()
        self._engine = None
        self._tasks = set()
        self._generation = 0
        self._ready = False
        self._pending: dict[str, list[tuple[str, str, float]]] = {}

    async def on_load(self):
        self._engine = SpeechEngine(self.ctx.paths.runtime_dir)
        self._ready = True
        self.ctx.logger.info("Neko TTS 已加载：按 reply 主动选择语音")

    async def _reset(self):
        self._ready = False
        self._generation += 1
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._engine:
            await self._engine.close()
        self._pending.clear()

    async def on_unload(self):
        await self._reset()

    async def on_config_update(self, scope, config_data, version):
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            await self._reset()
            self._engine = SpeechEngine(self.ctx.paths.runtime_dir)
            self._ready = True

    async def _render(self, text, style):
        if not self._ready or not self.config.plugin.enabled:
            raise ValueError("插件未启用或正在重载")
        if not isinstance(style, str) or len(style) > 240:
            raise ValueError("语气提示必须是240字以内文本")
        config = self.config.model_dump()
        chunks = split_text(text, config['general'])
        generation = self._generation
        task = asyncio.create_task(self._engine.render(chunks, style, config))
        self._tasks.add(task)
        try:
            result = await task
            if generation != self._generation:
                raise ValueError("配置已更新，请重新调用")
            return result
        finally:
            self._tasks.discard(task)

    @ReplyExtension(
        "voice",
        description=("你想用语音表达这条回复时才选择。默认仅文字，不必每次选；可按心情填写 style。"
                     "保留原文字和附件，另发独立语音；参数仅本次生效。"),
        parameters={"type": "object", "properties": {
            "style": {"type": "string", "maxLength": 240, "default": "",
                      "description": ("仅本轮声音表达，不是朗读正文。用简短、具体且一致的描述，"
                                      "可按情绪、语速、音量、停顿、表演程度组织；无需全部填写。"
                                      "例如：情绪温柔安慰；语速稍慢；音量偏低；自然短停顿；表演克制。"
                                      "避免只写可爱一点、有感情，不要求改写正文。")}},
            "additionalProperties": False},
        priority=20, timeout_ms=240000,
    )
    async def voice_reply_extension(self, phase="", text="", messages=None, parameters=None, **kwargs):
        if not self._ready or not self.config.plugin.enabled:
            return {}
        if phase == "prepare":
            return {}  # Voice style must not leak into the text-generation prompt.
        if phase != "before_send":
            return {}
        try:
            # Use actually generated text segments, excluding mentions/quotes/media.
            spoken = "\n".join(segment['data'] for message in (messages or [])
                               for segment in message.get('segments', [])
                               if segment.get('type') == 'text' and isinstance(segment.get('data'), str))
            style = str((parameters or {}).get('style') or '')
            if self.config.output.mode == "text_then_voice":
                stream_id = str(kwargs.get("session_id") or "").strip()
                self._pending.setdefault(stream_id, []).append(
                    (spoken, style, time.monotonic() + 180)
                )
                return {"messages": deepcopy(messages)}

            audio = await self._render(spoken, style)
            if self.config.output.mode == "voice_only":
                return {"messages": [voice_message(audio)]}
            return {"messages": deepcopy(messages) + [voice_message(audio)]}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("Neko TTS 合成失败，保留原回复：%s", type(exc).__name__)
            return {}

    @HookHandler("send_service.after_send", name="neko_tts_after_send", mode=HookMode.BLOCKING,
                 order=HookOrder.LATE, timeout_ms=300000, error_policy=ErrorPolicy.SKIP)
    async def after_text_send(self, message=None, sent=False, **kwargs):
        """For text_then_voice, queue the exact delivered reply for background speech."""
        if not sent or not self._ready or not self.config.plugin.enabled:
            return None
        if self.config.output.mode != "text_then_voice":
            return None
        stream_id = str(kwargs.get("stream_id") or (message or {}).get("session_id") or "").strip()
        text = "\n".join(
            str(item.get("data") or "")
            for item in (message or {}).get("raw_message", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
        if not stream_id or not text:
            return None
        pending = self._pending.get(stream_id, [])
        now = time.monotonic()
        match = next(((i, item) for i, item in enumerate(pending) if item[2] > now and item[0] == text), None)
        if match is None:
            self._pending[stream_id] = [item for item in pending if item[2] > now]
            return None
        index, (_, style, _) = match
        pending.pop(index)
        if not pending:
            self._pending.pop(stream_id, None)
        task = asyncio.create_task(self._background_voice(text, style, stream_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return None

    async def _background_voice(self, text: str, style: str, stream_id: str) -> None:
        try:
            audio = await self._render(text, style)
            await self.ctx.send.custom(
                "voice", base64.b64encode(audio).decode("ascii"), stream_id,
                processed_plain_text=text, sync_to_maisaka_history=False,
                maisaka_source_kind="neko_tts",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("Neko TTS 后台补发失败，文字已保留：%s", type(exc).__name__)

    @Command("neko_tts_cmd", description="测试语音：/neko-tts <文本>；/neko-tts status 查看模式",
             pattern=r"^/neko-tts(?:\s+(?P<text>[\s\S]+))?$")
    async def command(self, stream_id="", user_id="", matched_groups=None, **kwargs):
        if not self.config.plugin.enabled or not self._ready:
            return False, "Neko TTS 未启用", True
        if not user_id or str(user_id) not in {str(x) for x in self.config.command.allowed_user_ids}:
            return False, "测试命令未开放", True
        if not stream_id:
            return False, "无法获取当前聊天", True
        text = str((matched_groups or {}).get('text') or '').strip()
        if text == 'status':
            return True, f"Neko TTS：{self.config.mimo.synthesis_mode}；按回复选择语音。", True
        if not text:
            return False, "用法：/neko-tts <文本>", True
        try:
            audio = await self._render(text, '')
            sent = await self.ctx.send.custom('voice', base64.b64encode(audio).decode('ascii'), stream_id,
                                             processed_plain_text=text, sync_to_maisaka_history=True,
                                             maisaka_source_kind='tool_voice')
            return bool(sent), "语音已发送" if sent else "语音发送失败", True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.warning("Neko TTS 测试失败：%s", type(exc).__name__)
            return False, "语音合成失败，请检查配置和服务状态。", True


def create_plugin():
    return NekoTTS()
