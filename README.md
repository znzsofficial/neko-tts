# Neko TTS

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
{"plugin_options":{"neko.tts.voice":{"style":"轻声温柔，语速稍慢"}}}
```

这只是 `reply` 的扩展部分，其余必填参数照常填写。不填就完全不参与回复；填了才合成。`style` 只用于此次 TTS，不进入文字生成提示，也不保存为全局状态。基础 `clone_prompt` 与此次风格共同传给 MiMo；模型是否准确表现情绪需要试听验证。

初版 `neko_voice_reply` 工具、30秒聊天标记和出站拦截已移除。并发聊天和连续 reply 各用自己的参数，不会抢走另一条回复的语音选择。

## 发送与失败

在宿主发送前完成合成，把一条独立语音追加到原文字和图片之后，再由宿主按顺序发送。**不是文字已经发出才开始合成**，因此选择语音会增加本轮发送等待时间。文字引用与附件保留，语音不带引用，避免 QQ 不投递“引用＋语音”的组合。

合成出错或超出长度限制，返回原回复给宿主发送，日志只记录错误类型。宿主发送阶段的网络失败由宿主处理；不能保证 QQ 已投递。不会自动重试付费合成或重复发送。插件卸载/配置重载会取消本插件的合成任务。

## 合成配置

- `mimo.synthesis_mode=voiceclone`：使用目录内一段参考音频，`preferred_reference_file` 可固定文件名。
- 自动选择：最多分析32个文件、每文件前60秒，根据有效有声时长选一段；不是说话人识别或主观音质评分。统一单声道 WAV，裁首尾静音并限制长度，保留片段内停顿。原始文件不修改；参考仅缓存内存。
- `mimo.synthesis_mode=preset`：无需参考音频。音色支持冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo、Dean、mimo_default。
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

15项测试包含真实 SDK 导入、回复扩展参数隔离/失败回退、命令鉴权、分段保真、本地模拟 MiMo HTTP 接口，以及真实 FFmpeg 参考选取和合并。无系统 FFmpeg 时可在测试命令加 `--with imageio-ffmpeg`。尚未做付费 MiMo 试听和线上 QQ 全链路验收。

初版 MiMo 请求与 QQ 独立语音经验参考 MIT 项目 [ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)，本插件为独立实现与配置，不需要加载它。
