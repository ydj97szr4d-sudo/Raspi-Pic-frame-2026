"""The clock faces the picture frame's Face tile offers: id -> label, in menu order.

The drawing code is packaging/companions/faces.py (installed beside dht_mqtt.py); this is
only the menu, so the settings page and the config can name a face without importing the
renderer. tests/test_facetile.py checks the two stay in step.
"""

FACE_LABELS = {
    'dark': 'Dark',
    'light-minimal': 'Light minimal',
    'light': 'Light',
    'digital': 'Digital',
    'wide': 'Wide',
    'purple': 'Purple',
    'teal': 'Teal',
    'white-square': 'White square',
    'calendar': 'Calendar',
    'digital-orange': 'Digital orange',
    'wide-minimal': 'Wide minimal',
    'gradient': 'Gradient',
    'banner': 'Banner',
    'paper': 'Paper',
    'classic': 'Classic',
    'glass': 'Glass',
    'sunset': 'Sunset',
    'stacked': 'Stacked',
    'rugged': 'Rugged',
    'night': 'Night',
}

FACE_IDS = list(FACE_LABELS)
