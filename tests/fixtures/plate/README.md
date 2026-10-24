# Plate-contents fixtures

One plate in every file: **4 x "Hose Clip" (a 10 mm cube) + 2 x "Hook" (a 15 mm cylinder)**, sliced with the slicer's own
command line on 2026-10-18 and then trimmed. They hold no names, hosts, serials or paths (checked with grep before commit).

| File | Made with | Settings that decide the labels | What the slicer wrote |
|---|---|---|---|
| `prusa_mk4s_m486.gcode` | PrusaSlicer 3.0.0-alpha11, Prusa MK4S profile (Marlin 2) | Label objects = **Firmware** | `M486 S<n>` / `M486 A<name>` / `M486 S-1` block before the first layer; `; objects_info = {...}` in the config block |
| `prusa_mk4s_octoprint.gcode` | same | Label objects = **OctoPrint** | `; printing object <name> id:<n> copy <k>` + `; stop printing object ...` pairs before the first layer (and again inside every layer); `; objects_info` |
| `prusa_mk4s_nolabels.gcode` | same | Label objects = **Disabled** | nothing in the head; `; gcode_label_objects = disabled` and `; objects_info = {"objects":[]}` at the end |
| `prusa_klipper_exclude.gcode` | same, flavor = Klipper | Label objects = Firmware | `EXCLUDE_OBJECT_DEFINE NAME='Hose_Clip_stl' CENTER=... POLYGON=...` before the first layer. Names lose their dots and spaces; copies of one object may carry the SAME name, so lines are counted, names are not de-duplicated |
| `orca_voron_exclude.gcode` | OrcaSlicer 2.4.2, Voron 2.4 350 profile (Klipper) | Exclude objects = on, Label objects = on | `EXCLUDE_OBJECT_DEFINE NAME=Hose_Clip.stl_id_2_copy_0 ...` before the first layer, plus `; printing object Hose Clip.stl id:2 copy 0` comments with the readable name inside each layer |
| `orca_voron_nolabels.gcode` | same | Exclude objects = off, Label objects = off | nothing; `; exclude_object = 0`, `; gcode_label_objects = 0` in the config block |
| `orca_prusa_m486.gcode` | OrcaSlicer 2.4.2, Prusa MK4S profile (Marlin) | Label objects = on | `M486 S<n>` / `M486 A<name>_id_<n>_copy_<k>` block before the first layer, `; printing object` comments inside layers |
| `bambu_p1s_slice_info.config` | Bambu Studio 02.07, P1S 0.4 nozzle | (always on in Bambu Studio) | `<object identify_id name skipped>` per object in `Metadata/slice_info.config`; `; start printing object, unique label id: <n>` in the plate G-code |
| `bambu_p1s_nolabels_slice_info.config` | **hand-edited** copy of the file above with the `<object>` lines deleted | - | a slice_info with no objects (Bambu Studio itself never produced one: it lists the objects even with `exclude_object = 0`) |
| `prusa_mk4s_synthetic.bgcode`, `prusa_mk4s_nolabels_synthetic.bgcode` | **hand-built** from the published libbgcode layout (header `GCDE`, file / printer / print / slicer metadata blocks, one G-code block) with the values of the real files above | - | PrusaSlicer 3.0 alpha would not write a binary file from its command line, so these are modelled, not captured. `objects_info` is assumed to sit in the metadata like any other key (the string is in the slicer binary twice, once as `; objects_info = %s` for text G-code and once bare). The real G-code blocks (heatshrink + MeatPack) are not decoded by the Hub |

## What was trimmed

Every `.gcode` fixture keeps: the file header up to the first `;LAYER_CHANGE`, the label lines of layer 1 (`;` comments,
`EXCLUDE_OBJECT*`, `M486`) with its moves removed, the second `;LAYER_CHANGE` marker (so "the first layer was read from first line to
last" holds), then the config-block lines a reader looks at (`objects_info`, `gcode_flavor`, `gcode_label_objects`,
`exclude_object`, `filament_type`, `layer_height`, `nozzle_diameter`, `printer_model`) between the original block markers. Layers 2..n and
all moves are gone. Real files are 0.7 - 1.1 MB.

## Quirks worth knowing

* PrusaSlicer 3.0 alpha's `--merge` gave every object the same `id:6 copy 0`, so labels alone cannot tell the copies apart there: the
  reader counts the pre-layer pairs, not distinct ids.
* OrcaSlicer's Klipper names replace spaces with `_`; the reader puts the spaces back for Klipper and M486 names.
* The `; objects_info = ...` line is the only label source in the *config block* (end of the file) - useful when the head read stops early.
* PrusaSlicer 2.1 (a 2019 MK3S file inspected read-only) has `; gcode_label_objects = 0` and no `objects_info`: "labels off" is visible there too.
