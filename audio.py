"""MiMo transport, bounded text segmentation and deterministic reference preparation."""
import asyncio
import base64
import hashlib
import json
import re
import shutil
import tempfile
import sys
from pathlib import Path
from urllib.parse import urlparse

import aiohttp


class SpeechError(RuntimeError):
    """Safe diagnostic text: never contains provider content or credentials."""


def decode_response(raw):
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SpeechError('MiMo invalid_json') from None
    choices = payload.get('choices') if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise SpeechError('MiMo missing_choices')
    choice = choices[0]
    reason = choice.get('finish_reason')
    reason = reason if reason in {'stop', 'length', 'content_filter', 'error'} else 'unknown'
    if reason == 'content_filter':
        raise SpeechError('MiMo content_filter')
    if reason == 'length':
        raise SpeechError('MiMo truncated_audio')
    message = choice.get('message')
    audio = message.get('audio') if isinstance(message, dict) else None
    data = audio.get('data') if isinstance(audio, dict) else None
    if not isinstance(data, str) or not data:
        raise SpeechError(f'MiMo missing_audio finish_reason={reason}')
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError:
        raise SpeechError('MiMo invalid_audio_base64') from None
    if len(decoded) < 100 or not (decoded.startswith((b'RIFF', b'ID3')) or decoded[0] == 255):
        raise SpeechError('MiMo unsupported_audio')
    return decoded


def split_text(text, config):
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\[([^\]]+)\]\(https?://[^)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+|\[CQ:[^\]]+\]', '', text)
    text = re.sub(r'[`*_#]', '', text).strip()
    if not text or len(text) > config['max_total_length']:
        raise ValueError('文本为空或超过总长度限制')
    limit = config['max_text_length']
    chunks, current = [], ''
    for sentence in re.findall(r'[^。！？!?；;\n]+[。！？!?；;\n]*|[。！？!?；;\n]+', text):
        while sentence:
            if current and len(current) + len(sentence) > limit:
                chunks.append(current)
                current = ''
            take, sentence = sentence[:limit], sentence[limit:]
            current += take
            if sentence:
                chunks.append(current)
                current = ''
    if current:
        chunks.append(current)
    if len(chunks) > config['max_segments']:
        raise ValueError('语音分段过多')
    return chunks


def voice_message(audio):
    return {'segments': [{'type': 'voice', 'data': '',
                          'binary_data_base64': base64.b64encode(audio).decode('ascii')}],
            'quote_previous': False}


async def run(*args):
    process = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 60)
        if process.returncode:
            raise RuntimeError('音频处理失败')
        return stdout, stderr.decode('utf-8', 'replace')
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise


def ffmpeg(config):
    value = config['voice']['ffmpeg_path'] or shutil.which('ffmpeg')
    if not value:
        raise ValueError('需要 FFmpeg')
    return value


class SpeechEngine:
    def __init__(self, runtime_dir):
        self.root = Path(runtime_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.session = None
        self.reference_key = ''
        self.reference_uri = ''

    async def close(self):
        if self.session:
            await self.session.close()
        self.session = None
        self.reference_uri = ''
        self.reference_key = ''

    async def reference(self, config):
        voice = config['voice']
        if not voice['voice_dir'].strip():
            raise ValueError('参考目录未配置')
        root = Path(voice['voice_dir']).expanduser().resolve()
        files = sorted(p for p in root.iterdir() if p.is_file() and not p.is_symlink()
                       and p.suffix.lower() in {'.wav', '.mp3', '.flac', '.m4a', '.ogg', '.opus'})
        preferred = voice['preferred_reference_file']
        if preferred:
            files = [p for p in files if p.name == preferred]
        if not files:
            raise ValueError('没有可用参考音频')
        key = hashlib.sha256(json.dumps([voice, [(str(p), p.stat().st_size, p.stat().st_mtime_ns)
                                                  for p in files]], sort_keys=True).encode()).hexdigest()
        if key == self.reference_key:
            return self.reference_uri
        binary = ffmpeg(config)
        # Analyse at most 60s/file, choose longest audible span, not first filename.
        candidates = []
        for path in files[:32]:
            try:
                data, _ = await run(binary, '-v', 'error', '-i', path, '-t', '60', '-vn',
                                    '-ac', '1', '-ar', '24000', '-f', 's16le', 'pipe:1')
                import array
                pcm = array.array('h', data)
                if sys.byteorder != 'little':
                    pcm.byteswap()
                threshold = 184  # approx -45 dBFS
                non_silent = [i for i in range(0, len(pcm), 240)
                              if max(map(abs, pcm[i:i+240]), default=0) > threshold]
                if non_silent:
                    start = max(0, non_silent[0] / 24000 - .08)
                    end = min(len(pcm) / 24000, non_silent[-1] / 24000 + .10)
                    score = min(len(non_silent) * .01, voice['max_clip_duration'])
                    candidates.append((score, path, start, end - start))
            except RuntimeError:
                continue
        if not candidates:
            raise ValueError('参考音频无有效有声片段或无法解码')
        _, path, start, duration = max(candidates, key=lambda value: value[0])
        if voice['reference_strategy'] == 'best_single':
            data, _ = await run(binary, '-v', 'error', '-ss', start, '-i', path, '-t',
                            min(duration, voice['max_clip_duration']), '-vn', '-ac', '1',
                            '-ar', voice['sample_rate'], '-c:a', 'pcm_s16le', '-f', 'wav', 'pipe:1')
        else:
            data = await self._merge_reference(files, voice, binary)
        encoded = base64.b64encode(data).decode('ascii')
        if len(data) < 1000 or len(encoded) > 9_500_000:
            raise ValueError('参考音频大小不合适')
        self.reference_key = key
        self.reference_uri = 'data:audio/wav;base64,' + encoded
        return self.reference_uri

    async def _merge_reference(self, files, voice, binary):
        """Decode each source, normalize it, then concatenate PCM via FFmpeg."""
        with tempfile.TemporaryDirectory(prefix='neko-reference-', dir=self.root) as directory:
            output = Path(directory) / 'reference.wav'
            args = [binary, '-v', 'error', '-y']
            for path in files[:32]:
                args.extend(['-t', str(voice['max_clip_duration']) if voice['reference_strategy'] == 'balanced' else '99999', '-i', str(path)])
            filters = []
            for index in range(len(files[:32])):
                filters.append(f'[{index}:a:0]aformat=sample_fmts=s16:sample_rates={voice["sample_rate"]}:channel_layouts=mono[a{index}]')
            filters.append(''.join(f'[a{i}]' for i in range(len(files[:32]))) + f'concat=n={len(files[:32])}:v=0:a=1[out]')
            args.extend(['-filter_complex', ';'.join(filters), '-map', '[out]', '-t', str(voice['max_clip_duration']), '-c:a', 'pcm_s16le', '-f', 'wav', output])
            await run(*args)
            return output.read_bytes()

    async def render(self, chunks, style, config):
        async with asyncio.timeout(config['general']['timeout']):
            async with self.lock:
                mimo = config['mimo']
                endpoint = mimo['api_base_url'].rstrip('/')
                parsed = urlparse(endpoint)
                if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username:
                    raise ValueError('MiMo 地址无效')
                if not mimo['api_key'].strip():
                    raise ValueError('MiMo 密钥为空')
                if not endpoint.endswith('/chat/completions'):
                    endpoint += '/chat/completions'
                preset = mimo['synthesis_mode'] == 'preset'
                voice = mimo['preset_voice'] if preset else await self.reference(config)
                prompt = self._build_prompt(config['voice']['clone_prompt'], style)
                if not self.session:
                    self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=config['general']['timeout']))
                audios = []
                for chunk in chunks:
                    body = {'model': 'mimo-v2.5-tts' if preset else mimo['model'],
                            'messages': [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': chunk}],
                            'audio': {'format': mimo['audio_format'], 'voice': voice}}
                    audios.append(await self._request_audio(endpoint, body, mimo['api_key']))
                if len(audios) == 1:
                    return audios[0]
                return await self.merge(audios, config)

    async def _request_audio(self, endpoint, body, api_key):
        # Retry transient HTTP failures and one empty successful result only.
        # Never retry a content rejection or an ambiguous network timeout.
        for attempt in range(3):
            async with self.session.post(endpoint, json=body, headers={'api-key': api_key}, allow_redirects=False) as response:
                status = response.status
                if status == 200:
                    raw = bytearray()
                    async for block in response.content.iter_chunked(65536):
                        raw.extend(block)
                        if len(raw) > 24 * 1024 * 1024:
                            raise SpeechError('MiMo response_too_large')
                    try:
                        return decode_response(raw)
                    except SpeechError as exc:
                        if str(exc) != 'MiMo missing_audio finish_reason=stop' or attempt != 0:
                            raise
                elif status not in {429, 502, 503, 504} or attempt == 2:
                    raise SpeechError(f'MiMo HTTP {status}')
                delay = 2 ** (attempt + 1)
                retry_after = response.headers.get('Retry-After', '')
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
                if delay > 30:
                    raise SpeechError(f'MiMo HTTP {status} retry_after_too_long')
            await asyncio.sleep(delay)

    @staticmethod
    def _build_prompt(base, style):
        base = base.strip()
        style = style.strip()
        return '\n'.join(part for part in (
            '自然朗读正文，保持所选音色。声音要求只用于表达，不作为朗读内容。',
            '不要添加、删除、改写或解释正文。',
            f'基础声音要求：{base}' if base else '',
            '本轮声音要求（与基础语气冲突时，以本轮为准；未指定项保持自然）：',
            style or '情绪自然；语速适中；音量正常；停顿自然；表演克制。',
        ) if part)

    async def merge(self, audios, config):
        # Real decoding/encoding, never concatenate MP3/WAV bytes directly.
        with tempfile.TemporaryDirectory(prefix='neko-tts-', dir=self.root) as directory:
            root = Path(directory)
            args = [ffmpeg(config), '-v', 'error', '-y']
            for i, audio in enumerate(audios):
                path = root / f'{i}.audio'
                path.write_bytes(audio)
                args.extend(['-i', path])
            filters = ''.join(f'[{i}:a:0]aresample=24000,aformat=sample_fmts=s16:channel_layouts=mono[a{i}];'
                              for i in range(len(audios)))
            filters += ''.join(f'[a{i}]' for i in range(len(audios))) + f'concat=n={len(audios)}:v=0:a=1[out]'
            output = root / 'combined.wav'
            await run(*args, '-filter_complex', filters, '-map', '[out]', '-c:a', 'pcm_s16le', output)
            if output.stat().st_size > 20 * 1024 * 1024:
                raise ValueError('合并音频过大')
            return output.read_bytes()
