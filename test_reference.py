import base64
import io
import math
from pathlib import Path
import shutil
import struct
import tempfile
import wave
from unittest import IsolatedAsyncioTestCase

from audio import SpeechEngine
from plugin import Config


def wav(seconds):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as file:
        file.setnchannels(1); file.setsampwidth(2); file.setframerate(24000)
        file.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(i / 24000 * 440 * 2 * math.pi)))
                                   for i in range(int(seconds * 24000))))
    return stream.getvalue()


class ReferenceTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        binary = shutil.which('ffmpeg')
        if not binary:
            try:
                import imageio_ffmpeg
                binary = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError:
                self.skipTest('requires ffmpeg or imageio-ffmpeg')
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = Config().model_dump()
        self.config['voice'].update(voice_dir=str(self.root), ffmpeg_path=binary)
        self.engine = SpeechEngine(self.root / 'runtime')

    async def asyncTearDown(self):
        await self.engine.close()
        self.temp.cleanup()

    async def test_select_longer_and_preserve_files(self):
        short, long = wav(.4), wav(1.2)
        (self.root / 'a.wav').write_bytes(short)
        (self.root / 'b.wav').write_bytes(long)
        uri = await self.engine.reference(self.config)
        with wave.open(io.BytesIO(base64.b64decode(uri.split(',')[1]))) as output:
            self.assertEqual(output.getnchannels(), 1)
            self.assertEqual(output.getframerate(), 24000)
        self.assertEqual((self.root / 'a.wav').read_bytes(), short)
        self.assertEqual((self.root / 'b.wav').read_bytes(), long)
        self.config['voice']['preferred_reference_file'] = 'a.wav'
        smaller = await self.engine.reference(self.config)
        self.assertLess(len(smaller), len(uri))

    async def test_merge_and_cleanup(self):
        audio = await self.engine.merge([wav(.2), wav(.3)], self.config)
        with wave.open(io.BytesIO(audio)) as output:
            self.assertAlmostEqual(output.getnframes() / output.getframerate(), .5, places=2)
        self.assertEqual(list((self.root / 'runtime').iterdir()), [])
