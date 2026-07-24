from property_case_radar.auction.masking import mask_name, mask_name_field, redact_case_for_public, view_for_audience
from property_case_radar.auction.models import AuctionCase


def test_mask_name_basic() -> None:
    assert mask_name("王小明") == "王○○"
    assert mask_name("王明") == "王○"
    assert mask_name("A") == "A"
    assert mask_name("") == ""
    assert mask_name("  ") == ""


def test_mask_name_field_multiple_names() -> None:
    assert mask_name_field("王小明、陳大文") == "王○○、陳○○"
    assert mask_name_field("王小明,陳大文") == "王○○、陳○○"
    assert mask_name_field("") == ""


def test_redact_case_for_public_masks_debtor_and_owner(sample_case: AuctionCase) -> None:
    redacted = redact_case_for_public(sample_case)
    assert redacted.debtor == "王○○"
    assert redacted.owner == "王○○"
    # original untouched
    assert sample_case.debtor == "王小明"
    assert sample_case.owner == "王小明"
    # non-PII fields preserved
    assert redacted.case_number == sample_case.case_number
    assert redacted.city == sample_case.city


def test_view_for_audience_public_vs_private(sample_case: AuctionCase) -> None:
    public_view = view_for_audience(sample_case, "public")
    private_view = view_for_audience(sample_case, "private")
    assert public_view.debtor == "王○○"
    assert private_view.debtor == "王小明"
    assert private_view is sample_case  # private returns the original object, no copy
