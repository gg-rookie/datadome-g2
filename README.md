# datadome-g2

G2 DataDome cookie 服务（单机部署）：Flask API + Firefox 浏览器池同机运行。

## 架构

```
Client  --POST /cookie/acquire-->  Flask (app.py)
                                      |
                                      v
                               BrowserPool + Firefox
                                      |
                                      v
                                    Redis
```

## 快速开始

```powershell
uv venv --python 3.12 .venv
uv pip install -r requirements.txt --python .venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m ruyipage install
copy .env.example .env
# 编辑 .env：FIREFOX_PATH、RDS_*、API_KEY 等

.\.venv\Scripts\python.exe app.py
```

生产环境建议：

```powershell
waitress-serve --listen=0.0.0.0:51051 --threads=8 app:app
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/api/datadome/v1/config` | 配置（需 API Key） |
| POST | `/api/datadome/v1/cookie/acquire` | 取 cookie（阻塞） |
| GET | `/api/datadome/v1/cookie` | 最近一次 cookie（调试） |

```bash
curl -X POST "http://127.0.0.1:51051/api/datadome/v1/cookie/acquire?key=YOUR_KEY"
```

## 配置说明

复制 `.env.example` 为 `.env` 后填写：

- `FIREFOX_PATH`：ruyipage 安装的 Firefox 路径
- `PROXY_URL`：可选，`http://user:pass@host:port`
- `RDS_*` / `REDIS_KEY`：Redis 连接与 key 前缀

**勿将 `.env` 提交到 Git。**

## 测试

```powershell
.\.venv\Scripts\python.exe test_api.py
.\.venv\Scripts\python.exe test_stress.py --threads 3 --rounds 1
.\.venv\Scripts\python.exe test_pool_validity.py
```

## 目录

```
app.py
api/routes.py
services/browser.py
services/browser_pool.py
services/cookie_store.py
config.py
```
