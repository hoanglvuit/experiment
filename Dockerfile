FROM vllm/vllm-openai:v0.22.1

COPY trace-round1.json /trace-round1.json
COPY patch.py /patch.py

# Thực thi patch sửa đổi hành vi api_server
RUN python3 /patch.py

# Cài đặt aiohttp để thực hiện loopback warmup request bất đồng bộ
RUN pip install aiohttp
