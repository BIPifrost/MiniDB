"""D2 写入合同中的轻量事务状态守卫。

TransactionGuard 只负责状态和调用顺序，不负责快照、回滚或目录修改。
真正的 TransactionManager 可以在后续接管状态转换，但各模块先共享这一
组状态名称和校验规则，避免 StorageEngine/Executor 各写一套字符串判断。
"""

from __future__ import annotations

from enum import Enum

from minidb.core import errors


class TransactionState(Enum):
    BOOTSTRAP = "BOOTSTRAP"
    READ_ONLY_STARTUP = "READ_ONLY_STARTUP"
    RECOVERY = "RECOVERY"
    IDLE = "IDLE"
    ACTIVE = "ACTIVE"
    COMMITTING = "COMMITTING"
    ROLLING_BACK = "ROLLING_BACK"
    FAILED = "FAILED"
    CLOSED = "CLOSED"


class TransactionGuard:
    """在进程内约束写入状态的最小合同实现。"""

    def __init__(self, initial: TransactionState) -> None:
        if not isinstance(initial, TransactionState):
            raise TypeError("initial must be a TransactionState")
        self._state = initial
        self._generation = 0

    @property
    def state(self) -> TransactionState:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    def require(self, *allowed: TransactionState, operation: str) -> None:
        """确认当前状态允许执行 operation，不改变状态。"""
        if not allowed:
            raise ValueError("at least one allowed state is required")
        if self._state not in allowed:
            raise errors.DbError(
                errors.ErrorStage.STORAGE,
                errors.INVALID_TRANSACTION_STATE,
                "事务状态不允许执行当前操作",
                context={
                    "operation": operation,
                    "expected_state": [state.value for state in allowed],
                    "actual_state": self._state.value,
                },
            )

    def transition(
        self,
        expected: TransactionState | tuple[TransactionState, ...],
        target: TransactionState,
        *,
        operation: str,
    ) -> None:
        allowed = (expected,) if isinstance(expected, TransactionState) else expected
        self.require(*allowed, operation=operation)
        if not isinstance(target, TransactionState):
            raise TypeError("target must be a TransactionState")
        self._state = target
        self._generation += 1

    def transition_to(
        self, target: TransactionState, *, operation: str = "TransactionGuard.transition_to"
    ) -> None:
        """按总计划接口从当前状态推进到 target。"""
        transitions = {
            TransactionState.BOOTSTRAP: {TransactionState.IDLE, TransactionState.RECOVERY, TransactionState.FAILED},
            TransactionState.READ_ONLY_STARTUP: {TransactionState.IDLE, TransactionState.RECOVERY, TransactionState.FAILED},
            TransactionState.RECOVERY: {TransactionState.IDLE, TransactionState.FAILED},
            TransactionState.IDLE: {TransactionState.ACTIVE, TransactionState.CLOSED, TransactionState.FAILED},
            TransactionState.ACTIVE: {TransactionState.COMMITTING, TransactionState.ROLLING_BACK, TransactionState.FAILED},
            TransactionState.COMMITTING: {TransactionState.IDLE, TransactionState.FAILED},
            TransactionState.ROLLING_BACK: {TransactionState.IDLE, TransactionState.FAILED},
            TransactionState.FAILED: {TransactionState.CLOSED},
            TransactionState.CLOSED: set(),
        }
        if not isinstance(target, TransactionState):
            raise TypeError("target must be a TransactionState")
        if target not in transitions[self._state]:
            raise errors.DbError(
                errors.ErrorStage.STORAGE,
                errors.INVALID_TRANSACTION_STATE,
                "事务状态转换不被允许",
                context={
                    "operation": operation,
                    "actual_state": self._state.value,
                    "target_state": target.value,
                },
            )
        self._state = target
        self._generation += 1

    def fail(self, *, operation: str) -> None:
        """任何不可继续错误都进入 FAILED，后续写入必须被拒绝。"""
        self.require(
            TransactionState.BOOTSTRAP,
            TransactionState.READ_ONLY_STARTUP,
            TransactionState.RECOVERY,
            TransactionState.IDLE,
            TransactionState.ACTIVE,
            TransactionState.COMMITTING,
            TransactionState.ROLLING_BACK,
            operation=operation,
        )
        self._state = TransactionState.FAILED
        self._generation += 1

    def close(self, *, operation: str = "TransactionGuard.close") -> None:
        self.require(
            TransactionState.BOOTSTRAP,
            TransactionState.IDLE,
            TransactionState.READ_ONLY_STARTUP,
            TransactionState.RECOVERY,
            TransactionState.FAILED,
            operation=operation,
        )
        self._state = TransactionState.CLOSED
        self._generation += 1


__all__ = ["TransactionGuard", "TransactionState"]
