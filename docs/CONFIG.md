# Configuration reference

*Generated from the code by `tools/gen_config_docs.py` — do not edit by hand.*

The file lives at `~/.config/picframe3/config.yaml`. Every key is optional:
anything you leave out uses the default below, and an unknown key produces a
warning in the log rather than a crash. A fully commented starting point is in
[`config/picframe3.example.yaml`](../config/picframe3.example.yaml).

Every setting here also appears in the **Settings** tab of the web interface —
the page is generated from the same definitions as this file, so neither can
fall behind the code. That tab opens on a handful of cards, one per job
(*how fast pictures change*, *where the photographs come from*), each holding
the few settings that job needs; **All settings** at the foot of it is the
whole list below, grouped the same way. Settings marked *(advanced)* are
behind the “Show advanced” switch there. Anything can also be set from MQTT,
or with `picframe3 config --set slideshow.interval=90`.

## `display`

Which screen to use and how hard to drive it.

| key | type | default | |
|---|---|---|---|
| `backend` | string | `'auto'` | `auto` uses the screen if there is one, otherwise renders offscreen. *(takes effect on restart)* |
| `device` | str | None | `None` | `/dev/dri/card1`. Empty takes the first card with a connected output. *(takes effect on restart)* *(advanced)* |
| `connector` | str | None | `None` | `HDMI-A-1`, `HDMI-A-2`, `DSI-1`… Empty takes the first connected output. *(takes effect on restart)* *(advanced)* |
| `mode` | string | *(empty)* | `3840x2160@30`, `1920x1080`. Empty takes the mode the screen says it prefers, which is almost always right — name one when it is not: a 4K television asks for 2160p60, which a Pi 4 cannot drive without `hdmi_enable_4kp60` in `config.txt`. `picframe3 doctor` lists what this screen offers. *(takes effect on restart)* |
| `rotate` | integer | `0` | 0 or 180. For a quarter turn rotate in the kernel (`video=HDMI-A-1:1080x1920M@60,rotate=90`) so the Pi reports a portrait mode. |
| `vsync` | boolean | `True` | Page-flip on the vertical blank. Turning it off tears; it exists for debugging. *(takes effect on restart)* *(advanced)* |
| `fps_limit` | number | `60.0` | Only applies while something is animating; a still picture draws no frames. *(takes effect on restart)* |
| `background` | list of numbers | `[0.0, 0.0, 0.0, 1.0]` | Red, green, blue, alpha, each 0–1. Shown around a picture that does not fill the screen. *(advanced)* |
| `brightness` | number | `1.0` | 0–1, applied in the shader and to the backlight if there is one. |

## `slideshow`

Pacing and sequencing: how long each picture stays, how it changes, what comes next.

| key | type | default | |
|---|---|---|---|
| `interval` | number | `180.0` | Seconds each picture stays on screen. |
| `transition` | string | `'fade'` | Any name from `picframe3 transitions`, or `random`. |
| `transition_time` | number | `2.5` | Seconds the change from one picture to the next takes. |
| `order` | string | `'shuffle'` | `shuffle` gives every picture exactly one turn per round, in a fresh random order each round, and keeps the round in the index — so reboots, rescans and newly copied photographs cannot rob the pictures still waiting. `random` has no memory and may repeat. The order last picked on the web page or in Home Assistant is kept through a restart, even if it was never saved; this value is where a new frame starts, and editing it by hand overrides the remembered choice. |
| `recent_days` | integer | `7` | Pictures newer than this are shown first in each shuffle round. |
| `reshuffle_after` | integer | `1` | Complete passes over the library before the order is shuffled again. |
| `portrait_pairs` | boolean | `False` | Two upright photographs side by side on a landscape screen, each with its own caption. |
| `shuffle` | boolean | `True` | Kept only so a migrated picframe configuration still loads. It is ignored: `order` alone decides. *(advanced)* |
| `paused` | boolean | `False` | Whether the frame starts paused. *(advanced)* |
| `video_loop` | boolean | `False` | Repeat a short video until the picture interval is up, instead of moving on when it ends. |
| `video_mute` | boolean | `True` | Videos are silent by default; a frame on a shelf that suddenly talks is startling. |
| `video_max_seconds` | number | `0.0` | Cut every video off after this many seconds. 0 plays each one to the end, however long it is — a video is never cut short by the picture interval. |
| `video_engine` | string | `'gstreamer'` | `gstreamer` decodes into the frame's own renderer, so crossfades, Ken Burns and overlays keep working over video — but every frame costs a Python-side copy and a GL upload, which on some boards is slow enough to be visibly choppy regardless of resolution. `vlc` hands the whole screen to VLC's own direct-to-display output for the clip, which is smooth at any resolution but blanks out every overlay and transition for as long as the clip plays: no crossfade in or out, no clock or weather line, no Ken Burns. Needs `cvlc` or `vlc` installed, and falls back to `gstreamer` on its own if that is missing or the display backend cannot hand off control. |
| `kenburns` | boolean | `False` | Slow pan and zoom across each picture. |
| `kenburns_zoom` | number | `1.12` | How far in the pan starts, e.g. 1.12 = 12% larger than the screen. |
| `transition_choices` | list of strings | `[]` | Which transitions `transition: random` may draw from. None ticked means the standard pool. |

## `viewer`

How a picture is composed on screen, and what is written over it.

| key | type | default | |
|---|---|---|---|
| `fit` | string | `'auto'` | How a picture is placed on the screen. `auto` decides by shape and is what most frames want. |
| `detect_faces` | boolean | `False` | When a picture is cropped to fill the screen, move the crop only if the ordinary fixed one would have cut into a face -- a face already well clear of the crop is left as it was. Detection only -- it finds where a face-shaped region is, never whose. Needs OpenCV installed (`pip install picframe3[faces]`); harmlessly ignored without it. Runs once per photograph in the background over the next few minutes, not on every slide, so a large library catches up gradually rather than stalling the frame. |
| `fit_choices` | list of strings | `['mat']` | What `fit: auto` may do with a picture whose shape does not match the panel. Several means "pick one of these", chosen from the file path so a given photograph always looks the same. A picture that already matches the panel is shown edge to edge regardless, because that crops nothing. |
| `blur_amount` | number | `20.0` | Radius of the blurred backdrop behind a letterboxed picture. |
| `blur_zoom` | number | `1.06` | How far the blurred backdrop is enlarged, so its edges are off screen. *(advanced)* |
| `blur_dim` | number | `0.55` | 0 = black edges, 1 = the blurred copy at full strength. |
| `upscale_limit` | number | `2.5` | Beyond this the frame blur-fills rather than enlarging a small picture. |
| `max_decode_megapixels` | number | `64.0` | The largest picture the frame will open. A photograph from a camera or a phone is scaled down while it is decoded and never reaches this; a huge PNG or TIFF scan cannot be, and would take the frame down with it, so past this size it is skipped and the log says so. 0 turns the limit off. *(advanced)* |
| `shrink_oversized` | boolean | `True` | What happens to a picture past that size. On, the frame opens it once in a separate process and keeps a screen-sized copy in its cache, so the photograph is shown from then on; the original is never moved, changed or deleted. Off leaves such a picture out of the slideshow. *(advanced)* |
| `mat_style` | string | `'single'` | Tick several and each picture gets one of them. `polaroid` deliberately sits the print high with a deep lower margin; if pictures look badly centred, this style is usually why. |
| `mat_tolerance` | number | `0.01` | How different the picture and screen shapes must be before `auto` mats. `-1` always mats. |
| `mat_outer_color` | list[int] | None | `None` | Red, green, blue, 0–255. Empty derives the colour from the photograph. |
| `mat_inner_color` | list[int] | None | `None` | Red, green, blue, 0–255. Empty is a darker shade of the outer mat. |
| `mat_outer_border` | integer | `75` | Width of the board, in pixels at 1080p, scaled to your panel. |
| `mat_inner_border` | integer | `22` | Width of the inner mat, for the double styles. |
| `mat_texture` | boolean | `True` | Paper grain on the outer board — picframe's own board scan. |
| `mat_inner_texture` | boolean | `False` | Usually off: smooth card against the textured board. |
| `mat_auto_inner_color` | boolean | `True` | Off makes the inner mat a darker shade of the outer instead of its own colour from the photograph. |
| `mat_bevel_width` | integer | `5` | The chamfered cut of the opening, in pixels at 1080p, for the `*_bevel` styles. |
| `font` | str | None | `None` | Path to a .ttf. Empty finds DejaVu, Noto or Liberation. |
| `show_text` | list of strings | `['title', 'caption', 'date', 'location']` | What is written over the picture, in the order it is written. |
| `text_size` | integer | `34` | Caption type size, in pixels at 1080p. |
| `text_seconds` | number | `16.0` | How long the caption stays up after each change. 0 writes nothing by itself — the caption then appears only when you ask for it. |
| `peek_seconds` | number | `40.0` | How long the caption stays up when you *ask* for it — the Home Assistant button, the `i` key — rather than the seconds it gets by itself. A deliberate look wants longer than a glance. |
| `text_justify` | string | `'L'` | Left, centred or right. A pair of portraits always centres each caption under its own picture. |
| `text_opacity` | number | `1.0` | 0–1. *(advanced)* |
| `text_margin_x` | integer | `64` | Gap from the side of the screen to the caption. *(advanced)* |
| `text_margin_y` | integer | `36` | Gap above and below the caption inside its band. *(advanced)* |
| `pair_text_bottom_margin` | integer | `0` | Lift a portrait pair's caption band this many pixels clear of the very bottom of the screen. A pair's captions always span the full width at the bottom (text_position does not apply to them), which can land on top of anything else anchored to the bottom edge — a weather or status overlay from a companion script, say. 0 leaves it flush with the edge, as before. |
| `text_scrim` | number | `0.5` | Darkening behind the caption so it stays legible over a bright picture. 0–1. |
| `text_panel_opacity` | number | `0.0` | A rounded panel behind the caption text, sized to the text itself rather than the full width (unlike the darkening above) — the same look the calendar/clock/status overlay widgets have. 0 (default) draws no panel; the two can be used together. |
| `text_panel_color` | list of integers | `[0, 0, 0]` | Red, green, blue, 0–255. |
| `text_panel_padding_x` | integer | `28` | Gap, in pixels, between the caption text and the panel's left/right edge. Only matters when text_panel_opacity > 0. |
| `text_panel_padding_y` | integer | `16` | Gap, in pixels, between the caption text and the panel's top/bottom edge. Set separately from the horizontal gap since a caption is usually wide and short. Only matters when text_panel_opacity > 0. |
| `text_panel_min_width_pct` | number | `0.0` | Stretches the panel to at least this percent of the screen's width when the text is narrower — the text stays aligned inside per text_justify, only the panel widens. 0 leaves it hugging the text. Only matters when text_panel_opacity > 0. |
| `text_position` | string | *(empty)* | "" keeps the caption where it has always been: a full-width band at the bottom. One of TL/TC/TR/BL/BC/BR (same scheme as the clock) puts it in a corner instead — only for a single caption, not a portrait pair's two side-by-side ones, which always keep the full-width bottom band. |
| `text_offset_pct` | list of numbers | `[3.0, 3.0]` | Inset from the corner, percent of screen width/height — [x, y]. Only matters when text_position is set. |
| `text_separator` | string | `'  ·  '` | Written between the caption elements. |
| `date_format` | string | `'%-d %B %Y'` | strftime: `%-d %B %Y` is "7 September 2026", `%d.%m.%Y` is "07.09.2026". |
| `locale` | string | *(empty)* | Which language month and day names come out in: `de_DE.UTF-8`, `fr_FR.UTF-8`. Empty uses the system's own, which under systemd is usually English whatever the Pi is set to. A language has to be built on the Pi before it works, and only the one chosen here is built: the installer does it, or `picframe3 setup --yes` after choosing a new one here. `doctor` says whether it is. Only the dates change — the system, the installer and this page stay in English. |
| `show_clock` | boolean | `False` | A large clock over the picture. |
| `clock_format` | string | `'%H:%M'` | strftime: `%H:%M` or `%-I:%M %p`. |
| `clock_size` | integer | `120` | Type size, in pixels at 1080p. Also sets the size of the weather/sensor line drawn over the picture (temperature, humidity, AQI, etc.), which scales off this same value even when viewer.show_clock itself is off. |
| `clock_position` | string | `'TR'` | Top or bottom, left, centre or right. |
| `clock_opacity` | number | `0.9` | 0–1. |
| `clock_offset_pct` | list of numbers | `[3.0, 3.0]` | How far in from the corner, as a percentage of the screen: across, then down. |
| `clock_extra_file` | string | `'/dev/shm/picframe-clock.txt'` | If this file exists its contents are written under the time, much smaller — a weather line, a countdown, anything that writes to it. *(advanced)* |
| `overlay_image` | string | `'/dev/shm/picframe-overlay.png'` | If this PNG exists it is drawn over the picture, under the caption. A hook for anything. *(advanced)* |
| `no_files_img` | string | *(empty)* | Shown when there is nothing to show. Empty is the picture that ships with the frame; a path is your own; `none` draws a plain screen naming the folders it looked in. |

## `library`

Where the photographs are and how they are indexed.

| key | type | default | |
|---|---|---|---|
| `picture_folders` | list of strings | `['~/Pictures']` | One per line. `~` is your home directory. *(takes effect on restart)* |
| `database` | string | `'~/.local/share/picframe3/library.db3'` | The index. Moving it starts an empty library. *(takes effect on restart)* *(advanced)* |
| `follow_links` | boolean | `False` | Follow symbolic links while scanning. *(takes effect on restart)* *(advanced)* |
| `include_videos` | boolean | `True` | Index video files as well as photographs. *(takes effect on restart)* |
| `ignore_hidden` | boolean | `True` | Skip dot-files and dot-folders. *(takes effect on restart)* |
| `exclude` | list of strings | `['@eaDir', '.thumbnails', '#recycle']` | Folder names to skip anywhere in the tree. *(takes effect on restart)* |
| `watch` | boolean | `True` | inotify: new photographs appear within seconds. *(takes effect on restart)* |
| `rescan_interval` | number | `3600.0` | Full walk as a backstop behind inotify, in seconds. 0 disables it. *(takes effect on restart)* |
| `scan_on_start` | boolean | `True` | Index at startup. Off is faster to start but new files wait for the watch. *(takes effect on restart)* |
| `deleted_folder` | string | `'~/.local/share/picframe3/deleted'` | Where “Remove” moves a picture. Every removal is written to removals.jsonl in this folder — when it went, where it came from, and what it was. A removed picture also stays out of the slideshow if the file turns up again, under any name, from a sync or a backup; the Removed tab says when that has happened and is where you let it back in. That tab reads the journal, can put a picture back, and can empty the trash: that deletes the files and keeps the lines about them. *(takes effect on restart)* *(advanced)* |
| `edit_backup_folder` | string | `'~/.local/share/picframe3/edit-originals'` | Where the library grid's rotate/crop editor keeps a picture's original the moment before its first edit, so “Undo edit” has something to put back. Edited again without undoing in between, no second backup is made -- there is nowhere further back than the original. *(takes effect on restart)* *(advanced)* |
| `subfolder` | string | *(empty)* | Show only pictures whose path contains this. Pick one of your folders, or type any part of a path. Empty shows everything. |
| `prune_max_fraction` | number | `0.2` | How much of the index one scan may drop, as a fraction. A picture folder on a stick or a share is briefly absent now and then, and every file under it then looks deleted — this is what stops that from throwing away the play history and the place names. 0 removes the safeguard. *(takes effect on restart)* *(advanced)* |

## `sync`

Syncthing — photographs that arrive by themselves from your phone, your Mac or a NAS, without anybody copying anything.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` | Run Syncthing on this frame. Switching it on installs it if it is missing and starts it with the Pi; switching it off stops it and leaves the folder, the pairings and the photographs exactly where they are. |
| `folder_path` | string | *(empty)* | The folder Syncthing keeps in step. Empty means your first picture folder, which is almost always the right answer. |
| `folder_label` | string | `'Picture Frame'` | What this folder is called on your phone and your Mac. *(advanced)* |
| `folder_id` | string | `'picframe3-pictures'` | The id the two sides agree on. Changing it afterwards means pairing the folder again, so leave it alone unless you have a reason. *(advanced)* |
| `folder_type` | string | `'sendreceive'` | **Send & receive** is two-way: photographs arrive, and what the frame does to them travels back — including a removal. **Receive only** takes photographs and never sends a change of its own, so Remove on the frame stays on the frame. **Send only** is the frame handing pictures out and taking none. |
| `versioning_days` | integer | `30` | Days Syncthing keeps its own copy of anything deleted or overwritten in that folder — the safety net under a two-way folder. 0 switches the trash can off. |
| `gui_lan` | boolean | `True` | Syncthing listens on the frame itself out of the box, which on a Pi with no browser means nobody can open its page. On makes it reachable from your own network, the way the frame’s own page is — and, like it, with no password in front of it. |
| `gui_port` | integer | `8384` | The port Syncthing’s own page is served on. 8384 unless something else on the frame already wants it. *(advanced)* |

## `geo`

Turning GPS coordinates into place names for captions.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` | Reverse-geocode GPS coordinates into place names. |
| `contact` | string | *(empty)* | **Required when enabled.** Nominatim's usage policy needs a way to reach you. |
| `language` | string | `'en'` | Two-letter code: the language place names come back in. |
| `cache` | string | `'~/.local/share/picframe3/geocache.db3'` | Nominatim's replies, kept forever. Re-wording place names never costs a request. *(advanced)* |
| `detail` | string | `'full'` | How much of an address a caption shows. Changing it rewrites the names already in the index, from the cache. |
| `suppress` | list of strings | `[]` | Place names never to show — your own country, say. |
| `key_order` | list[list[str]] | `[['tourism', 'attraction', 'amenity', 'isolated_dwelling'], ['neighbourhood', 'suburb', 'village', 'town'], ['city', 'municipality', 'county'], ['state', 'province', 'region'], ['country']]` | Used when **How much of the address** is set to Custom. **One tier per line**, and within a line the keys you would accept for that tier, best first — the first one this particular address actually has is the one written, and the rest of the line is skipped. That is what makes a single setting behave the same in France and in Germany: a French hamlet comes back as `village`, a German one as `isolated_dwelling`, and a line reading `village, isolated_dwelling, town` catches both. |

## `mqtt`

The broker, and the Home Assistant device it announces.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` | Announce the frame to Home Assistant and accept commands. *(takes effect on restart)* |
| `host` | string | *(empty)* | Your broker. Home Assistant's built-in Mosquitto is usually the Home Assistant host itself. *(takes effect on restart)* |
| `port` | integer | `1883` | 1883 plain, 8883 with TLS. *(takes effect on restart)* |
| `username` | string | *(empty)* | Leave empty for an open broker. *(takes effect on restart)* |
| `password` | string | *(empty)* | Stored in the config file in plain text, so keep that file to yourself. *(takes effect on restart)* |
| `tls_ca` | string | *(empty)* | Path to a CA certificate; use port 8883 with it. *(takes effect on restart)* *(advanced)* |
| `tls_insecure` | boolean | `False` | Skip certificate verification. For a self-signed broker on your own LAN. *(takes effect on restart)* *(advanced)* |
| `device_id` | string | `'picframe'` | Identifies this frame. Change it if you have two. *(takes effect on restart)* |
| `device_name` | string | `'Picture Frame'` | What Home Assistant calls the device. *(takes effect on restart)* |
| `discovery_prefix` | string | `'homeassistant'` | `homeassistant` unless you changed it there. *(takes effect on restart)* *(advanced)* |
| `topic_prefix` | string | `'picframe'` | The frame publishes under `<prefix>/<device_id>/…`. *(takes effect on restart)* *(advanced)* |
| `publish_interval` | number | `30.0` | Heartbeat, in seconds. State is also published the moment anything changes. *(takes effect on restart)* *(advanced)* |
| `publish_image` | boolean | `True` | Send the picture itself to Home Assistant, so a dashboard can show what is on the frame. The photograph, not the screen — it is there even while the display is off. |
| `image_width` | integer | `1280` | Longest edge of that picture, in pixels. 1280 looks right on a dashboard and on a phone; larger costs more on every change. *(advanced)* |
| `image_quality` | integer | `82` | JPEG quality for it, 1–100. *(advanced)* |

## `http`

The web interface and REST API.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | This web interface and the REST API. *(takes effect on restart)* |
| `host` | string | `'0.0.0.0'` | `0.0.0.0` listens on every network; `127.0.0.1` only on the frame itself. *(takes effect on restart)* *(advanced)* |
| `port` | integer | `9000` | The port this page is served on. *(takes effect on restart)* |
| `auth_user` | string | *(empty)* | Set this and the password to require a login. *(takes effect on restart)* |
| `auth_password` | string | *(empty)* | Only used when a user name is set. *(takes effect on restart)* |
| `allow_delete` | boolean | `True` | Let the Remove button — and Home Assistant's *Remove current picture* — move pictures out of the library. Off refuses both, and a picture can then only be removed at the frame itself, with a button or a key. Nothing is ever unlinked either way: “remove” means moved to the deleted folder, and the Removed tab can put it back. *(takes effect on restart)* |
| `cors_origins` | list of strings | `[]` | Named sites that may call this API from a browser, e.g. `https://ha.example.com`. Anything listed here can do everything this page can do — skip pictures, change settings, remove photographs — on behalf of anyone who visits it while on your network. Leave it empty unless you are embedding the API somewhere. `*` is refused: it would grant that to every site on the web. *(takes effect on restart)* *(advanced)* |
| `allowed_hosts` | list of strings | `[]` | Extra names this frame answers to. It already answers to `localhost`, to its own hostname and to any address on your own network; add a name here only if you reach it through a reverse proxy or an unusual local domain. A request arriving under some other name is refused, which is what stops a web page from using your browser as a way in. *(takes effect on restart)* *(advanced)* |
| `tls_cert` | string | *(empty)* | Path to a PEM certificate. Both this and tls_key must be set to turn https:// on — `picframe3 enable-https` writes a self-signed pair here for you. Either blank falls back to plain http://. *(takes effect on restart)* *(advanced)* |
| `tls_key` | string | *(empty)* | Path to the matching PEM private key. Both this and tls_cert must be set to turn https:// on. *(takes effect on restart)* *(advanced)* |

## `appearance`

How this web page itself looks -- font and accent colour.

| key | type | default | |
|---|---|---|---|
| `font` | string | `'Arial, sans-serif'` | A CSS font-family value, e.g. `Arial, sans-serif` or `Georgia, serif`. Blank keeps the page's own built-in typeface. |
| `accent_color` | list of integers | `[37, 99, 235]` | Red, green, blue, 0–255 — replaces the colour used for links, buttons and highlights across the page. Empty keeps the built-in accent. |

## `input`

Keyboard, touchscreen, mouse and GPIO buttons.

| key | type | default | |
|---|---|---|---|
| `keyboard` | boolean | `True` | A keyboard plugged into the Pi, read straight from evdev. *(takes effect on restart)* |
| `touch` | boolean | `True` | A touchscreen: tap right half for next, left for previous. *(takes effect on restart)* |
| `mouse` | boolean | `False` | Off by default; a stray mouse should not skip pictures. *(takes effect on restart)* |
| `gpio_buttons` | mapping | `{}` | BCM pin numbers, e.g. `{"next": 17, "pause": 27}`. *(takes effect on restart)* |
| `gpio_pull_up` | boolean | `True` | Buttons wired to ground, which is the usual way. *(takes effect on restart)* *(advanced)* |
| `wake_on_input` | boolean | `True` | Any button press turns the screen back on. *(takes effect on restart)* |
| `keymap` | mapping | `{'next': ['KEY_RIGHT', 'KEY_SPACE', 'KEY_DOWN'], 'previous': ['KEY_LEFT', 'KEY_UP'], 'pause': ['KEY_P'], 'display_toggle': ['KEY_O'], 'info_toggle': ['KEY_I'], 'clock_toggle': ['KEY_C'], 'delete': ['KEY_DELETE'], 'quit': ['KEY_ESC', 'KEY_Q']}` | evdev key names per action, e.g. `KEY_RIGHT`. *(takes effect on restart)* *(advanced)* |

## `power`

When the screen is on, off, or dimmed.

| key | type | default | |
|---|---|---|---|
| `schedule` | mapping | `{}` | `{"all": ["22:30-07:00"]}` — ranges may cross midnight, and weekday names work in place of `all`. |
| `dim_schedule` | mapping | `{}` | `{"19:00-22:30": 0.45}` — brightness, not on/off. |
| `enabled` | boolean | `True` | Named `enabled`, not `on`: YAML reads a bare `on:` key as a boolean. |

## `health`

What the frame reports about the Pi it runs on — temperature, load, memory, free space and the power supply.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Measure the Pi’s temperature, load, memory and free space, and report them to Home Assistant and this page. |
| `interval` | number | `30.0` | Seconds between readings. Taken on a background thread, so it never interrupts a transition. *(advanced)* |
| `disk_path` | string | *(empty)* | Which disk the free-space reading is about. Empty means your first picture folder — the one that actually fills up. *(advanced)* |

## `network`

Watching the frame’s own link to the house, and mending it when it breaks.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Check every so often that the frame can still reach the house, and say so in Home Assistant. |
| `target` | string | *(empty)* | Empty means your router, found automatically. Never put an address on the internet here — the frame would mend a link that is not broken. |
| `interface` | string | *(empty)* | Empty means whichever interface the frame actually uses. *(advanced)* |
| `interval` | number | `60.0` | Seconds between checks. A minute is plenty — the frame is looking for an outage, not measuring latency. |
| `attempts` | integer | `3` | Ping runs per check. One lost packet is not an outage; needing all of them to fail is what makes a failed check mean something. *(advanced)* |
| `timeout` | number | `3.0` | How long to wait for a reply before that run counts as lost. *(advanced)* |
| `failures` | integer | `3` | Three checks a minute apart is several minutes of real silence, not one lost packet. |
| `repair` | boolean | `True` | Off watches and reports but never touches the connection. |
| `cooldown` | number | `1800.0` | The safety catch: however bad it looks, never mend more than once in this window. *(advanced)* |
| `settle` | number | `45.0` | After mending, wait this long before checking again — a link that has just come back needs a moment to finish coming back. *(advanced)* |

## `logging`

Where log output goes.

| key | type | default | |
|---|---|---|---|
| `level` | string | `'INFO'` | DEBUG · INFO · WARNING · ERROR |
| `file` | string | *(empty)* | Empty logs to the journal only. *(takes effect on restart)* *(advanced)* |
| `journald` | boolean | `True` | Log through systemd, so `journalctl -u picframe3@pi` works. *(takes effect on restart)* *(advanced)* |

## `motion`

The motion sensor that puts the screen to sleep and wakes it again.

| key | type | default | |
|---|---|---|---|
| `idle_timeout` | number | `900.0` | Seconds with no detected motion before the screen is put to sleep. The weather caption's dot grid counts down this same span, so the two always agree. |

## `video_proxy`

Keeping videos inside what a Raspberry Pi 4 can play smoothly.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Off leaves every video exactly as it is. Any proxy already made by this watcher is left alone either way. |
| `long_side_max` | integer | `1920` | The longer edge of the box a video must fit inside, in pixels. A clip already smaller than the box is left untouched. |
| `short_side_max` | integer | `1080` | The shorter edge of that same box — together with the longer edge above, this is one box, whichever way a clip is oriented (e.g. 1920×1080 for a landscape clip, 1080×1920 for a portrait one). |
| `video_bitrate` | string | `'8M'` | Target video bitrate passed straight to ffmpeg, e.g. `8M`. *(advanced)* |
| `audio_bitrate` | string | `'160k'` | Target audio bitrate passed straight to ffmpeg, e.g. `160k`. *(advanced)* |
| `scan_interval` | number | `300.0` | Seconds between sweeps of the picture library looking for a new or oversized video. *(advanced)* |

## `overlay`

The weather caption and its idle-countdown dot grid.

| key | type | default | |
|---|---|---|---|
| `dot_count` | integer | `16` | Dots in the idle-countdown grid, arranged as a square — use a perfect square (4, 9, 16, 25…). 16 draws a 4×4 grid. |
| `gap_to_text` | integer | `14` | Gap, in pixels, between the dot grid and the weather caption text it sits beside. *(advanced)* |
| `filled_alpha` | integer | `235` | Opacity (0–255) of a dot that still represents time left before the screen sleeps. *(advanced)* |
| `unfilled_alpha` | integer | `60` | Opacity (0–255) of a dot already counted down. *(advanced)* |
| `background_color` | list of integers | `[0, 0, 0]` | Red, green, blue, 0–255. The panel drawn behind the weather caption and its dot grid together — the same look the calendar/clock/status widgets have. |
| `background_opacity` | number | `0.35` | 0 (no panel at all — the original drop-shadowed text, no box) – 1 (solid panel). |
| `background_padding` | integer | `20` | Gap, in pixels, between the caption text/dot grid and the panel's own edge. Only matters when background_opacity > 0. *(advanced)* |

## `calendar`

The upper-left calendar widget: today's date and a small month grid.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Off removes the widget entirely. |
| `offset_pct` | list of numbers | `[3.0, 3.0]` | Inset from the top-left corner, percent of screen width/height — [x, y]. |
| `size` | integer | `34` | Size of the “today” line (e.g. “Saturday, 17 October”), in real screen pixels (drawn directly at your screen's own resolution, not through PicFrame3's scaled renderer). |
| `grid_size` | integer | `20` | Size of the month grid's day numbers and weekday-header row (M T W T F S S), in real screen pixels — independent of the “today” line size above. |
| `grid_row_gap` | integer | `0` | Extra vertical space (px) between every row of the grid — between the weekday-header row and the first week, and between each week after that. 0 packs the rows at their natural height. |
| `square_padding` | integer | `1` | How many pixels smaller the “today” highlight square is than its grid cell, per side. 0 fills the cell edge to edge; a larger number leaves a visible gap around the mark. |
| `text_color` | list of integers | `[255, 255, 255]` | Red, green, blue, 0–255. The date text, weekday headers and day numbers. |
| `text_opacity` | number | `0.9` | 0–1. |
| `background_color` | list of integers | `[0, 0, 0]` | Red, green, blue, 0–255. The panel drawn behind the whole widget, so the text stays legible over a busy photograph. |
| `background_opacity` | number | `0.35` | 0 (no panel at all — text floats directly over the picture) – 1 (solid panel). |
| `holiday_color` | string | `'dull_red'` | Colour of Sundays, holiday dates and the holiday list below the grid — picked from a fixed palette rather than mixed by hand. |

## `clock`

A custom-drawn clock with its own translucent panel.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `False` | Off removes the widget entirely. Turn off viewer.show_clock too if it's on, so you don't get two clocks. |
| `position` | string | `'TR'` | Which corner it's inset from. |
| `offset_pct` | list of numbers | `[3.0, 3.0]` | Inset from that corner, percent of screen width/height — [x, y]. |
| `size` | integer | `64` | Size of the time text, in real screen pixels. |
| `format_24h` | boolean | `True` | On: 24-hour (“14:07”). Off: 12-hour (“2:07 PM”). |
| `text_color` | list of integers | `[255, 255, 255]` | Red, green, blue, 0–255. |
| `text_opacity` | number | `0.9` | 0–1. |
| `background_color` | list of integers | `[0, 0, 0]` | Red, green, blue, 0–255. The panel drawn behind the time text. |
| `background_opacity` | number | `0.35` | 0 (no panel at all) – 1 (solid panel). |
| `panel_padding_x` | integer | `40` | Gap, in pixels, between the time text and the panel's left/right edge. Only matters when background_opacity > 0. |
| `panel_padding_y` | integer | `16` | Gap, in pixels, between the time text and the panel's top/bottom edge. Set separately from the horizontal gap, since the clock's text is wide but only one line tall. Only matters when background_opacity > 0. |

## `status`

A small status strip -- outdoor sensor link, and more to come.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Off removes the strip entirely. |
| `position` | string | `'TR'` | Which corner it's inset from. |
| `offset_pct` | list of numbers | `[3.0, 3.0]` | Inset from that corner, percent of screen width/height — [x, y]. |
| `size` | integer | `26` | Size of the status text, in real screen pixels. |
| `text_color` | list of integers | `[255, 255, 255]` | Red, green, blue, 0–255. |
| `text_opacity` | number | `0.9` | 0–1. |
| `background_color` | list of integers | `[0, 0, 0]` | Red, green, blue, 0–255. The panel drawn behind the strip. |
| `background_opacity` | number | `0.35` | 0 (no panel at all) – 1 (solid panel). |

## `brand`

The small animated maker's mark in a corner of the screen.

| key | type | default | |
|---|---|---|---|
| `enabled` | boolean | `True` | Off removes the mark entirely. *(takes effect on restart)* |
| `position` | string | `'BR'` | Which corner it's inset from. *(takes effect on restart)* |
| `offset_pct` | list of numbers | `[3.0, 3.0]` | Inset from that corner, percent of screen width/height — [x, y]. *(takes effect on restart)* |
| `size` | integer | `160` | Size of the mark, in real screen pixels (its longer side). *(takes effect on restart)* |
| `opacity` | number | `0.9` | 0 (invisible) – 1 (fully opaque), applied to the whole mark. *(takes effect on restart)* |
| `animated` | boolean | `True` | On: it runs its forming/dissolving animation. Off: a plain static mark, no motion. *(takes effect on restart)* |
| `styles` | list of strings | `['dots', 'scanline', 'pop', 'glitch', 'focus']` | Which cycles are in rotation -- one is picked at random each time the previous one finishes. Empty means every style. *(takes effect on restart)* |

