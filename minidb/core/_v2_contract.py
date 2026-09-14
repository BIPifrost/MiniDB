"""v2 对接边界：只调用公共错误模块，不复制队友的错误码或异常类型。"""
from minidb.core import errors


def fail(code, message, *, stage="SEMANTIC", span=None, **context):
    """队友尚未登记的 v2 错误明确报接口未提供，不伪装成其他业务错误。"""
    # 待赵凯航在 core/errors.py 提供文档第15节的错误码、阶段集合。
    # 正式签名仍是 DbError(stage, code, message, span=None, context=dict)。
    if code not in errors.ALL_ERROR_CODES:
        raise NotImplementedError(f"core.errors 尚未提供 v2 错误码 {code}：{message}")
    raise errors.DbError(getattr(errors.ErrorStage, stage), code, message, span,
                         {key: value for key, value in context.items() if value is not None})


def require_method(obj, name, signature):
    """标明缺失的外部调用合同；不提供返回假结果的生产替身。"""
    method = getattr(obj, name, None)
    if not callable(method):
        raise NotImplementedError(f"{type(obj).__name__}.{signature} 尚未提供")
    return method
