class HoldfastError(Exception):
    """Base error. Messages are safe to show; they must not contain secrets."""


class ConfigError(HoldfastError):
    pass


class MarketError(HoldfastError):
    pass


class ParseError(HoldfastError):
    pass


class InvalidOrder(HoldfastError):
    pass


class PolicyError(HoldfastError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__(",".join(reasons) if reasons else "policy")


class ApprovalError(HoldfastError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class LiveTradingError(HoldfastError):
    pass


class LLMError(HoldfastError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(message)
