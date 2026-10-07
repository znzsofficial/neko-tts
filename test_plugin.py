import asyncio
import base64
from copy import deepcopy
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock

from plugin import NekoTTS, Config
from audio import split_text, voice_message


class TextTests(TestCase):
    def test_no_truncation(self):
        text = '你好。' * 450
        parts = split_text(text, Config().general.model_dump())
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p) <= 500 for p in parts))

    def test_limits(self):
        for text in ('', '```python\nprint(1)\n```', 'a' * 2001):
            with self.assertRaises(ValueError):
                split_text(text, Config().general.model_dump())

    def test_voice_has_no_quote(self):
        message = voice_message(b'audio')
        self.assertFalse(message['quote_previous'])
        self.assertEqual(base64.b64decode(message['segments'][0]['binary_data_base64']), b'audio')


class PluginTests(IsolatedAsyncioTestCase):
    def setUp(self):
        # SDK's context/config properties are exercised through a tiny test subclass.
        class Plugin(NekoTTS):
            @property
            def config(self): return self.test_config
            @property
            def ctx(self): return self.test_ctx
        self.p = Plugin()
        self.p.test_config = Config()
        self.p.test_config.plugin.enabled = True
        self.p.test_ctx = SimpleNamespace(logger=SimpleNamespace(warning=lambda *a: None),
                                         send=SimpleNamespace(custom=AsyncMock(return_value=True)))
        self.p._ready = True
        self.p._render = AsyncMock(return_value=b'audio')
        self.messages = [{'segments': [{'type': 'text', 'data': '你好'}], 'quote_previous': True},
                         {'segments': [{'type': 'image', 'data': '', 'hash': 'abc'}], 'quote_previous': False}]

    async def test_prepare_is_noop(self):
        self.assertEqual(await self.p.voice_reply_extension(phase='prepare'), {})
        self.p._render.assert_not_called()

    async def test_selected_reply_preserves_everything(self):
        before = deepcopy(self.messages)
        result = await self.p.voice_reply_extension(phase='before_send', messages=self.messages,
                                                  parameters={'style': '轻声'})
        self.assertEqual(self.messages, before)
        self.assertEqual(result['messages'][:-1], before)
        self.assertFalse(result['messages'][-1]['quote_previous'])
        self.p._render.assert_awaited_once_with('你好', '轻声')
        self.p.ctx.send.custom.assert_not_called()

    async def test_voice_only_drops_text_but_keeps_one_voice(self):
        self.p.config.output.mode = 'voice_only'
        result = await self.p.voice_reply_extension(phase='before_send', messages=self.messages)
        self.assertEqual(len(result['messages']), 1)
        self.assertEqual(result['messages'][0]['segments'][0]['type'], 'voice')

    async def test_text_then_voice_registers_without_synthesizing(self):
        self.p.config.output.mode = 'text_then_voice'
        result = await self.p.voice_reply_extension(phase='before_send',
                                                    session_id='chat-a', messages=self.messages,
                                                    parameters={'style': '轻声'})
        self.assertEqual(result['messages'], self.messages)
        self.p._render.assert_not_called()
        self.assertEqual(self.p._pending['chat-a'][0][1], '轻声')

    async def test_text_then_voice_starts_only_after_matching_send(self):
        self.p.config.output.mode = 'text_then_voice'
        self.p._pending['chat-a'] = [('你好', '开心', 9999999999)]
        self.p._background_voice = AsyncMock()
        await self.p.after_text_send(
            message={'session_id': 'chat-a', 'raw_message': [{'type': 'text', 'data': '你好'}]},
            sent=True,
        )
        await asyncio.sleep(0)
        self.p._background_voice.assert_awaited_once_with('你好', '开心', 'chat-a')
        self.assertNotIn('chat-a', self.p._pending)

    async def test_failure_preserves_original(self):
        self.p._render.side_effect = RuntimeError('network')
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=self.messages), {})

    async def test_disabled_never_synthesizes(self):
        self.p.config.plugin.enabled = False
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=self.messages), {})
        self.p._render.assert_not_called()

    async def test_concurrent_styles_are_call_local(self):
        await asyncio.gather(*(self.p.voice_reply_extension(phase='before_send', messages=self.messages,
                                                          parameters={'style': style}) for style in ('开心', '轻声')))
        self.assertEqual({call.args[1] for call in self.p._render.await_args_list}, {'开心', '轻声'})

    async def test_command_authorization_and_send_failure(self):
        self.assertFalse((await self.p.command(stream_id='a', user_id='other', matched_groups={'text': '你好'}))[0])
        self.p._render.assert_not_called()
        self.p.config.command.allowed_user_ids = ['owner']
        self.p.ctx.send.custom.return_value = False
        result = await self.p.command(stream_id='a', user_id='owner', matched_groups={'text': '你好'})
        self.assertFalse(result[0])

    async def test_reset_cancels_pending_work(self):
        self.p._engine = SimpleNamespace(close=AsyncMock())
        task = asyncio.create_task(asyncio.sleep(100))
        self.p._tasks.add(task)
        await self.p._reset()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.p._ready)
