# OpenDify AGENTS.md

OpenDify 是一个将 Dify API 转换为 OpenAI API 格式的代理服务器（Flask 单文件应用，`main.py`）。

## 服务运行方式

- 启动命令：`python3 main.py`（在仓库根目录，读取 `.env` 配置）
- 默认监听：`127.0.0.1:5000`（由 `.env` 中 `SERVER_HOST` / `SERVER_PORT` 控制）
- 进程以守护方式运行（PPID 1），日志输出到 `/tmp/opendify.log`
- 依赖：`requirements.txt`（flask / httpx / python-dotenv）

## 重启流程

修改 `main.py` 后必须重启服务才能生效。步骤：

```bash
# 1. 找到旧进程（确认 PID 与监听端口）
ss -tlnp | grep 5000
# 或：pgrep -af "python3 main.py"

# 2. 停止旧进程（先 SIGTERM 优雅停止，必要时 SIGKILL）
kill <PID>
sleep 1
ps -p <PID> >/dev/null 2>&1 && kill -9 <PID> || echo "old process stopped"

# 3. 启动新进程（nohup 守护方式，日志写入 /tmp/opendify.log）
cd /home/wsl-ubuntu24/code/OpenDify && nohup python3 main.py > /tmp/opendify.log 2>&1 &

# 4. 验证
ss -tlnp | grep 5000          # 确认监听
curl -s http://127.0.0.1:5000/v1/models   # 健康检查，应返回模型列表 JSON
tail -5 /tmp/opendify.log     # 确认无启动报错
```

## 测试

改动后先跑 mock 端到端测试（含 DSML happy path 与 `<invoke>` 泄漏场景校验）：

```bash
cd /home/wsl-ubuntu24/code/OpenDify && python3 mock_e2e_test.py
```

全部通过时输出 `✓✓ 端到端通过` 且退出码为 0。注意测试占用 5998/5999 端口，若报 `Address already in use` 说明上次测试进程未完全退出，稍等重试即可。