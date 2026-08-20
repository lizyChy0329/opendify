import json
import logging
import asyncio
import codecs
import re
import hashlib
import threading
from flask import Flask, request, Response, stream_with_context, jsonify
import httpx
import time
from dotenv import load_dotenv
import os

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 设置httpx的日志级别为WARNING，减少不必要的输出
logging.getLogger("httpx").setLevel(logging.WARNING)

# 加载环境变量
load_dotenv()

# 从环境变量读取有效的API密钥（逗号分隔）
VALID_API_KEYS = [key.strip() for key in os.getenv("VALID_API_KEYS", "").split(",") if key]

# 获取会话记忆功能模式配置
# 1: 构造history_message附加到消息中的模式(默认)
# 2: 零宽字符模式
# 3: session-map 模式（session key -> Dify conversation_id 映射，上下文真正延续）
CONVERSATION_MEMORY_MODE = int(os.getenv('CONVERSATION_MEMORY_MODE', '1'))

# ================= session-map 会话延续组件 (MODE=3) =================
# 三级回退 session key：X-Session-Id header -> 请求体 user 字段 -> 首条 user 消息 hash
SESSION_MAP_TTL_HOURS = float(os.getenv('SESSION_MAP_TTL_HOURS', '24'))
SESSION_MAP_MAX_ENTRIES = int(os.getenv('SESSION_MAP_MAX_ENTRIES', '5000'))
SESSION_MAP_JSON = os.getenv('SESSION_MAP_JSON', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'session_map.json'))

_session_map_lock = threading.Lock()
session_map = {}  # session_key -> {"conversation_id": str, "created_at": float, "last_used_at": float}
_last_session_map_flush = 0.0


def load_session_map():
    """启动时从 JSON 文件加载 session 映射（重启恢复）"""
    global session_map
    try:
        if os.path.exists(SESSION_MAP_JSON):
            with open(SESSION_MAP_JSON, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                with _session_map_lock:
                    session_map = data
                logger.info(f"Loaded {len(session_map)} session entries from {SESSION_MAP_JSON}")
    except Exception as e:
        logger.warning(f"Failed to load session map: {e}")


def flush_session_map(force=False):
    """将 session 映射落盘（防抖：默认最多每 5 秒写一次）"""
    global _last_session_map_flush
    now = time.time()
    if not force and now - _last_session_map_flush < 5:
        return
    try:
        with _session_map_lock:
            data = dict(session_map)
        _last_session_map_flush = now
        with open(SESSION_MAP_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f"Session map flushed ({len(data)} entries)")
    except Exception as e:
        logger.warning(f"Failed to flush session map: {e}")


def cleanup_session_map():
    """TTL 过期清理 + 容量上限 LRU 淘汰"""
    now = time.time()
    expired = []
    with _session_map_lock:
        for key, entry in list(session_map.items()):
            if now - entry.get("last_used_at", entry.get("created_at", now)) > SESSION_MAP_TTL_HOURS * 3600:
                expired.append(key)
        for key in expired:
            del session_map[key]
        # 超出上限：按 last_used_at 升序淘汰最旧
        if len(session_map) > SESSION_MAP_MAX_ENTRIES:
            overflow = len(session_map) - SESSION_MAP_MAX_ENTRIES
            oldest = sorted(session_map.items(), key=lambda kv: kv[1].get("last_used_at", 0))[:overflow]
            for key, _ in oldest:
                del session_map[key]
    if expired:
        logger.info(f"Session map cleanup: removed {len(expired)} expired entries")


def compute_session_key(openai_request, headers=None):
    """三级回退计算 session key：
    1. X-Session-Id 请求头（客户端显式指定）
    2. 请求体 user 字段
    3. 首条非 system user 消息内容 hash（零客户端改动，pi/opencode 每次带全量历史故首条恒定）
    """
    if headers:
        sid = headers.get('X-Session-Id')
        if sid:
            return f"h:{sid}"
    user = openai_request.get("user")
    if user and user != "default_user":
        return f"u:{user}"
    messages = openai_request.get("messages", [])
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            if content:
                return "m:" + hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    return None


def session_map_get(session_key):
    """取 conversation_id 并刷新 last_used_at"""
    if not session_key:
        return None
    with _session_map_lock:
        entry = session_map.get(session_key)
        if entry:
            entry["last_used_at"] = time.time()
            return entry.get("conversation_id")
    return None


def session_map_set(session_key, conversation_id):
    """回写 conversation_id 并落盘"""
    if not session_key or not conversation_id:
        return
    now = time.time()
    with _session_map_lock:
        entry = session_map.get(session_key)
        if entry:
            entry["conversation_id"] = conversation_id
            entry["last_used_at"] = now
        else:
            session_map[session_key] = {
                "conversation_id": conversation_id,
                "created_at": now,
                "last_used_at": now
            }
    cleanup_session_map()
    flush_session_map()


def session_map_pop(session_key):
    """删除 session 映射（conversation 404 失效时）"""
    if not session_key:
        return
    with _session_map_lock:
        session_map.pop(session_key, None)
    flush_session_map(force=True)


# 启动时恢复持久化映射
load_session_map()

class DifyModelManager:
    def __init__(self):
        self.api_keys = []
        self.name_to_api_key = {}  # 应用名称到API Key的映射
        self.api_key_to_name = {}  # API Key到应用名称的映射
        self.load_api_keys()

    def load_api_keys(self):
        """从环境变量加载API Keys"""
        api_keys_str = os.getenv('DIFY_API_KEYS', '')
        if api_keys_str:
            self.api_keys = [key.strip() for key in api_keys_str.split(',') if key.strip()]
            logger.info(f"Loaded {len(self.api_keys)} API keys")

    async def fetch_app_info(self, api_key):
        """获取Dify应用信息"""
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                response = await client.get(
                    f"{DIFY_API_BASE}/info",
                    headers=headers,
                    params={"user": "default_user"}
                )
                
                if response.status_code == 200:
                    app_info = response.json()
                    return app_info.get("name", "Unknown App")
                else:
                    logger.error(f"Failed to fetch app info for API key: {api_key[:8]}...")
                    return None
        except Exception as e:
            logger.error(f"Error fetching app info: {str(e)}")
            return None

    async def refresh_model_info(self):
        """刷新所有应用信息"""
        self.name_to_api_key.clear()
        self.api_key_to_name.clear()
        
        for api_key in self.api_keys:
            app_name = await self.fetch_app_info(api_key)
            if app_name:
                self.name_to_api_key[app_name] = api_key
                self.api_key_to_name[api_key] = app_name
                logger.info(f"Mapped app '{app_name}' to API key: {api_key[:8]}...")

    def get_api_key(self, model_name):
        """根据模型名称获取API Key"""
        return self.name_to_api_key.get(model_name)

    def get_available_models(self):
        """获取可用模型列表"""
        return [
            {
                "id": name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "dify"
            }
            for name in self.name_to_api_key.keys()
        ]

# 创建模型管理器实例
model_manager = DifyModelManager()

# 从环境变量获取API基础URL
DIFY_API_BASE = os.getenv("DIFY_API_BASE", "")

app = Flask(__name__)

def get_api_key(model_name):
    """根据模型名称获取对应的API密钥"""
    api_key = model_manager.get_api_key(model_name)
    if not api_key:
        logger.warning(f"No API key found for model: {model_name}")
    return api_key

async def upload_image_to_dify(api_key, base64_data, user_id="default_user"):
    """上传图片到Dify并返回文件ID
    支持处理base64编码的图片数据，自动检测并提取有效的base64数据
    """
    try:
        # 解码base64数据
        if base64_data.startswith('data:image'):
            # 提取实际的base64数据 (去除data:image/*;base64,前缀)
            base64_data = base64_data.split(',')[1]
        
        import base64
        image_data = base64.b64decode(base64_data)
        
        # 创建临时文件
        import tempfile
        import os
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_file:
            tmp_file.write(image_data)
            tmp_file_path = tmp_file.name
        
        try:
            # 使用httpx上传文件到Dify
            async with httpx.AsyncClient(timeout=None) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}"
                }
                
                # 准备multipart数据用于文件上传
                # Dify当前仅支持图片类型附件的上传 (PNG, JPG, JPEG, WEBP, GIF)
                with open(tmp_file_path, 'rb') as file_handle:
                    files = {
                        'file': ('image.png', file_handle, 'image/png')
                    }
                    data = {
                        'user': user_id
                    }
                    
                    response = await client.post(
                        f"{DIFY_API_BASE}/files/upload",
                        headers=headers,
                        files=files,
                        data=data
                    )
                
                # 检查上传响应状态码
                # HTTP 200: OK, HTTP 201: Created
                if response.status_code in [200, 201]:
                    file_info = response.json()
                    logger.info(f"Successfully uploaded image, file_id: {file_info.get('id')}")
                    return file_info.get('id')
                else:
                    logger.error(f"Failed to upload image, status_code: {response.status_code}, response: {response.text}")
                    return None
                    
        except Exception as e:
            logger.error(f"Error uploading image: {str(e)}")
            return None
            
        finally:
            # 确保临时文件被清理，避免磁盘空间泄露
            if tmp_file_path and os.path.exists(tmp_file_path):
                try:
                    # 等待一小段时间确保文件句柄完全释放
                    import asyncio
                    await asyncio.sleep(0.1)
                    os.unlink(tmp_file_path)
                    logger.debug(f"Temporary file cleaned up: {tmp_file_path}")
                except Exception as cleanup_error:
                    logger.warning(f"Failed to cleanup temporary file {tmp_file_path}: {cleanup_error}")
                    # 如果立即删除失败，尝试延迟删除
                    try:
                        await asyncio.sleep(1)
                        if os.path.exists(tmp_file_path):
                            os.unlink(tmp_file_path)
                            logger.debug(f"Temporary file cleaned up after delay: {tmp_file_path}")
                    except Exception as delayed_cleanup_error:
                        logger.error(f"Failed to cleanup temporary file after delay {tmp_file_path}: {delayed_cleanup_error}")
            
    except Exception as e:
        logger.error(f"Error processing image data: {str(e)}")
        return None

async def transform_openai_to_dify(openai_request, endpoint, api_key=None, session_key=None, force_history_mode=False):
    """将OpenAI格式的请求转换为Dify格式"""
    
    if endpoint == "/chat/completions":
        messages = openai_request.get("messages", [])
        stream = openai_request.get("stream", False)
        user_id = openai_request.get("user", "default_user")
        inputs = openai_request.get("inputs", {})
        
        # 尝试从历史消息中提取conversation_id
        conversation_id = None
        
        # 提取system消息内容
        system_content = ""
        system_messages = [msg for msg in messages if msg.get("role") == "system"]
        if system_messages:
            system_content = system_messages[0].get("content", "")
            # 记录找到的system消息
            logger.info(f"Found system message: {system_content[:100]}{'...' if len(system_content) > 100 else ''}")
        
        # 注入工具定义（双向工具代理协议）
        tools_prompt = build_tools_prompt(openai_request.get("tools"))
        if tools_prompt:
            if system_content:
                system_content = system_content + "\n\n" + tools_prompt
            else:
                system_content = tools_prompt
            logger.info(f"Injected tools prompt ({len(tools_prompt)} chars) into system content")
        
        # 处理用户消息，支持图片
        user_message = messages[-1] if messages and messages[-1].get("role") != "system" else {}
        user_content = user_message.get("content", "")
        
        # 存储上传的文件ID
        uploaded_files = []
        
        # 检查用户消息是否包含图片
        if isinstance(user_content, list):
            # 处理多模态内容（文本+图片）
            text_parts = []
            image_parts = []
            
            for item in user_content:
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "image_url":
                    image_url = item.get("image_url", {}).get("url", "")
                    if image_url:
                        image_parts.append(image_url)
            
            # 组合文本内容
            user_query = "\n".join(text_parts) if text_parts else ""
            
            # 上传图片文件
            if api_key and image_parts:
                logger.info(f"Found {len(image_parts)} images to upload")
                successful_uploads = 0
                failed_uploads = 0
                
                for i, image_data in enumerate(image_parts):
                    try:
                        logger.info(f"Uploading image {i+1}/{len(image_parts)}")
                        file_id = await upload_image_to_dify(api_key, image_data, user_id)
                        if file_id:
                            uploaded_files.append({
                                "type": "image",
                                "transfer_method": "local_file",
                                "upload_file_id": file_id
                            })
                            successful_uploads += 1
                            logger.info(f"Successfully uploaded image {i+1}/{len(image_parts)}, file_id: {file_id}")
                        else:
                            failed_uploads += 1
                            logger.warning(f"Failed to upload image {i+1}/{len(image_parts)}")
                    except Exception as e:
                        failed_uploads += 1
                        logger.error(f"Exception occurred while uploading image {i+1}/{len(image_parts)}: {str(e)}")
                
                # 记录上传结果统计
                if successful_uploads > 0:
                    logger.info(f"Uploaded {successful_uploads}/{len(image_parts)} files successfully")
                if failed_uploads > 0:
                    logger.warning(f"Failed to upload {failed_uploads}/{len(image_parts)} files")
                
                # 如果所有图片都上传失败，记录警告
                if successful_uploads == 0 and failed_uploads > 0:
                    logger.warning("All image uploads failed, proceeding with text-only request")
        else:
            # 处理纯文本内容
            user_query = user_content
        
        logger.info(f"Processing request with {len(uploaded_files)} uploaded files")
        
        if CONVERSATION_MEMORY_MODE == 2:  # 零宽字符模式
            if len(messages) > 1:
                # 遍历历史消息，找到最近的assistant消息
                for msg in reversed(messages[:-1]):  # 除了最后一条消息
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        # 尝试解码conversation_id
                        conversation_id = decode_conversation_id(content)
                        if conversation_id:
                            break
            
            # 如果有system消息且是首次对话(没有conversation_id)，则将system内容添加到用户查询前
            if system_content and not conversation_id:
                user_query = f"系统指令: {system_content}\n\n用户问题: {user_query}"
                logger.info(f"[零宽字符模式] 首次对话，添加system内容到查询前")
            
            dify_request = {
                "inputs": inputs,
                "query": user_query,
                "response_mode": "streaming" if stream else "blocking",
                "conversation_id": conversation_id,
                "user": user_id
            }
            
            # 如果有上传的文件，添加到请求中
            if uploaded_files:
                dify_request["files"] = uploaded_files
                
        elif CONVERSATION_MEMORY_MODE == 3 and not force_history_mode:  # session-map 模式
            # 从 session 映射取 conversation_id（无则新建 conversation）
            conversation_id = session_map_get(session_key) if session_key else None
            
            # 首次对话（无 conversation_id）时，将 system 内容添加到查询前
            if system_content and not conversation_id:
                user_query = f"系统指令: {system_content}\n\n用户问题: {user_query}"
                logger.info(f"[session-map模式] 首次对话，添加system内容到查询前")
            
            dify_request = {
                "inputs": inputs,
                "query": user_query,
                "response_mode": "streaming" if stream else "blocking",
                "conversation_id": conversation_id,
                "user": user_id
            }
            
            # 如果有上传的文件，添加到请求中
            if uploaded_files:
                dify_request["files"] = uploaded_files
                
        else:  # history_message模式(默认) / MODE=3 的 404 回退（force_history_mode）
            # 构造历史消息
            if len(messages) > 1:
                history_messages = []
                has_system_in_history = False
                
                # 检查历史消息中是否已经包含system消息
                for msg in messages[:-1]:  # 除了最后一条消息
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    
                    # 工具执行结果消息：明确标记为工具结果
                    if role == "tool":
                        if isinstance(content, list):
                            content = json.dumps(content, ensure_ascii=False)
                        history_messages.append(f"工具执行结果: {content}")
                        continue
                    
                    # assistant 消息带 tool_calls：记录模型调用过哪些工具
                    if role == "assistant" and msg.get("tool_calls"):
                        for tc in msg.get("tool_calls", []):
                            fn = tc.get("function", {})
                            name = fn.get("name", "")
                            args = fn.get("arguments", "")
                            if name:
                                history_messages.append(f"模型请求调用工具: {name}({args})")
                        if content:
                            history_messages.append(f"assistant: {content}")
                        continue
                    
                    if role and content:
                        if role == "system":
                            has_system_in_history = True
                        history_messages.append(f"{role}: {content}")
                
                # 如果历史中没有system消息但现在有system消息，则添加到历史的最前面
                if system_content and not has_system_in_history:
                    history_messages.insert(0, f"system: {system_content}")
                    logger.info(f"[history_message模式] 添加system内容到历史消息前")
                
                # 将历史消息添加到查询中
                if history_messages:
                    history_context = "\n\n".join(history_messages)
                    user_query = f"<history>\n{history_context}\n</history>\n\n用户当前问题: {user_query}"
            elif system_content:  # 没有历史消息但有system消息
                user_query = f"系统指令: {system_content}\n\n用户问题: {user_query}"
                logger.info(f"[history_message模式] 首次对话，添加system内容到查询前")
            
            dify_request = {
                "inputs": inputs,
                "query": user_query,
                "response_mode": "streaming" if stream else "blocking",
                "user": user_id
            }
            
            # 如果有上传的文件，添加到请求中
            if uploaded_files:
                dify_request["files"] = uploaded_files

        return dify_request
    
    return None

def split_think(answer):
    """将 <think>...</think> 思考过程与最终回答分离
    返回 (thinking, content)
    支持多个 think 块以及嵌套/未闭合标签的容错
    """
    if not answer:
        return "", ""

    # 提取所有 <think>...</think> 块（非贪婪，跨行）
    think_pattern = re.compile(r'<think>(.*?)</think>', re.DOTALL)

    thinking_parts = []
    content_parts = []
    last_end = 0
    for m in think_pattern.finditer(answer):
        # think 之前的文本属于内容
        content_parts.append(answer[last_end:m.start()])
        thinking_parts.append(m.group(1))
        last_end = m.end()
    # 剩余的文本属于内容
    content_parts.append(answer[last_end:])

    return "".join(thinking_parts), "".join(content_parts)


def estimate_tokens(text):
    """粗略估算 token 数（用于 usage 字段，不精确）"""
    if not text:
        return 0
    # 中文字符大致 1 token/字，英文约 4 字符/token
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u4e00-\u9fff]', text))
    other = len(re.findall(r'[^\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\s]', text))
    return cjk + other // 4


async def aggregate_streaming_response(client, dify_endpoint, dify_request, headers, model, openai_request, session_key=None):
    """以 streaming 模式请求 Dify 并聚合为完整响应（用于 Agent 应用不支持 blocking 时的回退）"""
    stream_request = {**dify_request, "response_mode": "streaming"}
    answer_parts = []
    message_id = ""
    conversation_id = ""
    decoder = codecs.getincrementaldecoder('utf-8')()
    buffer = ""

    async with client.stream(
        'POST',
        dify_endpoint,
        json=stream_request,
        headers={**headers, 'Accept': 'text/event-stream'}
    ) as response:
        if response.status_code != 200:
            error_msg = f"Dify API error: {response.text}"
            logger.error(f"Request failed: {error_msg}")
            return {
                "error": {
                    "message": error_msg,
                    "type": "api_error",
                    "code": response.status_code
                }
            }, response.status_code

        async for raw_bytes in response.aiter_raw():
            if not raw_bytes:
                continue
            buffer += decoder.decode(raw_bytes)
            while '\n' in buffer:
                line, buffer = buffer.split('\n', 1)
                line = line.strip()
                if not line or not line.startswith('data: '):
                    continue
                try:
                    dify_chunk = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                event = dify_chunk.get("event")
                if event in ("message", "agent_message") and "answer" in dify_chunk:
                    answer_parts.append(dify_chunk.get("answer", ""))
                    if not message_id:
                        message_id = dify_chunk.get("message_id", "")
                elif event == "message_end":
                    conversation_id = dify_chunk.get("conversation_id", "")
                    # MODE=3: 回写 conversation_id 到 session map
                    if CONVERSATION_MEMORY_MODE == 3 and session_key and conversation_id:
                        session_map_set(session_key, conversation_id)
                        logger.info(f"[session-map] saved conversation_id={conversation_id} for {session_key}")
                    if not message_id:
                        message_id = dify_chunk.get("message_id", "")

    answer = "".join(answer_parts)
    thinking, answer = split_think(answer)

    # 工具调用协议：解析 tool_calls JSON/XML
    tool_calls = parse_tool_calls_any(answer)
    if tool_calls is not None:
        openai_tool_calls = to_openai_tool_calls(tool_calls, message_id)
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": openai_tool_calls
        }
        if thinking:
            message["reasoning_content"] = thinking
        openai_response = {
            "id": message_id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls"
            }],
            "usage": {
                "prompt_tokens": estimate_tokens(json.dumps(openai_request, ensure_ascii=False)),
                "completion_tokens": estimate_tokens(answer),
                "total_tokens": estimate_tokens(json.dumps(openai_request, ensure_ascii=False)) + estimate_tokens(answer)
            }
        }
        if conversation_id:
            return Response(
                json.dumps(openai_response),
                content_type='application/json',
                headers={'Conversation-Id': conversation_id}
            )
        return openai_response

    # 解析失败：剥离残留的工具调用 XML，防止原始标签泄漏到正文
    answer = strip_tool_call_markup(answer)
    message = {
        "role": "assistant",
        "content": answer
    }
    if thinking:
        message["reasoning_content"] = thinking

    openai_response = {
        "id": message_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "stop"
        }],
        "usage": {
            "prompt_tokens": estimate_tokens(json.dumps(openai_request, ensure_ascii=False)),
            "completion_tokens": estimate_tokens(answer),
            "total_tokens": estimate_tokens(json.dumps(openai_request, ensure_ascii=False)) + estimate_tokens(answer)
        }
    }

    if conversation_id:
        return Response(
            json.dumps(openai_response),
            content_type='application/json',
            headers={'Conversation-Id': conversation_id}
        )
    return openai_response


def build_tools_prompt(tools):
    """把 OpenAI tools 定义转为 Dify 模型可读的提示文本
    要求模型在需要工具时只输出严格 JSON（供 OpenDify 解析为原生 tool_calls）
    """
    if not tools:
        return ""
    funcs = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        f = t.get("function", {}) if isinstance(t.get("function"), dict) else {}
        if not f.get("name"):
            continue
        funcs.append({
            "name": f.get("name", ""),
            "description": f.get("description", ""),
            "parameters": f.get("parameters", {})
        })
    if not funcs:
        return ""
    prompt = (
        "以下是你可以调用的工具列表。当用户请求需要调用工具时，你的回答必须严格遵循如下格式：\n"
        '{"tool_calls": [{"name": "<工具名>", "arguments": {<参数对象>}}]}\n'
        "只输出这一个 JSON 对象，不要输出任何解释、代码块标记或其他文字。\n"
        "严禁输出 <tool_calls> 或 <invoke> 之类的 XML 标签格式，只允许上述 JSON 格式。\n"
        "不要自行编造工具结果，工具调用后用户会提供执行结果，你再基于结果继续回答。\n"
        "可用工具：\n" + json.dumps(funcs, ensure_ascii=False, indent=2)
    )
    return prompt


def parse_tool_calls(answer):
    """从模型 answer 中解析 tool_calls JSON
    成功返回 tool_calls 列表，失败返回 None
    """
    if not answer:
        return None
    text = answer.strip()
    # 兼容 ```json ... ``` 代码块包裹
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and isinstance(obj.get("tool_calls"), list):
        return obj["tool_calls"]
    if isinstance(obj, list):
        return obj
    return None


def _xml_unescape(text):
    """反转义 XML 实体"""
    return (text
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&apos;", "'")
            .replace("&amp;", "&"))


def parse_tool_calls_xml(answer):
    """从模型 answer 中解析 XML/DSML 风格 tool_calls
    支持：
    - 根标签 <tool_calls>（V4）或 <function_calls>（V3.2）
    - <antml:function_calls>/<antml:invoke>/<antml:parameter>（Anthropic 变体）
    - 无根标签的裸 <invoke name="x">...</invoke> 与 <invoke name="x"/>
    - <invoke name="x">...</invoke> 长格式
    - <invoke name="x"/> 自闭合（零参数）
    - <parameter name="k" string="true|false">v</parameter>
    - Format 2: invoke 体内直接放 JSON 对象
    成功返回 [{name, arguments: {...}}]，失败/未闭合返回 None
    """
    if not answer:
        return None
    text = answer.strip()
    # 兼容 ```xml ... ``` 代码块包裹
    text = re.sub(r'^```(?:xml)?\s*|\s*```$', '', text).strip()
    # 归一化 antml 前缀：<antml:invoke> → <invoke> 等
    text = re.sub(r'<(/?)\s*antml:', r'<\1', text)
    # 根标签完整闭合时在根内提取；根标签已出现但未闭合 → 等待闭合（流式继续累积）；
    # 无根标签 → 尝试提取裸 <invoke> 块
    root_closed = (("<tool_calls>" in text and "</tool_calls>" in text) or
                   ("<function_calls>" in text and "</function_calls>" in text))
    root_open = "<tool_calls>" in text or "<function_calls>" in text
    if root_closed:
        m = re.search(r'<(?:tool_calls|function_calls)>(.*?)</(?:tool_calls|function_calls)>', text, re.DOTALL)
        if not m:
            return None
        body = m.group(1)
    elif root_open:
        # 根标签已出现但未闭合 → 视为未完成（流式继续累积）
        return None
    else:
        # 存在 <invoke 开标签但无任何闭合 → 视为未完成（流式继续累积）
        if "<invoke" in text and "</invoke>" not in text and "/>" not in text:
            return None
        body = text
    result = []
    # 长格式 <invoke name="...">...</invoke>（容忍 name 后的其他属性）
    for name, invoke_body in re.findall(r'<invoke\s+name="([^"]+)"[^>]*>(.*?)</invoke>', body, re.DOTALL):
        name = name.strip()
        if not name:
            continue
        args = _parse_invoke_body(invoke_body)
        result.append({"name": name, "arguments": args})
    # 自闭合 <invoke name="..."/>（零参数）
    for name in re.findall(r'<invoke\s+name="([^"]+)"\s*/>', body):
        name = name.strip()
        if name:
            result.append({"name": name, "arguments": {}})
    return result if result else None


def _parse_invoke_body(invoke_body):
    """解析 invoke 体内参数：<parameter> 列表 或 Format 2 直接 JSON"""
    invoke_body = invoke_body.strip()
    # Format 2: invoke 体内直接放 JSON 对象
    if invoke_body.startswith("{"):
        try:
            obj = json.loads(invoke_body)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    args = {}
    # <parameter name="k" string="true|false">v</parameter>（属性顺序不固定）
    for tag, pvalue in re.findall(r'<parameter\s+([^>]*)>(.*?)</parameter>', invoke_body, re.DOTALL):
        name_m = re.search(r'name="([^"]+)"', tag)
        if not name_m:
            continue
        pname = name_m.group(1).strip()
        pvalue = _xml_unescape(pvalue.strip())
        is_str = re.search(r'string="(true|false)"', tag)
        if is_str and is_str.group(1) == "true":
            # string="true" → 字面字符串，原样取值
            args[pname] = pvalue
        else:
            # string="false" 或未标注 → 尝试 JSON 解码（数字/布尔/数组/对象），失败保留字符串
            try:
                args[pname] = json.loads(pvalue)
            except (json.JSONDecodeError, ValueError):
                args[pname] = pvalue
    return args


def restore_dsml(answer):
    """归一化 DSML / Claude Code 竖线变体为标准 XML 标签。

    DSML = DeepSeek Markup Language（DeepSeek V3.2/V4 原生工具调用标记），
    分隔符为全角竖线 ｜（U+FF5C），存在多种降级/变体：
    - 规范：<｜DSML｜tool_calls>（vLLM/SGLang）
    - 双竖线：<||DSML||tool_calls>、<｜｜DSML｜｜tool_calls>
    - Claude Code 风格：<｜tool_calls｜>、<｜invoke name="bash｜">、</｜invoke｜>
    - ASCII：<|tool_calls|>

    仅影响解析判断；解析失败时输出仍用原始候选，不会破坏普通文本。
    """
    if not answer:
        return answer
    text = answer
    # 1. 移除 DSML 标记：<｜DSML｜tool_calls> → <tool_calls>（容忍 1-2 个全角/ASCII 竖线）
    text = re.sub(r'[｜|]{1,2}DSML[｜|]{1,2}', '', text, flags=re.IGNORECASE)
    # 2. 标签开始处竖线：<｜tool_calls → <tool_calls；</｜invoke → </invoke
    text = re.sub(r'<(/?)[｜|]+', r'<\1', text)
    # 3. 属性值竖线闭合（ASCII 版引号缺失，竖线取代 ">）：name="bash|> → name="bash">
    text = re.sub(r'"([^"<>]*)[｜|]+>', r'"\1">', text)
    # 4. 属性值内竖线（全角版引号闭合）：name="bash｜" → name="bash"
    text = re.sub(r'"([^"<>]*)[｜|]+"', r'"\1"', text)
    # 5. 标签结尾处竖线：tool_calls｜> → tool_calls>；</parameter｜> → </parameter>
    text = re.sub(r'[｜|]+>', '>', text)
    return text


def parse_tool_calls_any(answer):
    """先试 JSON，再试 XML，兼容两种格式。入口先还原 Dify DSML 编码。"""
    if not answer:
        return None
    answer = restore_dsml(answer)
    parsed = parse_tool_calls(answer)
    if parsed is not None:
        return parsed
    return parse_tool_calls_xml(answer)


def strip_tool_call_markup(text):
    """从文本中剥离未解析成功的工具调用 XML 标记（<invoke>/<tool_calls>/<function_calls>/<antml:*>/<parameter>），
    仅保留正文。用于解析失败时防止原始 XML 泄漏到最终输出。"""
    if not text:
        return text or ""
    text = restore_dsml(text)
    text = re.sub(r'<(/?)\s*antml:', r'<\1', text)
    # 循环删除闭合块（处理嵌套）
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r'<(?:tool_calls|function_calls)\b[^>]*>.*?</(?:tool_calls|function_calls)>', '', text, flags=re.DOTALL)
        text = re.sub(r'<invoke\s+name="[^"]*"[^>]*>.*?</invoke>', '', text, flags=re.DOTALL)
        text = re.sub(r'<invoke\s+name="[^"]*"[^>]*?/>', '', text)
        text = re.sub(r'<parameter\s+[^>]*>.*?</parameter>', '', text, flags=re.DOTALL)
        text = re.sub(r'<parameter\s+[^>]*?/>', '', text)
    # 悬空开标签：找到第一个未闭合的工具标签，从它截断到末尾（覆盖流被截断）
    dangling = None
    for m in re.finditer(r'<(invoke|tool_calls|function_calls)\b', text):
        tag = m.group(1)
        if not re.search(r'</' + tag + r'>', text[m.end():]):
            dangling = m.start()
            break
    if dangling is not None:
        text = text[:dangling]
    # 收敛多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def to_openai_tool_calls(tool_calls, message_id):
    """把 {name, arguments} 列表转成 OpenAI 原生 tool_calls 列表"""
    result = []
    prefix = (message_id or "call")[:8]
    for i, tc in enumerate(tool_calls):
        if isinstance(tc, str):
            tc = {"name": tc}
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or ""
        args = tc.get("arguments", {})
        if isinstance(args, str):
            args_str = args
        else:
            args_str = json.dumps(args, ensure_ascii=False)
        result.append({
            "id": f"call_{prefix}_{i}",
            "index": i,
            "type": "function",
            "function": {
                "name": name,
                "arguments": args_str
            }
        })
    return result


def transform_dify_to_openai(dify_response, model="claude-3-5-sonnet-v2", stream=False, prompt_text=""):
    """将Dify格式的响应转换为OpenAI格式"""
    
    if not stream:
        # 首先获取回答内容，支持不同的响应模式
        answer = ""
        mode = dify_response.get("mode", "")
        
        # 普通聊天模式
        if "answer" in dify_response:
            answer = dify_response.get("answer", "")
        
        # 如果是Agent模式，需要从agent_thoughts中提取回答
        elif "agent_thoughts" in dify_response:
            # Agent模式下通常最后一个thought包含最终答案
            agent_thoughts = dify_response.get("agent_thoughts", [])
            if agent_thoughts:
                for thought in agent_thoughts:
                    if thought.get("thought"):
                        answer = thought.get("thought", "")
        
        # 只在零宽字符会话记忆模式时处理conversation_id
        if CONVERSATION_MEMORY_MODE == 2:
            conversation_id = dify_response.get("conversation_id", "")
            history = dify_response.get("conversation_history", [])
            
            # 检查历史消息中是否已经有会话ID
            has_conversation_id = False
            if history:
                for msg in history:
                    if msg.get("role") == "assistant":
                        content = msg.get("content", "")
                        if decode_conversation_id(content) is not None:
                            has_conversation_id = True
                            break
            
            # 只在新会话且历史消息中没有会话ID时插入
            if conversation_id and not has_conversation_id:
                logger.info(f"[Debug] Inserting conversation_id: {conversation_id}, history_length: {len(history)}")
                encoded = encode_conversation_id(conversation_id)
                answer = answer + encoded
                logger.info(f"[Debug] Response content after insertion: {repr(answer)}")
        
        # 剥离 <think> 思考过程，输出到 reasoning_content
        thinking, answer = split_think(answer)
        
        # 工具调用协议：解析 tool_calls JSON/XML
        tool_calls = parse_tool_calls_any(answer)
        if tool_calls is not None:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": to_openai_tool_calls(tool_calls, dify_response.get("message_id", ""))
            }
            if thinking:
                message["reasoning_content"] = thinking
            return {
                "id": dify_response.get("message_id", ""),
                "object": "chat.completion",
                "created": dify_response.get("created", int(time.time())),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls"
                }],
                "usage": {
                    "prompt_tokens": estimate_tokens(prompt_text),
                    "completion_tokens": estimate_tokens(answer),
                    "total_tokens": estimate_tokens(prompt_text) + estimate_tokens(answer)
                }
            }
        
        # 解析失败：剥离残留的工具调用 XML，防止原始标签泄漏到正文
        answer = strip_tool_call_markup(answer)
        message = {
            "role": "assistant",
            "content": answer
        }
        if thinking:
            message["reasoning_content"] = thinking
        
        return {
            "id": dify_response.get("message_id", ""),
            "object": "chat.completion",
            "created": dify_response.get("created", int(time.time())),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": estimate_tokens(prompt_text),
                "completion_tokens": estimate_tokens(answer),
                "total_tokens": estimate_tokens(prompt_text) + estimate_tokens(answer)
            }
        }
    else:
        # 流式响应的转换在stream_response函数中处理
        return dify_response

def create_openai_stream_response(content, message_id, model="claude-3-5-sonnet-v2"):
    """创建OpenAI格式的流式响应"""
    return {
        "id": message_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {
                "content": content
            },
            "finish_reason": None
        }]
    }

def encode_conversation_id(conversation_id):
    """将conversation_id编码为不可见的字符序列"""
    if not conversation_id:
        return ""
    
    # 使用Base64编码减少长度
    import base64
    encoded = base64.b64encode(conversation_id.encode()).decode()
    
    # 使用8种不同的零宽字符表示3位数字
    # 这样可以将编码长度进一步减少
    char_map = {
        '0': '\u200b',  # 零宽空格
        '1': '\u200c',  # 零宽非连接符
        '2': '\u200d',  # 零宽连接符
        '3': '\ufeff',  # 零宽非断空格
        '4': '\u2060',  # 词组连接符
        '5': '\u180e',  # 蒙古语元音分隔符
        '6': '\u2061',  # 函数应用
        '7': '\u2062',  # 不可见乘号
    }
    
    # 将Base64字符串转换为八进制数字
    result = []
    for c in encoded:
        # 将每个字符转换为8进制数字（0-7）
        if c.isalpha():
            if c.isupper():
                val = ord(c) - ord('A')
            else:
                val = ord(c) - ord('a') + 26
        elif c.isdigit():
            val = int(c) + 52
        elif c == '+':
            val = 62
        elif c == '/':
            val = 63
        else:  # '='
            val = 0
            
        # 每个Base64字符可以产生2个3位数字
        first = (val >> 3) & 0x7
        second = val & 0x7
        result.append(char_map[str(first)])
        if c != '=':  # 不编码填充字符的后半部分
            result.append(char_map[str(second)])
    
    return ''.join(result)

def decode_conversation_id(content):
    """从消息内容中解码conversation_id"""
    try:
        # 零宽字符到3位数字的映射
        char_to_val = {
            '\u200b': '0',  # 零宽空格
            '\u200c': '1',  # 零宽非连接符
            '\u200d': '2',  # 零宽连接符
            '\ufeff': '3',  # 零宽非断空格
            '\u2060': '4',  # 词组连接符
            '\u180e': '5',  # 蒙古语元音分隔符
            '\u2061': '6',  # 函数应用
            '\u2062': '7',  # 不可见乘号
        }
        
        # 提取最后一段零宽字符序列
        space_chars = []
        for c in reversed(content):
            if c not in char_to_val:
                break
            space_chars.append(c)
        
        if not space_chars:
            return None
            
        # 将零宽字符转换回Base64字符串
        space_chars.reverse()
        base64_chars = []
        for i in range(0, len(space_chars), 2):
            first = int(char_to_val[space_chars[i]], 8)
            if i + 1 < len(space_chars):
                second = int(char_to_val[space_chars[i + 1]], 8)
                val = (first << 3) | second
            else:
                val = first << 3
                
            # 转换回Base64字符
            if val < 26:
                base64_chars.append(chr(val + ord('A')))
            elif val < 52:
                base64_chars.append(chr(val - 26 + ord('a')))
            elif val < 62:
                base64_chars.append(str(val - 52))
            elif val == 62:
                base64_chars.append('+')
            else:
                base64_chars.append('/')
                
        # 添加Base64填充
        padding = len(base64_chars) % 4
        if padding:
            base64_chars.extend(['='] * (4 - padding))
            
        # 解码Base64字符串
        import base64
        base64_str = ''.join(base64_chars)
        return base64.b64decode(base64_str).decode()
        
    except Exception as e:
        logger.debug(f"Failed to decode conversation_id: {e}")
        return None

@app.route('/v1/chat/completions', methods=['POST'])
def chat_completions():
    try:
        # 新增：验证API密钥
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({
                "error": {
                    "message": "Missing Authorization header",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key"
                }
            }), 401

        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            return jsonify({
                "error": {
                    "message": "Invalid Authorization header format. Expected: Bearer <API_KEY>",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key"
                }
            }), 401

        provided_api_key = parts[1]
        if provided_api_key not in VALID_API_KEYS:
            return jsonify({
                "error": {
                    "message": "Invalid API key",
                    "type": "invalid_request_error",
                    "param": None,
                    "code": "invalid_api_key"
                }
            }), 401

        # 继续处理原始逻辑
        openai_request = request.get_json()
        
        logger.info(f"Received request: {json.dumps(openai_request, ensure_ascii=False)}")
        
        model = openai_request.get("model", "claude-3-5-sonnet")
        
        # 验证模型是否支持
        api_key = get_api_key(model)
        if not api_key:
            error_msg = f"Model {model} is not supported. Available models: {', '.join(model_manager.name_to_api_key.keys())}"
            logger.error(error_msg)
            return {
                "error": {
                    "message": error_msg,
                    "type": "invalid_request_error",
                    "code": "model_not_found"
                }
            }, 404
            
        # 转换请求并处理图片上传
        # MODE=3: 计算 session key（三级回退：X-Session-Id header -> user 字段 -> 首条 user 消息 hash）
        session_key = None
        if CONVERSATION_MEMORY_MODE == 3:
            session_key = compute_session_key(openai_request, request.headers)
            if session_key:
                logger.info(f"[session-map] session_key={session_key}")
        dify_request = asyncio.run(transform_openai_to_dify(openai_request, "/chat/completions", api_key, session_key=session_key))
        
        # Debug模式下打印转换后的请求
        if '--debug' in sys.argv:
            print("=" * 50)
            print("TRANSFORMED REQUEST DEBUG INFO")
            print("=" * 50)
            print(f"Transformed Body: {json.dumps(dify_request, ensure_ascii=False, indent=2)}")
            print("=" * 50)
        
        if not dify_request:
            logger.error("Failed to transform request")
            return {
                "error": {
                    "message": "Invalid request format",
                    "type": "invalid_request_error",
                }
            }, 400

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        stream = openai_request.get("stream", False)
        has_tools = bool(openai_request.get("tools"))
        dify_endpoint = f"{DIFY_API_BASE}/chat-messages"
        logger.info(f"Sending request to Dify endpoint: {dify_endpoint}, stream={stream}, has_tools={has_tools}")

        if stream:
            def generate():
                client = httpx.Client(timeout=None)
                
                def flush_chunk(chunk_data):
                    """Helper function to flush chunks immediately"""
                    return chunk_data.encode('utf-8')
                
                def calculate_delay(buffer_size):
                    """
                    根据缓冲区大小动态计算延迟
                    buffer_size: 缓冲区中剩余的字符数量
                    """
                    if buffer_size > 30:  # 缓冲区内容较多，快速输出
                        return 0.0005  # 0.5ms延迟
                    elif buffer_size > 20:  # 中等数量，适中速度
                        return 0.001  # 1ms延迟
                    elif buffer_size > 10:  # 较少内容，稍慢速度
                        return 0.002  # 2ms延迟
                    else:  # 内容很少，使用较慢的速度
                        return 0.003  # 3ms延迟
                
                def send_char(chars, message_id, is_think=False):
                    """Helper function to send characters (batch)
                    is_think=True 时内容输出到 reasoning_content（思考过程）
                    chars 可以是单个字符或字符串
                    """
                    delta = {}
                    # 首个 chunk 带上 role，符合 OpenAI 流式规范
                    if not send_char.role_sent:
                        delta["role"] = "assistant"
                        send_char.role_sent = True
                    if is_think:
                        delta["reasoning_content"] = chars
                    else:
                        delta["content"] = chars
                    openai_chunk = {
                        "id": message_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": delta,
                            "finish_reason": None
                        }]
                    }
                    chunk_data = f"data: {json.dumps(openai_chunk)}\n\n"
                    return flush_chunk(chunk_data)
                send_char.role_sent = False

                # 思考标签状态机（处理跨片段到达的 <think> 标签）
                THINK_OPEN = "<think>"
                THINK_CLOSE = "</think>"
                generate.in_think = False
                generate.tag_buffer = ""

                def process_think_char(char):
                    """逐字符判定 <think> 标签，返回 (is_think, text) 或 None（标签字符）
                    兼容标签跨多个 answer 片段到达的情况
                    """
                    target = THINK_CLOSE if generate.in_think else THINK_OPEN
                    generate.tag_buffer += char
                    if target.startswith(generate.tag_buffer):
                        if generate.tag_buffer == target:
                            generate.in_think = not generate.in_think
                            generate.tag_buffer = ""
                        return None  # 标签前缀，不输出
                    # 不是标签，累积的字符按当前状态输出
                    chars = generate.tag_buffer
                    generate.tag_buffer = ""
                    return (generate.in_think, chars)

                # 初始化缓冲区
                output_buffer = []
                
                try:
                    # MODE=3: Dify 404 时丢弃失效 conversation_id，重建请求重试一次
                    current_request = dify_request
                    for _retry in range(2):
                        with client.stream(
                            'POST',
                            dify_endpoint,
                            json=current_request,
                            headers={
                                **headers,
                                'Accept': 'text/event-stream',
                                'Cache-Control': 'no-cache',
                                'Connection': 'keep-alive'
                            }
                        ) as response:
                            if response.status_code == 404 and session_key:
                                if _retry == 0:
                                    logger.warning(f"[session-map] Dify 404, dropping conversation_id for {session_key}")
                                    session_map_pop(session_key)
                                    current_request = asyncio.run(transform_openai_to_dify(
                                        openai_request, "/chat/completions", api_key,
                                        session_key=session_key, force_history_mode=True
                                    ))
                                    continue
                                logger.error(f"[session-map] Dify 404 again after retry for {session_key}")
                                yield flush_chunk(f"data: {json.dumps({'error': {'message': 'Dify conversation not found', 'type': 'api_error', 'code': 404}})}\\n\\n")
                                yield flush_chunk("data: [DONE]\\n\\n")
                                break
                            generate.message_id = None
                            buffer = ""
                            decoder = codecs.getincrementaldecoder('utf-8')()
                            # 工具调用协议状态
                            generate.has_tools = has_tools
                            generate.tool_calls_emitted = False
                            generate.pending_tool_calls = []
                            generate.tool_candidate = ""
                        
                            for raw_bytes in response.iter_raw():
                                if not raw_bytes:
                                    continue
                                
                                try:
                                    # 增量解码，避免多字节 UTF-8 字符被块边界切断
                                    buffer += decoder.decode(raw_bytes)
                                
                                    while '\n' in buffer:
                                        line, buffer = buffer.split('\n', 1)
                                        line = line.strip()
                                    
                                        if not line or not line.startswith('data: '):
                                            continue
                                        
                                        try:
                                            json_str = line[6:]
                                            dify_chunk = json.loads(json_str)
                                        
                                            if dify_chunk.get("event") in ("message", "agent_message") and "answer" in dify_chunk:
                                                current_answer = dify_chunk["answer"]
                                                if not current_answer:
                                                    continue
                                                
                                                message_id = dify_chunk.get("message_id", "")
                                                if not generate.message_id:
                                                    generate.message_id = message_id
                                            
                                                # 逐字符经思考状态机处理后加入输出缓冲区
                                                for char in current_answer:
                                                    result = process_think_char(char)
                                                    if result is None:
                                                        continue
                                                    is_think, chars = result
                                                    for c in chars:
                                                        if generate.tool_calls_emitted and not is_think:
                                                            # 已确认工具调用，丢弃 JSON 尾部 content 字符
                                                            continue
                                                        if not is_think and (generate.has_tools or c == "<" or generate.tool_candidate.startswith("<")):
                                                            # 工具检测模式：累积候选 JSON/XML，不立即输出
                                                            generate.tool_candidate += c
                                                            tool_calls = parse_tool_calls_any(generate.tool_candidate)
                                                            if tool_calls is not None:
                                                                logger.info(f"Detected tool_calls: {json.dumps(tool_calls, ensure_ascii=False)}")
                                                                generate.tool_calls_emitted = True
                                                                generate.pending_tool_calls = to_openai_tool_calls(tool_calls, generate.message_id)
                                                            else:
                                                                # 候选明显不是 JSON/XML 开头 → 关闭检测，按普通文本输出
                                                                cand = generate.tool_candidate.strip()
                                                                if cand and not cand.startswith(("{", "[", "\"", "null", "true", "false", "<")):
                                                                    generate.has_tools = False
                                                                    cleaned = strip_tool_call_markup(generate.tool_candidate)
                                                                    if cleaned:
                                                                        for cc in cleaned:
                                                                            output_buffer.append((cc, generate.message_id, False))
                                                                    generate.tool_candidate = ""
                                                            continue
                                                        output_buffer.append((c, generate.message_id, is_think))
                                            
                                                # 根据缓冲区大小动态调整输出速度（批量合并同类型字符）
                                                while output_buffer:
                                                    c, msg_id, is_think = output_buffer.pop(0)
                                                    batch = [c]
                                                    # 合并后续同类型字符，减少 chunk 数量
                                                    while output_buffer and output_buffer[0][2] == is_think:
                                                        batch.append(output_buffer.pop(0)[0])
                                                    yield send_char("".join(batch), msg_id, is_think)
                                                    # 根据剩余缓冲区大小计算延迟
                                                    delay = calculate_delay(len(output_buffer))
                                                    time.sleep(delay)
                                            
                                                # 立即继续处理下一个请求
                                                continue
                                        
                                            # 处理Agent的思考过程，记录日志但不输出给用户
                                            elif dify_chunk.get("event") == "agent_thought":
                                                thought_id = dify_chunk.get("id", "")
                                                thought = dify_chunk.get("thought", "")
                                                tool = dify_chunk.get("tool", "")
                                                tool_input = dify_chunk.get("tool_input", "")
                                                observation = dify_chunk.get("observation", "")
                                            
                                                logger.info(f"[Agent Thought] ID: {thought_id}, Tool: {tool}")
                                                if thought:
                                                    logger.info(f"[Agent Thought] Thought: {thought}")
                                                if tool_input:
                                                    logger.info(f"[Agent Thought] Tool Input: {tool_input}")
                                                if observation:
                                                    logger.info(f"[Agent Thought] Observation: {observation}")
                                            
                                                # 获取message_id以关联思考和最终输出
                                                message_id = dify_chunk.get("message_id", "")
                                                if not generate.message_id and message_id:
                                                    generate.message_id = message_id
                                            
                                                continue
                                        
                                            # 处理消息中的文件(如图片)，记录日志但不直接输出给用户
                                            elif dify_chunk.get("event") == "message_file":
                                                file_id = dify_chunk.get("id", "")
                                                file_type = dify_chunk.get("type", "")
                                                file_url = dify_chunk.get("url", "")
                                            
                                                logger.info(f"[Message File] ID: {file_id}, Type: {file_type}, URL: {file_url}")
                                                continue
                                        
                                            elif dify_chunk.get("event") == "message_end":
                                                # MODE=3: 回写 conversation_id 到 session map
                                                if CONVERSATION_MEMORY_MODE == 3 and session_key:
                                                    cid = dify_chunk.get("conversation_id")
                                                    if cid:
                                                        session_map_set(session_key, cid)
                                                        logger.info(f"[session-map] saved conversation_id={cid} for {session_key}")
                                                # 处理流结束时未闭合的思考标签残留
                                                if generate.tag_buffer:
                                                    chars = generate.tag_buffer
                                                    generate.tag_buffer = ""
                                                    for c in chars:
                                                        output_buffer.append((c, generate.message_id, generate.in_think))
                                            
                                                # 工具候选兜底：流结束时仍未确认
                                                if not generate.tool_calls_emitted and generate.tool_candidate:
                                                    tool_calls = parse_tool_calls_any(generate.tool_candidate)
                                                    if tool_calls is not None:
                                                        logger.info(f"Detected tool_calls at end: {json.dumps(tool_calls, ensure_ascii=False)}")
                                                        generate.tool_calls_emitted = True
                                                        generate.pending_tool_calls = to_openai_tool_calls(tool_calls, generate.message_id)
                                                    else:
                                                        # 不是工具调用：剥离残留 XML 后按普通文本输出
                                                        cleaned = strip_tool_call_markup(generate.tool_candidate)
                                                        if cleaned:
                                                            for cc in cleaned:
                                                                output_buffer.append((cc, generate.message_id, False))
                                                        generate.tool_candidate = ""
                                            
                                                # 快速输出剩余内容（批量合并同类型字符）
                                                while output_buffer:
                                                    c, msg_id, is_think = output_buffer.pop(0)
                                                    batch = [c]
                                                    while output_buffer and output_buffer[0][2] == is_think:
                                                        batch.append(output_buffer.pop(0)[0])
                                                    yield send_char("".join(batch), msg_id, is_think)
                                                    time.sleep(0.001)  # 固定使用最小延迟快速输出剩余内容
                                            
                                                # 工具调用：输出 tool_calls chunks + finish_reason="tool_calls"
                                                if generate.tool_calls_emitted and generate.pending_tool_calls:
                                                    for tc in generate.pending_tool_calls:
                                                        tool_chunk = {
                                                            "id": generate.message_id,
                                                            "object": "chat.completion.chunk",
                                                            "created": int(time.time()),
                                                            "model": model,
                                                            "choices": [{
                                                                "index": 0,
                                                                "delta": {
                                                                    "role": "assistant",
                                                                    "tool_calls": [tc]
                                                                },
                                                                "finish_reason": None
                                                            }]
                                                        }
                                                        yield flush_chunk(f"data: {json.dumps(tool_chunk)}\n\n")
                                                    final_chunk = {
                                                        "id": generate.message_id,
                                                        "object": "chat.completion.chunk",
                                                        "created": int(time.time()),
                                                        "model": model,
                                                        "choices": [{
                                                            "index": 0,
                                                            "delta": {},
                                                            "finish_reason": "tool_calls"
                                                        }]
                                                    }
                                                    yield flush_chunk(f"data: {json.dumps(final_chunk)}\n\n")
                                                    yield flush_chunk("data: [DONE]\n\n")
                                                    continue
                                            
                                                # 只在零宽字符会话记忆模式时处理conversation_id
                                                if CONVERSATION_MEMORY_MODE == 2:
                                                    conversation_id = dify_chunk.get("conversation_id")
                                                    history = dify_chunk.get("conversation_history", [])
                                                
                                                    has_conversation_id = False
                                                    if history:
                                                        for msg in history:
                                                            if msg.get("role") == "assistant":
                                                                content = msg.get("content", "")
                                                                if decode_conversation_id(content) is not None:
                                                                    has_conversation_id = True
                                                                    break
                                                
                                                    # 只在新会话且历史消息中没有会话ID时插入
                                                    if conversation_id and not has_conversation_id:
                                                        logger.info(f"[Debug] Inserting conversation_id in stream: {conversation_id}")
                                                        encoded = encode_conversation_id(conversation_id)
                                                        logger.info(f"[Debug] Stream encoded content: {repr(encoded)}")
                                                        for char in encoded:
                                                            yield send_char(char, generate.message_id)
                                            
                                                final_chunk = {
                                                    "id": generate.message_id,
                                                    "object": "chat.completion.chunk",
                                                    "created": int(time.time()),
                                                    "model": model,
                                                    "choices": [{
                                                        "index": 0,
                                                        "delta": {},
                                                        "finish_reason": "stop"
                                                    }]
                                                }
                                                yield flush_chunk(f"data: {json.dumps(final_chunk)}\n\n")
                                                yield flush_chunk("data: [DONE]\n\n")
                                        
                                        except json.JSONDecodeError as e:
                                            logger.error(f"JSON decode error: {str(e)}")
                                            continue
                                        
                                except Exception as e:
                                    logger.error(f"Error processing chunk: {str(e)}")
                                    continue

                        break
                finally:
                    client.close()

            return Response(
                stream_with_context(generate()),
                content_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache, no-transform',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no'
                },
                direct_passthrough=True
            )
        else:
            async def sync_response():
                try:
                    async with httpx.AsyncClient(timeout=None) as client:
                        # MODE=3: Dify 404 时丢弃失效 conversation_id，重建请求重试一次
                        current_request = dify_request
                        for _retry in range(2):
                            response = await client.post(
                                dify_endpoint,
                                json=current_request,
                                headers=headers
                            )
                            if response.status_code == 404 and session_key:
                                if _retry == 0:
                                    logger.warning(f"[session-map] Dify 404, dropping conversation_id for {session_key}")
                                    session_map_pop(session_key)
                                    current_request = await transform_openai_to_dify(
                                        openai_request, "/chat/completions", api_key,
                                        session_key=session_key, force_history_mode=True
                                    )
                                    continue
                                logger.error(f"[session-map] Dify 404 again after retry for {session_key}")
                            break
                        
                        if response.status_code != 200:
                            # Agent 应用不支持 blocking 模式时，回退为 streaming 内部聚合
                            if "does not support blocking mode" in response.text:
                                logger.info("Blocking mode not supported, falling back to streaming aggregation")
                                return await aggregate_streaming_response(
                                    client, dify_endpoint, current_request, headers,
                                    model, openai_request, session_key
                                )
                            error_msg = f"Dify API error: {response.text}"
                            logger.error(f"Request failed: {error_msg}")
                            return {
                                "error": {
                                    "message": error_msg,
                                    "type": "api_error",
                                    "code": response.status_code
                                }
                            }, response.status_code

                        dify_response = response.json()
                        logger.info(f"Received response from Dify: {json.dumps(dify_response, ensure_ascii=False)}")
                        logger.info(f"[Debug] Response content: {repr(dify_response.get('answer', ''))}")
                        # MODE=3: 回写 conversation_id 到 session map
                        if CONVERSATION_MEMORY_MODE == 3 and session_key:
                            cid = dify_response.get("conversation_id")
                            if cid:
                                session_map_set(session_key, cid)
                                logger.info(f"[session-map] saved conversation_id={cid} for {session_key}")
                        openai_response = transform_dify_to_openai(
                            dify_response,
                            model=model,
                            prompt_text=json.dumps(openai_request, ensure_ascii=False)
                        )
                        conversation_id = dify_response.get("conversation_id")
                        if conversation_id:
                            # 在响应头中传递conversation_id
                            return Response(
                                json.dumps(openai_response),
                                content_type='application/json',
                                headers={
                                    'Conversation-Id': conversation_id
                                }
                            )
                        else:
                            return openai_response
                except httpx.RequestError as e:
                    error_msg = f"Failed to connect to Dify: {repr(e)}"
                    logger.error(error_msg)
                    return {
                        "error": {
                            "message": error_msg,
                            "type": "api_error",
                            "code": "connection_error"
                        }
                    }, 503

            return asyncio.run(sync_response())

    except Exception as e:
        logger.exception("Unexpected error occurred")
        return {
            "error": {
                "message": str(e),
                "type": "internal_error",
            }
        }, 500

@app.route('/v1/models', methods=['GET'])
def list_models():
    """返回可用的模型列表"""
    logger.info("Listing available models")
    
    # 刷新模型信息
    asyncio.run(model_manager.refresh_model_info())
    
    # 获取可用模型列表
    available_models = model_manager.get_available_models()
    
    response = {
        "object": "list",
        "data": available_models
    }
    logger.info(f"Available models: {json.dumps(response, ensure_ascii=False)}")
    return response

import sys

# 在main.py的最后初始化时添加环境变量检查：
if __name__ == '__main__':
    # 检查命令行参数
    debug_mode = '--debug' in sys.argv
    
    if not VALID_API_KEYS:
        print("Warning: No API keys configured. Set the VALID_API_KEYS environment variable with comma-separated keys.")
    
    # 启动时初始化模型信息
    asyncio.run(model_manager.refresh_model_info())
    
    host = os.getenv("SERVER_HOST", "127.0.0.1")
    port = int(os.getenv("SERVER_PORT", 5000))
    logger.info(f"Starting server on http://{host}:{port}")
    
    # 根据debug参数决定是否启用debug模式
    app.run(debug=debug_mode, host=host, port=port)
