import base64
import io
import tempfile
import wave
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from aiohttp import web
from audio import SpeechEngine
from plugin import Config


class TransportTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = SpeechEngine(self.temp.name)
        self.config = Config().model_dump()
        self.config['mimo'].update(synthesis_mode='preset', api_key='test')
        self.calls = []
        self.status = 200
        self.invalid = False
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as file:
            file.setnchannels(1); file.setsampwidth(2); file.setframerate(24000)
            file.writeframes(b'\0\0' * 2400)
        self.audio = stream.getvalue()
        async def handler(request):
            self.calls.append((await request.json(), request.headers.get('api-key')))
            if self.status != 200:
                return web.Response(status=self.status, text='private provider diagnostic')
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

    async def test_preset_and_style(self):
        self.engine.reference = AsyncMock(side_effect=AssertionError('no reference for preset'))
        audio = await self.engine.render(['你好'], '轻声', self.config)
        self.assertEqual(audio, self.audio)
        body, key = self.calls[0]
        self.assertEqual(key, 'test')
        self.assertEqual(body['model'], 'mimo-v2.5-tts')
        self.assertEqual(body['audio']['voice'], '冰糖')
        self.assertIn('轻声', body['messages'][0]['content'])
        self.assertEqual(body['messages'][1]['content'], '你好')

    async def test_clone_and_segment_order(self):
        self.config['mimo']['synthesis_mode'] = 'voiceclone'
        self.engine.reference = AsyncMock(return_value='data:audio/wav;base64,ref')
        self.engine.merge = AsyncMock(return_value=b'merged')
        self.assertEqual(await self.engine.render(['第一句', '第二句'], '', self.config), b'merged')
        self.assertEqual([c[0]['messages'][1]['content'] for c in self.calls], ['第一句', '第二句'])
        self.assertEqual(self.calls[0][0]['audio']['voice'], 'data:audio/wav;base64,ref')

    async def test_errors_are_not_retried_or_leaked(self):
        self.status = 429
        with self.assertRaisesRegex(RuntimeError, '^MiMo HTTP 429$'):
            await self.engine.render(['你好'], '', self.config)
        self.assertEqual(len(self.calls), 1)
        self.status = 200
        self.invalid = True
        with self.assertRaises(ValueError):
            await self.engine.render(['你好'], '', self.config)
