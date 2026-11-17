"""Hindu lunisolar calendar (panchang) arithmetic for the Hindi calendar widget.

Pure Python, no third-party imports, so it works on the Pi with nothing
installed beyond the interpreter and is trivially unit-testable.

Everything here is *computed* from the positions of the sun and moon
(Meeus, "Astronomical Algorithms": the low-precision solar longitude and the
principal terms of the lunar longitude, good to about 0.01 degree -- a minute
or so of tithi end time), not looked up from a printed table, so any year
works. What it follows is the North Indian convention the printed wall
calendars use:

* the tithi of a day is the one running at local sunrise (the *udaya* tithi);
* months are *purnimanta* -- they end on the full moon, so the dark half of
  a lunation already belongs to the next month's name;
* the Vikram Samvat year turns over on Chaitra Shukla Pratipada;
* an *adhika* (leap) month is a lunation with no solar ingress in it.

Where a printed calendar departs from the udaya rule (a tithi that is
skipped between two sunrises, or the occasional day the printers shift a
fast by one day) this can differ by a day. Festival *dates* on the widget
come from the gazetted lists in dht_mqtt.py, not from here, for that reason.
"""

from __future__ import annotations

import datetime
import math

#: Where "sunrise" is taken. Delhi, to match the Delhi calendars this mirrors.
LATITUDE = 28.6139
LONGITUDE = 77.2090
#: IST. A frame is assumed to live in India; the Hindi widget is for that.
UTC_OFFSET_HOURS = 5.5

MONTHS = ["चैत्र", "वैशाख", "ज्येष्ठ", "आषाढ़", "श्रावण", "भाद्रपद",
          "आश्विन", "कार्तिक", "मार्गशीर्ष", "पौष", "माघ", "फाल्गुन"]

_TITHI_NAMES = ["प्रतिपदा", "द्वितीया", "तृतीया", "चतुर्थी", "पंचमी", "षष्ठी",
                "सप्तमी", "अष्टमी", "नवमी", "दशमी", "एकादशी", "द्वादशी",
                "त्रयोदशी", "चतुर्दशी"]

SHUKLA = "शुक्ल"
KRISHNA = "कृष्ण"
ADHIKA = "अधिक"


def tithi_name(tithi: int) -> str:
    """Name of tithi 1..30 (15 = Purnima, 30 = Amavasya)."""
    if tithi == 15:
        return "पूर्णिमा"
    if tithi == 30:
        return "अमावस्या"
    return _TITHI_NAMES[(tithi - 1) % 15]


# --------------------------------------------------------------------------
# Astronomy

def julian_day(dt_utc: datetime.datetime) -> float:
    """Julian Day of a naive UTC datetime (Gregorian calendar)."""
    y, m = dt_utc.year, dt_utc.month
    d = dt_utc.day + (dt_utc.hour + (dt_utc.minute + dt_utc.second / 60) / 60) / 24
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def _centuries(jd: float) -> float:
    return (jd - 2451545.0) / 36525.0


def sun_longitude(jd: float) -> float:
    """Apparent geocentric tropical longitude of the sun, degrees 0..360."""
    t = _centuries(jd)
    l0 = 280.46646 + 36000.76983 * t
    m = math.radians(357.52911 + 35999.05029 * t)
    c = ((1.914602 - 0.004817 * t) * math.sin(m)
         + 0.019993 * math.sin(2 * m) + 0.000289 * math.sin(3 * m))
    omega = math.radians(125.04 - 1934.136 * t)
    return (l0 + c - 0.00569 - 0.00478 * math.sin(omega)) % 360.0


# (D, M, M', F, coefficient in 1e-6 degree) -- Meeus table 47.A, longitude.
_MOON_TERMS = [
    (0, 0, 1, 0, 6288774), (2, 0, -1, 0, 1274027), (2, 0, 0, 0, 658314),
    (0, 0, 2, 0, 213618), (0, 1, 0, 0, -185116), (0, 0, 0, 2, -114332),
    (2, 0, -2, 0, 58793), (2, -1, -1, 0, 57066), (2, 0, 1, 0, 53322),
    (2, -1, 0, 0, 45758), (0, 1, -1, 0, -40923), (1, 0, 0, 0, -34720),
    (0, 1, 1, 0, -30383), (2, 0, 0, -2, 15327), (0, 0, 1, 2, -12528),
    (0, 0, 1, -2, 10980), (4, 0, -1, 0, 10675), (0, 0, 3, 0, 10034),
    (4, 0, -2, 0, 8548), (2, 1, -1, 0, -7888), (2, 1, 0, 0, -6766),
    (1, 0, -1, 0, -5163), (1, 1, 0, 0, 4987), (2, -1, 1, 0, 4036),
    (2, 0, 2, 0, 3994), (4, 0, 0, 0, 3861), (2, 0, -3, 0, 3665),
    (0, 1, -2, 0, -2689), (2, 0, -1, 2, -2602), (2, -1, -2, 0, 2390),
    (1, 0, 1, 0, -2348), (2, -2, 0, 0, 2236), (0, 1, 2, 0, -2120),
    (0, 2, 0, 0, -2069), (2, -2, -1, 0, 2048), (2, 0, 1, -2, -1773),
    (2, 0, 0, 2, -1595), (4, -1, -1, 0, 1215), (0, 0, 2, 2, -1110),
    (3, 0, -1, 0, -892), (2, 1, 1, 0, -810), (4, -1, -2, 0, 759),
    (0, 2, -1, 0, -713), (2, 2, -1, 0, -700), (2, 1, -2, 0, 691),
    (2, -1, 0, -2, 596), (4, 0, 1, 0, 549), (0, 0, 4, 0, 537),
    (4, -1, 0, 0, 520), (1, 0, -2, 0, -487),
]


def moon_longitude(jd: float) -> float:
    """Geocentric tropical longitude of the moon, degrees 0..360."""
    t = _centuries(jd)
    lp = 218.3164477 + 481267.88123421 * t
    d = math.radians(297.8501921 + 445267.1114034 * t)
    m = math.radians(357.5291092 + 35999.0502909 * t)
    mp = math.radians(134.9633964 + 477198.8675055 * t)
    f = math.radians(93.2720950 + 483202.0175233 * t)
    e = 1 - 0.002516 * t - 0.0000074 * t * t
    total = 0.0
    for kd, km, kmp, kf, coeff in _MOON_TERMS:
        arg = kd * d + km * m + kmp * mp + kf * f
        total += coeff * (e ** abs(km)) * math.sin(arg)
    a1 = math.radians(119.75 + 131.849 * t)
    a2 = math.radians(53.09 + 479264.290 * t)
    total += (3958 * math.sin(a1)
              + 1962 * math.sin(math.radians(lp) - f)
              + 318 * math.sin(a2))
    return (lp + total / 1_000_000.0) % 360.0


def ayanamsa(jd: float) -> float:
    """Lahiri (Chitrapaksha) ayanamsa, degrees. Linear fit around 2000."""
    return 23.853 + 0.013969 * (jd - 2451545.0) / 365.25


def elongation(jd: float) -> float:
    """Moon minus sun, 0..360. 0 = new moon, 180 = full moon."""
    return (moon_longitude(jd) - sun_longitude(jd)) % 360.0


def _signed(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def new_moon_before(jd: float) -> float:
    """Julian Day of the most recent new moon at or before ``jd``."""
    x = jd - elongation(jd) / 12.190749   # mean daily elongation rate
    for _ in range(8):
        x -= _signed(elongation(x)) / 12.190749
    return x


def new_moon_after(jd: float) -> float:
    """Julian Day of the first new moon strictly after ``jd``."""
    prev = new_moon_before(jd)
    return new_moon_before(prev + 29.6 + 1.0)


def sidereal_rashi(jd: float) -> int:
    """Sidereal zodiac sign the sun is in, 0 = Mesha .. 11 = Meena."""
    return int(((sun_longitude(jd) - ayanamsa(jd)) % 360.0) // 30)


def sunrise_ist_hours(date: datetime.date) -> float:
    """Local (IST) sunrise at LATITUDE/LONGITUDE, hours after midnight."""
    n = date.toordinal() - datetime.date(date.year, 1, 1).toordinal() + 1
    g = 2 * math.pi / 365 * (n - 1 + (6 - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    lat = math.radians(LATITUDE)
    cos_h = ((math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl)))
             - math.tan(lat) * math.tan(decl))
    cos_h = max(-1.0, min(1.0, cos_h))
    ha = math.degrees(math.acos(cos_h))
    utc_minutes = 720 - 4 * (LONGITUDE + ha) - eqtime
    return utc_minutes / 60.0 + UTC_OFFSET_HOURS


def sunrise_jd(date: datetime.date) -> float:
    hours = sunrise_ist_hours(date) - UTC_OFFSET_HOURS
    base = datetime.datetime(date.year, date.month, date.day)
    return julian_day(base) + hours / 24.0


# --------------------------------------------------------------------------
# The panchang for one day

class Day:
    """What the widget needs to know about one civil day."""

    __slots__ = ("date", "tithi", "paksha", "tithi_in_paksha", "month",
                 "adhika", "samvat", "tithi_text", "sankranti")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


_lunation_cache: dict[int, tuple] = {}


def _lunation(jd: float) -> tuple:
    """(start_jd, end_jd, month_index, is_adhika) of the lunation containing
    ``jd`` -- amanta month name, 0 = Chaitra. Cached by lunation start."""
    start = new_moon_before(jd)
    key = round(start)
    hit = _lunation_cache.get(key)
    if hit:
        return hit
    end = new_moon_before(start + 29.6 + 1.0)
    # Sample the sun just *inside* each end: a new moon within hours of a
    # sankranti is where a point sample is at its least safe.
    r0 = sidereal_rashi(start + 0.02)
    r1 = sidereal_rashi(end - 0.02)
    month = (r0 + 1) % 12
    adhika = (r0 == r1)
    result = (start, end, month, adhika)
    _lunation_cache[key] = result
    return result


def panchang(date: datetime.date) -> Day:
    """The panchang (udaya tithi) for ``date``."""
    sr = sunrise_jd(date)
    tithi = int((elongation(sr) // 12.0)) + 1          # 1..30
    dark = tithi > 15
    start, end, month, adhika = _lunation(sr)
    if dark:
        # Purnimanta: the dark half already carries the *next* month's name.
        _, _, month, adhika = _lunation(end + 1.0)
    in_paksha = tithi - 15 if dark else tithi          # 1..15

    # Vikram Samvat turns over on Chaitra Shukla Pratipada.
    samvat = date.year + (57 if date >= _chaitra_shukla_1(date.year) else 56)

    # Solar ingress on this civil day: the sun's sidereal sign differs at the
    # next sunrise. Reports the sign entered (0 = Mesha .. 11 = Meena).
    r_today = sidereal_rashi(sr)
    r_next = sidereal_rashi(sunrise_jd(date + datetime.timedelta(days=1)))

    return Day(date=date, tithi=tithi, paksha=KRISHNA if dark else SHUKLA,
               tithi_in_paksha=in_paksha, month=month, adhika=adhika,
               samvat=samvat, tithi_text=tithi_name(tithi),
               sankranti=r_next if r_today != r_next else None)


_ny_cache: dict[int, datetime.date] = {}


def _chaitra_shukla_1(year: int) -> datetime.date:
    """Civil date of Chaitra Shukla Pratipada in Gregorian ``year`` -- the
    first sunrise after the new moon that starts the Chaitra lunation (the
    first new moon with the sun in sidereal Meena, mid-March to mid-April)."""
    hit = _ny_cache.get(year)
    if hit:
        return hit
    jd = julian_day(datetime.datetime(year, 3, 1))
    found = None
    for _ in range(3):
        nm = new_moon_after(jd)
        # a lunation whose *start* has the sun in Meena (index 11) is Chaitra,
        # and is not adhika if the sun reaches Mesha before it ends.
        _, _, month, adhika = _lunation(nm + 0.5)
        if month == 0 and not adhika:
            found = nm
            break
        jd = nm + 1.0
    if found is None:           # unreachable for any real year; keep safe
        found = new_moon_after(julian_day(datetime.datetime(year, 3, 15)))
    # First civil day whose sunrise is after the new moon.
    day = datetime.date(year, 3, 1)
    while sunrise_jd(day) <= found:
        day += datetime.timedelta(days=1)
    _ny_cache[year] = day
    return day


# --------------------------------------------------------------------------
# Hindi names

GREGORIAN_MONTHS_HI = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई",
                       "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
#: Sunday first, as Indian wall calendars print it.
WEEKDAYS_HI_SUN_FIRST = ["रवि", "सोम", "मंगल", "बुध", "गुरु", "शुक्र", "शनि"]
RASHI_HI = ["मेष", "वृषभ", "मिथुन", "कर्क", "सिंह", "कन्या",
            "तुला", "वृश्चिक", "धनु", "मकर", "कुंभ", "मीन"]
_DEVANAGARI_DIGITS = str.maketrans("0123456789", "०१२३४५६७८९")


def devanagari_digits(value) -> str:
    return str(value).translate(_DEVANAGARI_DIGITS)


#: English holiday name (as it appears in HOLIDAYS_BY_YEAR / RESTRICTED_BY_YEAR
#: in dht_mqtt.py, split on " / ") -> Hindi. A name missing here is shown in
#: English rather than dropped.
FESTIVALS_HI = {
    "Republic Day": "गणतंत्र दिवस", "Holi": "होली", "Id-ul-Fitr": "ईद-उल-फ़ितर",
    "Ram Navami": "राम नवमी", "Mahavir Jayanti": "महावीर जयंती",
    "Good Friday": "गुड फ्राइडे", "Buddha Purnima": "बुद्ध पूर्णिमा",
    "Id-ul-Zuha (Bakrid)": "ईद-उल-ज़ुहा (बकरीद)", "Muharram": "मुहर्रम",
    "Independence Day": "स्वतंत्रता दिवस", "Milad-un-Nabi": "मिलाद-उन-नबी",
    "Janmashtami": "जन्माष्टमी", "Gandhi Jayanti": "गांधी जयंती",
    "Dussehra": "दशहरा", "Diwali": "दीपावली",
    "Guru Nanak's Birthday": "गुरु नानक जयंती", "Christmas Day": "क्रिसमस",
    "New Year's Day": "नववर्ष", "Makar Sankranti": "मकर संक्रांति",
    "Pongal": "पोंगल", "Guru Gobind Singh's Birthday": "गुरु गोबिंद सिंह जयंती",
    "Basant Panchami": "बसंत पंचमी", "Shivaji Jayanti": "शिवाजी जयंती",
    "Guru Ravidas's Birthday": "गुरु रविदास जयंती",
    "Dayananda Saraswati's Birthday": "दयानंद सरस्वती जयंती",
    "Jamat-Ul-Vida": "जमात-उल-विदा", "Maha Shivratri": "महाशिवरात्रि",
    "Holika Dahan": "होलिका दहन", "Dolyatra": "डोल यात्रा",
    "Easter Sunday": "ईस्टर", "Gudi Padava": "गुड़ी पड़वा", "Ugadi": "उगादि",
    "1st Navratra": "प्रथम नवरात्र", "Vaisakhi": "बैसाखी", "Vishu": "विशु",
    "Bahag Bihu": "बोहाग बिहू",
    "Rabindranath Tagore's Birthday": "रवींद्रनाथ टैगोर जयंती",
    "Rath Yatra": "रथ यात्रा", "Parsi New Year": "पारसी नववर्ष",
    "Navroz": "नवरोज़", "Raksha Bandhan": "रक्षा बंधन",
    "Ganesh Chaturthi": "गणेश चतुर्थी", "Onam": "ओणम",
    "Dussehra (Saptami)": "दशहरा (सप्तमी)",
    "Dussehra (Mahashtami)": "दशहरा (महाअष्टमी)",
    "Dussehra (Mahanavami)": "दशहरा (महानवमी)",
    "Valmiki's Birthday": "वाल्मीकि जयंती", "Karwa Chauth": "करवा चौथ",
    "Naraka Chaturdashi": "नरक चतुर्दशी", "Govardhan Puja": "गोवर्धन पूजा",
    "Bhai Duj": "भाई दूज", "Chhat Puja": "छठ पूजा",
    "Guru Teg Bahadur's Martyrdom Day": "गुरु तेग़ बहादुर शहीदी दिवस",
    "Hazrat Ali's Birthday": "हज़रत अली जयंती",
    "Christmas Eve": "क्रिसमस की पूर्व संध्या",
}


def festival_hi(name: str) -> str:
    """Hindi for a (possibly " / "-joined) English holiday name."""
    return " / ".join(FESTIVALS_HI.get(part.strip(), part.strip())
                      for part in name.split(" / "))
