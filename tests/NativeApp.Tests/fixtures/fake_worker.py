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
    if mode == 'global-search':
        payload = req.get('payload', {})
        if req['command'] == 'search_messages':
            offset = payload.get('offset', 0)
            message_id = 'msg-1' if offset == 0 else 'msg-2'
            hit_id = 'hit-1' if offset == 0 else 'hit-2'
            result = {
                'searchId': 'search-1',
                'sourceSessionId': payload.get('sourceSessionId'),
                'items': [{
                    'hitId': hit_id, 'conversationId': 'conv-1', 'conversationTitle': '合成群聊',
                    'conversationKind': 'group', 'messageId': message_id, 'sender': '合成成员',
                    'timestamp': '2026-11-25T12:00:00+08:00', 'kind': 'text',
                    'snippet': '合成命中内容', 'matchedFields': ['content']
                }],
                'total': 2, 'resultLimit': 1000, 'truncated': False,
                'nextOffset': 1 if offset == 0 else None,
                'coverage': {'complete': True}, 'requestEcho': payload,
            }
        elif req['command'] == 'get_message_context':
            result = {
                'conversation': {'id': 'conv-1', 'title': '合成群聊', 'kind': 'group', 'messageCount': 3},
                'items': [
                    {'id': 'before-1', 'sender': '合成成员', 'timestamp': '2026-11-25T11:59:00+08:00', 'content': '前一条', 'kind': 'text'},
                    {'id': 'msg-1', 'sender': '合成成员', 'timestamp': '2026-11-25T12:00:00+08:00', 'content': '合成命中内容', 'kind': 'text', 'attachmentPath': 'C:/must-not-be-opened.png'},
                    {'id': 'after-1', 'sender': '合成成员', 'timestamp': '2026-11-25T12:01:00+08:00', 'content': '后一条', 'kind': 'text'}
                ],
                'focusIndex': 1, 'hasEarlier': True, 'hasLater': True,
                'requestEcho': payload,
            }
        else:
            result = {'requestEcho': payload}
        envelope = {'protocolVersion': 1, 'requestId': req['requestId'], 'type': 'response', 'ok': True, 'result': result}
        print(json.dumps(envelope, ensure_ascii=False), flush=True)
        if req['command'] == 'shutdown':
            break
        continue
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
