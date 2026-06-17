# Command-burst discipline icons

These four PNGs are the per-discipline icons used by the GUI to represent the four
EVE Online command-burst disciplines. The icon loader maps each discipline to one
of these filenames and falls back to a Unicode glyph if a file is missing or fails
to load.

## Discipline -> filename mapping

| Discipline   | Filename       |
| ------------ | -------------- |
| Shield       | `shield.png`   |
| Armor        | `armor.png`    |
| Skirmish     | `skirmish.png` |
| Information   | `info.png`     |

## Provenance

The committed files are the **real EVE Online module icons** (64x64 PNG),
downloaded from CCP's official image CDN (`images.evetech.net`). They are CCP
game assets and remain CCP's property; they are bundled here only as in-app
discipline icons.

The base tech-1 module of each discipline was used as the icon source (the icon is
shared across meta tiers of a discipline). Type IDs were resolved via the EVE Ref
ref-data API (`https://ref-data.everef.net/groups/1770`, the Command Burst group).

## Resolved type IDs and source URLs

To re-fetch or replace any icon, download from the URL below (already at `size=64`):

| Discipline   | Module (tech-1)              | type_id | Source URL                                                  |
| ------------ | ---------------------------- | ------- | ----------------------------------------------------------- |
| Shield       | Shield Command Burst I       | 42529   | https://images.evetech.net/types/42529/icon?size=64         |
| Armor        | Armor Command Burst I        | 42526   | https://images.evetech.net/types/42526/icon?size=64         |
| Skirmish     | Skirmish Command Burst I     | 42530   | https://images.evetech.net/types/42530/icon?size=64         |
| Information   | Information Command Burst I   | 42527   | https://images.evetech.net/types/42527/icon?size=64         |

Example re-fetch (matches how these were originally pulled):

```python
import requests
r = requests.get("https://images.evetech.net/types/42529/icon?size=64", timeout=20)
r.raise_for_status()
open("assets/bursts/shield.png", "wb").write(r.content)  # repeat per discipline
```
