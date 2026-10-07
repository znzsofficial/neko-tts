"""Neko TTS: voice is an explicit Planner choice for the current reply only."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from maibot_sdk import (CONFIG_RELOAD_SCOPE_SELF, Command, Field, HookHandler,
                        MaiBotPlugin, PluginConfigBase, Tool)
from maibot_sdk.types import (ErrorPolicy, HookMode, HookOrder,
                              ToolParameterInfo, ToolParamType)


class PluginConfig(PluginConfigBase):
    enabled: bool = Field(default=False, description="启用 Neko TTS")
    config_version: str = Field(default="0.1.0", description="配置版本")


class GeneralConfig(PluginConfigBase):
    timeout: int = Field(default=120, ge=5, le=300)
    max_text_length: int = Field(default=500, ge=1, le=2000)


class VoiceConfig(PluginConfigBase):
    voice_dir: str = Field(default="", description="参考音频目录")
    preferred_reference_file: str = Field(default="", description="固定参考音频文件名")
    clone_prompt: str = Field(default="保持参考音频中的音色、年龄感和说话节奏。自然、清晰、松弛地说话，不要夸张表演，不要添加额外语气词。")
    sample_rate: int = Field(default=24000, ge=8000, le=48000)
    max_clip_duration: float = Field(default=15.0, ge=3.0, le=30.0)
    ffmpeg_path: str = Field(default="")


class MimoConfig(PluginConfigBase):
    api_key: str = Field(default="")
    api_base_url: str = Field(default="https://api.xiaomimimo.com/v1")
    model: str = Field(default="mimo-v2.5-tts-voiceclone")
    audio_format: str = Field(default="mp3")


class CommandConfig(PluginConfigBase):
    allowed_user_ids: List[str] = Field(default_factory=list)


class Config(PluginConfigBase):
    plugin: PluginConfig = Field(default_factory=PluginConfig)
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    mimo: MimoConfig = Field(default_factory=MimoConfig)
    command: CommandConfig = Field(default_factory=CommandConfig)


class NekoTTS(MaiBotPlugin):
    config_model = Config

    def __init__(self) -> None:
        super().__init__()
        self._session: Optional[aiohttp.ClientSession] = None
        self._reference_uri = ""
        self._reference_signature = ""
        self._reference_lock = asyncio.Lock()
        self._synthesis_lock = asyncio.Lock()
        self._voice_intents: dict[str, tuple[float, str]] = {}

    async def on_load(self) -> None:
        if not self.config.plugin.enabled:
            self.ctx.logger.info("Neko TTS 已禁用")
            return
        self.ctx.logger.info("Neko TTS 已加载：Planner 主动选择语音")

    async def on_unload(self) -> None:
        await self._close_session()
        self._voice_intents.clear()
        self._reference_uri = ""
        self._reference_signature = ""

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        async with self._synthesis_lock:
            await self._close_session()
            self._reference_uri = ""
            self._reference_signature = ""
            self._voice_intents.clear()

    async def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.config.general.timeout)
            )
        return self._session

    def _audio_files(self) -> list[Path]:
        root = Path(self.config.voice.voice_dir.strip()).expanduser()
        if not root.is_dir():
            raise ValueError(f"参考音频目录不存在：{root}")
        files = sorted(
            p for p in root.iterdir()
            if p.is_file() and p.suffix.lower() in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus"}
        )
        if not files:
            raise ValueError("参考音频目录没有可用音频")
        preferred = self.config.voice.preferred_reference_file.strip()
        if preferred:
            selected = root / preferred
            if not selected.is_file():
                raise ValueError(f"指定参考音频不存在：{selected}")
            return [selected]
        return files

    def _ffmpeg(self) -> str:
        path = self.config.voice.ffmpeg_path.strip() or shutil.which("ffmpeg")
        if not path:
            raise RuntimeError("未找到 ffmpeg，无法准备参考音频")
        return path

    async def _ensure_reference_uri(self) -> str:
        async with self._reference_lock:
            files = self._audio_files()
            signature = "|".join(f"{p}:{p.stat().st_size}:{p.stat().st_mtime_ns}" for p in files)
            signature += f"|{self.config.voice.preferred_reference_file}|{self.config.voice.max_clip_duration}|{self.config.voice.sample_rate}"
            digest = hashlib.sha256(signature.encode()).hexdigest()
            if self._reference_uri and digest == self._reference_signature:
                return self._reference_uri
            assert self.ctx.paths.runtime_dir is not None
            output = self.ctx.paths.runtime_dir / f"neko-reference-{digest}.wav"
            if not output.exists():
                selected = files[0]
                command = [self._ffmpeg(), "-y", "-i", str(selected), "-vn",
                           "-af", "silenceremove=start_periods=1:start_duration=0.15:start_threshold=-45dB:stop_periods=1:stop_duration=0.35:stop_threshold=-45dB",
                           "-t", str(self.config.voice.max_clip_duration), "-ac", "1",
                           "-ar", str(self.config.voice.sample_rate), "-c:a", "pcm_s16le", str(output)]
                await asyncio.to_thread(subprocess.run, command, check=True,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        timeout=60)
            data = await asyncio.to_thread(output.read_bytes)
            if len(data) < 1000:
                raise RuntimeError("参考音频过短")
            if len(data) > 9 * 1024 * 1024:
                raise RuntimeError("参考音频超过 MiMo 安全大小上限")
            self._reference_uri = "data:audio/wav;base64," + base64.b64encode(data).decode("ascii")
            self._reference_signature = digest
            return self._reference_uri

    def _endpoint(self) -> str:
        base = self.config.mimo.api_base_url.strip().rstrip("/")
        return base if base.endswith("/chat/completions") else base + "/chat/completions"

    def _clean_text(self, text: str) -> str:
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"\[CQ:[^\]]+\]", "", text)
        return text.strip()[: self.config.general.max_text_length]

    async def _synthesize(self, text: str, style: str) -> bytes:
        clean = self._clean_text(text)
        if not clean:
            raise ValueError("没有可朗读文本")
        if not self.config.mimo.api_key.strip():
            raise ValueError("未配置 MiMo API Key")
        async with self._synthesis_lock:
            prompt = style.strip() or self.config.voice.clone_prompt.strip()
            body = {
                "model": self.config.mimo.model.strip() or "mimo-v2.5-tts-voiceclone",
                "messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": clean}],
                "audio": {"format": self.config.mimo.audio_format, "voice": await self._ensure_reference_uri()},
            }
            session = await self._get_session()
            async with session.post(self._endpoint(), json=body, headers={"api-key": self.config.mimo.api_key, "Content-Type": "application/json"}) as response:
                raw = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"MiMo API HTTP {response.status}: {raw[:500]}")
            data = json.loads(raw)["choices"][0]["message"]["audio"]["data"]
            audio = base64.b64decode(data, validate=True)
            if len(audio) < 100:
                raise RuntimeError("MiMo 返回音频过短")
            return audio

    @HookHandler("maisaka.planner.before_request", name="neko_tts_planner_mode", mode=HookMode.BLOCKING,
                 order=HookOrder.LATE, error_policy=ErrorPolicy.SKIP)
    async def planner_hint(self, **kwargs: Any) -> Dict[str, Any]:
        instruction = ("本轮默认只发文字。若你觉得这条回复适合用语音表达，主动调用 neko_voice_reply，"
                       "可填写语气提示；调用后再调用 reply。若不想发语音，直接调用 reply。"
                       "不要为了完成任务而调用语音工具。")
        items = kwargs.get("items")
        if isinstance(items, list) and not any(p.get("text") == instruction for i in items if isinstance(i, dict) for p in i.get("parts", []) if isinstance(p, dict)):
            items.append({"item_type": "SystemMessageItem", "meta": {"item_id": uuid.uuid4().hex, "logical_turn_id": None, "timestamp": datetime.now().isoformat()}, "parts": [{"type": "text", "text": instruction}]})
            kwargs["items"] = items
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool("neko_voice_reply", brief_description="让本轮回复额外发送语音，可指定语气；默认不发语音。",
          parameters=[ToolParameterInfo(name="style", param_type=ToolParamType.STRING, description="本轮语音语气，例如温柔、撒娇、轻声、兴奋；留空使用默认语气", required=False, default="")])
    async def request_voice(self, style: str = "", **kwargs: Any) -> Dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "Neko TTS 未启用"}
        stream = str(kwargs.get("stream_id") or kwargs.get("session_id") or "").strip()
        if not stream:
            return {"success": False, "content": "无法获取当前聊天流"}
        self._voice_intents[stream] = (time.monotonic() + 30.0, style.strip()[:240])
        return {"success": True, "content": "本轮已登记语音意图。现在调用 reply；语音会在文字发送成功后补发。"}

    @HookHandler("send_service.after_send", name="neko_tts_after_send", mode=HookMode.BLOCKING,
                 order=HookOrder.LATE, timeout_ms=300000, error_policy=ErrorPolicy.SKIP)
    async def after_send(self, message: Optional[Dict[str, Any]] = None, **kwargs: Any) -> None:
        if not self.config.plugin.enabled or not kwargs.get("sent"):
            return None
        stream = str(kwargs.get("stream_id") or (message or {}).get("session_id") or "").strip()
        intent = self._voice_intents.pop(stream, None)
        if not intent or intent[0] < time.monotonic():
            return None
        text = " ".join(str(x.get("data", "")) for x in (message or {}).get("raw_message", []) if isinstance(x, dict) and x.get("type") == "text").strip()
        try:
            audio = await self._synthesize(text, intent[1])
            await self.ctx.send.custom("voice", base64.b64encode(audio).decode("ascii"), stream, processed_plain_text=text, sync_to_maisaka_history=False, maisaka_source_kind="neko_tts")
        except Exception as exc:
            self.ctx.logger.error("Neko TTS 补发语音失败：%s", exc)

    @Command("neko_tts_cmd", description="手动生成 Neko TTS 语音", pattern=r"^/neko-tts\s+(?P<text>.+)$")
    async def command(self, stream_id: str = "", user_id: str = "", matched_groups: Optional[Dict[str, Any]] = None, **kwargs: Any):
        if str(user_id) not in {str(x) for x in self.config.command.allowed_user_ids}:
            return False, "测试命令未开放", True
        audio = await self._synthesize(str((matched_groups or {}).get("text") or ""), "")
        sent = await self.ctx.send.custom("voice", base64.b64encode(audio).decode("ascii"), stream_id, processed_plain_text="", sync_to_maisaka_history=False, maisaka_source_kind="neko_tts")
        return bool(sent), "语音已发送" if sent else "语音发送失败", True


def create_plugin() -> NekoTTS:
    return NekoTTS()
