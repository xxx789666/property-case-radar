import json
from pathlib import Path

import pytest

from crawlers.auction.captured_source import (
    AuctionCaptureError,
    _load_capture_payloads,
    _result_path_from_stdout,
)


def test_capture_stdout_and_three_county_summary_are_resolved(tmp_path: Path) -> None:
    county_path = tmp_path / "county.json"
    county_payload = {"captured_at": "2026-07-27T12:00:00+08:00", "downloads": []}
    county_path.write_text(json.dumps(county_payload), encoding="utf-8")
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "successful_counties": 1,
                "failed_counties": 0,
                "counties": [{"status": "ok", "json": str(county_path)}],
            }
        ),
        encoding="utf-8",
    )

    stdout = f'progress\n{{"status": "ok", "output": "{summary_path.as_posix()}"}}\n'
    assert _result_path_from_stdout(stdout) == summary_path
    assert _load_capture_payloads(summary_path) == [county_payload]


def test_capture_stdout_without_result_is_rejected() -> None:
    with pytest.raises(AuctionCaptureError, match="did not report"):
        _result_path_from_stdout("not json\n")


@pytest.mark.asyncio
async def test_ocr_is_not_started_when_api_is_already_ready(
    tmp_path: Path, monkeypatch
) -> None:
    from crawlers.auction.captured_source import CapturedAuctionAnnouncementSource

    source = CapturedAuctionAnnouncementSource(
        tmp_path / "capture.py",
        output_dir=tmp_path / "json",
        download_dir=tmp_path / "downloads",
    )

    async def ready() -> bool:
        return True

    monkeypatch.setattr(source, "_ocr_is_ready", ready)
    monkeypatch.setattr(
        source,
        "_launch_ocr",
        lambda: pytest.fail("running Umi-OCR must not be launched again"),
    )
    await source._ensure_ocr_ready()


@pytest.mark.asyncio
async def test_ocr_is_launched_and_waited_for_when_api_is_down(
    tmp_path: Path, monkeypatch
) -> None:
    from crawlers.auction.captured_source import CapturedAuctionAnnouncementSource

    executable = tmp_path / "Umi-OCR.exe"
    executable.write_bytes(b"fixture")
    source = CapturedAuctionAnnouncementSource(
        tmp_path / "capture.py",
        output_dir=tmp_path / "json",
        download_dir=tmp_path / "downloads",
        umi_ocr_executable=executable,
    )
    readiness = iter((False, True))
    launches: list[bool] = []

    async def ready() -> bool:
        return next(readiness)

    async def no_wait(_: float) -> None:
        return None

    monkeypatch.setattr(source, "_ocr_is_ready", ready)
    monkeypatch.setattr(source, "_launch_ocr", lambda: launches.append(True))
    monkeypatch.setattr("crawlers.auction.captured_source.asyncio.sleep", no_wait)

    await source._ensure_ocr_ready()
    assert launches == [True]


@pytest.mark.asyncio
async def test_missing_ocr_executable_has_actionable_error(
    tmp_path: Path, monkeypatch
) -> None:
    from crawlers.auction.captured_source import CapturedAuctionAnnouncementSource

    source = CapturedAuctionAnnouncementSource(
        tmp_path / "capture.py",
        output_dir=tmp_path / "json",
        download_dir=tmp_path / "downloads",
        umi_ocr_executable=tmp_path / "missing.exe",
    )

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(source, "_ocr_is_ready", unavailable)
    with pytest.raises(AuctionCaptureError, match="executable was not found"):
        await source._ensure_ocr_ready()
