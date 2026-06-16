# datadome-g2

G2 DataDome Cookie 自动补池服务。

当前项目不再启动 Flask HTTP 服务，而是作为 Redis 轮询 worker 运行：
当 Redis 中可用 cookie 数量不足目标数量时，自动启动 ruyipage Firefox 生产并验证 cookie，验证通过后写入 Redis 池。

## 架构

```text
app.py worker 主循环
  -> 读取 Redis 池长度
  -> 如果池数量 < TARGET_POOL_SIZE
  -> BrowserPool + ruyipage Firefox 打开 G2 页面
  -> 捕获浏览器真实发出的 Cookie header
  -> 请求 G2 验证 cookie 可用性
  -> 写入 latest key，并追加到 Redis pool list
```

默认运行路径没有 HTTP 接口。

## 启动

```powershell
uv venv --python 3.12 .venv
uv pip install -r requirements.txt --python .venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m ruyipage install
copy .env.example .env
.\.venv\Scripts\python.exe app.py
```

启动前需要编辑 `.env`，至少配置：

- `FIREFOX_PATH`：ruyipage Firefox 可执行文件路径。
- `RDS_HOST` / `RDS_PORT` / `RDS_PASSWORD`：Redis 连接信息。
- `TARGET_URL`：目标 G2 页面，默认是 Slack reviews 页面。

## Redis Key

- `REDIS_KEY`：最近一次验证通过的 cookie payload。
- `REDIS_KEY:pool`：可用 cookie 池，类型为 Redis list。

worker 会轮询 Redis。当 `LLEN REDIS_KEY:pool` 小于 `TARGET_POOL_SIZE` 时，会持续生产并补齐 cookie。

## 重要配置

- `TARGET_POOL_SIZE`：目标 cookie 池数量，默认 `10`。
- `REDIS_POLL_INTERVAL`：池已满时的轮询间隔。
- `REFILL_SUCCESS_INTERVAL`：成功生产一条 cookie 后的等待时间。
- `REFILL_FAILURE_INTERVAL`：生产失败后的等待时间。
- `FIREFOX_PATH`：ruyipage Firefox 路径。
- `PROFILES_DIR`：Firefox profile 目录。
- `BROWSER_POOL_SIZE`：浏览器 worker 进程数。G2 场景建议保持较低，默认 `1`。
- `BATCH_BASE_PORT`：ruyipage Firefox 调试端口起点。
- `REDIS_TTL`：latest key 和 pool list 的过期时间，`0` 表示不过期。

## 验证 Cookie 池

```powershell
.\.venv\Scripts\python.exe test_pool_validity.py
```

脚本会读取 `REDIS_KEY:pool` 中的全部 cookie，并请求 `TARGET_URL` 验证是否返回正常页面。

## 注意事项

- 不要提交 `.env`。
- `profiles/`、`*.log`、`test_*_results.json` 都是运行时产物，不应提交。
- 当前 cookie 提取方式优先使用 Firefox 真实网络请求里的完整 `Cookie` header；不要只从 `cookies.sqlite` 拼接 cookie，否则容易出现浏览器页面正常但重放 403 的情况。
