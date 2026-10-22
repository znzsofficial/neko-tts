import unittest
from focus_scope import planner_scopes
from history_visibility import VoiceVisibility, ReplyReceiptVisibility
from test_history_visibility import item, receipt


class FocusVisibility(unittest.TestCase):
    def test_routed_scope_required_bounded_and_not_guessed_from_text(self):
        def tools(scopes):
            return [{'type': 'function', 'function': {'name': 'reply', 'parameters': {
                'required': ['session_id'], 'properties': {'session_id': {'type': 'string', 'enum': scopes}}}}}]
        self.assertEqual(planner_scopes('', tools(['a', 'b'])), ('a', 'b'))
        for scopes in [[], ['a'] * 33, [None], ['a\nb']]:
            self.assertEqual(planner_scopes('', tools(scopes)), ())
        self.assertEqual(planner_scopes('', tools(['a']) * 2), ())
        self.assertEqual(planner_scopes('', None), ())

    def test_shared_prefix_supports_session_id_without_cross_group_message_id_collision(self):
        voice = VoiceVisibility(clock=lambda: 100)
        voice.ids[('a', 'same-id')] = 1000
        a, b = item('same-id', chat='a'), item('same-id', chat='b')
        a['parts'][0]['text'] = a['parts'][0]['text'].replace('chat_id=', 'session_id=')
        self.assertEqual(voice.filter_items([a, b], 'a', shared=True), [b])
        self.assertEqual(voice.filter_items([a, b], 'b', shared=True), [a, b])

    def test_shared_unscoped_assistant_other_voice_and_contradictory_prefix_preserved(self):
        voice = VoiceVisibility(clock=lambda: 100)
        values = [item('u'), item('self', chat='a', self_message=True),
                  {'item_type': 'AssistantMessageItem', 'parts': [{'type': 'text', 'text': '[语音消息]'}]}]
        conflict = item('same', chat='a'); conflict['parts'][0]['text'] = conflict['parts'][0]['text'].replace('>\n', ' session_id="b">\n')
        values.append(conflict)
        voice.ids[('a', 'same')] = 1000
        self.assertEqual(voice.filter_items(values, 'a', shared=True), values)

    def test_shared_reply_receipt_requires_matching_call_target_and_keeps_pairs(self):
        visibility = ReplyReceiptVisibility(clock=lambda: 100)
        visibility.remember('a', 'call-1', 1, 'text_and_voice')
        output = receipt('正文[语音消息]')
        call = {'item_type': 'FunctionCallItem', 'tool_call': {'func_name': 'reply', 'call_id': 'call-1', 'args': {'session_id': 'a'}}}
        self.assertEqual(visibility.filter_items([output], 'a', shared=True), [output])
        self.assertEqual(visibility.filter_items([call, output], 'b', shared=True), [call, output])
        kept = visibility.filter_items([call, output], 'a', shared=True)
        self.assertEqual(kept[0], call)
        self.assertEqual(kept[1]['call_id'], output['call_id'])
        self.assertNotIn('[语音消息]', kept[1]['output'])
        call['tool_call']['args']['session_id'] = 'b'
        self.assertEqual(visibility.filter_items([call, output], 'a', shared=True), [call, output])

    def test_conflicting_call_ids_do_not_authorize_shared_receipt_rewrite(self):
        visibility = ReplyReceiptVisibility(clock=lambda: 100)
        visibility.remember('a', 'same', 1, 'text_and_voice')
        def call(scope):
            return {'item_type': 'FunctionCallItem', 'tool_call': {'func_name': 'reply', 'call_id': 'same', 'args': {'session_id': scope}}}
        values = [call('a'), call('b'), receipt('正文[语音消息]', call_id='same')]
        self.assertEqual(visibility.filter_items(values, 'a', shared=True), values)
