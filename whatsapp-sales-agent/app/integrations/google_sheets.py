"""Google Sheets adapters: an output sink keyed by ``lead_id`` and a polling lead source.

The real backend uses the Sheets API v4 with a service account (share the
spreadsheet with the service-account email as *Editor* - nothing more is needed).
The output sink never appends a row for a lead that already has one: it always
re-reads the key column first, then updates in place.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from app.ingestion.csv_file import normalize_header

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA"""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


class SheetsBackend(Protocol):
    def get_values(self) -> list[list[str]]: ...

    def update_row(self, row_number: int, values: list[str]) -> None: ...  # 1-based

    def append_row(self, values: list[str]) -> None: ...


class GoogleSheetsApiBackend:
    def __init__(self, spreadsheet_id: str, worksheet: str, service_account_file: str) -> None:
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Install google-api-python-client and google-auth for Sheets") from exc
        creds = Credentials.from_service_account_file(service_account_file, scopes=SCOPES)
        self._values = build("sheets", "v4", credentials=creds, cache_discovery=False) \
            .spreadsheets().values()
        self.spreadsheet_id = spreadsheet_id
        self.worksheet = worksheet
        self._lock = threading.Lock()

    def _range(self, a1: str = "") -> str:
        name = self.worksheet.replace("'", "''")
        return f"'{name}'!{a1}" if a1 else f"'{name}'"

    def get_values(self) -> list[list[str]]:
        result = self._values.get(spreadsheetId=self.spreadsheet_id, range=self._range(),
                                  valueRenderOption="FORMATTED_VALUE").execute()
        return [[str(c) for c in row] for row in result.get("values", [])]

    def update_row(self, row_number: int, values: list[str]) -> None:
        a1 = f"A{row_number}:{column_letter(max(len(values) - 1, 0))}{row_number}"
        with self._lock:
            self._values.update(spreadsheetId=self.spreadsheet_id, range=self._range(a1),
                                valueInputOption="RAW", body={"values": [values]}).execute()

    def append_row(self, values: list[str]) -> None:
        with self._lock:
            self._values.append(spreadsheetId=self.spreadsheet_id, range=self._range("A1"),
                                valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                                body={"values": [values]}).execute()


class InMemorySheetsBackend:
    """Test double that behaves like a worksheet."""

    def __init__(self, values: list[list[str]] | None = None) -> None:
        self.values = [list(r) for r in (values or [])]
        self.fail_next = 0
        self.append_calls = 0

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("simulated Sheets API failure")

    def get_values(self) -> list[list[str]]:
        self._maybe_fail()
        return [list(r) for r in self.values]

    def update_row(self, row_number: int, values: list[str]) -> None:
        self._maybe_fail()
        while len(self.values) < row_number:
            self.values.append([])
        self.values[row_number - 1] = list(values)

    def append_row(self, values: list[str]) -> None:
        self._maybe_fail()
        self.append_calls += 1
        self.values.append(list(values))


class GoogleSheetsSink:
    def __init__(self, backend: SheetsBackend, key_column: str = "lead_id") -> None:
        self.backend = backend
        self.key_column = key_column

    def upsert(self, lead_id: str, input_values: dict[str, str], output_values: dict[str, str]) -> str:
        """Update the lead's row (adding missing header columns); append only if absent."""
        values = self.backend.get_values()
        if not values:
            header = [self.key_column, *[k for k in input_values if k != self.key_column]]
            values = [header]
            self.backend.update_row(1, header)
        header = list(values[0])
        norm = [normalize_header(h) for h in header]
        header_changed = False
        for col in [*input_values.keys(), *output_values.keys()]:
            if col not in norm:
                header.append(col)
                norm.append(col)
                header_changed = True
        if header_changed:
            self.backend.update_row(1, header)
        key_idx = norm.index(self.key_column)
        for i, row in enumerate(values[1:], start=2):
            if key_idx < len(row) and row[key_idx].strip() == lead_id:
                new_row = list(row) + [""] * (len(header) - len(row))
                for col, val in output_values.items():
                    new_row[norm.index(col)] = val
                self.backend.update_row(i, new_row)
                return "updated"
        new_row = [""] * len(header)
        new_row[key_idx] = lead_id
        for col, val in {**input_values, **output_values}.items():
            if col in norm:
                new_row[norm.index(col)] = val
        self.backend.append_row(new_row)
        return "appended"


class GoogleSheetsLeadSource:
    """Polls a worksheet for lead rows (Sheets has no local file events to watch)."""

    def __init__(self, backend: SheetsBackend) -> None:
        self.backend = backend

    def fetch_rows(self) -> list[dict[str, str]]:
        values = self.backend.get_values()
        if not values:
            return []
        header = [normalize_header(h) for h in values[0]]
        rows = []
        for raw in values[1:]:
            row = {h: (raw[i].strip() if i < len(raw) else "") for i, h in enumerate(header) if h}
            if any(row.values()):
                rows.append(row)
        return rows
