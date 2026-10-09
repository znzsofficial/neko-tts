# Neko TTS 0.4.1

独立的 MaiBot 语音插件。**默认文字，麦麦想用语音表达时才主动选择**，没有随机概率，也没有“默认语音、文字例外”。支持 MiMo 音色克隆与预置音色。

## 安装

需要 MaiBot **1.3.5+**、SDK **2.10.0+**、Python 3.12+、aiohttp 3.12+。音色克隆及多段音频合并需要 FFmpeg（PATH 或配置指定）。

```sh
git clone https://github.com/znzsofficial/neko-tts.git plugins/neko-tts
```

复制 `config.example.toml` 为 `config.toml`，填 MiMo 密钥，在 WebUI 启用。默认关闭。启用前停用 `ling.tts-bot` 等自动出站 TTS，防止双重合成。无需依赖旧插件、无需复制它的代码或缓存。

## 麦麦怎样决定

使用 1.3.5 原生 `ReplyExtension`。Planner 的 `reply` 自动出现可选参数，例如：

```json
{"plugin_options":{"neko.tts.voice":{"style":"温柔安慰"}}}
```

这只是 `reply` 的扩展部分，其余必填参数照常填写。不填就完全不参与回复；填了才合成。`style` 只用于此次 TTS，不进入文字生成提示，也不保存为全局状态。固定 `clone_prompt` 为基础，本轮只补充情绪语气；模型是否准确表现情绪需要试听验证。

初版 `neko_voice_reply` 工具已移除。`text_and_voice` / `voice_only` 的语气参数直接绑定当前 reply；后台模式的关联限制见下文。

## 发送与失败

| `output.mode` | 行为 |
| --- | --- |
| `text_and_voice`（默认、线上使用） | 先合成，再让宿主依次发送原文字、附件、独立语音；并非同时显示。 |
| `text_then_voice`（实验选项） | 原回复先发送，匹配成功回执后在后台合成、补发独立语音。 |
| `voice_only` | 合成成功后用一条语音替换整组消息，文字和附件都不发送；合成失败保留原回复。 |

语音不带引用，避免 QQ 不投递“引用＋语音”的组合。`text_and_voice` 会增加文字发送前的等待时间；两种文字＋语音模式在客户端都表现为文字先到、语音后到。宿主或适配器确认成功不代表 QQ 最终投递保证。

后台模式当前按聊天 ID＋完整文字匹配回执，标记有效期 180 秒，不是 reply ID 级关联。重复文字或宿主分段可能造成误匹配或漏匹配，日常建议 `text_and_voice`。插件卸载/配置重载会取消后台任务。

MiMo 返回 429/502/503/504 时最多退避重试两次；第一次请求 `finish_reason=stop` 但缺少音频时最多重试一次。均受整轮超时限制，重试不修改正文或提示词。内容拦截、截断结果、未知结构、鉴权失败和网络超时不自动重试。平台发送失败不自动重试。

合成失败或超过文本限制会保留原回复。日志对 MiMo 错误记录安全的状态原因（如 `HTTP 429`、`missing_audio`），其他异常只记录类型，不记录密钥、聊天正文或原始响应。后台平台发送返回失败也会记录日志。

## 合成配置

固定提示默认为“用原本的音色和语气说话，保持自然流畅”。不传 `style` 时原样发送固定提示，不添加通用朗读模板。本轮 `style` 只补充情绪语气，例如“温柔安慰”“开心”“略带委屈”，不覆盖固定基础、不引导改变音色、年龄、语速、音量或停顿。提示放在 MiMo 的 user 消息，朗读正文独立放在 assistant 消息。实际情绪表现仍需试听确认；这属于提示约束，不是对自由文本 style 的强制语义过滤。

- `mimo.synthesis_mode=voiceclone`：使用参考目录；`preferred_reference_file` 可将候选限制为固定文件名，适用于所有参考策略。
- `best_single` 自动选择：最多分析32个文件、每文件前60秒，根据有效有声时长选一段；不是说话人识别或主观音质评分。统一单声道 WAV，裁首尾静音并限制长度，保留片段内停顿。原始文件不修改；参考仅缓存内存。
- `mimo.synthesis_mode=preset`：无需参考音频。音色支持冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo、Dean、mimo_default。
- `voice.reference_strategy=balanced` / `full_merge`：按文件名排序取最多32个文件，解码统一单声道和采样率后拼接。`balanced` 每个输入最多取 `max_clip_duration`，`full_merge` 不做这个逐段裁剪；两者最终都只保留拼接开头的 `max_clip_duration` 秒。当前实现不保证每个文件都进入最终参考，前段足够长时后段不会保留；不是全量无损拼接或真正均分时长。各段音色、距离和噪声应尽量一致。
- 模型分别为 `mimo-v2.5-tts-voiceclone` 和 `mimo-v2.5-tts`，鉴权为 `api-key`。
- `general.max_text_length` 是每段上限，按标点切分，长句必要时硬切但不丢字。总长超过 `max_total_length` 或段数超过 `max_segments` 时保留文字，不悄悄截断。
- 多段按顺序合成，再经 FFmpeg 解码合并为一条 WAV，避免连发多条或直接拼接文件字节。临时文件用后清理。
- `general.timeout` 限制整次合成（包括排队、参考处理和合并）；当前每个插件实例串行合成。

## 手动测试

配置 `command.allowed_user_ids` 白名单后：

- `/neko-tts 你好呀`：合成并发送独立语音。
- `/neko-tts status`：查看模式，不展示密钥。

空名单不开放命令。命令权限与 Planner 自主选择是不同入口。

## 开发与验收

```sh
uv run --no-project --with maibot-plugin-sdk==2.10.0 --with aiohttp python -m unittest -q
```

24 项测试覆盖真实 SDK 导入、回复扩展参数隔离/失败回退、命令鉴权、分段保真、固定提示原样发送/情绪补充约束、模拟 MiMo HTTP、限流退避、缺失音频恢复和重试上限，以及实际 FFmpeg 参考选取和合并。无系统 FFmpeg 时可在测试命令加 `--with imageio-ffmpeg`。

2026-10-07 已用线上配置复现缺少 audio 和 HTTP 429，并在修复后对原失败文本完成真实合成；用户反馈暂时正常。接口合成测试与 QQ 最终投递是两项不同的验证。

初版 MiMo 请求与 QQ 独立语音经验参考 MIT 项目 [ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)，本插件为独立实现与配置，不需要加载它。
