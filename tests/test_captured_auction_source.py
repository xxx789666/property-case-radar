import json
from pathlib import Path

import pytest

from crawlers.auction.captured_source import (
    AuctionCaptureError,
    AuctionNetworkAccessDenied,
    CapturedAuctionAnnouncementSource,
    _load_capture_payloads,
    _probe_https_endpoint,
    _result_path_from_stdout,
)


class FakeSocket:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


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


def test_network_preflight_accepts_reachable_moj_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(
        "crawlers.auction.captured_source.socket.create_connection",
        lambda address, timeout: FakeSocket(),
    )

    _probe_https_endpoint("https://www.tpkonsale.moj.gov.tw/Estate", timeout=3)


def test_network_preflight_classifies_windows_access_denied(monkeypatch) -> None:
    def denied(*_args, **_kwargs):
        error = OSError(10013, "access denied")
        error.winerror = 10013
        raise error

    monkeypatch.setattr(
        "crawlers.auction.captured_source.socket.create_connection", denied
    )

    with pytest.raises(AuctionNetworkAccessDenied, match="Proton VPN") as captured:
        _probe_https_endpoint("https://www.tpkonsale.moj.gov.tw/Estate")

    assert captured.value.retryable is False


def test_capture_summary_exposes_failed_counties(tmp_path: Path) -> None:
    county_path = tmp_path / "county.json"
    county_path.write_text(
        json.dumps({"captured_at": "2026-07-28T13:00:00+08:00", "downloads": []}),
        encoding="utf-8",
    )
    summary_path = tmp_path / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "successful_counties": 1,
                "failed_counties": 1,
                "counties": [
                    {"county": "臺北市", "status": "ok", "json": str(county_path)},
                    {"county": "彰化縣", "status": "error", "error": "capture failed"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    source = CapturedAuctionAnnouncementSource(
        tmp_path / "capture.py",
        output_dir=tmp_path / "json",
        download_dir=tmp_path / "downloads",
    )

    assert source.load_result(summary_path) == []
    assert source.last_failed_counties == ("彰化縣",)


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


@pytest.mark.asyncio
async def test_failed_county_retry_runs_only_requested_counties(
    tmp_path: Path, monkeypatch
) -> None:
    source = CapturedAuctionAnnouncementSource(
        tmp_path / "capture.py",
        output_dir=tmp_path / "json",
        download_dir=tmp_path / "downloads",
    )
    successful_payload = tmp_path / "retry-success.json"
    successful_payload.write_text(
        json.dumps({"captured_at": "2026-07-28T13:30:00+08:00", "downloads": []}),
        encoding="utf-8",
    )
    calls: list[tuple[str, ...]] = []

    async def ready() -> None:
        return None

    async def run(*args: str) -> Path:
        calls.append(args)
        if "彰化縣" in args:
            return successful_payload
        raise AuctionCaptureError("still unavailable")

    monkeypatch.setattr(source, "_ensure_ocr_ready", ready)
    monkeypatch.setattr(source, "_run_capture_process", run)

    announcements = await source.retry_failed_counties(("彰化縣", "臺南市"))

    assert announcements == []
    assert source.last_failed_counties == ("臺南市",)
    assert len(calls) == 2
    assert all("--single-query" in call for call in calls)
    assert all("--auto-query" in call for call in calls)
    assert all("--auto-paginate" in call for call in calls)
    assert any("彰化縣" in call for call in calls)
    assert any("臺南市" in call for call in calls)
