"""Mock 端到端测试：模拟 Dify 分片流式返回 pi 场景的 Claude Code 风格工具调用，
验证中转站输出为原生 tool_calls。"""
import os
import sys
import json
import time
import threading
import http.server
import socketserver

# 先设置环境变量，再 import main（模块加载时读取）
os.environ['DIFY_API_BASE'] = 'http://127.0.0.1:5999/v1'
os.environ['DIFY_API_KEYS'] = 'app-mock-key'
os.environ['VALID_API_KEYS'] = 'sk-abc123'
os.environ['SERVER_HOST'] = '127.0.0.1'
os.environ['SERVER_PORT'] = '5998'

PI_CONTENT = '''<｜tool_calls｜>
<｜invoke name="bash｜>
<｜parameter name="command" string="true｜>cd /home/wsl-ubuntu24/code/actis && find src -type f -name ".ts" -o -type f -name ".vue" | head -50 && echo "---PACKAGE---" && cat package.json</｜parameter｜>
</｜invoke｜>
<｜invoke name="bash｜>
<｜parameter name="command" string="true｜>cd /home/wsl-ubuntu24/code/actis && git log --oneline -30 && echo "---STATUS---" && git status --short</｜parameter｜>
</｜invoke｜>
</｜tool_calls｜>'''


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        # /info 返回应用名 test
        if self.path.endswith('/info'):
            body = json.dumps({"name": "test"}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        # 按 2-4 字符分片模拟流式
        buf = ''
        for ch in PI_CONTENT:
            buf += ch
            if len(buf) >= 3:
                payload = json.dumps({
                    "event": "message",
                    "message_id": "msg_mock_001",
                    "answer": buf,
                }, ensure_ascii=False)
                self.wfile.write(f'data: {payload}\n\n'.encode())
                self.wfile.flush()
                buf = ''
                time.sleep(0.002)
        if buf:
            payload = json.dumps({
                "event": "message",
                "message_id": "msg_mock_001",
                "answer": buf,
            }, ensure_ascii=False)
            self.wfile.write(f'data: {payload}\n\n'.encode())
            self.wfile.flush()
        # message_end
        self.wfile.write(b'data: {"event":"message_end","message_id":"msg_mock_001","metadata":{}}\n\n')
        self.wfile.flush()

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(('127.0.0.1', 5999), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)

import main

# 确保映射存在（mock /info 返回 name=test）
main.model_manager.name_to_api_key['test'] = 'app-mock-key'
main.model_manager.api_key_to_name['app-mock-key'] = 'test'

client = main.app.test_client()
resp = client.post('/v1/chat/completions',
    json={
        'model': 'test',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{
            'type': 'function',
            'function': {
                'name': 'bash',
                'description': 'Run a shell command',
                'parameters': {
                    'type': 'object',
                    'properties': {'command': {'type': 'string'}},
                    'required': ['command'],
                },
            },
        }],
        'stream': True,
    },
    headers={'Authorization': 'Bearer sk-abc123'})

print('=== 中转站流式输出（完整）===')
tool_calls_found = []
for raw in resp.response:
    line = raw.decode()
    if not line.startswith('data: '):
        continue
    data = line[6:].strip()
    if data == '[DONE]':
        continue
    try:
        chunk = json.loads(data)
    except Exception:
        continue
    choices = chunk.get('choices', [])
    if not choices:
        continue
    delta = choices[0].get('delta', {})
    if 'tool_calls' in delta:
        for tc in delta['tool_calls']:
            tool_calls_found.append(tc)
            print(f'  [tool_call] index={tc.get("index")} name={tc.get("function", {}).get("name")} args={tc.get("function", {}).get("arguments", "")!r}')
    elif delta.get('content'):
        print(f'  [content] {delta["content"]!r}')
    elif delta.get('reasoning_content'):
        pass
    if choices[0].get('finish_reason'):
        print(f'  [finish] {choices[0]["finish_reason"]}')

print()
print('=== 工具调用完整还原 ===')
# 合并分片
merged = {}
for tc in tool_calls_found:
    idx = tc.get('index', 0)
    fn = tc.get('function', {})
    entry = merged.setdefault(idx, {'name': '', 'args': ''})
    if fn.get('name'):
        entry['name'] = fn['name']
    if fn.get('arguments'):
        entry['args'] += fn['arguments']
for idx in sorted(merged):
    e = merged[idx]
    print(f'  call[{idx}]: name={e["name"]!r}')
    print(f'           arguments={e["args"]!r}')

print()
print('=== 校验 ===')
ok = True
if len(merged) != 2:
    print(f'  ✗ 期望 2 个 tool_calls，实际 {len(merged)}')
    ok = False
else:
    for idx in sorted(merged):
        e = merged[idx]
        args = json.loads(e['args'])
        cmd = args.get('command', '')
        if 'find src -type f' in cmd or 'git log' in cmd:
            print(f'  ✓ call[{idx}] 命令完整: {cmd[:60]}...')
        else:
            print(f'  ✗ call[{idx}] 命令异常: {cmd!r}')
            ok = False
        if '| head' in cmd or '---STATUS---' in cmd:
            print(f'  ✓ call[{idx}] 管道符/内嵌引号保留')
        else:
            print(f'  ✗ call[{idx}] 管道符或引号丢失')
            ok = False
print(f'  {"✓✓ 端到端通过" if ok else "✗ 存在失败"}')
srv.shutdown()
sys.exit(0 if ok else 1)
