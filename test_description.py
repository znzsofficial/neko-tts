import ast
from pathlib import Path
import unittest


class VoiceDescriptionTests(unittest.TestCase):
    def test_voice_selection_description_is_short_with_unchanged_empty_parameters(self):
        tree = ast.parse(
            Path(__file__).with_name("plugin.py").read_text(encoding="utf-8")
        )
        extension = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ReplyExtension"
        )
        kwargs = {
            kw.arg: ast.literal_eval(kw.value)
            for kw in extension.keywords
            if kw.arg in {"description", "parameters"}
        }
        self.assertLessEqual(len(kwargs["description"]), 45)
        self.assertIn("语音", kwargs["description"])
        self.assertEqual(
            kwargs["parameters"],
            {"type": "object", "properties": {}, "additionalProperties": False},
        )
