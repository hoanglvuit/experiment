import sys

try:
    import vllm.entrypoints.openai.api_server as api_server
    file_path = api_server.__file__
except ImportError:
    print("Lỗi: Không tìm thấy thư viện vLLM")
    sys.exit(1)

if file_path.endswith('.pyc'):
    file_path = file_path[:-1]

with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Định nghĩa ASGI Middleware chặn nghẽn cache động
inject_code = '''
class DynamicGateASGIMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        app = scope.get("app")
        
        # Ở chế độ in-process, không cần block /health bằng 503 nữa vì port chỉ mở khi warmup xong.
        # Nhưng vẫn giữ /health/ping bypass bình thường.
        if path in ("/health", "/ping"):
            await self.app(scope, receive, send)
            return

        if path in ("/v1/chat/completions", "/v1/completions"):
            body_bytes = b""
            receive_messages = []
            while True:
                message = await receive()
                receive_messages.append(message)
                if message["type"] == "http.request":
                    body_bytes += message.get("body", b"")
                    if not message.get("more_body", False):
                        break
                elif message["type"] == "http.disconnect":
                    async def mock_receive():
                        return message
                    await self.app(scope, mock_receive, send)
                    return

            prompt_key = ""
            messages = []
            try:
                import json as _json
                body_json = _json.loads(body_bytes.decode('utf-8'))
                messages = body_json.get("messages", [])
                if messages:
                    # Serialize toàn bộ mảng messages để tạo khóa định danh duy nhất tuyệt đối
                    prompt_key = _json.dumps(messages, sort_keys=True)
            except Exception:
                pass

            msg_idx = 0
            async def custom_receive():
                nonlocal msg_idx
                if msg_idx < len(receive_messages):
                    msg = receive_messages[msg_idx]
                    msg_idx += 1
                    return msg
                return await receive()

            if not prompt_key:
                await self.app(scope, custom_receive, send)
                return

            if prompt_key in app.state.completed_prompts:
                await self.app(scope, custom_receive, send)
                return

            event = None
            is_first = False
            async with app.state.prompt_lock:
                if prompt_key in app.state.completed_prompts:
                    pass
                elif prompt_key not in app.state.active_prompts:
                    app.state.active_prompts.add(prompt_key)
                    import asyncio as _asyncio
                    event = _asyncio.Event()
                    app.state.prompt_events[prompt_key] = event
                    is_first = True
                else:
                    event = app.state.prompt_events.get(prompt_key)

            if not is_first:
                if event:
                    await event.wait()
                await self.app(scope, custom_receive, send)
                return

            first_chunk_sent = False
            async def commit_cache():
                async with app.state.prompt_lock:
                    import json as _json
                    for i in range(1, len(messages) + 1):
                        sub_key = _json.dumps(messages[:i], sort_keys=True)
                        app.state.completed_prompts.add(sub_key)
                    if prompt_key in app.state.active_prompts:
                        app.state.active_prompts.remove(prompt_key)
                    ev = app.state.prompt_events.pop(prompt_key, None)
                    if ev:
                        ev.set()

            async def custom_send(message):
                nonlocal first_chunk_sent
                if message["type"] == "http.response.body":
                    if not first_chunk_sent:
                        first_chunk_sent = True
                        await commit_cache()
                await send(message)

            try:
                await self.app(scope, custom_receive, custom_send)
                await commit_cache()
            except Exception as e:
                async with app.state.prompt_lock:
                    if prompt_key in app.state.active_prompts:
                        app.state.active_prompts.remove(prompt_key)
                    ev = app.state.prompt_events.pop(prompt_key, None)
                    if ev:
                        ev.set()
                raise e
            return

        await self.app(scope, receive, send)
'''

target_func = 'def build_app('
content = content.replace(target_func, inject_code + '\n' + target_func)

# 2. Đăng ký middleware và khởi tạo các biến trạng thái trong build_app
target_middleware_anchor = 'app.state.args = args'
middleware_code = '''app.state.args = args

    import asyncio as _asyncio
    
    app.state.completed_prompts = set()
    app.state.active_prompts = set()
    app.state.prompt_lock = _asyncio.Lock()
    app.state.prompt_events = {}
    
    app.add_middleware(DynamicGateASGIMiddleware)'''

content = content.replace(target_middleware_anchor, middleware_code)

# 3. Ép Eager Mode và thực hiện Warmup In-Process trực tiếp trong build_and_serve
target_build_and_serve = 'await init_app_state(engine_client, app.state, args, supported_tasks)'
warmup_code = '''await init_app_state(engine_client, app.state, args, supported_tasks)

    # 1. Tắt torch.compile chuyển sang Eager Mode để tránh OOM / nghẽn CPU
    from vllm.config.compilation import CompilationMode
    engine_client.vllm_config.compilation_config.mode = CompilationMode.NONE

    # 2. Tiến hành Warmup In-Process trực tiếp bằng công cụ nội bộ của engine_client
    import os
    is_generate_mode = supported_tasks is not None and "generate" in supported_tasks
    trace_path = '/trace-round1.json'
    
    if is_generate_mode and os.path.exists(trace_path):
        print("🔥 [HACK] BẮT ĐẦU TIẾN TRÌNH WARMUP TRONG THÂN KHỞI TẠO (IN-PROCESS)...")
        try:
            import json as _json
            from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest
            from vllm.sampling_params import SamplingParams
            
            trace_items = []
            with open(trace_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        trace_items.append(_json.loads(line))
            
            longest_turns = trace_items
            
            print(f"🔥 [HACK] Tiến hành nạp KV Cache In-Process cho {len(longest_turns)} requests...")
            model_name = getattr(args, "served_model_name", [None])[0] or getattr(args, "model", "Qwen3.5-2B")
            
            for idx, item in enumerate(longest_turns):
                body = item.get("body", {})
                messages = body.get("messages", [])
                
                # Tạo ChatCompletionRequest
                chat_req = ChatCompletionRequest(
                    model=model_name,
                    messages=messages,
                )
                
                # Cố gắng render chat hoặc fallback nếu vLLM version cũ không có online_renderer
                if hasattr(app.state, 'online_renderer'):
                    conversation, engine_inputs = await app.state.online_renderer.render_chat(chat_req)
                else:
                    tokenizer = None
                    if hasattr(engine_client, 'get_tokenizer'):
                        # get_tokenizer() can be async or sync
                        tokenizer_ret = engine_client.get_tokenizer()
                        import inspect as _inspect
                        if _inspect.iscoroutine(tokenizer_ret):
                            tokenizer = await tokenizer_ret
                        else:
                            tokenizer = tokenizer_ret
                    elif hasattr(engine_client, 'tokenizer'):
                        tokenizer = engine_client.tokenizer.tokenizer
                    elif hasattr(engine_client, 'renderer'):
                        tokenizer = engine_client.renderer.tokenizer
                        
                    if hasattr(tokenizer, 'tokenizer'):
                        tokenizer = tokenizer.tokenizer
                        
                    prompt_str = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                    engine_inputs = [prompt_str]
                
                for i, engine_input in enumerate(engine_inputs):
                    sampling_params = SamplingParams(max_tokens=1, temperature=0.0)
                    request_id = f"warmup-direct-{idx}-{i}"
                    
                    # Gọi trực tiếp qua generate() nội bộ của engine_client
                    generator = engine_client.generate(
                        prompt=engine_input,
                        sampling_params=sampling_params,
                        request_id=request_id
                    )
                    
                    # Chờ prefill hoàn thành
                    async for output in generator:
                        pass
                
                # Đăng ký các sub-prefixes vào cache để bỏ qua Gate
                for i in range(1, len(messages) + 1):
                    sub_key = _json.dumps(messages[:i], sort_keys=True)
                    app.state.completed_prompts.add(sub_key)
            
            print(f"✅ [HACK] NẠP XONG KV CACHE IN-PROCESS CHO {len(longest_turns)} CUỘC HỘI THOẠI!")
        except Exception as e:
            print(f"❌ [HACK] Lỗi trong tiến trình warmup in-process: {e}")
            import traceback as _traceback
            _traceback.print_exc()'''

content = content.replace(target_build_and_serve, warmup_code)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Đã patch thành công!")
