# Neko TTS 0.7.0

0.7.0修复先文字后语音的关联：只消费宿主精确的回复发送回执，不再按相同文字猜测。固定提示、现有随机概率、主动选择和逐宿主消息合成不变。

独立的 MaiBot 语音插件。固定合成提示，支持仅主动选择或**随机＋主动选择混合模式**，没有“默认语音、文字例外”。支持 MiMo 音色克隆与预置音色。

## 安装

0.7.0需要 MaiBot **1.3.11+并具备下述精确回执本地补丁**、SDK **2.10.0+**、Python 3.12+、aiohttp 3.12+。宿主注册组件时会校验Hook是否存在，因此即使暂用同步模式，未修改的上游宿主也不能直接安装0.7.0；无补丁请保留0.6.5，不绕过Hook校验。音色克隆及多段音频合并需要 FFmpeg（PATH 或配置指定）。

```sh
git clone https://github.com/znzsofficial/neko-tts.git plugins/neko-tts
```

复制 `config.example.toml` 为 `config.toml`，填 MiMo 密钥，在 WebUI 启用。默认关闭。启用前停用 `ling.tts-bot` 等自动出站 TTS，防止双重合成。无需依赖旧插件、无需复制它的代码或缓存。

## 麦麦怎样决定

使用 1.3.5 原生 `ReplyExtension`。Planner 的 `reply` 自动出现可选参数，例如：

```json
{"plugin_options":{"neko.tts.voice":{}}}
```

这只是 `reply` 的扩展部分，其余必填参数照常填写。`trigger.mode=planner`（默认）仅在主动选择时合成。`trigger.mode=hybrid` 每条 reply 按 `trigger.probability`（默认0.3）随机；用户要求语音时 Planner 仍可主动选择，不受概率限制。随机与主动都使用同一个 ReplyExtension，不会叠加两遍合成。只是引导 Planner 正确选择工具，不保证模型每次都遵循用户要求。

混合模式在 `maisaka.planner.after_response` 对未主动选择语音的 reply 调用补充扩展参数，不改正文或其他工具。按 reply 抽样，不是每个断句单独抽样；抽中后分别朗读宿主断句后的各条文字消息，无聊天级概率缓存。概率0为仅主动选择，概率1为每条 reply 都选语音。异常或不支持的 Hook 载荷跳过随机路径。扩展参数只接受空对象 `{}`。

主动选择和随机选择均绑定当前 reply；后台模式的关联限制见下文。

## 麦麦的上下文

0.6.4补齐Replyer的另一种真实历史格式：宿主把guided_reply自身历史重新构造成无消息ID/前缀的AssistantMessageItem。仅过滤这种assistant单一文本part且正文恰为`[语音消息]`的空占位；用户同名占位、混合内容、转写正文和对语音的讨论保留。此处身份依赖宿主assistant角色，无法区分bot刻意发出完全同名的纯文字；不做任意正文替换。0.6.3工具回执清理保留。

0.6.3补齐另一条漏出的路径：宿主会把扩展语音的`[语音消息]`拼到`reply`工具成功回执正文末尾，0.6.2仅过滤独立语音历史，因此仍会看见这些标记。现在按聊天ID＋call_id＋本次追加语音段数，仅清理已登记调用的宿主成功回执末尾对应标记；不删除工具调用/结果，不全局替换用户或原回复中的同名字样。voice_only的纯占位回执改为“回复已发送。”。段数元数据持久存于data_dir/reply-receipts.json（4096条/2MiB/7天、0600原子写，无正文/音频），损坏不覆盖；未登记旧调用、过期回执或宿主格式变化保守保留。已有可核实旧记录可按工具记录及音频发送ID回填段数，不按正文猜测。

这不是让模型完全不知道语音能力：扩展说明、主动选择的plugin_options、用户提及语音和此前已形成的记忆仍可能存在；过滤只针对本插件独立语音及工具回执中的冗余语音占位。切回纯随机不等同自动隐藏历史，仍需检查发送路径。

0.6.1起，TTS实际合成的音频哈希与成功发送回执关联，记下当前聊天的真实消息编号。在Planner和Replyer发起模型请求前，只排除这些语音消息的Context Items；不按正文去重，不删除文字、不屏蔽用户语音或其他插件的有内容消息。旧历史中明确标记自身且仅包含“[语音消息]”的空占位也被过滤。发送路径、断句、固定提示和混合概率保持不变。

宿主仍会存储text_and_voice/voice_only的发送记录，WebUI里可能仍看得到；过滤的是模型输入，不是删除数据库或清空聊天。reply的工具调用记录（包括plugin_options）也不删除。直接测试命令和后台语音关闭storage_message与sync_to_maisaka_history。回执缓存仅聊天ID/消息ID/有效期，无音频或正文；上限4096条/2MiB、保留7天，原子0600写入持久data_dir/voice-receipts.json。0.6.2首次启动从旧runtime_dir缓存迁移且保留旧文件，不覆盖已有持久缓存；损坏或极端数据保留不覆盖。重复回执不消耗其他语音的匹配次数。过期/超容量的有正文旧语音不保证过滤，旧空占位不依赖缓存。宿主前缀/短消息ID别名格式变化需重新适配；当前MaiBot1.3.5链路已验证。

## 发送与失败

| `output.mode` | 行为 |
| --- | --- |
| `text_and_voice`（默认） | 每条宿主断句后的文字消息单独合成；全部成功后先按原顺序发文字/附件，再依次发各段独立语音。不把音频插在文字中间，避免改变原引用链。 |
| `text_then_voice`（需宿主精确回执支持） | 立即交回原文字/附件供宿主发送；整组发送结束后，仅为有明确成功回执的文字后台合成语音，不阻塞下一轮文字。 |
| `voice_only` | 合成成功后以对应多条语音替换整组消息，文字和附件都不发送；任意一段合成失败保留原回复。 |

语音不带引用，避免 QQ 不投递“引用＋语音”的组合。`text_and_voice` 会增加文字发送前的等待时间；两种文字＋语音模式在客户端都表现为文字先到、语音后到。宿主或适配器确认成功不代表 QQ 最终投递保证。

后台模式要求宿主只读Hook `maisaka.reply.after_send`，参数为session_id、reply_id、call_id及send_results；每项含index、严格bool success、非空平台message_id和实际发送的纯文字text。当前由MaiBot1.3.11本地补丁`neko-reply-latency-v1`提供，不是未修改上游的保证；缺Hook或标识时不回退旧文字匹配、不猜图或语音。完整宿主补丁维护在vps-panel的`scripts/maibot_reply_latency_patch.py`，宿主升级须先核对/移植。其它两种模式沿用原实现。

登记按聊天＋reply_id＋call_id绑定，仅朗读原扩展中的文字消息序号；使用最终发送正文，排除at/引用/附件。部分发送失败只朗读已确认成功的项；未知/缺ID/损坏回执不合成。重复文字的不同reply不混淆，同平台消息或重复回执最多消费一次。待确认最多32条/5分钟、已消费与消息ID去重各4096条/1小时，不存正文/音频、不落盘；重载清空后不恢复或重放。后台队列最多8批，单worker按确认先后串行，每条宿主文字独立合成发送；general.timeout含整批排队/合成/发送等待，过期/超限/队满直接舍弃语音保留文字，不重试未知投递。失败可能已发前几条语音，不会倒退或重发；仅保证调用顺序，不承诺QQ最终到达顺序。

MiMo 返回 429/502/503/504 时最多退避重试两次；第一次请求 `finish_reason=stop` 但缺少音频时最多重试一次。均受整轮超时限制，重试不修改正文或提示词。内容拦截、截断结果、未知结构、鉴权失败和网络超时不自动重试。平台发送失败不自动重试。

合成失败或超过文本限制会保留原回复。日志对 MiMo 错误记录安全的状态原因（如 `HTTP 429`、`missing_audio`），其他异常只记录类型，不记录密钥、聊天正文或原始响应。后台平台发送返回失败也会记录日志。

单段音频最多8MiB，整组扩展结果JSON最多12MiB，为宿主16MiB RPC帧预留开销；超过时回退原回复，不交给宿主发送无法传输的语音组。命令和后台路径也受单段限制。

## 合成配置

固定提示默认为“用原本的音色和语气说话，保持自然流畅”，原样发送，不追加麦麦的语气或模板。提示放在 MiMo 的 user 消息，朗读正文独立放在 assistant 消息，与旧 ling-tts-bot 的固定提示模式一致；参考音频处理仍沿用本插件，不代表两插件的全部音频链路相同。

- `mimo.synthesis_mode=voiceclone`：使用参考目录；`preferred_reference_file` 可将候选限制为固定文件名，适用于所有参考策略。
- `best_single` 自动选择：最多分析32个文件、每文件前60秒，根据有效有声时长选一段；不是说话人识别或主观音质评分。统一单声道 WAV，裁首尾静音并限制长度，保留片段内停顿。原始文件不修改；参考仅缓存内存。
- `mimo.synthesis_mode=preset`：无需参考音频。音色支持冰糖、茉莉、苏打、白桦、Mia、Chloe、Milo、Dean、mimo_default。
- `voice.reference_strategy=balanced` / `full_merge`：按文件名排序取最多32个文件，解码统一单声道和采样率后拼接。`balanced` 每个输入最多取 `max_clip_duration`，`full_merge` 不做这个逐段裁剪；两者最终都只保留拼接开头的 `max_clip_duration` 秒。当前实现不保证每个文件都进入最终参考，前段足够长时后段不会保留；不是全量无损拼接或真正均分时长。各段音色、距离和噪声应尽量一致。
- 模型分别为 `mimo-v2.5-tts-voiceclone` 和 `mimo-v2.5-tts`，鉴权为 `api-key`。
- `general.max_text_length` 是每段上限，按标点切分，长句必要时硬切但不丢字。总长超过 `max_total_length` 或段数超过 `max_segments` 时保留文字，不悄悄截断。
- 宿主断句后的消息边界不再拼接：例如“第一句”“第二句”分别请求、分别发两条语音，不取before_send的整段text。只朗读文字，排除at/引用/图片等。只有同一条文字超长时，为满足API长度限制内部切段并合并；不会跨宿主消息合并音频。临时文件用后清理。
- 总长度/合成段数限制仍按整条reply累计，text_and_voice/voice_only的整轮期限包含所有分段；后段失败或期限耗尽时不发送已准备的部分语音，保留整组原回复。
- `general.timeout` 限制整次合成（包括排队、参考处理和合并）；当前每个插件实例串行合成。

## 手动测试

配置 `command.allowed_user_ids` 白名单后：

- `/neko-tts 你好呀`：合成并发送独立语音。
- `/neko-tts status`：查看模式，不展示密钥。

空名单不开放命令。命令权限与 Planner 自主选择是不同入口。

## 开发与验收

```sh
python -m pip install 'maibot-plugin-sdk==2.10.0' 'aiohttp>=3.12' imageio-ffmpeg tzdata
python scripts/check.py
python scripts/package.py
# 可选：输出到尚不存在的路径，不创建 GitHub Release
python scripts/package.py --output neko-tts.zip
```

测试覆盖真实 SDK 导入、混合触发/主动优先/避免重复/其他工具不变、回复扩展失败回退、命令鉴权、分段保真、固定提示原样发送、模拟 MiMo HTTP、限流退避、缺失音频恢复和重试上限，以及实际 FFmpeg 参考选取和合并。无系统 FFmpeg 时可在测试命令加 `--with imageio-ffmpeg`。

GitHub Actions 在 Ubuntu/Windows × Python 3.12/3.14 × SDK `==2.10.0`/`==2.12.0`/`>=2.12,<3` 共12档运行，
额外保留manifest声明的2.10最低支持档。PyPI包名`maibot-plugin-sdk`，导入名`maibot_sdk`。
门禁要求至少90项、0 skip、0失败；缺真实SDK、缺FFmpeg导致关键测试跳过均失败。
CI显式装`imageio-ffmpeg`作为真实FFmpeg备用（测试优先系统PATH，否则使用其二进制）、`tzdata`统一Windows时区依赖。
FFmpeg测试仅使用临时目录生成的合成音频，不扫描真实参考音频目录；HTTP测试仅本机假服务/Mock，不调用MiMo、付费模型或QQ。
新增未知结果回归确认供应商响应超时不重新请求、命令投递超时不重发、后台已消耗匹配记录不因重复回执重发。
不改变逐宿主消息合成、Planner主动选择、用户语音可见、命令权限、固定提示或概率。

源码ZIP只读取`scripts/package.py`列出的9个文件（含deferred_speech.py）：源码、manifest、许可证、README/CHANGELOG和公开示例配置。
不递归扫描，拒绝符号链接；实际`config.toml`、`.env`、Key、音频、语音/回复回执、缓存及运行数据均不打包。
包边界回归检查私密哨兵和多余成员；CI临时解压并用真实SDK导入、构造插件，不调用`on_load`，不加载宿主服务。
默认产物随临时目录清理，不上传artifact、不创建Release、不部署；仅显式`--output`保留本地ZIP。

2026-10-07 已用线上配置复现缺少 audio 和 HTTP 429，并在修复后对原失败文本完成真实合成；用户反馈暂时正常。接口合成测试与 QQ 最终投递是两项不同的验证。

初版 MiMo 请求与 QQ 独立语音经验参考 MIT 项目 [ling-tts-bot](https://github.com/Ling-LA/ling-tts-bot)，本插件为独立实现与配置，不需要加载它。
