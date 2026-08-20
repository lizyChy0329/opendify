"""Mock 端到端测试：模拟 Dify 分片流式返回 pi 场景的 Claude Code 风格工具调用，
验证中转站输出为原生 tool_calls；并覆盖 <invoke> 原始 XML 泄漏场景。"""
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

# 泄漏场景：按请求 inputs.scenario 分派
SCENARIOS = {
    # 无根标签的裸 <invoke>（完整闭合）
    "bare_xml": '<invoke name="bash"><parameter name="command">echo ok</parameter></invoke>',
    # Anthropic antml 变体
    "antml": '<antml:function_calls><antml:invoke name="bash"><antml:parameter name="command">echo ok</antml:parameter></antml:invoke></antml:function_calls>',
    # 流被截断的半截标签
    "partial": 'text <tool_calls><invoke name="bash"',
}

# 非流式场景的 answer（blocking 响应）
BLOCKING_ANSWERS = {
    # 未闭合的裸 invoke → 解析失败，走剥离清洗
    "blocking_xml": 'prefix <invoke name="bash"><parameter name="command">ls',
    # 完整闭合的裸 invoke → 解析成功，转原生 tool_calls
    "blocking_bare": 'prefix <invoke name="bash"><parameter name="command">ls</parameter></invoke> suffix',
}


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
        req = json.loads(self.rfile.read(length) or b'{}')
        scenario = (req.get('inputs') or {}).get('scenario', '')

        # 非流式阻塞场景
        if scenario in BLOCKING_ANSWERS:
            body = json.dumps({
                "answer": BLOCKING_ANSWERS[scenario],
                "message_id": "msg_mock_block",
                "conversation_id": "conv_1",
            }, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        content = SCENARIOS.get(scenario, PI_CONTENT)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        # 按 2-4 字符分片模拟流式
        buf = ''
        for ch in content:
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

TOOLS = [{
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
}]


def run_stream(scenario, with_tools=True):
    """发起流式请求，返回 (content, tool_calls, done)"""
    body = {
        'model': 'test',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'inputs': {'scenario': scenario},
        'stream': True,
    }
    if with_tools:
        body['tools'] = TOOLS
    resp = client.post('/v1/chat/completions', json=body, headers={'Authorization': 'Bearer sk-abc123'})
    content_parts = []
    tool_calls = []
    done = False
    for raw in resp.response:
        line = raw.decode()
        if not line.startswith('data: '):
            continue
        data = line[6:].strip()
        if data == '[DONE]':
            done = True
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
            tool_calls.extend(delta['tool_calls'])
        if delta.get('content'):
            content_parts.append(delta['content'])
    return ''.join(content_parts), tool_calls, done


# ============ 既有 happy path：DSML 流式 → 原生 tool_calls ============
resp = client.post('/v1/chat/completions',
    json={
        'model': 'test',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': TOOLS,
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

# ============ 泄漏场景校验 ============
print()
print('=== <invoke> 泄漏场景校验 ===')
leak_ok = True


def check(cond, msg):
    global leak_ok
    if cond:
        print(f'  ✓ {msg}')
    else:
        print(f'  ✗ {msg}')
        leak_ok = False


# 单元：解析器
bare = '<invoke name="bash"><parameter name="command">echo ok</parameter></invoke>'
antml = '<antml:function_calls><antml:invoke name="bash"><antml:parameter name="command">echo ok</antml:parameter></antml:invoke></antml:function_calls>'
partial = 'text <tool_calls><invoke name="bash"'
tc = main.parse_tool_calls_any(bare)
check(tc and tc[0]['name'] == 'bash' and tc[0]['arguments'] == {'command': 'echo ok'},
      f'裸 <invoke> 解析为原生 tool_calls: {tc}')
tc = main.parse_tool_calls_any(antml)
check(tc and tc[0]['name'] == 'bash' and tc[0]['arguments'] == {'command': 'echo ok'},
      f'<antml:*> 变体解析为原生 tool_calls: {tc}')
check(main.parse_tool_calls_any(partial) is None, '截断标签返回 None（流式继续累积）')

# 单元：清洗器
check(main.strip_tool_call_markup(bare) == '', '闭合裸 invoke 被清空')
check(main.strip_tool_call_markup(partial) == 'text', f'悬空截断保留前缀: {main.strip_tool_call_markup(partial)!r}')
check(main.strip_tool_call_markup(antml) == '', 'antml 串被清空')
cleaned = main.strip_tool_call_markup('prefix <invoke name="x">raw</invoke> suffix')
check('prefix' in cleaned and 'suffix' in cleaned and '<invoke' not in cleaned, f'正文保留、XML 剥离: {cleaned!r}')

# 流式：bare_xml 带 tools → 原生 tool_calls，无 XML 泄漏
content, tcs, done = run_stream('bare_xml', with_tools=True)
check(tcs and tcs[0].get('function', {}).get('name') == 'bash', f'bare_xml 带 tools 发出 tool_calls: {tcs}')
check('<invoke' not in content and '</invoke>' not in content and '<tool_calls' not in content,
      f'bare_xml content 无 XML 泄漏: {content!r}')

# 流式：antml 带 tools → 原生 tool_calls
content, tcs, done = run_stream('antml', with_tools=True)
check(tcs and tcs[0].get('function', {}).get('name') == 'bash', f'antml 带 tools 发出 tool_calls: {tcs}')
check('<invoke' not in content and '<tool_calls' not in content, f'antml content 无 XML 泄漏: {content!r}')

# 流式：partial 带 tools → 无 XML 泄漏，正常结束
content, tcs, done = run_stream('partial', with_tools=True)
check('<invoke' not in content and '<tool_calls' not in content, f'partial content 无 XML 泄漏: {content!r}')
check(done, 'partial 流以 [DONE] 结束')

# 流式：bare_xml 不带 tools → 仍转原生 tool_calls，无 XML 泄漏
content, tcs, done = run_stream('bare_xml', with_tools=False)
check(tcs and tcs[0].get('function', {}).get('name') == 'bash', f'无 tools bare_xml 仍发出 tool_calls: {tcs}')
check('<invoke' not in content and '</invoke>' not in content and '<tool_calls' not in content,
      f'无 tools content 无 XML 泄漏: {content!r}')

# 流式：DSML 不带 tools（用户实际场景）→ 转原生 tool_calls
content, tcs, done = run_stream('', with_tools=False)
check(len(tcs) == 2, f'无 tools DSML 仍发出 2 个 tool_calls: {tcs}')
check('<invoke' not in content and '<tool_calls' not in content, f'无 tools DSML content 无 XML 泄漏: {content!r}')

# 非流式：blocking_xml（未闭合）→ 正文保留，无 XML
resp = client.post('/v1/chat/completions',
    json={
        'model': 'test',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'inputs': {'scenario': 'blocking_xml'},
        'stream': False,
    },
    headers={'Authorization': 'Bearer sk-abc123'})
data = json.loads(resp.get_data())
content = data['choices'][0]['message']['content']
check('prefix' in content and '<invoke' not in content and '</invoke>' not in content,
      f'blocking 未闭合 invoke 剥离、正文保留: {content!r}')

# 非流式：blocking_bare（完整闭合）→ 转原生 tool_calls
resp = client.post('/v1/chat/completions',
    json={
        'model': 'test',
        'messages': [{'role': 'user', 'content': 'hi'}],
        'inputs': {'scenario': 'blocking_bare'},
        'stream': False,
    },
    headers={'Authorization': 'Bearer sk-abc123'})
data = json.loads(resp.get_data())
msg = data['choices'][0]['message']
tcs = msg.get('tool_calls') or []
check(tcs and tcs[0]['function']['name'] == 'bash', f'blocking 完整 invoke 转原生 tool_calls: {tcs}')
check(msg.get('content') is None, 'blocking 完整 invoke 时 content 为 None')

ok = ok and leak_ok
print(f'  {"✓✓ 端到端通过" if ok else "✗ 存在失败"}')
srv.shutdown()
sys.exit(0 if ok else 1)