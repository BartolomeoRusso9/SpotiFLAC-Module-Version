"""library_view.py — Inspect a local or SMB music library from the TUI."""

from __future__ import annotations

import asyncio
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Input, Label, Select, Static, Switch

from ..core.network_destinations import DESTINATIONS, DestinationError


class LibraryPanel(VerticalScroll):
    """Manage saved SMB destinations and scan their audio tags read-only."""

    BORDER_TITLE = "Library"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._profiles: dict[str, dict[str, Any]] = {}
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Label(
            "Read tags directly from a saved SMB share. The scan does not copy or change files.",
            classes="panel-intro",
        )
        yield Label("Saved SMB destination", classes="section-heading")
        yield Select(
            [],
            prompt="Choose a destination…",
            allow_blank=True,
            id="library-smb-profile",
        )
        yield Label("Destination profile", classes="section-heading")
        yield Horizontal(
            Input(placeholder="Profile name", id="library-smb-name"),
            Input(placeholder="SMB server or IP", id="library-smb-host"),
            classes="library-fields",
        )
        yield Horizontal(
            Input(placeholder="Share name", id="library-smb-share"),
            Input(placeholder="Subfolder (optional)", id="library-smb-subdirectory"),
            classes="library-fields",
        )
        yield Horizontal(
            Input(placeholder="Username (optional)", id="library-smb-username"),
            Input(placeholder="Domain (optional)", id="library-smb-domain"),
            classes="library-fields",
        )
        yield Horizontal(
            Input(
                placeholder="Password (session only unless secure keychain is available)",
                password=True,
                id="library-smb-password",
            ),
            Input(
                value="15",
                placeholder="Timeout",
                id="library-smb-timeout",
                type="integer",
            ),
            classes="library-fields",
        )
        yield Horizontal(
            Switch(id="library-smb-encrypt"),
            Label("Require SMB3 encryption"),
            classes="library-encryption",
        )
        yield Horizontal(
            Button("Save profile", id="library-smb-save", variant="primary"),
            Button("Test connection", id="library-smb-test"),
            Button("Scan library", id="library-smb-scan", variant="success"),
            classes="library-actions",
        )
        yield Label("", id="library-status")
        yield Static(
            "Choose or save an SMB destination to start a scan.",
            id="library-report",
            markup=False,
        )

    def on_mount(self) -> None:
        self.run_worker(self.reload_destinations(), exclusive=False)

    def _say(self, message: str) -> None:
        self.query_one("#library-status", Label).update(message)

    async def reload_destinations(self, selected: str = "") -> None:
        try:
            profiles = await asyncio.to_thread(DESTINATIONS.list)
        except Exception as exc:
            self._say(f"Could not read SMB destinations — {exc}")
            return
        if not self.is_mounted:
            return
        self._profiles = {str(profile["id"]): profile for profile in profiles}
        picker = self.query_one("#library-smb-profile", Select)
        picker.set_options(
            [
                (
                    f"{profile['name']} — {profile['host']}/{profile['share']}",
                    profile_id,
                )
                for profile_id, profile in self._profiles.items()
            ]
        )
        if selected in self._profiles:
            picker.value = selected
        else:
            picker.clear()
        if not self._profiles:
            self._say("No saved SMB destinations. Create a profile below.")
        else:
            self._say(f"{len(self._profiles)} destination(s) available.")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "library-smb-profile" or not event.value:
            return
        profile = self._profiles.get(str(event.value))
        if profile is None:
            return
        for field, key in (
            ("name", "name"),
            ("host", "host"),
            ("share", "share"),
            ("subdirectory", "subdirectory"),
            ("username", "username"),
            ("domain", "domain"),
            ("timeout", "timeout"),
        ):
            self.query_one(f"#library-smb-{field}", Input).value = str(
                profile.get(key) or ""
            )
        self.query_one("#library-smb-password", Input).value = ""
        self.query_one("#library-smb-encrypt", Switch).value = (
            profile.get("encrypt") is True
        )
        self._say(f"Selected {profile['name']}. Password is not displayed.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if self._busy:
            return
        if event.button.id == "library-smb-save":
            self.run_worker(self._save_profile(), exclusive=True)
        elif event.button.id == "library-smb-test":
            self.run_worker(self._test_connection(), exclusive=True)
        elif event.button.id == "library-smb-scan":
            self.run_worker(self._scan(), exclusive=True)

    def _profile_data(self) -> dict[str, Any]:
        selected = self.query_one("#library-smb-profile", Select).value
        return {
            "id": str(selected) if selected else None,
            "name": self.query_one("#library-smb-name", Input).value.strip(),
            "host": self.query_one("#library-smb-host", Input).value.strip(),
            "share": self.query_one("#library-smb-share", Input).value.strip(),
            "subdirectory": self.query_one(
                "#library-smb-subdirectory", Input
            ).value.strip(),
            "username": self.query_one("#library-smb-username", Input).value.strip(),
            "domain": self.query_one("#library-smb-domain", Input).value.strip(),
            "timeout": int(self.query_one("#library-smb-timeout", Input).value or "15"),
            "encrypt": self.query_one("#library-smb-encrypt", Switch).value,
        }

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for button_id in ("library-smb-save", "library-smb-test", "library-smb-scan"):
            self.query_one(f"#{button_id}", Button).disabled = busy
        self.query_one("#library-smb-profile", Select).disabled = busy

    def _saved_profile_and_password(self):
        profile_id = self.query_one("#library-smb-profile", Select).value
        profile = DESTINATIONS.get(str(profile_id or ""))
        if profile is None:
            raise DestinationError("Save or select an SMB destination first.")
        password_input = self.query_one("#library-smb-password", Input)
        password = DESTINATIONS.credentials(
            profile,
            password_input.value or None,
        )
        password_input.value = ""
        return profile, password

    async def _save_profile(self) -> None:
        self._set_busy(True)
        try:
            profile = await asyncio.to_thread(
                DESTINATIONS.save,
                self._profile_data(),
                self.query_one("#library-smb-password", Input).value or None,
            )
        except (DestinationError, ValueError) as exc:
            self._say(f"Could not save destination — {exc}")
            return
        finally:
            self.query_one("#library-smb-password", Input).value = ""
            self._set_busy(False)
        await self.reload_destinations(profile.id)
        self._say(f"Saved {profile.name}. Password is stored securely when available.")

    async def _test_connection(self) -> None:
        self._set_busy(True)
        try:
            profile, password = await asyncio.to_thread(
                self._saved_profile_and_password
            )
            from ..core.network_destinations import SMBBackend

            result = await asyncio.to_thread(
                SMBBackend(profile, profile.username, password).test_connection
            )
            self._say(result.get("message", "SMB connection verified."))
        except DestinationError as exc:
            self._say(str(exc))
        except Exception as exc:
            self._say(f"SMB connection failed — {exc}")
        finally:
            self._set_busy(False)

    async def _scan(self) -> None:
        self._set_busy(True)
        report = self.query_one("#library-report", Static)
        report.update("Connecting to SMB library…")
        try:
            profile, password = await asyncio.to_thread(
                self._saved_profile_and_password
            )
            from ..core.smb_library_scan import scan_smb_library

            def progress(done: int, total: int) -> None:
                self.app.call_from_thread(self._update_progress, done, total)

            result = await asyncio.to_thread(
                scan_smb_library,
                profile,
                profile.username,
                password,
                progress=progress,
            )
            report.update(self._format_report(result))
            self._say(
                f"Scanned {result['scan']['files']} file(s); "
                f"{result['scan']['unreadable']} unreadable."
            )
        except DestinationError as exc:
            report.update("")
            self._say(str(exc))
        except Exception as exc:
            report.update("")
            self._say(f"SMB library scan failed — {exc}")
        finally:
            self._set_busy(False)

    def _update_progress(self, done: int, total: int) -> None:
        self._say(f"Scanning SMB library: {done}/{total} files…")

    @staticmethod
    def _format_report(report: dict[str, Any]) -> str:
        from ..core.stats import human_bytes, human_duration

        def item_label(name: str, artist: str | None = None) -> str:
            if artist and artist.strip():
                return f"{name} — {artist}"
            return name

        totals = report["totals"]
        scan = report["scan"]
        lines = [
            f"{report['source_name']} · SMB library",
            f"{totals['tracks']} readable track(s) · {scan['files']} audio file(s) · "
            f"{scan['unreadable']} unreadable",
            f"{totals['artists']} artist(s) · {totals['albums']} album(s) · "
            f"{human_bytes(totals['bytes'])} · "
            f"{human_duration(totals['listening_ms'])} tagged duration",
        ]
        for title, key in (("Artists", "top_artists"), ("Albums", "top_albums")):
            entries = report.get(key) or []
            if entries:
                lines.extend(("", title))
                if key == "top_artists":
                    lines.extend(
                        f"  {entry['name']} ({entry['tracks']})"
                        for entry in entries[:8]
                    )
                else:
                    lines.extend(
                        f"  {item_label(entry['name'], entry.get('artist'))} ({entry['tracks']})"
                        for entry in entries[:8]
                    )
        tracks = report.get("top_tracks") or []
        if tracks:
            lines.extend(("", "Tracks"))
            lines.extend(
                f"  {item_label(entry['name'], entry.get('artist'))} ({entry['tracks']})"
                for entry in tracks[:8]
            )
        for title, key in (("Genres", "top_genres"), ("Decades", "decades")):
            section = report.get(key) or {}
            entries = section.get("entries") or []
            if entries:
                lines.extend(("", title))
                lines.extend(
                    f"  {entry['name']} ({entry['tracks']})" for entry in entries[:8]
                )
        formats = report.get("formats") or []
        if formats:
            lines.extend(("", "Formats"))
            lines.extend(
                f"  {entry['name'].upper()} ({entry['tracks']})"
                for entry in formats[:8]
            )
        return "\n".join(lines)
