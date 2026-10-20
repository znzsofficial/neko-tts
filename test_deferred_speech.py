import asyncio
from unittest import TestCase, IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from deferred_speech import DeferredSpeech
import test_plugin


def receipt(index=0, mid='m', text='你好', success=True):
    return {'index': index, 'message_id': mid, 'text': text, 'success': success}


class AdmissionTests(TestCase):
    def setUp(self):
        self.state = DeferredSpeech()
        self.state.register('chat', 'reply', 'call', [(0, '你好'), (2, '第二句')])

    def test_exact_identity_cross_chat_call_and_reply_isolation(self):
        for identity in [('other', 'reply', 'call'), ('chat', 'other', 'call'), ('chat', 'reply', 'other')]:
            self.assertEqual(self.state.claim(*identity, [receipt()]), [])
        self.assertEqual(self.state.claim('chat', 'reply', 'call', [receipt()]), [(0, '你好')])

    def test_final_sent_text_is_authoritative_not_registered_text(self):
        self.assertEqual(self.state.claim('chat', 'reply', 'call', [receipt(text='宿主修改后正文')]), [(0, '宿主修改后正文')])

    def test_partial_success_missing_ids_and_attachments_do_not_get_speech(self):
        results = [receipt(), receipt(1, 'image', ''), receipt(2, '', '第二句'), receipt(3, 'x', '第三句', False)]
        self.assertEqual(self.state.claim('chat', 'reply', 'call', results), [(0, '你好')])

    def test_unknown_and_failed_never_synthesize(self):
        self.assertEqual(self.state.claim('chat', 'reply', 'call', [receipt(success=False)]), [])
        self.assertEqual(self.state.claim('chat', 'reply', 'call', [receipt()]), [])

    def test_duplicate_registration_and_delivery_consumed_once(self):
        self.assertFalse(self.state.register('chat', 'reply', 'call', [(0, '你好')]))
        self.assertTrue(self.state.claim('chat', 'reply', 'call', [receipt()]))
        self.assertEqual(self.state.claim('chat', 'reply', 'call', [receipt()]), [])
        self.assertFalse(self.state.register('chat', 'reply', 'call', [(0, '你好')]))

    def test_same_text_in_two_replies_is_not_confused(self):
        self.state.register('chat', 'r2', 'c2', [(0, '你好')])
        self.assertTrue(self.state.claim('chat', 'r2', 'c2', [receipt(mid='m2')]))
        self.assertTrue(self.state.claim('chat', 'reply', 'call', [receipt(mid='m1')]))

    def test_same_platform_id_cannot_be_claimed_by_two_replies(self):
        self.assertTrue(self.state.claim('chat', 'reply', 'call', [receipt()]))
        self.state.register('chat', 'r2', 'c2', [(0, '你好')])
        self.assertEqual(self.state.claim('chat', 'r2', 'c2', [receipt()]), [])

    def test_duplicate_receipt_indices_fail_closed_without_partial_admission(self):
        self.assertEqual(self.state.claim('chat', 'reply', 'call', [receipt(), receipt(mid='m2')]), [])
        self.assertFalse(self.state.delivered)

    def test_invalid_receipt_types_consumed_without_retry(self):
        for bad in [None, {}, [], [None], [receipt(index=True)], [receipt(success='true')], [receipt(text='x'*6001)]]:
            state = DeferredSpeech(); state.register('chat', 'r', 'c', [(0, 'a')])
            self.assertEqual(state.claim('chat', 'r', 'c', bad), [])
            self.assertEqual(state.claim('chat', 'r', 'c', [receipt()]), [])

    def test_pending_cap_and_expiry_are_bounded(self):
        for n in range(31):
            self.assertTrue(self.state.register('chat', str(n), str(n), [(0, 'a')]))
        self.assertFalse(self.state.register('chat', 'overflow', 'c', [(0, 'a')]))
        self.state.pending[('chat', 'reply')]['expires'] = 0
        self.assertTrue(self.state.register('chat', 'new', 'c', [(0, 'a')]))

    def test_empty_or_untrusted_identifiers_cannot_register(self):
        for key in ['', None, 'x'*257]:
            self.assertFalse(self.state.register('chat', key, 'c', [(0, 'a')]))

    def test_same_text_repeated_within_one_reply_keeps_message_boundaries(self):
        self.assertEqual(self.state.claim('chat', 'reply', 'call', [receipt(), receipt(2, 'm2')]), [(0, '你好'), (2, '你好')])


class WorkerTests(IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse only fixture initialization, not a second copy of its test suite.
        fixture = test_plugin.PluginTests(methodName='test_prepare_is_noop'); fixture.setUp()
        self.p = fixture.p
        self.p.config.output.mode = 'text_then_voice'

    async def asyncTearDown(self):
        await self.p._reset()

    async def admit(self, reply='r', mid='m', text='你好'):
        self.p._deferred.register('chat', reply, reply, [(0, text)])
        await self.p.after_reply_delivery(session_id='chat', reply_id=reply, call_id=reply, send_results=[receipt(mid=mid, text=text)])

    async def test_slow_synthesis_does_not_block_delivery_hook(self):
        gate = asyncio.Event()
        async def slow(text):
            await gate.wait(); return b'audio'
        self.p._render.side_effect = slow
        await asyncio.wait_for(self.admit(), .1)
        await asyncio.sleep(0)
        self.p.ctx.send.custom.assert_not_awaited()
        gate.set(); await asyncio.gather(*list(self.p._tasks))
        self.p.ctx.send.custom.assert_awaited_once()

    async def test_batch_fifo_preserves_order(self):
        self.p._background_voice = AsyncMock(return_value=True)
        await self.admit('r1', 'm1', '第一句'); await self.admit('r2', 'm2', '第二句')
        await asyncio.gather(*list(self.p._tasks))
        self.assertEqual([c.args for c in self.p._background_voice.await_args_list], [('第一句', 'chat'), ('第二句', 'chat')])

    async def test_expired_queue_never_synthesizes(self):
        self.p._voice_queue.put_nowait(('chat', [(0, 'a')], 0, self.p._generation))
        await self.p._drain_voice_queue()
        self.p._render.assert_not_awaited()

    async def test_queue_full_drops_voice_without_replay(self):
        for _ in range(8):self.p._voice_queue.put_nowait(('chat', [], 0, 0))
        await self.admit()
        self.assertIsNone(self.p._voice_worker)
        self.assertFalse(self.p._deferred.pending)
        self.p._render.assert_not_awaited()

    async def test_reload_after_render_does_not_send_stale_audio(self):
        async def reloaded(text):
            self.p._generation += 1; return b'audio'
        self.p._render.side_effect = reloaded
        await self.admit(); await asyncio.gather(*list(self.p._tasks))
        self.p.ctx.send.custom.assert_not_awaited()

    async def test_one_deadline_covers_whole_batch(self):
        async def slow(text, stream):
            if text == 'b':
                await asyncio.Event().wait()
            return True
        self.p._background_voice = AsyncMock(side_effect=slow)
        self.p._voice_queue.put_nowait(('chat', [(0, 'a'), (1, 'b')], 9999999999, self.p._generation))
        original = asyncio.timeout
        with patch('plugin.asyncio.timeout', side_effect=lambda _: original(.04)):
            await self.p._drain_voice_queue()
        self.assertEqual(self.p._background_voice.await_count, 2)
        self.assertEqual(self.p._voice_queue.qsize(), 0)

    async def test_unrelated_text_send_never_admits_voice(self):
        self.p._deferred.register('chat', 'r', 'c', [(0, '你好')])
        await self.p.after_text_send(message={'session_id': 'chat', 'message_id': 'other', 'raw_message': [{'type':'text','data':'你好'}]}, sent=True)
        self.p._render.assert_not_awaited()
        self.assertFalse(self.p._tasks)

    async def test_final_changed_text_still_obeys_whole_reply_limits(self):
        self.p.config.general.max_total_length = 50
        await self.admit(text='x'*51)
        self.p._render.assert_not_awaited()
