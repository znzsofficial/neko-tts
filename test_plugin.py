import asyncio
import base64
import hashlib
from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from plugin import NekoTTS, Config
from audio import split_text, voice_message


class TextTests(TestCase):
    def test_voice_extension_has_no_parameters(self):
        info = NekoTTS.voice_reply_extension.__maibot_component_info__
        self.assertEqual(info.parameters_schema, {'type': 'object', 'properties': {}, 'additionalProperties': False})

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
                                                   parameters={})
        self.assertEqual(self.messages, before)
        self.assertEqual(result['messages'][:-1], before)
        self.assertFalse(result['messages'][-1]['quote_previous'])
        self.p._render.assert_awaited_once_with('你好')
        self.p.ctx.send.custom.assert_not_called()

    async def test_tts_receipt_removed_from_both_model_prompts(self):
        result = await self.p.voice_reply_extension(phase='before_send', messages=self.messages, session_id='chat-a')
        voice = result['messages'][-1]['segments'][0]
        self.assertEqual(voice['hash'], hashlib.sha256(b'audio').hexdigest())
        await self.p.after_text_send(message={'session_id': 'chat-a', 'message_id': '123', 'raw_message': [voice]}, sent=True)
        own = {'item_type': 'UserMessageItem', 'parts': [{'text': '<message msg_id="123">\n'}, {'text': '[语音消息]'}]}
        user = {'item_type': 'UserMessageItem', 'parts': [{'text': '<message msg_id="124">\n'}, {'text': '用户语音正文'}]}
        for handler in [self.p.hide_planner_voice, self.p.hide_replyer_voice]:
            change = await handler(items=[own, user], session_id='chat-a', item_schema_version=1, other='preserved')
            self.assertEqual(change['modified_kwargs']['items'], [user])
            self.assertEqual(change['modified_kwargs']['other'], 'preserved')

    async def test_direct_and_background_voice_do_not_sync_history(self):
        self.p.config.command.allowed_user_ids = ['owner']
        await self.p.command(stream_id='chat-a', user_id='owner', matched_groups={'text': 'test'})
        options = self.p.ctx.send.custom.call_args.kwargs
        self.assertFalse(options['sync_to_maisaka_history'])
        self.assertFalse(options['storage_message'])
        await self.p._background_voice('test', 'chat-a')
        self.assertFalse(self.p.ctx.send.custom.call_args.kwargs['sync_to_maisaka_history'])
        self.assertFalse(self.p.ctx.send.custom.call_args.kwargs['storage_message'])

    async def test_oversized_audio_preserves_original_and_no_receipts(self):
        self.p._render.return_value = b'x' * (8 * 1024 * 1024 + 1)
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=self.messages, session_id='chat-a'), {})
        self.assertFalse(self.p._visibility.pending)
        self.p.ctx.send.custom.assert_not_called()

    async def test_audio_batch_rpc_budget_preserves_original(self):
        self.p._render.return_value = b'x' * (5 * 1024 * 1024)
        messages = [self.messages[0], self.messages[0]]
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=messages, session_id='chat-a'), {})
        self.assertFalse(self.p._visibility.pending)

    async def test_oversized_command_audio_is_never_sent(self):
        self.p.config.command.allowed_user_ids = ['owner']
        self.p._render.return_value = b'x' * (8 * 1024 * 1024 + 1)
        result = await self.p.command(stream_id='chat-a', user_id='owner', matched_groups={'text': 'test'})
        self.assertFalse(result[0])
        self.p.ctx.send.custom.assert_not_called()

    async def test_voice_only_drops_text_but_keeps_one_voice(self):
        self.p.config.output.mode = 'voice_only'
        result = await self.p.voice_reply_extension(phase='before_send', messages=self.messages)
        self.assertEqual(len(result['messages']), 1)
        self.assertEqual(result['messages'][0]['segments'][0]['type'], 'voice')

    async def test_host_split_messages_render_and_send_separately(self):
        messages = [
            {'segments': [{'type': 'text', 'data': '第一句'}], 'quote_previous': True},
            self.messages[1],
            {'segments': [{'type': 'at', 'data': 'someone'}, {'type': 'text', 'data': '第二句'}], 'quote_previous': False},
        ]
        original = deepcopy(messages)
        self.p._render.side_effect = [b'first-audio', b'second-audio']
        result = await self.p.voice_reply_extension(phase='before_send', text='断句前整句不要朗读', messages=messages)
        self.assertEqual([call.args for call in self.p._render.await_args_list], [('第一句',), ('第二句',)])
        out = result['messages']
        self.assertEqual(out[:3], original)
        self.assertEqual([out[i]['segments'][0]['type'] for i in [3, 4]], ['voice', 'voice'])
        self.assertEqual([base64.b64decode(out[i]['segments'][0]['binary_data_base64']) for i in [3, 4]], [b'first-audio', b'second-audio'])
        self.assertEqual(messages, original)

    async def test_voice_only_keeps_host_split_boundaries(self):
        self.p.config.output.mode = 'voice_only'
        messages = [self.messages[0], {'segments': [{'type': 'text', 'data': '第二句'}], 'quote_previous': False}]
        result = await self.p.voice_reply_extension(phase='before_send', messages=messages)
        self.assertEqual(len(result['messages']), 2)
        self.assertTrue(all(m['segments'][0]['type'] == 'voice' and not m['quote_previous'] for m in result['messages']))

    async def test_second_segment_failure_keeps_entire_original_reply(self):
        messages = [self.messages[0], {'segments': [{'type': 'text', 'data': '第二句'}], 'quote_previous': False}]
        original = deepcopy(messages)
        self.p._render.side_effect = [b'first', RuntimeError('failed')]
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=messages), {})
        self.assertEqual(messages, original)
        self.p.ctx.send.custom.assert_not_called()

    async def test_whole_reply_limits_apply_across_host_messages(self):
        self.p.config.general.max_total_length = 50
        messages = [{'segments': [{'type': 'text', 'data': 'a' * 30}], 'quote_previous': False} for _ in range(2)]
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=messages), {})
        self.p._render.assert_not_called()
        self.p.config.general.max_total_length = 2000
        self.p.config.general.max_segments = 1
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=messages), {})
        self.p._render.assert_not_called()

    async def test_whole_reply_deadline_and_reload_preserve_original(self):
        messages = [self.messages[0], {'segments': [{'type': 'text', 'data': '第二句'}], 'quote_previous': False}]
        async def slow(text):
            await asyncio.sleep(.02)
            return b'audio'
        self.p._render.side_effect = slow
        timeout = asyncio.timeout
        with patch('plugin.asyncio.timeout', side_effect=lambda seconds: timeout(.03)):
            self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=messages), {})
        async def reloaded(text):
            self.p._generation += 1
            return b'audio'
        self.p._render.side_effect = reloaded
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=messages), {})

    async def test_background_matches_each_host_message_without_rejoining(self):
        self.p.config.output.mode = 'text_then_voice'
        messages = [self.messages[0], {'segments': [{'type': 'text', 'data': '第二句'}], 'quote_previous': False}]
        await self.p.voice_reply_extension(phase='before_send', messages=messages, session_id='chat-a')
        self.assertEqual([item[0] for item in self.p._pending['chat-a']], ['你好', '第二句'])
        self.p._background_voice = AsyncMock()
        for text in ['你好', '第二句']:
            await self.p.after_text_send(message={'session_id': 'chat-a', 'raw_message': [{'type': 'text', 'data': text}]}, sent=True)
        await asyncio.sleep(0)
        self.assertEqual([call.args for call in self.p._background_voice.await_args_list], [('你好', 'chat-a'), ('第二句', 'chat-a')])

    async def test_attachment_only_is_unchanged(self):
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=[self.messages[1]]), {})
        self.p._render.assert_not_called()

    async def test_text_then_voice_registers_without_synthesizing(self):
        self.p.config.output.mode = 'text_then_voice'
        result = await self.p.voice_reply_extension(phase='before_send',
                                                    session_id='chat-a', messages=self.messages,
                                                     parameters={})
        self.assertEqual(result['messages'], self.messages)
        self.p._render.assert_not_called()
        self.assertEqual(self.p._pending['chat-a'][0][0], '你好')

    async def test_text_then_voice_starts_only_after_matching_send(self):
        self.p.config.output.mode = 'text_then_voice'
        self.p._pending['chat-a'] = [('你好', 9999999999)]
        self.p._background_voice = AsyncMock()
        await self.p.after_text_send(
            message={'session_id': 'chat-a', 'raw_message': [{'type': 'text', 'data': '你好'}]},
            sent=True,
        )
        await asyncio.sleep(0)
        self.p._background_voice.assert_awaited_once_with('你好', 'chat-a')
        self.assertNotIn('chat-a', self.p._pending)

    async def test_background_registration_requires_chat(self):
        self.p.config.output.mode = 'text_then_voice'
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=self.messages), {})
        self.assertEqual(self.p._pending, {})

    async def test_expired_background_records_are_removed(self):
        self.p.config.output.mode = 'text_then_voice'
        self.p._pending['chat-a'] = [('你好', 0)]
        await self.p.after_text_send(message={'session_id': 'chat-a', 'raw_message': [{'type': 'text', 'data': '你好'}]}, sent=True)
        self.assertNotIn('chat-a', self.p._pending)
        self.p._render.assert_not_called()

    async def test_failure_preserves_original(self):
        self.p._render.side_effect = RuntimeError('network')
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=self.messages), {})

    async def test_disabled_never_synthesizes(self):
        self.p.config.plugin.enabled = False
        self.assertEqual(await self.p.voice_reply_extension(phase='before_send', messages=self.messages), {})
        self.p._render.assert_not_called()

    async def test_concurrent_replies_use_text_only(self):
        await asyncio.gather(*(self.p.voice_reply_extension(phase='before_send', messages=self.messages,
                                                          parameters={}) for _ in range(2)))
        self.assertEqual([call.args for call in self.p._render.await_args_list], [('你好',), ('你好',)])

    def reply_item(self, args=None):
        return {'item_type': 'FunctionCallItem', 'meta': {'item_id': 'id'},
                'tool_call': {'call_id': 'call', 'func_name': 'reply', 'args': args or {'msg_id': 'message'}, 'extra_content': None}}

    async def test_hybrid_random_injects_once_preserving_other_options(self):
        self.p.config.trigger.mode = 'hybrid'
        item = self.reply_item({'msg_id': 'x', 'plugin_options': {'other.ext': {}}})
        original = deepcopy(item)
        with patch('plugin.random.random', return_value=0.1) as roll:
            result = await self.p.hybrid_reply(output_items=[item], session_id='chat', item_schema_version=1)
            modified = result['modified_kwargs']
            self.assertEqual(modified['output_items'][0]['tool_call']['args']['plugin_options'], {'other.ext': {}, 'neko.tts.voice': {}})
            self.assertEqual(modified['item_schema_version'], 1)
            self.assertEqual(item, original)
            self.assertIsNone(await self.p.hybrid_reply(**modified))
            roll.assert_called_once()

    async def test_hybrid_explicit_selection_bypasses_random(self):
        self.p.config.trigger.mode = 'hybrid'
        self.p.config.trigger.probability = 0
        with patch('plugin.random.random', side_effect=AssertionError('explicit never rolls')):
            self.assertIsNone(await self.p.hybrid_reply(output_items=[self.reply_item({'plugin_options': {'neko.tts.voice': {}}})], session_id='chat'))
        result = await self.p.voice_reply_extension(phase='before_send', messages=self.messages)
        self.assertEqual(result['messages'][-1]['segments'][0]['type'], 'voice')

    async def test_hybrid_miss_and_planner_disabled_do_not_inject(self):
        with patch('plugin.random.random', return_value=0.9):
            self.assertIsNone(await self.p.hybrid_reply(output_items=[self.reply_item()], session_id='chat'))
            self.p.config.trigger.mode = 'hybrid'
            self.assertIsNone(await self.p.hybrid_reply(output_items=[self.reply_item()], session_id='chat'))
            self.p.config.plugin.enabled = False
            self.assertIsNone(await self.p.hybrid_reply(output_items=[self.reply_item()], session_id='chat'))

    async def test_hybrid_ignores_other_tools_and_malformed_items(self):
        self.p.config.trigger.mode = 'hybrid'
        other = self.reply_item(); other['tool_call']['func_name'] = 'neko_draw'
        with patch('plugin.random.random', side_effect=AssertionError('not a reply')):
            self.assertIsNone(await self.p.hybrid_reply(output_items=[None, {}, other, self.reply_item({'plugin_options': None})], session_id='chat'))
            self.assertIsNone(await self.p.hybrid_reply(output_items=[self.reply_item()], session_id=''))

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
