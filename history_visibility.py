"""Hide only positively identified TTS receipts from model prompt Items."""
import hashlib
import base64
import html
import json
import os
import re
import tempfile
import time
import math
from collections import OrderedDict
from pathlib import Path


class VoiceVisibility:
    MAX_IDS = 4096
    MAX_PENDING = 256
    TTL = 7 * 86400
    PENDING_TTL = 300

    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else None
        self.clock = clock
        self.ids = OrderedDict()
        self.pending = OrderedDict()
        self.healthy_file = True

    def load(self):
        if self.path is None or not self.path.exists():
            return
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('receipt cache too large')
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(raw, list) or len(raw) > self.MAX_IDS:
                raise ValueError('invalid receipt cache')
            for row in raw:
                if (not isinstance(row, list) or len(row) != 3 or
                        not isinstance(row[0], str) or not 0 < len(row[0]) <= 128 or
                        not isinstance(row[1], str) or not 0 < len(row[1]) <= 256 or
                        isinstance(row[2], bool) or not isinstance(row[2], (float, int)) or not math.isfinite(row[2])):
                    raise ValueError('invalid receipt cache')
                if row[2] > self.clock():
                    self.ids[(row[0], row[1])] = row[2]
        except (OSError, ValueError, UnicodeError):
            self.ids.clear()
            self.healthy_file = False
            raise ValueError('语音回执缓存无法读取，已保留原文件') from None

    def remember_audio(self, scope, audio):
        if not scope:
            return
        self._prune()
        digest = hashlib.sha256(audio).hexdigest()
        key = (scope, digest)
        expires, count = self.pending.get(key, (0, 0))
        self.pending[key] = (max(expires, self.clock() + self.PENDING_TTL), count + 1)
        self.pending.move_to_end(key)
        while len(self.pending) > self.MAX_PENDING:
            self.pending.popitem(last=False)

    def observe(self, scope, message, sent):
        """Match outgoing pure voice bytes/hash, not a text or time-window guess."""
        if not scope or not sent or not isinstance(message, dict):
            return False
        segments = message.get('raw_message')
        if not isinstance(segments, list) or len(segments) != 1:
            return False
        segment = segments[0]
        if not isinstance(segment, dict) or segment.get('type') != 'voice':
            return False
        digest = segment.get('hash', '')
        if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
            return False
        message_id = message.get('message_id')
        if not isinstance(message_id, str) or not 0 < len(message_id) <= 256:
            return False
        self._prune()
        key = (scope, digest)
        entry = self.pending.get(key)
        if entry is None:
            return False
        expires, count = entry
        if count == 1:
            del self.pending[key]
        else:
            self.pending[key] = (expires, count - 1)
        self.ids[(scope, message_id)] = self.clock() + self.TTL
        self.ids.move_to_end((scope, message_id))
        while len(self.ids) > self.MAX_IDS:
            self.ids.popitem(last=False)
        return True

    def _prune(self):
        for key, expires in list(self.ids.items()):
            if expires <= self.clock():
                del self.ids[key]
        for key, (expires, _) in list(self.pending.items()):
            if expires <= self.clock():
                del self.pending[key]

    def save(self):
        if self.path is None or not self.healthy_file:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [[scope, message_id, expires] for (scope, message_id), expires in self.ids.items()]
        fd, name = tempfile.mkstemp(prefix='.voice-receipts-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                os.chmod(name, 0o600)
                json.dump(rows, stream, ensure_ascii=False)
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def filter_items(self, items, scope):
        if not isinstance(items, list) or not scope:
            return items
        self._prune()
        aliases = set()
        for chat, message_id in self.ids:
            if len(message_id) > 12:
                alias = 'm' + base64.b32encode(hashlib.sha1(message_id.encode('utf-8')).digest()).decode('ascii').lower()[:6]
                aliases.add((chat, alias))
        kept = []
        for item in items:
            hidden = False
            if isinstance(item, dict) and item.get('item_type') == 'UserMessageItem':
                parts = item.get('parts')
                first = parts[0] if isinstance(parts, list) and parts else None
                text = first.get('text') if isinstance(first, dict) else None
                # Only the Host-generated first message prefix. Do not scan
                # quoted text, tool results, user bodies or attachment captions.
                prefix = re.match(r'^<message\s+([^>]+)>\n', text) if isinstance(text, str) else None
                if prefix:
                    attrs = dict(re.findall(r'([a-z_]+)="([^"]*)"', prefix.group(1)))
                    message_id = html.unescape(attrs.get('msg_id', ''))
                    chat = html.unescape(attrs.get('chat_id', '')) or scope
                    hidden = (chat, message_id) in self.ids
                    if not hidden and attrs.get('is_self_message') == 'true':
                        hidden = (chat, message_id) in aliases
                        # Historical self-voice placeholders contain no speech
                        # text to remember. Do not match arbitrary user bodies.
                        if not hidden and len(parts) == 2:
                            body = parts[1].get('text') if isinstance(parts[1], dict) else None
                            hidden = body == '[语音消息]'
            if not hidden:
                kept.append(item)
        return kept
