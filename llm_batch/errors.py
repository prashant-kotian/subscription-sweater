"""Exception types used across the tool."""


class ToolError(Exception):
    """Base class for all tool errors (shown to the user)."""


class InputError(ToolError):
    """Bad input file / bad settings."""


class LoginError(ToolError):
    """The user must log in (or the browser could not start)."""


class PromptError(ToolError):
    """A single prompt failed; the batch can keep going."""


class Stopped(Exception):
    """The user pressed Stop / interrupted the run."""
