"""Test-only pipe peer. Never shipped with the application."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

mode = sys.argv[1]
if mode == 'spawn':
    # Stands in for Weixin.exe: a long-lived child of the worker. The test
    # verifies that a worker timeout does not kill it.
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    root = os.environ.get('WECHAT_MEMORY_TEMP_ROOT')
    if root:
        Path(root, 'child.pid').write_text(str(child.pid), encoding='ascii')
for line in sys.stdin:
    req = json.loads(line)
    if mode == 'exit':
        sys.exit(17)
    if mode == 'hang' or mode == 'spawn':
        time.sleep(60)
    if mode == 'noisy':
        for _ in range(2048):
            print('diagnostic' * 80, file=sys.stderr)
        sys.stderr.flush()
    envelope = {'protocolVersion': 1, 'requestId': req['requestId'], 'type': 'response', 'ok': True, 'result': {'hello': '中文 😀'}}
    if mode == 'mismatch':
        envelope['requestId'] = 'wrong'
    if mode == 'error':
        envelope.update(ok=False, error={'code': 'OUTPUT_NOT_WRITABLE', 'message': '输出位置不可写。', 'retryable': False})
    if mode == 'badjson':
        print('not a protocol message', flush=True)
        continue
    if mode == 'progress':
        print(json.dumps(dict(envelope, type='progress')), flush=True)
    print(json.dumps(envelope, ensure_ascii=False), flush=True)
    if req['command'] == 'shutdown':
        break
