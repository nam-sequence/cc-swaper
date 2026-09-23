"""Terminal tables for account usage and in-place loading updates."""

from __future__ import annotations

import textwrap
from collections.abc import Mapping, Sequence
from typing import TextIO


def _clean(value: object) -> str:
    return "".join(character if character.isprintable() else "?" for character in str(value))


def _bar(value: object, width: int = 10) -> str:
    if not isinstance(value, int) or isinstance(value, bool):
        return "No data"
    filled = min(width, max(0, (value * width + 50) // 100))
    return f"[{'█' * filled}{'░' * (width - filled)}] {value}%"


def _grid(headers: Sequence[str], rows: Sequence[Sequence[str]], widths: Sequence[int]) -> str:
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def render_row(cells: Sequence[str]) -> list[str]:
        wrapped = [textwrap.wrap(_clean(cell), width=width) or [""]
                   for cell, width in zip(cells, widths)]
        height = max(len(lines) for lines in wrapped)
        return [
            "| " + " | ".join(
                (wrapped[index][line] if line < len(wrapped[index]) else "").ljust(width)
                for index, width in enumerate(widths)
            ) + " |"
            for line in range(height)
        ]

    lines = [border, *render_row(headers), border]
    for index, row in enumerate(rows):
        if index > 0 and row[0]:
            lines.append(border)
        lines.extend(render_row(row))
    lines.append(border)
    return "\n".join(lines)


def render_usage_table(
    names: Sequence[str],
    reports: Mapping[str, Mapping[str, object]],
    pending: Mapping[str, str],
    *,
    columns: int,
    compact: bool = False,
) -> str:
    """Render in configured profile order, regardless of completion order."""

    columns = max(60, columns)
    if compact:
        if columns < 80:
            widths = (9, 15, 15, max(8, columns - 9 - 15 - 15 - 13))
            bar_width = 8
        else:
            widths = (12, 17, 17, max(14, columns - 12 - 17 - 17 - 13))
            bar_width = 10
        rows: list[tuple[str, str, str, str]] = []
        for name in names:
            report = reports.get(name)
            account = name
            if report is None:
                rows.append((account, "—", "—", pending.get(name, "Loading")))
            elif "error" in report:
                rows.append((account, "—", "—", f"Error: {report['error']}"))
            else:
                plan = report.get("plan")
                if plan:
                    account += f" ({plan})"
                five_hour = report.get("five_hour")
                seven_day = report.get("seven_day")
                five_percent = five_hour.get("used_percent") if isinstance(five_hour, dict) else None
                seven_percent = seven_day.get("used_percent") if isinstance(seven_day, dict) else None
                rows.append((account, _bar(five_percent, bar_width),
                             _bar(seven_percent, bar_width), "✓ Done"))
        return _grid(("Account", "5-hour", "7-day", "Status"), rows, widths)

    if columns < 80:
        widths = (9, 12, 16, max(12, columns - 9 - 12 - 16 - 13))
        bar_width = 8
    else:
        widths = (12, 14, 18, max(18, columns - 12 - 14 - 18 - 13))
        bar_width = 10
    rows = []
    for name in names:
        report = reports.get(name)
        if report is None:
            rows.append((name, "5-hour / 7-day", "—", pending.get(name, "Loading")))
            continue
        if "error" in report:
            rows.append((name, "Error", "—", str(report["error"])))
            continue
        plan = report.get("plan")
        account = f"{name} ({plan})" if plan else name
        metrics: list[tuple[str, object]] = [
            ("5-hour", report.get("five_hour")),
            ("7-day", report.get("seven_day")),
        ]
        model_weekly = report.get("model_weekly")
        if isinstance(model_weekly, list):
            for item in model_weekly:
                if isinstance(item, dict):
                    metrics.append((f"7-day ({item.get('model', '?')})", item))
        for index, (label, metric) in enumerate(metrics):
            percent = metric.get("used_percent") if isinstance(metric, dict) else None
            reset = metric.get("resets_at") if isinstance(metric, dict) else None
            rows.append((
                account if index == 0 else "",
                label,
                _bar(percent, bar_width),
                str(reset) if reset else "Reset time unavailable",
            ))
    return _grid(("Account", "Window", "Used", "Reset / status"), rows, widths)


class LiveUsageTable:
    """Show transient progress on the alternate screen; leave a final static table."""

    def __init__(self, stream: TextIO, *, enabled: bool):
        self.stream = stream
        self.enabled = enabled
        self._entered = False

    def __enter__(self) -> "LiveUsageTable":
        if self.enabled:
            try:
                self.stream.write("\x1b[?1049h\x1b[?25l")
                self.stream.flush()
                self._entered = True
            except BaseException:
                self.stream.write("\x1b[?25h\x1b[?1049l")
                self.stream.flush()
                raise
        return self

    def draw(self, table: str) -> None:
        if self.enabled:
            self.stream.write("\x1b[H\x1b[2J" + table + "\n")
            self.stream.flush()

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        if self._entered:
            self.stream.write("\x1b[?25h\x1b[?1049l")
            self.stream.flush()
            self._entered = False
