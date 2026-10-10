import base64
import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from history_visibility import VoiceVisibility


def item(message_id, body='[语音消息]', *, chat='', self_message=False, kind='UserMessageItem'):
    attrs = f'msg_id="{message_id}"'
    if chat:
        attrs += f' chat_id="{chat}"'
    if self_message:
        attrs += ' is_self_message="true"'
    return {'item_type': kind, 'meta': {'item_id': 'context-' + message_id},
            'parts': [{'text': '<message ' + attrs + '>\n'}, {'text': body}]}


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
        self.assertTrue(self.observe(message_id='124'))
        self.assertFalse(self.observe(message_id='125'))
        self.assertEqual(len(self.v.ids), 2)

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
