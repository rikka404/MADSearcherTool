class UserError(Exception):
    """An actionable error safe to show to the desktop user."""

    def __init__(self, message: str, code: str = "validation"):
        super().__init__(message)
        self.code = code
