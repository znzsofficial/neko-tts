import base64
import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from history_visibility import ReplyReceiptVisibility, VoiceVisibility


def item(message_id, body='[语音消息]', *, chat='', self_message=False, kind='UserMessageItem'):
    attrs = f'msg_id="{message_id}"'
    if chat:
        attrs += f' chat_id="{chat}"'
    if self_message:
        attrs += ' is_self_message="true"'
    return {'item_type': kind, 'meta': {'item_id': 'context-' + message_id},
            'parts': [{'text': '<message ' + attrs + '>\n'}, {'text': body}]}


def receipt(body, call_id='call-1', **kwargs):
    return dict(item_type='FunctionCallOutputItem', meta={'item_id': 'tool-1'}, call_id=call_id,
                tool_name='reply', success=True, output='"麦麦"已生成并向"用户"发送了回复"' + body + '"', **kwargs)


class ReplyReceiptTests(TestCase):
    def setUp(self):
        self.v = ReplyReceiptVisibility(clock=lambda: 1000)
        self.v.remember('chat', 'call-1', 2, 'text_and_voice')

    def test_strip_exact_appended_clips_keep_original_literal_and_identity(self):
        source = receipt('文字' + '[语音消息]' * 4)
        cleaned = self.v.filter_items([source], 'chat')[0]
        self.assertEqual(cleaned['output'], receipt('文字[语音消息][语音消息]')['output'])
        self.assertEqual(cleaned['meta'], source['meta'])
        self.assertEqual(cleaned['call_id'], source['call_id'])
        self.assertEqual(source['output'].count('[语音消息]'), 4)
        self.assertEqual(self.v.filter_items([cleaned], 'chat'), [cleaned])

    def test_unrelated_user_tools_failed_and_other_chat_untouched(self):
        source = [receipt('文字[语音消息][语音消息]', call_id='unknown'),
                  item('u', '[语音消息]'),
                  dict(receipt('[语音消息][语音消息]'), tool_name='search'),
                  dict(receipt('[语音消息][语音消息]'), success=False),
                  receipt('正文里[语音消息]但末尾没有'),
                  dict(receipt('[语音消息][语音消息]'), output='[语音消息][语音消息]')]
        self.assertEqual(self.v.filter_items(source, 'chat'), source)
        own = receipt('[语音消息][语音消息]')
        self.assertEqual(self.v.filter_items([own], 'other'), [own])

    def test_voice_only_keeps_tool_result_without_audio_marker(self):
        self.v.remember('chat', 'call-1', 2, 'voice_only')
        self.assertEqual(self.v.filter_items([receipt('[语音消息]' * 2)], 'chat')[0]['output'], '回复已发送。')
        mixed = receipt('文字[语音消息][语音消息]')
        self.assertEqual(self.v.filter_items([mixed], 'chat'), [mixed])

    def test_current_chat_receipt_and_quotes_in_body(self):
        source = dict(receipt('正文'), output='"麦麦"已生成并向当前会话发送了回复"正文"引号"[语音消息][语音消息]"')
        cleaned = self.v.filter_items([source], 'chat')[0]
        self.assertEqual(cleaned['output'], '"麦麦"已生成并向当前会话发送了回复"正文"引号""')

    def test_persistent_counts_expiry_and_corrupt_preservation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'calls.json'
            self.v.path = path
            self.v.save()
            loaded = ReplyReceiptVisibility(path, clock=lambda: 1000)
            loaded.load()
            self.assertEqual(loaded.calls, self.v.calls)
            self.assertNotIn('正文', path.read_text(encoding='utf-8'))
            expired = ReplyReceiptVisibility(path, clock=lambda: 1000 + self.v.TTL + 1)
            expired.load()
            self.assertFalse(expired.calls)
            path.write_text('bad', encoding='utf-8')
            broken = ReplyReceiptVisibility(path)
            with self.assertRaises(ValueError): broken.load()
            broken.save()
            self.assertEqual(path.read_text(encoding='utf-8'), 'bad')

    def test_invalid_counts_and_scope_are_not_remembered(self):
        for count in [True, 0, -1, 257, '2']:
            self.v.remember('chat', 'bad', count, 'text_and_voice')
        self.assertNotIn(('chat', 'bad'), self.v.calls)


class VisibilityTests(TestCase):
    def setUp(self):
        self.now = [1000]
        self.v = VoiceVisibility(clock=lambda: self.now[0])
        self.audio = b'owned-audio'
        self.digest = hashlib.sha256(self.audio).hexdigest()

    def observe(self, scope='chat-a', message_id='123', digest=None, sent=True):
        return self.v.observe(scope, {'message_id': message_id, 'raw_message': [{'type': 'voice', 'hash': digest or self.digest}]}, sent)

    def test_exact_owned_audio_receipt_hides_only_that_message(self):
        self.v.remember_audio('chat-a', self.audio)
        self.assertTrue(self.observe())
        outgoing = item('123', 'voice transcript')
        user_voice = item('124', 'user speech')
        text = item('125', 'original text', self_message=True)
        source = [outgoing, user_voice, text]
        self.assertEqual(self.v.filter_items(source, 'chat-a'), [user_voice, text])
        self.assertEqual(source, [outgoing, user_voice, text])
        self.assertEqual(self.v.filter_items(source, 'other-chat'), source)

    def test_unsent_unowned_and_mixed_messages_not_hidden(self):
        self.v.remember_audio('chat-a', self.audio)
        self.assertFalse(self.observe(sent=False))
        self.assertFalse(self.observe(digest='f' * 64))
        self.assertFalse(self.observe(scope='other'))
        self.assertFalse(self.v.observe('chat-a', {'message_id': '123', 'raw_message': [{'type': 'voice', 'hash': self.digest}, {'type': 'text', 'data': 'keep'}]}, True))
        self.assertTrue(self.observe())

    def test_duplicate_audio_receipts_are_counted_not_retriggered(self):
        self.v.remember_audio('chat-a', self.audio)
        self.v.remember_audio('chat-a', self.audio)
        self.assertTrue(self.observe(message_id='123'))
        self.assertFalse(self.observe(message_id='123'))
        self.assertTrue(self.observe(message_id='124'))
        self.assertFalse(self.observe(message_id='125'))
        self.assertEqual(len(self.v.ids), 2)

    def test_legacy_cache_migrates_to_persistent_path_without_deleting_it(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy = Path(temp) / 'runtime' / 'voice-receipts.json'
            self.v.path = legacy
            self.v.remember_audio('chat-a', self.audio)
            self.observe()
            self.v.save()
            original = legacy.read_bytes()
            persistent = Path(temp) / 'data' / 'voice-receipts.json'
            migrated = VoiceVisibility(persistent, clock=lambda: self.now[0])
            migrated.load_with_legacy(legacy)
            self.assertEqual(migrated.ids, self.v.ids)
            self.assertEqual(legacy.read_bytes(), original)
            self.assertTrue(persistent.exists())
            # New cache wins; stale legacy data must never overwrite it.
            persistent.write_text('[]', encoding='utf-8')
            current = VoiceVisibility(persistent, clock=lambda: self.now[0])
            current.load_with_legacy(legacy)
            self.assertEqual(current.ids, {})

    def test_extreme_invalid_expiry_is_safe_and_preserves_file(self):
        for value in [10 ** 1000, float('inf'), float('nan')]:
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'voice-receipts.json'
                original = json.dumps([['chat-a', '123', value]])
                path.write_text(original, encoding='utf-8')
                cache = VoiceVisibility(path)
                with self.assertRaises(ValueError):
                    cache.load()
                cache.save()
                self.assertEqual(path.read_text(encoding='utf-8'), original)

    def test_cache_byte_limit_compacts_oldest_not_newest(self):
        with tempfile.TemporaryDirectory() as temp:
            self.v.path = Path(temp) / 'voice-receipts.json'
            self.v.MAX_CACHE_BYTES = 150
            for message_id in ['1' * 50, '2' * 50, '3' * 50]:
                self.v.remember_audio('chat-a', self.audio)
                self.observe(message_id=message_id)
            self.v.save()
            self.assertLessEqual(self.v.path.stat().st_size, 150)
            self.assertIn(('chat-a', '3' * 50), self.v.ids)

    def test_historical_empty_self_voice_placeholder_only(self):
        old = item('old', self_message=True)
        user = item('user', self_message=False)
        speech = item('other', 'meaningful speech', self_message=True)
        self.assertEqual(self.v.filter_items([old, user, speech], 'chat-a'), [user, speech])

    def test_do_not_parse_ids_in_tool_results_or_quoted_user_body(self):
        self.v.remember_audio('chat-a', self.audio)
        self.observe()
        tool = item('123', kind='FunctionCallOutputItem')
        quoted = {'item_type': 'UserMessageItem', 'parts': [{'text': 'user says: <message msg_id="123">\n'}]}
        real_user = item('new', '<message msg_id="123">\n')
        source = [tool, quoted, real_user]
        self.assertEqual(self.v.filter_items(source, 'chat-a'), source)

    def test_focus_chat_scope_and_alias(self):
        raw_id = 'very-long-platform-id-123456789'
        self.v.remember_audio('chat-a', self.audio)
        self.observe(message_id=raw_id)
        alias = 'm' + base64.b32encode(hashlib.sha1(raw_id.encode()).digest()).decode().lower()[:6]
        own = item(alias, 'spoken words', chat='chat-a', self_message=True)
        other = item(alias, 'user speech', chat='chat-b', self_message=False)
        collision = item(alias, 'user speech', chat='chat-a', self_message=False)
        self.assertEqual(self.v.filter_items([own, other, collision], 'focus-chat'), [other, collision])

    def test_file_survives_reload_without_audio_or_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'voice-receipts.json'
            self.v.path = path
            self.v.remember_audio('chat-a', self.audio)
            self.observe()
            self.v.save()
            loaded = VoiceVisibility(path, clock=lambda: self.now[0])
            loaded.load()
            self.assertEqual(loaded.filter_items([item('123', 'spoken')], 'chat-a'), [])
            content = path.read_text(encoding='utf-8')
            self.assertNotIn('owned-audio', content)
            self.assertEqual(len(json.loads(content)), 1)

    def test_corrupt_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'voice-receipts.json'
            path.write_text('broken', encoding='utf-8')
            v = VoiceVisibility(path)
            with self.assertRaises(ValueError):
                v.load()
            v.save()
            self.assertEqual(path.read_text(encoding='utf-8'), 'broken')

    def test_expiry_and_cap(self):
        self.v.MAX_IDS = 2
        self.v.remember_audio('chat-a', self.audio)
        self.now[0] += 301
        self.assertFalse(self.observe())
        for message_id in ['1', '2', '3']:
            self.v.remember_audio('chat-a', self.audio)
            self.assertTrue(self.observe(message_id=message_id))
        self.assertEqual(list(self.v.ids), [('chat-a', '2'), ('chat-a', '3')])
        self.now[0] += 7 * 86400 + 1
        self.assertEqual(self.v.filter_items([item('3', 'spoken')], 'chat-a'), [item('3', 'spoken')])
