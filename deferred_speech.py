"""Bounded, volatile speech admission from exact host reply delivery receipts."""
import time
from collections import OrderedDict


class DeferredSpeech:
    def __init__(self):
        self.pending = OrderedDict()
        self.completed = OrderedDict()
        self.delivered = OrderedDict()

    def prune(self):
        now = time.monotonic()
        for table in [self.pending, self.completed, self.delivered]:
            for key, value in list(table.items()):
                expires = value['expires'] if isinstance(value, dict) else value
                if expires <= now:
                    table.pop(key, None)
        for table in [self.completed, self.delivered]:
            while len(table) > 4096:
                table.popitem(last=False)

    def register(self, session, reply, call, speech):
        self.prune()
        if not all(isinstance(value, str) and 0 < len(value) <= 256 for value in [session, reply, call]):
            return False
        key = (session, reply)
        if key in self.pending or key in self.completed or len(self.pending) >= 32:
            return False
        self.pending[key] = {'call': call, 'indices': {index for index, _ in speech},
                             'expires': time.monotonic() + 300}
        return True

    def claim(self, session, reply, call, receipts):
        self.prune()
        key = (session, reply)
        entry = self.pending.get(key)
        if not entry or call != entry['call']:
            return []
        # Consume before validation/async work: no retry even after ambiguous data.
        self.pending.pop(key)
        self.completed[key] = time.monotonic() + 3600
        if not isinstance(receipts, list) or not 0 < len(receipts) <= 64:
            return []
        indices, ids, selected = set(), set(), []
        for item in receipts:
            if not isinstance(item, dict):
                return []
            index, success = item.get('index'), item.get('success')
            mid, text = item.get('message_id', ''), item.get('text', '')
            if (type(index) is not int or index < 0 or index in indices or type(success) is not bool
                    or not isinstance(mid, str) or len(mid) > 256 or not isinstance(text, str) or len(text) > 6000):
                return []
            indices.add(index)
            if not success or not mid or not text.strip() or index not in entry['indices']:
                continue
            identity = (session, mid)
            if identity in ids or identity in self.delivered:
                return []
            ids.add(identity)
            selected.append((index, text.strip()))
        for identity in ids:
            self.delivered[identity] = time.monotonic() + 3600
        self.prune()
        return sorted(selected)
