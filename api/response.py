"""统一 API 响应格式 {code, message, data} — 供 Flask-RESTful Resource 返回。"""


def ok(data=None, message: str = "success"):
    return {"code": 0, "message": message, "data": data}


def fail(message: str, code: int = 1, http_status: int = 400, data=None):
    return {"code": code, "message": message, "data": data}, http_status
