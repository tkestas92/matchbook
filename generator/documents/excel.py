"""One Excel media plan per client: the IOs a planner keeps on their laptop."""

import io
import zipfile
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.writer.excel import ExcelWriter

from generator.world import Client, InsertionOrder, World

PLAN_HEADERS = ("Platform", "Campaign ref", "Flight start", "Flight end", "Budget (EUR)")
HEADER_ROW = 6
# openpyxl's save() stamps "now" into document properties and zip entries; pin both.
FIXED_TIMESTAMP = datetime(2026, 1, 1)
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def write_media_plan(world: World, client: Client, path: Path) -> None:
    platforms = {p.key: p.name for p in world.platforms}
    ios = [o for o in world.insertion_orders if o.client_code == client.code]
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Media plan"
    sheet["A1"] = f"Media plan — {client.name}"
    sheet["A1"].font = Font(bold=True, size=14)
    sheet["A2"], sheet["B2"] = "Client code", client.code
    sheet["A3"], sheet["B3"] = "Billing model", client.billing_model
    sheet["A4"], sheet["B4"] = "Period", period_label(ios)
    sheet.append([])  # row 5 left blank
    sheet.append(list(PLAN_HEADERS))
    for cell in sheet[HEADER_ROW]:
        cell.font = Font(bold=True)
    for order in ios:
        sheet.append(
            [
                platforms[order.platform_key],
                order.campaign_ref,
                order.start_date,
                order.end_date,
                order.budget,
            ]
        )
        row = sheet.max_row
        sheet.cell(row, 3).number_format = "yyyy-mm-dd"
        sheet.cell(row, 4).number_format = "yyyy-mm-dd"
        sheet.cell(row, 5).number_format = "#,##0.00"
    for column, width in zip("ABCDE", (12, 20, 14, 14, 16), strict=True):
        sheet.column_dimensions[column].width = width
    workbook.properties.created = FIXED_TIMESTAMP
    workbook.properties.modified = FIXED_TIMESTAMP
    workbook.properties.creator = "Matchbook generator"
    path.write_bytes(normalize_zip(workbook_bytes(workbook)))


def workbook_bytes(workbook: Workbook) -> bytes:
    # ExcelWriter directly: Workbook.save() would overwrite properties.modified with now.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        ExcelWriter(workbook, archive).save()
    return buffer.getvalue()


def period_label(ios: list[InsertionOrder]) -> str:
    start = min(o.start_date for o in ios)
    end = max(o.end_date for o in ios)
    return f"{start.isoformat()} – {end.isoformat()}"


def normalize_zip(data: bytes) -> bytes:
    """Rewrite the zip with fixed entry timestamps so equal content gives equal bytes."""
    output = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(data)) as source,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for info in source.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=ZIP_DATE_TIME)
            fixed.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(fixed, source.read(info.filename))
    return output.getvalue()
