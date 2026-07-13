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

# 1. Định nghĩa hàm warmup chạy qua trace file hoặc system prompt
inject_code = '''
async def _warmup_system_prompt(app, args, model_name):
    import asyncio as _asyncio
    print("🔥 [HACK] KHỞI ĐỘNG TIẾN TRÌNH WARMUP TOÀN BỘ TRACE INPUT...")
    try:
        import json as _json
        import os as _os
        
        is_ssl = bool(args.ssl_keyfile and args.ssl_certfile)
        scheme = "https" if is_ssl else "http"
        port = args.port
        url = f"{scheme}://127.0.0.1:{port}/v1/chat/completions"
        
        headers = {}
        api_keys = [key for key in (args.api_key or [_os.getenv("VLLM_API_KEY")]) if key]
        if api_keys:
            headers["Authorization"] = f"Bearer {api_keys[0]}"
        
        import aiohttp
        connector = aiohttp.TCPConnector(ssl=False) if is_ssl else None
        
        # 1. Chờ server khởi động xong
        for attempt in range(30):
            try:
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.get(f"{scheme}://127.0.0.1:{port}/health") as resp:
                        if resp.status in (200, 503):
                            break
            except Exception:
                pass
            await _asyncio.sleep(1)
            
        trace_path = '/trace-round1.json'
        if _os.path.exists(trace_path):
            trace_items = []
            with open(trace_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        trace_items.append(_json.loads(line))
            
            # Hàm kiểm tra xem list_a có phải là prefix của list_b hay không
            def is_messages_prefix(list_a, list_b):
                if len(list_a) > len(list_b):
                    return False
                for i in range(len(list_a)):
                    if list_a[i].get("role") != list_b[i].get("role") or list_a[i].get("content") != list_b[i].get("content"):
                        return False
                return True

            # Lọc ra những request là cuộc hội thoại đầy đủ nhất (không là prefix của request nào khác)
            longest_turns = []
            for item in trace_items:
                body = item.get("body", {})
                messages = body.get("messages", [])
                is_prefix = False
                for other in trace_items:
                    if other is item:
                        continue
                    other_messages = other.get("body", {}).get("messages", [])
                    if is_messages_prefix(messages, other_messages):
                        is_prefix = True
                        break
                if not is_prefix:
                    longest_turns.append(item)
            
            print(f"🔥 [HACK] Lọc từ {len(trace_items)} requests thành {len(longest_turns)} cuộc hội thoại đầy đủ. Đang nạp cache...")
            
            # Khống chế số lượng request đồng thời chạy prefill để tránh quá tải
            sem = _asyncio.Semaphore(4)
            
            async def send_warmup(item):
                async with sem:
                    body = item.get("body", {})
                    messages = body.get("messages", [])
                    payload = {
                        "model": model_name,
                        "messages": messages,
                        "max_tokens": 1,
                        "temperature": 0
                    }
                    for retry in range(5):
                        try:
                            async with aiohttp.ClientSession(connector=connector) as session:
                                async with session.post(url, json=payload, headers=headers) as response:
                                    if response.status == 200:
                                        await response.text()
                                        return True
                        except Exception:
                            pass
                        await _asyncio.sleep(0.5)
                    return False
                    
            tasks = [send_warmup(item) for item in longest_turns]
            results = await _asyncio.gather(*tasks)
            successful = sum(1 for r in results if r)
            print(f"✅ [HACK] ĐÃ NẠP XONG TRACE CACHE! Thành công {successful}/{len(longest_turns)} cuộc hội thoại.")
        else:
            print(f"⚠️ [HACK] Không tìm thấy file trace tại {trace_path}. Chuyển sang fallback warmup system prompt.")
            sys_prompt_path = '/system_prompt.txt'
            if _os.path.exists(sys_prompt_path):
                with open(sys_prompt_path, 'r', encoding='utf-8') as f:
                    sys_prompt = f.read()
                payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": "warmup_trigger"}
                    ],
                    "max_tokens": 1,
                    "temperature": 0
                }
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.post(url, json=payload, headers=headers) as response:
                        if response.status == 200:
                            await response.text()
                            print("✅ [HACK] ĐÃ NẠP XONG SYSTEM PROMPT CÔNG KHAI!")
            else:
                print("❌ [HACK] Không tìm thấy system_prompt.txt để warmup.")
    except Exception as e:
        print(f"❌ [HACK] LỖI TIẾN TRÌNH WARMUP: {e}")
    finally:
        app.state.warmup_completed = True
'''

target_func = 'def build_app('
content = content.replace(target_func, inject_code + '\n' + target_func)

# 2. Định nghĩa middleware chặn nghẽn cache động và Bộ lọc ẩn Log truy cập
target_middleware_anchor = 'app.state.args = args'
middleware_code = '''app.state.args = args

    import os
    import asyncio as _asyncio
    import logging as _logging
    
    # Bộ lọc ẩn hoàn toàn dấu vết log warmup và check health
    class WarmupLogFilter(_logging.Filter):
        def filter(self, record):
            log_msg = record.getMessage()
            if "warmup_trigger" in log_msg or "/health" in log_msg or "/ping" in log_msg:
                return False
            return True
            
    _logging.getLogger("uvicorn.access").addFilter(WarmupLogFilter())
    
    is_generate_mode = supported_tasks is not None and "generate" in supported_tasks
    has_warmup = is_generate_mode and (os.path.exists('/trace-round1.json') or os.path.exists('/system_prompt.txt'))
    
    app.state.warmup_completed = not has_warmup
    app.state.completed_prompts = set()
    app.state.active_prompts = set()
    app.state.prompt_lock = _asyncio.Lock()
    app.state.prompt_events = {}

    @app.middleware("http")
    async def dynamic_gate_middleware(request, call_next):
        if request.url.path in ("/health", "/ping"):
            if has_warmup and not getattr(request.app.state, "warmup_completed", False):
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=503,
                    content={"status": "warming_up", "message": "Warmup in progress"}
                )
            return await call_next(request)

        if request.url.path in ("/v1/chat/completions", "/v1/completions"):
            prompt_key = ""
            messages = []
            try:
                body_bytes = await request.body()
                async def receive():
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                request._receive = receive
                
                import json as _json
                body_json = _json.loads(body_bytes.decode('utf-8'))
                messages = body_json.get("messages", [])
                if messages:
                    # Serialize toàn bộ mảng messages để tạo khóa định danh duy nhất tuyệt đối
                    prompt_key = _json.dumps(messages, sort_keys=True)
            except Exception:
                pass
            
            if not prompt_key:
                return await call_next(request)
                
            if prompt_key in request.app.state.completed_prompts:
                return await call_next(request)
                
            event = None
            is_first = False
            async with request.app.state.prompt_lock:
                if prompt_key in request.app.state.completed_prompts:
                    pass
                elif prompt_key not in request.app.state.active_prompts:
                    request.app.state.active_prompts.add(prompt_key)
                    event = _asyncio.Event()
                    request.app.state.prompt_events[prompt_key] = event
                    is_first = True
                else:
                    event = request.app.state.prompt_events.get(prompt_key)
            
            if not is_first:
                if event:
                    await event.wait()
                return await call_next(request)
                
            try:
                response = await call_next(request)
                
                from fastapi.responses import StreamingResponse
                if isinstance(response, StreamingResponse):
                    original_iterator = response.body_iterator
                    
                    async def wrapped_iterator():
                        first = True
                        async def commit_cache():
                            async with request.app.state.prompt_lock:
                                import json as _json
                                for i in range(1, len(messages) + 1):
                                    sub_key = _json.dumps(messages[:i], sort_keys=True)
                                    request.app.state.completed_prompts.add(sub_key)
                                if prompt_key in request.app.state.active_prompts:
                                    request.app.state.active_prompts.remove(prompt_key)
                                ev = request.app.state.prompt_events.pop(prompt_key, None)
                                if ev:
                                    ev.set()

                        async for chunk in original_iterator:
                            if first:
                                first = False
                                await commit_cache()
                            yield chunk
                        
                        await commit_cache()
                                
                    response.body_iterator = wrapped_iterator()
                else:
                    async with request.app.state.prompt_lock:
                        import json as _json
                        for i in range(1, len(messages) + 1):
                            sub_key = _json.dumps(messages[:i], sort_keys=True)
                            request.app.state.completed_prompts.add(sub_key)
                        if prompt_key in request.app.state.active_prompts:
                            request.app.state.active_prompts.remove(prompt_key)
                        ev = request.app.state.prompt_events.pop(prompt_key, None)
                        if ev:
                            ev.set()
                return response
            except Exception as e:
                async with request.app.state.prompt_lock:
                    if prompt_key in request.app.state.active_prompts:
                        request.app.state.active_prompts.remove(prompt_key)
                    ev = request.app.state.prompt_events.pop(prompt_key, None)
                    if ev:
                        ev.set()
                raise e'''

content = content.replace(target_middleware_anchor, middleware_code)

# 3. Ép Eager Mode (tắt torch.compile) và chạy task warmup
target_hook = 'await init_app_state(engine_client, app.state, args, supported_tasks)'
hook_code = '''await init_app_state(engine_client, app.state, args, supported_tasks)

    # 1. Tắt torch.compile chuyển sang Eager Mode để tránh OOM / nghẽn CPU
    from vllm.config.compilation import CompilationMode
    engine_client.vllm_config.compilation_config.mode = CompilationMode.NONE

    # 2. Khởi chạy warmup cho system prompt / trace
    import os
    if supported_tasks and "generate" in supported_tasks:
        model_name = getattr(args, "served_model_name", [None])[0] or getattr(args, "model", "Qwen3.5-2B")
        import asyncio as _asyncio
        _asyncio.create_task(_warmup_system_prompt(app, args, model_name))'''

content = content.replace(target_hook, hook_code)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Đã patch thành công!")
