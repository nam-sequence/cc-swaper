"""Multi-account switcher for Claude Code."""

from importlib.metadata import version

# This engine is vendored into the cc-swaper distribution. Keeping the
# distribution lookup here makes `ccs accounts` report the bundled release
# version without installing or migrating the upstream `claude-swap` package.
__version__ = version("cc-swaper")

from claude_swap.switcher import ClaudeAccountSwitcher

__all__ = ["ClaudeAccountSwitcher", "__version__"]
