from __future__ import annotations

import base64
import gzip
from pathlib import Path

payload = Path(__file__).with_name('optimized_payload.b64').read_text(encoding='utf-8').strip()
source = gzip.decompress(base64.b64decode(payload)).decode('utf-8')
exec(compile(source, str(Path(__file__).with_name('run_optimized.py')), 'exec'), {'__name__': '__main__', '__file__': str(Path(__file__).with_name('run_optimized.py'))})
