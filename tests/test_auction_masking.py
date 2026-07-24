from notifications.auction_masking import display_debtor_owner, mask_name, mask_name_field


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


def test_display_debtor_owner_public_masks() -> None:
    debtor, owner = display_debtor_owner("王小明", "陳大文", audience="public")
    assert debtor == "王○○"
    assert owner == "陳○○"


def test_display_debtor_owner_private_unmasked() -> None:
    debtor, owner = display_debtor_owner("王小明", "陳大文", audience="private")
    assert debtor == "王小明"
    assert owner == "陳大文"


def test_display_debtor_owner_does_not_mutate_inputs() -> None:
    original_debtor = "王小明"
    original_owner = "陳大文"
    display_debtor_owner(original_debtor, original_owner, audience="public")
    assert original_debtor == "王小明"
    assert original_owner == "陳大文"
