import base64
import io
import json
import math
import struct
import tempfile
import wave
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from audio import SpeechEngine, SpeechError, decode_response
from plugin import Config


def pcm_wav():
    """Real, generated mono PCM WAV: no private samples or codec dependency."""
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(24000)
        file.writeframes(b''.join(struct.pack('<h', int(4000 * math.sin(i * math.tau * 440 / 24000)))
                                  for i in range(2400)))
    return stream.getvalue()


def audio_response(reason='stop'):
    return json.dumps({'choices': [{'finish_reason': reason, 'message': {'audio': {
        'data': base64.b64encode(pcm_wav()).decode('ascii')}}}]}).encode()


class DecodeTests(TestCase):
    def test_success_and_legacy_missing_reason_accept_real_pcm_wav(self):
        for reason in ('stop', None):
            with self.subTest(reason=reason):
                audio = decode_response(audio_response(reason))
                with wave.open(io.BytesIO(audio), 'rb') as file:
                    self.assertEqual((file.getnchannels(), file.getsampwidth(), file.getframerate(), file.getnframes()),
                                     (1, 2, 24000, 2400))
        payload = json.loads(audio_response())
        del payload['choices'][0]['finish_reason']
        self.assertEqual(decode_response(json.dumps(payload)), pcm_wav())

    def test_failure_reasons_reject_even_valid_residual_pcm(self):
        for reason, expected in [('error', 'synthesis_error'), ('length', 'truncated_audio'),
                                 ('content_filter', 'content_filter')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(SpeechError, '^MiMo ' + expected + '$'):
                decode_response(audio_response(reason))

    def test_unknown_explicit_reasons_fail_closed_without_leaking_content(self):
        for reason in ('private provider diagnostic', '', 'tool_calls', [], {}, 0, False):
            with self.subTest(reason=reason), self.assertRaisesRegex(SpeechError, '^MiMo unknown_finish_reason$'):
                decode_response(audio_response(reason))


class TransportTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = SpeechEngine(self.temp.name)
        self.config = Config().model_dump()
        self.config['mimo'].update(synthesis_mode='preset', api_key='test')
        self.calls = []
        self.status = 200
        self.invalid = False
        self.payload = None
        self.empty_once = False
        self.statuses = []
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as file:
            file.setnchannels(1); file.setsampwidth(2); file.setframerate(24000)
            file.writeframes(b'\0\0' * 2400)
        self.audio = stream.getvalue()
        async def handler(request):
            self.calls.append((await request.json(), request.headers.get('api-key')))
            status = self.statuses.pop(0) if self.statuses else self.status
            if status != 200:
                return web.Response(status=status, text='private provider diagnostic')
            if self.payload is not None:
                return web.json_response(self.payload)
            if self.empty_once:
                self.empty_once = False
                return web.json_response({'choices': [{'finish_reason': 'stop', 'message': {}}]})
            return web.json_response({'choices': [{'message': {'audio': {
                'data': 'invalid' if self.invalid else base64.b64encode(self.audio).decode()}}}]})
        app = web.Application()
        app.router.add_post('/v1/chat/completions', handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.config['mimo']['api_base_url'] = f'http://127.0.0.1:{port}/v1'

    async def asyncTearDown(self):
        await self.engine.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_preset_fixed_prompt(self):
        self.engine.reference = AsyncMock(side_effect=AssertionError('no reference for preset'))
        audio = await self.engine.render(['你好'], self.config)
        self.assertEqual(audio, self.audio)
        body, key = self.calls[0]
        self.assertEqual(key, 'test')
        self.assertEqual(body['model'], 'mimo-v2.5-tts')
        self.assertEqual(body['audio']['voice'], '冰糖')
        self.assertEqual(body['messages'][0]['content'], self.config['voice']['clone_prompt'])
        self.assertEqual(body['messages'][1]['content'], '你好')

    async def test_fixed_prompt_is_verbatim(self):
        base = '用原本的音色和语气说话，保持自然流畅'
        self.assertEqual(self.config['voice']['clone_prompt'], base)
        await self.engine.render(['你好'], self.config)
        self.assertEqual(self.calls[0][0]['messages'][0]['content'], base)

    async def test_custom_fixed_prompt_is_verbatim(self):
        self.config['voice']['clone_prompt'] = '固定声音要求'
        await self.engine.render(['你好'], self.config)
        self.assertEqual(self.calls[0][0]['messages'][0]['content'], '固定声音要求')

    async def test_clone_and_segment_order(self):
        self.config['mimo']['synthesis_mode'] = 'voiceclone'
        self.engine.reference = AsyncMock(return_value='data:audio/wav;base64,ref')
        self.engine.merge = AsyncMock(return_value=b'merged')
        self.assertEqual(await self.engine.render(['第一句', '第二句'], self.config), b'merged')
        self.assertEqual([c[0]['messages'][1]['content'] for c in self.calls], ['第一句', '第二句'])
        self.assertEqual(self.calls[0][0]['audio']['voice'], 'data:audio/wav;base64,ref')

    async def test_errors_are_not_retried_or_leaked(self):
        self.status = 401
        with self.assertRaisesRegex(SpeechError, '^MiMo HTTP 401$'):
            await self.engine.render(['你好'], self.config)
        self.assertEqual(len(self.calls), 1)
        self.status = 200
        self.invalid = True
        with self.assertRaisesRegex(SpeechError, 'invalid_audio_base64'):
            await self.engine.render(['你好'], self.config)

    async def test_transient_retries_keep_body_and_return_audio(self):
        self.statuses = [429, 503, 200]
        with patch('audio.asyncio.sleep', new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.engine.render(['你好'], self.config), self.audio)
        self.assertEqual([c.args[0] for c in sleep.await_args_list], [2, 4])
        self.assertEqual(self.calls, [self.calls[0]] * 3)

    async def test_retry_exhaustion_is_bounded(self):
        self.status = 429
        with patch('audio.asyncio.sleep', new_callable=AsyncMock):
            with self.assertRaisesRegex(SpeechError, '^MiMo HTTP 429$'):
                await self.engine.render(['你好'], self.config)
        self.assertEqual(len(self.calls), 3)

    async def test_ambiguous_provider_timeout_is_not_retried(self):
        # The request may have reached the provider: a lost response is not a
        # transient HTTP rejection and must not trigger another paid request.
        session = Mock()
        session.post.side_effect = TimeoutError('response acknowledgement lost')
        self.engine.session = session
        try:
            with patch('audio.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaises(TimeoutError):
                    await self.engine.render(['你好'], self.config)
                sleep.assert_not_awaited()
            session.post.assert_called_once()
        finally:
            self.engine.session = None

    async def test_empty_success_recovers_without_changing_text(self):
        self.empty_once = True
        with patch('audio.asyncio.sleep', new_callable=AsyncMock):
            self.assertEqual(await self.engine.render(['你好'], self.config), self.audio)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0], self.calls[1])

    async def test_residual_audio_error_and_unknown_reason_are_not_retried(self):
        for reason in ('error', 'length', 'content_filter', 'future_reason'):
            with self.subTest(reason=reason):
                self.calls.clear()
                self.payload = json.loads(audio_response(reason))
                with patch('audio.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    with self.assertRaises(SpeechError):
                        await self.engine.render(['你好'], self.config)
                self.assertEqual(len(self.calls), 1)
                sleep.assert_not_awaited()

    async def test_missing_audio_is_bounded_and_filter_is_not_retried(self):
        for reason, expected in [('stop', 'missing_audio'), ('content_filter', 'content_filter')]:
            self.calls.clear()
            self.payload = {'choices': [{'finish_reason': reason, 'message': {'content': 'private diagnostic'}}]}
            with patch('audio.asyncio.sleep', new_callable=AsyncMock):
                with self.assertRaisesRegex(SpeechError, expected) as error:
                    await self.engine.render(['你好'], self.config)
            self.assertNotIn('private', str(error.exception))
            self.assertEqual(len(self.calls), 2 if reason == 'stop' else 1)
