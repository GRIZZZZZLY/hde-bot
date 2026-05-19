from bot.ticket_fields import (
    FIELD_OKRUZHENIE,
    FIELD_KLASSIFIKACIYA,
    FIELD_ROL,
    KLASSIFIKACIYA_OBORUDOVANIE,
    ROL_NE_VAZHNO,
    OKRUZHENIE_OPTIONS,
)


def test_field_ids_are_strings():
    assert FIELD_OKRUZHENIE == "2"
    assert FIELD_KLASSIFIKACIYA == "3"
    assert FIELD_ROL == "24"


def test_fixed_option_ids():
    assert KLASSIFIKACIYA_OBORUDOVANIE == "20"
    assert ROL_NE_VAZHNO == "197"


def test_okruzhenie_options_complete():
    # 21 environments discovered from production scan
    assert len(OKRUZHENIE_OPTIONS) == 21
    assert OKRUZHENIE_OPTIONS["11"] == "POS"
    assert OKRUZHENIE_OPTIONS["146"] == "Эвотор"
    assert OKRUZHENIE_OPTIONS["14"] == "Другое"
    # all keys are numeric strings
    assert all(k.isdigit() for k in OKRUZHENIE_OPTIONS)
