# neko-tts

独立的 MaiBot MiMo TTS 插件。默认只发文字；Planner 觉得本轮适合语音时调用 `neko_voice_reply`，再调用 `reply`，文字发送成功后插件补发语音。

`style` 只对当前回复生效，发送后立即清除。需要在确认新插件稳定后停用 `ling.tts-bot`，避免两个插件同时补发语音。
