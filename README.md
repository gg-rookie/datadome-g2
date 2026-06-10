# datadome-g2

**单机版**：在生产服务器上同时跑 Flask API + Firefox 浏览器池（旧架构）。

与 `datadome-service`（中转 API + 生产 worker 分离）并存，适合一台机器搞定全部的场景。

## 架构

```
下游 POST /cookie/acquire  →  BrowserPool(N)  →  Firefox  →  cookie → Redis
         （本机 Flask app.py，无 Redis 任务队列）
```

## 启动

```powershell
.\.venv\Scripts\python.exe app.py
# 生产: waitress-serve --listen=0.0.0.0:51051 --threads=8 app:app
```

`app.py` 启动时会初始化 BrowserPool；Firefox 仅在收到 `POST /cookie/acquire` 时打开。

## 下游

```bash
curl -X POST "http://<本机IP>:51051/api/datadome/v1/cookie/acquire?key=xxx"
```

或读 Redis：`datadome:g2:ck` / `datadome:g2:ck:pool`

## 安装

```powershell
uv venv --python 3.12 .venv
uv pip install -r requirements.txt --python .venv\Scripts\python.exe
.\.venv\Scripts\python.exe -m ruyipage install
copy .env.example .env
```

## 自测

```powershell
.\.venv\Scripts\python.exe test_api.py
.\.venv\Scripts\python.exe test_stress.py --threads 3 --rounds 1
.\.venv\Scripts\python.exe test_pool_validity.py
```

## 与 datadome-service 的区别

| | datadome-g2（本目录） | datadome-service |
|---|---|---|
| 部署 | 单机 Flask + 浏览器 | 中转 `app.py` + 生产 `worker.py` |
| 入口 | `app.py` | 中转 `app.py` / 生产 `worker.py` |
| Redis 队列 | 无 | 有（`:queue` / `:task:{id}`） |

## 目录

```
app.py                 Flask + BrowserPool 入口
api/routes.py          HTTP 路由
services/browser.py    Firefox 取 cookie
services/browser_pool.py
services/cookie_store.py
config.py
```
