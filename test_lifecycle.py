"""Offline concurrent RPC-style regressions using the real plugin render path.

Host contract checked at MaiBot c602a278: reply_extensions.transform leaves
messages unchanged for {}, then ensure_active rejects an unloaded/replaced
entry. Runner _handle_invoke catches Exception, not CancelledError. The small
harness below models that boundary, not a running host/QQ or a delivery claim.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from audio import decode_response
from plugin import Config, NekoTTS
from test_audio import audio_response, pcm_wav


class LifecycleTests(IsolatedAsyncioTestCase):
    def setUp(self):
        class Plugin(NekoTTS):
            @property
            def config(self):
                return self.test_config

            @property
            def ctx(self):
                return self.test_ctx

        self.p = Plugin()
        self.p.test_config = Config()
        self.p.config.plugin.enabled = True
        self.p.config.command.allowed_user_ids = ['owner']
        self.p.test_ctx = SimpleNamespace(
            logger=SimpleNamespace(warning=Mock(), info=Mock()),
            paths=SimpleNamespace(runtime_dir='unused'),
            send=SimpleNamespace(custom=AsyncMock(return_value=True)))
        self.p._ready = True
        self.started = asyncio.Queue()
        self.release = asyncio.Event()

        async def render(chunks, config):
            await self.started.put(asyncio.current_task())
            await self.release.wait()
            return pcm_wav()

        self.p._engine = SimpleNamespace(render=AsyncMock(side_effect=render), close=AsyncMock())
        self.messages = [
            {'segments': [{'type': 'text', 'data': '正文'}], 'quote_previous': True},
            {'segments': [{'type': 'image', 'data': '', 'hash': 'attachment'}], 'quote_previous': False}]
        self.active = True

    async def asyncTearDown(self):
        await self.p._reset()

    async def host_transform(self):
        # Same empty-result and post-RPC active check as the pinned host.
        result = await self.p.voice_reply_extension(phase='before_send', messages=self.messages,
                                                   session_id='chat', reply_id='reply', call_id='call')
        if not self.active:
            raise ValueError('reply extension unloaded or replaced')
        return deepcopy(result.get('messages', self.messages))

    async def reload(self):
        replacement = SimpleNamespace(render=AsyncMock(return_value=pcm_wav()), close=AsyncMock())
        with patch('plugin.SpeechEngine', return_value=replacement):
            await self.p.on_config_update('self', {}, 1)
        return replacement

    async def test_reload_cancels_concurrent_internal_renders_but_preserves_original_batches(self):
        for mode in ('text_and_voice', 'voice_only'):
            with self.subTest(mode=mode):
                self.p.config.output.mode = mode
                # Restore the blocking engine after each generation.
                async def render(chunks, config):
                    await self.started.put(asyncio.current_task())
                    await self.release.wait()
                    return pcm_wav()
                self.p._engine.render = AsyncMock(side_effect=render)
                callers = [asyncio.create_task(self.host_transform()) for _ in range(2)]
                children = [await self.started.get() for _ in callers]
                replacement = await self.reload()
                self.assertEqual(await asyncio.gather(*callers), [self.messages, self.messages])
                self.assertTrue(all(child.cancelled() for child in children))
                self.assertTrue(all(not caller.cancelled() and caller.cancelling() == 0 for caller in callers))
                self.assertFalse(self.p._tasks)
                self.assertFalse(self.p._visibility.pending)
                self.assertFalse(self.p._reply_visibility.calls)
                self.p.ctx.send.custom.assert_not_awaited()
                # New generation remains usable, with no old work replayed.
                result = await self.host_transform()
                self.assertEqual(result[-1]['segments'][0]['type'], 'voice')
                replacement.render.assert_awaited_once()
                self.p._visibility.pending.clear()
                self.p._reply_visibility.calls.clear()

    async def test_external_cancellation_propagates_without_fallback_or_send(self):
        caller = asyncio.create_task(self.host_transform())
        child = await self.started.get()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(child.cancelled())
        self.assertFalse(self.p._tasks)
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_reload_discards_already_prepared_audio_if_a_later_message_is_cancelled(self):
        self.messages.append({'segments': [{'type': 'text', 'data': '第二句'}], 'quote_previous': False})
        blocked = self.p._engine.render.side_effect
        async def render(chunks, config):
            if chunks == ['正文']:
                return pcm_wav()
            return await blocked(chunks, config)
        self.p._engine.render.side_effect = render
        self.p.config.output.mode = 'voice_only'
        caller = asyncio.create_task(self.host_transform())
        await self.started.get()
        await self.reload()
        self.assertEqual(await caller, self.messages)
        self.assertFalse(self.p._visibility.pending)
        self.assertFalse(self.p._reply_visibility.calls)
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_deferred_worker_reset_cancels_real_render_without_replaying_receipts(self):
        self.p.config.output.mode = 'text_then_voice'
        self.assertEqual(await self.host_transform(), self.messages)
        receipt = dict(session_id='chat', reply_id='reply', call_id='call', send_results=[
            {'index': 0, 'success': True, 'message_id': 'message', 'text': '正文'}])
        await self.p.after_reply_delivery(**receipt)
        child = await self.started.get()
        worker = self.p._voice_worker
        await self.reload()
        self.assertTrue(child.cancelled())
        self.assertTrue(worker.cancelled())
        self.assertFalse(self.p._tasks)
        self.assertTrue(self.p._voice_queue.empty())
        await self.p.after_reply_delivery(**receipt)
        self.assertIsNone(self.p._voice_worker)
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_external_cancellation_wins_when_reset_races_it(self):
        caller = asyncio.create_task(self.host_transform())
        await self.started.get()
        caller.cancel()
        await self.reload()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertTrue(caller.cancelled())

    async def test_unrelated_child_cancellation_is_not_hidden(self):
        caller = asyncio.create_task(self.host_transform())
        child = await self.started.get()
        child.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller

    async def test_reply_deadline_is_not_swallowed(self):
        async def limited():
            async with asyncio.timeout(None) as deadline:
                # Expire cooperatively only after render has actually started.
                async def expire():
                    await self.started.get()
                    deadline.reschedule(asyncio.get_running_loop().time() - 1)
                trigger = asyncio.create_task(expire())
                try:
                    return await self.host_transform()
                finally:
                    await trigger
        with self.assertRaises(TimeoutError):
            await limited()
        self.assertFalse(self.p._tasks)
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_unload_still_fails_host_active_check_not_successful_delivery(self):
        caller = asyncio.create_task(self.host_transform())
        await self.started.get()
        self.active = False
        await self.p.on_unload()
        with self.assertRaisesRegex(ValueError, 'unloaded or replaced'):
            await caller
        self.assertFalse(self.p._ready)
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_residual_error_pcm_keeps_text_and_attachments_in_both_sync_modes(self):
        self.p._engine.render.side_effect = lambda *_: decode_response(audio_response('error'))
        original = deepcopy(self.messages)
        for mode in ('text_and_voice', 'voice_only'):
            with self.subTest(mode=mode):
                self.p.config.output.mode = mode
                self.assertEqual(await self.host_transform(), original)
                self.assertEqual(self.messages, original)
                self.assertFalse(self.p._visibility.pending)
                self.assertFalse(self.p._reply_visibility.calls)
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_command_synthesis_failure_never_claims_delivery_or_sends(self):
        self.p._engine.render.side_effect = lambda *_: decode_response(audio_response('error'))
        result = await self.p.command(stream_id='chat', user_id='owner', matched_groups={'text': '正文'})
        self.assertEqual(result, (False, '语音合成失败，请检查配置和服务状态。', True))
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_command_delivery_exception_is_unknown_and_never_retried(self):
        self.release.set()
        for exc in (TimeoutError('private acknowledgement'), RuntimeError('private rpc diagnostic')):
            with self.subTest(error=type(exc).__name__):
                self.p._engine.render.reset_mock()
                self.p.ctx.send.custom.reset_mock()
                self.p.ctx.send.custom.side_effect = exc
                result = await self.p.command(stream_id='chat', user_id='owner', matched_groups={'text': '正文'})
                self.assertFalse(result[0])
                self.assertIn('发送结果未知', result[1])
                self.assertIn('勿立即重试', result[1])
                self.assertIn('不会自动重发', result[1])
                self.assertNotIn('private', result[1])
                self.p._engine.render.assert_awaited_once()
                self.p.ctx.send.custom.assert_awaited_once()
                self.assertNotIn('private', str(self.p.ctx.logger.warning.call_args))

    async def test_command_send_caller_cancel_is_not_reported_as_failure(self):
        self.release.set()
        entered = asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.p.ctx.send.custom.side_effect = send
        caller = asyncio.create_task(self.p.command(stream_id='chat', user_id='owner', matched_groups={'text': '正文'}))
        await entered.wait()
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.p.ctx.send.custom.assert_awaited_once()
