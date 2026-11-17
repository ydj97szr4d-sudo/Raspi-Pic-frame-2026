"""The Hindi calendar's lunisolar arithmetic, checked against the festival
dates on the Army Postal Service Corps 2027 (Delhi) calendar."""

from __future__ import annotations

import datetime
import importlib.util
import re
from pathlib import Path

COMP = Path(__file__).resolve().parent.parent / "packaging" / "companions"
_spec = importlib.util.spec_from_file_location("panchang", COMP / "panchang.py")
panchang = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(panchang)


def day(y, m, d):
    return panchang.panchang(datetime.date(y, m, d))


def test_known_2027_festival_tithis():
    # (date, month index in MONTHS, paksha, tithi within the paksha)
    cases = [
        ((2027, 3, 22), 11, panchang.SHUKLA, 15),    # Holika Dahan: Phalguna Purnima
        ((2027, 3, 23), 0, panchang.KRISHNA, 1),     # Holi: Chaitra Krishna 1 (purnimanta)
        ((2027, 4, 7), 0, panchang.SHUKLA, 1),       # Gudi Padwa
        ((2027, 4, 15), 0, panchang.SHUKLA, 9),      # Ram Navami
        ((2027, 5, 20), 1, panchang.SHUKLA, 15),     # Buddha Purnima
        ((2027, 8, 17), 4, panchang.SHUKLA, 15),     # Raksha Bandhan
        ((2027, 8, 25), 5, panchang.KRISHNA, 8),     # Janmashtami
        ((2027, 9, 4), 5, panchang.SHUKLA, 4),       # Ganesh Chaturthi
        ((2027, 10, 29), 7, panchang.KRISHNA, 15),   # Diwali: Amavasya
        ((2027, 11, 14), 7, panchang.SHUKLA, 15),    # Guru Nanak: Kartik Purnima
    ]
    for (y, m, d), month, paksha, tithi in cases:
        x = day(y, m, d)
        assert (x.month, x.paksha, x.tithi_in_paksha) == (month, paksha, tithi), (y, m, d)


def test_vikram_samvat_turns_at_chaitra_shukla_pratipada():
    assert day(2027, 4, 6).samvat == 2083
    assert day(2027, 4, 7).samvat == 2084
    assert day(2027, 1, 1).samvat == 2083
    assert day(2027, 12, 31).samvat == 2084


def test_adhika_month_2026_not_2027():
    assert day(2026, 5, 20).adhika            # Adhika Jyeshtha
    assert not day(2026, 7, 1).adhika
    d = datetime.date(2027, 1, 1)
    assert not any(panchang.panchang(d + datetime.timedelta(days=i)).adhika
                   for i in range(0, 365, 3))


def test_tithi_names_and_digits():
    assert panchang.tithi_name(15) == "पूर्णिमा"
    assert panchang.tithi_name(30) == "अमावस्या"
    assert panchang.tithi_name(26) == "एकादशी"
    assert panchang.devanagari_digits(2027) == "२०२७"


def test_every_listed_holiday_has_a_hindi_name():
    src = (COMP / "dht_mqtt.py").read_text(encoding="utf-8")
    names = set(re.findall(r'\(\d+, \d+\): "([^"]+)"', src))
    for name in names:
        for part in name.split(" / "):
            assert part.strip() in panchang.FESTIVALS_HI, part
