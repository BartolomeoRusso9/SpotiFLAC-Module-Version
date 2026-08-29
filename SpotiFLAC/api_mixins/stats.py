"""api_mixins/stats.py — the dashboard's GUI/web surface.

See core/stats.py for what is computed and why each ranking carries its own
coverage. This mixin is only the adapter (see api_mixins/__init__.py): one
read-only call, answered synchronously because it is a single SQLite query
over a table the same process already writes.

Multi-user mode is the one thing decided here rather than there: each
`SpotiFLAC_API` instance belongs to one account (see webapp.py's
ApiRegistry), so a dashboard asked for through the bridge is that account's
own — never a view of everybody's downloads.
"""

from __future__ import annotations


class StatsMixin:
    def get_stats(
        self,
        year: int | None = None,
        days: int | None = None,
        top: int = 10,
    ) -> dict:
        """Totals, rankings and activity for one period.

        `year` and `days` are alternative ways to name the period; giving
        neither covers the whole history. Never raises: the dashboard is a
        view, and a database that cannot be read should not take the
        interface down with it.
        """
        from ..core import stats

        try:
            window = stats.parse_window(
                year=int(year) if year else None,
                days=int(days) if days else None,
            )
            return stats.wrapped(
                owner=self.stats_owner,
                window=window,
                top=max(1, min(100, int(top or stats.DEFAULT_TOP))),
            )
        except Exception as e:
            return {"error": str(e)}

    @property
    def stats_owner(self) -> str | None:
        """Whose downloads this instance's dashboard covers.

        `None` means the whole instance, which is right for the desktop app
        and for single-user `--web`. In multi-user mode webapp.py builds one
        Api per account and sets `owner`, and the dashboard narrows to it.
        """
        owner = getattr(self, "owner", "") or ""
        return owner or None
